"""Print every number quoted in calib/VALIDATION.md from results/s10.json and results/s15.json.

I use this to refresh VALIDATION.md after a rerun and to check that each quoted value matches.

    .venv/bin/python calib/validation_numbers.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.1f}%"


def apct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def main() -> None:
    d = json.loads((ROOT / "results" / "s10.json").read_text())
    an, b = d["analysis"], d["analysis_tier_b"]
    print("windows", d["setup"]["scenario_windows_s"], "tierB window", d["setup"]["tier_b_s1_window_s"])
    print("acceptance", {k: (apct(v) if isinstance(v, float) else v) for k, v in an["acceptance"].items()})
    for c, v in an["scenarios"].items():
        print(f"A {c:20s} mean {apct(v['mean'].get('mean_abs'))} p99 {apct(v['p99'].get('mean_abs'))} "
              f"n={v['mean'].get('n')} w10 {apct(v['frac_mean_within_10pct'])} w30 {apct(v['frac_mean_within_30pct'])} "
              f"| mb mean {apct(an['microbench'][c]['stats'].get('mean_abs'))} median {apct(an['microbench'][c]['stats'].get('median_abs'))}")
    for c in ("L0", "L1"):
        print("worst", c, [(w["case"], pct(w["rel_err_mean"])) for w in an["worst"][c]])
    for c, v in b["scenarios"].items():
        print(f"B {c:20s} mean {apct(v['mean'].get('mean_abs'))} p99 {apct(v['p99'].get('mean_abs'))} n={v['mean'].get('n')} "
              f"| mb mean {apct(b['microbench'][c]['stats'].get('mean_abs'))}")
        for t, r in v["per_task"].items():
            if "T1_ingest_overhead" in t:
                print("   ", t, round(r["ref_mean_ms"], 2), round(r["cfg_mean_ms"], 2))
    for k, v in d["trace_replay"]["cases"].items():
        print("trace", k, {c: (pct(v[c]["rel_err_mean"]), pct(v[c]["rel_err_p99"]), v[c].get("ramulator_achieved_gbps"))
                           for c in ("dram", "noc")})
    print("trace summary", d["trace_replay"]["summary"])
    for k, v in d["cpu_vs_gem5"]["cases"].items():
        print("cpu", k, {lv: pct(x["rel_err_vs_gem5"]) for lv, x in v["levels"].items()})
    for k, v in d["speed"].items():
        print("speed", k, f"{v['sim_s_per_cpu_s']:.3g}")
    s = json.loads((ROOT / "results" / "s15.json").read_text())
    o = s["orin_sanity"]
    for t, v in o["models"].items():
        print("orin", t, v["public_single_stream_ms"],
              {k: (round(r["modeled_ms"], 3), pct(r["rel_err"])) for k, r in v["runs"].items() if k.endswith("study")})
    print("orin n within", {k: v["n_within_30pct"] for k, v in o["summary"].items()})
    n = s["npu_vs_scalesim"]
    print("npu overall", n["overall"])
    print("npu tasks", {t: pct(v["total_rel_err"]) for t, v in n["tasks"].items()})
    print("l2 consistency", {t: v["op_duration_over_table_min"] for t, v in n["l2_consistency"].items()})


if __name__ == "__main__":
    main()
