"""Regenerate calib/tables/noc_booksim.json (spike V6, NoC L2 reference).

Two kinds of BookSim 2 runs on the VX-1 4x4 mesh (see calib/booksim/booksim_cfg.py
for how vx1.yaml maps to BookSim parameters):

1. Native synthetic mode (BookSim's own Bernoulli injection, latency sim with warmup and
   convergence checks): uniform random, transpose, and hotspot to the two DRAM controller
   nodes [0,3] and [2,3]. I sweep the offered load in flits/node/cycle for 2-flit (64 B)
   and 8-flit (256 B) packets and record mean packet and network latency and accepted
   throughput. BookSim reports "unstable" past saturation; I keep those points flagged.
2. Trace mode (my patch) on Bernoulli traces generated here with the same rates, to get
   p50/p99 latency, which native mode does not print, and to check that trace mode agrees
   with native mode on the mean.

Saturation throughput is reported two ways: the largest accepted rate among stable runs,
and the offered load where mean packet latency crosses 3x the zero-load latency (linear
interpolation).

    .venv/bin/python calib/run_booksim.py [--jobs 12]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "booksim"))

import booksim_cfg as bc  # noqa: E402
import replay as noc_replay  # noqa: E402

N = 16
RATES = [0.005, 0.01, 0.02, 0.03, 0.05, 0.07, 0.08, 0.09, 0.1, 0.11, 0.115, 0.12, 0.125,
         0.13, 0.15, 0.2, 0.25, 0.28, 0.3, 0.32, 0.34, 0.36, 0.4, 0.45, 0.5, 0.55, 0.6, 0.63,
         0.66, 0.69, 0.72, 0.75, 0.8, 0.9]
PACKETS = (2, 8)


def traffic_spec(name: str) -> str:
    if name == "uniform":
        return "uniform"
    if name == "transpose":
        return "transpose"
    if name == "hotspot_dram":
        d = bc.dram_nodes()
        # BookSim parses the parameter list as one brace list, so the syntax is
        # hotspot({{n0,n1},{w0,w1}}). hotspot({n0,n1},{w0,w1}) silently becomes a
        # single hotspot at n0; the trace-mode cross-check below caught that.
        return "hotspot({{" + ",".join(map(str, d)) + "},{" + ",".join("1" for _ in d) + "}})"
    raise ValueError(name)


_NUM = r"([-+0-9.eE]+|nan|inf)"


def parse_overall(out: str) -> dict[str, float]:
    sec = out.split("====== Overall Traffic Statistics ======")[-1]
    keys = {
        "plat_mean": "Packet latency average",
        "nlat_mean": "Network latency average",
        "accepted_flit_rate": "Accepted flit rate average",
        "injected_flit_rate": "Injected flit rate average",
        "hops_mean": "Hops average",
    }
    res = {}
    for k, label in keys.items():
        m = re.search(re.escape(label) + r" = " + _NUM, sec)
        res[k] = float(m.group(1)) if m else float("nan")
    return res


def native_point(args: tuple[str, int, float, int]) -> dict[str, Any]:
    traffic, psize, rate, seed = args
    cfg = bc.base_config()
    cfg.update({
        "traffic": traffic_spec(traffic), "packet_size": psize, "injection_rate": rate,
        "injection_rate_uses_flits": 1, "sim_type": "latency", "warmup_periods": 3,
        "sample_period": 10000, "max_samples": 10, "seed": seed,
    })
    with tempfile.TemporaryDirectory() as d:
        p = bc.write_cfg(Path(d) / "s.cfg", cfg)
        r = subprocess.run([str(bc.BOOKSIM), str(p)], capture_output=True, text=True)
    out = r.stdout
    unstable = ("unstable" in out.lower()) or ("Overall Traffic Statistics" not in out)
    pt = {"traffic": traffic, "packet_flits": psize, "offered_flit_rate": rate,
          "stable": not unstable}
    if not unstable:
        pt.update(parse_overall(out))
    return pt


def trace_point(args: tuple[str, int, float, int, int]) -> dict[str, Any]:
    """Bernoulli trace with the same pattern, replayed through trace mode."""
    traffic, psize, rate, cycles, seed = args
    rng = np.random.default_rng(seed)
    p_pkt = rate / psize
    recs = []
    dram = bc.dram_nodes()
    for c in range(cycles):
        srcs = np.nonzero(rng.random(N) < p_pkt)[0]
        for s in srcs:
            if traffic == "uniform":
                d = int(rng.integers(0, N))
            elif traffic == "transpose":
                x, y = s % 4, s // 4
                d = int(y + 4 * x)
            else:
                d = int(dram[rng.integers(0, len(dram))])
            recs.append((c, int(s), d, psize))
    with tempfile.TemporaryDirectory() as td:
        rows = noc_replay.run_raw_trace(recs, Path(td))
    warm = cycles // 5
    plat = np.array([r["plat"] for r in rows if r["create_cycle"] >= warm])
    return {"traffic": traffic, "packet_flits": psize, "offered_flit_rate": rate,
            "packets": int(plat.size), "plat_mean": float(plat.mean()),
            "plat_p50": float(np.percentile(plat, 50)),
            "plat_p99": float(np.percentile(plat, 99)),
            "drain_cycles_after_last_inject": int(max(r["arrive_cycle"] for r in rows) - cycles)}


def hop_check() -> list[dict[str, int]]:
    recs = [(0, 0, 1, 1), (200, 0, 3, 1), (400, 0, 15, 1), (600, 5, 6, 1), (800, 0, 15, 8)]
    with tempfile.TemporaryDirectory() as td:
        return noc_replay.run_raw_trace(recs, Path(td))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--trace-cycles", type=int, default=20000)
    ap.add_argument("--out", default=str(HERE / "tables" / "noc_booksim.json"))
    a = ap.parse_args()
    noc = bc.load_noc()
    fb = bc.flit_bytes(noc)
    cyc_ns = 1e9 / noc["freq_hz"]
    traffics = ("uniform", "transpose", "hotspot_dram")
    jobs = [(t, p, r, 7) for t in traffics for p in PACKETS for r in RATES]
    with ThreadPoolExecutor(a.jobs) as ex:
        native = list(ex.map(native_point, jobs))

    curves: dict[str, Any] = {}
    for t in traffics:
        for p in PACKETS:
            pts = sorted([x for x in native if x["traffic"] == t and x["packet_flits"] == p],
                         key=lambda x: x["offered_flit_rate"])
            stable = [x for x in pts if x["stable"]]
            zl = stable[0]["plat_mean"] if stable else float("nan")
            sat3 = None
            for lo, hi in zip(stable, stable[1:]):
                if lo["plat_mean"] <= 3 * zl < hi["plat_mean"]:
                    f = (3 * zl - lo["plat_mean"]) / (hi["plat_mean"] - lo["plat_mean"])
                    sat3 = lo["offered_flit_rate"] + f * (hi["offered_flit_rate"] - lo["offered_flit_rate"])
            if sat3 is None and len(stable) < len(pts):
                sat3 = stable[-1]["offered_flit_rate"] if stable else None
            max_acc = max((x["accepted_flit_rate"] for x in stable), default=float("nan"))
            curves[f"{t}/{p}flit"] = {
                "zero_load_plat_cycles": zl,
                "zero_load_plat_ns": zl * cyc_ns,
                "saturation_offered_flit_rate_3x_zero_load": sat3,
                "max_stable_accepted_flit_rate": max_acc,
                "max_stable_accepted_gbps_total": max_acc * fb * noc["freq_hz"] * N / 1e9,
                "first_unstable_offered_rate": next((x["offered_flit_rate"] for x in pts
                                                     if not x["stable"]), None),
            }

    # Trace-mode p99 at rates below each curve's first unstable point.
    tjobs = []
    for t in traffics:
        for p in PACKETS:
            fu = curves[f"{t}/{p}flit"]["first_unstable_offered_rate"] or 1.0
            for r in RATES:
                if r < fu:
                    tjobs.append((t, p, r, a.trace_cycles, 11))
    with ThreadPoolExecutor(a.jobs) as ex:
        tpts = list(ex.map(trace_point, tjobs))
    # agreement check between native and trace mode means (stable, below 70% of sat)
    agree = []
    agree_all = []
    for tp in tpts:
        nat = next(x for x in native if x["traffic"] == tp["traffic"]
                   and x["packet_flits"] == tp["packet_flits"]
                   and x["offered_flit_rate"] == tp["offered_flit_rate"])
        if nat["stable"]:
            e = abs(tp["plat_mean"] - nat["plat_mean"]) / nat["plat_mean"]
            agree_all.append(e)
            fu = curves[f"{tp['traffic']}/{tp['packet_flits']}flit"]["first_unstable_offered_rate"] or 1.0
            if tp["offered_flit_rate"] <= 0.7 * fu:
                agree.append(e)

    out = {
        "provenance": {
            "script": "calib/run_booksim.py",
            "tool": "BookSim 2 (github.com/booksim/booksim2)",
            "tool_commit": subprocess.run(["git", "-C", str(ROOT / "third_party" / "booksim2"),
                                           "rev-parse", "HEAD"], capture_output=True,
                                          text=True).stdout.strip(),
            "local_patches": ["trace-driven injection mode (trace_file/trace_out) in "
                              "src/trafficmanager.{cpp,hpp}, src/booksim_config.cpp"],
            "config_files": ["configs/soc/vx1.yaml"],
            "date": dt.date.today().isoformat(),
            "seeds": {"native": 7, "trace": 11},
        },
        "setup": {
            "booksim_config": bc.base_config(),
            "flit_bytes": fb,
            "noc_cycle_ns": cyc_ns,
            "dram_controller_nodes": bc.dram_nodes(),
            "node_numbering": "id = x + 4*y",
            "rate_units": "flits per node per cycle (offered); gbps_total = rate * flit_bytes "
                          "* freq * 16 nodes",
            "hotspot_note": "all sources send only to the two DRAM nodes, so ejection bandwidth "
                            "(2 flits/cycle total, 0.125 flits/node/cycle) bounds throughput",
            "per_hop_check": hop_check(),
        },
        "curves": curves,
        "native_points": native,
        "trace_points": tpts,
        "trace_vs_native_mean_rel_err": {
            "below_70pct_of_saturation": {
                "n": len(agree), "mean": float(np.mean(agree)) if agree else None,
                "max": float(np.max(agree)) if agree else None},
            "all_stable_points": {
                "n": len(agree_all), "mean": float(np.mean(agree_all)) if agree_all else None,
                "max": float(np.max(agree_all)) if agree_all else None},
            "note": "near saturation the finite 20k-cycle trace and BookSim's converged "
                    "latency run diverge, as expected",
        },
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(curves, indent=1))
    print("trace vs native mean rel err:", out["trace_vs_native_mean_rel_err"])


if __name__ == "__main__":
    main()
