"""Tests for studies/runner.py (skipped when glasschip_core is not built)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from cosim.core_chip import core_available, scale_graph_bitrate

pytestmark = pytest.mark.skipif(not core_available(), reason="glasschip_core not built")


def test_runner_cosim_short():
    from studies.runner import run
    r = run("S1", "P0", seed=0, duration_s=1.0)
    assert r["robot"] is not None
    assert r["tasks"]["T7_control"]["completed"] >= 950
    assert r["power"]["energy_per_robot_hour_wh"] > 0
    assert r["activity"]["dram_gbps"] > 0
    assert set(r["tasks"]["T4_vla"]["response_ms"]) >= {"p50", "p95", "p99"}
    assert r["provenance"]["core_build"]


def test_runner_chip_only_and_overrides():
    from studies.runner import run
    r = run("S2", "P2", seed=0, duration_s=1.0,
            overrides={"chip_only": True, "qos": True, "cache_partition": {"safety": 2, "realtime": 10, "best_effort": 4},
                       "soc": {"blocks": {"npu": {"freq_hz": 1.0e9}}}})
    assert r["robot"] is None
    assert r["tasks"]["T2_detect"]["completed"] > 0


def test_bitrate_copies_report_under_base_id():
    from studies.runner import run
    ev = [{"t_s": 0.0, "action": "set_bitrate", "args": {"task": "T10_record_4k", "bitrate_bps": 24e9}}]
    r = run("S7", "P0", seed=0, duration_s=0.5, overrides={"chip_only": True, "scenario": {"events": ev}})
    assert "T10_record_4k" in r["tasks"]
    assert not any("@" in t for t in r["tasks"])


def test_scale_graph_bitrate():
    g = json.load(open("workloads/graphs/T10_record_4k.json"))
    g2 = scale_graph_bitrate(g, 2 * g["params"]["bitrate_bps"], "x")
    a = sum(o.get("bytes_in", 0) for o in g["ops"] if o.get("bytes_scale_param"))
    b = sum(o.get("bytes_in", 0) for o in g2["ops"] if o.get("bytes_scale_param"))
    assert abs(b / a - 2.0) < 1e-6


def test_thin_records_merges():
    from studies.runner import thin_records
    from tools.replay_writer import EVENT_DTYPE
    rec = np.zeros(4, dtype=EVENT_DTYPE)
    rec["kind"] = [5, 5, 4, 4]
    rec["t_ps"] = [0, 10_000_000, 0, 1_000_000]
    rec["dur_ps"] = [1_000_000, 1_000_000, 1_000_000, 1_000_000]
    rec["bytes"] = [64, 64, 0, 0]
    out = thin_records(rec, 50_000_000)
    assert len(out) == 2
    assert out[out["kind"] == 5]["bytes"][0] == 128
