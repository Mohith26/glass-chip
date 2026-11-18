"""T8_safety: human proximity from RGB-D plus speed-and-separation monitoring (SSM).

NPU part: torchvision LR-ASPP MobileNetV3-Large (COCO-with-VOC-labels weights,
which include a 'person' class) exported to ONNX at 320x240 and walked op by op.
DSP part: fuse the person mask with the aligned depth image (analytic per-pixel count).
RT part: SSM distance and speed scaling, written in C++ (workloads/capture/control/ssm.cpp),
cross-compiled to RV64 and counted statically (see control/count_rv64.py).

Run: .venv/bin/python workloads/capture/safety.py
"""
from __future__ import annotations

import json
from pathlib import Path

import onnx

from common import (DSP, MODELS, RT, WORKLOADS, chain, make_graph, op, provenance, sha256_file,
                    write_graph)
from onnx_walk import WalkConfig, walk

SCRIPT = "workloads/capture/safety.py"
H, W = 240, 320
OPSET = 17
HUMAN_POINTS = 64   # clustered human points handed to the RT core (assumption, matches ssm.cpp)


def export_onnx() -> tuple[Path, str]:
    MODELS.mkdir(parents=True, exist_ok=True)
    out = MODELS / f"lraspp_mobilenet_v3_large_{W}x{H}.onnx"
    import torchvision
    if not out.exists():
        import torch
        from torchvision.models.segmentation import LRASPP_MobileNet_V3_Large_Weights, lraspp_mobilenet_v3_large
        torch.manual_seed(0)
        m = lraspp_mobilenet_v3_large(weights=LRASPP_MobileNet_V3_Large_Weights.COCO_WITH_VOC_LABELS_V1).eval()

        class Wrap(torch.nn.Module):
            def __init__(self, mm: torch.nn.Module) -> None:
                super().__init__()
                self.m = mm

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return self.m(x)["out"]

        x = torch.zeros(1, 3, H, W)
        torch.onnx.export(Wrap(m), (x,), str(out), opset_version=OPSET, input_names=["image"],
                          output_names=["logits"], dynamo=False)
    return out, f"torchvision {torchvision.__version__}"


def layer_fn(name: str) -> str:
    parts = [p for p in name.strip("/").split("/") if p]
    parts = [p for p in parts if p != "m"]
    if len(parts) >= 3 and parts[0] == "backbone":
        return parts[1]
    if len(parts) >= 2 and parts[0] == "classifier":
        return "head"
    return "head"


def rt_counts() -> dict:
    p = WORKLOADS / "capture" / "control" / "rv64_counts.json"
    if not p.exists():
        raise SystemExit("run workloads/capture/control/count_rv64.py first (run_all.py does this)")
    return json.loads(p.read_text())["ssm_step"]


def build() -> dict:
    path, tv = export_onnx()
    ops, stats = walk(str(path), WalkConfig(prefix="seg", layer_fn=layer_fn))
    last = ops[-1]["op_id"]
    px = H * W
    # Mask x depth fusion on DSP: argmax over 21 classes, person test, depth lookup,
    # deprojection of person pixels, min-distance reduction, 64-point clustering by grid.
    f = px * 21 + px * 2 + px * 0.3 * 12 + px * 0.3 * 4
    ops.append(op("dsp.mask_depth_fuse", "dsp_kernel", DSP, dtype="int8", flops=f,
                  bytes_in=px * 21 + px * 2, bytes_out=HUMAN_POINTS * 12, depends_on=[last],
                  notes=("per pixel: argmax over 21 VOC classes, person test, depth lookup (aligned 16UC1 depth "
                         "downsampled to 320x240), deprojection for person pixels (30% coverage assumed), "
                         f"grid clustering to {HUMAN_POINTS} points. analytic_count")))
    rc = rt_counts()
    ops.append(op("rt.ssm", "control_law", RT, dtype="fp32", instr=rc["instr"], flops=rc["flops_est"],
                  bytes_in=HUMAN_POINTS * 12 + 61 * 16, bytes_out=64,
                  notes=(f"speed-and-separation monitoring (ISO/TS 15066 protective distance), {HUMAN_POINTS} human points "
                         f"x 61 robot spheres; instruction counts from clang RV64 static count of ssm.cpp "
                         f"({rc['instr_total']} instr per call, straight-line, no loops)")))
    chain(ops)
    prov = provenance(
        source="torchvision lraspp_mobilenet_v3_large (COCO_WITH_VOC_LABELS_V1) + SSM in C++",
        script=SCRIPT, method="mixed", model_name="lraspp_mobilenet_v3_large",
        model_version=f"{tv}, onnx opset {OPSET}, input 1x3x{H}x{W}",
        model_hash=f"sha256:{sha256_file(path)}",
        notes=(f"NPU part: onnx_graph_walk ({stats['onnx_nodes']} ONNX nodes, {stats['fused']} activations fused, "
               f"{stats['kept_ops']} ops). int8 is a deployment assumption. DSP mask/depth fusion: analytic_count. "
               "RT SSM: compiled RV64 static instruction count of workloads/capture/control/ssm.cpp."),
        references=["ISO/TS 15066:2016 speed and separation monitoring formula"],
    )
    msgs = [{"topic": "/safety/speed_scale", "bytes": 32, "consumer": "T7_control"}]
    w = sum(o.get("bytes_w", 0) for o in ops)
    return make_graph("T8_safety", "Safety monitor: person segmentation + SSM", prov, ops, messages=msgs,
                      footprint={"weights_bytes": w,
                                 "working_set_bytes": max(o.get("bytes_in", 0) + o.get("bytes_out", 0) for o in ops)})


def main() -> None:
    g = build()
    write_graph(g)
    s = g["summary"]
    print(f"T8_safety: {s['num_ops']} ops, {s['macs']/1e9:.3f} GMAC")


if __name__ == "__main__":
    main()
