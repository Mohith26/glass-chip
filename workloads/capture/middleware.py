"""T9 middleware parameters -> workloads/graphs/middleware.json (format per notes/CONTRACTS.md).

Fallback (spec V4 failed: no Docker / ROS 2 in this sandbox):
1. Serialization, deserialization and copy costs are MEASURED on this Mac with
   workloads/capture/middleware/mw_bench.cpp, a CDR (XCDR1) implementation of the
   stack's actual ROS 2 message layouts and sizes.
2. They are converted to per-KB and fixed costs, then scaled to a VX-1 application
   core by a documented factor (an assumption, not a measurement).
3. Executor dispatch overhead comes from a published ROS 2 executor study (cited
   below), not from this machine.

Run: .venv/bin/python workloads/capture/middleware.py
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common import GRAPHS, ROOT, machine_string, provenance

SCRIPT = "workloads/capture/middleware.py"
SRC = ROOT / "workloads" / "capture" / "middleware" / "mw_bench.cpp"
BIN = ROOT / "build" / "workloads_mw" / "mw_bench"
RAW_OUT = ROOT / "workloads" / "capture" / "middleware" / "mw_bench_results.json"

# Mac -> VX-1 app-core scaling (ASSUMPTION). Mac performance cores run around 3.5-4 GHz with a very
# wide out-of-order core; VX-1 app cores are 2.0 GHz with an assumed sustained IPC of 2.0
# (configs/soc/vx1.yaml). Bulk copies here run at about 80 GB/s single-core; a VX-1 core is assumed
# to sustain about 25 GB/s. Both ratios land near 3, so every measured time is multiplied by 3.0.
MAC_TO_TARGET = 3.0

# Published executor latency: ROS 2 Autoware reference system, hot path (front LiDAR driver ->
# points transformer -> point cloud fusion -> voxel grid downsampler -> ray ground filter ->
# object collision estimator: 5 message hops), N=64 workload so callbacks do almost no work.
# Average critical-path latency in ms, Table I of the Klepsydra ring-buffer executor paper.
PUBLISHED_EXECUTOR = {
    "source": ("Klepsydra, 'The ring-buffer ROS2 executor: a novel approach for real-time ROS2 Space applications', "
               "2023, Table I (Autoware reference system, N=64, average critical-path latency); hot-path definition from "
               "github.com/ros-realtime/reference-system autoware_reference_system README"),
    "avg_critical_path_ms": {"static_single_threaded": 1.0, "single_threaded": 1.9, "multi_threaded": 5.4},
    "hot_path_hops": 5,
}


def run_bench() -> dict:
    BIN.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["clang++", "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(BIN), str(SRC)], check=True)
    out = subprocess.run([str(BIN)], check=True, capture_output=True, text=True).stdout
    res = json.loads(out)
    res["compiler"] = subprocess.run(["clang++", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    res["machine"] = machine_string()
    RAW_OUT.write_text(json.dumps(res, indent=1) + "\n")
    return res


def fit(msgs: list[dict], key: str) -> tuple[float, float]:
    """Per-KB slope from bulk messages (>= 64 KB), fixed cost from small ones (median residual, >= 0)."""
    big = [m for m in msgs if m["bytes"] >= 65536]
    small = [m for m in msgs if m["bytes"] < 65536]
    xs = [m["bytes"] / 1024 for m in big]
    ys = [m[key] for m in big]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    resid = sorted(max(0.0, m[key] - slope * m["bytes"] / 1024) for m in small)
    fixed = resid[len(resid) // 2] if resid else 0.0
    return slope, fixed


def build() -> dict:
    res = run_bench()
    msgs = res["messages"]
    ser_kb, ser_fix = fit(msgs, "serialize_ns")
    de_kb, de_fix = fit(msgs, "deserialize_ns")
    cp_kb, _ = fit(msgs, "memcpy_ns")
    s = MAC_TO_TARGET
    hops = PUBLISHED_EXECUTOR["hot_path_hops"]
    lat = PUBLISHED_EXECUTOR["avg_critical_path_ms"]
    per_hop = {k: v * 1e6 / hops for k, v in lat.items()}
    mw = {
        "provenance": provenance(
            source="CDR micro-benchmark of the stack's ROS 2 message types (mw_bench.cpp) + published executor latency",
            script=SCRIPT, method="mixed",
            notes=("FALLBACK (spec V4 failed: no Docker/ROS 2). Serialization/deserialization/copy measured on this Mac "
                   f"({res['compiler']}, -O2), scaled by mac_to_target_scale={MAC_TO_TARGET} (ASSUMPTION, see "
                   "scaling.rationale). Executor dispatch from the cited study: average hot-path latency divided by its "
                   f"{hops} hops, not scaled (platform of the study, assumption). events_dispatch_ns uses the "
                   "StaticSingleThreadedExecutor figure as a proxy (assumption: both avoid rebuilding the wait set). "
                   "The per-hop figures include DDS delivery and thread wake-ups, not only executor bookkeeping."),
            references=[PUBLISHED_EXECUTOR["source"]]),
        "copy": {
            "serialize_ns_per_kb": round(ser_kb * s, 2),
            "deserialize_ns_per_kb": round(de_kb * s, 2),
            "fixed_ns_per_msg": round((ser_fix + de_fix) * s, 1),
            "copies": 2,
            "copy_ns_per_kb": round(cp_kb * s, 2),
            "notes": ("copy transport: serialize into a CDR buffer, DDS copies it into its history / shared segment, the "
                      "reader copies it out, then deserializes: 2 full-buffer copies besides ser/deser."),
        },
        "zero_copy": {
            "serialize_ns_per_kb": 0.0,
            "deserialize_ns_per_kb": 0.0,
            "fixed_ns_per_msg": round(max(res["zero_copy_handoff_ns"], 1.0) * s + 500.0, 1),
            "copies": 0,
            "copy_ns_per_kb": 0.0,
            "notes": ("loaned messages (bounded/plain types or type adaptation): refcount + pointer hand-off measured, plus "
                      "500 ns assumed for the loan/return calls into the RMW. Requires fixed-size message types."),
        },
        "executor": {
            "single_threaded_dispatch_ns": round(per_hop["single_threaded"]),
            "multi_threaded_dispatch_ns": round(per_hop["multi_threaded"]),
            "events_dispatch_ns": round(per_hop["static_single_threaded"]),
            "thread_wakeup_floor_ns_measured_mac": round(res["thread_wakeup_ns"], 1),
            "published": PUBLISHED_EXECUTOR,
        },
        "scaling": {
            "mac_to_target_scale": MAC_TO_TARGET,
            "rationale": ("Assumption. Mac P-core about 3.5-4 GHz, very wide OoO; VX-1 app core 2.0 GHz, IPC 2.0 assumed "
                          "(vx1.yaml). Measured single-core bulk copy here is about "
                          f"{1024 / cp_kb:.0f} GB/s; a VX-1 core is assumed to sustain about 25 GB/s. Both ratios are near 3."),
        },
        "mac_measured": {"serialize_ns_per_kb": round(ser_kb, 3), "deserialize_ns_per_kb": round(de_kb, 3),
                         "serialize_fixed_ns": round(ser_fix, 1), "deserialize_fixed_ns": round(de_fix, 1),
                         "memcpy_ns_per_kb": round(cp_kb, 3), "zero_copy_handoff_ns": res["zero_copy_handoff_ns"]},
        "per_message": {m["name"]: {"bytes": m["bytes"], "serialize_ns": round(m["serialize_ns"] * s, 1),
                                    "deserialize_ns": round(m["deserialize_ns"] * s, 1),
                                    "copy_ns": round(m["memcpy_ns"] * s, 1)} for m in msgs},
        "topic_types": {
            "/camera/wrist/image": "Image_rgb8_640x480", "/camera/overhead/image": "Image_rgb8_640x480",
            "/camera/wrist/depth": "Image_16UC1_640x480", "/camera/overhead/depth": "Image_16UC1_640x480",
            "/camera/overhead/points": "PointCloud2_640x480_xyzrgb", "/arm1/joint_states": "JointState",
            "/arm2/joint_states": "JointState", "/plan/trajectory": "JointTrajectory_100pts",
            "/vla/action_chunk": "Float32MultiArray_action_chunk", "/perception/detections": "Detection3DArray_8",
        },
    }
    return mw


def main() -> None:
    mw = build()
    GRAPHS.mkdir(parents=True, exist_ok=True)
    (GRAPHS / "middleware.json").write_text(json.dumps(mw, indent=1) + "\n")
    c = mw["copy"]
    print(f"middleware: copy ser {c['serialize_ns_per_kb']} ns/KB, deser {c['deserialize_ns_per_kb']} ns/KB, "
          f"fixed {c['fixed_ns_per_msg']} ns, copy {c['copy_ns_per_kb']} ns/KB (target-scaled); executor {mw['executor']['single_threaded_dispatch_ns']} ns/hop single")


if __name__ == "__main__":
    main()
