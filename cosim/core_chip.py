"""Adapter that plugs the real SoC simulator (glasschip_core) into the co-sim.

glasschip_core.Sim already satisfies the ChipModel protocol (release_job, run_until,
pending_jobs). This module builds one from config files and task graphs, and routes any task
that has no graph to a FixedLatencyChip fallback so a scenario never stalls on a missing graph.

The import is guarded: if the core module is not built, `core_available()` is False and
`make_core_chip` raises a clear error. Tests and studies that need it skip themselves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

from cosim.fake_chip import FixedLatencyChip, TaskTiming

ROOT = Path(__file__).resolve().parents[1]
CORE_BUILD = ROOT / "core" / "build"

try:  # guarded import: the core is built by a separate stream
    if str(CORE_BUILD) not in sys.path:
        sys.path.insert(0, str(CORE_BUILD))
    import glasschip_core as _gc  # type: ignore
except Exception:  # pragma: no cover - depends on the build
    _gc = None


def core_available() -> bool:
    return _gc is not None


def _load_yaml(p: Path) -> dict[str, Any]:
    return yaml.safe_load(p.read_text())


class CoreChip:
    """glasschip_core.Sim plus a fixed-latency fallback for tasks without a graph."""

    name = "glasschip_core"

    def __init__(self, sim: Any, known_tasks: set[str], fallback: FixedLatencyChip | None = None,
                 info: dict[str, Any] | None = None) -> None:
        self.sim = sim
        self.known = known_tasks
        self.fallback = fallback or FixedLatencyChip(default=TaskTiming(1.0, 0.0, "pipeline"))
        self.info = info or {}
        self._fb_base = 1 << 40  # fallback job ids live in a separate high range

    def release_job(self, task_id: str, t_ps: int, tag: int) -> int:
        if task_id in self.known:
            return int(self.sim.release_job(task_id, int(t_ps), int(tag)))
        jid = self.fallback.release_job(task_id, t_ps, tag)
        return -1 if jid < 0 else self._fb_base + jid

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        out = list(self.sim.run_until(int(t_ps)))
        for c in self.fallback.run_until(t_ps):
            c = dict(c)
            c["job_id"] = self._fb_base + int(c["job_id"])
            out.append(c)
        return out

    def pending_jobs(self) -> list[dict[str, Any]]:
        return list(self.sim.pending_jobs()) + self.fallback.pending_jobs()

    def __getattr__(self, item: str) -> Any:
        # counters, set_domain_freq, trace_* and the rest go straight to the core (power manager).
        return getattr(self.sim, item)


def make_core_chip(tasks: Iterable[str], soc: str | Path = ROOT / "configs/soc/vx1.yaml",
                   fidelity: str | Path = ROOT / "configs/fidelity/interactive.yaml",
                   graph_dir: str | Path = ROOT / "workloads/graphs", seed: int = 0,
                   transport: str = "copy", executor: str = "single_threaded",
                   scheduler: dict[str, str] | None = None) -> CoreChip:
    """Build a CoreChip with every task in `tasks` that has a graph file in graph_dir."""
    if _gc is None:
        raise RuntimeError(f"glasschip_core is not importable from {CORE_BUILD}; build the core first")
    soc_d = _load_yaml(Path(soc))
    fid_d = _load_yaml(Path(fidelity))
    sim = _gc.Sim(soc_d, fid_d, int(seed))
    gdir = Path(graph_dir)
    mw = gdir / "middleware.json"
    if mw.exists():
        sim.set_middleware(json.loads(mw.read_text()), transport, executor)
    known: set[str] = set()
    for t in sorted(set(tasks)):
        f = gdir / f"{t}.json"
        if f.exists():
            sim.add_task(json.loads(f.read_text()))
            known.add(t)
    for eng, pol in (scheduler or {}).items():
        sim.set_scheduler(eng, pol)
    return CoreChip(sim, known, info={"soc": str(soc), "fidelity": str(fidelity), "graph_dir": str(gdir),
                                      "tasks_on_core": sorted(known)})
