"""s07: voltage cliff. Robot success, deadline misses and oscillation versus Vmin per domain.

Setup: the S8 task set (the full S1 stack) with the scenario's time-stepped set_vmin events
replaced by one fixed operating point per run, so each point is a separate closed-loop run:

* Static undervolt (P0 with set_vmin = pin): the domain group runs at exactly V (frequency from the
  alpha-power law). This is where the cliff shows up.
* P4 with set_vmin = floor: P4 may use any OPP at or above V. I report its misses, its energy, and
  the fraction of time it spends below the static cliff voltage, which is how it "moves" the cliff:
  it runs below V* when slack allows and climbs back when demand needs it.
* Hypothetical series (clearly labeled): the low-voltage arithmetic toggle extends the NPU MAC
  array down to 0.40 V with a MAC energy reduction factor of 2x and 3x. That factor range comes from
  Velaura's own Titan Core claim (2-4x), not a measurement; this model does not implement their
  technology.

Domain groups: npu; cpu (cpu_a and cpu_b together); rt. Points below a domain's configured OPP
table are extrapolated from the alpha-power law and labeled.

    .venv/bin/python studies/s07_voltage_cliff.py [--seeds 1 2] [--duration 20] [--workers 9]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
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
SCENARIO = "S8"

GROUPS: dict[str, dict[str, Any]] = {
    "npu": {"domains": ["npu"], "volts": [0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45], "extra": [0.40]},
    "cpu": {"domains": ["cpu_a", "cpu_b"], "volts": [0.75, 0.70, 0.65, 0.60, 0.55, 0.50], "extra": [0.45, 0.40]},
    "rt": {"domains": ["rt"], "volts": [0.75, 0.70, 0.65, 0.60, 0.55], "extra": [0.45, 0.40, 0.35, 0.32]},
}
LV_FACTORS = [2.0, 3.0]


def _no_vmin_events() -> list[dict[str, Any]]:
    return []


def _job(args: tuple[str, str, float, int, float, float | None]) -> dict[str, Any]:
    group, policy, volts, seed, dur, lv = args
    from studies.runner import run

    doms = GROUPS[group]["domains"]
    ov: dict[str, Any] = {"power": {"thermal_init": "settle", "vmin": {d: volts for d in doms},
                                    "record_series": True},
                          "scenario": {"events": _no_vmin_events()}}
    if lv is not None:
        ov["power"]["low_voltage_arith"] = {"enabled": True, "reduction_x": lv, "mac_vmin_v": 0.40}
    t0 = time.time()
    r = run(SCENARIO, policy, seed=seed, duration_s=dur, return_objects=True, overrides=ov)
    pm = r["_objects"]["pm"]
    s = pm.summary()
    ser = pm.series_arrays()
    v_series = {d: ser["volts"][d] for d in doms}
    rob = r["robot"]
    tasks = {t: {k: v.get(k) for k in ("completed", "missed", "skipped", "criticality")}
             | {"p99_ms": (v.get("response_ms") or {}).get("p99")} for t, v in r["tasks"].items()}
    jobs = sum(v["completed"] or 0 for v in tasks.values())
    misses = r["misses"]["total"]
    skips = sum(v["skipped"] or 0 for v in tasks.values())
    dvf = pm.vf[doms[0]]
    return {
        "group": group, "policy": policy, "vmin": volts, "seed": seed, "lv_reduction_x": lv,
        "hypothetical": bool(s["hypothetical"]), "labels": s["labels"],
        "extrapolated": any(o.extrapolated and o.volts <= volts + 1e-9 for o in dvf.opps),
        "freq_hz_at_vmin": float(dvf.opps[dvf.index_at_or_below_voltage(volts)].freq_hz),
        "energy_per_robot_hour_wh": s["energy_per_robot_hour_wh"],
        "domain_energy_j": {d: s["energy_j_by_domain"].get(d) for d in doms}, "accounted_s": s["duration_s"],
        "mean_volts": {d: float(np.mean(v)) for d, v in v_series.items()},
        "volts_samples": {d: v[::10].round(3).tolist() for d, v in v_series.items()},
        "misses_total": misses, "safety_misses": r["misses"]["safety"], "skipped_total": int(skips),
        "jobs": int(jobs), "miss_rate": (misses + skips) / max(jobs + skips, 1),
        "tasks": tasks,
        "grasp_attempts": rob.get("grasp_attempts"), "grasp_success": rob.get("grasp_success"),
        "grasp_success_rate": rob.get("grasp_success_rate"), "picks_per_min": rob.get("picks_per_min"),
        "oscillation_episodes": rob.get("oscillation_episodes"), "oscillation_time_s": rob.get("oscillation_time_s"),
        "joint_tracking_rms_rad": rob.get("joint_tracking_rms_rad"),
        "provenance": r["provenance"], "wall_s": time.time() - t0,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for g in GROUPS:
        gr = [r for r in rows if r["group"] == g]
        series: dict[str, list[dict[str, Any]]] = {}
        for key in sorted({(r["policy"], r["lv_reduction_x"]) for r in gr}, key=str):
            pol, lv = key
            name = pol if lv is None else f"{pol}+LV{lv:g}x (hypothetical)"
            pts = []
            for v in sorted({r["vmin"] for r in gr if (r["policy"], r["lv_reduction_x"]) == key}, reverse=True):
                rr = [r for r in gr if (r["policy"], r["lv_reduction_x"]) == key and r["vmin"] == v]
                att = sum(r["grasp_attempts"] or 0 for r in rr)
                suc = sum(r["grasp_success"] or 0 for r in rr)
                jobs = sum(r["jobs"] + r["skipped_total"] for r in rr)
                pts.append({
                    "vmin": v, "n_runs": len(rr), "extrapolated": any(r["extrapolated"] for r in rr),
                    "hypothetical": any(r["hypothetical"] for r in rr),
                    "freq_hz": rr[0]["freq_hz_at_vmin"],
                    "grasp_success_rate": suc / att if att else None, "grasp_attempts": att,
                    "picks_per_min": float(np.mean([r["picks_per_min"] or 0 for r in rr])),
                    "miss_rate": sum(r["misses_total"] + r["skipped_total"] for r in rr) / max(jobs, 1),
                    "misses_total": int(sum(r["misses_total"] for r in rr)),
                    "skipped_total": int(sum(r["skipped_total"] for r in rr)),
                    "safety_misses": int(sum(r["safety_misses"] for r in rr)),
                    "oscillation_episodes": int(sum(r["oscillation_episodes"] or 0 for r in rr)),
                    "energy_per_robot_hour_wh": float(np.mean([r["energy_per_robot_hour_wh"] for r in rr])),
                    "group_energy_j_per_s": float(np.mean([sum(v for v in r["domain_energy_j"].values() if v)
                                                           / r["accounted_s"] for r in rr])),
                    "mean_volts": float(np.mean([np.mean(list(r["mean_volts"].values())) for r in rr])),
                    "misses_by_task": _sum(rr, "missed"), "skipped_by_task": _sum(rr, "skipped"),
                    "task_miss_rate": _task_rates(rr),
                })
            series[name] = pts
        # cliff: highest static (P0) Vmin at which any deadline miss/skip appears
        p0 = series.get("P0", [])
        cliff = next((p["vmin"] for p in p0 if p["misses_total"] + p["skipped_total"] > 0), None)
        last_ok = None
        for p in p0:
            if p["misses_total"] + p["skipped_total"] == 0:
                last_ok = p["vmin"]
            else:
                break
        cliff1 = next((p["vmin"] for p in p0 if max(p["task_miss_rate"].values(), default=0.0) >= 0.01), None)
        info: dict[str, Any] = {"static_cliff_first_miss_v": cliff, "static_lowest_clean_v": last_ok,
                                "static_cliff_1pct_v": cliff1,
                                "static_cliff_1pct_tasks": next(({t: r for t, r in p["task_miss_rate"].items() if r >= 0.01}
                                                                 for p in p0 if p["vmin"] == cliff1), None)}
        p4 = [r for r in gr if r["policy"] == "P4" and r["lv_reduction_x"] is None]
        for key, cv in (("first_miss", cliff), ("1pct", cliff1)):
            if cv is not None and p4:
                below = []
                for r in p4:
                    if r["vmin"] > cv:
                        continue          # only floors that allow going below the cliff
                    for d, vs in r["volts_samples"].items():
                        below.append(float(np.mean(np.asarray(vs) < cv - 1e-9)))
                info[f"p4_time_fraction_below_static_cliff_{key}"] = float(np.mean(below)) if below else None
        if p4:
            info["p4_misses_total"] = int(sum(r["misses_total"] + r["skipped_total"] for r in p4))
        out[g] = {"series": series, "cliff": info}
    return out


def _task_rates(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Per-task (missed + skipped) / (completed + skipped), pooled over runs."""
    out: dict[str, float] = {}
    for t in sorted({t for r in rows for t in r["tasks"]}):
        bad = sum((r["tasks"].get(t, {}).get("missed") or 0) + (r["tasks"].get(t, {}).get("skipped") or 0) for r in rows)
        tot = sum((r["tasks"].get(t, {}).get("completed") or 0) + (r["tasks"].get(t, {}).get("skipped") or 0) for r in rows)
        if tot:
            out[t] = bad / tot
    return out


def _sum(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    acc: dict[str, int] = {}
    for r in rows:
        for t, v in r["tasks"].items():
            n = int(v.get(key) or 0)
            if n:
                acc[t] = acc.get(t, 0) + n
    return acc


def figure(agg: dict[str, Any], stem: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(13, 6.5), sharex="col")
    style = {"P0": ("#6b7280", "o", "-"), "P4": ("#2563eb", "s", "-")}
    for j, g in enumerate(GROUPS):
        for name, pts in agg[g]["series"].items():
            base = name.split("+")[0]
            col, mk, ls = style.get(base, ("#059669", "^", "--"))
            if "hypothetical" in name:
                col, ls = ("#c026d3" if "2x" in name else "#9333ea"), ":"
            v = [p["vmin"] for p in pts]
            axes[0, j].plot(v, [max(p["task_miss_rate"].values(), default=0.0) * 100 for p in pts], marker=mk, color=col,
                            ls=ls, label=name)
            axes[1, j].plot(v, [p["grasp_success_rate"] if p["grasp_success_rate"] is not None else np.nan for p in pts],
                            marker=mk, color=col, ls=ls, label=name)
        c = agg[g]["cliff"].get("static_cliff_1pct_v")
        for i in range(2):
            if c is not None:
                axes[i, j].axvline(c, color="#dc2626", alpha=0.5, ls="--")
            axes[i, j].invert_xaxis()
            axes[i, j].grid(alpha=0.3)
        axes[0, j].set_title(f"{g}: Vmin sweep (P0 = static pin, P4 = floor)")
        axes[1, j].set_xlabel("Vmin (V); points below the OPP table are extrapolated")
    axes[0, 0].set_ylabel("worst task miss + skip rate (%)")
    axes[1, 0].set_ylabel("grasp success rate")
    axes[0, 0].legend(fontsize=7)
    fig.suptitle("s07 voltage cliff (VX-1 fictional; LV series hypothetical, factor from Velaura's claim)", fontsize=10)
    fig.tight_layout()
    files = []
    for ext in ("svg", "png"):
        f = stem.with_suffix("." + ext)
        fig.savefig(f, dpi=150)
        files.append(str(f.relative_to(ROOT)))
    plt.close(fig)
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=9)
    ap.add_argument("--groups", nargs="+", default=list(GROUPS))
    ap.add_argument("--rerun-policy", default=None,
                    help="rerun only this policy's runs and replace them in the existing results/s07.json")
    a = ap.parse_args()
    jobs = []
    for g in a.groups:
        vs = GROUPS[g]["volts"] + GROUPS[g]["extra"]
        for s in a.seeds:
            for v in vs:
                jobs.append((g, "P0", v, s, a.duration, None))
            for v in [GROUPS[g]["volts"][0], GROUPS[g]["volts"][-1]] + GROUPS[g]["extra"]:
                jobs.append((g, "P4", v, s, a.duration, None))
            if g == "npu":
                for lv in LV_FACTORS:
                    for v in (0.45, 0.40):
                        jobs.append((g, "P0", v, s, a.duration, lv))
                    jobs.append((g, "P4", 0.40, s, a.duration, lv))
    kept: list[dict[str, Any]] = []
    rerun_note = None
    if a.rerun_policy and (RESULTS / "s07.json").exists():
        old = json.loads((RESULTS / "s07.json").read_text())
        kept = [r for r in old["runs"] if r["policy"] != a.rerun_policy]
        jobs = [j for j in jobs if j[1] == a.rerun_policy]
        rerun_note = {"rerun_policy": a.rerun_policy, "kept_rows": len(kept), "date": dt.date.today().isoformat()}
    t0 = time.time()
    rows = []
    with Pool(a.workers) as pool:
        for r in pool.imap_unordered(_job, jobs):
            rows.append(r)
            print(f"[{len(rows)}/{len(jobs)}] {r['group']} {r['policy']} lv={r['lv_reduction_x']} V={r['vmin']} seed {r['seed']}: "
                  f"miss {r['misses_total']} skip {r['skipped_total']} grasp {r['grasp_success']}/{r['grasp_attempts']} "
                  f"osc {r['oscillation_episodes']} {r['energy_per_robot_hour_wh']:.3f} Wh/h", flush=True)
    rows = kept + rows
    rows.sort(key=lambda r: (r["group"], r["policy"], str(r["lv_reduction_x"]), -r["vmin"], r["seed"]))
    agg = aggregate(rows)
    FIG.mkdir(parents=True, exist_ok=True)
    figs = figure(agg, FIG / "s07_voltage_cliff")
    prov0 = rows[0]["provenance"]
    out = {
        "study": "s07_voltage_cliff",
        "question": "Where is the voltage cliff per domain (success, misses, oscillation vs Vmin), and how does P4 move it?",
        "labels": [
            "VX-1 is a fictional SoC; every power parameter is an assumption (power/params_vx1.yaml).",
            "Points below a domain's configured OPP table are extrapolated from the alpha-power law.",
            "LV series are HYPOTHETICAL: the 2x/3x MAC energy factors come from Velaura's Titan Core claim (2-4x), "
            "not a measurement, and this model does not implement their technology.",
            "The model has no timing-error physics: the cliff here is deadline-driven (frequency too low), not a "
            "logic-failure cliff.",
            "Robot behavior comes from a scripted expert in hybrid mode; VLA compute is modeled from its operator graph.",
        ],
        "aggregate": agg,
        "runs": rows,
        "figures": figs,
        "provenance": {"script": "studies/s07_voltage_cliff.py", "git_sha": prov0.get("git_sha"),
                       "core_build": prov0.get("core_build"), "date": dt.date.today().isoformat(),
                       "scenario": prov0.get("scenario"), "scenario_events_replaced": "set_vmin events removed; one fixed Vmin per run",
                       "configs": ["configs/soc/vx1.yaml", "configs/fidelity/interactive.yaml", "power/params_vx1.yaml",
                                   "configs/policies/P0_nominal.yaml", "configs/policies/P4_pace_to_deadline.yaml"],
                       "seeds": a.seeds, "duration_s": a.duration, "thermal_init": "settle", "wall_s": time.time() - t0,
                       "rerun": rerun_note},
    }
    (RESULTS / "s07.json").write_text(json.dumps(out, indent=1, default=str))
    print("wrote", RESULTS / "s07.json")


if __name__ == "__main__":
    main()
