"""Run one closed-loop co-simulation (MuJoCo robot + glasschip_core + PowerManager).

This is a thin wrapper used by studies s06-s08. It wires the pieces the same way the shared
runner (studies/runner.py, co-sim worker) does: the core chip from cosim.core_chip, the
bridge from cosim.bridge, and the PowerManager through power.cosim_hooks. When the shared
runner is available, `run_power_cosim` delegates to it (see `_runner`).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .cosim_hooks import ObservedChip, make_event_hook
from .manager import PowerManager
from .params import ROOT, load_policy, load_soc
from .predictor import task_info_from_graphs

FIDELITY_DIR = ROOT / "configs" / "fidelity"


def _graphs_for(task_ids: list[str], graph_dir: Path) -> list[dict[str, Any]]:
    out = []
    for t in task_ids:
        f = graph_dir / f"{t}.json"
        if f.exists():
            out.append(json.loads(f.read_text()))
    return out


def run_power_cosim(scenario_path: str | Path, policy: str | Mapping[str, Any], seed: int,
                    duration_s: float | None = None, fidelity: str = "interactive",
                    ambient_c: float | None = None, power_kwargs: Mapping[str, Any] | None = None,
                    surrogate: Callable | None = None, extra_events: list[dict[str, Any]] | None = None,
                    vmin: Mapping[str, float] | None = None, record: bool = False) -> dict[str, Any]:
    """Run a scenario with a power policy. Returns {metrics, power, pm, wall_s, ...}.

    vmin: {domain: volts} applied before the run starts (static S8-style sweep point).
    extra_events: additional scenario events [{t_s, action, args}] (merged and time-sorted).
    """
    from cosim.bridge import CoSim, CoSimConfig
    from cosim.core_chip import make_core_chip
    from cosim.scenario import load_scenario

    sc = load_scenario(str(scenario_path))
    sc.seed = int(seed)
    if duration_s is not None:
        sc.duration_s = float(duration_s)
    if extra_events:
        from cosim.scenario import ScenarioEvent
        for e in extra_events:
            sc.events.append(ScenarioEvent(float(e["t_s"]), e["action"], dict(e.get("args", {}))))
        sc.events.sort(key=lambda e: e.t_s)
    raw = sc.raw or {}
    graph_dir = ROOT / raw.get("graph_dir", "workloads/graphs")
    pol = load_policy(policy) if isinstance(policy, str) else dict(policy)
    amb = float(ambient_c if ambient_c is not None else raw.get("ambient_c", 25.0))
    graphs = _graphs_for(list(sc.tasks.keys()), graph_dir)
    pm = PowerManager(pol, load_soc(), ambient_c=amb, task_info=task_info_from_graphs(graphs),
                      surrogate=surrogate, **dict(power_kwargs or {}))
    for d, v in (vmin or {}).items():
        pm.set_vmin(d, float(v))
    fid = FIDELITY_DIR / f"{fidelity}.yaml"
    core = make_core_chip(sc.tasks.keys(), fidelity=fid, graph_dir=graph_dir, seed=int(seed),
                          transport=raw.get("transport", "copy"), executor=raw.get("executor", "single_threaded"))
    chip = ObservedChip(core, pm)
    pm.apply_scheduler(chip)
    cfg = CoSimConfig(scenario=sc, record=record)
    t0 = time.time()
    sim = CoSim(cfg, chip=chip, power_manager=pm, event_hook=make_event_hook(pm))
    metrics = sim.run()
    pm.step(chip, int(round(sc.duration_s * 1e12)))   # account the final step
    return {"metrics": metrics, "power": pm.summary(), "pm": pm, "cosim": sim, "wall_s": time.time() - t0,
            "scenario": str(scenario_path), "seed": int(seed), "policy": pol.get("name", str(policy)),
            "fidelity": str(fid), "duration_s": sc.duration_s}
