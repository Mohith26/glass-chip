"""s03: root-cause attribution of deadline misses.

Question: for every job that missed its deadline, which resource dominated the delay?

Method: run the scenarios that produce misses (S7 bandwidth hog, S8 low-voltage sweep, plus
S2 and S6 as controls) chip-only at interactive fidelity under P0 (FIFO, nominal V/f), two
seeds each. The core splits every job's response time exactly into compute, run-queue wait,
engine-queue wait, NoC, DRAM, cache, DMA, preemption, DVFS stall and middleware, and reports
the critical path with the wait cause of each op. For each missed job I take the largest
attribution component as its dominant cause, and I also sum critical-path waits by cause.

Run: .venv/bin/python studies/s03_root_cause.py [--quick]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import provenance, run_many, savefig, slim_result, style, write_json  # noqa: E402

KEYS = ("compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware")
CASES = [("S7", "P0", 16.0), ("S8", "P0", 20.0), ("S6", "P0", 12.0), ("S2", "P0", 10.0)]
KEY_COLORS = {"compute": "#2E86AB", "runqueue": "#D1495B", "engine_queue": "#F0A202", "noc": "#8E7CC3",
              "dram": "#3BB273", "cache": "#6C8EBF", "dma": "#B5838D", "preemption": "#555555", "dvfs": "#E07A5F",
              "middleware": "#9AA5B1"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args()
    seeds = [1, 2] if not a.quick else [1]
    jobs = [{"scenario": s, "policy": p, "seed": sd,
             "kwargs": dict(scenario_yaml=s, policy_yaml=p, seed=sd, duration_s=(d if not a.quick else min(d, 3.0)),
                            overrides={"chip_only": True})}
            for s, p, d in CASES for sd in seeds]
    runs = run_many(jobs, a.workers, slim=lambda r: slim_result(r, keep_missed=True))
    by_case: dict[str, dict] = {}
    for s, p, _ in CASES:
        rs = [r for r in runs if r["_job"]["scenario"] == s and r.get("_ok")]
        missed = [m for r in rs for m in r.get("missed_jobs", [])]
        per_task: dict[str, dict] = {}
        for t in sorted({m["task_id"] for m in missed}):
            ms = [m for m in missed if m["task_id"] == t]
            att = {k: float(np.mean([m["attribution_ms"].get(k, 0.0) for m in ms])) for k in KEYS}
            tot = sum(att.values()) or 1.0
            dom = Counter(m["dominant_attribution"] for m in ms)
            # "Delay" excludes the job's own compute: which resource made it wait?
            wait_dom = Counter(max((k for k in KEYS if k != "compute"), key=lambda k: m["attribution_ms"].get(k, 0.0))
                               for m in ms)
            cp = defaultdict(float)
            for m in ms:
                for k, v in (m.get("critical_path_wait_ms") or {}).items():
                    cp[k] += v
            ok_att = {}
            for r in rs:
                st = r["tasks"].get(t, {})
                for k, v in (st.get("attribution_mean_ms") or {}).items():
                    ok_att.setdefault(k, []).append(v)
            per_task[t] = {
                "missed": len(ms), "mean_response_ms": float(np.mean([m["response_ms"] for m in ms])),
                "deadline_ms": float(np.mean([(m["deadline_ps"] - m["release_ps"]) / 1e9 for m in ms])),
                "attribution_mean_ms": att, "attribution_share": {k: v / tot for k, v in att.items()},
                "dominant_attribution_counts": dict(dom), "dominant_wait_counts": dict(wait_dom),
                "dominant_wait": wait_dom.most_common(1)[0][0] if wait_dom else None,
                "critical_path_wait_ms_total": dict(cp),
                "all_jobs_attribution_mean_ms": {k: float(np.mean(v)) for k, v in ok_att.items()},
            }
        by_case[f"{s}_{p}"] = {"scenario": s, "policy": p, "missed_jobs": len(missed),
                               "safety_missed": int(sum(r["misses"]["safety"] for r in rs)), "tasks": per_task}
    overall_wait = Counter()
    for c in by_case.values():
        for t, v in c["tasks"].items():
            for k, n in v["dominant_wait_counts"].items():
                overall_wait[k] += n
    headline = {"missed_jobs_by_case": {k: v["missed_jobs"] for k, v in by_case.items()},
                "dominant_wait_overall": dict(overall_wait),
                "dominant_wait_by_case_task": {k: {t: v["dominant_wait"] for t, v in c["tasks"].items()}
                                               for k, c in by_case.items()}}
    figs = make_figures(by_case)
    write_json("s03", {"headline": headline, "cases": by_case, "figures": figs,
                       "definitions": {"dominant_wait": "largest non-compute attribution component of a missed job",
                                       "attribution": "core's exact split of response time; components sum to the response"},
                       "provenance": provenance("studies/s03_root_cause.py",
                                                [f"configs/scenarios/{s}" for s, _, _ in CASES] + ["configs/policies/P0_nominal.yaml"],
                                                seeds, {"mode": "chip_only", "quick": a.quick}, runs)})
    print(headline)


def make_figures(by_case) -> list[str]:
    plt = style()
    rows = [(c, t, v) for c, cv in by_case.items() for t, v in cv["tasks"].items() if v["missed"] >= 3]
    fig, ax = plt.subplots(figsize=(11, 0.42 * max(len(rows), 3) + 1.4), constrained_layout=True)
    y = np.arange(len(rows))
    left = np.zeros(len(rows))
    for k in KEYS:
        w = np.array([r[2]["attribution_share"][k] for r in rows])
        if w.sum() == 0:
            continue
        ax.barh(y, w, left=left, color=KEY_COLORS[k], label=k.replace("_", " "))
        left += w
    ax.set_yticks(y, [f"{c.split('_')[0]}  {t.replace('_', ' ')}  ({v['missed']} missed)" for c, t, v in rows], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xlabel("share of response time, averaged over missed jobs")
    ax.set_title("Where the time went in jobs that missed their deadline")
    ax.legend(ncol=5, loc="lower center", bbox_to_anchor=(0.5, 1.06), fontsize=8)
    return savefig(fig, "s03_root_cause", "Chip-only runs under P0 (FIFO, nominal V/f). Attribution from glasschip_core. "
                   "VX-1 is fictional; parameters are assumptions.")


if __name__ == "__main__":
    main()
