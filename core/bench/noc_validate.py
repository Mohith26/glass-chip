"""Validate the L1 NoC model against BookSim 2 on identical open-loop traces.

Traces follow calib/run_booksim.py trace_point(): Bernoulli packet injection per
node per cycle at an offered flit rate, uniform random / transpose / hotspot to
the two DRAM controller nodes, 2- and 8-flit packets, seed 11, 20k cycles, the
first fifth discarded as warm-up. Each trace is replayed through BookSim 2
(calib/booksim/replay.py run_raw_trace) and through the core's L1 NoC
(Sim.noc_replay) in two configurations:
  before: legacy train model (noc_l1_arbitration 0, upstream hold, no NI cycles)
  after:  calendar model (default)
Reports mean packet latency per load, the relative error vs BookSim, and the
saturation point (offered rate where mean latency reaches 3x zero load).
Writes core/bench/noc_validation.json.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import pathlib
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(ROOT / "calib" / "booksim"))
import glasschip_core as gc  # noqa: E402
import replay as bs_replay  # noqa: E402  (calib/booksim/replay.py)

N = 16
CYCLES = 20000
SEED = 11
RATES = {
    "uniform": [0.02, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75],
    "transpose": [0.02, 0.08, 0.15, 0.2, 0.25, 0.3, 0.33],
    "hotspot_dram": [0.01, 0.03, 0.05, 0.07, 0.09, 0.1, 0.11, 0.12],
}
MODELS = {
    "before": {"noc_l1_arbitration": 0, "noc_backpressure_hold": True, "noc_ni_cycles": 0, "noc_vc_flow_control": False, "noc_rr_tail": 0},
    "after": {},
}


def make_trace(traffic: str, psize: int, rate: float) -> list[tuple[int, int, int, int]]:
    """Same generator as calib/run_booksim.py trace_point (cycle, src, dst, flits)."""
    rng = np.random.default_rng(SEED)
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    dram = [x + 4 * y for x, y in soc["blocks"]["dram"]["nodes"]]
    p_pkt = rate / psize
    recs = []
    for c in range(CYCLES):
        for s in np.nonzero(rng.random(N) < p_pkt)[0]:
            if traffic == "uniform":
                d = int(rng.integers(0, N))
            elif traffic == "transpose":
                d = int(s // 4 + 4 * (s % 4))
            else:
                d = int(dram[rng.integers(0, len(dram))])
            recs.append((c, int(s), d, psize))
    return recs


def booksim_mean(recs: list[tuple[int, int, int, int]]) -> float:
    with tempfile.TemporaryDirectory() as td:
        rows = bs_replay.run_raw_trace(recs, pathlib.Path(td))
    warm = CYCLES // 5
    return float(np.mean([r["plat"] for r in rows if r["create_cycle"] >= warm]))


def l1_mean(recs: list[tuple[int, int, int, int]], params: dict[str, Any]) -> float:
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    freq = float(soc["noc"]["freq_hz"])
    flit = soc["noc"]["link_bits"] // 8
    cyc_ps = 1e12 / freq
    sim = gc.Sim(soc, {"cpu": "L0", "npu": "L1", "simt": "L0", "dsp": "L0", "noc": "L1", "dram": "L1", "cache": "L1", "params": params}, 1)
    arr = np.array([[int(round(c * cyc_ps)), s % 4, s // 4, d % 4, d // 4, n * flit] for c, s, d, n in recs], dtype=np.int64)
    lat = sim.noc_replay(arr) / cyc_ps
    warm = CYCLES // 5
    mask = np.array([c >= warm for c, _, _, _ in recs])
    return float(lat[mask].mean())


def saturation(points: list[tuple[float, float]]) -> float | None:
    """Offered rate where mean latency reaches 3x the zero-load (first point) value."""
    zl = points[0][1]
    for (r0, l0), (r1, l1) in zip(points, points[1:]):
        if l0 <= 3 * zl < l1:
            return r0 + (3 * zl - l0) / (l1 - l0) * (r1 - r0)
    return None


def run_point(args: tuple[str, int, float]) -> dict[str, Any]:
    traffic, psize, rate = args
    recs = make_trace(traffic, psize, rate)
    out = {"traffic": traffic, "packet_flits": psize, "offered_flit_rate": rate, "packets": len(recs), "booksim": booksim_mean(recs)}
    for m, params in MODELS.items():
        out[m] = l1_mean(recs, params)
    return out


def main() -> None:
    jobs = [(t, p, r) for t, rates in RATES.items() for p in (2, 8) for r in rates]
    with ThreadPoolExecutor(4) as ex:
        pts = list(ex.map(run_point, jobs))
    curves: dict[str, Any] = {}
    for t in RATES:
        for p in (2, 8):
            rows = sorted([x for x in pts if x["traffic"] == t and x["packet_flits"] == p], key=lambda x: x["offered_flit_rate"])
            sat = {m: saturation([(x["offered_flit_rate"], x[m]) for x in rows]) for m in ("booksim", "before", "after")}
            bs_sat = sat["booksim"] or math.inf
            for x in rows:
                for m in ("before", "after"):
                    x[f"{m}_err"] = (x[m] - x["booksim"]) / x["booksim"]
                x["below_booksim_saturation"] = x["offered_flit_rate"] < bs_sat
            below = [x for x in rows if x["below_booksim_saturation"]]
            curves[f"{t}/{p}flit"] = {
                "points": rows,
                "saturation_3x_zero_load": sat,
                "mean_abs_err_below_sat": {m: float(np.mean([abs(x[f"{m}_err"]) for x in below])) for m in ("before", "after")},
                "max_abs_err_below_sat": {m: float(np.max([abs(x[f"{m}_err"]) for x in below])) for m in ("before", "after")},
            }
            c = curves[f"{t}/{p}flit"]
            print(f"{t:13s} {p}flit  sat3x booksim {sat['booksim']}  before {sat['before']}  after {sat['after']}  "
                  f"|err| below sat: before {c['mean_abs_err_below_sat']['before']:.1%} after {c['mean_abs_err_below_sat']['after']:.1%}")
            for x in rows:
                print(f"    rate {x['offered_flit_rate']:.3f}  booksim {x['booksim']:8.2f}  before {x['before']:8.2f} ({x['before_err']:+.1%})  "
                      f"after {x['after']:8.2f} ({x['after_err']:+.1%})")
    doc = {
        "provenance": {
            "script": "core/bench/noc_validate.py",
            "date": _dt.date.today().isoformat(),
            "configs": ["configs/soc/vx1.yaml", "calib/booksim/booksim_cfg.py"],
            "tools": "BookSim 2 trace mode via calib/booksim/replay.py",
            "seed": SEED,
            "cycles": CYCLES,
            "latency_units": "NoC cycles (1.5 GHz), packet create to tail arrival, mean over packets created after warm-up",
        },
        "models": MODELS,
        "curves": curves,
    }
    (ROOT / "core/bench/noc_validation.json").write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
