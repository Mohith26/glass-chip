"""Export the surrogate to one ONNX file for onnxruntime-web, plus meta.json for the viz.

The ONNX graph takes x: float32 [N, 20] (the design knobs in dataset.INPUTS order, categorical
knobs as integer codes) and returns, for each exported output o, three tensors [N, 1]:
  o            point prediction in output units
  o__lo, o__hi conformal interval at the exported coverage (default 90 percent)
Everything happens inside the graph: derived feature, every LightGBM model as one
ai.onnx.ml TreeEnsembleRegressor (one target per model), MLPs as MatMul/Add/Relu, the conformal
offsets (q-hat), CQR clamping and the inverse output transforms. The area proxy is a closed
form built from Mul/Add nodes.

Run:  .venv/bin/python -m surrogate.export_onnx
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surrogate.dataset import AREA, INPUTS, NOC_BITS, SPACE  # noqa: E402
from surrogate.train import CATEGORICAL, LAT_EPS, RATE_EPS, output_kind  # noqa: E402

OUT_DIR = ROOT / "viz" / "public" / "surrogate"

# Outputs shown in the browser panel (label, unit). Everything else stays in the Python bundle.
WEB_OUTPUTS: list[tuple[str, str, str]] = [
    ("energy_per_robot_hour_wh", "Energy per robot-hour", "Wh"),
    ("peak_power_w", "Peak SoC power", "W"),
    ("peak_temp_c", "Peak junction temperature", "C"),
    ("worst_safety_miss_rate", "Worst safety-task miss rate", ""),
    ("worst_rt_miss_rate", "Worst real-time miss rate", ""),
    ("T8_safety.p99_ms", "Safety monitor (T8) p99", "ms"),
    ("T2_detect.miss_rate", "Detection (T2) miss rate", ""),
    ("T2_detect.p99_ms", "Detection (T2) p99", "ms"),
    ("T4_vla.miss_rate", "VLA policy (T4) miss rate", ""),
    ("T4_vla.p99_ms", "VLA policy (T4) p99", "ms"),
    ("T5_plan.p99_ms", "Motion planning (T5) p99", "ms"),
    ("chain_camera_to_plan.p99_ms", "Camera to plan chain p99", "ms"),
    ("chain_human_to_control.p99_ms", "Human detection to control chain p99", "ms"),
    ("area_mm2", "Area proxy (rough)", "mm2"),
]


def safe(name: str) -> str:
    return name.replace(".", "__")


class G:
    """Tiny ONNX graph builder."""

    def __init__(self) -> None:
        self.nodes: list[onnx.NodeProto] = []
        self.inits: list[onnx.TensorProto] = []
        self.n = 0

    def name(self, p: str) -> str:
        self.n += 1
        return f"{p}_{self.n}"

    def const(self, arr: Any, dtype: Any = np.float32, p: str = "c") -> str:
        nm = self.name(p)
        self.inits.append(numpy_helper.from_array(np.asarray(arr, dtype=dtype), nm))
        return nm

    def op(self, op_type: str, inputs: list[str], out: str | None = None, domain: str | None = None, **attrs: Any) -> str:
        o = out or self.name(op_type.lower())
        self.nodes.append(helper.make_node(op_type, inputs, [o], domain=domain, **attrs))
        return o

    def col(self, x: str, j: int) -> str:
        """x[:, j:j+1]"""
        return self.op("Slice", [x, self.const([j], np.int64), self.const([j + 1], np.int64), self.const([1], np.int64)])


# --------------------------------------------------------------------------- trees
def lgb_trees(model: Any) -> list[dict[str, Any]]:
    return model.booster_.dump_model()["tree_info"]


def add_tree_ensemble(g: G, F: str, ensembles: list[Any]) -> tuple[str, int]:
    """One TreeEnsembleRegressor over all LightGBM models; target k = ensembles[k]."""
    a: dict[str, list[Any]] = {k: [] for k in ("nodes_treeids", "nodes_nodeids", "nodes_featureids", "nodes_modes",
                                              "nodes_values", "nodes_truenodeids", "nodes_falsenodeids",
                                              "nodes_missing_value_tracks_true", "target_treeids", "target_nodeids",
                                              "target_ids", "target_weights")}
    tid = 0
    for k, model in enumerate(ensembles):
        for tree in lgb_trees(model):
            counter = [0]

            def walk(nd: dict[str, Any]) -> int:
                my = counter[0]
                counter[0] += 1
                if "leaf_value" in nd or "split_index" not in nd:
                    a["nodes_treeids"].append(tid); a["nodes_nodeids"].append(my); a["nodes_featureids"].append(0)
                    a["nodes_modes"].append("LEAF"); a["nodes_values"].append(0.0)
                    a["nodes_truenodeids"].append(0); a["nodes_falsenodeids"].append(0)
                    a["nodes_missing_value_tracks_true"].append(0)
                    a["target_treeids"].append(tid); a["target_nodeids"].append(my); a["target_ids"].append(k)
                    a["target_weights"].append(float(nd.get("leaf_value", 0.0)))
                    return my
                assert nd.get("decision_type", "<=") == "<=", nd.get("decision_type")
                pos = len(a["nodes_treeids"])
                a["nodes_treeids"].append(tid); a["nodes_nodeids"].append(my)
                a["nodes_featureids"].append(int(nd["split_feature"])); a["nodes_modes"].append("BRANCH_LEQ")
                a["nodes_values"].append(float(nd["threshold"]))
                a["nodes_truenodeids"].append(-1); a["nodes_falsenodeids"].append(-1)
                a["nodes_missing_value_tracks_true"].append(1 if nd.get("default_left", True) else 0)
                left = walk(nd["left_child"])
                right = walk(nd["right_child"])
                a["nodes_truenodeids"][pos] = left
                a["nodes_falsenodeids"][pos] = right
                return my

            walk(tree["tree_structure"])
            tid += 1
    out = g.name("trees")
    g.nodes.append(helper.make_node("TreeEnsembleRegressor", [F], [out], domain="ai.onnx.ml",
                                    n_targets=len(ensembles), aggregate_function="SUM", post_transform="NONE",
                                    base_values=[0.0] * len(ensembles),
                                    **{k: v for k, v in a.items() if k != "nodes_missing_value_tracks_true"}))
    # nodes_missing_value_tracks_true is optional and only matters for NaN inputs, which the
    # panel never sends; dropping it saves about 15 percent of the file.
    return out, tid


# --------------------------------------------------------------------------- features
def lgb_feature_graph(g: G, x: str) -> str:
    i_t, i_a = INPUTS.index("npu_tiles"), INPUTS.index("npu_array")
    arr = g.col(x, i_a)
    macs = g.op("Mul", [g.col(x, i_t), g.op("Mul", [arr, arr])])
    return g.op("Concat", [x, macs], axis=1)


def mlp_feature_graph(g: G, x: str) -> str:
    parts = []
    for j, k in enumerate(INPUTS):
        kind, lo, hi, _, _ = SPACE[k]
        c = g.col(x, j)
        if k in CATEGORICAL:
            codes = g.const(np.arange(CATEGORICAL[k], dtype=np.float32)[None, :])
            parts.append(g.op("Cast", [g.op("Equal", [g.op("Round", [c]), codes])], to=TensorProto.FLOAT))
        elif kind == "logfloat":
            parts.append(g.op("Div", [g.op("Sub", [g.op("Log", [c]), g.const([math.log(lo)])]),
                                     g.const([math.log(hi) - math.log(lo)])]))
        else:
            parts.append(g.op("Div", [g.op("Sub", [c, g.const([lo])]), g.const([hi - lo])]))
    i_t, i_a = INPUTS.index("npu_tiles"), INPUTS.index("npu_array")
    arr = g.col(x, i_a)
    macs = g.op("Mul", [g.col(x, i_t), g.op("Mul", [arr, arr])])
    parts.append(g.op("Div", [macs, g.const([8 * 128 ** 2])]))
    return g.op("Concat", parts, axis=1)


def mlp_graph(g: G, M: str, m: Any) -> str:
    h = M
    for i, (W, b) in enumerate(zip(m.weights, m.biases)):
        h = g.op("Add", [g.op("MatMul", [h, g.const(W)]), g.const(b)])
        if i < len(m.weights) - 1:
            h = g.op("Relu", [h])
    return g.op("Add", [g.op("Mul", [h, g.const([m.y_std])]), g.const([m.y_mean])])


def inverse_graph(g: G, name: str, z: str, out: str) -> str:
    kind = output_kind(name)
    if kind == "latency":
        return g.op("Max", [g.op("Sub", [g.op("Exp", [z]), g.const([LAT_EPS])]), g.const([0.0])], out=out)
    if kind == "rate":
        v = g.op("Sub", [g.op("Exp", [z]), g.const([RATE_EPS])])
        return g.op("Min", [g.op("Max", [v, g.const([0.0])]), g.const([1.0])], out=out)
    return g.op("Identity", [z], out=out)


def area_graph(g: G, x: str) -> str:
    a = AREA
    c = lambda k: g.col(x, INPUTS.index(k))  # noqa: E731
    sram = a["sram_mm2_per_mb"]
    arr = c("npu_array")
    per_tile = g.op("Add", [g.op("Add", [g.op("Mul", [g.op("Mul", [arr, arr]), g.const([a["mac_mm2"]])]),
                                         g.const([a["npu_tile_overhead_mm2"]])]),
                            g.op("Mul", [c("npu_sram_mb"), g.const([sram])])])
    npu = g.op("Add", [g.op("Mul", [c("npu_tiles"), per_tile]), g.const([4.0 * sram])])
    cores = c("cpu_cores")
    cpu = g.op("Add", [g.op("Mul", [cores, g.const([a["cpu_core_mm2"]])]),
                       g.op("Mul", [g.op("Ceil", [g.op("Div", [cores, g.const([4.0])])]),
                                    g.const([a["cpu_l2_mb_per_4_cores"] * sram])])])
    cache = g.op("Mul", [c("cache_mb"), g.const([sram * 1.1])])
    # noc_bits code 0/1/2 -> 128/256/512 bits = 128 * 2^code
    bits = g.op("Mul", [g.op("Pow", [g.const([2.0]), c("noc_bits")]), g.const([128.0])])
    noc = g.op("Mul", [bits, g.const([a["noc_routers"] * a["router_mm2_per_256b"] / 256.0])])
    dram = g.op("Mul", [c("dram_gbps"), g.const([a["dram_phy_mm2_per_100gbps"] / 100.0])])
    s = npu
    for t in (cpu, cache, noc, dram):
        s = g.op("Add", [s, t])
    return g.op("Add", [s, g.const([a["fixed_mm2"]])])


def build(bundle: Any, outputs: list[str], alpha: float) -> onnx.ModelProto:
    g = G()
    x = "x"
    F = lgb_feature_graph(g, x)
    M = None
    ens: list[Any] = []
    slots: dict[str, dict[str, int]] = {}
    for name in outputs:
        if name == "area_mm2":
            continue
        m = bundle.models[name]
        s: dict[str, int] = {}
        if m.chosen == "lgb":
            s["point"] = len(ens); ens.append(m.point)
        if m.conformal["method"] == "cqr":
            s["lo"] = len(ens); ens.append(m.q_lo)
            s["hi"] = len(ens); ens.append(m.q_hi)
        slots[name] = s
    T, n_trees = add_tree_ensemble(g, F, ens) if ens else (None, 0)
    outs = []
    for name in outputs:
        sn = safe(name)
        if name == "area_mm2":
            a = area_graph(g, x)
            for suf in ("", "__lo", "__hi"):
                g.op("Identity", [a], out=sn + suf)
                outs.append(sn + suf)
            continue
        m = bundle.models[name]
        s = slots[name]
        if m.chosen == "lgb":
            z = g.col(T, s["point"])
        else:
            if M is None:
                M = mlp_feature_graph(g, x)
            z = mlp_graph(g, M, m.point)
        q = float(m.conformal["qhat"][str(alpha)])
        q = q if math.isfinite(q) else 1e6
        if m.conformal["method"] == "cqr":
            lo = g.op("Min", [g.op("Sub", [g.col(T, s["lo"]), g.const([q])]), z])
            hi = g.op("Max", [g.op("Add", [g.col(T, s["hi"]), g.const([q])]), z])
        else:
            lo = g.op("Sub", [z, g.const([q])])
            hi = g.op("Add", [z, g.const([q])])
        inverse_graph(g, name, z, sn)
        inverse_graph(g, name, lo, sn + "__lo")
        inverse_graph(g, name, hi, sn + "__hi")
        outs += [sn, sn + "__lo", sn + "__hi"]
    graph = helper.make_graph(
        g.nodes, "glasschip_surrogate",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["N", len(INPUTS)])],
        [helper.make_tensor_value_info(o, TensorProto.FLOAT, ["N", 1]) for o in outs], g.inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17), helper.make_opsetid("ai.onnx.ml", 3)],
                              producer_name="glasschip-surrogate")
    model.ir_version = 8
    onnx.checker.check_model(model)
    model.doc_string = f"{n_trees} trees"
    return model


def input_meta() -> list[dict[str, Any]]:
    from surrogate.chiprun import VX1_DEFAULT
    from surrogate.dataset import POWER_POLICIES, SCENARIOS, SCHEDULERS, TRANSPORTS
    defaults = {"noc_bits": NOC_BITS.index(256), "scheduler": SCHEDULERS.index("edf"),
                "power_policy": POWER_POLICIES.index("pace_to_deadline"), "transport": 0, "scenario": 0}
    choices = {"noc_bits": [str(b) for b in NOC_BITS], "scheduler": ["FIFO", "Rate monotonic", "EDF", "EDF mixed criticality"],
               "power_policy": ["P0 nominal", "P3 race to idle", "P4 pace to deadline"],
               "transport": ["copy", "zero-copy"], "scenario": ["S1 nominal", "S2 second camera", "S3 human enters", "S6 fifteen at once"],
               "cache_partition": ["off", "on"], "qos": ["off", "on"]}
    out = []
    for k in INPUTS:
        kind, lo, hi, label, unit = SPACE[k]
        d = defaults.get(k, VX1_DEFAULT.get(k))
        if k == "dram_gbps":
            d = 136.0
        if k == "bitrate_gbps":
            d = 0.2
        step = {"int": 1, "int8": 8, "cat": 1, "choice": 1}.get(kind, round((hi - lo) / 100, 4))
        item: dict[str, Any] = {"name": k, "label": label, "unit": unit if unit not in ("code", "off/on") else "",
                                "min": lo, "max": hi, "step": step, "default": float(d)}
        if k in choices:
            item["choices"] = choices[k]
        if kind == "logfloat":
            item["scale"] = "log"
        if k == "lv_arith_factor":
            item["note"] = "hypothetical: 1x is off; other values represent the class of techniques Velaura claims for Titan Core, not their technology"
        out.append(item)
    return out


def export(alpha: float = 0.1, outputs: list[str] | None = None, bundle: Any = None,
           out_dir: Path = OUT_DIR) -> dict[str, Any]:
    from surrogate.conformal import load_bundle
    bundle = bundle or load_bundle()
    outputs = [o for o, _, _ in WEB_OUTPUTS] if outputs is None else outputs
    outputs = [o for o in outputs if o == "area_mm2" or o in bundle.models]
    model = build(bundle, outputs, alpha)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "surrogate.onnx"
    onnx.save(model, path)
    size = path.stat().st_size
    labels = {o: (l, u) for o, l, u in WEB_OUTPUTS}
    rep = bundle.report
    out_meta = []
    for o in outputs:
        l, u = labels.get(o, (o, ""))
        item = {"name": safe(o), "key": o, "label": l, "unit": u, "lo": safe(o) + "__lo", "hi": safe(o) + "__hi"}
        if o == "area_mm2":
            item.update(method="closed form (rough proxy)", exact=True)
        else:
            r = rep[o]
            a = r.get(f"alpha_{alpha}", {})
            item.update(method="CQR" if r["method"] == "cqr" else "split conformal", model=r["chosen"],
                        qhat=r["qhat"][str(alpha)], test_coverage=a.get("coverage"), test_coverage_ci95=a.get("ci95"),
                        n_calib=r["n_calib"], n_test=r["n_test"])
            from surrogate.conformal import DEADLINES_MS, FALLBACK_RATE, FALLBACK_REL, task_of
            if o.endswith("miss_rate"):
                item["uncertain_if_width_gt"] = FALLBACK_RATE
            elif o.endswith("_ms") and task_of(o) in DEADLINES_MS:
                item["deadline_ms"] = DEADLINES_MS[task_of(o)]
                item["uncertain_if_width_gt"] = FALLBACK_REL * DEADLINES_MS[task_of(o)]
        out_meta.append(item)
    return {"model": "surrogate.onnx", "model_bytes": size, "doc": model.doc_string, "outputs": out_meta,
            "path": str(path)}


def write_meta(alpha: float = 0.1) -> dict[str, Any]:
    """Export the ONNX model and write viz/public/surrogate/meta.json (shape: viz/src/dse/surrogate.ts)."""
    from surrogate.conformal import FALLBACK_RATE, FALLBACK_REL, load_bundle
    from surrogate.dataset import load_dataset, provenance
    bundle = load_bundle()
    ex = export(alpha, bundle=bundle)
    covs = [o["test_coverage"] for o in ex["outputs"] if o.get("test_coverage") is not None]
    df = load_dataset()
    prov = provenance()
    prov.update({"exporter": "surrogate/export_onnx.py", "n_samples": int(len(df)),
                 "n_lhs": int((df["phase"] == "lhs").sum()), "n_active": int((df["phase"] != "lhs").sum()),
                 "split": {k: len(v) for k, v in bundle.split.items()}})
    meta: dict[str, Any] = {
        "model": ex["model"], "model_bytes": ex["model_bytes"], "input_name": "x",
        "inputs": input_meta(), "outputs": ex["outputs"],
        "coverage": 1 - alpha,
        "coverage_achieved": {"min": min(covs), "median": float(np.median(covs)), "max": max(covs),
                              "per_output": "see outputs[].test_coverage (held-out test split, Clopper-Pearson 95% CI)"},
        "fallback_rule": {"latency": f"uncertain, run full model when interval width > {FALLBACK_REL} x deadline",
                          "miss_rate": f"uncertain, run full model when interval width > {FALLBACK_RATE}"},
        "guarantee": ("Split conformal / CQR: marginal coverage >= nominal over design points drawn like the "
                      "calibration set (exchangeability). Not conditional on a specific design, and not valid "
                      "outside the sampled ranges or for a changed simulator."),
        "labels": ["VX-1 is a fictional reference SoC", "area_mm2 is a rough proxy",
                   "low-voltage arithmetic factor above 1x is hypothetical",
                   "trained on chip-only runs at the interactive fidelity mix (1.2 s simulated per sample)"],
        "provenance": prov,
    }
    s12 = ROOT / "results" / "s12.json"
    if s12.exists():
        r = json.loads(s12.read_text())
        if "pareto_for_viz" in r:
            meta["pareto"] = r["pareto_for_viz"]
        if "recommended" in r:
            meta["recommended"] = [{"name": c["name"], "inputs": c["inputs_encoded"]} for c in r["recommended"]]
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


if __name__ == "__main__":
    m = write_meta()
    print(json.dumps({k: m[k] for k in ("model", "model_bytes", "coverage", "coverage_achieved")}, indent=1))
