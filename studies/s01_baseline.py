"""s01: baseline characterization.

Question: how much of VX-1 does each robot task use on its own, and what changes when the
nominal pick scenario (S1) runs them together?

Part A, each task alone (chip only, P0 nominal V/f, FIFO): response time p50/p99 against the
deadline, busy fraction per power domain, average SoC power.
Part B, S1 in closed loop (co-sim, P0, 3 seeds): the same per-task numbers under concurrency,
deadline miss rates, end-to-end chain latencies, utilization and power by domain, robot metrics.

Run: .venv/bin/python studies/s01_baseline.py [--quick]
Writes results/s01.json and results/figures/s01_*.svg/png.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import (TASK_ORDER, mean_ci, provenance, run_many, savefig, slim_result, style,  # noqa: E402
                            task_color, write_json)

ALONE = ["T1_ingest_wrist", "T1_ingest_overhead", "T2_detect", "T2_detect_overhead", "T3_esdf", "T4_vla", "T5_plan",
         "T6_estimate", "T7_control", "T8_safety", "T10_record", "T10_record_4k", "T11_speech", "T12_arm2_control",
         "T13_map_save", "T14_ota", "T15_diag"]
DOMAINS = ["cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args()
    seeds = [1, 2, 3] if not a.quick else [1]
    d_alone = 1.0 if a.quick else 3.0
    d_s1 = 3.0 if a.quick else 10.0
    jobs = [{"part": "alone", "task": t, "kwargs": dict(scenario_yaml="S1", policy_yaml="P0", seed=1, duration_s=d_alone,
                                                       overrides={"chip_only": True, "tasks_only": [t]})}
            for t in ALONE]
    jobs += [{"part": "S1", "seed": s, "kwargs": dict(scenario_yaml="S1", policy_yaml="P0", seed=s, duration_s=d_s1)}
             for s in seeds]
    runs = run_many(jobs, a.workers, slim=slim_result)

    alone = {}
    for r in runs:
        if r["_job"]["part"] != "alone" or not r.get("_ok"):
            continue
        t = r["_job"]["task"]
        st = r["tasks"].get(t, {})
        alone[t] = {"response_ms": st.get("response_ms"), "deadline_ms": (st.get("deadline_us") or 0) / 1000,
                    "completed": st.get("completed"), "missed": st.get("missed"),
                    "utilization": {d: r["activity"]["utilization"].get(d, 0.0) for d in DOMAINS},
                    "unit_utilization": {d: r["activity"]["unit_utilization"].get(d, 0.0) for d in DOMAINS},
                    "avg_power_w": r["power"]["avg_power_w"], "dram_gbps": r["activity"]["dram_gbps"],
                    "attribution_mean_ms": st.get("attribution_mean_ms")}
    s1 = [r for r in runs if r["_job"]["part"] == "S1" and r.get("_ok")]
    tasks = sorted({t for r in s1 for t in r["tasks"]}, key=lambda t: TASK_ORDER.index(t) if t in TASK_ORDER else 99)
    s1_tasks = {}
    for t in tasks:
        rs = [r["tasks"][t] for r in s1 if t in r["tasks"]]
        comp = sum(x["completed"] for x in rs)
        miss = sum(x["missed"] for x in rs)
        s1_tasks[t] = {"completed": comp, "missed": miss, "miss_rate": miss / comp if comp else None,
                       "deadline_ms": (rs[0].get("deadline_us") or 0) / 1000,
                       "p50_ms": mean_ci([x["response_ms"]["p50"] for x in rs]),
                       "p99_ms": mean_ci([x["response_ms"]["p99"] for x in rs]),
                       "max_ms": max(x["response_ms"]["max"] for x in rs)}
    s1_summary = {
        "utilization": {d: mean_ci([r["activity"]["utilization"].get(d, 0.0) for r in s1]) for d in DOMAINS},
        "unit_utilization": {d: mean_ci([r["activity"]["unit_utilization"].get(d, 0.0) for r in s1]) for d in DOMAINS},
        "avg_power_w": mean_ci([r["power"]["avg_power_w"] for r in s1]),
        "energy_per_robot_hour_wh": mean_ci([r["power"]["energy_per_robot_hour_wh"] for r in s1]),
        "peak_power_w": mean_ci([r["power"]["peak_power_w"] for r in s1]),
        "peak_temp_c": mean_ci([r["power"]["peak_temp_c"] for r in s1]),
        "power_w_by_domain": {d: mean_ci([r["power"]["energy_j_by_domain"].get(d, 0.0) / r["duration_s"] for r in s1])
                              for d in DOMAINS},
        "dram_gbps": mean_ci([r["activity"]["dram_gbps"] for r in s1]),
        "misses_total": sum(r["misses"]["total"] for r in s1),
        "safety_misses": sum(r["misses"]["safety"] for r in s1),
        "chains_ms": {k: {"p50": mean_ci([r["chains"][k]["p50"] for r in s1]), "p99": mean_ci([r["chains"][k]["p99"] for r in s1])}
                      for k in s1[0]["chains"]} if s1 else {},
        "robot": {k: mean_ci([r["robot"][k] for r in s1]) for k in
                  ("grasp_success_rate", "grasp_attempts", "placed", "joint_tracking_rms_rad", "oscillation_episodes")},
    }
    vla = alone.get("T4_vla", {})
    headline = {
        "s1_misses_total": s1_summary["misses_total"], "s1_safety_misses": s1_summary["safety_misses"],
        "vla_alone_p99_ms": (vla.get("response_ms") or {}).get("p99"),
        "vla_s1_p99_ms": s1_tasks.get("T4_vla", {}).get("p99_ms", {}).get("mean"),
        "s1_avg_power_w": s1_summary["avg_power_w"]["mean"],
        "s1_dram_gbps": s1_summary["dram_gbps"]["mean"],
        "s1_npu_utilization": s1_summary["utilization"]["npu"]["mean"],
        "s1_camera_to_grasp_p50_ms": s1_summary["chains_ms"].get("camera_to_grasp", {}).get("p50", {}).get("mean"),
    }
    figs = make_figures(alone, s1_tasks, s1_summary)
    write_json("s01", {"headline": headline, "alone": alone, "s1_tasks": s1_tasks, "s1": s1_summary, "figures": figs,
                       "provenance": provenance("studies/s01_baseline.py",
                                                ["configs/scenarios/S1_nominal.yaml", "configs/policies/P0_nominal.yaml",
                                                 "workloads/graphs/*.json"], seeds,
                                                {"duration_alone_s": d_alone, "duration_s1_s": d_s1, "quick": a.quick}, runs),
                       "runs": runs})
    print(headline)


def make_figures(alone, s1_tasks, s1) -> list[str]:
    plt = style()
    tasks = [t for t in s1_tasks if t in alone and s1_tasks[t]["deadline_ms"]]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.4), constrained_layout=True)
    ax = axes[0]
    x = np.arange(len(tasks))
    a99 = [alone[t]["response_ms"]["p99"] / s1_tasks[t]["deadline_ms"] for t in tasks]
    s99 = [s1_tasks[t]["p99_ms"]["mean"] / s1_tasks[t]["deadline_ms"] for t in tasks]
    ax.bar(x - 0.2, a99, 0.4, color=[task_color(t) for t in tasks], alpha=0.45, label="alone")
    ax.bar(x + 0.2, s99, 0.4, color=[task_color(t) for t in tasks], label="in S1")
    ax.axhline(1.0, color="#D1495B", lw=1.2, ls="--")
    ax.text(len(tasks) - 0.5, 1.02, "deadline", color="#D1495B", ha="right", fontsize=8)
    ax.set_xticks(x, [t.replace("_", " ") for t in tasks], rotation=40, ha="right", fontsize=8)
    ax.set_ylabel("p99 response / deadline")
    ax.set_title("Per-task p99 latency against its deadline")
    ax.legend(loc="upper left")
    ax = axes[1]
    doms = list(s1["utilization"])
    u = [s1["utilization"][d]["mean"] for d in doms]
    pw = [s1["power_w_by_domain"][d]["mean"] for d in doms]
    ax.bar(np.arange(len(doms)) - 0.2, u, 0.4, color="#2E86AB", label="busy fraction")
    ax2 = ax.twinx()
    ax2.bar(np.arange(len(doms)) + 0.2, pw, 0.4, color="#F0A202", label="power (W)")
    ax.set_xticks(np.arange(len(doms)), doms, rotation=30, ha="right")
    ax.set_ylabel("busy fraction")
    ax2.set_ylabel("average power (W)")
    ax2.grid(False)
    ax.set_title("S1: utilization and power by domain")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left")
    return savefig(fig, "s01_baseline", "VX-1 is fictional; all parameters are assumptions. P0 nominal V/f, FIFO, "
                   "interactive fidelity mix. S1 values are means over seeds.")


if __name__ == "__main__":
    main()
