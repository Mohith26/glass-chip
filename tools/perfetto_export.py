"""Convert a Glass Chip trace to Chrome JSON trace format (loads in ui.perfetto.dev).

Input is either a binary .gct file written by `Sim.trace_open()` or the list of
dicts returned by `Sim.trace_drain()`. The output has one track per engine unit
(CPU core, NPU tile, SIMT CU, DSP unit, DMA engine, RT core), one track per task
with a span per job, counter tracks for DVFS frequency and DRAM queue depth, and
optionally the NoC transfers.

Usage:
    python tools/perfetto_export.py trace.gct -o trace.json [--xfers] [--max-xfers N]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
from collections.abc import Iterable
from typing import Any

RECORD = struct.Struct("<qqqqdiiBBHbbbb")  # 56 bytes, see core/src/sim.cpp pack_rec()
assert RECORD.size == 56

PID_ENGINES = 1
PID_TASKS = 2
PID_COUNTERS = 3
PID_NOC = 4


def read_gct(path: str | pathlib.Path) -> list[dict[str, Any]]:
    """Read a .gct file into the same dict format as Sim.trace_drain()."""
    data = pathlib.Path(path).read_bytes()
    if data[:4] != b"GCT1":
        raise ValueError(f"{path}: not a .gct file")
    _ver, rec_size = struct.unpack_from("<II", data, 4)
    if rec_size != RECORD.size:
        raise ValueError(f"{path}: unexpected record size {rec_size}")
    if data[-4:] != b"GCTE":
        raise ValueError(f"{path}: missing footer (was trace_close() called?)")
    (flen,) = struct.unpack_from("<Q", data, len(data) - 12)
    footer_start = len(data) - 12 - flen
    names = json.loads(data[footer_start : len(data) - 12])
    body = data[12:footer_start]
    out: list[dict[str, Any]] = []
    tasks, ops, kinds, blocks = names["tasks"], names["ops"], names["kinds"], names["blocks"]
    for off in range(0, len(body) - len(body) % RECORD.size, RECORD.size):
        t, dur, job, nbytes, value, task, op, kind, block, unit, sx, sy, dx, dy = RECORD.unpack_from(body, off)
        kname = kinds[kind]
        rec: dict[str, Any] = {
            "t_ps": t,
            "dur_ps": dur,
            "kind": kname,
            "block": blocks[block],
            "unit": unit,
            "task_id": tasks[task] if 0 <= task < len(tasks) else "",
            "job_id": job,
            "op_id": ops[task][op] if 0 <= task < len(ops) and 0 <= op < len(ops[task]) else "",
            "src_node": (sx, sy) if kname == "xfer" else None,
            "dst_node": (dx, dy) if kname == "xfer" else None,
            "bytes": nbytes,
            "value": value,
        }
        if kname == "op":
            mask = (sx & 0xFF) | ((sy & 0xFF) << 8) | ((dx & 0xFF) << 16) | ((dy & 0xFF) << 24)
            rec["units"] = [b for b in range(32) if mask >> b & 1]
        out.append(rec)
    return out


def to_chrome(records: Iterable[dict[str, Any]], include_xfers: bool = False, max_xfers: int = 200_000) -> dict[str, Any]:
    """Build a Chrome JSON trace dict from trace records (times converted to us)."""
    events: list[dict[str, Any]] = []
    engine_tids: dict[tuple[str, int], int] = {}
    task_tids: dict[str, int] = {}
    releases: dict[int, dict[str, Any]] = {}
    n_xfers = 0

    def us(ps: int) -> float:
        return ps / 1e6

    def engine_tid(block: str, unit: int) -> int:
        key = (block, unit)
        if key not in engine_tids:
            engine_tids[key] = len(engine_tids) + 1
        return engine_tids[key]

    def task_tid(task: str) -> int:
        if task not in task_tids:
            task_tids[task] = len(task_tids) + 1
        return task_tids[task]

    for r in records:
        kind = r["kind"]
        if kind == "op":
            units = r.get("units") or [int(r["unit"])]
            n_units = len(units)
            for u in units:
                events.append({
                    "ph": "X", "pid": PID_ENGINES, "tid": engine_tid(r["block"], int(u)),
                    "ts": us(r["t_ps"]), "dur": us(r["dur_ps"]), "name": r["op_id"] or "op", "cat": r["task_id"],
                    "args": {"task": r["task_id"], "job": r["job_id"], "bytes": r["bytes"], "util": r["value"], "units": n_units},
                })
        elif kind == "job_release":
            releases[r["job_id"]] = r
        elif kind == "job_finish":
            rel = releases.pop(r["job_id"], None)
            start = rel["t_ps"] if rel else r["t_ps"] - r["dur_ps"]
            deadline = int(r["value"])
            events.append({
                "ph": "X", "pid": PID_TASKS, "tid": task_tid(r["task_id"]), "ts": us(start), "dur": us(r["t_ps"] - start),
                "name": f"{r['task_id']} #{r['job_id']}", "cat": "job",
                "args": {"response_ms": (r["t_ps"] - start) / 1e9, "deadline_ms": (deadline - start) / 1e9, "missed": r["t_ps"] > deadline},
            })
        elif kind == "job_miss":
            events.append({"ph": "i", "s": "t", "pid": PID_TASKS, "tid": task_tid(r["task_id"]), "ts": us(r["t_ps"]),
                           "name": "deadline miss", "args": {"late_ms": r["value"] / 1e9, "job": r["job_id"]}})
        elif kind == "preempt":
            events.append({"ph": "i", "s": "t", "pid": PID_ENGINES, "tid": engine_tid(r["block"], int(r["unit"])), "ts": us(r["t_ps"]),
                           "name": "preempted", "args": {"task": r["task_id"], "job": r["job_id"]}})
        elif kind == "fault":
            names = {0: "npu bit flip", 1: "noc delay", 2: "noc drop+retry"}
            events.append({"ph": "i", "s": "g", "pid": PID_TASKS, "tid": task_tid(r["task_id"] or "faults"), "ts": us(r["t_ps"]),
                           "name": names.get(int(r["unit"]), "fault"), "args": {"job": r["job_id"], "op": r["op_id"], "value": r["value"]}})
        elif kind == "freq":
            events.append({"ph": "C", "pid": PID_COUNTERS, "ts": us(r["t_ps"]), "name": f"freq {r['block']} (MHz)",
                           "args": {"MHz": r["value"] / 1e6}})
        elif kind == "dram_q":
            events.append({"ph": "C", "pid": PID_COUNTERS, "ts": us(r["t_ps"]), "name": f"DRAM ch{r['unit']} queue",
                           "args": {"depth": r["value"]}})
        elif kind == "xfer" and include_xfers and n_xfers < max_xfers:
            n_xfers += 1
            src, dst = r["src_node"], r["dst_node"]
            events.append({
                "ph": "X", "pid": PID_NOC, "tid": int(src[1]) * 8 + int(src[0]) + 1, "ts": us(r["t_ps"]), "dur": us(r["dur_ps"]),
                "name": f"{src[0]},{src[1]}->{dst[0]},{dst[1]}", "cat": r["task_id"] or "writeback",
                "args": {"bytes": r["bytes"], "task": r["task_id"], "job": r["job_id"]},
            })
    meta: list[dict[str, Any]] = [
        {"ph": "M", "pid": PID_ENGINES, "name": "process_name", "args": {"name": "Engines"}},
        {"ph": "M", "pid": PID_TASKS, "name": "process_name", "args": {"name": "Tasks"}},
        {"ph": "M", "pid": PID_COUNTERS, "name": "process_name", "args": {"name": "Counters"}},
    ]
    if include_xfers:
        meta.append({"ph": "M", "pid": PID_NOC, "name": "process_name", "args": {"name": "NoC transfers (by source node)"}})
    for (block, unit), tid in sorted(engine_tids.items(), key=lambda kv: kv[1]):
        meta.append({"ph": "M", "pid": PID_ENGINES, "tid": tid, "name": "thread_name", "args": {"name": f"{block}[{unit}]"}})
        meta.append({"ph": "M", "pid": PID_ENGINES, "tid": tid, "name": "thread_sort_index", "args": {"sort_index": tid}})
    for task, tid in task_tids.items():
        meta.append({"ph": "M", "pid": PID_TASKS, "tid": tid, "name": "thread_name", "args": {"name": task}})
    return {"traceEvents": meta + events, "displayTimeUnit": "ns", "otherData": {"source": "glasschip-core"}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", help=".gct file, or a .json list of drained trace dicts")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--xfers", action="store_true", help="include NoC transfers (can be large)")
    ap.add_argument("--max-xfers", type=int, default=200_000)
    args = ap.parse_args()
    if args.trace.endswith(".gct"):
        recs = read_gct(args.trace)
    else:
        recs = json.loads(pathlib.Path(args.trace).read_text())
    doc = to_chrome(recs, include_xfers=args.xfers, max_xfers=args.max_xfers)
    pathlib.Path(args.out).write_text(json.dumps(doc))
    print(f"wrote {args.out}: {len(doc['traceEvents'])} events from {len(recs)} records")


if __name__ == "__main__":
    main()
