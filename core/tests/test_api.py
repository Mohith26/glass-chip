"""Python API tests for glasschip_core using the synthetic fixture graphs."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest
from conftest import ALL_FIXTURES, PS_MS, ROOT, drive, fixture_graph, gc, load_fidelity

ATTR_KEYS = {"compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware"}
DOMAINS = {"cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io"}
ACT_KEYS = {"int8_macs", "bf16_macs", "fp8_macs", "sram_bytes", "flit_hops", "cache_bytes", "dram_bytes", "dram_activates", "cpu_instr",
            "vec_ops", "simt_flops", "dsp_ops", "dma_bytes"}


@pytest.mark.parametrize("mix", ["interactive", "study", "analytic", "memory_focus"])
def test_every_fidelity_mix_runs(make_sim, mix):
    sim = make_sim(mix)
    done = drive(sim, 0, 140)
    assert len(done) > 150
    info = sim.info()
    assert info["fidelity"]["name"] == mix
    for c in done:
        assert sum(c["attribution"].values()) == c["response_ps"]


def test_completion_dict_matches_contract(make_sim):
    sim = make_sim()
    done = drive(sim, 0, 40)
    c = next(d for d in done if d["task_id"] == "T91_det")
    assert {"job_id", "task_id", "tag", "release_ps", "start_ps", "finish_ps", "deadline_ps", "missed", "response_ps", "attribution",
            "critical_path"} <= set(c)
    assert set(c["attribution"]) == ATTR_KEYS
    assert c["response_ps"] == c["finish_ps"] - c["release_ps"]
    assert c["deadline_ps"] == c["release_ps"] + 33_000 * 1_000_000
    assert c["tag"] == 0
    path = c["critical_path"]
    assert [p["op_id"] for p in path if not p["op_id"].startswith("__")] == ["det.conv0", "det.conv1", "det.head", "det.nms"]
    for p in path:
        assert {"op_id", "engine", "start_ps", "end_ps", "wait_ps", "wait_cause"} <= set(p)
        assert p["end_ps"] >= p["start_ps"]
    assert path[0]["engine"] in {"cpu_a", "cpu_b", "none"}
    assert path[1]["engine"] == "npu"


def test_middleware_copy_vs_zero_copy(make_sim):
    res = {}
    for transport in ("copy", "zero_copy"):
        sim = make_sim(middleware=(transport, "single_threaded"))
        done = drive(sim, 0, 100)
        det = [d for d in done if d["task_id"] == "T91_det"]
        res[transport] = (np.mean([d["attribution"]["middleware"] for d in det]), sim.counters(False)["dram_io"]["dram_bytes"])
    assert res["copy"][0] > res["zero_copy"][0]


def test_no_middleware_means_no_middleware_cost(make_sim):
    sim = make_sim(middleware=None, tasks=["T95_est"])
    done = drive(sim, 0, 20, ["T95_est"])
    assert all(d["attribution"]["middleware"] == 0 for d in done)


def test_trace_dicts_numpy_and_hash(make_sim):
    sim = make_sim()
    sim.trace_enable(True)
    drive(sim, 0, 40)
    recs = sim.trace_drain()
    kinds = {r["kind"] for r in recs}
    assert {"job_release", "job_start", "job_finish", "op", "xfer"} <= kinds
    x = next(r for r in recs if r["kind"] == "xfer")
    assert len(x["src_node"]) == 2 and len(x["dst_node"]) == 2 and x["bytes"] > 0
    assert set(recs[0]) >= {"t_ps", "dur_ps", "kind", "block", "unit", "task_id", "job_id", "op_id", "src_node", "dst_node", "bytes", "value"}
    drive(sim, 40, 60)
    arr = sim.trace_drain(as_numpy=True)
    assert isinstance(arr, np.ndarray) and arr.dtype.names and "t_ps" in arr.dtype.names and len(arr) > 0
    names = sim.trace_names()
    assert names["kinds"][int(arr["kind"][0])] in kinds | {"dram_q", "freq", "preempt", "job_miss"}
    assert len(sim.trace_hash()) == 32


def test_determinism_and_checkpoint_restore(make_sim):
    hashes = []
    for _ in range(2):
        sim = make_sim(seed=7)
        drive(sim, 0, 120)
        hashes.append(sim.trace_hash())
    assert hashes[0] == hashes[1]
    sim = make_sim(seed=7)
    drive(sim, 0, 60)
    blob = sim.checkpoint()
    assert isinstance(blob, bytes) and len(blob) > 1000
    a = drive(sim, 60, 120)
    ha = sim.trace_hash()
    sim.restore(blob)
    assert sim.now_ps == 60 * PS_MS
    b = drive(sim, 60, 120)
    assert sim.trace_hash() == ha
    assert [(d["job_id"], d["finish_ps"]) for d in a] == [(d["job_id"], d["finish_ps"]) for d in b]
    fresh = make_sim(seed=999)
    fresh.restore(blob)
    drive(fresh, 60, 120)
    assert fresh.trace_hash() == ha


def test_overrun_skip_returns_minus_one(make_sim):
    sim = make_sim(tasks=["T92_policy"], middleware=None)
    assert sim.release_job("T92_policy", 0, 1) > 0
    sim.run_until(100_000)  # 0.1 us: first job still active
    assert sim.release_job("T92_policy", 100_000, 2) == -1
    sim.run_until(50 * PS_MS)
    assert sim.release_job("T92_policy", 50 * PS_MS, 3) > 0


def _cpu_task(tid: str, deadline_us: float, scalar_instr: float) -> dict:
    g = fixture_graph("T95_est")
    g = json.loads(json.dumps(g))
    g["task_id"] = tid
    g["deadline_us"] = deadline_us
    g["release"] = {"kind": "periodic", "period_us": deadline_us, "on_overrun": "queue"}
    g.pop("affinity", None)
    g["ops"][0]["instr"] = {"scalar": scalar_instr}
    return g


def test_scheduler_edf_vs_fifo(soc):
    import copy

    soc1 = copy.deepcopy(soc)
    soc1["blocks"]["cpu_a"]["cores"] = 1
    soc1["blocks"]["cpu_b"]["cores"] = 0
    res = {}
    for pol in ("fifo", "edf"):
        sim = gc.Sim(soc1, load_fidelity("analytic"), 1)
        sim.set_scheduler("cpu", pol)
        sim.add_task(_cpu_task("T80_long", 50_000, 2e7))   # 5 ms at 2 GHz, IPC 2
        sim.add_task(_cpu_task("T81_short", 2_000, 2e5))   # 0.05 ms
        sim.release_job("T80_long", 0, 0)
        sim.run_until(100_000_000)  # 0.1 ms
        sim.release_job("T81_short", 100_000_000, 0)
        res[pol] = {d["task_id"]: d for d in sim.run_until(50 * PS_MS)}
    assert res["fifo"]["T81_short"]["missed"] and not res["edf"]["T81_short"]["missed"]
    assert res["edf"]["T81_short"]["response_ps"] < 0.2 * PS_MS
    assert res["edf"]["T80_long"]["attribution"]["preemption"] > 0


def test_dvfs_scales_compute_and_counters(make_sim):
    sim = make_sim(tasks=["T95_est"], middleware=None)
    assert sim.domain_freq("cpu_a") == pytest.approx(2.0e9)
    base = drive(sim, 0, 10, ["T95_est"])
    sim.set_domain_freq("cpu_a", 1.0e9)
    assert sim.domain_freq("cpu_a") == pytest.approx(1.0e9)
    slow = drive(sim, 10, 20, ["T95_est"])
    assert np.mean([d["response_ps"] for d in slow[1:]]) > 1.6 * np.mean([d["response_ps"] for d in base])
    c = sim.counters(reset=True)
    assert set(c) == DOMAINS
    for d in DOMAINS:
        assert ACT_KEYS <= set(c[d])
        assert c[d]["busy_ps"] + c[d]["idle_ps"] == 20 * PS_MS
    assert c["cpu_a"]["cpu_instr"] > 0 and c["cpu_a"]["busy_ps"] > 0
    c2 = sim.counters(reset=True)
    assert c2["cpu_a"]["cpu_instr"] == 0


def test_counters_activity_by_domain(make_sim):
    sim = make_sim()
    drive(sim, 0, 70)
    c = sim.counters(False)
    assert c["npu"]["int8_macs"] > 0 and c["npu"]["bf16_macs"] > 0 and c["npu"]["sram_bytes"] > 0
    assert c["simt"]["simt_flops"] > 0
    assert c["dsp"]["dsp_ops"] > 0
    assert c["rt"]["cpu_instr"] > 0
    assert c["noc_cache"]["flit_hops"] > 0 and c["noc_cache"]["cache_bytes"] > 0 and c["noc_cache"]["dma_bytes"] > 0
    assert c["dram_io"]["dram_bytes"] > 0 and c["dram_io"]["dram_activates"] > 0


def test_pending_jobs_and_queue_depths(make_sim):
    sim = make_sim()
    for t in ALL_FIXTURES:
        sim.release_job(t, 0, 0)
    sim.run_until(200_000_000)  # 0.2 ms
    pend = sim.pending_jobs()
    assert pend
    p = next(x for x in pend if x["task_id"] == "T92_policy")
    assert {"job_id", "task_id", "release_ps", "deadline_ps", "started", "frac_done", "est_remaining_cycles"} <= set(p)
    assert 0 <= p["frac_done"] < 1 and p["est_remaining_cycles"].get("npu", 0) > 0
    q = sim.queue_depths()
    assert {"dram", "dram_channels", "noc", "engines"} <= set(q)
    assert len(q["dram"]) == 2


def test_qos_partition_and_mapping_toggles(make_sim):
    sim = make_sim()
    sim.set_qos(True)
    sim.set_cache_partition({"safety": 2, "realtime": 10, "best_effort": 4})
    sim.set_mapping("eft")
    for e in ("npu", "simt", "cpu", "rt", "dsp", "dma"):
        sim.set_scheduler(e, "edf_mc")
    done = drive(sim, 0, 70)
    info = sim.info()
    assert info["qos"] and info["cache_partitioned"] and info["mapping"] == "eft"
    assert info["schedulers"]["npu"] == "edf_mc"
    assert len(done) > 50
    sim.set_cache_partition(None)
    assert not sim.info()["cache_partitioned"]


def test_npu_l2_tables_and_fallback_warning(make_sim):
    fid = load_fidelity("study")
    sim = make_sim(fidelity=fid, tasks=["T92_policy"], middleware=None)
    drive(sim, 0, 130, ["T92_policy"])
    assert any("npu: L2 requested" in w for w in sim.info()["warnings"])
    sim2 = make_sim(fidelity=fid, tasks=["T92_policy"], middleware=None)
    sim2.set_l2_tables({"npu_cycles": {f"pol.l{i}.mm": 2_000_000 for i in range(8)}})
    done = drive(sim2, 0, 130, ["T92_policy"])
    info = sim2.info()
    assert info["l2"]["npu_table_hits"] >= 8
    # 8 layers x 2M cycles / tiles at 1.2 GHz dominates the response.
    assert done[0]["response_ps"] > 8 * 2_000_000 / 4 / 1.2e9 * 1e12


def test_gct_file_and_perfetto_export(make_sim, tmp_path):
    import perfetto_export as pe

    sim = make_sim()
    sim.trace_enable(True)
    path = tmp_path / "t.gct"
    assert sim.trace_open(str(path))
    drive(sim, 0, 40)
    sim.set_domain_freq("npu", 0.8e9)
    drive(sim, 40, 50)
    sim.trace_close()
    drained = sim.trace_drain()
    from_file = pe.read_gct(path)
    assert len(from_file) == len(drained)
    assert from_file[5]["t_ps"] == drained[5]["t_ps"] and from_file[5]["op_id"] == drained[5]["op_id"]
    doc = pe.to_chrome(from_file, include_xfers=True)
    out = tmp_path / "t.json"
    out.write_text(json.dumps(doc))
    names = {e["args"]["name"] for e in doc["traceEvents"] if e.get("name") == "thread_name"}
    assert any(n.startswith("npu[") for n in names) and "T91_det" in names
    assert any(e.get("ph") == "C" and "freq" in e["name"] for e in doc["traceEvents"])


def test_real_workload_graphs_smoke(soc):
    """Loads every graph in workloads/graphs/ (if the workloads worker has produced them)."""
    gdir = ROOT / "workloads" / "graphs"
    graphs = sorted(p for p in gdir.glob("T*.json"))
    if not graphs:
        pytest.skip("no workload graphs yet")
    import jsonschema

    schema = json.loads((ROOT / "workloads" / "schema" / "taskgraph.schema.json").read_text())
    sim = gc.Sim(soc, load_fidelity("interactive"), 1)
    ids = []
    for p in graphs:
        g = json.loads(p.read_text())
        jsonschema.validate(g, schema)
        sim.add_task(g)
        ids.append(g["task_id"])
    mw = gdir / "middleware.json"
    if mw.exists():
        sim.set_middleware(json.loads(mw.read_text()), "copy", "single_threaded")
    for i, t in enumerate(ids):
        assert sim.release_job(t, i * 1000, i) > 0
    done = sim.run_until(3_000 * PS_MS)
    assert {d["task_id"] for d in done} == set(ids)
    for d in done:
        assert sum(d["attribution"].values()) == d["response_ps"]
