"""Live mode for glasschip-viz: run the co-sim + glasschip_core + PowerManager and stream it.

The browser connects with `?live=ws://127.0.0.1:8770` and receives msgpack messages:

    {type: "hello", manifest, series_meta, commands}       once per connection
    {type: "batch", t_ps, events, jobs, critpaths, strings_add, series, robot, robot_events, stats}

`events` and `jobs` are byte strings of the same 48-byte and 80-byte records used by replay
files (viz/REPLAY_FORMAT.md), produced by `ReplayWriter.encode_trace` / `jobs_to_records`, so
the browser decodes live and recorded data with one code path. Commands arrive as JSON text:

    {"cmd": "scenario", "id": "second_camera" | "human" | "conveyor_fast" | "hot_day" |
                               "fifteen" | "record_4k" | "lower_voltage" | "human_4k" | "nominal"}
    {"cmd": "policy", "id": "P4_pace_to_deadline"}
    {"cmd": "vmin", "domain": "npu", "v": 0.55}
    {"cmd": "qos", "on": true}
    {"cmd": "pause"} | {"cmd": "resume"} | {"cmd": "max_rate", "x": 1.0}
    {"cmd": "restart", "policy": "P0_nominal"} | {"cmd": "shutdown"}   (shutdown: local dev only)
    {"cmd": "run_config", "soc_overrides": {"blocks.npu.tiles": 6}}     (DSE "run full model": restart with a modified SoC)

Everything runs at the interactive fidelity mix with the scripted-expert policy (hybrid mode).
The achieved simulated-seconds-per-wall-second is measured here and sent in every batch.

Usage:
    .venv/bin/python tools/live_server.py --port 8770
    .venv/bin/python tools/live_server.py --offline data/viz/live_test --duration 3   # same engine, writes a replay
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import msgpack
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools", ROOT / "core" / "build"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from replay_writer import ReplayWriter, update_index  # noqa: E402

PS_PER_S = 1_000_000_000_000
BASE_SCENARIO = ROOT / "configs" / "scenarios" / "S1_nominal.yaml"
# Tasks registered with the chip but disabled until a scenario button turns them on.
OPTIONAL_TASKS = ["T1_ingest_overhead", "T2_detect_overhead", "T10_record", "T10_record_4k", "T11_speech",
                  "T12_arm2_control", "T13_map_save", "T14_ota", "T15_diag"]
DOMAINS = ["cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io"]


class _Stop(Exception):
    pass


class _CaptureChip:
    """Chip proxy: forwards to the core, reports completions to the current PowerManager and keeps them."""

    def __init__(self, core: Any, engine: "LiveEngine") -> None:
        self._core = core
        self._engine = engine
        self.completed: list[dict[str, Any]] = []

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        out = self._core.run_until(t_ps)
        if out:
            self._engine.pm.observe_completions(out)
            self.completed.extend(out)
        return out

    def __getattr__(self, item: str) -> Any:
        return getattr(self._core, item)


class _Tap:
    """Stands in for the bridge's power_manager; the bridge calls step() every 1 ms physics step."""

    def __init__(self, engine: "LiveEngine") -> None:
        self.engine = engine

    def step(self, chip: Any, t_ps: int) -> None:
        self.engine.on_step(t_ps)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.engine.pm, item)


class LiveEngine:
    """One live co-simulation. Thread-safe command queue in, encoded batches out."""

    def __init__(self, scenario: Path = BASE_SCENARIO, policy: str = "P0_nominal", seed: int = 1,
                 fidelity: str = "interactive", batch_ms: int = 50, record_hz: int = 60,
                 duration_s: float = 3600.0, max_rate: float | None = None,
                 soc_overrides: dict[str, Any] | None = None) -> None:
        from cosim.bridge import CoSim, CoSimConfig
        from cosim.core_chip import make_core_chip
        from cosim.scenario import load_scenario, task_spec
        from power.cosim_hooks import make_event_hook
        from power.params import load_policy, load_soc
        from power.predictor import task_info_from_graphs
        from power.manager import PowerManager

        self._PowerManager = PowerManager
        self._load_policy = load_policy
        self.soc = load_soc()
        self.soc_overrides = dict(soc_overrides or {})
        soc_path = ROOT / "configs" / "soc" / "vx1.yaml"
        if self.soc_overrides:
            for path, v in self.soc_overrides.items():
                node = self.soc
                keys = str(path).split(".")
                for k in keys[:-1]:
                    node = node.setdefault(k, {})
                node[keys[-1]] = v
            soc_path = ROOT / "data" / "viz" / "_live_soc.yaml"
            soc_path.parent.mkdir(parents=True, exist_ok=True)
            soc_path.write_text(yaml.safe_dump(self.soc, sort_keys=False))
        sc = load_scenario(str(scenario))
        sc.seed = int(seed)
        sc.duration_s = float(duration_s)
        raw = sc.raw or {}
        graph_dir = ROOT / raw.get("graph_dir", "workloads/graphs")
        for t in OPTIONAL_TASKS:
            if t not in sc.tasks and (graph_dir / f"{t}.json").exists():
                spec = task_spec(t, 0.0)
                spec.enabled = False
                sc.tasks[t] = spec
        self.scenario = sc
        graphs = [json.loads((graph_dir / f"{t}.json").read_text()) for t in sc.tasks if (graph_dir / f"{t}.json").exists()]
        self.task_info = task_info_from_graphs(graphs)
        self.ambient = float(raw.get("ambient_c", 25.0))
        self.policy_id = policy
        self.pm = PowerManager(load_policy(policy), self.soc, ambient_c=self.ambient, task_info=self.task_info)
        fid = ROOT / "configs" / "fidelity" / f"{fidelity}.yaml"
        self.fidelity = str(fid.relative_to(ROOT))
        self.core = make_core_chip(sc.tasks.keys(), soc=soc_path, fidelity=fid, graph_dir=graph_dir, seed=int(seed),
                                   transport=raw.get("transport", "copy"), executor=raw.get("executor", "single_threaded"))
        self.chip = _CaptureChip(self.core, self)
        self.pm.apply_scheduler(self.chip)
        self.core.trace_enable(True)
        def event_hook(event: dict[str, Any], chip: Any) -> None:
            make_event_hook(self.pm)(event, chip)

        self.cosim = CoSim(CoSimConfig(scenario=sc, record=False), chip=self.chip, power_manager=_Tap(self),
                           event_hook=event_hook)
        self.body_names = list(self.cosim.sc.body_names())
        task_ids = sorted(set(sc.tasks) | {"T2_detect", "T5_plan"})
        self.tasks = [self._task_row(t, graph_dir) for t in task_ids]
        self.encoder = ReplayWriter(ROOT / "data" / "viz" / "_live_scratch", name="Live", soc=self.soc, tasks=self.tasks,
                                    duration_ps=10**9, scenario="live", policy=policy, seed=int(seed),
                                    synthetic=False, provenance={"script": "tools/live_server.py"})
        self.names: dict[str, Any] | None = None
        self.batch_ps = int(batch_ms) * 1_000_000_000
        self.frame_ps = PS_PER_S // int(record_hz)
        self.record_hz = int(record_hz)
        self.next_batch = self.batch_ps
        self.next_frame = 0
        self.frames: list[np.ndarray] = []
        self.pm_idx = 0
        self.robot_ev_idx = 0
        self.strings_sent = 0
        self.cmds: queue.Queue[dict[str, Any]] = queue.Queue()
        self.out: queue.Queue[bytes] = queue.Queue(maxsize=40)
        self.paused = False
        self.max_rate = max_rate
        self.stop_flag = False
        self.t_ps = 0
        self.wall0 = time.perf_counter()
        self.rate_hist: list[tuple[float, float]] = []
        self.applied: list[dict[str, Any]] = []
        self.thread: threading.Thread | None = None
        self.sink: Any = None  # offline: a ReplayWriter collecting everything
        from concurrent.futures import Future, ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="encode")
        self._pending: list[Future[None]] = []

    @staticmethod
    def _task_row(t: str, graph_dir: Path) -> dict[str, Any]:
        g: dict[str, Any] = {}
        f = graph_dir / f"{t}.json"
        if f.exists():
            g = json.loads(f.read_text())
        rel = g.get("release", {})
        return {"id": t, "name": g.get("name", t), "period_us": rel.get("period_us"),
                "deadline_us": g.get("deadline_us"), "criticality": g.get("criticality", "high")}

    # ------------------------------------------------------------------ protocol
    def hello(self) -> dict[str, Any]:
        m = self.encoder.base_manifest()
        m.update({
            "name": "Live co-simulation", "scenario": "live", "scenario_label": "Live", "policy": self.policy_id,
            "policy_label": self.policy_id.replace("_", " "), "live": True, "duration_ps": self.t_ps,
            "events": {"record_bytes": 48, "kinds": ["job_release", "job_start", "job_finish", "job_miss", "op",
                                                       "xfer", "dram_q", "freq", "preempt"],
                       "chunk_ps": 250_000_000_000, "chunks": [], "strings": list(self.encoder.strings), "count": 0},
            "jobs": {"file": "", "record_bytes": 80, "count": 0,
                     "attribution_keys": ["compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma",
                                          "preemption", "dvfs", "middleware"], "critpath_file": ""},
            "series": {"file": "", "items": []},
            "lod": {"file": "", "bin_ps": 1_000_000_000, "bins": 0, "units": self.encoder._units,
                    "links": [list(l) for l in self.encoder._links], "busy_offset": 0, "busy_bytes": 0, "link_offset": 0},
            "robot": {"fps": self.record_hz, "frames": 0, "bodies": self.body_names, "chunk_frames": 120,
                      "chunks": [], "events": [], "scene": "scenes/cosim_conveyor"},
            "results": {"source": "live run; whole-run summaries are not available until the run ends"},
            "behavior_source": "scripted expert in hybrid mode; VLA compute modeled from its operator graph",
            "fidelity": self.fidelity,
            "soc_overrides": self.soc_overrides,
        })
        self.strings_sent = len(self.encoder.strings)
        return {"type": "hello", "manifest": m, "applied": self.applied,
                "commands": ["second_camera", "human", "conveyor_fast", "hot_day", "fifteen", "record_4k",
                             "lower_voltage", "human_4k", "nominal"]}

    # ------------------------------------------------------------------ commands
    def _apply(self, c: dict[str, Any]) -> None:
        from cosim.scenario import ScenarioEvent
        t_s = self.t_ps / PS_PER_S
        ev: list[ScenarioEvent] = []
        cmd = c.get("cmd")
        if cmd == "scenario":
            sid = c.get("id")
            if sid == "second_camera":
                ev += [ScenarioEvent(t_s, "enable_task", {"task": "T1_ingest_overhead"}),
                       ScenarioEvent(t_s, "enable_task", {"task": "T2_detect_overhead"})]
            elif sid == "human_4k":
                self._apply({"cmd": "scenario", "id": "record_4k"})
                self._apply({"cmd": "scenario", "id": "human"})
                self.applied = self.applied[:-2]
            elif sid == "human":
                ev.append(ScenarioEvent(t_s, "human_enter", {
                    "from": "left", "speed_mps": 1.6, "closest_approach_m": 0.45,
                    "replan_burst": {"task": "T5_plan", "count": 4, "window_ms": 200},
                    "safety_escalation": {"task": "T8_safety", "period_us": 5000, "deadline_us": 5000},
                    "speed_scale_floor": 0.3}))
                ev.append(ScenarioEvent(t_s + 4.0, "human_enter", {"leave": True}))
            elif sid == "conveyor_fast":
                ev.append(ScenarioEvent(t_s, "set_conveyor_speed", {"mps": 0.4}))
            elif sid == "hot_day":
                ev.append(ScenarioEvent(t_s, "set_ambient", {"ambient_c": 50.0}))
            elif sid == "fifteen":
                for t in ("T1_ingest_overhead", "T2_detect_overhead", "T11_speech", "T12_arm2_control",
                          "T13_map_save", "T14_ota", "T15_diag"):
                    ev.append(ScenarioEvent(t_s, "enable_task", {"task": t}))
            elif sid == "record_4k":
                ev += [ScenarioEvent(t_s, "disable_task", {"task": "T10_record"}),
                       ScenarioEvent(t_s, "enable_task", {"task": "T10_record_4k"})]
            elif sid == "lower_voltage":
                for d in ("npu", "cpu_a", "cpu_b"):
                    ev.append(ScenarioEvent(t_s, "set_vmin", {"domain": d, "v_min": float(self.soc["power_domains"][d]["v_min"])}))
            elif sid == "nominal":
                for t in OPTIONAL_TASKS:
                    if t != "T10_record":
                        ev.append(ScenarioEvent(t_s, "disable_task", {"task": t}))
                ev.append(ScenarioEvent(t_s, "enable_task", {"task": "T10_record"}))
                ev.append(ScenarioEvent(t_s, "set_conveyor_speed", {"mps": 0.2}))
                ev.append(ScenarioEvent(t_s, "set_ambient", {"ambient_c": self.ambient}))
        elif cmd == "vmin":
            ev.append(ScenarioEvent(t_s, "set_vmin", {"domain": str(c["domain"]), "v_min": float(c["v"])}))
        elif cmd == "policy":
            self._switch_policy(str(c["id"]))
        elif cmd == "qos":
            on = bool(c.get("on"))
            self.core.set_qos(on)
        elif cmd == "pause":
            self.paused = True
        elif cmd == "resume":
            self.paused = False
        elif cmd == "max_rate":
            x = c.get("x")
            self.max_rate = float(x) if x else None
        if ev:
            todo = self.cosim.events_todo
            todo.extend(ev)
            todo.sort(key=lambda e: e.t_s)
        self.applied.append({"t_ps": self.t_ps, **c})

    def _switch_policy(self, pid: str) -> None:
        """Swap the PowerManager, carrying the thermal state so temperatures stay continuous."""
        old = self.pm
        new = self._PowerManager(self._load_policy(pid), self.soc, ambient_c=self.ambient, task_info=self.task_info)
        new.thermal = old.thermal
        new.throttle = old.throttle
        self.pm = new
        self.pm_idx = 0
        new.apply_scheduler(self.chip)
        self.policy_id = pid

    # ------------------------------------------------------------------ the 1 ms hook
    def on_step(self, t_ps: int) -> None:
        if self.stop_flag:
            raise _Stop()
        while not self.cmds.empty():
            self._apply(self.cmds.get_nowait())
        if self.paused:
            self.flush()
        while self.paused and not self.stop_flag:
            time.sleep(0.05)
            while not self.cmds.empty():
                self._apply(self.cmds.get_nowait())
        self.t_ps = int(t_ps)
        self.pm.step(self.chip, t_ps)
        if t_ps >= self.next_frame:
            d = self.cosim.sc.data
            self.frames.append(np.concatenate([d.xpos.astype(np.float32), d.xquat.astype(np.float32)], axis=1))
            self.next_frame += self.frame_ps
        if t_ps >= self.next_batch:
            self._emit(t_ps)
            self.next_batch += self.batch_ps
            if self.max_rate:
                ahead = t_ps / PS_PER_S / self.max_rate - (time.perf_counter() - self.wall0)
                if ahead > 0:
                    time.sleep(ahead)

    def _emit(self, t_ps: int) -> None:
        """Grab this batch's raw data on the sim thread, encode it on the encoder thread.

        glasschip_core releases the GIL inside run_until, so encoding the previous batch overlaps
        with simulating the next one. A single encoder thread keeps batches (and the string table)
        in order.
        """
        if self.names is None:
            self.names = self.core.trace_names()
        tr = self.core.trace_drain(as_numpy=True)
        comps = self.chip.completed
        self.chip.completed = []
        series = self._pm_series(t_ps)
        frames = np.stack(self.frames) if self.frames else np.zeros((0, len(self.body_names), 7), np.float32)
        self.frames = []
        rev = list(self.cosim.robot_events[self.robot_ev_idx:])
        self.robot_ev_idx = len(self.cosim.robot_events)
        wall = time.perf_counter() - self.wall0
        self.rate_hist.append((wall, t_ps / PS_PER_S))
        while len(self.rate_hist) > 2 and self.rate_hist[-1][0] - self.rate_hist[0][0] > 3.0:
            self.rate_hist.pop(0)
        (w0, s0), (w1, s1) = self.rate_hist[0], self.rate_hist[-1]
        rate = (s1 - s0) / (w1 - w0) if w1 > w0 else 0.0
        stats = {"sim_s": t_ps / PS_PER_S, "wall_s": wall, "rate": rate, "policy": self.policy_id,
                 "paused": self.paused, "fidelity": self.fidelity}
        while len(self._pending) > 3:
            self._pending.pop(0).result()
        self._pending.append(self._pool.submit(self._encode, t_ps, tr, comps, series, frames, rev, stats))

    def _encode(self, t_ps: int, tr: np.ndarray, comps: list[dict[str, Any]], series: dict[str, Any],
                frames: np.ndarray, rev: list[dict[str, Any]], stats: dict[str, Any]) -> None:
        enc = self.encoder.encode_trace(tr, self.names, lod=self.sink is not None)
        jrec, cps = self.encoder.jobs_to_records(comps)
        if enc["dram_q"] is not None:
            series.update(self._dram_series(enc["dram_q"], t_ps))
        strings_add = self.encoder.strings[self.strings_sent:]
        self.strings_sent = len(self.encoder.strings)
        stats = dict(stats, raw_xfers=enc["raw_xfers"], stored_xfers=enc["stored_xfers"])
        if self.sink is not None:
            self.sink.collect(enc, comps, series, frames, rev)
            return
        msg = {
            "type": "batch", "t_ps": t_ps, "events": enc["records"].tobytes(), "jobs": jrec.tobytes(),
            "critpaths": {str(k): v for k, v in cps.items()}, "strings_add": strings_add, "series": series,
            "robot": frames.astype("<f4").tobytes(), "robot_events": rev, "stats": stats,
        }
        data = msgpack.packb(msg, use_bin_type=True, default=_np_default)
        while not self.stop_flag:
            try:
                self.out.put(data, timeout=0.2)
                break
            except queue.Full:
                continue

    def flush(self) -> None:
        """Wait for every submitted batch to finish encoding."""
        for f in self._pending:
            f.result()
        self._pending = []

    def _pm_series(self, t_ps: int) -> dict[str, Any]:
        s = self.pm.series
        n = len(s["t_s"])
        i0 = self.pm_idx
        self.pm_idx = n
        if n <= i0:
            return {}
        t0 = int(round(s["t_s"][i0] * PS_PER_S))
        out: dict[str, Any] = {}

        def put(name: str, vals: Any, unit: str, group: str) -> None:
            out[name] = {"t0_ps": t0, "rate_hz": 1000.0, "unit": unit, "group": group,
                         "values": np.asarray(vals[i0:n], dtype="<f4").tobytes()}

        for d in DOMAINS:
            if d in s["power_w"]:
                put(f"power.{d}", s["power_w"][d], "W", "power")
                put(f"volt.{d}", s["volts"][d], "V", "voltage")
                put(f"freq.{d}", s["freq_hz"][d], "Hz", "frequency")
            if d in s["temp_c"]:
                put(f"temp.{d}", s["temp_c"][d], "C", "temperature")
            if d in s["throttled"]:
                put(f"throttle.{d}", np.asarray(s["throttled"][d], dtype=np.float32), "", "throttle")
        return out

    def _dram_series(self, rows: np.ndarray, t_ps: int) -> dict[str, Any]:
        """Peak queue depth per ms over this batch for each controller channel."""
        out: dict[str, Any] = {}
        b0 = (t_ps - self.batch_ps) // 1_000_000_000
        nb = self.batch_ps // 1_000_000_000
        for u in np.unique(rows[:, 1]).astype(int):
            r = rows[rows[:, 1] == u]
            b = np.clip((r[:, 0] // 1_000_000_000).astype(np.int64) - b0, 0, nb - 1)
            peak = np.zeros(nb)
            np.maximum.at(peak, b, r[:, 2])
            out[f"dram_q.{u}"] = {"t0_ps": int(b0 * 1_000_000_000), "rate_hz": 1000.0, "unit": "req", "group": "dram",
                                  "values": peak.astype("<f4").tobytes()}
        return out

    # ------------------------------------------------------------------ run
    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.wall0 = time.perf_counter()
        try:
            self.cosim.run()
        except _Stop:
            pass

    def stop(self) -> None:
        self.stop_flag = True
        if self.thread is not None:
            self.thread.join(timeout=5)


def _np_default(o: Any) -> Any:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


class _OfflineSink:
    """Collects the engine's batches into a ReplayWriter (same encoding path as live)."""

    def __init__(self, w: ReplayWriter) -> None:
        self.w = w
        self.series: dict[str, list[np.ndarray]] = {}
        self.meta: dict[str, tuple[int, str, str]] = {}
        self.frames: list[np.ndarray] = []
        self.robot_events: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []

    def collect(self, enc: dict[str, Any], comps: list[dict[str, Any]], series: dict[str, Any], frames: np.ndarray,
                rev: list[dict[str, Any]]) -> None:
        self.w._events.append(enc["records"])
        if enc["dram_q"] is not None:
            self.w._dram_q.append(enc["dram_q"])
        self.jobs.extend(comps)
        for k, v in series.items():
            if k.startswith("dram_q."):
                continue
            self.series.setdefault(k, []).append(np.frombuffer(v["values"], dtype="<f4"))
            self.meta.setdefault(k, (v["t0_ps"], v["unit"], v["group"]))
        if len(frames):
            self.frames.append(frames)
        self.robot_events.extend(rev)


def run_offline(out: Path, duration_s: float, policy: str, scenario: Path, commands: list[dict[str, Any]],
                index: Path | None, fidelity: str = "interactive") -> None:
    """Run the live engine headless for duration_s and write a replay (for testing and quick looks)."""
    eng = LiveEngine(scenario=scenario, policy=policy, duration_s=duration_s, fidelity=fidelity)
    w = ReplayWriter(out, name=f"Live engine / {policy}", soc=eng.soc, tasks=eng.tasks,
                     duration_ps=int(duration_s * PS_PER_S), scenario=eng.scenario.name, policy=policy,
                     seed=eng.scenario.seed, synthetic=False,
                     provenance={"script": "tools/live_server.py --offline", "fidelity": eng.fidelity,
                                 "commands": commands}, scene="../../scenes/cosim_conveyor")
    eng.encoder = w  # share the string table and LOD accumulators
    sink = _OfflineSink(w)
    eng.sink = sink
    pending = sorted(commands, key=lambda c: c.get("t_s", 0))
    orig = eng.on_step

    def on_step(t_ps: int) -> None:
        while pending and pending[0].get("t_s", 0) * PS_PER_S <= t_ps:
            eng.cmds.put(pending.pop(0))
        orig(t_ps)

    eng.on_step = on_step  # type: ignore[method-assign]
    t0 = time.perf_counter()
    eng.cosim.run()
    wall = time.perf_counter() - t0
    # final partial batch
    eng._emit(eng.t_ps)
    eng.flush()
    w.add_jobs(sink.jobs)
    for k, chunks in sink.series.items():
        t0p, unit, group = sink.meta[k]
        w.add_series(k, t0_ps=t0p, rate_hz=1000.0, values=np.concatenate(chunks), unit=unit, group=group)
    if sink.frames:
        w.set_robot(eng.record_hz, eng.body_names, np.concatenate(sink.frames), sink.robot_events)
    w.set_extra("behavior_source", "scripted expert in hybrid mode; VLA compute modeled from its operator graph")
    w.set_extra("live_engine", {"wall_s": wall, "sim_s": duration_s, "rate": duration_s / wall, "fidelity": eng.fidelity})
    w.write()
    print(json.dumps({"out": str(out), "wall_s": round(wall, 2), "rate": round(duration_s / wall, 3), **w.stats}))
    if index is not None:
        update_index(index, {"id": out.name, "path": f"{out.name}/manifest.json", "scenario": eng.scenario.name,
                             "scenario_label": "Live engine", "policy": policy, "policy_label": policy, "seed": eng.scenario.seed,
                             "synthetic": False})


async def serve(host: str, port: int, policy: str, scenario: Path, fidelity: str = "interactive",
                allow_shutdown: bool = True) -> None:
    import websockets

    state: dict[str, Any] = {"engine": None, "clients": set()}

    def new_engine(pol: str, soc_overrides: dict[str, Any] | None = None) -> LiveEngine:
        e = LiveEngine(scenario=scenario, policy=pol, fidelity=fidelity, soc_overrides=soc_overrides)
        e.start()
        return e

    state["engine"] = new_engine(policy)

    async def pump() -> None:
        loop = asyncio.get_running_loop()
        while True:
            eng: LiveEngine = state["engine"]
            try:
                data = await loop.run_in_executor(None, eng.out.get, True, 0.5)
            except queue.Empty:
                continue
            dead = []
            for ws in list(state["clients"]):
                try:
                    await ws.send(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                state["clients"].discard(ws)

    async def handler(ws: Any) -> None:
        eng: LiveEngine = state["engine"]
        await ws.send(msgpack.packb(eng.hello(), use_bin_type=True, default=_np_default))
        state["clients"].add(ws)
        try:
            async for msg in ws:
                try:
                    c = json.loads(msg)
                except Exception:
                    continue
                if c.get("cmd") == "shutdown" and allow_shutdown:
                    import os
                    state["engine"].stop()
                    os._exit(0)
                if c.get("cmd") in ("restart", "run_config"):
                    old = state["engine"]
                    old.stop()
                    state["engine"] = new_engine(str(c.get("policy", old.policy_id)), c.get("soc_overrides") or None)
                    for w in list(state["clients"]):
                        await w.send(msgpack.packb(state["engine"].hello(), use_bin_type=True, default=_np_default))
                else:
                    state["engine"].cmds.put(c)
        finally:
            state["clients"].discard(ws)

    async with websockets.serve(handler, host, port, max_size=None, compression=None):
        print(f"glasschip live server on ws://{host}:{port}  (open the viewer with ?live=ws://{host}:{port})", flush=True)
        await pump()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--policy", default="P0_nominal")
    ap.add_argument("--scenario", default=str(BASE_SCENARIO))
    ap.add_argument("--fidelity", default="interactive", help="fidelity mix in configs/fidelity (interactive, analytic, ...)")
    ap.add_argument("--offline", help="write a replay directory instead of serving")
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--commands", default="[]", help='offline: JSON list like [{"t_s": 1, "cmd": "scenario", "id": "second_camera"}]')
    ap.add_argument("--index", help="offline: also add the replay to this index.json")
    a = ap.parse_args()
    if a.offline:
        run_offline(Path(a.offline), a.duration, a.policy, Path(a.scenario), json.loads(a.commands),
                    Path(a.index) if a.index else None, a.fidelity)
        return
    asyncio.run(serve(a.host, a.port, a.policy, Path(a.scenario), a.fidelity))


if __name__ == "__main__":
    main()
