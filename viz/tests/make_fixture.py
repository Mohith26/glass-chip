"""Write the tiny replay fixture used by `npm run smoke` (viz/tests/fixtures/tiny).

It is synthetic on purpose (manifest synthetic: true): the smoke test checks the format and the
decoders, not results. Run: .venv/bin/python viz/tests/make_fixture.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from replay_writer import ReplayWriter  # noqa: E402

OUT = ROOT / "viz" / "tests" / "fixtures" / "tiny"


def main() -> None:
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    tasks = [{"id": "T4_vla", "name": "VLA", "period_us": 125000, "deadline_us": 100000, "criticality": "high"},
             {"id": "T7_control", "name": "Control", "period_us": 1000, "deadline_us": 1000, "criticality": "safety"}]
    dur = 500_000_000_000  # 0.5 s
    w = ReplayWriter(OUT, name="smoke fixture", soc=soc, tasks=tasks, duration_ps=dur, scenario="S0_fixture",
                     policy="P0_nominal", seed=0, synthetic=True, provenance={"script": "viz/tests/make_fixture.py"})
    ev, jobs = [], []
    for k in range(4):
        r = k * 125_000_000_000
        ev.append({"t_ps": r, "dur_ps": 60_000_000_000, "kind": "op", "block": "npu", "unit": k % 4, "task_id": "T4_vla",
                   "job_id": k, "op_id": f"vla.layer{k}"})
        for p in range(50):
            ev.append({"t_ps": r + p * 1_000_000_000, "dur_ps": 90_000, "kind": "xfer", "task_id": "T4_vla", "job_id": k,
                       "op_id": f"vla.layer{k}", "src_node": [0, 3], "dst_node": [1, 1], "bytes": 4096})
        jobs.append({"job_id": k, "task_id": "T4_vla", "release_ps": r, "start_ps": r, "finish_ps": r + 60_000_000_000 + k * 15_000_000_000,
                     "deadline_ps": r + 100_000_000_000, "missed": k == 3, "attribution": {"compute": 5e10, "dram": 1e10},
                     "critical_path": [{"op_id": f"vla.layer{k}", "engine": f"npu{k % 4}", "start_ps": r, "end_ps": r + 6e10,
                                        "wait_ps": 0, "wait_cause": "none"}]})
    w.add_events(ev)
    w.add_jobs(jobs)
    w.add_series("power.npu", t0_ps=0, rate_hz=1000, values=np.linspace(1, 3, 500), unit="W", group="power")
    w.set_robot(60, ["link0", "hand"], np.zeros((30, 2, 7), dtype=np.float32))
    w.write()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
