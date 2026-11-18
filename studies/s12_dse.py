"""s12: design-space exploration with the conformal surrogate (spec Section 12).

Question: across the VX-1 design space, what is the Pareto frontier of energy per robot-hour vs
worst safety-task miss rate vs area proxy, and which three configurations would I recommend?

Method
------
1. Draw N candidate designs uniformly from the design knobs (hardware, scheduler, power policy,
   Vmin per domain, transport). The low-voltage arithmetic factor is fixed at 1x (off), so no
   recommendation depends on the hypothetical knob; I report its effect separately.
2. Evaluate every candidate with the surrogate on all four workloads (S1, S2, S3, S6) at a fixed
   recording bitrate of 1 Gbit/s and 35 C ambient. Objectives:
     energy  = mean over workloads of predicted energy per robot-hour (Wh)
     safety  = max over workloads of the conformal UPPER bound (90%) on the worst safety-task
               miss rate (T7, T8, and T12 in S6)
     area    = closed-form area proxy (mm^2, rough)
   Feasible = safety upper bound <= 0.005 on every workload, and the 90% upper bounds of the T4
   VLA and T2 detection miss rates <= 0.05 on every workload, and no interval flagged
   "uncertain, run full model" for those outputs. Why these limits: a run has about 100 T8 jobs
   and about 8 T4 jobs, so one miss is 0.01 (T8) or 0.125 (T4); an upper bound below 0.005 or
   0.05 means the surrogate is 90% confident of zero misses in the window. The split-conformal
   offset is global in log space, so the smallest upper bound it can give for a predicted zero
   is about 0.0033; a "certified 0.001" limit is not reachable with this interval method.
3. Pareto frontier (non-dominated in all three objectives, minimizing) over all candidates and
   over feasible candidates.
4. Three recommendations from the feasible set: lowest energy, smallest area, and a balanced
   knee point (minimum of normalized energy + normalized area). Each is re-run with the full
   model (chiprun) on all four workloads at the training run length (2 s) and at a 3x longer run (6 s),
   and compared with the surrogate prediction and interval. The VX-1 baseline is run too.

Writes results/s12.json and results/figures/s12_*.{png,svg}.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surrogate.conformal import load_bundle  # noqa: E402
from surrogate.dataset import (INPUTS, NOC_BITS, POWER_POLICIES, SCENARIOS, SCHEDULERS, SPACE,  # noqa: E402
                               TRANSPORTS, area_proxy, decode, provenance, to_cfg)

FIG = ROOT / "results" / "figures"
N_CAND = 60000
BITRATE = 1.0
AMBIENT = 35.0
SAFETY_LIMIT = 0.005
RT_LIMIT = 0.05
WORKLOAD_KNOBS = ("scenario", "bitrate_gbps", "ambient_c")
ALPHA = 0.1


def candidates(n: int, seed: int = 12) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        u = dict(zip(INPUTS, rng.random(len(INPUTS))))
        num = decode(u)
        num["lv_arith_factor"] = 1.0
        num["bitrate_gbps"] = BITRATE
        num["ambient_c"] = AMBIENT
        rows.append([num[k] for k in INPUTS])
    return np.array(rows)


def vx1_default_encoded(policy: str = "pace_to_deadline", scheduler: str = "edf") -> np.ndarray:
    from surrogate.chiprun import VX1_DEFAULT
    c = dict(VX1_DEFAULT)
    c.update(scheduler=scheduler, power_policy=policy, bitrate_gbps=BITRATE, ambient_c=AMBIENT, dram_gbps=136.5)
    enc = dict(c)
    enc["noc_bits"] = NOC_BITS.index(256)
    enc["scheduler"] = SCHEDULERS.index(scheduler)
    enc["power_policy"] = POWER_POLICIES.index(policy)
    enc["transport"] = TRANSPORTS.index(c["transport"])
    enc["scenario"] = 0
    return np.array([float(enc[k]) for k in INPUTS])


def evaluate(bundle: Any, C: np.ndarray, alpha: float = ALPHA) -> dict[str, np.ndarray]:
    """Surrogate objectives and feasibility over all workloads for candidate rows C."""
    i_s = INPUTS.index("scenario")
    names = ["energy_per_robot_hour_wh", "worst_safety_miss_rate", "T4_vla.miss_rate", "T2_detect.miss_rate",
             "avg_power_w", "peak_temp_c", "T4_vla.p99_ms", "chain_camera_to_plan.p99_ms"]
    names = [n for n in names if n in bundle.models]
    per: dict[str, list[dict[str, np.ndarray]]] = {n: [] for n in names}
    for s in range(len(SCENARIOS)):
        Xs = C.copy()
        Xs[:, i_s] = s
        pr = bundle.predict(Xs, names, alpha)
        for n in names:
            per[n].append(pr[n])
    st = lambda n, k: np.stack([p[k] for p in per[n]], axis=1)  # noqa: E731  (N, scenarios)
    energy = st("energy_per_robot_hour_wh", "point").mean(axis=1)
    safety_hi = st("worst_safety_miss_rate", "hi").max(axis=1)
    safety_pt = st("worst_safety_miss_rate", "point").max(axis=1)
    t4_hi = st("T4_vla.miss_rate", "hi").max(axis=1)
    t2_hi = st("T2_detect.miss_rate", "hi").max(axis=1)
    unc = (st("worst_safety_miss_rate", "uncertain") | st("T4_vla.miss_rate", "uncertain")
           | st("T2_detect.miss_rate", "uncertain")).any(axis=1)
    area = np.array([area_proxy(dict(zip(INPUTS, r))) for r in C])
    feasible = (safety_hi <= SAFETY_LIMIT) & (t4_hi <= RT_LIMIT) & (t2_hi <= RT_LIMIT) & ~unc
    return {"energy": energy, "energy_lo": st("energy_per_robot_hour_wh", "lo").mean(axis=1),
            "energy_hi": st("energy_per_robot_hour_wh", "hi").mean(axis=1), "safety_hi": safety_hi,
            "safety_pt": safety_pt, "t4_hi": t4_hi, "t2_hi": t2_hi, "uncertain": unc, "area": area,
            "feasible": feasible, "per_scenario": {n: {k: st(n, k) for k in ("point", "lo", "hi")} for n in names}}


def pareto_mask(F: np.ndarray) -> np.ndarray:
    """Non-dominated rows of F (minimize every column).

    Points are visited in lexicographic order, so a point can only be dominated by points
    already visited; each check is one vectorized comparison against the current front.
    """
    order = np.lexsort(F.T[::-1])
    keep = np.zeros(len(F), bool)
    front = np.empty((0, F.shape[1]))
    for i in order:
        f = F[i]
        if len(front) and np.any(np.all(front <= f, axis=1) & np.any(front < f, axis=1)):
            continue
        if len(front) and np.any(np.all(front == f, axis=1)):
            continue          # exact duplicate of a front point
        keep[i] = True
        front = np.vstack([front, f])
    return keep


def describe(row: np.ndarray) -> dict[str, Any]:
    num = dict(zip(INPUTS, row.tolist()))
    cfg = to_cfg(num)
    return {k: cfg[k] for k in INPUTS if k not in WORKLOAD_KNOBS}


def verify(row: np.ndarray, durations: tuple[float, ...] = (2.0, 6.0), workers: int = 4) -> dict[str, Any]:
    """Re-run the full chip model for every workload and duration."""
    import multiprocessing as mp
    jobs = []
    for d in durations:
        for s in range(len(SCENARIOS)):
            num = dict(zip(INPUTS, row.tolist()))
            num["scenario"] = float(s)
            jobs.append((num, d))
    with mp.get_context("spawn").Pool(min(workers, len(jobs))) as pool:
        res = pool.map(_verify_one, jobs)
    out: dict[str, Any] = {}
    for (num, d), r in zip(jobs, res):
        out.setdefault(f"{d:g}s", {})[SCENARIOS[int(num["scenario"])]] = r
    return out


def _verify_one(args: tuple[dict[str, float], float]) -> dict[str, Any]:
    from surrogate.chiprun import run_chip
    num, d = args
    r = run_chip(to_cfg(num), seed=101, duration_s=d, warmup_s=1.0)
    keep = ["energy_per_robot_hour_wh", "worst_safety_miss_rate", "worst_rt_miss_rate", "T4_vla.miss_rate",
            "T2_detect.miss_rate", "T4_vla.p99_ms", "T8_safety.p99_ms", "T7_control.miss_rate", "T8_safety.miss_rate",
            "chain_camera_to_plan.p99_ms", "avg_power_w", "peak_power_w", "peak_temp_c", "cpu_s"]
    return {k: (float(r[k]) if r.get(k) is not None else None) for k in keep}


def main() -> None:
    t_start = time.time()
    bundle = load_bundle()
    C = candidates(N_CAND)
    t0 = time.time()
    ev = evaluate(bundle, C)
    eval_s = time.time() - t0
    F = np.column_stack([ev["energy"], ev["safety_hi"], ev["area"]])
    pm_all = pareto_mask(F)
    feas = np.where(ev["feasible"])[0]
    pm_feas = np.zeros(len(C), bool)
    if len(feas):
        pm_feas[feas[pareto_mask(F[feas])]] = True

    # Recommendations.
    rec_idx: list[tuple[str, int, str]] = []
    if len(feas):
        i_e = feas[np.argmin(ev["energy"][feas])]
        rec_idx.append(("lowest_energy", int(i_e),
                        "Lowest predicted energy per robot-hour (mean over S1, S2, S3, S6) among designs whose 90% "
                        "upper bounds keep every safety task under 0.5% misses and T4/T2 under 5% misses (zero expected misses "
                        "in the window) on every workload."))
        i_a = feas[np.argmin(ev["area"][feas] + 1e-6 * ev["energy"][feas])]
        rec_idx.append(("smallest_area", int(i_a),
                        "Smallest area proxy whose 90% upper bounds pass the same deadline limits."))
        e, a = ev["energy"][feas], ev["area"][feas]
        en = (e - e.min()) / (e.max() - e.min() + 1e-12)
        an = (a - a.min()) / (a.max() - a.min() + 1e-12)
        i_k = feas[np.argmin(en + an)]
        if i_k in (i_e, i_a):
            order = feas[np.argsort(en + an)]
            i_k = next(int(i) for i in order if i not in (i_e, i_a))
        rec_idx.append(("balanced", int(i_k),
                        "Knee of the feasible frontier: minimum of normalized energy plus normalized area."))

    base_row = vx1_default_encoded()
    base_row0 = vx1_default_encoded("nominal", "fifo")
    base_ev = evaluate(bundle, np.vstack([base_row, base_row0]))

    recs = []
    for name, i, why in rec_idx:
        row = C[i]
        pred = {SCENARIOS[s]: {n: {k: float(v[k][i, s]) for k in ("point", "lo", "hi")} for n, v in ev["per_scenario"].items()}
                for s in range(len(SCENARIOS))}
        t0 = time.time()
        ver = verify(row)
        # interval check at the training run length
        checks = []
        for s in SCENARIOS:
            for n in ("energy_per_robot_hour_wh", "worst_safety_miss_rate", "T4_vla.miss_rate", "T4_vla.p99_ms"):
                if n in pred[s] and ver["2s"][s].get(n) is not None:
                    y = ver["2s"][s][n]
                    checks.append({"workload": s, "output": n, "full_model": y, **pred[s][n],
                                   "inside_90": bool(pred[s][n]["lo"] - 1e-9 <= y <= pred[s][n]["hi"] + 1e-9)})
        e_full = float(np.mean([ver["2s"][s]["energy_per_robot_hour_wh"] for s in SCENARIOS]))
        e_full_long = float(np.mean([ver["6s"][s]["energy_per_robot_hour_wh"] for s in SCENARIOS]))
        safety_full = max(ver["2s"][s]["worst_safety_miss_rate"] or 0 for s in SCENARIOS)
        safety_full_long = max(ver["6s"][s]["worst_safety_miss_rate"] or 0 for s in SCENARIOS)
        rt_full_long = max(max(ver["6s"][s]["T4_vla.miss_rate"] or 0, ver["6s"][s]["T2_detect.miss_rate"] or 0)
                           for s in SCENARIOS)
        recs.append({
            "name": name, "why": why, "design": describe(row), "inputs_encoded": dict(zip(INPUTS, row.tolist())),
            "predicted": {"energy_wh_mean": float(ev["energy"][i]), "energy_wh_interval_mean": [float(ev["energy_lo"][i]), float(ev["energy_hi"][i])],
                          "worst_safety_miss_upper_90": float(ev["safety_hi"][i]), "t4_miss_upper_90": float(ev["t4_hi"][i]),
                          "t2_miss_upper_90": float(ev["t2_hi"][i]), "area_mm2": float(ev["area"][i]), "per_workload": pred},
            "verified": {"energy_wh_mean_2s": e_full, "energy_wh_mean_6s": e_full_long,
                         "worst_safety_miss_2s": safety_full, "worst_safety_miss_6s": safety_full_long,
                         "worst_t4_t2_miss_6s": rt_full_long, "runs": ver, "interval_checks": checks,
                         "frac_inside_90": float(np.mean([c["inside_90"] for c in checks])) if checks else None,
                         "wall_s": time.time() - t0},
            "on_pareto_all": bool(pm_all[i]), "on_pareto_feasible": bool(pm_feas[i]),
        })
    base_ver = verify(base_row)
    baseline = {"design": describe(base_row), "inputs_encoded": dict(zip(INPUTS, base_row.tolist())),
                "predicted_energy_wh_mean": float(base_ev["energy"][0]), "predicted_safety_upper": float(base_ev["safety_hi"][0]),
                "area_mm2": float(base_ev["area"][0]), "feasible_by_surrogate": bool(base_ev["feasible"][0]),
                "verified_energy_wh_mean_2s": float(np.mean([base_ver["2s"][s]["energy_per_robot_hour_wh"] for s in SCENARIOS])),
                "verified_energy_wh_mean_6s": float(np.mean([base_ver["6s"][s]["energy_per_robot_hour_wh"] for s in SCENARIOS])),
                "verified_worst_safety_miss_6s": max(base_ver["6s"][s]["worst_safety_miss_rate"] or 0 for s in SCENARIOS),
                "runs": base_ver,
                "p0_fifo_predicted_energy_wh_mean": float(base_ev["energy"][1])}

    # Hypothetical low-voltage arithmetic: same lowest-energy design with the factor at 2x and 4x.
    hypo = []
    if recs:
        row = np.array([recs[0]["inputs_encoded"][k] for k in INPUTS])
        for f in (2.0, 4.0):
            r2 = row.copy()
            r2[INPUTS.index("lv_arith_factor")] = f
            e2 = evaluate(bundle, r2[None, :])
            hypo.append({"lv_arith_factor": f, "predicted_energy_wh_mean": float(e2["energy"][0]),
                         "label": "hypothetical (represents the class of techniques Velaura claims for Titan Core; not their technology)"})

    # Pareto points for the viz (energy vs worst safety miss upper bound, feasible + all frontier).
    front = np.where(pm_all)[0]
    pareto_for_viz = {"x": "energy_per_robot_hour_wh", "y": "worst_safety_miss_rate",
                      "x_label": "Energy per robot-hour (Wh, mean of S1/S2/S3/S6)",
                      "y_label": "Worst safety-task miss rate, 90% upper bound (max over workloads)",
                      "points": [[round(float(ev["energy"][i]), 4), round(float(ev["safety_hi"][i]), 6),
                                  round(float(ev["area"][i]), 2)] for i in front[np.argsort(ev["energy"][front])]],
                      "point_fields": ["energy_wh", "safety_miss_upper", "area_mm2"]}

    figs = make_figures(ev, pm_all, pm_feas, recs, baseline)
    prov = provenance()
    prov.update({"script": "studies/s12_dse.py", "date": str(date.today()), "runtime_s": time.time() - t_start})
    out = {
        "provenance": prov,
        "setup": {"n_candidates": N_CAND, "workloads": SCENARIOS, "bitrate_gbps": BITRATE, "ambient_c": AMBIENT,
                  "alpha": ALPHA, "safety_limit": SAFETY_LIMIT, "rt_limit": RT_LIMIT, "lv_arith_factor": 1.0,
                  "surrogate_eval_s": eval_s, "surrogate_eval_ms_per_design_workload": eval_s / (N_CAND * len(SCENARIOS)) * 1000,
                  "objectives": ["energy (mean over workloads, point)", "worst safety miss rate (max over workloads, 90% upper bound)",
                                 "area proxy (closed form, rough)"]},
        "counts": {"feasible": int(ev["feasible"].sum()), "pareto_all": int(pm_all.sum()), "pareto_feasible": int(pm_feas.sum()),
                   "flagged_uncertain": int(ev["uncertain"].sum())},
        "feasible_by_policy": {p: int(ev["feasible"][C[:, INPUTS.index("power_policy")] == i].sum())
                               for i, p in enumerate(POWER_POLICIES)},
        "recommended": recs, "baseline_vx1_p4_edf": baseline, "hypothetical_low_voltage_arith": hypo,
        "pareto_for_viz": pareto_for_viz, "figures": figs,
        "caveats": ["VX-1 is fictional; the area proxy is rough.",
                    "Feasibility uses the surrogate's 90% marginal upper bounds; the guarantee is marginal, so each "
                    "recommendation is re-run with the full model.",
                    "Runs are 2.0 simulated seconds with a 1.0 s burn-in (verification also at 6 s); rare events longer than that are not sampled."],
    }
    (ROOT / "results" / "s12.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({k: out[k] for k in ("counts", "feasible_by_policy")}, indent=1))
    for r in recs:
        print(r["name"], json.dumps(r["design"]), json.dumps(r["predicted"]["energy_wh_mean"]),
              r["verified"]["energy_wh_mean_2s"], r["verified"]["worst_safety_miss_6s"], r["verified"]["frac_inside_90"])


def make_figures(ev: dict[str, Any], pm_all: np.ndarray, pm_feas: np.ndarray, recs: list[dict[str, Any]],
                 base: dict[str, Any]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIG.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9})
    files = []
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.6))
    ax = axs[0]
    sh = np.maximum(ev["safety_hi"], 1e-4)
    ax.scatter(ev["energy"], sh, s=1, color="#cbd5e1", label="candidates", rasterized=True)
    sc = ax.scatter(ev["energy"][pm_all], np.maximum(ev["safety_hi"][pm_all], 1e-4), s=10, c=ev["area"][pm_all],
                    cmap="viridis", label="Pareto (energy, safety, area)")
    plt.colorbar(sc, ax=ax, label="area proxy (mm2, rough)")
    for r, mk in zip(recs, ("*", "D", "P")):
        ax.scatter(r["predicted"]["energy_wh_mean"], max(r["predicted"]["worst_safety_miss_upper_90"], 1e-4), marker=mk,
                   s=120, color="#dc2626", edgecolor="k", zorder=5, label=r["name"])
    ax.scatter(base["predicted_energy_wh_mean"], max(base["predicted_safety_upper"], 1e-4), marker="X", s=100,
               color="#f59e0b", edgecolor="k", zorder=5, label="VX-1 default (P4, EDF)")
    ax.axhline(SAFETY_LIMIT, color="#dc2626", lw=0.8, ls="--")
    ax.set_yscale("log")
    ax.set_xlabel("energy per robot-hour, mean of S1/S2/S3/S6 (Wh)")
    ax.set_ylabel("worst safety miss rate, 90% upper bound (floored at 1e-4)")
    ax.legend(fontsize=7, loc="upper right")
    ax.set_title("s12: surrogate design-space exploration")
    ax = axs[1]
    f = ev["feasible"]
    ax.scatter(ev["area"][f], ev["energy"][f], s=2, color="#93c5fd", label="feasible", rasterized=True)
    ax.scatter(ev["area"][pm_feas], ev["energy"][pm_feas], s=10, color="#1d4ed8", label="feasible Pareto")
    for r, mk in zip(recs, ("*", "D", "P")):
        ax.scatter(r["predicted"]["area_mm2"], r["predicted"]["energy_wh_mean"], marker=mk, s=120, color="#dc2626",
                   edgecolor="k", zorder=5, label=f"{r['name']} (verified {r['verified']['energy_wh_mean_2s']:.2f} Wh)")
    ax.set_xlabel("area proxy (mm2, rough)")
    ax.set_ylabel("energy per robot-hour (Wh)")
    ax.legend(fontsize=7)
    ax.set_title("feasible designs: 90% upper bounds inside the safety and T4/T2 limits")
    fig.tight_layout()
    for ext in ("png", "svg"):
        p = FIG / f"s12_pareto.{ext}"
        fig.savefig(p, dpi=160)
        files.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    return files


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    main()
