"""T2_detect: YOLOv8n (ultralytics) exported to ONNX at 640x640, walked op by op,
plus CPU post-processing (score decode, NMS, depth-based pose refinement)
counted analytically.

Run: .venv/bin/python workloads/capture/detect.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import onnx

from common import (CPU, MODELS, chain, cpu_instr_estimate, make_graph, op, provenance,
                    sha256_file, write_graph)
from onnx_walk import WalkConfig, walk

SCRIPT = "workloads/capture/detect.py"
WEIGHTS = "yolov8n.pt"
IMGSZ = 640
OPSET = 17

# Post-processing assumptions (labeled in the graph notes).
NUM_CLASSES = 80
CONF_CANDIDATES = 200      # boxes surviving the confidence threshold before NMS (assumption)
NUM_OBJECTS = 8            # objects tracked on the conveyor per frame (assumption)
CROP_PIXELS = 60 * 60      # depth pixels per object crop at 640x480 (assumption)
ICP_ITERS = 5              # point-to-plane refinement iterations per object (assumption)


def export_onnx() -> Path:
    """Export yolov8n to ONNX once; reuse the cached file afterwards."""
    MODELS.mkdir(parents=True, exist_ok=True)
    out = MODELS / "yolov8n.onnx"
    if not out.exists():
        import os
        from ultralytics import YOLO
        cwd = os.getcwd()
        os.chdir(MODELS)
        try:
            YOLO(WEIGHTS).export(format="onnx", imgsz=IMGSZ, opset=OPSET, simplify=False, dynamic=False)
        finally:
            os.chdir(cwd)
    return out


def layer_fn(name: str) -> str:
    parts = name.strip("/").split("/")
    if parts and parts[0] == "model.22":
        # split the Detect head per branch; flat decode ops share one layer
        return f"{parts[0]}.{parts[1]}" if len(parts) > 2 else f"{parts[0]}.decode"
    return parts[0] if parts else "net"


def post_ops(dep: str, out_elems: int) -> list[dict]:
    """CPU post-processing after the NPU part, counted analytically."""
    A = out_elems // (4 + NUM_CLASSES)  # anchors (8400 at 640)
    ops = []
    f = A * NUM_CLASSES + A * 4 + A                      # class max + box dequant + threshold
    b = out_elems * 1 + CONF_CANDIDATES * 6 * 4
    ops.append(op("post.decode", "cpu_kernel", CPU, dtype="fp32", flops=f, bytes_in=out_elems, bytes_out=CONF_CANDIDATES * 24,
                  instr=cpu_instr_estimate(f, b, 0.8), depends_on=[dep],
                  notes=f"dequantize int8 head output, per-anchor class max over {NUM_CLASSES} classes, confidence threshold; {A} anchors"))
    K = CONF_CANDIDATES
    f = K * math.log2(K) * 4 + (K * K / 2) * 15
    ops.append(op("post.nms", "cpu_kernel", CPU, dtype="fp32", flops=f, bytes_in=K * 24, bytes_out=NUM_OBJECTS * 24,
                  instr=cpu_instr_estimate(f, K * 24 * K / 8, 0.3),
                  notes=f"sort {K} candidates then greedy NMS, worst case K^2/2 IoU tests at ~15 flops each"))
    P = CROP_PIXELS
    per_obj = P * 8 + P * 9 * 2 + 600 + ICP_ITERS * P * 60
    f = NUM_OBJECTS * per_obj
    depth_bytes = NUM_OBJECTS * P * 2
    ops.append(op("post.pose_refine", "cpu_kernel", CPU, dtype="fp32", flops=f, bytes_in=depth_bytes,
                  bytes_out=NUM_OBJECTS * 7 * 4, instr=cpu_instr_estimate(f, depth_bytes * (1 + ICP_ITERS) * 6, 0.7),
                  notes=(f"per object: deproject {P} depth px, centroid+PCA (3x3 covariance, Jacobi eigen), "
                         f"{ICP_ITERS} ICP iterations at ~60 flops/point; {NUM_OBJECTS} objects")))
    return chain(ops)


def build() -> dict:
    path = export_onnx()
    m = onnx.load(str(path))
    opset = max(o.version for o in m.opset_import if o.domain in ("", "ai.onnx"))
    import ultralytics
    cfg = WalkConfig(prefix="yolo", layer_fn=layer_fn)
    ops, stats = walk(str(path), cfg)
    out_shape = [d.dim_value for d in m.graph.output[0].type.tensor_type.shape.dim]
    out_elems = 1
    for d in out_shape:
        out_elems *= d
    ops += post_ops(ops[-1]["op_id"], out_elems)
    pt = MODELS / WEIGHTS
    weights_bytes = sum(o.get("bytes_w", 0) for o in ops)
    peak_act = max(o.get("bytes_in", 0) + o.get("bytes_out", 0) for o in ops)
    prov = provenance(
        source="ultralytics YOLOv8n (COCO-pretrained) exported to ONNX",
        script=SCRIPT, method="mixed",
        model_name="yolov8n", model_version=f"ultralytics {ultralytics.__version__}, onnx opset {opset}",
        model_hash=f"sha256:{sha256_file(path)} (onnx); pt sha256:{sha256_file(pt) if pt.exists() else 'n/a'}",
        notes=(f"NPU part: onnx_graph_walk of the real exported graph ({stats['onnx_nodes']} ONNX nodes, "
               f"{stats['dropped_metadata']} metadata nodes dropped, {stats['fused']} activations fused, "
               f"{stats['kept_ops']} ops kept). dtype int8 is a deployment assumption (W8A8, int32 bias); "
               f"the export itself is fp32. Layers follow ultralytics module index (model.N), Detect head split per branch. "
               f"CPU post-processing is analytic_count with assumptions: {CONF_CANDIDATES} pre-NMS candidates, "
               f"{NUM_OBJECTS} objects, {CROP_PIXELS} depth px per crop, {ICP_ITERS} ICP iterations. Output {out_shape}."),
    )
    msgs = [{"topic": "/perception/detections", "bytes": 64 + NUM_OBJECTS * 200, "consumer": "T4_vla"},
            {"topic": "/perception/object_poses", "bytes": 64 + NUM_OBJECTS * 72, "consumer": "T6_estimate"}]
    g = make_graph("T2_detect", "Object detection + pose (YOLOv8n 640x640, int8 NPU, CPU post)", prov, ops,
                   messages=msgs, footprint={"weights_bytes": weights_bytes, "working_set_bytes": peak_act})
    return g


def main() -> None:
    g = build()
    p = write_graph(g)
    # S2: the overhead camera also feeds detection; same graph, separate task instance.
    g2 = json.loads(json.dumps(g))
    g2["task_id"] = "T2_detect_overhead"
    g2["name"] = "Object detection + pose on the overhead camera (second instance of T2_detect)"
    g2["release"]["offset_us"] = 18000
    for o in g2["ops"]:
        o["op_id"] = "ovh." + o["op_id"]
        if "depends_on" in o:
            o["depends_on"] = ["ovh." + d for d in o["depends_on"]]
    g2["provenance"]["notes"] += " This file is a renamed copy of T2_detect for the overhead camera (scenario S2)."
    write_graph(g2)
    s = g["summary"]
    print(f"T2_detect: {s['num_ops']} ops, {s['macs']/1e9:.3f} GMAC, weights {g['footprint']['weights_bytes']/1e6:.2f} MB -> {p}")


if __name__ == "__main__":
    main()
