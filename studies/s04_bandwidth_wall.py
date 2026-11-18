"""s04: the bandwidth wall (scenario S7).

Question: at what recording bitrate does the robot stack start missing deadlines (the VLA in
particular), what resource is responsible, and do QoS arbitration, system-cache partitioning
or EDF scheduling fix it?

Method: S7's task set (robot stack plus raw 4K recording, T10_record_4k) at a constant
recording bitrate per run, chip-only at interactive fidelity, nominal V/f, 2 seeds per point.
Configurations: P0 (FIFO), P0 + QoS + cache partitioning, P2 (EDF), P2 + QoS + cache
partitioning, mixed-criticality EDF (edf_mc), and edf_mc + QoS + cache partitioning. The
partition gives the safety class 2 ways, real-time 10 and best-effort 4 of the 16 (an
assumption). Runs are 10 s: above the wall the recorder (on_overrun: queue) builds a backlog,
so short runs understate late-onset misses.

Run: .venv/bin/python studies/s04_bandwidth_wall.py [--quick]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from studies.common import mean_ci, provenance, run_many, savefig, slim_result, style, write_json  # noqa: E402

BITRATES_GBPS = [1.0, 5.97, 11.9, 24.0, 48.0, 96.0]
PART = {"safety": 2, "realtime": 10, "best_effort": 4}
MC = {"policy": {"scheduler": "edf_mc"}}
CONFIGS = {
    "P0": ("P0", {}),
    "P0+QoS+part": ("P0", {"qos": True, "cache_partition": PART}),
    "P2": ("P2", {}),
    "P2+QoS+part": ("P2", {"qos": True, "cache_partition": PART}),
    "EDF-MC": ("P2", MC),
    "EDF-MC+QoS+part": ("P2", {**MC, "qos": True, "cache_partition": PART}),
}
COLORS = {"P0": "#D1495B", "P0+QoS+part": "#8E7CC3", "P2": "#2E86AB", "P2+QoS+part": "#3BB273",
          "EDF-MC": "#F0A202", "EDF-MC+QoS+part": "#222222"}
WATCH = ["T4_vla", "T2_detect", "T6_estimate", "T8_safety", "T1_ingest_wrist", "T10_record_4k"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args()
    seeds = [1, 2] if not a.quick else [1]
    dur = 1.0 if a.quick else 10.0
    rates = BITRATES_GBPS if not a.quick else [1.0, 96.0]
    jobs = []
    for name, (pol, ov) in CONFIGS.items():
        for br in rates:
            for s in seeds:
                ev = [{"t_s": 0.0, "action": "set_bitrate", "args": {"task": "T10_record_4k", "bitrate_bps": br * 1e9}}]
                jobs.append({"config": name, "bitrate_gbps": br, "seed": s,
                             "kwargs": dict(scenario_yaml="S7", policy_yaml=pol, seed=s, duration_s=dur,
                                            overrides={**ov, "chip_only": True, "scenario": {"events": ev}})})
    runs = run_many(jobs, a.workers, slim=slim_result)
    points = []
    for name in CONFIGS:
        for br in rates:
            rs = [r for r in runs if r["_job"]["config"] == name and r["_job"]["bitrate_gbps"] == br and r.get("_ok")]
            per = {}
            for t in WATCH:
                xs = [r["tasks"].get(t) for r in rs if r["tasks"].get(t)]
                comp = sum(x["completed"] for x in xs)
                miss = sum(x["missed"] for x in xs)
                per[t] = {"miss_rate": miss / comp if comp else None, "missed": miss,
                          "p99_ms": mean_ci([x["response_ms"]["p99"] for x in xs]),
                          "dominant_missed": [r["attribution"].get(t, {}).get("dominant_missed") for r in rs]}
            points.append({"config": name, "bitrate_gbps": br, "tasks": per,
                           "safety_misses": int(sum(r["misses"]["safety"] for r in rs)),
                           "robot_misses": int(sum(sum(v for t, v in r["misses"]["by_task"].items()
                                                       if t != "T10_record_4k") for r in rs)),
                           "dram_gbps": mean_ci([r["activity"]["dram_gbps"] for r in rs]),
                           "avg_power_w": mean_ci([r["power"]["avg_power_w"] for r in rs])})

    def wall(name, task="T4_vla"):
        for pt in points:
            if pt["config"] == name and (pt["tasks"][task]["miss_rate"] or 0) > 0:
                return pt["bitrate_gbps"]
        return None

    def robot_wall(name):
        for pt in points:
            if pt["config"] == name and pt["robot_misses"] > 0:
                return pt["bitrate_gbps"]
        return None

    headline = {"vla_first_miss_bitrate_gbps": {n: wall(n) for n in CONFIGS},
                "robot_stack_first_miss_bitrate_gbps": {n: robot_wall(n) for n in CONFIGS},
                "safety_misses_at_max_bitrate": {n: next(pt["safety_misses"] for pt in points
                                                         if pt["config"] == n and pt["bitrate_gbps"] == rates[-1])
                                                 for n in CONFIGS},
                "dram_gbps_at_max_bitrate": {n: next(pt["dram_gbps"]["mean"] for pt in points
                                                     if pt["config"] == n and pt["bitrate_gbps"] == rates[-1])
                                             for n in CONFIGS}}
    figs = make_figures(points, rates)
    write_json("s04", {"headline": headline, "points": points, "figures": figs,
                       "assumptions": {"cache_partition_ways": PART, "recording_dram_passes": 3},
                       "provenance": provenance("studies/s04_bandwidth_wall.py",
                                                ["configs/scenarios/S7_bandwidth_hog.yaml", "workloads/graphs/T10_record_4k.json"],
                                                seeds, {"duration_s": dur, "mode": "chip_only", "quick": a.quick}, runs)})
    print(headline)


def make_figures(points, rates) -> list[str]:
    plt = style()
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.3), constrained_layout=True)
    for name in CONFIGS:
        pts = [pt for pt in points if pt["config"] == name]
        x = [pt["bitrate_gbps"] for pt in pts]
        axes[0].plot(x, [pt["tasks"]["T4_vla"]["p99_ms"]["mean"] for pt in pts], "-o", ms=4, color=COLORS[name], label=name)
        axes[1].plot(x, [pt["robot_misses"] for pt in pts], "-o", ms=4, color=COLORS[name], label=name)
        axes[2].plot(x, [pt["dram_gbps"]["mean"] for pt in pts], "-o", ms=4, color=COLORS[name], label=name)
    for ax in axes:
        ax.set_xscale("log")
        ax.set_xlabel("recording bitrate (Gbit/s)")
    axes[0].axhline(100, color="#D1495B", ls="--", lw=1)
    axes[0].set_ylabel("VLA p99 response (ms)")
    axes[0].set_title("VLA tail latency vs recording bitrate")
    axes[0].legend(fontsize=8)
    axes[1].set_ylabel("robot-stack deadline misses (all seeds)")
    axes[1].set_title("Misses outside the recorder")
    axes[2].set_ylabel("DRAM traffic (GB/s)")
    axes[2].set_title("Average DRAM traffic")
    return savefig(fig, "s04_bandwidth_wall", "Chip-only, nominal V/f, interactive fidelity. 5.97 Gbit/s is raw 4K30; "
                   "higher points stand for several raw streams. VX-1 is fictional; parameters are assumptions.")


if __name__ == "__main__":
    main()
