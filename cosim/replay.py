"""Write a co-sim run as a glasschip-viz replay using tools/replay_writer.py.

The viz worker owns the on-disk format (viz/REPLAY_FORMAT.md). This module only gathers the
co-sim's plain data (body poses, robot events, chip job records, speed scale series) and
hands it to ReplayWriter.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import yaml

from cosim.bridge import CoSim

ROOT = Path(__file__).resolve().parents[1]
GRAPHS = ROOT / "workloads" / "graphs"


def task_table(task_ids: Iterable[str]) -> list[dict[str, Any]]:
    """ReplayWriter task dicts {id, name, period_us, deadline_us, criticality} from the graphs."""
    out = []
    for tid in sorted(set(task_ids)):
        g: dict[str, Any] = {}
        f = GRAPHS / f"{tid}.json"
        if f.exists():
            g = json.loads(f.read_text())
        rel = g.get("release", {})
        out.append({"id": tid, "name": g.get("name", tid), "period_us": rel.get("period_us"),
                    "deadline_us": g.get("deadline_us"), "criticality": g.get("criticality", "high")})
    return out


def write_replay(sim: CoSim, out_dir: str | Path, *, name: str, policy: str = "P0_nominal",
                 soc_path: str | Path = ROOT / "configs" / "soc" / "vx1.yaml",
                 trace_events: Iterable[Mapping[str, Any]] | np.ndarray | None = None,
                 jobs: Iterable[Mapping[str, Any]] | None = None, synthetic: bool = False,
                 scene: str | None = None, provenance: Mapping[str, Any] | None = None) -> Path:
    """Write the replay directory and return its path.

    jobs defaults to the bridge's job log (no attribution); pass the core's full completion
    dicts to keep attribution and critical paths.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.replay_writer import ReplayWriter  # owned by the viz stream

    soc = yaml.safe_load(Path(soc_path).read_text())
    job_list = list(jobs) if jobs is not None else sim.completions()
    tasks = task_table({j["task_id"] for j in job_list} | set(sim.tasks))
    sc = sim.cfg.scenario
    w = ReplayWriter(out_dir, name=name, soc=soc, tasks=tasks, duration_ps=int(round(sim.t * 1e12)),
                     scenario=sc.name, policy=policy, seed=sc.seed, synthetic=synthetic,
                     provenance=dict(provenance or {"script": "cosim/replay.py"}), scene=scene)
    payload = sim.replay_robot_payload()
    w.set_robot(payload["fps"], payload["body_names"], payload["poses"], payload["events"])
    w.add_jobs(job_list)
    if trace_events is not None:
        w.add_events(trace_events)
    w.add_series("robot.speed_scale", t0_ps=0, rate_hz=sim.cfg.record_hz,
                 values=np.asarray(sim.frames_alpha, dtype=np.float32), unit="", group="robot",
                 label="Robot speed scale (safety)")
    m = sim.metrics()
    w.set_extra("robot_metrics", {k: v for k, v in m.items() if k != "tasks"})
    w.set_extra("behavior_source", "scripted expert in hybrid mode; VLA compute modeled from its operator graph")
    return w.write()
