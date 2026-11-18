"""Scenario configuration for the co-sim (CONTRACTS.md scenario YAML, plus co-sim defaults).

A scenario YAML looks like:

    name: S3_human_enters
    duration_s: 20
    seed: 3
    tasks: [{graph: T2_detect, enabled_at_s: 0, overrides: {period_us: 33333}}, ...]
    events: [{t_s: 6.0, action: human_enter, args: {...}}, ...]
    robot: {conveyor_speed_mps: 0.2, object_spacing_m: 0.25, human: false}

Robot-stack tasks (T4, T5, T6, T7, T8) always run because they are the robot. Camera ingest
tasks (T1_ingest_*) run when listed. Detection follows the workloads naming: T2_detect runs
on wrist frames and T2_detect_overhead on overhead frames; each is released when its
camera's T1 job completes (chained), and only while it is enabled. Release periods and phase
offsets come from workloads/graphs/<task>.json when present. An optional per-scenario
`cosim:` block overrides co-sim parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PS_PER_US = 1_000_000

# Default release periods (us). T2 is chained to T1 and T5 is triggered by new goals.
DEFAULT_PERIOD_US: dict[str, int] = {
    "T1_ingest_wrist": 33333, "T1_ingest_overhead": 33333, "T3_esdf": 66667, "T4_vla": 125000,
    "T6_estimate": 5000, "T7_control": 1000, "T8_safety": 10000, "T10_record": 33333,
    "T11_speech": 200000, "T12_arm2_control": 1000, "T13_map_save": 1000000, "T14_ota": 100000,
    "T15_diag": 500000,
}
CHAINED = {"T2_detect", "T2_detect_overhead", "T5_plan"}
ROBOT_TASKS = ["T4_vla", "T5_plan", "T6_estimate", "T7_control", "T8_safety"]
CAMERA_OF = {"T1_ingest_wrist": "wrist", "T1_ingest_overhead": "overhead"}
DETECT_TASK = {"wrist": "T2_detect", "overhead": "T2_detect_overhead"}
GRAPH_DIR = Path(__file__).resolve().parents[1] / "workloads" / "graphs"


def graph_release(task: str, graph_dir: Path = GRAPH_DIR) -> tuple[int | None, int]:
    """(period_us, offset_us) from the task graph, or (None, 0) if there is no graph."""
    f = graph_dir / f"{task}.json"
    if not f.exists():
        return None, 0
    try:
        import json
        rel = json.loads(f.read_text()).get("release", {})
    except Exception:
        return None, 0
    return (int(rel["period_us"]) if "period_us" in rel else None), int(rel.get("offset_us", 0) or 0)


@dataclass
class TaskSpec:
    task_id: str
    period_us: int            # 0 for chained / event tasks
    enabled_at_s: float = 0.0
    enabled: bool = True
    offset_us: int = 0        # release phase


def task_spec(task: str, enabled_at_s: float = 0.0, period_override: int | None = None) -> TaskSpec:
    """TaskSpec with the period/offset from the task graph (defaults if no graph)."""
    if task in CHAINED:
        return TaskSpec(task, 0, enabled_at_s)
    per, off = graph_release(task)
    if per is None:
        per = DEFAULT_PERIOD_US.get(task, 100000)
    if period_override is not None:
        per = int(period_override)
    return TaskSpec(task, per, enabled_at_s, True, off)


@dataclass
class ScenarioEvent:
    t_s: float
    action: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str = "dev"
    description: str = ""
    duration_s: float = 20.0
    seed: int = 0
    tasks: dict[str, TaskSpec] = field(default_factory=dict)
    events: list[ScenarioEvent] = field(default_factory=list)
    conveyor_speed_mps: float = 0.2
    object_spacing_m: float = 0.30
    human: bool = False
    cosim: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    path: str | None = None

def default_tasks(detect: tuple[str, ...] = ("overhead",)) -> dict[str, TaskSpec]:
    """Robot stack plus camera ingest and detection for the given cameras.

    The overhead camera always runs T1 because the safety monitor uses its depth stream.
    """
    ids = ["T1_ingest_overhead", "T3_esdf", *ROBOT_TASKS]
    for cam in detect:
        ids += [f"T1_ingest_{cam}", DETECT_TASK[cam]]
    return {t: task_spec(t) for t in dict.fromkeys(ids)}


def make_scenario(detect: tuple[str, ...] = ("overhead",), **kw: Any) -> Scenario:
    """Programmatic scenario (default: overhead-camera detection, no extra load)."""
    sc = Scenario(**kw)
    if not sc.tasks:
        sc.tasks = default_tasks(detect)
    for t in ROBOT_TASKS:
        sc.tasks.setdefault(t, task_spec(t))
    return sc


def load_scenario(path: str | Path) -> Scenario:
    """Parse a scenario YAML (CONTRACTS.md format)."""
    raw = yaml.safe_load(Path(path).read_text())
    tasks: dict[str, TaskSpec] = {}
    for t in raw.get("tasks", []) or []:
        tid = t.get("graph") or t.get("task") or t.get("id")
        ov = t.get("overrides", {}) or {}
        tasks[tid] = task_spec(tid, float(t.get("enabled_at_s", 0.0) or 0.0), ov.get("period_us"))
    if "T1_ingest_overhead" not in tasks:
        tasks["T1_ingest_overhead"] = task_spec("T1_ingest_overhead")
    if not any(t in tasks for t in DETECT_TASK.values()):
        tasks["T2_detect_overhead"] = task_spec("T2_detect_overhead")
    for t in ROBOT_TASKS:
        tasks.setdefault(t, task_spec(t))
    robot = raw.get("robot", {}) or {}
    events = [ScenarioEvent(float(e["t_s"]), str(e["action"]), dict(e.get("args", {}) or {}))
              for e in raw.get("events", []) or []]
    events.sort(key=lambda e: e.t_s)
    return Scenario(
        name=str(raw.get("name", Path(path).stem)), description=str(raw.get("description", "")),
        duration_s=float(raw.get("duration_s", 20.0)), seed=int(raw.get("seed", 0)), tasks=tasks, events=events,
        conveyor_speed_mps=float(robot.get("conveyor_speed_mps", 0.2)),
        object_spacing_m=float(robot.get("object_spacing_m", 0.30)), human=bool(robot.get("human", False)),
        cosim=dict(raw.get("cosim", {}) or {}), raw=raw, path=str(path))
