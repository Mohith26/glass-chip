"""PowerManager: per-step power, energy, thermal and policy loop for the co-sim bridge.

Usage (the co-sim bridge calls step() every 1 ms physics step):

    pm = PowerManager(load_policy("P4"), load_soc(), ambient_c=25.0, task_info=...)
    pm.apply_scheduler(sim)              # sim.set_scheduler(engine, pm.scheduler)
    for each physics step at time t:
        release jobs due at t
        pm.step(sim, t)                  # account [t_prev, t], update thermal, pick OPPs
        done = sim.run_until(t + 1 ms)
        pm.observe_completions(done)     # optional; feeds P4/P6 statistics and miss counts
    summary = pm.summary()

Energy for the interval [t_prev, t] in each domain:
    E_dyn   = sum_class count * activity * C_eff * V^2          (energy.py)
    E_clk   = P_clk(V, f) * t_on                                 (clocked, not gated)
    E_leak  = P_leak(V, T) * t_on + retention * P_leak(V, T) * t_gated   (leakage.py)
    E_wake  = wakes * C_rail * V^2 ;  E_dvfs = 0.5 * C_rail * |V1^2 - V0^2| per change
    P_out   = sum / dt ;  P_in = P_out + regulator loss(P_out)    (dvfs.py)
V and f are the OPP the manager set at the previous step (the DVFS transition is
20 us of a 1 ms step, so this is a small approximation). T is the junction
temperature at the start of the interval (explicit coupling of leakage to thermal).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Mapping

import numpy as np

from .dvfs import RegulatorModel, gating_from_params, transition_energy_j
from .energy import EnergyModel
from .leakage import LeakageModel
from .params import DOMAINS, load_params
from .policies import PolicyContext, SurrogateFn, make_policy
from .predictor import ExecTimePredictor
from .thermal import ThrottleController, build_soc_thermal
from .vf import DomainVF, build_domain_vf, extend_below

ENGINES = ("npu", "simt", "cpu", "rt", "dsp", "dma")
PS_PER_S = 1_000_000_000_000


class PowerManager:
    """Owns the V/f, energy, leakage, regulator, thermal and policy models for one run."""

    def __init__(self, policy_cfg: Mapping[str, Any], soc_cfg: Mapping[str, Any], ambient_c: float = 25.0,
                 params: Mapping[str, Any] | None = None, task_info: Mapping[str, Mapping[str, Any]] | None = None,
                 surrogate: SurrogateFn | None = None, step_ps: int = PS_PER_S // 1000,
                 thermal_init: str = "ambient", warm_start_w: Mapping[str, float] | float | None = None,
                 low_voltage_arith: Mapping[str, Any] | None = None, record_series: bool = True,
                 domains: tuple[str, ...] = DOMAINS):
        self.policy_cfg = dict(policy_cfg)
        self.soc = soc_cfg
        base_params = dict(params) if params is not None else load_params()
        if low_voltage_arith is not None:
            lv = dict(base_params.get("low_voltage_arith", {}))
            lv.update(low_voltage_arith)
            base_params["low_voltage_arith"] = lv
        self.params = base_params
        self.domains = domains
        self.step_ps = int(step_ps)
        self.scheduler = str(self.policy_cfg.get("scheduler", "fifo"))
        self.policy = make_policy(self.policy_cfg, surrogate=surrogate)
        pp = self.policy_cfg.get("params") or {}
        self.predictor = ExecTimePredictor(
            quantile=float(pp.get("quantile", 0.95)), safety_quantile=float(pp.get("safety_quantile", 0.999)),
            prior_inflation=float(pp.get("prior_inflation", 1.25)), min_samples=int(pp.get("min_samples", 5)),
            window=int(pp.get("window", 200)), task_info=task_info,
            nominal_freqs={d: v.nominal.freq_hz for d, v in build_domain_vf(soc_cfg, domains).items()},
            anchor_window=int(pp.get("anchor_window", 50)))

        # V/f tables, with the hypothetical low-voltage arithmetic extension if enabled.
        self.vf: dict[str, DomainVF] = build_domain_vf(soc_cfg, domains)
        lv = self.params.get("low_voltage_arith", {}) or {}
        self.lv_enabled = bool(lv.get("enabled", False))
        if self.lv_enabled:
            d = str(lv.get("domain", "npu"))
            self.vf[d] = extend_below(self.vf[d], float(lv["mac_vmin_v"]), float(lv.get("opp_step_v", 0.05)),
                                      label="hypothetical low-voltage arithmetic")
        self.energy = EnergyModel(self.params, soc_cfg, self.vf)
        peak = {d: self.energy.peak_dynamic_nominal_w(d) for d in domains}
        self.leakage = LeakageModel.from_params(self.params, peak, {d: self.vf[d].nominal.volts for d in domains})
        rp = self.params["regulator"]
        self.regulators: dict[str, RegulatorModel] = {}
        for d in domains:
            top = self.vf[d].opps[-1]
            rated = float(rp["rated_over_peak"]) * (
                self.energy.peak_dynamic_w(d, top.volts, top.freq_hz) + self.leakage.power_w(d, top.volts, 105.0))
            self.regulators[d] = RegulatorModel(rated, float(rp["a0"]), float(rp["a1"]), float(rp["a2"]))
        self.reg_loss_node = str(rp.get("loss_node", "enclosure"))
        self.rail_cap_f = float(self.params["dvfs"]["rail_cap_nf"]) * 1e-9
        self.gating = gating_from_params(self.params)
        self.parallel_units = self._parallel_units()

        # Thermal network and throttling.
        self.thermal = build_soc_thermal(soc_cfg, self.params, domains, ambient_c)
        th = self.params["thermal"]
        self.tj_limit_c = float(th["tj_limit_c"])
        self._throttle_cfg = (float(th["throttle_c"]), float(th["release_c"]), self.tj_limit_c,
                              int(round(float(th["throttle_step_ms"]) * 1e9)))
        self.throttle = {d: self._new_throttle(d) for d in domains if not self.vf[d].fixed}
        if thermal_init not in ("ambient", "steady", "settle"):
            raise ValueError(f"thermal_init must be 'ambient', 'steady' or 'settle', not {thermal_init!r}")
        if thermal_init == "steady":
            self.warm_start(warm_start_w)
        # "settle": after a burn-in window, jump the thermal state to the steady state (with
        # temperature-dependent leakage) for the power measured so far, and restart the energy
        # accounting there. Approximates long-run operation in a short run (enclosure time
        # constants are minutes). Misses and robot metrics are not reset.
        self.thermal_init = thermal_init
        self._settle_ps = int(round(float(self.params["thermal"].get("settle_s", 1.0)) * 1e12)) \
            if thermal_init == "settle" else None
        self.settle_info: dict[str, Any] | None = None
        self._v_time: dict[str, float] = defaultdict(float)
        self._gated_time: dict[str, float] = defaultdict(float)

        # Run state.
        self.opp: dict[str, int] = {d: self.vf[d].nominal_index for d in domains}
        self._last_t_ps: int | None = None
        self._f_hist_t: list[int] = []                       # step boundary times
        self._f_hist_cum: dict[str, list[float]] = {d: [] for d in domains}   # cumulative f * dt (Hz*ps)
        self._f_at: dict[str, list[float]] = {d: [] for d in domains}         # f of the segment from step k
        self.last_util: dict[str, float] = {}
        self._pending_transition: dict[str, tuple[float, float]] = {}
        self._gating_enabled_on_sim = False
        self._scheduler_applied = False
        self.record_series = record_series
        self.series: dict[str, Any] = {"t_s": [], "power_w": defaultdict(list), "power_in_w": [], "volts": defaultdict(list),
                                       "freq_hz": defaultdict(list), "opp": defaultdict(list), "temp_c": defaultdict(list),
                                       "energy_j": [], "throttled": defaultdict(list), "gated_frac": defaultdict(list),
                                       "throttle_cap": defaultdict(list), "throttle_binding": defaultdict(list),
                                       "ambient_c": []}
        self.energy_by_domain_j: dict[str, float] = defaultdict(float)
        self.energy_parts_j: dict[str, float] = defaultdict(float)
        self.energy_parts_by_domain_j: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.reg_loss_j = 0.0
        self.total_energy_j = 0.0
        self.peak_power_w = 0.0
        self.peak_temp_c = float(max(self.thermal.T))
        self.throttled_ps = 0
        self.throttled_ps_by_domain: dict[str, int] = defaultdict(int)
        self.elapsed_ps = 0
        self.transitions = 0
        self.wakeups = 0
        self.busy_cycles: dict[str, float] = defaultdict(float)
        self.dyn_energy_nominal_j: dict[str, float] = defaultdict(float)
        self.completions = 0
        self.misses: dict[str, int] = defaultdict(int)
        self.jobs_by_task: dict[str, int] = defaultdict(int)
        self.job_trace: list[dict[str, Any]] = []
        self._pending_seen: dict[int, dict[str, Any]] = {}
        self.vmin_notes: list[str] = []
        self.accounted_from_ps = 0
        self.first_throttle_ps: int | None = None          # first step any OPP cap is below max
        self.first_binding_ps: int | None = None           # first step a cap lowers the policy's choice
        self.binding_ps = 0
        self._binding_now: set[str] = set()
        self.binding_ps_by_domain: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ setup
    def _new_throttle(self, d: str) -> ThrottleController:
        thr, rel, trip, step = self._throttle_cfg
        return ThrottleController(self.vf[d].max_index, thr, rel, trip, step)

    def _parallel_units(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for b in self.soc.get("blocks", {}).values():
            d = b.get("power_domain")
            n = b.get("cores") or b.get("tiles") or b.get("units") or b.get("compute_units")
            if d and n:
                out[d] = max(out.get(d, 0), int(n))
        if self.soc.get("blocks", {}).get("rt", {}).get("lockstep"):
            out["rt"] = 1
        return out

    def warm_start(self, power_w: Mapping[str, float] | float | None) -> None:
        """Initialize temperatures at the steady state for an explicitly given power.

        power_w: {domain: W} (any thermal node name is accepted), or a total in W spread over the
        domains in proportion to their clock-tree + leakage power at nominal plus 30 percent of peak
        dynamic (the split is an assumption; the total is the caller's). Leakage feedback is not
        iterated here. There is deliberately no default: an assumed workload made every short run
        start near 86 C and inflated S1/P0 power by about 37 percent (6.17 W vs 4.50 W settled).
        Use thermal_init="settle" to start from the steady state for the run's own measured power.
        """
        if power_w is None:
            raise ValueError(
                'thermal_init="steady" needs an explicit warm_start_w (dict of W per domain, or total W). '
                'For "S1 at nominal" style runs use thermal_init="settle", which settles to the steady '
                'state for the run\'s own measured power; use "ambient" for a cold start.')
        if isinstance(power_w, Mapping):
            p = {k: float(v) for k, v in power_w.items()}
        else:
            raw = {}
            for d in self.domains:
                n = self.vf[d].nominal
                raw[d] = (0.3 * self.energy.peak_dynamic_nominal_w(d) + self.energy.clock_power_w(d, n.volts, n.freq_hz)
                          + self.leakage.power_w(d, n.volts, 60.0))
            s = sum(raw.values())
            p = {d: float(power_w) * v / s for d, v in raw.items()}
        loss = sum(self.regulators[d].loss_w(v) for d, v in p.items() if d in self.regulators)
        p[self.reg_loss_node] = p.get(self.reg_loss_node, 0.0) + loss
        self.thermal.T = self.thermal.steady_state(p)
        self.peak_temp_c = float(max(self.thermal.T))

    def apply_scheduler(self, sim: Any, engines: Iterable[str] = ENGINES) -> None:
        """Call sim.set_scheduler(engine, self.scheduler) for every engine."""
        if hasattr(sim, "set_scheduler"):
            for e in engines:
                sim.set_scheduler(e, self.scheduler)
        self._scheduler_applied = True

    def set_ambient(self, ambient_c: float) -> None:
        self.thermal.set_ambient(ambient_c)

    def set_vmin(self, domain: str, v: float) -> None:
        """Voltage-sweep hook (scenario S8).

        Static policies (P0-P3, P5-online): pins the domain to the highest OPP at or below v,
        i.e. a static undervolt (frequency follows the alpha-power law).
        Dynamic policies (P4, P6): sets the lowest OPP the policy may choose.
        If v is below the SoC config's table, OPPs are extrapolated down to v and the run is
        flagged (extrapolated, not part of the configured table).
        """
        dvf = self.vf[domain]
        if dvf.fixed:
            self.vmin_notes.append(f"{domain}: fixed-frequency domain, set_vmin ignored")
            return
        if v < dvf.opps[0].volts - 1e-9:
            old_max = dvf.max_index
            dvf = extend_below(dvf, v, label="set_vmin")
            self.vf[domain] = dvf
            shift = dvf.max_index - old_max
            self.opp[domain] += shift
            self.throttle[domain] = self._new_throttle(domain)
            self.vmin_notes.append(f"{domain}: extrapolated OPPs down to {v:.3f} V (below configured table)")
        idx = dvf.index_at_or_below_voltage(v)
        if self.policy.static:
            dvf.pinned_index = idx
            dvf.floor_index = 0
        else:
            dvf.pinned_index = None
            dvf.floor_index = idx
        self.energy.vf = self.vf

    # ------------------------------------------------------------- per step
    def _cum_f(self, d: str, t_ps: int) -> float:
        """Integral of domain d's OPP frequency from the first step to t_ps (Hz * ps)."""
        import bisect
        ts = self._f_hist_t
        cum = self._f_hist_cum[d]
        f_now = self.vf[d].opps[self.opp[d]].freq_hz
        if not ts:
            return f_now * t_ps
        k = bisect.bisect_right(ts, t_ps) - 1
        if k < 0:
            return 0.0
        # the OPP set at step k holds until step k+1
        f_k = self._f_at[d][k]
        return cum[k] + f_k * (t_ps - ts[k])

    def avg_freq(self, d: str, t0_ps: int, t1_ps: int) -> float:
        """Time-averaged frequency of domain d over [t0, t1] from the manager's OPP history."""
        if t1_ps <= t0_ps or not self._f_hist_t:
            return self.vf[d].opps[self.opp[d]].freq_hz
        return (self._cum_f(d, t1_ps) - self._cum_f(d, t0_ps)) / (t1_ps - t0_ps)

    def observe_completions(self, completions: Iterable[Mapping[str, Any]]) -> None:
        """Feed completed jobs (sim.run_until output) into statistics, miss counts and the job trace."""
        for c in completions:
            t0 = int(c.get("start_ps", c["release_ps"]))
            t1 = int(c.get("finish_ps", t0))
            freqs = {d: self.avg_freq(d, t0, t1) for d in self.domains}
            self.predictor.observe_completion(c, freqs)
            task = str(c["task_id"])
            self.completions += 1
            self.jobs_by_task[task] += 1
            if c.get("missed"):
                self.misses[task] += 1
            seen = self._pending_seen.pop(int(c["job_id"]), None)
            cyc = c.get("cycles_by_domain") or (seen or {}).get("est")
            self.job_trace.append({
                "job_id": int(c["job_id"]), "task_id": task, "release_ps": int(c["release_ps"]),
                "deadline_ps": int(c["deadline_ps"]), "finish_ps": int(c.get("finish_ps", 0)),
                "cycles_by_domain": dict(cyc or {}), "cycles_source": "actual" if c.get("cycles_by_domain") else "estimate",
                "safety": self.predictor.is_safety(task),
            })

    def step(self, sim: Any, t_ps: int) -> dict[str, int]:
        """Account the interval since the last call, update thermal state, apply the policy."""
        t_ps = int(t_ps)
        if not self._gating_enabled_on_sim and self.policy.gating and hasattr(sim, "set_power_gating"):
            for d in self.gating.gateable:
                if self.gating.idle_before_gate_ps > 0:
                    try:
                        sim.set_power_gating(d, True, self.gating.wake_latency_ps, self.gating.idle_before_gate_ps)
                        continue
                    except TypeError:
                        pass   # simulator without idle hysteresis
                sim.set_power_gating(d, True, self.gating.wake_latency_ps)
            self._gating_enabled_on_sim = True
        counters = sim.counters(True) or {}
        if self._last_t_ps is not None and t_ps > self._last_t_ps:
            self._account(counters, t_ps - self._last_t_ps, t_ps)
        self._last_t_ps = t_ps

        pending = list(sim.pending_jobs()) if hasattr(sim, "pending_jobs") else []
        self.predictor.observe_pending(pending)
        for j in pending:
            if int(j["job_id"]) not in self._pending_seen and not j.get("started") and not j.get("frac_done"):
                self._pending_seen[int(j["job_id"])] = {"est": dict(j.get("est_remaining_cycles") or {})}
        new_seg = self._record_freq_step(t_ps)
        ctx = PolicyContext(t_ps, self.step_ps, self.vf, dict(self.opp), pending, self.predictor, self.parallel_units,
                            dict(self.last_util))
        choice = self.policy.decide(ctx)
        self._binding_now = set()
        for d, idx in choice.items():
            dvf = self.vf[d]
            idx = max(int(idx), dvf.floor_index)
            if dvf.pinned_index is not None:
                idx = dvf.pinned_index
            if idx > self.throttle[d].cap:
                self._binding_now.add(d)
                if self.first_binding_ps is None:
                    self.first_binding_ps = t_ps
            idx = min(idx, self.throttle[d].cap)
            idx = max(0, min(idx, dvf.max_index))
            if idx != self.opp[d]:
                v0 = dvf.opps[self.opp[d]].volts
                self.opp[d] = idx
                self._pending_transition[d] = (v0, dvf.opps[idx].volts)
                self.transitions += 1
                sim.set_domain_freq(d, dvf.opps[idx].freq_hz)
        if new_seg:
            for d in self.domains:
                self._f_at[d].append(self.vf[d].opps[self.opp[d]].freq_hz)
        return dict(self.opp)

    def _record_freq_step(self, t_ps: int) -> bool:
        """Called before the policy sets new OPPs at t_ps: closes the previous segment."""
        if self._f_hist_t and t_ps <= self._f_hist_t[-1]:
            return False
        for d in self.domains:
            f_prev = self._f_at[d][-1] if self._f_at[d] else self.vf[d].opps[self.opp[d]].freq_hz
            cum_prev = self._f_hist_cum[d][-1] if self._f_hist_cum[d] else 0.0
            t_prev = self._f_hist_t[-1] if self._f_hist_t else 0
            self._f_hist_cum[d].append(cum_prev + f_prev * (t_ps - t_prev) if self._f_hist_t else f_prev * t_ps)
        self._f_hist_t.append(t_ps)
        return True

    def _account(self, counters: Mapping[str, Mapping[str, Any]], dt_ps: int, t_ps: int) -> None:
        dt_s = dt_ps / PS_PER_S
        temps = self.thermal.temps()
        node_power: dict[str, float] = {}
        total_in = 0.0
        loss_total = 0.0
        for d in self.domains:
            c = counters.get(d, {}) or {}
            dvf = self.vf[d]
            o = dvf.opps[self.opp[d]]
            v, f = o.volts, o.freq_hz
            busy = float(c.get("busy_ps", 0.0))
            idle = float(c.get("idle_ps", max(dt_ps - busy, 0.0)))
            if "busy_ps" in c:
                self.last_util[d] = min(busy / dt_ps, 1.0)
            gating_on = self.policy.gating and d in self.gating.gateable
            if "gated_ps" in c:
                gated = float(c["gated_ps"])
            else:
                gated = idle if gating_on else 0.0
            gated = min(max(gated, 0.0), float(dt_ps))
            on_ps = dt_ps - gated
            if "wakeups" in c:
                wakes = int(c["wakeups"])
            else:
                wakes = 1 if (gating_on and busy > 0 and gated > 0) else 0
            e_dyn, _ = self.energy.dynamic_energy_j(d, c, v)
            _, v_rest = self.energy.voltages_for(d, v)
            p_leak = self.leakage.power_w(d, v_rest, temps[d])
            e_clk = self.energy.clock_power_w(d, v, f) * on_ps / PS_PER_S
            e_leak = p_leak * on_ps / PS_PER_S + p_leak * self.gating.retention_leak_fraction * gated / PS_PER_S
            e_wake = wakes * self.gating.wake_energy_j(v_rest)
            e_dvfs = 0.0
            if d in self._pending_transition:
                v0, v1 = self._pending_transition.pop(d)
                e_dvfs = transition_energy_j(self.rail_cap_f, v0, v1)
            e_out = e_dyn + e_clk + e_leak + e_wake + e_dvfs
            p_out = e_out / dt_s
            p_in = self.regulators[d].input_w(p_out)
            loss = p_in - p_out
            node_power[d] = p_out
            loss_total += loss
            total_in += p_in
            self.energy_by_domain_j[d] += p_in * dt_s
            for k, e in (("dynamic", e_dyn), ("clock", e_clk), ("leakage", e_leak), ("wake", e_wake), ("dvfs", e_dvfs),
                         ("regulator", loss * dt_s)):
                if k != "regulator":
                    self.energy_parts_j[k] += e
                self.energy_parts_by_domain_j[d][k] += e
            self.wakeups += wakes
            self._v_time[d] += v * dt_ps
            self._gated_time[d] += gated
            self.busy_cycles[d] += busy / PS_PER_S * f
            vn = dvf.nominal.volts
            self.dyn_energy_nominal_j[d] += self.energy.dynamic_energy_j(d, c, vn)[0] if not dvf.fixed else e_dyn
            if self.record_series:
                self.series["power_w"][d].append(p_in)
                self.series["volts"][d].append(v)
                self.series["freq_hz"][d].append(f)
                self.series["opp"][d].append(self.opp[d])
                self.series["gated_frac"][d].append(gated / dt_ps)
        self.reg_loss_j += loss_total * dt_s
        self.energy_parts_j["regulator"] += loss_total * dt_s
        node_power[self.reg_loss_node] = node_power.get(self.reg_loss_node, 0.0) + loss_total
        self.thermal.step(node_power, dt_s)
        self.total_energy_j += total_in * dt_s
        self.elapsed_ps += dt_ps
        self.peak_power_w = max(self.peak_power_w, total_in)
        self.peak_temp_c = max(self.peak_temp_c, float(max(self.thermal.T)))
        any_thr = False
        for d, ctl in self.throttle.items():
            ctl.update(self.thermal.temp(d), t_ps)
            if ctl.throttled:
                any_thr = True
                self.throttled_ps_by_domain[d] += dt_ps
        if any_thr:
            self.throttled_ps += dt_ps
            if self.first_throttle_ps is None:
                self.first_throttle_ps = t_ps
        if self._binding_now:
            self.binding_ps += dt_ps
            for d in self._binding_now:
                self.binding_ps_by_domain[d] += dt_ps
        if self._settle_ps is not None and self.elapsed_ps >= self._settle_ps:
            self._settle()
        if self.record_series:
            self.series["t_s"].append(t_ps / PS_PER_S)
            self.series["power_in_w"].append(total_in)
            self.series["energy_j"].append(self.total_energy_j)
            for n, tc in self.thermal.temps().items():
                self.series["temp_c"][n].append(tc)
            for d, ctl in self.throttle.items():
                self.series["throttled"][d].append(ctl.throttled)
                self.series["throttle_cap"][d].append(ctl.cap)
                self.series["throttle_binding"][d].append(d in self._binding_now)
            self.series["ambient_c"].append(self.thermal.ambient_c)

    def _settle(self) -> None:
        from .thermal_analysis import steady_state_with_leakage
        el = float(self.elapsed_ps)
        pnl = {d: sum(v for k, v in parts.items() if k in ("dynamic", "clock", "wake", "dvfs")) / (el / PS_PER_S)
               for d, parts in self.energy_parts_by_domain_j.items()}
        volts = {d: self._v_time[d] / el for d in self.domains}
        gf = {d: self._gated_time[d] / el for d in self.domains}
        ss = steady_state_with_leakage(self, pnl, volts, self.thermal.ambient_c, gated_frac=gf)
        # Without a steady state (runaway) start from the throttle threshold: the die would have heated to
        # it and throttling takes over from there.
        cap = self._throttle_cfg[0]
        self.thermal.T = np.array([min(ss["temps_c"][n], cap) for n in self.thermal.names])
        self.settle_info = {"t_s": self.elapsed_ps / PS_PER_S, "steady_t_max_c": ss["t_max_c"],
                            "runaway": ss["runaway"], "capped_at_c": cap if ss["runaway"] else None,
                            "nonleak_w": sum(pnl.values())}
        self._settle_ps = None
        self.energy_by_domain_j.clear()
        self.energy_parts_j.clear()
        self.energy_parts_by_domain_j.clear()
        self.reg_loss_j = 0.0
        self.total_energy_j = 0.0
        self.peak_power_w = 0.0
        self.peak_temp_c = float(max(self.thermal.T))
        self.throttled_ps = 0
        self.throttled_ps_by_domain.clear()
        self.binding_ps = 0
        self.binding_ps_by_domain.clear()
        self.busy_cycles.clear()
        self.dyn_energy_nominal_j.clear()
        self.accounted_from_ps = self.elapsed_ps
        self.elapsed_ps = 0

    # ---------------------------------------------------------------- results
    def cycle_energy_profile(self) -> dict[str, float]:
        """Average switching energy per busy cycle at nominal voltage, per domain (J/cycle).

        Used by the P5 offline solver so its energy model matches this run's activity mix.
        """
        return {d: (self.dyn_energy_nominal_j[d] / self.busy_cycles[d]) if self.busy_cycles[d] > 0 else 0.0
                for d in self.domains}

    @property
    def hypothetical(self) -> bool:
        return self.energy.hypothetical or (self.lv_enabled and any(o.extrapolated for o in self.vf[self.energy.lv_domain].opps))

    def summary(self) -> dict[str, Any]:
        dur_s = self.elapsed_ps / PS_PER_S
        avg = self.total_energy_j / dur_s if dur_s > 0 else 0.0
        safety_misses = sum(n for t, n in self.misses.items() if self.predictor.is_safety(t))
        labels = []
        if self.hypothetical:
            labels.append("hypothetical: low-voltage arithmetic factor or Vmin outside the configured table "
                          "(represents the class of techniques Velaura claims for Titan Core; not their technology)")
        labels.extend(self.vmin_notes)
        return {
            "policy": self.policy_cfg.get("name", self.policy.name),
            "power_mode": self.policy.name,
            "scheduler": self.scheduler,
            "duration_s": dur_s,
            "energy_j": self.total_energy_j,
            "energy_soc_j": self.total_energy_j - self.reg_loss_j,
            "regulator_loss_j": self.reg_loss_j,
            "avg_power_w": avg,
            "peak_power_w": self.peak_power_w,
            "energy_per_robot_hour_wh": avg,     # avg W x 1 h = Wh per robot-hour
            "peak_temp_c": self.peak_temp_c,
            "time_throttled_s": self.throttled_ps / PS_PER_S,
            "time_throttled_s_by_domain": {d: v / PS_PER_S for d, v in self.throttled_ps_by_domain.items()},
            "first_throttle_s": None if self.first_throttle_ps is None else self.first_throttle_ps / PS_PER_S,
            "first_throttle_binding_s": None if self.first_binding_ps is None else self.first_binding_ps / PS_PER_S,
            "time_throttle_binding_s": self.binding_ps / PS_PER_S,
            "time_throttle_binding_s_by_domain": {d: v / PS_PER_S for d, v in self.binding_ps_by_domain.items()},
            "energy_j_by_domain": dict(self.energy_by_domain_j),
            "energy_j_by_part": dict(self.energy_parts_j),
            "energy_j_by_domain_part": {d: dict(v) for d, v in self.energy_parts_by_domain_j.items()},
            "dvfs_transitions": self.transitions,
            "wakeups": self.wakeups,
            "jobs_completed": self.completions,
            "misses_by_task": dict(self.misses),
            "misses_total": int(sum(self.misses.values())),
            "safety_misses": int(safety_misses),
            "hypothetical": self.hypothetical,
            "labels": labels,
            "low_voltage_arith": dict(self.params.get("low_voltage_arith", {})),
            "surrogate": getattr(self.policy, "surrogate_kind", None),
            "surrogate_stats": self.policy.surrogate_stats() if hasattr(self.policy, "surrogate_stats") else None,
            "thermal_init": self.thermal_init,
            "accounted_from_s": self.accounted_from_ps / PS_PER_S,
            "settle": self.settle_info,
            "ambient_c_end": self.thermal.ambient_c,
        }

    def series_arrays(self) -> dict[str, Any]:
        """Time series as numpy arrays (power per domain, V, f, OPP, temperature per node, energy)."""
        s = self.series
        out: dict[str, Any] = {"t_s": np.asarray(s["t_s"]), "power_in_w": np.asarray(s["power_in_w"]),
                               "energy_j": np.asarray(s["energy_j"]), "ambient_c": np.asarray(s["ambient_c"])}
        for k in ("power_w", "volts", "freq_hz", "opp", "temp_c", "throttled", "gated_frac", "throttle_cap",
                  "throttle_binding"):
            out[k] = {n: np.asarray(v) for n, v in s[k].items()}
        return out

    def opp_table(self) -> dict[str, list[dict[str, Any]]]:
        return {d: [{"index": o.index, "volts": o.volts, "freq_hz": o.freq_hz, "extrapolated": o.extrapolated}
                    for o in v.opps] for d, v in self.vf.items()}


def finite_or_none(x: float) -> float | None:
    return x if math.isfinite(x) else None
