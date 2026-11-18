"""Produce the real replays for the web viewer (viz/public/replays) and clean the site index.

Each replay is one closed-loop run (co-sim + glasschip_core + PowerManager) recorded through
tools/replay_writer.py with filtered chip trace events. All replays share seed 1 so the
policy switcher compares like with like.

Run: .venv/bin/python studies/make_replays.py [--only S2_P0,S7_P2] [--duration 20] [--workers 6]
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
# 3 minutes, so a 20 s replay of the ramp never heats up. The replay instead starts the module
# at thermal steady state for a 45 C ambient (labeled); studies/s08 (power stream) covers the ramp.
S5_HOT = {"ambient_c": 45.0, "events": []}

REPLAYS = {
    "S1_P0": ("S1", "P0", {"default": True}),
    "S2_P0": ("S2", "P0", {}),
    "S2_P2Q": ("S2", "P2", {"qos": True, "cache_partition": PART, "policy": {"name": "P2_edf_qos"},
                            "policy_label": "P2 EDF + QoS + cache partition"}),
    "S1_P4": ("S1", "P4", {}),
    "S2_P4": ("S2", "P4", {}),
    "S3_P0": ("S3", "P0", {}),
    "S4_P0": ("S4", "P0", {}),
    "S5_P0": ("S5", "P0", {"scenario": S5_HOT, "scenario_label": "Hot day (45 C ambient, steady state)"}),
    "S6_P0": ("S6", "P0", {}),
    "S7_P0": ("S7", "P0", {}),
    "S7_P2": ("S7", "P2", {}),
    # s04: mixed-criticality EDF plus QoS and cache partitioning is the configuration with no
    # robot-stack misses up to 96 Gbit/s, so it is the "fix" replay for the bandwidth hog.
    "S7_P2MQ": ("S7", "P2", {"qos": True, "cache_partition": PART,
                             "policy": {"name": "P2_edf_mc_qos", "scheduler": "edf_mc"},
                             "policy_label": "EDF mixed-criticality + QoS + cache partition"}),
    "S8_P0": ("S8", "P0", {}),
}


def make(item):
    rid, dur = item
    s, p, ov = REPLAYS[rid]
    t0 = time.perf_counter()
    try:
        r = run(s, p, seed=SEED, duration_s=dur, record_replay_dir=REPLAY_ROOT / rid, trace=True,
                overrides={**ov, "replay_coalesce_us": 50})
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
    ap.add_argument("--duration", type=float, default=20.0)
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
