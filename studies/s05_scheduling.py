"""s05: scheduling policies.

Question: how do FIFO (P0), fixed priority / rate monotonic (P1), EDF (P2) and
mixed-criticality EDF (edf_mc: safety tasks are never preempted by lower criticality) compare
on deadline misses across scenarios?

Method: chip-only runs at interactive fidelity, nominal V/f, 3 seeds, scenarios S1, S2, S6
and S7 (S7 with its bitrate ramp). edf_mc is P2 with the scheduler overridden.

Run: .venv/bin/python studies/s05_scheduling.py [--quick]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import mean_ci, provenance, run_many, savefig, slim_result, style, write_json  # noqa: E402

SCENARIOS = {"S1": 8.0, "S2": 8.0, "S6": 12.0, "S7": 16.0}
POLICIES = {"P0 FIFO": ("P0", {}), "P1 fixed priority": ("P1", {}), "P2 EDF": ("P2", {}),
            "EDF mixed-criticality": ("P2", {"policy": {"scheduler": "edf_mc"}})}
COLORS = ["#D1495B", "#F0A202", "#2E86AB", "#3BB273"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args()
    seeds = [1, 2, 3] if not a.quick else [1]
    jobs = [{"scenario": s, "policy": name, "seed": sd,
             "kwargs": dict(scenario_yaml=s, policy_yaml=pol, seed=sd, duration_s=(d if not a.quick else 2.0),
                            overrides={**ov, "chip_only": True})}
            for s, d in SCENARIOS.items() for name, (pol, ov) in POLICIES.items() for sd in seeds]
    runs = run_many(jobs, a.workers, slim=slim_result)
    cells = []
    for s in SCENARIOS:
        for name in POLICIES:
            rs = [r for r in runs if r["_job"]["scenario"] == s and r["_job"]["policy"] == name and r.get("_ok")]
            tasks = sorted({t for r in rs for t in r["tasks"]})
            per = {}
            for t in tasks:
                xs = [r["tasks"][t] for r in rs if t in r["tasks"]]
                comp = sum(x["completed"] for x in xs)
                miss = sum(x["missed"] for x in xs)
                per[t] = {"miss_rate": miss / comp if comp else None, "missed": miss,
                          "p99_ms": mean_ci([x["response_ms"]["p99"] for x in xs])}
            comp_all = sum(v["missed"] for v in per.values())
            cells.append({"scenario": s, "policy": name, "tasks": per,
                          "misses_per_s": mean_ci([r["misses"]["total"] / r["duration_s"] for r in rs]),
                          "safety_misses": int(sum(r["misses"]["safety"] for r in rs)),
                          "missed_total": comp_all,
                          "vla_p99_ms": per.get("T4_vla", {}).get("p99_ms", {}).get("mean"),
                          "t8_p99_ms": per.get("T8_safety", {}).get("p99_ms", {}).get("mean"),
                          "t6_p99_ms": per.get("T6_estimate", {}).get("p99_ms", {}).get("mean")})
    headline = {f"{c['scenario']} / {c['policy']}": {"misses_per_s": c["misses_per_s"]["mean"],
                                                      "safety_misses": c["safety_misses"],
                                                      "vla_p99_ms": c["vla_p99_ms"]} for c in cells}
    figs = make_figures(cells)
    write_json("s05", {"headline": headline, "cells": cells, "figures": figs,
                       "provenance": provenance("studies/s05_scheduling.py",
                                                [f"configs/scenarios/{s}" for s in SCENARIOS] +
                                                ["configs/policies/P0_nominal.yaml", "configs/policies/P1_fixed_priority.yaml",
                                                 "configs/policies/P2_edf.yaml"], seeds,
                                                {"mode": "chip_only", "quick": a.quick}, runs)})
    print(headline)


def make_figures(cells) -> list[str]:
    plt = style()
    scen = list(SCENARIOS)
    pols = list(POLICIES)
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.2), constrained_layout=True)
    w = 0.8 / len(pols)
    for i, p in enumerate(pols):
        ms = [next(c for c in cells if c["scenario"] == s and c["policy"] == p) for s in scen]
        x = np.arange(len(scen)) + (i - (len(pols) - 1) / 2) * w
        axes[0].bar(x, [m["misses_per_s"]["mean"] or 0 for m in ms], w, color=COLORS[i], label=p)
        axes[1].bar(x, [m["safety_misses"] for m in ms], w, color=COLORS[i], label=p)
    for ax in axes:
        ax.set_xticks(np.arange(len(scen)), scen)
    axes[0].set_ylabel("deadline misses per second (all tasks)")
    axes[0].set_title("Deadline misses by scheduler")
    axes[0].legend(fontsize=8)
    axes[1].set_ylabel("safety-critical misses (T7, T8, T12; all seeds)")
    axes[1].set_title("Safety-critical misses by scheduler")
    return savefig(fig, "s05_scheduling", "Chip-only, nominal V/f, interactive fidelity, mean over seeds. "
                   "VX-1 is fictional; parameters are assumptions.")


if __name__ == "__main__":
    main()
