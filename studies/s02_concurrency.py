"""s02: concurrency scaling.

Question: as tasks are added one at a time, from the bare robot loop up to the sixteen task
graphs of S6, when do deadlines start to slip, and which tasks slip first?

Method: chip-only runs (the release schedule of the scenarios, no physics) at interactive
fidelity, nominal V/f, under FIFO (P0) and EDF (P2) scheduling, 3 seeds per point. Each
step adds one task graph in the S6 order.

Run: .venv/bin/python studies/s02_concurrency.py [--quick]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import SAFETY, mean_ci, provenance, run_many, savefig, slim_result, style, write_json  # noqa: E402

BASE = ["T7_control", "T8_safety", "T6_estimate", "T1_ingest_wrist", "T1_ingest_overhead", "T2_detect"]
ADD = ["T3_esdf", "T4_vla", "T5_plan", "T10_record", "T2_detect_overhead", "T11_speech", "T12_arm2_control",
       "T13_map_save", "T14_ota", "T15_diag"]
POLICIES = ["P0", "P2"]


def ladder() -> list[list[str]]:
    sets = [list(BASE)]
    for t in ADD:
        sets.append(sets[-1] + [t])
    return sets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--replot", action="store_true")
    a = ap.parse_args()
    if a.replot:
        import json
        from studies.common import RESULTS
        d = json.loads((RESULTS / "s02.json").read_text())
        make_figures(d["points"])
        return
    seeds = [1, 2, 3] if not a.quick else [1]
    dur = 1.5 if a.quick else 6.0
    sets = ladder() if not a.quick else ladder()[::3]
    jobs = [{"n_tasks": len(ts), "added": ts[-1], "policy": p, "seed": s,
             "kwargs": dict(scenario_yaml="S6", policy_yaml=p, seed=s, duration_s=dur,
                            overrides={"chip_only": True, "tasks_only": ts})}
            for ts in sets for p in POLICIES for s in seeds]
    runs = run_many(jobs, a.workers, slim=slim_result)
    points = []
    for ts in sets:
        for p in POLICIES:
            rs = [r for r in runs if r["_job"]["n_tasks"] == len(ts) and r["_job"]["policy"] == p and r.get("_ok")]
            per_task = {}
            for t in ts:
                xs = [r["tasks"].get(t) for r in rs if r["tasks"].get(t)]
                comp = sum(x["completed"] for x in xs)
                miss = sum(x["missed"] for x in xs)
                per_task[t] = {"miss_rate": miss / comp if comp else None, "missed": miss, "completed": comp,
                               "p99_ms": mean_ci([x["response_ms"]["p99"] for x in xs]),
                               "deadline_ms": (xs[0]["deadline_us"] or 0) / 1000 if xs else None}
            jobs_total = sum(v["completed"] for v in per_task.values())
            miss_total = sum(v["missed"] for v in per_task.values())
            points.append({
                "n_tasks": len(ts), "added": ts[-1], "policy": p, "tasks": ts,
                "miss_rate_all": miss_total / jobs_total if jobs_total else None,
                "misses_per_s": mean_ci([r["misses"]["total"] / r["duration_s"] for r in rs]),
                "safety_misses": int(sum(per_task[t]["missed"] for t in ts if t in SAFETY)),
                "tasks_missing": sorted([t for t, v in per_task.items() if v["missed"]]),
                "vla_p99_ms": per_task.get("T4_vla", {}).get("p99_ms", {}).get("mean"),
                "dram_gbps": mean_ci([r["activity"]["dram_gbps"] for r in rs]),
                "npu_util": mean_ci([r["activity"]["utilization"].get("npu", 0) for r in rs]),
                "avg_power_w": mean_ci([r["power"]["avg_power_w"] for r in rs]),
                "per_task": per_task,
            })
    first_miss = {p: next((pt["n_tasks"] for pt in points if pt["policy"] == p and pt["misses_per_s"]["mean"]), None)
                  for p in POLICIES}
    headline = {"first_task_count_with_misses": first_miss,
                "full_s6_misses_per_s": {p: next(pt["misses_per_s"]["mean"] for pt in points
                                                 if pt["policy"] == p and pt["n_tasks"] == len(sets[-1])) for p in POLICIES},
                "full_s6_tasks_missing": {p: next(pt["tasks_missing"] for pt in points
                                                  if pt["policy"] == p and pt["n_tasks"] == len(sets[-1])) for p in POLICIES},
                "full_s6_vla_p99_ms": {p: next(pt["vla_p99_ms"] for pt in points
                                               if pt["policy"] == p and pt["n_tasks"] == len(sets[-1])) for p in POLICIES}}
    figs = make_figures(points)
    write_json("s02", {"headline": headline, "points": points, "figures": figs,
                       "provenance": provenance("studies/s02_concurrency.py", ["configs/scenarios/S6_fifteen_at_once.yaml",
                                                "configs/policies/P0_nominal.yaml", "configs/policies/P2_edf.yaml"],
                                                seeds, {"duration_s": dur, "mode": "chip_only", "quick": a.quick}, runs),
                       "runs": [{k: r.get(k) for k in ("_job", "_ok", "misses", "tasks", "wall_s", "error")} for r in runs]})
    print(headline)


def make_figures(points) -> list[str]:
    plt = style()
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.2), constrained_layout=True)
    for p, c in (("P0", "#D1495B"), ("P2", "#2E86AB")):
        pts = [pt for pt in points if pt["policy"] == p]
        n = [pt["n_tasks"] for pt in pts]
        m = [pt["misses_per_s"]["mean"] or 0 for pt in pts]
        lo = [pt["misses_per_s"]["lo"] or 0 for pt in pts]
        hi = [pt["misses_per_s"]["hi"] or 0 for pt in pts]
        axes[0].plot(n, m, "-o", color=c, ms=4, label=f"{p} ({'FIFO' if p == 'P0' else 'EDF'})")
        axes[0].fill_between(n, np.maximum(lo, 0), hi, color=c, alpha=0.15, lw=0)
        axes[1].plot(n, [pt["vla_p99_ms"] if pt["vla_p99_ms"] is not None else np.nan for pt in pts], "-o", color=c,
                     ms=4, label=p)
    short = {"T2_detect_overhead": "T2 ovh", "T2_detect": "T2"}
    labels = {pt["n_tasks"]: short.get(pt["added"], pt["added"].split("_")[0]) for pt in points}
    for ax in axes:
        ax.set_xticks(sorted(labels), [f"{k}\n+{labels[k]}" for k in sorted(labels)], fontsize=7.5)
        ax.set_xlabel("task graphs running (last one added)")
    axes[0].set_ylabel("deadline misses per second")
    axes[0].set_ylim(bottom=0)
    axes[0].set_title("Deadline misses as tasks are added")
    axes[0].legend()
    axes[1].axhline(100, color="#D1495B", ls="--", lw=1)
    axes[1].set_ylabel("VLA (T4) p99 response (ms)")
    axes[1].set_title("VLA tail latency against its 100 ms deadline")
    return savefig(fig, "s02_concurrency", "Chip-only runs at interactive fidelity, nominal V/f. Shaded: 95 % CI over "
                   "seeds. VX-1 is fictional; parameters are assumptions.")


if __name__ == "__main__":
    main()
