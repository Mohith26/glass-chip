"""End-to-end experiment runner: co-sim robot + glasschip_core + PowerManager + scenario events.

One call runs one (scenario, policy, seed) and returns a plain dict of results:

    from studies.runner import run
    r = run("S2", "P0", seed=0, duration_s=10)
    r["robot"]["grasp_success_rate"], r["tasks"]["T4_vla"]["miss_rate"], r["power"]["energy_per_robot_hour_wh"]

Arguments
    scenario_yaml   path, or a short id ("S2" -> configs/scenarios/S2_*.yaml, "dev_human" -> cosim/scenarios_dev)
    policy_yaml     path, or a short id ("P4" -> configs/policies/P4_*.yaml)
    fidelity_yaml   path or id ("interactive" -> configs/fidelity/interactive.yaml)
    soc_yaml        path (default configs/soc/vx1.yaml)
    seed            master seed (robot, chip and jitter)
    duration_s      override the scenario duration
    record_replay_dir  write a glasschip-viz replay there (tools/replay_writer.py)
    trace           with a replay: include chip trace events (filtered and binned to stay small)
    overrides       dict, all keys optional:
        soc: {...}            deep-merged into the SoC dict
        fidelity: {...}       deep-merged into the fidelity dict (e.g. {"npu": "L2", "params": {...}})
        policy: {...}         deep-merged into the policy dict (e.g. {"scheduler": "edf_mc"})
        power: {params: {...}, low_voltage_arith: {...}, vmin: {domain: v}, thermal_init: "steady"|"ambient"}
        scenario: {transport, executor, ambient_c, conveyor_speed_mps, object_spacing_m, events: [...]}
        qos: bool             NoC + DRAM priority arbitration by qos_class
        cache_partition: {safety: ways, realtime: ways, best_effort: ways}
        mapping: "static" | "eft"
        task_priority: {task_id: priority}
        graphs: {task_id: graph dict}  replaces the workloads/graphs file for that task
        params: {...}         sim.set_params after construction
        l2_tables: {...}      sim.set_l2_tables after construction
        keep_jobs: bool       result["jobs"] = every core completion dict
        tasks_only: [task ids]  run only these tasks (no scenario events); with chip_only, a detector
                              whose camera is absent is released on its own period
        replay_coalesce_us (20), replay_critpath_every (8): replay size controls
        cosim: {...}          CoSimConfig field overrides (rarely needed)
        chip_only: bool       no physics: periodic releases from the scenario, T2 chained to T1,
                              T5 released at its sporadic rate (fast, for design-space sweeps)
        replay_id, scenario_label, policy_label, index (bool, default True), default (bool)
    return_objects  if True, result["_objects"] = {"pm", "sim", "chip"} (not picklable)

Results keys: scenario, policy, seed, duration_s, robot, tasks, chains, attribution, power,
activity, misses, provenance, wall_s, wall_per_sim_s (and replay when written).

CLI:
    .venv/bin/python -m studies.runner --scenario S2 --policy P0 --replay
    .venv/bin/python -m studies.runner --scenario S1 --policy P4 --duration 5 --json out.json
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

from cosim.bridge import CoSim, CoSimConfig, _json_default  # noqa: E402
from cosim.core_chip import deep_merge, make_core_chip  # noqa: E402
from cosim.metrics import quantiles  # noqa: E402
from cosim.scenario import CAMERA_OF, DETECT_TASK, load_scenario  # noqa: E402

SCEN_DIR = ROOT / "configs" / "scenarios"
DEV_DIR = ROOT / "cosim" / "scenarios_dev"
POL_DIR = ROOT / "configs" / "policies"
FID_DIR = ROOT / "configs" / "fidelity"
GRAPH_DIR = ROOT / "workloads" / "graphs"
REPLAY_ROOT = ROOT / "viz" / "public" / "replays"
SCENE_SRC = ROOT / "data" / "cosim" / "scene"
SCENE_SITE = ROOT / "viz" / "public" / "scenes" / "cosim_conveyor"
PS_PER_S = 1_000_000_000_000
PS_PER_MS = 1_000_000_000
ATTR_KEYS = ("compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware")
SCENARIO_LABELS = {"S1": "Nominal pick", "S2": "Second camera", "S3": "Human enters", "S4": "Conveyor ramp",
                   "S5": "Hot day", "S6": "Fifteen at once", "S7": "Bandwidth hog", "S8": "Low-voltage sweep",
                   "S9": "Person enters during recording"}
POLICY_LABELS = {"P0": "P0 Nominal", "P1": "P1 Fixed priority", "P2": "P2 EDF", "P3": "P3 Race to idle",
                 "P4": "P4 Pace to deadline", "P5": "P5 Offline optimal", "P6": "P6 Surrogate guided"}


# ----------------------------------------------------------------------------- resolution
def _resolve(name: str | os.PathLike, dirs: list[Path], kind: str) -> Path:
    p = Path(name)
    if p.exists():
        return p.resolve()
    for d in dirs:
        for pat in (f"{name}_*.yaml", f"{name}.yaml"):
            m = sorted(d.glob(pat))
            if m:
                return m[0]
    raise FileNotFoundError(f"no {kind} config for {name!r}")


def resolve_scenario(name: str | os.PathLike) -> Path:
    return _resolve(name, [SCEN_DIR, DEV_DIR], "scenario")


def resolve_policy(name: str | os.PathLike) -> Path:
    return _resolve(name, [POL_DIR], "policy")


def resolve_fidelity(name: str | os.PathLike) -> Path:
    return _resolve(name, [FID_DIR], "fidelity")


def load_graphs(task_ids) -> dict[str, dict[str, Any]]:
    out = {}
    for t in task_ids:
        f = GRAPH_DIR / f"{t}.json"
        if f.exists():
            out[t] = json.loads(f.read_text())
    return out


_CORE_BUILD: dict[str, Any] | None = None


def core_build_id() -> dict[str, Any]:
    """Identify the glasschip_core binary used: the core's own build_id (sim.info()) plus a file hash."""
    global _CORE_BUILD
    if _CORE_BUILD is not None:
        return dict(_CORE_BUILD)
    import hashlib
    so = sorted((ROOT / "core" / "build").glob("glasschip_core*.so"))
    if not so:
        return {}
    b = so[0].read_bytes()
    out: dict[str, Any] = {"file": so[0].name, "sha256_12": hashlib.sha256(b).hexdigest()[:12],
                           "mtime": _dt.datetime.fromtimestamp(so[0].stat().st_mtime).isoformat(timespec="seconds")}
    try:
        from cosim.core_chip import _gc
        sim = _gc.Sim(yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text()),
                      yaml.safe_load((ROOT / "configs/fidelity/interactive.yaml").read_text()), 0)
        info = sim.info()
        out["build_id"] = info.get("build_id")
        out["version"] = info.get("version")
    except Exception:
        pass
    _CORE_BUILD = out
    return dict(out)


def _coalescing_sink(writer: Any, gap_us: int, xfer_bin_ms: int = 1):
    """Trace sink: the writer's encoder (exact per-ms LOD from every packet), then thinner records.

    Stored records are reduced for a static site: NoC transfers become one record per
    (1 ms bin, src, dst, task) with summed bytes and packet counts, and back-to-back ops of the
    same job on the same unit (gap under gap_us) merge into one busy span.
    """
    def sink(arr, names):
        if not hasattr(writer, "encode_trace"):
            writer.add_trace(arr, names)
            return
        enc = writer.encode_trace(arr, names, coalesce_gap_ps=gap_us * 1_000_000, lod=True)
        if enc["dram_q"] is not None:
            writer._dram_q.append(enc["dram_q"])
        if enc["freq"] is not None:
            writer._freq.append(enc["freq"])
            writer._freq_blocks = list(names["blocks"])
        writer._events.append(thin_records(enc["records"], gap_us * 1_000_000, xfer_bin_ms))
    return sink


def thin_records(rec: np.ndarray, op_gap_ps: int, xfer_bin_ms: int = 1) -> np.ndarray:
    """Merge replay event records (tools/replay_writer.EVENT_DTYPE) to cut their count."""
    if len(rec) == 0:
        return rec
    kind = rec["kind"]
    parts = [rec[(kind != 4) & (kind != 5)]]
    x = rec[kind == 5]
    if len(x):
        key = np.stack([x["t_ps"] // (PS_PER_MS * max(1, xfer_bin_ms)), x["src"].astype(np.int64), x["dst"].astype(np.int64),
                        x["task"].astype(np.int64)], axis=1)
        order = np.lexsort(key.T[::-1])
        x, key = x[order], key[order]
        new = np.ones(len(x), dtype=bool)
        new[1:] = np.any(key[1:] != key[:-1], axis=1)
        idx = np.flatnonzero(new)
        out = x[idx].copy()
        end = np.maximum.reduceat(x["t_ps"] + x["dur_ps"], idx)
        out["dur_ps"] = end - out["t_ps"]
        out["bytes"] = np.add.reduceat(x["bytes"].astype(np.float64), idx).astype(np.float32)
        out["value"] = np.add.reduceat(x["value"].astype(np.float64), idx).astype(np.float32)
        out["job"] = 0xFFFFFFFF
        out["op"] = 0xFFFFFFFF
        parts.append(out)
    o = rec[kind == 4]
    if len(o):
        order = np.lexsort((o["t_ps"], o["unit"], o["block"], o["job"], o["task"]))
        o = o[order]
        end = o["t_ps"] + o["dur_ps"]
        same = np.zeros(len(o), dtype=bool)
        same[1:] = ((o["task"][1:] == o["task"][:-1]) & (o["job"][1:] == o["job"][:-1]) &
                    (o["block"][1:] == o["block"][:-1]) & (o["unit"][1:] == o["unit"][:-1]) &
                    (o["t_ps"][1:] <= end[:-1] + op_gap_ps))
        idx = np.flatnonzero(~same)
        out = o[idx].copy()
        gend = np.maximum.reduceat(end, idx)
        out["dur_ps"] = gend - out["t_ps"]
        out["bytes"] = np.add.reduceat(o["bytes"].astype(np.float64), idx).astype(np.float32)
        parts.append(out)
    res = np.concatenate(parts)
    return res[np.argsort(res["t_ps"], kind="stable")]


def _thin_critpaths(comps: list[dict[str, Any]], every: int) -> list[dict[str, Any]]:
    """Keep critical paths for missed jobs and every Nth job per task (replay size)."""
    out = []
    seen: dict[str, int] = defaultdict(int)
    for c in comps:
        t = str(c["task_id"])
        seen[t] += 1
        if c.get("critical_path") and not c.get("missed") and (seen[t] - 1) % max(every, 1) != 0:
            c = dict(c)
            c["critical_path"] = None
        out.append(c)
    return out


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


# ----------------------------------------------------------------------------- chip wrapper
class InstrumentedChip:
    """Wraps a CoreChip: feeds completions to the power manager and collects per-task stats.

    Also drains, filters and bins the chip trace when a replay with trace is requested.
    """

    def __init__(self, chip: Any, pm: Any = None, keep_completions: bool = False, trace: bool = False,
                 max_missed_records: int = 20000, trace_sink: Any = None) -> None:
        self._chip = chip
        self._pm = pm
        self.keep = keep_completions
        self.completions: list[dict[str, Any]] = []
        self.resp: dict[str, list[int]] = defaultdict(list)
        self.missed: dict[str, int] = defaultdict(int)
        self.attr_sum: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.attr_sum_missed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.cp_cause_missed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.missed_records: list[dict[str, Any]] = []
        self.max_missed_records = max_missed_records
        self.trace = trace
        self.trace_sink = trace_sink          # callable(numpy_trace, names); e.g. ReplayWriter.add_trace
        self.trace_events = 0
        self._names = None
        self._last_drain = 0
        if trace and hasattr(chip, "trace_enable"):
            chip.trace_enable(True)

    @property
    def inner(self) -> Any:
        return self._chip

    def release_job(self, task_id: str, t_ps: int, tag: int) -> int:
        return self._chip.release_job(task_id, t_ps, tag)

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        out = self._chip.run_until(t_ps)
        if out:
            if self._pm is not None:
                self._pm.observe_completions(out)
            for c in out:
                self._stat(c)
            if self.keep:
                self.completions.extend(out)
        if self.trace and t_ps - self._last_drain >= 250 * PS_PER_MS:
            self.drain_trace()
            self._last_drain = t_ps
        return out

    def _stat(self, c: Mapping[str, Any]) -> None:
        t = str(c["task_id"])
        r = int(c.get("response_ps", int(c["finish_ps"]) - int(c["release_ps"])))
        self.resp[t].append(r)
        attr = c.get("attribution") or {}
        for k in ATTR_KEYS:
            v = attr.get(k)
            if v:
                self.attr_sum[t][k] += float(v)
        if c.get("missed"):
            self.missed[t] += 1
            for k in ATTR_KEYS:
                v = attr.get(k)
                if v:
                    self.attr_sum_missed[t][k] += float(v)
            for op in c.get("critical_path") or []:
                cause = op.get("wait_cause") or ""
                if cause and op.get("wait_ps"):
                    self.cp_cause_missed[t][cause] += float(op["wait_ps"])
                gcause = op.get("gap_cause") or ""
                if gcause and op.get("gap_ps"):
                    self.cp_cause_missed[t][gcause] += float(op["gap_ps"])
            if len(self.missed_records) < self.max_missed_records:
                dom = max(ATTR_KEYS, key=lambda k: float(attr.get(k, 0) or 0)) if attr else None
                waits = defaultdict(float)
                for op in c.get("critical_path") or []:
                    if op.get("wait_cause") and op.get("wait_ps"):
                        waits[op["wait_cause"]] += float(op["wait_ps"])
                self.missed_records.append({
                    "task_id": t, "job_id": int(c["job_id"]), "release_ps": int(c["release_ps"]),
                    "finish_ps": int(c["finish_ps"]), "deadline_ps": int(c["deadline_ps"]),
                    "response_ms": r / PS_PER_MS, "attribution_ms": {k: float(attr.get(k, 0) or 0) / PS_PER_MS for k in ATTR_KEYS},
                    "dominant_attribution": dom,
                    "critical_path_wait_ms": {k: v / PS_PER_MS for k, v in waits.items()},
                    "dominant_wait": max(waits, key=waits.get) if waits else None,
                })

    def drain_trace(self) -> None:
        if not hasattr(self._chip, "trace_drain"):
            return
        a = self._chip.trace_drain(True)
        if a is None or len(a) == 0:
            return
        if self._names is None:
            self._names = self._chip.trace_names()
        self.trace_events += len(a)
        if self.trace_sink is not None:
            self.trace_sink(a, self._names)

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._chip, item)


# ----------------------------------------------------------------------------- chip-only driver
def _chip_only_run(sc, chip, pm, event_hook, duration_s: float, rng_seed: int) -> dict[str, Any]:
    """Release jobs on the scenario schedule without physics (fast mode)."""
    from cosim.scenario import task_spec
    rng = np.random.default_rng(rng_seed)
    tasks = {k: copy.copy(v) for k, v in sc.tasks.items()}
    raw_tasks = {(t.get("graph") or t.get("task")): t for t in (sc.raw.get("tasks") or [])}
    t5_rate = float(((raw_tasks.get("T5_plan") or {}).get("overrides") or {}).get("sporadic_rate_hz", 2.0))
    from cosim.scenario import graph_release
    for cam, det in DETECT_TASK.items():   # a detector without its camera runs on its own period
        if det in tasks and f"T1_ingest_{cam}" not in tasks:
            per, off = graph_release(det)
            tasks[det].period_us, tasks[det].offset_us = int(per or 33333), int(off)
    nxt = {t: int(round(s.enabled_at_s * PS_PER_S)) + s.offset_us * 1_000_000
           for t, s in tasks.items() if s.period_us > 0}
    events = list(sc.events)
    n = int(round(duration_s * 1000))
    bursts: list[tuple[int, str]] = []
    next_t5 = 0
    tag = 0
    for k in range(n):
        t_ps = k * PS_PER_MS
        t = k / 1000
        while events and events[0].t_s <= t + 1e-9:
            e = events.pop(0)
            if e.action in ("enable_task", "disable_task"):
                tid = e.args.get("graph") or e.args.get("task")
                on = e.action == "enable_task"
                if tid not in tasks:
                    tasks[tid] = task_spec(tid, t)
                tasks[tid].enabled = on
                tasks[tid].enabled_at_s = min(tasks[tid].enabled_at_s, t)
                if on and tasks[tid].period_us > 0 and tid not in nxt:
                    nxt[tid] = t_ps
            if e.action == "human_enter" and isinstance(e.args.get("replan_burst"), dict):
                b = e.args["replan_burst"]
                nb, win = int(b.get("count", 0)), float(b.get("window_ms", 200.0))
                for kk in range(nb):
                    bursts.append((t_ps + int(round(kk * win / max(nb, 1))) * PS_PER_MS, str(b.get("task", "T5_plan"))))
                bursts.sort()
            if event_hook is not None:
                event_hook({"t_s": e.t_s, "action": e.action, "args": e.args}, chip)

        def enabled(tid):
            s = tasks.get(tid)
            return s is not None and s.enabled and t + 1e-9 >= s.enabled_at_s

        for c in chip.run_until(t_ps):
            if c["task_id"] in CAMERA_OF and f"T1_ingest_{CAMERA_OF[c['task_id']]}" in tasks:
                det = DETECT_TASK.get(CAMERA_OF[c["task_id"]])
                if det and enabled(det):
                    chip.release_job(det, t_ps, int(c.get("tag", 0)))
        for tid in list(nxt):
            if t_ps >= nxt[tid]:
                if enabled(tid):
                    tag += 1
                    chip.release_job(tid, t_ps, tag)
                per = tasks[tid].period_us * 1_000_000
                while nxt[tid] <= t_ps:
                    nxt[tid] += per
        while bursts and bursts[0][0] <= t_ps:
            tag += 1
            chip.release_job(bursts.pop(0)[1], t_ps, tag)
        if enabled("T5_plan") and t_ps >= next_t5:
            tag += 1
            chip.release_job("T5_plan", t_ps, tag)
            next_t5 = t_ps + int(rng.exponential(1.0 / t5_rate) * PS_PER_S) + 40 * PS_PER_MS
        pm.step(chip, t_ps)
    chip.run_until(n * PS_PER_MS)
    return {"duration_s": duration_s}


# ----------------------------------------------------------------------------- main entry
def run(scenario_yaml: str | os.PathLike, policy_yaml: str | os.PathLike | Mapping[str, Any] = "P0",
        fidelity_yaml: str | os.PathLike | Mapping[str, Any] = "configs/fidelity/interactive.yaml",
        soc_yaml: str | os.PathLike | Mapping[str, Any] = "configs/soc/vx1.yaml", seed: int = 0, duration_s: float | None = None,
        record_replay_dir: str | os.PathLike | None = None, trace: bool = False,
        overrides: Mapping[str, Any] | None = None, return_objects: bool = False) -> dict[str, Any]:
    """Run one experiment. See the module docstring for arguments and result keys."""
    from power.cosim_hooks import make_event_hook
    from power.manager import PowerManager
    from power.params import load_params
    from power.predictor import task_info_from_graphs

    ov = dict(overrides or {})
    t_wall0 = time.perf_counter()
    scen_path = resolve_scenario(scenario_yaml)
    pol_path = resolve_policy(policy_yaml) if not isinstance(policy_yaml, Mapping) else POL_DIR / "custom.yaml"
    if isinstance(fidelity_yaml, Mapping):
        fid_path, fid = ROOT / "configs" / "fidelity" / "custom.yaml", copy.deepcopy(dict(fidelity_yaml))
    else:
        fid_path = Path(fidelity_yaml) if Path(fidelity_yaml).exists() else resolve_fidelity(fidelity_yaml)
        fid = yaml.safe_load(fid_path.read_text())
    if isinstance(soc_yaml, Mapping):
        soc_path, soc = ROOT / "configs" / "soc" / "custom.yaml", copy.deepcopy(dict(soc_yaml))
    else:
        soc_path = Path(soc_yaml) if Path(soc_yaml).is_absolute() else ROOT / soc_yaml
        soc = yaml.safe_load(soc_path.read_text())

    sc = load_scenario(scen_path)
    sc.seed = int(seed)
    if duration_s is not None:
        sc.duration_s = float(duration_s)
    if ov.get("tasks_only"):
        from cosim.scenario import task_spec
        only = list(ov["tasks_only"])
        sc.tasks = {t: task_spec(t) for t in only}
        sc.events = []
    sov = dict(ov.get("scenario") or {})
    if "events" in sov:
        from cosim.scenario import ScenarioEvent
        sc.events = sorted([ScenarioEvent(float(e["t_s"]), e["action"], dict(e.get("args") or {}))
                            for e in sov["events"]], key=lambda e: e.t_s)
    if "conveyor_speed_mps" in sov:
        sc.conveyor_speed_mps = float(sov["conveyor_speed_mps"])
    if "object_spacing_m" in sov:
        sc.object_spacing_m = float(sov["object_spacing_m"])
    transport = sov.get("transport", sc.raw.get("transport", "copy"))
    executor = sov.get("executor", sc.raw.get("executor", "single_threaded"))
    ambient = float(sov.get("ambient_c", sc.raw.get("ambient_c", 25.0)))

    if ov.get("soc"):
        soc = deep_merge(soc, ov["soc"])
    if ov.get("fidelity"):
        fid = deep_merge(fid, ov["fidelity"])
    policy = copy.deepcopy(dict(policy_yaml)) if isinstance(policy_yaml, Mapping) else yaml.safe_load(pol_path.read_text())
    if ov.get("policy"):
        policy = deep_merge(policy, ov["policy"])

    # Task set: scenario tasks plus anything an event enables; bitrate copies for set_bitrate.
    task_ids = set(sc.tasks)
    bitrates: dict[str, set[float]] = defaultdict(set)
    for e in sc.events:
        tid = e.args.get("graph") or e.args.get("task")
        if e.action in ("enable_task", "disable_task") and tid:
            task_ids.add(tid)
        if e.action == "set_bitrate" and tid:
            bitrates[tid].add(float(e.args.get("bitrate_bps")))
    graphs = load_graphs(task_ids)
    graphs.update({k: dict(v) for k, v in (ov.get("graphs") or {}).items()})
    chip_core = make_core_chip(task_ids, soc=soc, fidelity=fid, seed=seed, transport=transport, executor=executor,
                               bitrates={k: sorted(v) for k, v in bitrates.items()},
                               graph_overrides=ov.get("graphs"))
    if ov.get("qos") is not None:
        chip_core.set_qos(bool(ov["qos"]))
    if ov.get("cache_partition") is not None:
        chip_core.set_cache_partition(ov["cache_partition"] or None)
    if ov.get("mapping"):
        chip_core.set_mapping(str(ov["mapping"]))
    if ov.get("params"):
        chip_core.set_params(dict(ov["params"]))
    if ov.get("l2_tables") and hasattr(chip_core, "set_l2_tables"):
        chip_core.set_l2_tables(dict(ov["l2_tables"]))
    for tid, pr in (ov.get("task_priority") or {}).items():
        chip_core.set_task_priority(tid, int(pr))

    pov = dict(ov.get("power") or {})
    params = load_params(overrides=pov.get("params")) if pov.get("params") else None
    task_info = task_info_from_graphs(graphs.values())
    pm = PowerManager(policy, soc, ambient_c=ambient, params=params, task_info=task_info,
                      thermal_init=pov.get("thermal_init", "steady"), low_voltage_arith=pov.get("low_voltage_arith"),
                      record_series=record_replay_dir is not None or bool(pov.get("record_series", False)))
    for d, v in (pov.get("vmin") or {}).items():
        pm.set_vmin(d, float(v))

    keep = record_replay_dir is not None or bool(ov.get("keep_jobs"))
    writer = None
    if record_replay_dir is not None:
        writer = _make_writer(Path(record_replay_dir), sc, scen_path, pol_path, policy, soc, ov, task_ids,
                              chip_core)
    chip = InstrumentedChip(chip_core, pm, keep_completions=keep, trace=bool(trace and writer is not None),
                            trace_sink=(_coalescing_sink(writer, int(ov.get("replay_coalesce_us", 50)),
                                                         int(ov.get("replay_xfer_bin_ms", 1)))
                                        if writer is not None else None))
    pm.apply_scheduler(chip)

    def chip_events(event: dict[str, Any], c: Any) -> None:
        if event.get("action") == "set_bitrate":
            a = event.get("args") or {}
            chip_core.set_bitrate(str(a.get("task")), float(a.get("bitrate_bps")))

    hook = make_event_hook(pm, chained=chip_events)
    robot: dict[str, Any] | None = None
    chains: dict[str, Any] = {}
    sim = None
    if ov.get("chip_only"):
        _chip_only_run(sc, chip, pm, hook, sc.duration_s, seed)
        dur = sc.duration_s
    else:
        ccfg = CoSimConfig(scenario=sc, record=keep)
        for k, v in (ov.get("cosim") or {}).items():
            setattr(ccfg, k, v)
        sim = CoSim(ccfg, chip=chip, power_manager=pm, event_hook=hook)
        m = sim.run()
        dur = m["duration_s"]
        robot = {k: v for k, v in m.items() if k not in ("tasks", "chain_latency_ms", "wall_s", "wall_per_sim_s")}
        chains = m["chain_latency_ms"]
    if chip.trace:
        chip.drain_trace()

    # Per-task stats.
    tasks: dict[str, Any] = {}
    skipped = dict(sim.skipped) if sim is not None else {}
    for t in sorted(set(chip.resp) | set(skipped)):
        r = np.asarray(chip.resp.get(t, []), dtype=np.float64) / PS_PER_MS
        n = len(r)
        q = quantiles(r)
        tasks[t] = {
            "completed": n, "missed": int(chip.missed.get(t, 0)), "skipped": int(skipped.get(t, 0)),
            "miss_rate": (chip.missed.get(t, 0) / n) if n else None,
            "response_ms": {k: q[k] for k in ("mean", "p50", "p95", "p99", "max")},
            "deadline_us": (graphs.get(t) or {}).get("deadline_us"),
            "criticality": (graphs.get(t) or {}).get("criticality"),
            "attribution_mean_ms": {k: v / n / PS_PER_MS for k, v in chip.attr_sum[t].items()} if n else {},
            "attribution_missed_mean_ms": ({k: v / chip.missed[t] / PS_PER_MS for k, v in chip.attr_sum_missed[t].items()}
                                           if chip.missed.get(t) else {}),
            "critical_path_wait_missed_ms": {k: v / PS_PER_MS for k, v in chip.cp_cause_missed[t].items()},
        }
    # Activity totals from the counters the power manager pulled every step.
    totals = getattr(chip_core, "totals", {})
    activity = {"dram_bytes": float(sum(v.get("dram_bytes", 0) for v in totals.values())),
                "dma_bytes": float(sum(v.get("dma_bytes", 0) for v in totals.values())),
                "flit_hops": float(sum(v.get("flit_hops", 0) for v in totals.values())),
                "utilization": {d: (v.get("busy_ps", 0) / (dur * PS_PER_S)) for d, v in totals.items()},
                "unit_utilization": {d: (v.get("unit_busy_ps", 0) / (dur * PS_PER_S * max(v.get("units", 1), 1)))
                                     for d, v in totals.items()},
                "by_domain": {d: dict(v) for d, v in totals.items()}}
    activity["dram_gbps"] = activity["dram_bytes"] / dur / 1e9 if dur else None
    psum = pm.summary()
    safety_tasks = [t for t, g in graphs.items() if g.get("criticality") == "safety"]
    misses = {"total": int(sum(chip.missed.values())),
              "safety": int(sum(chip.missed.get(t, 0) for t in safety_tasks)),
              "by_task": {t: int(v) for t, v in chip.missed.items()}}
    attribution = {t: {"dominant_missed": (max(v["attribution_missed_mean_ms"], key=v["attribution_missed_mean_ms"].get)
                                           if v["attribution_missed_mean_ms"] else None)}
                   for t, v in tasks.items()}
    wall = time.perf_counter() - t_wall0
    res: dict[str, Any] = {
        "scenario": sc.name, "scenario_file": str(scen_path.relative_to(ROOT)) if scen_path.is_relative_to(ROOT) else str(scen_path),
        "policy": policy.get("name", pol_path.stem), "seed": int(seed), "duration_s": dur,
        "chip_only": bool(ov.get("chip_only")),
        "robot": robot, "tasks": tasks, "chains": chains, "attribution": attribution,
        "missed_jobs": chip.missed_records, "power": psum, "activity": activity, "misses": misses,
        "wall_s": wall, "wall_per_sim_s": wall / dur if dur else None,
        "provenance": {
            "script": "studies/runner.py", "git_sha": git_sha(), "date": _dt.date.today().isoformat(),
            "core_build": core_build_id(),
            "scenario": str(scen_path.relative_to(ROOT)) if scen_path.is_relative_to(ROOT) else str(scen_path),
            "policy": str(pol_path.relative_to(ROOT)), "fidelity": str(fid_path.relative_to(ROOT)) if fid_path.is_relative_to(ROOT) else str(fid_path),
            "soc": str(soc_path.relative_to(ROOT)) if soc_path.is_relative_to(ROOT) else str(soc_path),
            "overrides": json.loads(json.dumps(ov, default=str)), "seed": int(seed),
            "transport": transport, "executor": executor, "ambient_c": ambient,
            "behavior_source": "scripted expert in hybrid mode; VLA compute modeled from its operator graph",
        },
    }
    if writer is not None and sim is not None:
        res["replay"] = _write_replay(writer, sim, chip, pm, sc, scen_path, pol_path, policy, Path(record_replay_dir),
                                      ov, res)
    if ov.get("keep_jobs"):
        res["jobs"] = chip.completions
    if ov.get("keep_chains") and sim is not None:
        res["chains_raw"] = {"human_to_slowdown": [c for c in sim.chains["human_to_slowdown"] if c.get("state") != "ok"],
                             "camera_to_grasp": sim.chains["camera_to_grasp"]}
    if return_objects:
        res["_objects"] = {"pm": pm, "sim": sim, "chip": chip}
    return res


def run_scenario(scenario: str | os.PathLike, soc: str | os.PathLike | Mapping[str, Any] = "configs/soc/vx1.yaml",
                 fidelity: str | os.PathLike | Mapping[str, Any] = "configs/fidelity/interactive.yaml",
                 duration_s: float | None = None, seed: int = 0, chip_only: bool = True,
                 policy: str | Mapping[str, Any] = "P0", l2_tables: Mapping[str, Any] | None = None,
                 params: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Calibration-friendly wrapper: returns {jobs, wall_s, sim_s, info, result}."""
    ov = dict(overrides or {})
    ov.update({"chip_only": chip_only, "keep_jobs": True})
    if l2_tables:
        ov["l2_tables"] = dict(l2_tables)
    if params:
        ov["params"] = dict(params)
    r = run(scenario, policy, fidelity, soc, seed=seed, duration_s=duration_s, overrides=ov, return_objects=True)
    objs = r.pop("_objects")
    jobs = r.pop("jobs")
    inner = objs["chip"].inner
    info = inner.sim.info() if hasattr(inner, "sim") and hasattr(inner.sim, "info") else {}
    return {"jobs": jobs, "wall_s": r["wall_s"], "sim_s": r["duration_s"], "info": info, "result": r}


# ----------------------------------------------------------------------------- replay
def _short(path: Path) -> str:
    return path.stem.split("_")[0]


def _labels(sc, scen_path: Path, pol_path: Path, policy: dict, ov: Mapping[str, Any]) -> tuple[str, str]:
    s_id, p_id = _short(scen_path), _short(pol_path)
    scen_label = ov.get("scenario_label") or SCENARIO_LABELS.get(s_id, sc.name)
    pol_label = ov.get("policy_label") or POLICY_LABELS.get(p_id, policy.get("name", p_id))
    return scen_label, pol_label


def _make_writer(out_dir: Path, sc, scen_path: Path, pol_path: Path, policy: dict, soc: dict,
                 ov: Mapping[str, Any], task_ids, chip_core) -> Any:
    from cosim.replay import task_table
    from tools.replay_writer import ReplayWriter, stage_scene

    if (SCENE_SRC / "scene.json").exists() and not (SCENE_SITE / "scene.json").exists():
        stage_scene(SCENE_SRC, SCENE_SITE)
    out_dir = out_dir.resolve()
    rel_scene = os.path.relpath(SCENE_SITE, out_dir)
    scen_label, pol_label = _labels(sc, scen_path, pol_path, policy, ov)
    tasks = task_table(set(task_ids))
    w = ReplayWriter(out_dir, name=f"{scen_label} / {pol_label}", soc=soc, tasks=tasks,
                     duration_ps=int(round(sc.duration_s * PS_PER_S)), scenario=scen_path.stem,
                     policy=policy.get("name", pol_path.stem), seed=sc.seed, synthetic=False, scene=rel_scene,
                     scenario_label=scen_label, policy_label=pol_label,
                     provenance={"script": "studies/runner.py"})
    # Bitrate copies of a task (S7) report under the base task's index.
    for base, m in getattr(chip_core, "_aliases", {}).items():
        for alias in m.values():
            if alias != base and base in w.task_index:
                w.task_index[alias] = w.task_index[base]
    return w


def _write_replay(w, sim, chip, pm, sc, scen_path: Path, pol_path: Path, policy: dict, out_dir: Path,
                  ov: Mapping[str, Any], res: dict) -> dict[str, Any]:
    from tools.replay_writer import update_index

    out_dir = out_dir.resolve()
    scen_label, pol_label = _labels(sc, scen_path, pol_path, policy, ov)
    p_id = _short(pol_path)
    w.duration_ps = int(round(sim.t * PS_PER_S))
    w.provenance = {"script": "studies/runner.py", "git_sha": res["provenance"]["git_sha"],
                    "scenario": res["provenance"]["scenario"], "policy": res["provenance"]["policy"],
                    "fidelity": res["provenance"]["fidelity"], "overrides": res["provenance"]["overrides"],
                    "date": res["provenance"]["date"], "core_build": res["provenance"]["core_build"],
                    "seed": sc.seed}
    payload = sim.replay_robot_payload()
    w.set_robot(payload["fps"], payload["body_names"], payload["poses"], payload["events"])
    w.add_jobs(_thin_critpaths([c for c in chip.completions if str(c["task_id"]) in w.task_index],
                               int(ov.get("replay_critpath_every", 8))))
    n_ev = chip.trace_events
    # Power / thermal series, decimated to 100 Hz.
    ser = pm.series_arrays()
    dec = 10
    t0 = int(round(float(ser["t_s"][0]) * PS_PER_S)) if len(ser["t_s"]) else 0
    for d, arr in ser["power_w"].items():
        w.add_series(f"power.{d}", t0_ps=t0, rate_hz=100.0, values=arr[::dec], unit="W", group="power")
    for d, arr in ser["volts"].items():
        w.add_series(f"volt.{d}", t0_ps=t0, rate_hz=100.0, values=arr[::dec], unit="V", group="voltage")
    for d, arr in (ser["freq_hz"].items() if not chip.trace else []):
        w.add_series(f"freq.{d}", t0_ps=t0, rate_hz=100.0, values=arr[::dec], unit="Hz", group="frequency")
    for d, arr in ser["temp_c"].items():
        w.add_series(f"temp.{d}", t0_ps=t0, rate_hz=100.0, values=arr[::dec], unit="C", group="thermal")
    for d, arr in ser["throttled"].items():
        w.add_series(f"throttle.{d}", t0_ps=t0, rate_hz=100.0, values=np.asarray(arr, dtype=np.float32)[::dec],
                     unit="", group="thermal")
    w.add_series("robot.speed_scale", t0_ps=0, rate_hz=sim.cfg.record_hz,
                 values=np.asarray(sim.frames_alpha, dtype=np.float32), unit="", group="robot",
                 label="Robot speed scale (safety)")
    w.set_extra("scenario_events", [{"t_s": e.t_s, "action": e.action,
                                     "label": _event_label(e.action, e.args)} for e in sc.events])
    rm = {k: v for k, v in res["robot"].items() if k != "zone_visits"}
    rm["chain_latency_ms"] = res["chains"]
    rm["zone_visits"] = res["robot"].get("zone_visits", [])
    w.set_extra("robot_metrics", json.loads(json.dumps(rm, default=_json_default)))
    w.set_extra("power_summary", json.loads(json.dumps(res["power"], default=_json_default)))
    w.set_extra("behavior_source", res["provenance"]["behavior_source"])
    w.set_extra("conveyor_speed_mps", sc.conveyor_speed_mps)
    lt = ROOT / "results" / "s14.json"
    if lt.exists():
        r14 = json.loads(lt.read_text())
        spd = f"{sc.conveyor_speed_mps:.1f}"
        if spd in r14.get("sweep", {}):
            pts = [r14["baseline"][spd]] + r14["sweep"][spd]["points"]
            w.set_extra("latency_tolerance", {"latency_ms": [p["latency_ms"] for p in pts],
                                              "success": [p["grasp_success_rate"] for p in pts],
                                              "conveyor_speed_mps": float(spd), "synthetic": False,
                                              "note": "results/s14.json, injected camera-to-grasp latency"})
    path = w.write()
    size = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    info = {"dir": str(path), "bytes": size, "events": n_ev, "jobs": len(chip.completions)}
    if ov.get("index", True) and out_dir.parent == REPLAY_ROOT.resolve():
        update_index(REPLAY_ROOT / "index.json", {
            "id": out_dir.name, "path": f"{out_dir.name}/manifest.json", "scenario": scen_path.stem,
            "scenario_label": scen_label, "policy": policy.get("name", p_id), "policy_label": pol_label,
            "seed": sc.seed, "synthetic": False, "default": bool(ov.get("default", False))})
    return info


def _event_label(action: str, a: Mapping[str, Any]) -> str:
    if action == "enable_task":
        return f"Enable {a.get('task') or a.get('graph')}"
    if action == "set_conveyor_speed":
        return f"Conveyor {a.get('speed_mps', a.get('mps'))} m/s"
    if action == "human_enter":
        return "Person leaves" if a.get("leave") else "Person enters"
    if action == "set_ambient":
        return f"Ambient {a.get('ambient_c')} C"
    if action == "set_bitrate":
        return f"Recording {float(a.get('bitrate_bps', 0)) / 1e9:.3g} Gbit/s"
    if action == "set_vmin":
        return f"{a.get('domain')} Vmin {a.get('v_min')} V"
    return action


# ----------------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description="Run one Glass Chip experiment.")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--policy", default="P0")
    ap.add_argument("--fidelity", default="configs/fidelity/interactive.yaml")
    ap.add_argument("--soc", default="configs/soc/vx1.yaml")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--replay", nargs="?", const="auto", help="write a replay (default viz/public/replays/<S>_<P>)")
    ap.add_argument("--trace", action="store_true", help="include chip trace events in the replay")
    ap.add_argument("--overrides", help="JSON string or file with overrides")
    ap.add_argument("--json", help="write the result dict to this file")
    a = ap.parse_args()
    ov = {}
    if a.overrides:
        ov = json.loads(Path(a.overrides).read_text()) if Path(a.overrides).exists() else json.loads(a.overrides)
    rdir = None
    if a.replay:
        rdir = (REPLAY_ROOT / f"{_short(resolve_scenario(a.scenario))}_{_short(resolve_policy(a.policy))}"
                if a.replay == "auto" else Path(a.replay))
    r = run(a.scenario, a.policy, a.fidelity, a.soc, seed=a.seed, duration_s=a.duration, record_replay_dir=rdir,
            trace=a.trace, overrides=ov)
    r.pop("missed_jobs", None)
    txt = json.dumps(r, indent=1, default=_json_default)
    if a.json:
        Path(a.json).write_text(txt)
    print(txt if len(txt) < 20000 else json.dumps({k: r[k] for k in ("robot", "misses", "wall_per_sim_s")},
                                                   indent=1, default=_json_default))


if __name__ == "__main__":
    main()
