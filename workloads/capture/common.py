"""Shared helpers for the workload capture scripts.

Every capture script builds a task graph (a dict matching
workloads/schema/taskgraph.schema.json) and writes it with `write_graph`.
This module holds the pieces they share: paths, provenance, the default
release/deadline table from spec Section 6.1, op-record helpers, and a
simple CPU instruction-mix estimator for analytically counted kernels.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "workloads"
GRAPHS = WORKLOADS / "graphs"
SCHEMA = WORKLOADS / "schema" / "taskgraph.schema.json"
DATA = ROOT / "data" / "workloads"
MODELS = DATA / "models"
SRC_CACHE = DATA / "src"

# Default release/deadline table, spec Section 6.1. These are reasonable
# defaults for a pick-from-conveyor task, NOT measured requirements.
DEFAULTS: dict[str, dict[str, Any]] = {
    "T1_ingest_wrist": dict(kind="periodic", period_us=33333, deadline_us=15000, criticality="high", jitter_us=500),
    "T1_ingest_overhead": dict(kind="periodic", period_us=33333, deadline_us=15000, criticality="high", jitter_us=500, offset_us=16000),
    "T2_detect": dict(kind="periodic", period_us=33333, deadline_us=33000, criticality="high", jitter_us=500, offset_us=2000),
    "T3_esdf": dict(kind="periodic", period_us=66667, deadline_us=66000, criticality="medium", jitter_us=500, offset_us=3000),
    "T4_vla": dict(kind="periodic", period_us=125000, deadline_us=100000, criticality="high", jitter_us=2000, offset_us=4000),
    "T5_plan": dict(kind="sporadic", period_us=40000, deadline_us=50000, criticality="high"),
    "T6_estimate": dict(kind="periodic", period_us=5000, deadline_us=5000, criticality="high", jitter_us=50),
    "T7_control": dict(kind="periodic", period_us=1000, deadline_us=1000, criticality="safety", jitter_us=10),
    "T8_safety": dict(kind="periodic", period_us=10000, deadline_us=10000, criticality="safety", jitter_us=100),
    "T10_record": dict(kind="periodic", period_us=33333, deadline_us=33333, criticality="low"),
    "T11_speech": dict(kind="sporadic", period_us=1000000, deadline_us=500000, criticality="medium"),
    "T12_arm2_control": dict(kind="periodic", period_us=1000, deadline_us=1000, criticality="safety", jitter_us=10, offset_us=500),
    "T13_map_save": dict(kind="periodic", period_us=5000000, deadline_us=5000000, criticality="low"),
    "T14_ota": dict(kind="periodic", period_us=100000, deadline_us=100000, criticality="low"),
    "T15_diag": dict(kind="periodic", period_us=100000, deadline_us=100000, criticality="low"),
}

QOS = {"safety": "safety", "high": "realtime", "medium": "realtime", "low": "best_effort"}

# Engine eligibility presets.
NPU_MAC = ["npu", "simt", "cpu"]
NPU_VEC = ["npu", "simt", "dsp", "cpu"]
NPU_MOVE = ["npu", "dma", "cpu"]
CPU = ["cpu"]
DSP = ["dsp", "cpu"]
SIMT = ["simt", "cpu"]
DMA = ["dma"]
RT = ["rt"]


def sha256_file(path: Path | str) -> str:
    """Return the hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def machine_string() -> str:
    """Describe the capture machine without needing privileged sysctl calls."""
    mac = platform.mac_ver()[0]
    os_s = f"macOS {mac}" if mac else platform.platform()
    return f"{os_s}, {platform.machine()} (Apple Silicon), Python {sys.version.split()[0]}"


def today() -> str:
    return _dt.date.today().isoformat()


def provenance(
    *,
    source: str,
    script: str,
    method: str,
    model_name: str | None = None,
    model_version: str | None = None,
    model_hash: str | None = None,
    notes: str = "",
    references: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a provenance block. `script` is a path relative to the repo root."""
    p: dict[str, Any] = {
        "source": source,
        "captured_by": script,
        "date": today(),
        "machine": machine_string(),
        "method": method,
    }
    script_path = ROOT / script
    if script_path.exists():
        p["script_sha256"] = sha256_file(script_path)
    if model_name:
        p["model_name"] = model_name
    if model_version:
        p["model_version"] = model_version
    if model_hash:
        p["model_hash"] = model_hash
    if notes:
        p["notes"] = notes
    refs = list(references)
    if refs:
        p["references"] = refs
    return p


def release_block(task_id: str, **override: Any) -> tuple[dict[str, Any], float, str, str]:
    """Return (release, deadline_us, criticality, qos_class) for a task from the defaults table."""
    d = dict(DEFAULTS[task_id])
    d.update(override)
    rel = {"kind": d["kind"], "period_us": d["period_us"]}
    for k in ("jitter_us", "offset_us"):
        if k in d:
            rel[k] = d[k]
    rel["on_overrun"] = "skip" if d["criticality"] in ("safety", "high") else "queue"
    return rel, d["deadline_us"], d["criticality"], QOS[d["criticality"]]


def make_graph(
    task_id: str,
    name: str,
    prov: dict[str, Any],
    ops: list[dict[str, Any]],
    *,
    messages: list[dict[str, Any]] | None = None,
    affinity: list[str] | None = None,
    footprint: dict[str, int] | None = None,
    params: dict[str, Any] | None = None,
    release_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a schema-conforming task graph and attach a totals summary."""
    rel, deadline, crit, qos = release_block(task_id if task_id in DEFAULTS else task_id.rsplit("_", 1)[0],
                                             **(release_override or {}))
    for op in ops:
        finalize_op(op)
    g: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": task_id,
        "name": name,
        "provenance": prov,
        "release": rel,
        "deadline_us": deadline,
        "criticality": crit,
        "qos_class": qos,
    }
    if affinity:
        g["affinity"] = affinity
    if footprint:
        g["footprint"] = {k: int(v) for k, v in footprint.items()}
    if params:
        g["params"] = params
    g["ops"] = ops
    if messages:
        g["messages"] = messages
    g["summary"] = totals(ops)
    return g


def finalize_op(op: dict[str, Any]) -> None:
    """Fill defaults and the arithmetic-intensity field on one op in place."""
    for k in ("macs", "flops", "bytes_in", "bytes_w", "bytes_out"):
        op.setdefault(k, 0)
        v = op[k]
        op[k] = int(round(v)) if float(v).is_integer() or v > 1e3 else float(v)
    if op["macs"] and op["flops"] < 2 * op["macs"]:
        op["flops"] = 2 * op["macs"]
    b = op["bytes_in"] + op["bytes_w"] + op["bytes_out"]
    op["arith_intensity"] = round(op["flops"] / b, 4) if b else 0.0


def totals(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-graph totals used by summary.py and validate.py."""
    t = {k: sum(o.get(k, 0) for o in ops) for k in ("macs", "flops", "bytes_in", "bytes_w", "bytes_out")}
    instr = {"scalar": 0, "vector": 0, "mem": 0}
    for o in ops:
        for k in instr:
            instr[k] += o.get("instr", {}).get(k, 0)
    t["instr"] = instr
    t["num_ops"] = len(ops)
    t["num_layers"] = len({o.get("layer", o["op_id"]) for o in ops})
    b = t["bytes_in"] + t["bytes_w"] + t["bytes_out"]
    t["arith_intensity"] = round(t["flops"] / b, 4) if b else 0.0
    return t


def write_graph(g: dict[str, Any]) -> Path:
    """Write a graph to workloads/graphs/<task_id>.json (stable formatting)."""
    GRAPHS.mkdir(parents=True, exist_ok=True)
    path = GRAPHS / f"{g['task_id']}.json"
    with open(path, "w") as f:
        json.dump(g, f, indent=1)
        f.write("\n")
    return path


def chain(ops: list[dict[str, Any]], first_deps: list[str] | None = None) -> list[dict[str, Any]]:
    """Give every op without explicit deps a dependency on the previous op."""
    prev: str | None = None
    for i, op in enumerate(ops):
        if "depends_on" not in op:
            if prev is not None:
                op["depends_on"] = [prev]
            elif first_deps:
                op["depends_on"] = list(first_deps)
        prev = op["op_id"]
    return ops


# RV64 vector unit assumption from configs/soc/vx1.yaml: VLEN 256 bits, so 8 fp32 lanes.
FP32_LANES = 8


def cpu_instr_estimate(flops: float, bytes_touched: float, vector_fraction: float = 0.7,
                       scalar_overhead: float = 1.5) -> dict[str, int]:
    """Estimate an RV64GCV instruction mix for an analytically counted CPU kernel.

    vector part: one vector instruction per 8 fp32 lanes of work, FMA counted as one.
    scalar part: one instruction per flop plus loop/address overhead.
    memory: one vector load/store per 32 bytes for the vector part, 8 bytes otherwise.
    This is an estimate (labeled analytic_count), not a compiled count.
    """
    vf = max(0.0, min(1.0, vector_fraction))
    vec = vf * flops / FP32_LANES / 1.6
    scal = (1 - vf) * flops * scalar_overhead + 0.05 * vec
    mem = vf * bytes_touched / 32 + (1 - vf) * bytes_touched / 8
    return {"scalar": int(scal), "vector": int(vec), "mem": int(mem)}


def op(op_id: str, type_: str, eligible: list[str], **kw: Any) -> dict[str, Any]:
    """Small constructor so capture scripts read cleanly."""
    d: dict[str, Any] = {"op_id": op_id, "type": type_}
    d.update(kw)
    d["eligible"] = eligible
    return d


GEM5_TABLE = ROOT / "calib" / "tables" / "cpu_gem5.json"


def dynamic_instr(static: dict[str, Any], gem5_key: str) -> tuple[dict[str, int], int, str]:
    """Per-call instruction mix for a compiled kernel.

    If the calibration worker's gem5 table (calib/tables/cpu_gem5.json) has a measured
    instr_per_call for this kernel, use it: it is a dynamic count, while the static count
    in rv64_counts.json counts both sides of every branch. The scalar/vector/memory split
    keeps the static mix, scaled to the measured total. Otherwise fall back to the static count.
    Returns (instr, total, provenance note).
    """
    st_total = static["instr_total"]
    if GEM5_TABLE.exists():
        k = json.loads(GEM5_TABLE.read_text()).get("kernels", {}).get(gem5_key, {})
        m = next((v for v in k.values() if isinstance(v, dict) and v.get("instr_per_call", 0) > 0), None)
        if m:
            dyn = float(m["instr_per_call"])
            f = dyn / st_total
            instr = {c: int(round(v * f)) for c, v in static["instr"].items()}
            return instr, sum(instr.values()), (
                f"instr = gem5-measured dynamic count {dyn:.0f}/call (calib/tables/cpu_gem5.json, {gem5_key}; includes a "
                f"few harness instructions), split by the static mix; static count of the compiled code was {st_total} "
                "(both branch sides counted)")
    return dict(static["instr"]), st_total, f"instr = static count of the compiled code ({st_total}, both branch sides counted)"
