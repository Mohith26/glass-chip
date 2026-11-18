"""Command-line runner: one scenario, one chip model, metrics to stdout, optional recording.

Examples:
    .venv/bin/python -m cosim.run --scenario configs/scenarios/S1_nominal.yaml --chip zero
    .venv/bin/python -m cosim.run --scenario configs/scenarios/S3_human_enters.yaml --chip core \\
        --record data/cosim/runs/S3_core
    .venv/bin/python -m cosim.run --speed 0.3 --duration 30 --chip fixed --e2e-ms 200
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cosim.bridge import CoSim, CoSimConfig, _json_default
from cosim.fake_chip import FixedLatencyChip, ZeroLatencyChip
from cosim.scenario import load_scenario, make_scenario


def build_chip(kind: str, scenario, seed: int, e2e_ms: float, fidelity: str | None):
    if kind == "zero":
        return ZeroLatencyChip()
    if kind == "fixed":
        return FixedLatencyChip.with_injected_e2e(e2e_ms, seed=seed)
    if kind == "core":
        from cosim.core_chip import make_core_chip
        kw = {"fidelity": fidelity} if fidelity else {}
        raw = scenario.raw or {}
        return make_core_chip(scenario.tasks.keys(), seed=seed, transport=raw.get("transport", "copy"),
                              executor=raw.get("executor", "single_threaded"), **kw)
    raise ValueError(kind)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", help="scenario YAML (configs/scenarios/*.yaml or cosim/scenarios_dev/*.yaml)")
    ap.add_argument("--speed", type=float, default=0.2)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--chip", choices=["zero", "fixed", "core"], default="zero")
    ap.add_argument("--e2e-ms", type=float, default=100.0, help="injected end-to-end latency for --chip fixed")
    ap.add_argument("--fidelity", help="fidelity YAML for --chip core")
    ap.add_argument("--record", help="path prefix for <prefix>.npz and <prefix>.json")
    ap.add_argument("--replay", help="write a glasschip-viz replay directory (tools/replay_writer.py)")
    ap.add_argument("--trace", action="store_true", help="with --chip core: include chip trace events in the replay")
    a = ap.parse_args()
    sc = load_scenario(a.scenario) if a.scenario else make_scenario(conveyor_speed_mps=a.speed)
    if a.duration is not None:
        sc.duration_s = a.duration
    if a.seed is not None:
        sc.seed = a.seed
    chip = build_chip(a.chip, sc, sc.seed, a.e2e_ms, a.fidelity)
    sim = CoSim(CoSimConfig(scenario=sc, record=a.record is not None or a.replay is not None), chip=chip)
    if a.trace and hasattr(chip, "trace_enable"):
        chip.trace_enable(True)
    m = sim.run()
    if a.replay:
        from cosim.replay import write_replay
        ev = chip.trace_drain() if a.trace and hasattr(chip, "trace_drain") else None
        write_replay(sim, a.replay, name=f"{sc.name} / {a.chip} chip", trace_events=ev,
                     provenance={"script": "cosim/run.py", "chip": a.chip, "scenario": a.scenario})
        m["replay"] = a.replay
    if a.record:
        npz, js = sim.save_recording(Path(a.record))
        m["recording"] = [str(npz), str(js)]
    print(json.dumps(m, indent=1, default=_json_default))


if __name__ == "__main__":
    main()
