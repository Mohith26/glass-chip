"""s14: robot latency tolerance (spec Sections 9.3 and 11.4).

Question: how much camera-to-grasp latency can the pick task tolerate at each conveyor speed?

Method:
1. Zero-latency baseline: every chip job completes at release (ZeroLatencyChip).
2. Sweep an injected end-to-end latency (20 to 400 ms) with FixedLatencyChip, split over the
   camera-to-grasp chain (T1 10 %, detection 35 %, T4 35 %, T5 20 %), at conveyor speeds
   0.1 to 0.4 m/s. T6, T7 and T8 keep small latencies inside their deadlines, so this isolates
   perception-to-plan latency from control and safety timing.
3. Objects arrive one every 4 s at every speed (spacing = speed x 4 s), so throughput limits
   do not mask grasp quality. Detection uses the overhead camera.
4. A sensitivity sweep varies the per-object slip disturbance at 0.4 m/s, because the
   disturbance model (an assumption) sets how quickly stale observations go wrong.

Outputs: results/s14.json and results/figures/s14_latency_tolerance.{svg,png}.

Run:  .venv/bin/python studies/s14_latency_tolerance.py [--quick] [--workers N]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import subprocess
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from cosim.bridge import CoSim, CoSimConfig  # noqa: E402
from cosim.conveyor import ConveyorConfig  # noqa: E402
from cosim.fake_chip import E2E_SPLIT, FixedLatencyChip, ZeroLatencyChip  # noqa: E402
from cosim.scenario import make_scenario  # noqa: E402

SPEEDS = [0.1, 0.2, 0.3, 0.4]
LATENCIES_MS = [20, 50, 100, 150, 200, 250, 300, 400]
SEEDS = [11, 12, 13, 14, 15, 16]
DURATION_S = 60.0
ARRIVAL_PERIOD_S = 4.0
SENS_SLIP = [0.05, 0.15, 0.25]
SENS_LAT = [0, 100, 200, 300, 400]
SENS_SPEED = 0.4
SENS_SEEDS = [21, 22, 23]
TOLERANCE_FRAC = 0.9   # tolerance = largest latency whose success stays >= 90 % of the baseline


def run_one(job: dict) -> dict:
    """One co-sim run; returns raw counts and a few diagnostics."""
    v, lat, seed, slip = job["speed"], job["latency_ms"], job["seed"], job.get("slip_sigma", 0.15)
    sc = make_scenario(duration_s=job["duration_s"], seed=seed, conveyor_speed_mps=v,
                       object_spacing_m=v * ARRIVAL_PERIOD_S)
    conv = ConveyorConfig(slip_sigma=slip)
    chip = ZeroLatencyChip() if lat == 0 else FixedLatencyChip.with_injected_e2e(lat, seed=seed)
    sim = CoSim(CoSimConfig(scenario=sc, conveyor=conv, record=False), chip=chip)
    m = sim.run()
    return {
        **job,
        "attempts": m["grasp_attempts"], "grasp_success": m["grasp_success"], "placed": m["placed"],
        "offered": m["objects_offered"], "missed": m["objects_missed"],
        "missed_per_min": m["objects_missed_per_min"],
        "oscillation_episodes": m["oscillation_episodes"],
        "align_err_p50_mm": None if m["grasp_align_err_m"]["p50"] is None else 1e3 * m["grasp_align_err_m"]["p50"],
        "align_err_p95_mm": None if m["grasp_align_err_m"]["p95"] is None else 1e3 * m["grasp_align_err_m"]["p95"],
        "chain_camera_to_grasp_p50_ms": m["chain_latency_ms"]["camera_to_grasp"]["p50"],
        "placement_error_m": m["mean_placement_error_m"],
        "wall_per_sim_s": m["wall_per_sim_s"],
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def spearman(x: list[float], y: list[float]) -> float:
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def aggregate(rows: list[dict]) -> dict:
    att = sum(r["attempts"] for r in rows)
    gs = sum(r["grasp_success"] for r in rows)
    pl = sum(r["placed"] for r in rows)
    off = sum(r["offered"] for r in rows)
    lo, hi = wilson(gs, att)
    plo, phi = wilson(pl, off)
    al = [r["align_err_p50_mm"] for r in rows if r["align_err_p50_mm"] is not None]
    al95 = [r["align_err_p95_mm"] for r in rows if r["align_err_p95_mm"] is not None]
    ch = [r["chain_camera_to_grasp_p50_ms"] for r in rows if r["chain_camera_to_grasp_p50_ms"] is not None]
    return {
        "runs": len(rows), "attempts": att, "grasp_success": gs, "placed": pl, "offered": off,
        "grasp_success_rate": gs / att if att else None, "grasp_success_ci95": [lo, hi],
        "pick_rate": pl / off if off else None, "pick_rate_ci95": [plo, phi],
        "missed_per_min": float(np.mean([r["missed_per_min"] for r in rows])),
        "oscillation_episodes": int(sum(r["oscillation_episodes"] for r in rows)),
        "align_err_p50_mm": float(np.mean(al)) if al else None,
        "align_err_p95_mm": float(np.mean(al95)) if al95 else None,
        "measured_camera_to_grasp_p50_ms": float(np.mean(ch)) if ch else None,
        "per_seed_grasp_success_rate": [r["grasp_success"] / r["attempts"] if r["attempts"] else None for r in rows],
    }


def monotonic_check(lat: list[int], rate: list[float], ci: list[list[float]]) -> dict:
    """Non-increasing within noise: each step may rise by at most the larger CI half-width."""
    viol = []
    for i in range(1, len(rate)):
        tol = max((ci[i][1] - ci[i][0]) / 2, (ci[i - 1][1] - ci[i - 1][0]) / 2)
        if rate[i] > rate[i - 1] + tol:
            viol.append({"from_ms": lat[i - 1], "to_ms": lat[i], "rise": rate[i] - rate[i - 1], "tol": tol})
    return {"spearman_rho": spearman(lat, rate), "violations": viol,
            "monotonic_within_noise": not viol, "end_below_start": rate[-1] < rate[0]}


def tolerance_ms(base: float, lat: list[int], rate: list[float]) -> float | None:
    """Largest swept latency before success first falls under TOLERANCE_FRAC x baseline."""
    ok = None
    for L, r in zip(lat, rate):
        if r >= TOLERANCE_FRAC * base:
            ok = L
        else:
            break
    return ok


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except Exception:
        return None


def make_figure(res: dict, out_base: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25,
                         "axes.titleweight": "bold", "axes.titlesize": 11})
    colors = {0.1: "#2E86AB", 0.2: "#3BB273", 0.3: "#F0A202", 0.4: "#D1495B"}
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.2), constrained_layout=True)
    for v in SPEEDS:
        key = f"{v:.1f}"
        sw = res["sweep"][key]
        lat = [0] + [p["latency_ms"] for p in sw["points"]]
        base = res["baseline"][key]
        pts = [base] + sw["points"]
        g = [p["grasp_success_rate"] for p in pts]
        glo = [p["grasp_success_ci95"][0] for p in pts]
        ghi = [p["grasp_success_ci95"][1] for p in pts]
        pr = [p["pick_rate"] for p in pts]
        al = [p["align_err_p50_mm"] for p in pts]
        c = colors[v]
        axes[0].plot(lat, g, "-o", color=c, ms=3.5, lw=1.8, label=f"{v:.1f} m/s")
        axes[0].fill_between(lat, glo, ghi, color=c, alpha=0.12, lw=0)
        axes[1].plot(lat, pr, "-o", color=c, ms=3.5, lw=1.8, label=f"{v:.1f} m/s")
        axes[2].plot(lat, al, "-o", color=c, ms=3.5, lw=1.8, label=f"{v:.1f} m/s")
    axes[0].set_title("Grasp success per attempt")
    axes[0].set_ylabel("success rate")
    axes[0].set_ylim(0, 1.04)
    axes[1].set_title("Objects placed in the bin per object offered")
    axes[1].set_ylabel("pick rate")
    axes[1].set_ylim(0, 1.04)
    axes[2].set_title("Gripper-to-object offset at close (median)")
    axes[2].set_ylabel("offset (mm)")
    for ax in axes:
        ax.set_xlabel("injected camera-to-grasp latency (ms)")
        ax.set_xlim(-10, 410)
    axes[0].legend(title="conveyor speed", frameon=False, loc="lower left")
    fig.text(0.005, -0.04, "Zero-latency baseline plotted at 0 ms. Shaded: 95 % Wilson interval. "
             "Scripted expert in hybrid mode; disturbance model is an assumption (see results/s14.json).",
             fontsize=8, color="#333333")
    for ext in ("svg", "png"):
        fig.savefig(out_base.with_suffix(f".{ext}"), dpi=160, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="2 seeds x 30 s, for smoke testing")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--out", default=str(ROOT / "results" / "s14.json"))
    ap.add_argument("--replot", action="store_true", help="redraw the figure from an existing results JSON")
    a = ap.parse_args()
    if a.replot:
        make_figure(json.loads(Path(a.out).read_text()), ROOT / "results" / "figures" / "s14_latency_tolerance")
        return
    seeds = SEEDS[:2] if a.quick else SEEDS
    dur = 30.0 if a.quick else DURATION_S
    jobs = [{"kind": "sweep", "speed": v, "latency_ms": L, "seed": s, "duration_s": dur}
            for v in SPEEDS for L in [0, *LATENCIES_MS] for s in seeds]
    sens_seeds = SENS_SEEDS[:1] if a.quick else SENS_SEEDS
    jobs += [{"kind": "sens", "speed": SENS_SPEED, "latency_ms": L, "seed": s, "duration_s": dur, "slip_sigma": sg}
             for sg in SENS_SLIP for L in SENS_LAT for s in sens_seeds]
    t0 = time.perf_counter()
    with Pool(a.workers) as pool:
        rows = pool.map(run_one, jobs, chunksize=1)
    wall = time.perf_counter() - t0

    res: dict = {"baseline": {}, "sweep": {}, "sensitivity": {}, "checks": {}}
    for v in SPEEDS:
        key = f"{v:.1f}"
        base_rows = [r for r in rows if r["kind"] == "sweep" and r["speed"] == v and r["latency_ms"] == 0]
        res["baseline"][key] = {"latency_ms": 0, **aggregate(base_rows)}
        pts = []
        for L in LATENCIES_MS:
            rr = [r for r in rows if r["kind"] == "sweep" and r["speed"] == v and r["latency_ms"] == L]
            pts.append({"latency_ms": L, **aggregate(rr)})
        res["sweep"][key] = {"points": pts}
        lat = [0, *LATENCIES_MS]
        allp = [res["baseline"][key], *pts]
        g = [p["grasp_success_rate"] for p in allp]
        ci = [p["grasp_success_ci95"] for p in allp]
        pr = [p["pick_rate"] for p in allp]
        pci = [p["pick_rate_ci95"] for p in allp]
        res["checks"][key] = {
            "grasp_success": monotonic_check(lat, g, ci),
            "pick_rate": monotonic_check(lat, pr, pci),
            "grasp_tolerance_ms": tolerance_ms(g[0], LATENCIES_MS, g[1:]),
            "pick_tolerance_ms": tolerance_ms(pr[0], LATENCIES_MS, pr[1:]),
        }
    for sg in SENS_SLIP:
        pts = []
        for L in SENS_LAT:
            rr = [r for r in rows if r["kind"] == "sens" and r["slip_sigma"] == sg and r["latency_ms"] == L]
            pts.append({"latency_ms": L, **aggregate(rr)})
        res["sensitivity"][f"slip_sigma_{sg:.2f}"] = {"speed_mps": SENS_SPEED, "points": pts}

    per_sim = [r["wall_per_sim_s"] for r in rows]
    conv = ConveyorConfig()
    res["summary"] = {
        "baseline_grasp_success": {k: v["grasp_success_rate"] for k, v in res["baseline"].items()},
        "baseline_pick_rate": {k: v["pick_rate"] for k, v in res["baseline"].items()},
        "grasp_success_at_400ms": {k: v["points"][-1]["grasp_success_rate"] for k, v in res["sweep"].items()},
        "pick_rate_at_400ms": {k: v["points"][-1]["pick_rate"] for k, v in res["sweep"].items()},
        "grasp_tolerance_ms": {k: c["grasp_tolerance_ms"] for k, c in res["checks"].items()},
        "pick_tolerance_ms": {k: c["pick_tolerance_ms"] for k, c in res["checks"].items()},
        "all_grasp_curves_monotonic_within_noise": all(c["grasp_success"]["monotonic_within_noise"]
                                                       for c in res["checks"].values()),
        "wall_per_sim_s_mean": float(np.mean(per_sim)),
        "total_runs": len(rows), "study_wall_s": wall, "workers": a.workers,
    }
    res["definitions"] = {
        "grasp_success_rate": "grasp attempts (gripper close commands on a target) whose target object was lifted",
        "pick_rate": "objects placed in the bin / objects that entered the conveyor",
        "injected latency": "sum of fixed T1 + detection + T4 + T5 latencies on the camera-to-grasp chain",
        "measured_camera_to_grasp_p50_ms": "median of frame capture to first actuator command using the resulting "
                                           "plan; includes sampling waits for T6 (5 ms) and T4 (125 ms)",
        "tolerance_ms": f"largest swept latency before success first drops below {TOLERANCE_FRAC} x baseline",
        "monotonic_within_noise": "no step increases by more than the larger 95 % CI half-width",
    }
    res["assumptions"] = {
        "policy": "scripted expert in hybrid mode (VLA compute is modeled separately from its operator graph)",
        "belt_ripple": {"sigma": conv.ripple_sigma, "tau_s": conv.ripple_tau_s},
        "object_slip": {"sigma": conv.slip_sigma, "tau_s": conv.slip_tau_s},
        "perception_noise_m": {"overhead": 0.003},
        "arrival_period_s": ARRIVAL_PERIOD_S,
        "latency_split": {k: v for k, v in E2E_SPLIT.items() if k not in ("T1_ingest_wrist", "T2_detect")},
        "detection_camera": "overhead",
        "controller": "computed-torque joint impedance, wn 40 rad/s, 1 kHz",
    }
    res["provenance"] = {
        "script": "studies/s14_latency_tolerance.py", "git_sha": git_sha(),
        "config_files": ["cosim/envs/conveyor_scene.xml", "cosim/envs/panda_torque.xml",
                         "workloads/graphs/*.json (release periods)"],
        "seeds": seeds, "sensitivity_seeds": sens_seeds, "duration_s": dur, "speeds_mps": SPEEDS,
        "latencies_ms": LATENCIES_MS, "chip_models": ["ZeroLatencyChip", "FixedLatencyChip.with_injected_e2e"],
        "date": _dt.date.today().isoformat(), "quick": bool(a.quick),
    }
    res["runs"] = rows
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    fig_base = ROOT / "results" / "figures" / "s14_latency_tolerance"
    fig_base.parent.mkdir(parents=True, exist_ok=True)
    make_figure(res, fig_base)
    print(json.dumps(res["summary"], indent=1))
    print("checks:", json.dumps({k: {"grasp_rho": round(c["grasp_success"]["spearman_rho"], 3),
                                     "grasp_mono": c["grasp_success"]["monotonic_within_noise"],
                                     "pick_rho": round(c["pick_rate"]["spearman_rho"], 3),
                                     "pick_mono": c["pick_rate"]["monotonic_within_noise"]}
                                 for k, c in res["checks"].items()}))


if __name__ == "__main__":
    main()
