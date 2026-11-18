"""Produce the real replays for the web viewer (viz/public/replays) and clean the site index.

Each replay is one closed-loop run (co-sim + glasschip_core + PowerManager) recorded through
tools/replay_writer.py with filtered chip trace events. All replays share seed 1 so the
policy switcher compares like with like.

Run: .venv/bin/python studies/make_replays.py [--only S2_P0,S7_P2] [--duration 40] [--workers 6]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studies.common import write_json  # noqa: E402
from studies.runner import REPLAY_ROOT, run  # noqa: E402

SEED = 1
PART = {"safety": 2, "realtime": 10, "best_effort": 4}
# S5 is a 60 s ambient ramp in its YAML, but the enclosure's thermal time constant is about
# 3 minutes, so a short replay of the ramp never heats up. The replay instead runs at a 45 C ambient
# with the runner's "settle" thermal start: under P0 there is no thermal steady state at 45 C (the
# leakage feedback runs away), so after the 1 s burn-in the power manager starts the die at the
# 100 C throttle threshold and throttling takes over from there (labeled); studies/s08 covers the ramp.
# It runs the conveyor at 0.4 m/s with one object every 4 s, the condition where s16 measured a
# robot consequence of throttling (objects missed per minute roughly doubled).
S5_HOT = {"ambient_c": 45.0, "events": [], "conveyor_speed_mps": 0.4, "object_spacing_m": 1.6}
# S9 demo: the s16 condition with a measurable safety consequence (24 Gbit/s, four raw 4K30 streams).
# The S9 YAML itself records two raw streams (11.9 Gbit/s); s16 sweeps both.
S9_24 = {"events": [{"t_s": 0.0, "action": "set_bitrate", "args": {"task": "T10_record_4k", "bitrate_bps": 24e9}}] +
         [{"t_s": float(t), "action": "human_enter",
           "args": {"from": "front", "speed_mps": 1.6, "stop_x": 0.75, "stop_y": -0.1, "dwell_s": 2.0}}
          for t in (6, 14, 22, 30)]}
S9_LABEL = "Person enters during recording (4 raw 4K streams, 24 Gbit/s)"

REPLAYS = {
    "S1_P0": ("S1", "P0", {"default": True}),
    "S2_P0": ("S2", "P0", {}),
    "S2_P2Q": ("S2", "P2", {"qos": True, "cache_partition": PART, "policy": {"name": "P2_edf_qos"},
                            "policy_label": "P2 EDF + QoS + cache partition"}),
    "S1_P4": ("S1", "P4", {}),
    "S2_P4": ("S2", "P4", {}),
    "S3_P0": ("S3", "P0", {}),
    "S4_P0": ("S4", "P0", {}),
    "S5_P0": ("S5", "P0", {"scenario": S5_HOT, "scenario_label": "Hot module: 45 C ambient, no steady state, starts at the 100 C throttle threshold; conveyor 0.4 m/s"}),
    "S6_P0": ("S6", "P0", {}),
    "S7_P0": ("S7", "P0", {}),
    "S7_P2": ("S7", "P2", {}),
    # s04: mixed-criticality EDF plus QoS and cache partitioning is the configuration with no
    # robot-stack misses up to 96 Gbit/s, so it is the "fix" replay for the bandwidth hog.
    "S7_P2MQ": ("S7", "P2", {"qos": True, "cache_partition": PART,
                             "policy": {"name": "P2_edf_mc_qos", "scheduler": "edf_mc"},
                             "policy_label": "EDF mixed-criticality + QoS + cache partition"}),
    "S8_P0": ("S8", "P0", {}),
    "S9_P0": ("S9", "P0", {"scenario": S9_24, "scenario_label": S9_LABEL}),
    "S9_P2MQ": ("S9", "P2", {"scenario": S9_24, "scenario_label": S9_LABEL, "qos": True, "cache_partition": PART,
                             "policy": {"name": "P2_edf_mc_qos", "scheduler": "edf_mc"},
                             "policy_label": "EDF mixed-criticality + QoS + cache partition"}),
}


def make(item):
    rid, dur = item
    s, p, ov = REPLAYS[rid]
    t0 = time.perf_counter()
    try:
        # Size controls (replay budget: < 15 MB each, < 150 MB total at 40 s): ops of one job on one unit
        # closer than 500 us merge into one busy span, NoC transfers are binned per 4 ms x flow, critical
        # paths are kept for every missed job and every 32nd job per task.
        r = run(s, p, seed=SEED, duration_s=dur, record_replay_dir=REPLAY_ROOT / rid, trace=True,
                overrides={**ov, "replay_coalesce_us": 500, "replay_xfer_bin_ms": 4, "replay_critpath_every": 32})
    except Exception as e:  # report and continue
        return rid, {"error": repr(e)}
    return rid, {"bytes": r["replay"]["bytes"], "wall_s": time.perf_counter() - t0, "duration_s": r["duration_s"],
                 "misses": r["misses"], "robot": {k: r["robot"][k] for k in ("grasp_attempts", "grasp_success", "placed",
                                                                            "oscillation_episodes")},
                 "energy_per_robot_hour_wh": r["power"]["energy_per_robot_hour_wh"],
                 "vla_p99_ms": r["tasks"].get("T4_vla", {}).get("response_ms", {}).get("p99")}


def clean_index() -> dict:
    """Drop synthetic entries from the site index (their files stay on disk, ignored)."""
    p = REPLAY_ROOT / "index.json"
    data = json.loads(p.read_text())
    data["replays"] = [r for r in data["replays"] if not r.get("synthetic") and (REPLAY_ROOT / r["path"]).exists()]
    order = list(REPLAYS)
    data["replays"].sort(key=lambda r: order.index(r["id"]) if r["id"] in order else 99)
    for r in data["replays"]:
        r["default"] = r["id"] == "S1_P0"
    p.write_text(json.dumps(data, indent=1))
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    ids = a.only.split(",") if a.only else list(REPLAYS)
    out = {}
    with Pool(a.workers) as pool:
        for rid, info in pool.imap_unordered(make, [(i, a.duration) for i in ids]):
            out[rid] = info
            print(rid, json.dumps(info), flush=True)
    idx = clean_index()
    sizes = {r["id"]: sum(f.stat().st_size for f in (REPLAY_ROOT / r["id"]).rglob("*") if f.is_file())
             for r in idx["replays"]}
    prev = {}
    pth = ROOT / "results" / "replays.json"
    if pth.exists():
        prev = json.loads(pth.read_text()).get("replays", {})
    prev.update(out)
    write_json("replays", {"replays": prev, "site_sizes_bytes": sizes, "total_bytes": sum(sizes.values()),
                           "seed": SEED, "provenance": {"script": "studies/make_replays.py"}})
    print("total MB", sum(sizes.values()) / 1e6)


if __name__ == "__main__":
    main()
