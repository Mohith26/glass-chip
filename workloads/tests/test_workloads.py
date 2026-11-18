"""Tests for the workload layer. Offline: they read the generated graphs and build tiny
ONNX models in memory. Run: .venv/bin/python -m pytest -q workloads/tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper, numpy_helper

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workloads"))
sys.path.insert(0, str(ROOT / "workloads" / "capture"))
sys.path.insert(0, str(ROOT / "workloads" / "capture" / "control"))

import validate  # noqa: E402
from onnx_walk import WalkConfig, walk  # noqa: E402

GRAPHS = ROOT / "workloads" / "graphs"


def load(task: str) -> dict:
    return json.loads((GRAPHS / f"{task}.json").read_text())


def test_all_graphs_and_scenarios_validate() -> None:
    assert validate.validate_all(verbose=False) == 0


def test_cycle_detection() -> None:
    ops = [{"op_id": "a", "depends_on": ["c"]}, {"op_id": "b", "depends_on": ["a"]}, {"op_id": "c", "depends_on": ["b"]}]
    assert validate.find_cycle(ops) is not None
    ops[0]["depends_on"] = []
    assert validate.find_cycle(ops) is None


def test_zero_mac_matmul_is_rejected() -> None:
    g = load("T7_control")
    g["ops"].append({"op_id": "bad", "type": "matmul", "eligible": ["npu"], "macs": 0, "flops": 0})
    import jsonschema
    v = jsonschema.Draft202012Validator(json.loads(validate.SCHEMA.read_text()))
    errs = validate.check_graph(g, v)
    assert any("zero MACs" in e for e in errs)


def _tiny_model(path: Path) -> None:
    """Conv(3->8, 3x3, pad 1) + SiLU on 1x3x16x16, then Flatten + MatMul to 10."""
    w = numpy_helper.from_array(np.zeros((8, 3, 3, 3), np.float32), "w")
    b = numpy_helper.from_array(np.zeros((8,), np.float32), "b")
    m = numpy_helper.from_array(np.zeros((8 * 16 * 16, 10), np.float32), "m")
    nodes = [
        helper.make_node("Conv", ["x", "w", "b"], ["c"], name="/blk0/conv/Conv", pads=[1, 1, 1, 1]),
        helper.make_node("Sigmoid", ["c"], ["s"], name="/blk0/act/Sigmoid"),
        helper.make_node("Mul", ["c", "s"], ["a"], name="/blk0/act/Mul"),
        helper.make_node("Flatten", ["a"], ["f"], name="/head/Flatten"),
        helper.make_node("MatMul", ["f", "m"], ["y"], name="/head/fc/MatMul"),
    ]
    g = helper.make_graph(nodes, "tiny", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 16, 16])],
                          [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 10])], [w, b, m])
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)]), str(path))


def test_onnx_walk_counts_and_fusion(tmp_path: Path) -> None:
    p = tmp_path / "tiny.onnx"
    _tiny_model(p)
    ops, stats = walk(str(p), WalkConfig(prefix="t"))
    assert [o["type"] for o in ops] == ["conv2d", "matmul"]
    conv, fc = ops
    assert conv["macs"] == 8 * 16 * 16 * 3 * 3 * 3
    assert conv["onnx_ops"] == ["Conv", "Sigmoid", "Mul"]
    assert conv["bytes_w"] == 8 * 3 * 3 * 3 + 8 * 4          # int8 weights + int32 bias
    assert conv["bytes_in"] == 3 * 16 * 16 and conv["bytes_out"] == 8 * 16 * 16
    assert fc["macs"] == 8 * 16 * 16 * 10
    assert fc["depends_on"] == [conv["op_id"]]             # dependency threaded through Flatten
    assert conv["preempt_boundary"] and fc["preempt_boundary"]


def test_detector_matches_published_flops() -> None:
    # Ultralytics lists YOLOv8n at 8.7 GFLOPs (2 x MACs) for 640x640.
    g = load("T2_detect")
    macs = sum(o["macs"] for o in g["ops"])
    assert 4.2e9 < macs < 4.5e9


def test_vla_param_crosscheck_against_safetensors_header() -> None:
    g = load("T4_vla")
    assert g["params"]["derived_total_params"] == g["params"]["header_total_params"]
    for grp, v in g["params"]["param_check"].items():
        assert v["derived"] == v["header"], grp
    kv = g["params"]["kv_cache"]
    assert g["footprint"]["kv_cache_bytes"] == kv["layers"] * 2 * kv["tokens"] * kv["kv_dim"] * 2
    # attention is explicit: one QK^T and one AV per layer per pass
    qk = [o for o in g["ops"] if o["op_id"].endswith(".attn.qk")]
    assert len(qk) == 16 + 10 * 16


def test_control_counts_are_compiled_counts() -> None:
    c = json.loads((ROOT / "workloads" / "capture" / "control" / "rv64_counts.json").read_text())
    for f in ("control_step", "estimate_step", "ssm_step"):
        assert c[f]["instr_total"] > 0
        assert not c[f]["external_calls"], f
    assert c["control_step"]["instr"]["vector"] == 0       # RT core is rv64gc
    t7 = load("T7_control")
    assert t7["affinity"] == ["rt"] and t7["deadline_us"] == 1000 and t7["criticality"] == "safety"


def test_classify_rv64() -> None:
    import count_rv64 as cr
    assert cr.classify("fld") == "mem" and cr.classify("vle32.v") == "mem"
    assert cr.classify("vfmacc.vv") == "vector" and cr.classify("fmadd.d") == "scalar"
    assert cr.classify("sll") == "scalar"


def test_middleware_contract_fields() -> None:
    m = json.loads((GRAPHS / "middleware.json").read_text())
    assert m["copy"]["copies"] == 2 and m["zero_copy"]["copies"] == 0
    assert m["copy"]["serialize_ns_per_kb"] > 0 and m["zero_copy"]["serialize_ns_per_kb"] == 0
    assert m["scaling"]["mac_to_target_scale"] > 0


def test_curobo_defaults_recorded() -> None:
    g = load("T5_plan")
    d = g["params"]["cuRobo_defaults"]
    assert d["collision_spheres"] == 61 and d["trajopt_seeds"] == 4 and d["num_knots"] == 16
    assert g["release"]["kind"] == "sporadic"
    assert "FALLBACK" in g["provenance"]["notes"]


def test_scenarios_cover_s1_to_s8() -> None:
    names = sorted(p.stem.split("_")[0] for p in (ROOT / "configs" / "scenarios").glob("S*.yaml"))
    assert names == [f"S{i}" for i in range(1, 9)]


def test_summary_md_has_no_em_dash() -> None:
    assert "\u2014" not in (GRAPHS / "SUMMARY.md").read_text()
    assert "\u2014" not in (ROOT / "workloads" / "README.md").read_text()


@pytest.mark.parametrize("task", ["T2_detect", "T4_vla", "T8_safety", "T11_speech"])
def test_onnx_graphs_have_hash_and_preempt_boundaries(task: str) -> None:
    g = load(task)
    assert g["provenance"]["model_hash"]
    assert any(o.get("preempt_boundary") for o in g["ops"])


def test_declared_macs_match_shape_in_every_graph() -> None:
    for p in sorted(GRAPHS.glob("T*.json")):
        for o in json.loads(p.read_text())["ops"]:
            assert not validate.mac_shape_errors(o), (p.name, o["op_id"])


def test_mac_shape_check_flags_hidden_loop() -> None:
    # The old T11 encoding: one 1x384x384 op carrying a whole decoder step.
    o = {"op_id": "x", "type": "matmul", "eligible": ["npu"], "shape": {"M": 1, "K": 384, "N": 384, "layers": 4},
         "macs": 32_793_984, "flops": 65_587_968}
    assert validate.mac_shape_errors(o)
    o["macs"] = 384 * 384
    assert not validate.mac_shape_errors(o)


def test_speech_decoder_is_explicit() -> None:
    g = load("T11_speech")
    dec = [o for o in g["ops"] if o["op_id"].startswith("asr.dec.t")]
    assert len([o for o in dec if o["op_id"].endswith(".logits")]) == 12
    assert all(o["shape"]["M"] == 1 for o in dec)


def test_simt_ops_are_one_launch_each() -> None:
    for t in ("T3_esdf", "T5_plan"):
        for o in load(t)["ops"]:
            if o["type"] == "simt_kernel":
                assert o["shape"]["kernel_launches"] == 1, (t, o["op_id"])
