"""s06: power policies. Energy per robot-hour at zero safety-critical deadline misses.

For each scenario (S1, S2, S6), seed, and policy I run the closed-loop co-sim (MuJoCo robot +
glasschip_core at the interactive fidelity + PowerManager) through studies/runner.py, then solve
the P5 offline lower bound on that run's recorded job trace (power/offline_opt.py, HiGHS).

Policies: P0 nominal (FIFO), P3 race to idle, P4 pace to deadline, P4G (P4 plus idle power
gating, a labeled variant), P6 with a PLACEHOLDER conservative finish-time bound (the conformal
surrogate is not ready; this is not P6's final form), and P5 as a lower bound.

Thermal: each run uses thermal_init="settle": after 1 s of burn-in the thermal state jumps to the
steady state (temperature-dependent leakage included) for the power measured so far, and energy is
accounted from there. Energy per robot-hour = average power over the accounted window x 1 h.
All power parameters are assumptions (power/params_vx1.yaml); VX-1 is fictional.

    .venv/bin/python studies/s06_power_policies.py [--seeds 1 2 3 4 5] [--workers 9] [--quick]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

RESULTS = ROOT / "results"
FIG = RESULTS / "figures"

POLICIES: dict[str, dict[str, Any]] = {
    "P0": {"policy": "P0", "overrides": {}},
    "P3": {"policy": "P3", "overrides": {}},
    "P4": {"policy": "P4", "overrides": {}},
    "P4G": {"policy": "P4", "overrides": {"policy": {"name": "P4G_pace_plus_gating", "params": {"gate_idle": True}}}},
    "P6ph": {"policy": "P6", "overrides": {"policy": {"name": "P6_placeholder_bound",
                                                      "params": {"placeholder_surrogate": True}}}},
    "P6": {"policy": "P6", "overrides": {"policy": {"name": "P6_conformal_surrogate"}}, "surrogate": True},
}
SCENARIOS = {"S1": 10.0, "S2": 10.0, "S6": 15.0}


def _job(args: tuple[str, str, int, float]) -> dict[str, Any]:
    scen, pkey, seed, dur = args
    from power.offline_opt import solve_trace
    from power.params import deep_merge
    from studies.runner import run

    spec = POLICIES[pkey]
    ov = deep_merge({"power": {"thermal_init": "settle"}}, spec["overrides"])
    if spec.get("surrogate"):
        # conformal surrogate from the surrogate worker (surrogate/p6.py), built inside the policy
        ov = deep_merge(ov, {"policy": {"params": {"surrogate_factory": {
            "callable": "surrogate.p6:SurrogateFinishBound", "kwargs": {"scenario": scen}}}}})
    t0 = time.time()
    r = run(scen, spec["policy"], seed=seed, duration_s=dur, return_objects=True, overrides=ov)
    pm = r["_objects"]["pm"]
    s = pm.summary()
    t1 = time.time()
    bound = solve_trace(pm.job_trace, pm, time_limit_s=60)
    tasks = {t: {k: v.get(k) for k in ("completed", "missed", "skipped", "miss_rate", "criticality")}
             | {"p99_ms": (v.get("response_ms") or {}).get("p99")} for t, v in r["tasks"].items()}
    safety_tasks = [t for t, v in r["tasks"].items() if v.get("criticality") == "safety"]
    return {
        "scenario": scen, "policy": pkey, "seed": seed, "duration_s": dur,
        "energy_per_robot_hour_wh": s["energy_per_robot_hour_wh"], "energy_j": s["energy_j"],
        "accounted_s": s["duration_s"], "peak_power_w": s["peak_power_w"], "peak_temp_c": s["peak_temp_c"],
        "energy_j_by_domain": s["energy_j_by_domain"], "energy_j_by_part": s["energy_j_by_part"],
        "dvfs_transitions": s["dvfs_transitions"], "wakeups": s["wakeups"], "settle": s["settle"],
        "surrogate": s.get("surrogate"), "surrogate_stats": s.get("surrogate_stats"),
        "misses_total": r["misses"]["total"], "safety_misses": r["misses"]["safety"],
        "skipped_total": int(sum(v.get("skipped", 0) or 0 for v in r["tasks"].values())),
        "safety_skipped": int(sum(r["tasks"][t].get("skipped", 0) or 0 for t in safety_tasks)),
        "tasks": tasks,
        "robot": {k: r["robot"].get(k) for k in ("grasp_attempts", "grasp_success", "grasp_success_rate", "placed",
                                                  "picks_per_min", "oscillation_episodes", "joint_tracking_rms_rad")},
        "p5_bound_on_this_trace": {"energy_j": bound["energy_j"],
                                   "energy_per_robot_hour_wh": bound["energy_per_robot_hour_wh"],
                                   "per_domain": bound["per_domain"], "t_ref_c": bound["t_ref_c"]},
        "provenance": r["provenance"], "wall_s": t1 - t0, "p5_solve_s": time.time() - t1,
    }


def ci95(xs: list[float]) -> dict[str, float | int | None]:
    from scipy import stats
    a = np.asarray(xs, float)
    n = len(a)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "sd": None}
    m = float(a.mean())
    if n == 1:
        return {"n": 1, "mean": m, "lo": None, "hi": None, "sd": None}
    sd = float(a.std(ddof=1))
    h = float(stats.t.ppf(0.975, n - 1)) * sd / math.sqrt(n)
    return {"n": n, "mean": m, "lo": m - h, "hi": m + h, "sd": sd}


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scen in sorted({r["scenario"] for r in rows}):
        sr = [r for r in rows if r["scenario"] == scen]
        seeds = sorted({r["seed"] for r in sr})
        by = {(r["policy"], r["seed"]): r for r in sr}
        pols: dict[str, Any] = {}
        for pk in POLICIES:
            pr = [by[(pk, s)] for s in seeds if (pk, s) in by]
            if not pr:
                continue
            pols[pk] = {
                "energy_per_robot_hour_wh": ci95([r["energy_per_robot_hour_wh"] for r in pr]),
                "p5_bound_wh": ci95([r["p5_bound_on_this_trace"]["energy_per_robot_hour_wh"] for r in pr]),
                "misses_total": int(sum(r["misses_total"] for r in pr)),
                "safety_misses": int(sum(r["safety_misses"] for r in pr)),
                "skipped_total": int(sum(r["skipped_total"] for r in pr)),
                "safety_skipped": int(sum(r["safety_skipped"] for r in pr)),
                "zero_safety_misses": all(r["safety_misses"] == 0 and r["safety_skipped"] == 0 for r in pr),
                "peak_temp_c_max": max(r["peak_temp_c"] for r in pr),
                "peak_power_w_max": max(r["peak_power_w"] for r in pr),
                "grasp_success": int(sum(r["robot"]["grasp_success"] or 0 for r in pr)),
                "grasp_attempts": int(sum(r["robot"]["grasp_attempts"] or 0 for r in pr)),
                "oscillation_episodes": int(sum(r["robot"]["oscillation_episodes"] or 0 for r in pr)),
                "misses_by_task": _sum_task(pr, "missed"),
                "skipped_by_task": _sum_task(pr, "skipped"),
                "energy_j_by_domain_mean": _mean_dict([r["energy_j_by_domain"] for r in pr]),
                "energy_j_by_part_mean": _mean_dict([r["energy_j_by_part"] for r in pr]),
            }
        # paired ratios vs P0 and the ordering check per seed
        ratios: dict[str, Any] = {}
        for pk in POLICIES:
            if pk == "P0":
                continue
            xs = [by[(pk, s)]["energy_per_robot_hour_wh"] / by[("P0", s)]["energy_per_robot_hour_wh"]
                  for s in seeds if (pk, s) in by and ("P0", s) in by]
            ratios[pk] = ci95(xs)
        ordering = []
        for s in seeds:
            if ("P0", s) not in by or ("P4", s) not in by:
                continue
            e0 = by[("P0", s)]["energy_per_robot_hour_wh"]
            e4 = by[("P4", s)]["energy_per_robot_hour_wh"]
            b0 = by[("P0", s)]["p5_bound_on_this_trace"]["energy_per_robot_hour_wh"]
            b4 = by[("P4", s)]["p5_bound_on_this_trace"]["energy_per_robot_hour_wh"]
            ordering.append({"seed": s, "p5_on_p4_trace_wh": b4, "p5_on_p0_trace_wh": b0, "p4_wh": e4, "p0_wh": e0,
                             "p5_le_p4": b4 <= e4 + 1e-12, "p4_le_p0": e4 <= e0 + 1e-12,
                             "p5_le_p4_le_p0": b4 <= e4 + 1e-12 and e4 <= e0 + 1e-12})
        out[scen] = {"seeds": seeds, "policies": pols, "ratio_vs_p0": ratios, "ordering": ordering,
                     "ordering_holds_all_seeds": all(o["p5_le_p4_le_p0"] for o in ordering) if ordering else None}
    return out


def _sum_task(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    acc: dict[str, int] = {}
    for r in rows:
        for t, v in r["tasks"].items():
            n = int(v.get(key) or 0)
            if n:
                acc[t] = acc.get(t, 0) + n
    return acc


def _mean_dict(ds: list[dict[str, float]]) -> dict[str, float]:
    keys = sorted({k for d in ds for k in d})
    return {k: float(np.mean([d.get(k, 0.0) for d in ds])) for k in keys}


def figure(agg: dict[str, Any], path_stem: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = {"P0": "P0 nominal", "P3": "P3 race to idle", "P4": "P4 pace to deadline",
              "P4G": "P4 + idle gating", "P6ph": "P6 (placeholder bound)", "P6": "P6 (conformal surrogate)"}
    colors = {"P0": "#6b7280", "P3": "#d97706", "P4": "#2563eb", "P4G": "#7c3aed", "P6ph": "#059669",
              "P6": "#0d9488"}
    scens = list(agg)
    fig, ax = plt.subplots(figsize=(10, 5.4))
    w = 0.13
    for i, pk in enumerate(POLICIES):
        xs, ys, err = [], [], []
        for j, sc in enumerate(scens):
            c = agg[sc]["policies"].get(pk, {}).get("energy_per_robot_hour_wh")
            if not c or c["mean"] is None:
                continue
            xs.append(j + (i - 2.5) * w)
            ys.append(c["mean"])
            err.append((c["hi"] - c["mean"]) if c["hi"] is not None else 0.0)
        bars = ax.bar(xs, ys, w, yerr=err, capsize=3, label=labels[pk], color=colors[pk])
        k = 0
        for j, sc in enumerate(scens):
            c = agg[sc]["policies"].get(pk, {})
            if not c.get("energy_per_robot_hour_wh") or c["energy_per_robot_hour_wh"]["mean"] is None:
                continue
            if not c.get("zero_safety_misses", True):
                bars[k].set_hatch("//")
                bars[k].set_edgecolor("#dc2626")
                ax.annotate("safety\nmisses", (xs[k], ys[k]), ha="center", va="bottom", fontsize=7, color="#dc2626",
                            xytext=(0, 6), textcoords="offset points")
            k += 1
    for j, sc in enumerate(scens):
        b = agg[sc]["policies"].get("P4", {}).get("p5_bound_wh")
        if b and b["mean"] is not None:
            ax.hlines(b["mean"], j - 3.1 * w, j + 3.1 * w, colors="black", linestyles="dashed",
                      label="P5 lower bound (on P4 traces)" if j == 0 else None)
    ax.set_xticks(range(len(scens)))
    ax.set_xticklabels(scens)
    ax.set_ylabel("Energy per robot-hour (Wh)")
    ax.set_title("s06: power policies on VX-1 (fictional SoC, assumed power parameters), mean and 95% CI")
    ax.legend(fontsize=8, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.08))
    ax.set_ylim(0, max(ax.get_ylim()[1], 5.2))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    files = []
    for ext in ("svg", "png"):
        f = path_stem.with_suffix("." + ext)
        fig.savefig(f, dpi=150)
        files.append(str(f.relative_to(ROOT)))
    plt.close(fig)
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    ap.add_argument("--policies", nargs="+", default=list(POLICIES))
    ap.add_argument("--workers", type=int, default=9)
    ap.add_argument("--quick", action="store_true", help="2 s runs, for a smoke test (not for results)")
    ap.add_argument("--replot", action="store_true", help="only redraw the figure from results/s06.json")
    ap.add_argument("--replace-policies", action="store_true",
                    help="rerun only --policies and replace their rows in the existing results/s06.json")
    a = ap.parse_args()
    if a.replot:
        figure(json.loads((RESULTS / "s06.json").read_text())["aggregate"], FIG / "s06_energy_per_robot_hour")
        return
    jobs = [(sc, pk, s, 2.0 if a.quick else SCENARIOS[sc]) for sc in a.scenarios for s in a.seeds for pk in a.policies]
    t0 = time.time()
    rows: list[dict[str, Any]] = []
    with Pool(a.workers) as pool:
        for r in pool.imap_unordered(_job, jobs):
            rows.append(r)
            print(f"[{len(rows)}/{len(jobs)}] {r['scenario']} {r['policy']} seed {r['seed']}: "
                  f"{r['energy_per_robot_hour_wh']:.3f} Wh/h, P5 {r['p5_bound_on_this_trace']['energy_per_robot_hour_wh']:.3f}, "
                  f"misses {r['misses_total']} (safety {r['safety_misses']}), wall {r['wall_s']:.0f}s", flush=True)
    replaced_note = None
    if a.replace_policies and (RESULTS / "s06.json").exists():
        old = json.loads((RESULTS / "s06.json").read_text())
        kept = [r for r in old["runs"] if r["policy"] not in a.policies]
        replaced_note = {"replaced_policies": a.policies, "date": dt.date.today().isoformat(),
                         "kept_rows": len(kept), "new_rows": len(rows),
                         "previous_wall_s": old.get("provenance", {}).get("wall_s")}
        rows = kept + rows
    rows.sort(key=lambda r: (r["scenario"], r["policy"], r["seed"]))
    agg = aggregate(rows)
    FIG.mkdir(parents=True, exist_ok=True)
    figs = figure(agg, FIG / "s06_energy_per_robot_hour")
    prov0 = rows[0]["provenance"]
    out = {
        "study": "s06_power_policies",
        "question": "Energy per robot-hour of P0, P3, P4, P5 (lower bound), P6 at zero safety-critical misses",
        "policy_params_note": "P4/P6 margins (deadline_margin_frac 0.08, slack feedback, anchored time model) were set "
                              "after 60 s S1 pilot runs (seeds 1-3) showed ~0.2% T4 misses at 0.05; see power/README.md.",
        "labels": [
            "VX-1 is a fictional SoC; every power parameter is an assumption (power/params_vx1.yaml).",
            "P6ph uses a PLACEHOLDER conservative finish-time bound; P6 uses the conformal surrogate "
            "(surrogate/p6.py, a calibrated heuristic with marginal coverage, not a per-job guarantee).",
            "P4G (pace plus idle gating) is an extra variant, not one of the spec's P0-P6.",
            "P5 is a lower bound under this energy model, computed per run on that run's job trace; leakage at ambient.",
            "Robot behavior comes from a scripted expert in hybrid mode; VLA compute is modeled from its operator graph.",
        ],
        "quick": a.quick,
        "aggregate": agg,
        "runs": rows,
        "figures": figs,
        "provenance": {"script": "studies/s06_power_policies.py", "git_sha": prov0.get("git_sha"),
                       "core_build": prov0.get("core_build"), "date": dt.date.today().isoformat(),
                       "configs": ["configs/soc/vx1.yaml", "configs/fidelity/interactive.yaml",
                                   "power/params_vx1.yaml"] + [f"configs/policies/{p}" for p in
                                                               ("P0_nominal.yaml", "P3_race_to_idle.yaml",
                                                                "P4_pace_to_deadline.yaml", "P6_surrogate_guided.yaml")]
                       + [prov0.get("scenario", "")],
                       "scenarios": a.scenarios, "seeds": a.seeds, "thermal_init": "settle",
                       "wall_s": time.time() - t0, "rerun": replaced_note},
    }
    name = "s06_quick.json" if a.quick else "s06.json"
    (RESULTS / name).write_text(json.dumps(out, indent=1, default=lambda o: float(o) if isinstance(o, np.floating) else str(o)))
    print("wrote", RESULTS / name)


if __name__ == "__main__":
    main()
