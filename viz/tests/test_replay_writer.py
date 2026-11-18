"""Round-trip tests for tools/replay_writer.py (run with .venv/bin/python -m pytest viz/tests)."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from replay_writer import (  # noqa: E402
    EVENT_DTYPE,
    JOB_DTYPE,
    ReplayWriter,
    mesh_links,
    update_index,
    xy_route,
)


def _soc() -> dict:
    return yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())


def _writer(tmp: Path) -> ReplayWriter:
    tasks = [
        {"id": "T2_detect", "name": "Detect", "period_us": 33333, "deadline_us": 33000, "criticality": "high"},
        {"id": "T7_control", "name": "Control", "period_us": 1000, "deadline_us": 1000, "criticality": "safety"},
    ]
    return ReplayWriter(tmp / "r", name="test", soc=_soc(), tasks=tasks, duration_ps=10**12, scenario="S1_test",
                        policy="P0_nominal", seed=1, synthetic=True, provenance={"script": "test"})


def test_xy_route_goes_x_then_y() -> None:
    hops = xy_route(0, 15, [4, 4])
    assert hops[0] == (0, 1) and hops[2] == (2, 3) and hops[-1] == (11, 15)
    assert len(hops) == 6
    assert len(mesh_links([4, 4])) == 48


def test_round_trip(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.add_events([
        {"t_ps": 5, "dur_ps": 1000, "kind": "op", "block": "npu", "unit": 2, "task_id": "T2_detect", "job_id": 7,
         "op_id": "det.layer0", "critical": True},
        {"t_ps": 3 * 10**11, "dur_ps": 10**6, "kind": "xfer", "task_id": "T2_detect", "job_id": 7,
         "src_node": [0, 3], "dst_node": [1, 1], "bytes": 4096.0},
    ])
    w.add_jobs([{"job_id": 7, "task_id": "T2_detect", "release_ps": 0, "start_ps": 5, "finish_ps": 40 * 10**9,
                 "deadline_ps": 33 * 10**9, "missed": True, "attribution": {"dram": 30e9, "compute": 10e9},
                 "critical_path": [{"op_id": "det.layer0", "engine": "npu2", "start_ps": 5, "end_ps": 1005,
                                    "wait_ps": 0, "wait_cause": "dram"}]}])
    w.add_series("power.npu", t0_ps=0, rate_hz=1000, values=np.full(1000, 2.0), unit="W", group="power")
    w.set_robot(60, ["link0", "hand"], np.zeros((60, 2, 7), dtype=np.float32))
    out = w.write()

    m = json.loads((out / "manifest.json").read_text())
    assert m["synthetic"] is True and m["events"]["count"] == 2
    assert m["events"]["strings"] == ["det.layer0"]
    ev = np.frombuffer(gzip.decompress((out / m["events"]["chunks"][0]["file"]).read_bytes()), dtype=EVENT_DTYPE)
    assert ev[0]["unit"] == 2 and ev[0]["flags"] & 1
    ev2 = np.frombuffer(gzip.decompress((out / m["events"]["chunks"][1]["file"]).read_bytes()), dtype=EVENT_DTYPE)
    assert ev2[0]["src"] == 12 and ev2[0]["dst"] == 5
    jobs = np.frombuffer(gzip.decompress((out / "jobs.bin.gz").read_bytes()), dtype=JOB_DTYPE)
    assert jobs[0]["flags"] & 1 and jobs[0]["flags"] & 4
    assert abs(float(jobs[0]["attr_ns"][4]) - 30e6) < 1
    cp = json.loads(gzip.decompress((out / "critpath.json.gz").read_bytes()))
    assert cp["7"][0]["engine"] == "npu2"
    r = m["results"]
    assert r["tasks"]["T2_detect"]["misses"] == 1
    assert abs(r["avg_power_w"] - 2.0) < 1e-6 and abs(r["energy_per_robot_hour_wh"] - 2.0) < 1e-6
    lod = gzip.decompress((out / "lod.bin.gz").read_bytes())
    links = np.frombuffer(lod[m["lod"]["link_offset"]:], dtype="<f4").reshape(m["lod"]["bins"], -1)
    assert links.sum() == 4096.0 * 3  # three hops from node 12 to node 5
    assert m["robot"]["frames"] == 60


def test_update_index(tmp_path: Path) -> None:
    idx = tmp_path / "index.json"
    update_index(idx, {"id": "a", "path": "a/manifest.json"})
    update_index(idx, {"id": "a", "path": "b/manifest.json"})
    data = json.loads(idx.read_text())
    assert len(data["replays"]) == 1 and data["replays"][0]["path"] == "b/manifest.json"
