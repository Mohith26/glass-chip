"""s08: thermal. Throttling onset versus ambient (scenario S5, hot day) and its effect on deadlines.

Four parts, each labeled by what it is:

A. Steady-state analysis (model calculation on measured power). Run S1 at 25 C ambient for P0 and
   P4, take each domain's measured non-leakage power and mean voltage, then solve the steady-state
   junction temperatures with temperature-dependent leakage for ambients 25-60 C. Reports the
   ambient where the hottest junction reaches the throttle threshold (100 C), and where no fixed
   point exists (leakage thermal runaway without throttling).
B. Onset time with physical thermal mass (open-loop model calculation). From the 25 C steady
   state, ambient steps to A; the RC network is driven with the measured non-leakage power,
   recomputing leakage and regulator loss every step, until the hottest junction reaches 100 C.
   Exact up to onset (nothing throttles before then); minutes-scale because the enclosure time
   constant is about 4.5 minutes.
C. Closed-loop S5 co-sim (robot + core + power manager). S5 compresses a hot-day ramp (25 to 50 C)
   into 60 s, so package and enclosure heat capacities are divided by `slow_node_time_scale`
   (stated in the JSON) to keep the slow nodes quasi-static relative to the ramp. Junction nodes
   keep their physical time constants. Reports onset time and ambient, binding throttle time,
   deadline misses before and after onset, and robot metrics.
D. Closed-loop ambient steps: S1 settled at 25 C, then the ambient steps to A at t = 1.5 s, with
   the same time-compressed slow nodes as part C. Sustained effect of throttling on deadlines and
   the robot at each ambient, and onset time after the step (scenario seconds).

All power and thermal parameters are assumptions (power/params_vx1.yaml); VX-1 is fictional.

    .venv/bin/python studies/s08_thermal.py [--workers 8]
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
SLOW_SCALE = 100.0
AMBIENTS_A = [25, 30, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5, 55, 60]
AMBIENTS_B = [40, 42.5, 45, 47.5, 50, 55]
AMBIENTS_D = [40, 45, 50, 55]
STEP_T_S = 1.5


def _measure(policy: str, seed: int) -> dict[str, Any]:
    """Part A/B input: S1 at 25 C, per-domain non-leakage power and mean volts."""
    from studies.runner import run
    r = run("S1", policy, seed=seed, duration_s=10.0, return_objects=True,
            overrides={"power": {"thermal_init": "settle", "record_series": True}})
    pm = r["_objects"]["pm"]
    s = pm.summary()
    dur = s["duration_s"]
    parts = s["energy_j_by_domain_part"]
    pnl = {d: (v.get("dynamic", 0) + v.get("clock", 0) + v.get("wake", 0) + v.get("dvfs", 0)) / dur for d, v in parts.items()}
    volts = {d: float(np.mean(v)) for d, v in pm.series["volts"].items()}
    gated = {d: float(np.mean(v)) for d, v in pm.series["gated_frac"].items()}
    from power.thermal_analysis import open_loop_onset, steady_state_with_leakage
    thr = pm._throttle_cfg[0]
    a_rows = []
    for amb in AMBIENTS_A:
        ss = steady_state_with_leakage(pm, pnl, volts, amb, gated_frac=gated)
        a_rows.append({"ambient_c": amb, "t_max_c": None if ss["runaway"] else ss["t_max_c"], "runaway": ss["runaway"],
                       "power_w": None if ss["runaway"] else ss["power_w"],
                       "leakage_w": None if ss["runaway"] else ss["leakage_w"]})
    onset_amb = None
    for lo, hi in zip(a_rows, a_rows[1:]):
        t_lo = lo["t_max_c"]
        if t_lo is not None and t_lo < thr and (hi["runaway"] or (hi["t_max_c"] or 0) >= thr):
            if hi["runaway"]:
                onset_amb = {"between_c": [lo["ambient_c"], hi["ambient_c"]], "note": "runaway above"}
            else:
                f = (thr - t_lo) / (hi["t_max_c"] - t_lo)
                onset_amb = {"interpolated_c": lo["ambient_c"] + f * (hi["ambient_c"] - lo["ambient_c"])}
            break
    ss25 = steady_state_with_leakage(pm, pnl, volts, 25.0, gated_frac=gated)
    b_rows = []
    for amb in AMBIENTS_B:
        o = open_loop_onset(pm, pnl, volts, amb, thr, init_temps=ss25["temps_c"], gated_frac=gated)
        b_rows.append({"ambient_c": amb, "onset_s": o["onset_s"], "t_max_c_end": o["t_max_c"]})
    rule = None
    if policy == "P0":
        # What the enclosure design rule (85 C hottest junction at 40 C ambient, S1 P0) would give on this
        # build. Recorded only; the parameter file is not changed by this study.
        from power.params import deep_merge
        lo, hi = 1.0, 8.0
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            prm = deep_merge(pm.params, {"thermal": {"enclosure_to_ambient_r_k_per_w": mid}})
            ss = steady_state_with_leakage(pm, pnl, volts, 40.0, gated_frac=gated, params=prm)
            if ss["runaway"] or ss["t_max_c"] > 85.0:
                hi = mid
            else:
                lo = mid
        rule = {"rule": "S1 P0 hottest junction 85 C at 40 C ambient", "implied_enclosure_r_k_per_w": round(lo, 3),
                "used_enclosure_r_k_per_w": pm.params["thermal"]["enclosure_to_ambient_r_k_per_w"]}
    return {"policy": policy, "seed": seed, "nonleak_w_by_domain": pnl, "design_rule_check": rule, "mean_volts": volts,
            "avg_power_w_25c": s["avg_power_w"], "steady_state": a_rows, "throttle_onset_ambient": onset_amb,
            "steady_25c_t_max_c": ss25["t_max_c"], "onset_time_after_step": b_rows,
            "provenance": r["provenance"]}


def _window_misses(pm: Any, t0: float, t1: float) -> dict[str, Any]:
    n = miss = 0
    by: dict[str, int] = {}
    for j in pm.job_trace:
        tf = j["finish_ps"] / 1e12
        if t0 <= tf < t1:
            n += 1
            if j["finish_ps"] > j["deadline_ps"]:
                miss += 1
                by[j["task_id"]] = by.get(j["task_id"], 0) + 1
    return {"jobs": n, "misses": miss, "miss_rate": miss / n if n else None, "by_task": by}


def _closed(args: tuple[str, str, int, float | None, float]) -> dict[str, Any]:
    kind, policy, seed, ambient, dur = args
    from studies.runner import run
    ov: dict[str, Any] = {"power": {"thermal_init": "settle", "record_series": True}}
    if kind == "S5":
        ov["power"]["params"] = {"thermal": {"slow_node_time_scale": SLOW_SCALE}}
        scen = "S5"
    else:
        ov["power"]["params"] = {"thermal": {"slow_node_time_scale": SLOW_SCALE}}
        ov["scenario"] = {"ambient_c": 25.0, "events": [{"t_s": STEP_T_S, "action": "set_ambient",
                                                          "args": {"ambient_c": ambient}}]}
        scen = "S1"
    t0 = time.time()
    r = run(scen, policy, seed=seed, duration_s=dur, return_objects=True, overrides=ov)
    pm = r["_objects"]["pm"]
    s = pm.summary()
    ser = pm.series_arrays()
    tmax = np.max(np.stack([ser["temp_c"][d] for d in pm.domains]), axis=0)
    onset = s["first_throttle_binding_s"]
    amb_at_onset = None
    if onset is not None:
        k = int(np.searchsorted(ser["t_s"], onset))
        amb_at_onset = float(ser["ambient_c"][min(k, len(ser["ambient_c"]) - 1)])
    T = float(r["duration_s"])
    before = _window_misses(pm, 0.0, onset if onset is not None else T)
    after = _window_misses(pm, onset, T) if onset is not None else None
    step = max(len(ser["t_s"]) // 600, 1)
    series = {"t_s": ser["t_s"][::step].round(3).tolist(), "ambient_c": ser["ambient_c"][::step].round(2).tolist(),
              "t_max_c": tmax[::step].round(2).tolist(), "power_in_w": ser["power_in_w"][::step].round(3).tolist(),
              "npu_opp": ser["opp"]["npu"][::step].tolist(), "npu_cap": ser["throttle_cap"]["npu"][::step].tolist(),
              "cpu_a_opp": ser["opp"]["cpu_a"][::step].tolist(), "cpu_a_cap": ser["throttle_cap"]["cpu_a"][::step].tolist()}
    return {
        "kind": kind, "policy": policy, "seed": seed, "ambient_c": ambient, "duration_s": dur,
        "slow_node_time_scale": SLOW_SCALE,
        "first_throttle_s": s["first_throttle_s"], "first_throttle_binding_s": onset,
        "ambient_at_binding_onset_c": amb_at_onset, "time_throttle_binding_s": s["time_throttle_binding_s"],
        "time_throttle_binding_s_by_domain": s["time_throttle_binding_s_by_domain"],
        "peak_temp_c": s["peak_temp_c"], "energy_per_robot_hour_wh": s["energy_per_robot_hour_wh"],
        "settle": s["settle"], "misses_total": r["misses"]["total"], "safety_misses": r["misses"]["safety"],
        "skipped_total": int(sum(v.get("skipped", 0) or 0 for v in r["tasks"].values())),
        "misses_before_onset": before, "misses_after_onset": after,
        "tasks": {t: {k: v.get(k) for k in ("completed", "missed", "skipped")} | {"p99_ms": (v.get("response_ms") or {}).get("p99")}
                  for t, v in r["tasks"].items()},
        "robot": {k: r["robot"].get(k) for k in ("grasp_attempts", "grasp_success", "grasp_success_rate",
                                                  "picks_per_min", "oscillation_episodes")},
        "series": series, "provenance": r["provenance"], "wall_s": time.time() - t0,
    }


def figure(meas: list[dict[str, Any]], closed: list[dict[str, Any]], stem: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 3, figsize=(14, 4.2))
    col = {"P0": "#6b7280", "P4": "#2563eb"}
    for m in meas:
        a = [r["ambient_c"] for r in m["steady_state"] if not r["runaway"]]
        t = [r["t_max_c"] for r in m["steady_state"] if not r["runaway"]]
        ax[0].plot(a, t, marker="o", color=col[m["policy"]], label=f"{m['policy']} (no throttling)")
        run = [r["ambient_c"] for r in m["steady_state"] if r["runaway"]]
        if run:
            ax[0].axvline(min(run), color=col[m["policy"]], ls=":", alpha=0.7)
        b = [(r["ambient_c"], r["onset_s"] / 60) for r in m["onset_time_after_step"] if r["onset_s"] is not None]
        if b:
            ax[1].plot(*zip(*b), marker="s", color=col[m["policy"]], label=m["policy"])
    ax[0].axhline(100, color="#dc2626", ls="--", alpha=0.6)
    ax[0].set_xlabel("ambient (C)")
    ax[0].set_ylabel("steady-state hottest junction (C)")
    ax[0].set_title("A: steady state, S1 power (dotted: runaway)")
    ax[0].legend(fontsize=8)
    ax[1].set_xlabel("ambient after step from 25 C (C)")
    ax[1].set_ylabel("minutes to throttle threshold")
    ax[1].set_title("B: onset time, physical thermal mass")
    ax[1].legend(fontsize=8)
    for c in closed:
        if c["kind"] == "S5" and c["seed"] == min(x["seed"] for x in closed if x["kind"] == "S5"):
            s = c["series"]
            ax[2].plot(s["t_s"], s["t_max_c"], color=col[c["policy"]], label=f"{c['policy']} hottest junction")
            ax[2].plot(s["t_s"], s["ambient_c"], color=col[c["policy"]], ls=":", alpha=0.5)
            if c["first_throttle_binding_s"] is not None:
                ax[2].axvline(c["first_throttle_binding_s"], color=col[c["policy"]], ls="--", alpha=0.6)
    ax[2].axhline(100, color="#dc2626", ls="--", alpha=0.6)
    ax[2].set_xlabel("scenario time (s), time-compressed slow nodes")
    ax[2].set_ylabel("C")
    ax[2].set_title("C: S5 hot day, closed loop (dashed: binding onset)")
    ax[2].legend(fontsize=8)
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.suptitle("s08 thermal (VX-1 fictional; all thermal parameters are assumptions)", fontsize=10)
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
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--rerun-policy", default=None,
                    help="rerun only this policy (all parts) and keep the other policy's results from results/s08.json")
    ap.add_argument("--reuse-ab-s5", action="store_true",
                    help="keep parts A, B and the S5 runs from the existing results/s08.json; rerun part D only")
    a = ap.parse_args()
    t0 = time.time()
    reused = None
    closed: list[dict[str, Any]] = []
    meas: list[dict[str, Any]] = []
    if a.reuse_ab_s5 and (RESULTS / "s08.json").exists():
        old = json.loads((RESULTS / "s08.json").read_text())
        meas = old["steady_state_and_onset_time"]
        closed = [r for r in old["runs"] if r["kind"] == "S5"]
        reused = {"from": "results/s08.json", "parts": ["A", "B", "C"], "previous_provenance": old.get("provenance")}
    pols = ("P0", "P4")
    if a.rerun_policy and (RESULTS / "s08.json").exists():
        old = json.loads((RESULTS / "s08.json").read_text())
        meas = [m for m in old["steady_state_and_onset_time"] if m["policy"] != a.rerun_policy]
        closed = [r for r in old["runs"] if r["policy"] != a.rerun_policy]
        reused = {"from": "results/s08.json", "kept_policies": [p for p in pols if p != a.rerun_policy],
                  "rerun_policy": a.rerun_policy, "previous_provenance": old.get("provenance")}
        pols = (a.rerun_policy,)
        closed_jobs = [("S5", p, s, None, 60.0) for p in pols for s in a.seeds]
    else:
        closed_jobs = [] if reused else [("S5", p, s, None, 60.0) for p in pols for s in a.seeds]
    closed_jobs += [("step", p, s, amb, 20.0) for p in pols for s in a.seeds[:2] for amb in AMBIENTS_D]
    with Pool(a.workers) as pool:
        meas_async = [] if (reused and not a.rerun_policy) else [pool.apply_async(_measure, (p, a.seeds[0])) for p in pols]
        n0 = len(closed)
        for r in pool.imap_unordered(_closed, closed_jobs):
            closed.append(r)
            print(f"[{len(closed) - n0}/{len(closed_jobs)}] {r['kind']} {r['policy']} amb={r['ambient_c']} seed {r['seed']}: "
                  f"onset {r['first_throttle_binding_s']} amb@onset {r['ambient_at_binding_onset_c']} "
                  f"Tpk {r['peak_temp_c']:.1f} misses {r['misses_total']} (safety {r['safety_misses']})", flush=True)
        meas = meas + [m.get() for m in meas_async]
    meas.sort(key=lambda m: m["policy"])
    closed.sort(key=lambda r: (r["kind"], r["policy"], r["ambient_c"] or 0, r["seed"]))
    s5 = {}
    for p in ("P0", "P4"):
        rr = [r for r in closed if r["kind"] == "S5" and r["policy"] == p]
        on = [r["first_throttle_binding_s"] for r in rr if r["first_throttle_binding_s"] is not None]
        s5[p] = {"runs": len(rr), "runs_with_binding_throttle": len(on),
                 "onset_s_mean": float(np.mean(on)) if on else None,
                 "onset_s_range": [float(min(on)), float(max(on))] if on else None,
                 "ambient_at_onset_c": sorted({r["ambient_at_binding_onset_c"] for r in rr if r["ambient_at_binding_onset_c"] is not None}),
                 "time_binding_s_mean": float(np.mean([r["time_throttle_binding_s"] for r in rr])),
                 "misses_before_onset": int(sum(r["misses_before_onset"]["misses"] for r in rr)),
                 "jobs_before_onset": int(sum(r["misses_before_onset"]["jobs"] for r in rr)),
                 "misses_after_onset": int(sum((r["misses_after_onset"] or {}).get("misses", 0) for r in rr)),
                 "jobs_after_onset": int(sum((r["misses_after_onset"] or {}).get("jobs", 0) for r in rr)),
                 "safety_misses": int(sum(r["safety_misses"] for r in rr)),
                 "peak_temp_c_max": float(max(r["peak_temp_c"] for r in rr)),
                 "grasp": [int(sum(r["robot"]["grasp_success"] or 0 for r in rr)),
                           int(sum(r["robot"]["grasp_attempts"] or 0 for r in rr))]}
    fixed = {}
    for p in ("P0", "P4"):
        for amb in AMBIENTS_D:
            rr = [r for r in closed if r["kind"] == "step" and r["policy"] == p and r["ambient_c"] == amb]
            if not rr:
                continue
            on = [r["first_throttle_binding_s"] for r in rr]
            fixed[f"{p}@{amb:g}C"] = {
                "onset_after_step_s": [None if o is None else round(o - STEP_T_S, 3) for o in on],
                "time_binding_frac": float(np.mean([r["time_throttle_binding_s"] / max(r["duration_s"] - 1.0, 1e-9) for r in rr])),
                "misses": int(sum(r["misses_total"] for r in rr)), "skipped": int(sum(r["skipped_total"] for r in rr)),
                "safety_misses": int(sum(r["safety_misses"] for r in rr)),
                "peak_temp_c_max": float(max(r["peak_temp_c"] for r in rr)),
                "energy_per_robot_hour_wh": float(np.mean([r["energy_per_robot_hour_wh"] for r in rr])),
                "settle_steady_t_max_c": [r["settle"]["steady_t_max_c"] if r["settle"] else None for r in rr],
                "settle_runaway": [bool(r["settle"]["runaway"]) if r["settle"] else None for r in rr],
                "grasp": [int(sum(r["robot"]["grasp_success"] or 0 for r in rr)),
                          int(sum(r["robot"]["grasp_attempts"] or 0 for r in rr))],
                "oscillation_episodes": int(sum(r["robot"]["oscillation_episodes"] or 0 for r in rr)),
                "misses_by_task": {t: int(sum(r["tasks"][t]["missed"] or 0 for r in rr if t in r["tasks"]))
                                   for t in sorted({t for r in rr for t in r["tasks"]})
                                   if sum(r["tasks"][t]["missed"] or 0 for r in rr if t in r["tasks"])},
            }
    FIG.mkdir(parents=True, exist_ok=True)
    figs = figure(meas, closed, FIG / "s08_thermal")
    prov0 = closed[0]["provenance"]
    out = {
        "study": "s08_thermal",
        "question": "S5: throttling onset time vs ambient and its effect on deadlines",
        "labels": [
            "VX-1 is a fictional SoC; every power and thermal parameter is an assumption (power/params_vx1.yaml).",
            "Thermal design rule (assumption): enclosure sized so S1 P0 keeps the hottest junction at 85 C at a 40 C "
            "rated ambient, giving enclosure_to_ambient_r_k_per_w = 4.5.",
            f"Parts C and D compress slow thermal dynamics: package and enclosure heat capacity divided by {SLOW_SCALE:g}. "
            "Onset times there are scenario seconds, not wall-clock minutes; part B gives physical-mass times.",
            "Thermal settle: 1 s burn-in, then the thermal state jumps to the steady state for the measured power "
            "(to the 100 C throttle threshold if no steady state exists).",
            "Parts A and B are model calculations on power measured in a co-sim run, not co-sim results.",
            "Onset = first step where a thermal OPP cap is below the OPP the policy wanted (binding throttle).",
        ],
        "thermal_params": json.loads(json.dumps(__import__("power.params", fromlist=["load_params"]).load_params()["thermal"])),
        "steady_state_and_onset_time": meas,
        "s5_closed_loop": s5,
        "ambient_step_closed_loop": fixed,
        "runs": closed,
        "figures": figs,
        "build_check": __import__("power.provenance", fromlist=["check_build_ids"]).check_build_ids(closed + meas),
        "provenance": {"script": "studies/s08_thermal.py", "git_sha": prov0.get("git_sha"),
                       "core_build": prov0.get("core_build"), "date": dt.date.today().isoformat(),
                       "configs": ["configs/soc/vx1.yaml", "configs/fidelity/interactive.yaml", "power/params_vx1.yaml",
                                   "configs/scenarios/S5_hot_day.yaml", "configs/scenarios/S1_nominal.yaml",
                                   "configs/policies/P0_nominal.yaml", "configs/policies/P4_pace_to_deadline.yaml"],
                       "seeds": a.seeds, "wall_s": time.time() - t0, "reused": reused},
    }
    (RESULTS / "s08.json").write_text(json.dumps(out, indent=1, default=str))
    print("wrote", RESULTS / "s08.json")


if __name__ == "__main__":
    main()
