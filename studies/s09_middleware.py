"""s09: the middleware tax (copy versus zero-copy transport).

Question: how much end-to-end latency and DRAM traffic does ROS 2 style message passing cost
on VX-1, and how much does zero-copy (loaned messages) save?

Method: closed-loop co-sim (P0, interactive fidelity), 3 seeds, scenarios S1, S2 and S6 with
transport = copy or zero_copy (single-threaded executor), plus the three executor models
(single-threaded, multi-threaded, events) on S1. Middleware costs come from
workloads/graphs/middleware.json, which was measured on a Mac in Docker and scaled by a
documented factor, not measured on a robot SoC.

Run: .venv/bin/python studies/s09_middleware.py [--quick]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import mean_ci, provenance, run_many, savefig, slim_result, style, write_json  # noqa: E402

CASES = ([(s, tr, "single_threaded") for s in ("S1", "S2", "S6") for tr in ("copy", "zero_copy")] +
         [("S1", tr, ex) for tr in ("copy", "zero_copy") for ex in ("multi_threaded", "events")])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--replot", action="store_true")
    a = ap.parse_args()
    if a.replot:
        import json
        from studies.common import RESULTS
        pth = RESULTS / "s09.json"
        d = json.loads(pth.read_text())
        d["figures"] = make_figures(d["points"])
        d["notes"] = ("chain latencies are measured at 1 ms physics steps, so their medians move in 1 ms "
                      "increments; middleware_ms_per_job is the core's exact per-job middleware attribution")
        pth.write_text(json.dumps(d, indent=1))
        return
    seeds = [1, 2, 3] if not a.quick else [1]
    dur = 2.0 if a.quick else 8.0
    jobs = [{"scenario": s, "transport": tr, "executor": ex, "seed": sd,
             "kwargs": dict(scenario_yaml=s, policy_yaml="P0", seed=sd, duration_s=dur,
                            overrides={"scenario": {"transport": tr, "executor": ex}})}
            for s, tr, ex in CASES for sd in seeds]
    runs = run_many(jobs, a.workers, slim=slim_result)
    points = []
    for s, tr, ex in CASES:
        rs = [r for r in runs if (r["_job"]["scenario"], r["_job"]["transport"], r["_job"]["executor"]) == (s, tr, ex)
              and r.get("_ok")]

        def task_mean(t, key):
            return mean_ci([r["tasks"][t][key] if key != "mw" else (r["tasks"][t]["attribution_mean_ms"].get("middleware", 0.0))
                            for r in rs if t in r["tasks"]])

        points.append({
            "scenario": s, "transport": tr, "executor": ex,
            "camera_to_detection_p50_ms": mean_ci([r["chains"]["camera_to_detection"]["p50"] for r in rs]),
            "camera_to_detection_p99_ms": mean_ci([r["chains"]["camera_to_detection"]["p99"] for r in rs]),
            "camera_to_grasp_p50_ms": mean_ci([r["chains"]["camera_to_grasp"]["p50"] for r in rs]),
            "human_to_slowdown_p50_ms": mean_ci([r["chains"]["human_to_slowdown"]["p50"] for r in rs]),
            "dram_gbps": mean_ci([r["activity"]["dram_gbps"] for r in rs]),
            "dma_gbps": mean_ci([r["activity"]["dma_bytes"] / r["duration_s"] / 1e9 for r in rs]),
            "middleware_ms_per_job": {t: task_mean(t, "mw")["mean"] for t in
                                      ("T1_ingest_wrist", "T2_detect", "T4_vla", "T6_estimate", "T8_safety")},
            "misses_total": int(sum(r["misses"]["total"] for r in rs)),
            "avg_power_w": mean_ci([r["power"]["avg_power_w"] for r in rs]),
        })

    def get(s, tr, ex="single_threaded"):
        return next(p for p in points if (p["scenario"], p["transport"], p["executor"]) == (s, tr, ex))

    headline = {}
    for s in ("S1", "S2", "S6"):
        c, z = get(s, "copy"), get(s, "zero_copy")
        headline[s] = {
            "camera_to_detection_p50_ms": {"copy": c["camera_to_detection_p50_ms"]["mean"],
                                           "zero_copy": z["camera_to_detection_p50_ms"]["mean"]},
            "dram_gbps": {"copy": c["dram_gbps"]["mean"], "zero_copy": z["dram_gbps"]["mean"]},
            "dram_saving_frac": (1 - z["dram_gbps"]["mean"] / c["dram_gbps"]["mean"]) if c["dram_gbps"]["mean"] else None,
        }
    figs = make_figures(points)
    write_json("s09", {"headline": headline, "points": points, "figures": figs,
                       "notes": ("chain latencies are measured at 1 ms physics steps, so their medians move in 1 ms "
                                 "increments; middleware_ms_per_job is the core's exact per-job middleware attribution"),
                       "assumptions": {"middleware_costs": "workloads/graphs/middleware.json (Mac in Docker, scaled; see its provenance)"},
                       "provenance": provenance("studies/s09_middleware.py",
                                                ["configs/scenarios/S1_nominal.yaml", "configs/scenarios/S2_second_camera.yaml",
                                                 "configs/scenarios/S6_fifteen_at_once.yaml", "workloads/graphs/middleware.json"],
                                                seeds, {"duration_s": dur, "mode": "cosim", "quick": a.quick}, runs)})
    print(headline)


def make_figures(points) -> list[str]:
    plt = style()
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.3), constrained_layout=True)
    tasks = ["T1_ingest_wrist", "T2_detect", "T4_vla", "T6_estimate", "T8_safety"]
    combos = [(tr, ex) for ex in ("single_threaded", "multi_threaded", "events") for tr in ("copy", "zero_copy")]
    cols = {"copy": "#D1495B", "zero_copy": "#2E86AB"}
    hatch = {"single_threaded": "", "multi_threaded": "//", "events": ".."}
    w = 0.8 / len(combos)
    x = np.arange(len(tasks))
    for i, (tr, ex) in enumerate(combos):
        p = next(q for q in points if q["scenario"] == "S1" and q["transport"] == tr and q["executor"] == ex)
        axes[0].bar(x + (i - (len(combos) - 1) / 2) * w, [p["middleware_ms_per_job"][t] or 0 for t in tasks], w,
                    color=cols[tr], hatch=hatch[ex], edgecolor="white", lw=0,
                    label=f"{tr.replace('_', '-')}, {ex.replace('_', '-')}")
    axes[0].set_xticks(x, [t.split("_")[0] for t in tasks])
    axes[0].set_ylabel("middleware time per job (ms)")
    axes[0].set_title("S1: middleware time per job by transport and executor")
    axes[0].legend(fontsize=7.5, ncol=2, loc="upper right")
    scen = ["S1", "S2", "S6"]
    xs = np.arange(len(scen))
    for i, tr in enumerate(("copy", "zero_copy")):
        ps = [next(p for p in points if p["scenario"] == s and p["transport"] == tr and p["executor"] == "single_threaded")
              for s in scen]
        axes[1].bar(xs + (i - 0.5) * 0.38, [p["dram_gbps"]["mean"] for p in ps], 0.38, color=cols[tr],
                    label=tr.replace("_", "-"))
    axes[1].set_xticks(xs, scen)
    axes[1].set_ylabel("average DRAM traffic (GB/s)")
    axes[1].set_title("DRAM traffic by transport (single-threaded executor)")
    axes[1].set_ylim(0, max(p["dram_gbps"]["mean"] for p in points) * 1.25)
    axes[1].legend(loc="upper left", ncol=2)
    return savefig(fig, "s09_middleware", "Closed-loop co-sim, P0, mean over seeds. Middleware costs measured on a Mac in "
                   "Docker and scaled (assumption, workloads/graphs/middleware.json). VX-1 is fictional.")


if __name__ == "__main__":
    main()
