"""Co-sim tests: determinism, baseline success, stale-data semantics, safety stop, I/O.

Run: .venv/bin/python -m pytest cosim/tests -q
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from cosim.bridge import CoSim, CoSimConfig
from cosim.fake_chip import FixedLatencyChip, TaskTiming, ZeroLatencyChip
from cosim.scenario import load_scenario, make_scenario

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "cosim" / "scenarios_dev"


def _human_scenario(duration: float = 10.0, seed: int = 3):
    sc = load_scenario(DEV / "dev_human.yaml")
    sc.duration_s = duration
    sc.seed = seed
    return sc


def _fingerprint(sim: CoSim) -> str:
    h = hashlib.sha256()
    h.update(np.asarray(sim.frames_pos).tobytes())
    h.update(json.dumps(sim.robot_events, sort_keys=True, default=str).encode())
    return h.hexdigest()


def test_determinism_same_seed_same_run():
    def run(seed):
        sim = CoSim(CoSimConfig(scenario=_human_scenario(7.0, seed)),
                    chip=FixedLatencyChip.with_injected_e2e(80, seed=seed, jitter_frac=0.3))
        sim.run()
        return _fingerprint(sim)

    a, b, c = run(3), run(3), run(4)
    assert a == b
    assert a != c


def test_zero_latency_baseline_succeeds():
    sc = make_scenario(duration_s=40.0, seed=5, conveyor_speed_mps=0.2, object_spacing_m=0.8)
    m = CoSim(CoSimConfig(scenario=sc, record=False), chip=ZeroLatencyChip()).run()
    assert m["grasp_attempts"] >= 6
    assert m["grasp_success_rate"] >= 0.85
    assert m["placed"] >= m["grasp_success"] - 1
    assert m["oscillation_episodes"] == 0


def test_control_zero_order_hold_between_completions():
    """With a 3 ms controller, motor torques change only when a T7 job completes."""
    sc = make_scenario(duration_s=3.0, seed=1)
    chip = FixedLatencyChip.with_injected_e2e(40, seed=1, base={"T7_control": TaskTiming(3.0, 0.0, "skip")})
    sim = CoSim(CoSimConfig(scenario=sc, record=False), chip=chip)
    t7_steps: set[int] = set()
    ctrl: list[np.ndarray] = []
    orig_complete, orig_gt = sim._complete, sim._ground_truth

    def complete(c, t_ps, t):
        if c["task_id"] == "T7_control":
            t7_steps.add(int(round(t * 1000)))
        orig_complete(c, t_ps, t)

    def gt(t):
        ctrl.append(sim.sc.data.ctrl[:7].copy())
        orig_gt(t)

    sim._complete, sim._ground_truth = complete, gt
    m = sim.run()
    assert m["tasks"]["T7_control"]["missed"] > 0          # 3 ms > 1 ms deadline
    assert m["tasks"]["T7_control"]["skipped"] > 0         # releases while busy are skipped
    changes = [k for k in range(1, len(ctrl)) if not np.array_equal(ctrl[k], ctrl[k - 1])]
    assert changes, "controller never updated"
    assert all(k in t7_steps for k in changes)             # held between completions


def test_late_control_causes_oscillation_and_misses():
    def run(lat_ms):
        sc = make_scenario(duration_s=20.0, seed=1, conveyor_speed_mps=0.2, object_spacing_m=0.6)
        chip = FixedLatencyChip.with_injected_e2e(40, seed=1, base={"T7_control": TaskTiming(lat_ms, 0.0, "skip")})
        return CoSim(CoSimConfig(scenario=sc, record=False), chip=chip).run()

    ok, late = run(0.2), run(12.0)
    assert ok["oscillation_episodes"] == 0
    assert late["oscillation_episodes"] >= 1
    assert late["oscillation_time_s"] > 5.0
    assert (late["grasp_success_rate"] or 0.0) < (ok["grasp_success_rate"] or 0.0)


def test_late_detection_keeps_previous_and_dropping_it_starves_policy():
    """T2 over deadline: the stack keeps acting on older detections (spec 9.1).

    With late outputs dropped instead of applied, no detection ever reaches the tracker, so the
    policy only ever has the previous (here: none) and never attempts a grasp.
    """
    base = {"T2_detect_overhead": TaskTiming(60.0, 0.0, "pipeline")}
    sc = make_scenario(duration_s=12.0, seed=2, conveyor_speed_mps=0.2, object_spacing_m=0.6)
    sim = CoSim(CoSimConfig(scenario=sc, record=False), chip=FixedLatencyChip(base, seed=2))
    m = sim.run()
    t2 = m["tasks"]["T2_detect_overhead"]
    assert t2["missed"] == t2["completed"] > 0
    assert m["chain_latency_ms"]["camera_to_detection"]["p50"] >= 60.0
    assert m["grasp_attempts"] >= 1
    sc = make_scenario(duration_s=12.0, seed=2, conveyor_speed_mps=0.2, object_spacing_m=0.6)
    m2 = CoSim(CoSimConfig(scenario=sc, record=False, drop_late=("T2_detect_overhead",)),
               chip=FixedLatencyChip(base, seed=2)).run()
    assert m2["grasp_attempts"] == 0


def test_safety_stop_and_late_safety_monitor():
    def run(t8_ms):
        chip = FixedLatencyChip.with_injected_e2e(40, seed=3, base={"T8_safety": TaskTiming(t8_ms, 0.0, "pipeline")})
        sim = CoSim(CoSimConfig(scenario=_human_scenario(10.0), record=False), chip=chip)
        return sim.run(), sim

    m_ok, sim_ok = run(1.0)
    assert m_ok["safety_zone_entries"] >= 1
    assert m_ok["safety_stop_latency_ms"]["n"] >= 1
    assert m_ok["safety_stop_latency_ms"]["max"] < 150.0
    assert any(e["kind"] == "safety_stop" for e in sim_ok.robot_events)
    m_late, _ = run(150.0)
    assert m_late["safety_stop_latency_ms"]["n"] >= 1
    assert m_late["safety_stop_latency_ms"]["max"] > m_ok["safety_stop_latency_ms"]["max"] + 100.0
    assert m_late["tasks"]["T8_safety"]["missed"] > 0


def test_chain_latency_tracks_injected_latency():
    sc = make_scenario(duration_s=10.0, seed=4, conveyor_speed_mps=0.2, object_spacing_m=0.6)
    m = CoSim(CoSimConfig(scenario=sc, record=False), chip=FixedLatencyChip.with_injected_e2e(100, seed=4)).run()
    c = m["chain_latency_ms"]["camera_to_grasp"]
    assert c["n"] > 0
    # injected 100 ms plus sampling waits (T6 up to 5 ms, T4 up to 125 ms) plus one control period
    assert 100.0 <= c["p50"] <= 100.0 + 5 + 125 + 2


def test_all_scenario_yamls_load():
    files = sorted((ROOT / "configs" / "scenarios").glob("*.yaml")) + sorted(DEV.glob("*.yaml"))
    assert files
    for f in files:
        sc = load_scenario(f)
        for t in ("T4_vla", "T5_plan", "T6_estimate", "T7_control", "T8_safety", "T1_ingest_overhead"):
            assert t in sc.tasks, (f.name, t)
        assert sc.tasks["T7_control"].period_us == 1000


def test_recording_roundtrip(tmp_path):
    sc = make_scenario(duration_s=2.0, seed=1)
    sim = CoSim(CoSimConfig(scenario=sc))
    sim.run()
    npz, js = sim.save_recording(tmp_path / "rec")
    z = np.load(npz)
    meta = json.loads(js.read_text())
    assert z["xpos"].shape == (meta["n_frames"], len(meta["body_names"]), 3)
    assert z["xquat"].shape[2] == 4
    assert abs(meta["n_frames"] - 2.0 * 60) <= 1
    payload = sim.replay_robot_payload()
    assert payload["poses"].shape[2] == 7


def test_scene_export(tmp_path):
    from cosim.export_scene import export
    man = export(tmp_path)
    data = (tmp_path / "scene.bin").read_bytes()
    assert len(data) == man["bin_bytes"]
    roles = {g["role"] for g in man["geoms"]}
    assert {"robot", "object", "conveyor", "bin", "human"} <= roles
    mesh = man["meshes"][0]
    v = mesh["vertices"]
    arr = np.frombuffer(data, dtype=v["dtype"], count=int(np.prod(v["shape"])), offset=v["offset"])
    assert arr.size == mesh["n_vert"] * 3


def test_core_chip_runs_if_built():
    from cosim.core_chip import core_available, make_core_chip
    if not core_available():
        pytest.skip("glasschip_core not built")
    sc = load_scenario(ROOT / "configs" / "scenarios" / "S1_nominal.yaml")
    sc.duration_s = 2.0
    chip = make_core_chip(sc.tasks.keys(), seed=sc.seed)
    m = CoSim(CoSimConfig(scenario=sc, record=False), chip=chip).run()
    assert m["tasks"]["T7_control"]["completed"] >= 1900
    assert m["chain_latency_ms"]["camera_to_detection"]["n"] > 0
