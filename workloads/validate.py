"""Validate every task graph in workloads/graphs/ and every scenario in configs/scenarios/.

Checks:
- JSON Schema (workloads/schema/taskgraph.schema.json)
- provenance present with script, date, method, machine; the capture script exists
- unique op ids; every depends_on target exists; the dependency graph is acyclic
- compute ops are not empty: conv/matmul/attention need MACs > 0; elementwise/softmax/
  norm/pool/resize/reduce/dsp/simt kernels need FLOPs > 0; cpu/rt kernels need
  instructions or FLOPs > 0
- totals in `summary` match the ops
- middleware.json has the CONTRACTS.md fields
- scenarios reference existing graphs and use known event actions

Run: .venv/bin/python workloads/validate.py   (exit code 1 on any error)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
GRAPHS = ROOT / "workloads" / "graphs"
SCHEMA = ROOT / "workloads" / "schema" / "taskgraph.schema.json"
SCENARIOS = ROOT / "configs" / "scenarios"

MAC_TYPES = {"conv2d", "dwconv2d", "matmul", "attention"}
FLOP_TYPES = {"elementwise", "softmax", "norm", "pool", "resize", "reduce", "dsp_kernel", "simt_kernel"}
INSTR_TYPES = {"cpu_kernel", "control_law", "serialize"}
ACTIONS = {"enable_task", "disable_task", "set_conveyor_speed", "human_enter", "set_ambient", "set_bitrate", "set_vmin"}
EXPECTED = ["T1_ingest_wrist", "T1_ingest_overhead", "T2_detect", "T3_esdf", "T4_vla", "T5_plan", "T6_estimate",
            "T7_control", "T8_safety", "T10_record", "T11_speech", "T12_arm2_control", "T13_map_save", "T14_ota",
            "T15_diag"]


def find_cycle(ops: list[dict[str, Any]]) -> list[str] | None:
    """Return one dependency cycle as a list of op ids, or None if the graph is a DAG."""
    deps = {o["op_id"]: o.get("depends_on", []) for o in ops}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for d in deps.get(n, []):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if state.get(d) is None:
                c = visit(d)
                if c:
                    return c
        stack.pop()
        state[n] = 2
        return None

    sys.setrecursionlimit(max(10000, 4 * len(ops)))
    for n in deps:
        if state.get(n) is None:
            c = visit(n)
            if c:
                return c
    return None


def check_graph(g: dict[str, Any], validator: jsonschema.Draft202012Validator) -> list[str]:
    errs = [f"schema: {e.message} at {'/'.join(map(str, e.path))}" for e in validator.iter_errors(g)]
    if errs:
        return errs[:10]
    p = g["provenance"]
    for k in ("source", "captured_by", "date", "method", "machine"):
        if not p.get(k):
            errs.append(f"provenance.{k} missing")
    if p.get("captured_by") and not (ROOT / p["captured_by"]).exists():
        errs.append(f"provenance.captured_by {p['captured_by']} does not exist")
    ids = [o["op_id"] for o in g["ops"]]
    if len(ids) != len(set(ids)):
        errs.append("duplicate op_id")
    idset = set(ids)
    for o in g["ops"]:
        for d in o.get("depends_on", []):
            if d not in idset:
                errs.append(f"{o['op_id']}: unknown dependency {d}")
        t = o["type"]
        if t in MAC_TYPES and not o.get("macs", 0) > 0:
            errs.append(f"{o['op_id']}: {t} with zero MACs")
        if t in FLOP_TYPES and not o.get("flops", 0) > 0:
            errs.append(f"{o['op_id']}: {t} with zero FLOPs")
        if t in INSTR_TYPES and not (sum(o.get("instr", {}).values()) > 0 or o.get("flops", 0) > 0):
            errs.append(f"{o['op_id']}: {t} with no instructions or FLOPs")
        if o.get("macs", 0) and o.get("flops", 0) < 2 * o["macs"]:
            errs.append(f"{o['op_id']}: flops < 2*macs")
    cyc = find_cycle(g["ops"])
    if cyc:
        errs.append(f"dependency cycle: {' -> '.join(cyc[:6])}")
    if "summary" in g:
        for k in ("macs", "flops", "bytes_in", "bytes_w", "bytes_out"):
            tot = sum(o.get(k, 0) for o in g["ops"])
            if abs(tot - g["summary"][k]) > 1e-6 * max(1.0, tot):
                errs.append(f"summary.{k} {g['summary'][k]} != sum of ops {tot}")
    if g["release"]["kind"] == "periodic" and "period_us" not in g["release"]:
        errs.append("periodic release without period_us")
    return errs


def check_middleware(m: dict[str, Any]) -> list[str]:
    errs = []
    for mode in ("copy", "zero_copy"):
        for k in ("serialize_ns_per_kb", "deserialize_ns_per_kb", "fixed_ns_per_msg", "copies"):
            if k not in m.get(mode, {}):
                errs.append(f"middleware.{mode}.{k} missing")
    for k in ("single_threaded_dispatch_ns", "multi_threaded_dispatch_ns", "events_dispatch_ns"):
        if not m.get("executor", {}).get(k, 0) > 0:
            errs.append(f"middleware.executor.{k} missing or zero")
    if not m.get("provenance", {}).get("method"):
        errs.append("middleware.provenance missing")
    return errs


def check_scenario(s: dict[str, Any], graphs: set[str]) -> list[str]:
    errs = []
    for k in ("name", "description", "duration_s", "seed", "tasks", "events", "robot", "ambient_c", "transport", "executor"):
        if k not in s:
            errs.append(f"missing {k}")
    for t in s.get("tasks", []):
        if t["graph"] not in graphs:
            errs.append(f"unknown graph {t['graph']}")
    for e in s.get("events", []):
        if e["action"] not in ACTIONS:
            errs.append(f"unknown action {e['action']}")
        if not 0 <= e["t_s"] <= s.get("duration_s", 0):
            errs.append(f"event at {e['t_s']} s outside duration")
    if s.get("transport") not in ("copy", "zero_copy"):
        errs.append("transport must be copy or zero_copy")
    if s.get("executor") not in ("single_threaded", "multi_threaded", "events"):
        errs.append("bad executor")
    return errs


def validate_all(verbose: bool = True) -> int:
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    n_err = 0
    names = set()
    for p in sorted(GRAPHS.glob("T*.json")):
        g = json.loads(p.read_text())
        names.add(g["task_id"])
        errs = check_graph(g, validator)
        if g["task_id"] != p.stem:
            errs.append("file name does not match task_id")
        n_err += len(errs)
        if verbose:
            print(f"{'OK  ' if not errs else 'FAIL'} {p.name} ({len(g['ops'])} ops)")
            for e in errs:
                print(f"     {e}")
    for t in EXPECTED:
        if t not in names:
            print(f"FAIL missing graph {t}")
            n_err += 1
    mw = GRAPHS / "middleware.json"
    errs = check_middleware(json.loads(mw.read_text())) if mw.exists() else ["middleware.json missing"]
    n_err += len(errs)
    if verbose:
        print(f"{'OK  ' if not errs else 'FAIL'} middleware.json")
        for e in errs:
            print(f"     {e}")
    for p in sorted(SCENARIOS.glob("S*.yaml")):
        errs = check_scenario(yaml.safe_load(p.read_text()), names)
        n_err += len(errs)
        if verbose:
            print(f"{'OK  ' if not errs else 'FAIL'} {p.name}")
            for e in errs:
                print(f"     {e}")
    if verbose:
        print(f"{n_err} error(s)")
    return n_err


if __name__ == "__main__":
    sys.exit(1 if validate_all() else 0)
