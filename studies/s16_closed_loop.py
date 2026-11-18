"""s16: where does chip timing change what the robot does?

The co-sim couples chip timing to the robot, but the scripted expert compensates for steady
latency well (s14) and the safety monitor's slowdown band gives margin. This study looks for
consequences honestly, in three places, without tuning VX-1 or the scenarios to force them.

Part A, safety under load. A person repeatedly walks into the workspace (1.6 m/s, every 8 s)
while (i) the cell records camera streams at 0.2, 11.9 (two raw 4K30) or 24 Gbit/s
(configs/scenarios/S9_human_during_recording.yaml), or (ii) the fifteen-task workload of S6 runs.
Each under P0 (FIFO) and under mixed-criticality EDF + QoS + cache partitioning. Robot outcome per
protective-zone entry: stop latency (zone entry to all joints below 0.05 rad/s), separation at the
moment the robot stops, and how far the hand moves after entry; chip side: safety-monitor (T8)
misses and their attribution, and the human-to-slowdown chain latency.

Part B, fast conveyor under chip stress. 0.4 m/s with one object every 4 s, wrist-camera detection
(as in S1): nominal; NPU voltage floor 0.55 and 0.50 V (S8's lower steps); a hot module (45 C
ambient at thermal steady state, as in the S5 replay); 24 Gbit/s recording under FIFO. Robot
outcome: grasp success and pick rate (Wilson 95 % intervals over pooled attempts), plus the
measured camera-to-grasp latency and VLA tail latency that cause them.

Part C, control timing. T7 response and misses, tracking error and oscillation episodes in the
Part A runs (S6 adds a second arm's 1 kHz loop on the same RT island).

Run: .venv/bin/python studies/s16_closed_loop.py [--quick] [--part A|B]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studies.common import mean_ci, provenance, run_many, savefig, style, wilson, write_json  # noqa: E402

PART = {"safety": 2, "realtime": 10, "best_effort": 4}
POLICIES = {"P0 FIFO": ("P0", {}),
            "EDF-MC + QoS + partition": ("P2", {"qos": True, "cache_partition": PART,
                                               "policy": {"scheduler": "edf_mc"}})}
HUMAN = [{"t_s": float(t), "action": "human_enter",
          "args": {"from": "front", "speed_mps": 1.6, "stop_x": 0.75, "stop_y": -0.1, "dwell_s": 2.0}}
         for t in (6, 14, 22, 30)]


def a_cases() -> dict[str, tuple[str, list]]:
    def rec(b):
        return [{"t_s": 0.0, "action": "set_bitrate", "args": {"task": "T10_record_4k", "bitrate_bps": b * 1e9}}] + HUMAN
    return {"recording 0.2 Gbit/s": ("S9", rec(0.2)), "recording 11.9 Gbit/s": ("S9", rec(11.9)),
            "recording 24 Gbit/s": ("S9", rec(24.0)), "S6 fifteen tasks": ("S6", list(HUMAN))}


def b_cases() -> dict[str, tuple[str, str, dict]]:
    vmin = lambda v: [{"t_s": 0.0, "action": "set_vmin", "args": {"domain": "npu", "v_min": v}}]  # noqa: E731
    rec = [{"t_s": 0.0, "action": "set_bitrate", "args": {"task": "T10_record_4k", "bitrate_bps": 24e9}}]
    return {"nominal": ("S1", "P0", {"events": []}),
            "NPU Vmin 0.55 V": ("S1", "P0", {"events": vmin(0.55)}),
            "NPU Vmin 0.50 V": ("S1", "P0", {"events": vmin(0.50)}),
            "hot module (45 C)": ("S5", "P0", {"events": [], "ambient_c": 45.0}),
            "recording 24 Gbit/s (FIFO)": ("S7", "P0", {"events": rec})}


def slim(r: dict) -> dict:
    keep = {k: r.get(k) for k in ("_job", "_ok", "robot", "chains", "misses", "power", "wall_s", "duration_s",
                                  "chains_raw", "error")}
    keep["tasks"] = {t: {k: v[k] for k in ("completed", "missed", "skipped", "miss_rate", "response_ms")}
                     for t, v in (r.get("tasks") or {}).items()}
    mj = r.get("missed_jobs") or []
    keep["missed_summary"] = summarize_missed(mj)
    keep["missed_t8"] = [{"release_s": m["release_ps"] / 1e12, "response_ms": m["response_ms"],
                          "dominant": m["dominant_attribution"]} for m in mj if m["task_id"] == "T8_safety"]
    if keep.get("robot"):
        keep["robot"] = {k: v for k, v in keep["robot"].items() if k != "tasks"}
    return keep


def summarize_missed(mj: list[dict]) -> dict:
    """Per task: misses and the dominant non-compute attribution (the causal chain's chip side)."""
    out = {}
    by = defaultdict(list)
    for m in mj:
        by[m["task_id"]].append(m)
    keys = ("runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware")
    for t, ms in by.items():
        wait = Counter(max(keys, key=lambda k: m["attribution_ms"].get(k, 0.0)) for m in ms)
        share = {k: float(np.mean([m["attribution_ms"].get(k, 0.0) / max(m["response_ms"], 1e-9) for m in ms]))
                 for k in ("compute",) + keys}
        out[t] = {"missed": len(ms), "dominant_wait": wait.most_common(1)[0][0], "mean_share": share,
                  "mean_response_ms": float(np.mean([m["response_ms"] for m in ms]))}
    return out


def visits_of(rs: list[dict]) -> list[dict]:
    return [v for r in rs for v in (r["robot"] or {}).get("zone_visits", [])]


def part_a_summary(runs: list[dict]) -> dict:
    out = {}
    for case in a_cases():
        for pol in POLICIES:
            rs = [r for r in runs if r["_job"].get("case") == case and r["_job"].get("policy") == pol and r.get("_ok")]
            vs = visits_of(rs)
            moving = [v for v in vs if v["speed_at_entry"] >= 0.05 and v["stop_latency_s"] is not None]
            t8c = sum(r["tasks"]["T8_safety"]["completed"] for r in rs)
            t8m = sum(r["tasks"]["T8_safety"]["missed"] for r in rs)
            t7 = [r["tasks"]["T7_control"] for r in rs]
            causes = Counter()
            for r in rs:
                ms = r["missed_summary"].get("T8_safety")
                if ms:
                    causes[ms["dominant_wait"]] += ms["missed"]
            out[f"{case} | {pol}"] = {
                "case": case, "policy": pol, "runs": len(rs), "zone_entries": len(vs), "entries_while_moving": len(moving),
                "stop_latency_ms": mean_ci([v["stop_latency_s"] * 1e3 for v in moving]),
                "stop_latency_ms_max": max((v["stop_latency_s"] * 1e3 for v in moving), default=None),
                "speed_at_entry_rad_s": mean_ci([v["speed_at_entry"] for v in moving]),
                "hand_travel_after_entry_cm": mean_ci([v["tcp_travel_after_entry_m"] * 100 for v in moving]),
                "separation_at_stop_m": mean_ci([v["sep_at_stop_m"] for v in moving if v.get("sep_at_stop_m") is not None]),
                "min_separation_while_moving_m": min((v["sep_at_stop_m"] for v in moving
                                                      if v.get("sep_at_stop_m") is not None), default=None),
                "human_to_slowdown_p99_ms": mean_ci([r["chains"]["human_to_slowdown"]["p99"] for r in rs]),
                "human_to_slowdown_max_ms": max((r["chains"]["human_to_slowdown"]["max"] for r in rs), default=None),
                "t8_miss_rate": t8m / t8c if t8c else None, "t8_misses": t8m,
                "t8_p99_ms": mean_ci([r["tasks"]["T8_safety"]["response_ms"]["p99"] for r in rs]),
                "t8_miss_dominant_wait": dict(causes),
                "t1_overhead_misses": sum(r["tasks"]["T1_ingest_overhead"]["missed"] for r in rs),
                "t7_max_response_ms": max((x["response_ms"]["max"] for x in t7), default=None),
                "t7_misses": sum(x["missed"] for x in t7),
                "t12_misses": sum(r["tasks"].get("T12_arm2_control", {}).get("missed", 0) for r in rs),
                "tracking_rms_rad": mean_ci([r["robot"]["joint_tracking_rms_rad"] for r in rs]),
                "oscillation_episodes": int(sum(r["robot"]["oscillation_episodes"] for r in rs)),
                "grasp_success": int(sum(r["robot"]["grasp_success"] for r in rs)),
                "grasp_attempts": int(sum(r["robot"]["grasp_attempts"] for r in rs)),
            }
    return out


def part_b_summary(runs: list[dict]) -> dict:
    out = {}
    for case in b_cases():
        rs = [r for r in runs if r["_job"].get("bcase") == case and r.get("_ok")]
        att = sum(r["robot"]["grasp_attempts"] for r in rs)
        ok = sum(r["robot"]["grasp_success"] for r in rs)
        pl = sum(r["robot"]["placed"] for r in rs)
        off = sum(r["robot"]["objects_offered"] for r in rs)
        causes = defaultdict(int)
        for r in rs:
            for t, v in r["missed_summary"].items():
                causes[f"{t}: {v['dominant_wait']}"] += v["missed"]
        out[case] = {
            "runs": len(rs), "grasp_attempts": att, "grasp_success": ok,
            "grasp_success_rate": ok / att if att else None, "grasp_success_ci95": list(wilson(ok, att)),
            "placed": pl, "offered": off, "pick_rate": pl / off if off else None, "pick_rate_ci95": list(wilson(pl, off)),
            "objects_missed_per_min": mean_ci([r["robot"]["objects_missed_per_min"] for r in rs]),
            "camera_to_grasp_p50_ms": mean_ci([r["chains"]["camera_to_grasp"]["p50"] for r in rs]),
            "camera_to_grasp_p99_ms": mean_ci([r["chains"]["camera_to_grasp"]["p99"] for r in rs]),
            "vla_p99_ms": mean_ci([r["tasks"]["T4_vla"]["response_ms"]["p99"] for r in rs]),
            "vla_miss_rate": (sum(r["tasks"]["T4_vla"]["missed"] for r in rs) /
                              max(1, sum(r["tasks"]["T4_vla"]["completed"] for r in rs))),
            "vla_skipped": int(sum(r["tasks"]["T4_vla"]["skipped"] for r in rs)),
            "align_err_p50_mm": mean_ci([(r["robot"]["grasp_align_err_m"]["p50"] or 0) * 1e3 for r in rs]),
            "time_throttled_s": mean_ci([r["power"]["time_throttled_s"] for r in rs]),
            "chip_misses_by_cause": dict(causes),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--part", choices=["A", "B", "AB"], default="AB")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--replot", action="store_true")
    a = ap.parse_args()
    if a.replot:
        import json
        from studies.common import RESULTS
        pth = RESULTS / "s16.json"
        d = json.loads(pth.read_text())
        d["figures"] = make_figures(d["part_a_safety_under_load"], d["part_b_fast_conveyor"])
        pth.write_text(json.dumps(d, indent=1))
        return
    seeds_a = [1, 2, 3, 4, 5] if not a.quick else [1]
    seeds_b = [1, 2, 3, 4, 5, 6, 7, 8] if not a.quick else [1]
    dur_a = 40.0 if not a.quick else 16.0
    dur_b = 60.0 if not a.quick else 16.0
    jobs = []
    if "A" in a.part:
        for case, (scen, ev) in a_cases().items():
            for pol, (p, ov) in POLICIES.items():
                for s in seeds_a:
                    jobs.append({"part": "A", "case": case, "policy": pol, "seed": s,
                                 "kwargs": dict(scenario_yaml=scen, policy_yaml=p, seed=s, duration_s=dur_a,
                                                overrides={**ov, "keep_chains": True, "scenario": {"events": ev}})})
    if "B" in a.part:
        for case, (scen, p, sov) in b_cases().items():
            for s in seeds_b:
                jobs.append({"part": "B", "bcase": case, "seed": s,
                             "kwargs": dict(scenario_yaml=scen, policy_yaml=p, seed=s, duration_s=dur_b,
                                            overrides={"scenario": {**sov, "conveyor_speed_mps": 0.4,
                                                                    "object_spacing_m": 1.6}})})
    runs = run_many(jobs, a.workers, slim=slim)
    ra = [r for r in runs if r["_job"]["part"] == "A"]
    rb = [r for r in runs if r["_job"]["part"] == "B"]
    A = part_a_summary(ra) if ra else {}
    B = part_b_summary(rb) if rb else {}
    chains = []
    for r in runs:
        if not r.get("_ok"):
            continue
        j = r["_job"]
        worst = sorted(r["missed_summary"].items(), key=lambda kv: -kv[1]["missed"])[:3]
        chains.append({"part": j["part"], "case": j.get("case") or j.get("bcase"), "policy": j.get("policy", "P0 FIFO"),
                       "seed": j["seed"],
                       "chip": [{"task": t, "missed": v["missed"], "why": v["dominant_wait"],
                                 "mean_response_ms": round(v["mean_response_ms"], 2)} for t, v in worst],
                       "robot": {k: (r["robot"] or {}).get(k) for k in ("grasp_attempts", "grasp_success", "placed",
                                                                        "objects_offered", "oscillation_episodes",
                                                                        "safety_zone_entries")},
                       "stop_latency_ms": [round(v["stop_latency_s"] * 1e3, 1) for v in (r["robot"] or {}).get("zone_visits", [])
                                           if v["stop_latency_s"] is not None and v["speed_at_entry"] >= 0.05]})
    headline = make_headline(A, B)
    figs = make_figures(A, B)
    write_json("s16", {"headline": headline, "part_a_safety_under_load": A, "part_b_fast_conveyor": B,
                       "causal_chains": chains, "figures": figs,
                       "definitions": {
                           "stop_latency": "protective-zone entry (separation <= 0.45 m) to all arm joints below 0.05 rad/s; "
                                           "only entries where the arm was still moving (>= 0.05 rad/s)",
                           "separation_at_stop": "human-to-robot surface distance when the arm reached the stop threshold",
                           "pick_rate": "objects placed in the bin / objects offered",
                           "why": "largest non-compute attribution component of the missed jobs"},
                       "realism": {"S9": "configs/scenarios/S9_human_during_recording.yaml header",
                                   "fast_conveyor": "0.4 m/s is the top of the spec's 0.1-0.4 m/s range; NPU floors are "
                                                    "S8 steps; 45 C ambient is S5's range; 24 Gbit/s is an S7 step"},
                       "provenance": provenance("studies/s16_closed_loop.py",
                                                ["configs/scenarios/S9_human_during_recording.yaml",
                                                 "configs/scenarios/S6_fifteen_at_once.yaml", "configs/scenarios/S1_nominal.yaml",
                                                 "configs/scenarios/S5_hot_day.yaml", "configs/scenarios/S7_bandwidth_hog.yaml"],
                                                sorted(set(seeds_a) | set(seeds_b)),
                                                {"duration_part_a_s": dur_a, "duration_part_b_s": dur_b, "quick": a.quick},
                                                runs)})
    print(headline)


def make_headline(A: dict, B: dict) -> dict:
    h: dict = {}
    if A:
        h["stop_latency_ms_mean"] = {k: v["stop_latency_ms"]["mean"] for k, v in A.items()}
        h["t8_misses"] = {k: v["t8_misses"] for k, v in A.items()}
        h["min_separation_while_moving_m"] = {k: v["min_separation_while_moving_m"] for k, v in A.items()}
        h["t7_max_response_ms"] = max((v["t7_max_response_ms"] or 0) for v in A.values())
        h["oscillation_episodes_total"] = sum(v["oscillation_episodes"] for v in A.values())
    if B:
        h["grasp_success_rate"] = {k: v["grasp_success_rate"] for k, v in B.items()}
        h["pick_rate"] = {k: v["pick_rate"] for k, v in B.items()}
        h["camera_to_grasp_p50_ms"] = {k: v["camera_to_grasp_p50_ms"]["mean"] for k, v in B.items()}
    return h


def make_figures(A: dict, B: dict) -> list[str]:
    plt = style()
    out = []
    if A:
        cases = list(a_cases())
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.4), constrained_layout=True)
        cols = {"P0 FIFO": "#D1495B", "EDF-MC + QoS + partition": "#2E86AB"}
        w = 0.38
        x = np.arange(len(cases))
        for i, pol in enumerate(POLICIES):
            vals = [A[f"{c} | {pol}"] for c in cases]
            for ax, key in zip(axes, ("stop_latency_ms", "human_to_slowdown_p99_ms", "separation_at_stop_m")):
                m = np.array([v[key]["mean"] if v[key]["mean"] is not None else np.nan for v in vals])
                lo = np.array([v[key]["lo"] if v[key]["lo"] is not None else np.nan for v in vals])
                hi = np.array([v[key]["hi"] if v[key]["hi"] is not None else np.nan for v in vals])
                ax.bar(x + (i - 0.5) * w, m, w, color=cols[pol], label=pol,
                       yerr=np.vstack([m - lo, hi - m]), capsize=3, error_kw={"lw": 1})
        labels = [c.replace("recording ", "rec ").replace(" fifteen tasks", "") for c in cases]
        for ax in axes:
            ax.set_xticks(x, labels, fontsize=8.5)
        axes[0].set_ylabel("ms")
        axes[0].set_title("Stop latency after zone entry")
        axes[1].set_ylabel("ms")
        axes[1].set_title("Human-to-slowdown chain, p99")
        axes[2].set_ylabel("m")
        axes[2].set_title("Separation when the arm stopped")
        axes[0].legend(fontsize=8, loc="upper left")
        out += savefig(fig, "s16_safety_under_load", "Bars: mean over protective-zone entries (or runs for the chain), "
                       "95 % CI. 5 seeds x 4 approaches. Scripted expert in hybrid mode; VX-1 is fictional.")
    if B:
        cases = list(B)
        fig, axes = plt.subplots(1, 3, figsize=(16, 4.4), constrained_layout=True)
        x = np.arange(len(cases))
        g = np.array([B[c]["grasp_success_rate"] for c in cases])
        gl = np.array([B[c]["grasp_success_ci95"][0] for c in cases])
        gh = np.array([B[c]["grasp_success_ci95"][1] for c in cases])
        p = np.array([B[c]["pick_rate"] for c in cases])
        pl = np.array([B[c]["pick_rate_ci95"][0] for c in cases])
        ph = np.array([B[c]["pick_rate_ci95"][1] for c in cases])
        axes[0].bar(x - 0.19, g, 0.38, color="#2E86AB", yerr=np.vstack([g - gl, gh - g]), capsize=3, label="grasp success")
        axes[0].bar(x + 0.19, p, 0.38, color="#3BB273", yerr=np.vstack([p - pl, ph - p]), capsize=3, label="pick rate")
        axes[0].set_ylim(0, 1.1)
        axes[0].set_title("Robot outcome at 0.4 m/s")
        axes[0].legend(fontsize=8, loc="lower left")
        c2g = [B[c]["camera_to_grasp_p50_ms"]["mean"] for c in cases]
        vla = [B[c]["vla_p99_ms"]["mean"] for c in cases]
        axes[1].bar(x - 0.19, c2g, 0.38, color="#8E7CC3", label="camera to grasp, p50")
        axes[1].bar(x + 0.19, vla, 0.38, color="#D1495B", label="VLA response, p99")
        axes[1].set_ylabel("ms")
        axes[1].set_title("Chip-side cause")
        axes[1].legend(fontsize=8, loc="upper left")
        mm = np.array([B[c]["objects_missed_per_min"]["mean"] for c in cases])
        ml = np.array([B[c]["objects_missed_per_min"]["lo"] for c in cases])
        mh = np.array([B[c]["objects_missed_per_min"]["hi"] for c in cases])
        axes[2].bar(x, mm, 0.6, color="#F0A202", yerr=np.vstack([mm - ml, mh - mm]), capsize=3)
        axes[2].set_ylabel("objects per minute")
        axes[2].set_title("Objects that reached the end of the belt")
        for ax in axes:
            ax.set_xticks(x, [c.replace(" (FIFO)", "\n(FIFO)").replace(" (45 C)", "\n(45 C)") for c in cases], fontsize=8.5)
        out += savefig(fig, "s16_fast_conveyor", "0.4 m/s, one object every 4 s, wrist-camera detection, 8 seeds x 60 s. "
                       "Wilson 95 % intervals over pooled attempts. VX-1 is fictional; parameters are assumptions.")
    return out


if __name__ == "__main__":
    main()
