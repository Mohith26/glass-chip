"""P6 support: a surrogate upper bound on a job's finish time.

PowerManager(policy P6, surrogate=SurrogateFinishBound(...)) calls

    bound(job, domain, freq_hz, now_ps) -> finish_ub_ps

for every pending job with work on `domain` and every candidate OPP, and picks the lowest OPP
whose bound meets every deadline (power/policies/pace.py, SurrogateGuidedPolicy).

How the bound is built
----------------------
The DSE surrogate predicts a task's p99 response time for a whole design point, with a
conformal interval. A static policy with Vmin v pins that domain at v, so the surrogate at
(base design, vmin_<domain> = v, other domains nominal, static P0 policy) answers: "if this
domain ran at voltage v, how long would this task's slowest 1 percent of jobs take?". I take the
upper end of the conformal interval of the p95 response time at 90 percent coverage (safety
tasks: p99 at 95 percent), mirroring P4's 0.95 / 0.999 empirical quantiles, and precompute it on a voltage grid for every (task, domain) pair, so a call
is a dictionary lookup plus linear interpolation (no model inference inside the 1 ms loop).

    finish_ub = now + (1 - frac_done) * R_hi(task, domain, V(freq))

where V(freq) inverts the alpha-power law used by power/vf.py. Fallbacks ("uncertain, run full
model"): when the task or domain is not covered by the surrogate (for example T7 on the RT
core, or noc_cache), when V is outside the sampled Vmin range, or when the interval is flagged
uncertain (width above conformal.FALLBACK_REL x deadline), the bound falls back to
    now + est_remaining_cycles[domain] / freq * fallback_inflation
which is the P4-style cycle estimate.

What the bound is not: a per-job guarantee. The conformal coverage is marginal over design
points drawn like the calibration set, it describes the p99 of a run, and the pinned-voltage
setting is not the same as a DVFS policy switching every millisecond. Treat it as a calibrated
heuristic.
"""
from __future__ import annotations

import bisect
import math
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PS_PER_MS = 1_000_000_000
DOMAIN_KNOB = {"npu": "vmin_npu", "cpu_a": "vmin_cpu", "cpu_b": "vmin_cpu", "simt": "vmin_simt", "dsp": "vmin_dsp"}
SAFETY = {"T7_control", "T8_safety", "T12_arm2_control"}


def _alpha_power_inverse(freq_hz: float, f_nom: float, v_nom: float = 0.75, vth: float = 0.30,
                         alpha: float = 1.3) -> float:
    """Voltage at which f(V) = k (V - Vth)^alpha / V equals freq_hz, with f(v_nom) = f_nom."""
    k = f_nom * v_nom / (v_nom - vth) ** alpha
    f = lambda v: k * (v - vth) ** alpha / v  # noqa: E731
    lo, hi = vth + 1e-4, 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) < freq_hz:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class SurrogateFinishBound:
    """Callable for P6: upper bound (ps) on a job's finish time at a candidate frequency."""

    def __init__(self, scenario: str = "S1", base: Mapping[str, Any] | None = None, alpha: float = 0.1,
                 safety_alpha: float = 0.05, quantile: str = "p95", safety_quantile: str = "p99",
                 fallback_inflation: float = 1.25,
                 soc: Mapping[str, Any] | None = None, bundle_path: str | Path | None = None,
                 grid_step_v: float = 0.01) -> None:
        from surrogate.chiprun import VX1_DEFAULT
        from surrogate.conformal import DEADLINES_MS, FALLBACK_REL, load_bundle
        from surrogate.dataset import INPUTS, SCENARIOS, SCHEDULERS, POWER_POLICIES, SPACE, TRANSPORTS, NOC_BITS
        self.bundle = load_bundle(bundle_path)
        self.fallback_inflation = float(fallback_inflation)
        self.calls = 0
        self.fallbacks = 0
        cfg = dict(VX1_DEFAULT)
        cfg.update({"scheduler": "edf", "power_policy": "nominal", "scenario": scenario})
        cfg.update(dict(base or {}))
        enc = dict(cfg)
        enc["noc_bits"] = NOC_BITS.index(int(cfg["noc_bits"]))
        enc["scheduler"] = SCHEDULERS.index(str(cfg["scheduler"]))
        enc["power_policy"] = POWER_POLICIES.index(str(cfg["power_policy"]))
        enc["transport"] = TRANSPORTS.index(str(cfg["transport"]))
        enc["scenario"] = SCENARIOS.index(str(cfg["scenario"]))
        self.base_vec = np.array([float(enc[k]) for k in INPUTS])
        self.inputs = INPUTS
        # Nominal frequencies per domain (for the V(f) inversion).
        if soc is None:
            import yaml
            soc = yaml.safe_load((ROOT / "configs" / "soc" / "vx1.yaml").read_text())
        b = soc["blocks"]
        self.f_nom = {d: float(b[d]["freq_hz"]) for d in ("npu", "cpu_a", "cpu_b", "simt", "dsp")}
        self._v_cache: dict[tuple[str, float], float] = {}
        pr = soc.get("process", {})
        self.v_nom, self.vth, self.alpha_pl = float(pr.get("v_nominal", 0.75)), float(pr.get("vth", 0.30)), float(pr.get("alpha", 1.3))
        # Precompute tables: (task, domain) -> (volts grid, hi_ms, uncertain flags).
        self.tables: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        tasks = sorted({n.rsplit(".", 1)[0] for n in self.bundle.models if n.endswith("_ms")
                        and not n.startswith("chain_")})
        for d, knob in DOMAIN_KNOB.items():
            _, lo, hi, _, _ = SPACE[knob]
            grid = np.round(np.arange(lo, hi + 1e-9, grid_step_v), 4)
            X = np.tile(self.base_vec, (len(grid), 1))
            X[:, INPUTS.index(knob)] = grid
            for t in tasks:
                name = f"{t}.{safety_quantile if t in SAFETY else quantile}_ms"
                if name not in self.bundle.models:
                    continue
                a = safety_alpha if t in SAFETY else alpha
                key = str(a)
                if key not in self.bundle.models[name].conformal["qhat"]:
                    continue
                p = self.bundle.predict(X, [name], a)[name]
                dl = DEADLINES_MS.get(t)
                unc = (p["hi"] - p["lo"]) > FALLBACK_REL * dl if dl else np.zeros(len(grid), bool)
                self.tables[(t, d)] = (grid.tolist(), p["hi"].tolist(), unc.tolist())
        self.v_range = {d: (SPACE[k][1], SPACE[k][2]) for d, k in DOMAIN_KNOB.items()}

    def voltage_for(self, domain: str, freq_hz: float) -> float:
        k = (domain, float(freq_hz))
        v = self._v_cache.get(k)
        if v is None:
            v = _alpha_power_inverse(float(freq_hz), self.f_nom[domain], self.v_nom, self.vth, self.alpha_pl)
            self._v_cache[k] = v
        return v

    def _fallback(self, job: Mapping[str, Any], domain: str, freq_hz: float, now_ps: int) -> float:
        self.fallbacks += 1
        cyc = float((job.get("est_remaining_cycles") or {}).get(domain, 0.0) or 0.0)
        return now_ps + cyc / max(freq_hz, 1.0) * 1e12 * self.fallback_inflation

    def __call__(self, job: Mapping[str, Any], domain: str, freq_hz: float, now_ps: int) -> float:
        self.calls += 1
        tab = self.tables.get((str(job.get("task_id")), domain))
        if tab is None:
            return self._fallback(job, domain, freq_hz, now_ps)
        v = self.voltage_for(domain, freq_hz)
        lo, hi = self.v_range[domain]
        if v > hi:
            v = hi          # above the sampled range: use the nominal-voltage bound (conservative)
        if v < lo - 1e-6:
            return self._fallback(job, domain, freq_hz, now_ps)
        grid, r_hi, unc = tab
        i = min(max(bisect.bisect_left(grid, v), 1), len(grid) - 1)
        if unc[i - 1] or unc[i]:
            return self._fallback(job, domain, freq_hz, now_ps)
        w = (v - grid[i - 1]) / (grid[i] - grid[i - 1])
        r = float(r_hi[i - 1] * (1 - w) + r_hi[i] * w)
        frac = min(max(float(job.get("frac_done", 0.0) or 0.0), 0.0), 1.0)
        return now_ps + (1.0 - frac) * r * PS_PER_MS

    def stats(self) -> dict[str, Any]:
        return {"calls": self.calls, "fallbacks": self.fallbacks,
                "fallback_frac": self.fallbacks / self.calls if self.calls else None,
                "tables": len(self.tables)}


def make_p6_surrogate(scenario: str = "S1", **kw: Any) -> SurrogateFinishBound:
    """Convenience constructor used by the power stream's P6 runs."""
    return SurrogateFinishBound(scenario=scenario, **kw)
