"""Task graphs for public MLPerf Edge models, for the Orin-like sanity check (study s15).

I build the same three networks MLPerf Inference Edge uses and NVIDIA reports for Jetson AGX
Orin (calib/tables/orin_public.json), export them to ONNX (opset 17) and walk them with the
same ONNX walker the workloads use (workloads/capture/onnx_walk.py):

* ResNet-50 v1.5, 1x3x224x224 (torchvision resnet50; v1.5 = stride 2 in the 3x3 conv).
* RetinaNet as in MLPerf: ResNeXt50-32x4d FPN backbone (P3-P7), 9 anchors, 264 OpenImages
  classes, 1x3x800x800. Box decoding and NMS are not part of the graph (TensorRT runs them as
  plugins; MLPerf's NVIDIA submission offloads NMS), which I note in the graph.
* BERT-Large (24 layers, hidden 1024, 16 heads, FFN 4096), sequence length 384, with the SQuAD
  span head (Linear 1024 -> 2).

Weights are random: only shapes matter for op counting, not values. Precision is int8 for the
walk, matching the INT8 tensor-core path; this is an assumption for BERT, where NVIDIA's
submission mixes INT8 and FP16 (recorded as unverified in orin_public.json).

    .venv/bin/python calib/public_models.py
writes data/calib/models/*.onnx and calib/graphs/T9x_*.json
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "workloads" / "capture"))

from common import finalize_op, totals  # noqa: E402
from onnx_walk import WalkConfig, walk  # noqa: E402

MODELS = ROOT / "data" / "calib" / "models"
GRAPHS = HERE / "graphs"
OPSET = 17


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _export(model: Any, args: tuple, path: Path, names: list[str]) -> Path:
    import torch

    MODELS.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    model.eval()
    with torch.no_grad():
        torch.onnx.export(model, args, str(path), opset_version=OPSET, input_names=names,
                          dynamo=False, do_constant_folding=True)
    return path


def export_resnet50() -> Path:
    import torch
    import torchvision

    torch.manual_seed(0)
    m = torchvision.models.resnet50(weights=None)
    return _export(m, (torch.randn(1, 3, 224, 224),), MODELS / "resnet50_224.onnx", ["image"])


def export_retinanet() -> Path:
    import torch
    from torchvision.models.detection.backbone_utils import resnet_fpn_backbone
    from torchvision.models.detection.retinanet import RetinaNetHead
    from torchvision.ops.feature_pyramid_network import LastLevelP6P7

    torch.manual_seed(0)

    class RetinaNetCore(torch.nn.Module):
        """Backbone + FPN + heads; anchor decode and NMS stay outside, as in TensorRT."""

        def __init__(self) -> None:
            super().__init__()
            self.backbone = resnet_fpn_backbone(
                backbone_name="resnext50_32x4d", weights=None, trainable_layers=5,
                returned_layers=[2, 3, 4], extra_blocks=LastLevelP6P7(256, 256))
            self.head = RetinaNetHead(256, 9, 264)

        def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
            feats = list(self.backbone(x).values())
            out = self.head(feats)
            return out["cls_logits"], out["bbox_regression"]

    return _export(RetinaNetCore(), (torch.randn(1, 3, 800, 800),),
                   MODELS / "retinanet_rx50_fpn_800.onnx", ["image"])


def export_bert_large() -> Path:
    import torch
    from transformers import BertConfig, BertModel

    torch.manual_seed(0)
    cfg = BertConfig(hidden_size=1024, num_hidden_layers=24, num_attention_heads=16,
                     intermediate_size=4096, max_position_embeddings=512)

    class BertQA(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.bert = BertModel(cfg, add_pooling_layer=False)
            self.qa = torch.nn.Linear(1024, 2)

        def forward(self, ids: torch.Tensor, mask: torch.Tensor, seg: torch.Tensor) -> torch.Tensor:
            h = self.bert(input_ids=ids, attention_mask=mask, token_type_ids=seg).last_hidden_state
            return self.qa(h)

    seq = 384
    args = (torch.ones(1, seq, dtype=torch.long), torch.ones(1, seq, dtype=torch.long),
            torch.zeros(1, seq, dtype=torch.long))
    return _export(BertQA(), args, MODELS / "bert_large_384.onnx", ["input_ids", "attention_mask",
                                                                      "token_type_ids"])


def layer_fn_torch(name: str) -> str:
    parts = [p for p in name.strip("/").split("/") if p]
    if not parts:
        return "net"
    # Group by the first two module levels (e.g. layer1/layer1.0, bert/encoder/layer.3).
    keep = [p for p in parts[:-1]][:3]
    return "/".join(keep) if keep else parts[0]


def build(task_id: str, name: str, onnx_path: Path, source: str, notes: str,
          public_key: str, prefix: str) -> dict[str, Any]:
    ops, stats = walk(str(onnx_path), WalkConfig(prefix=prefix, layer_fn=layer_fn_torch))
    for o in ops:
        finalize_op(o)
    g = {
        "schema_version": "1.0",
        "task_id": task_id,
        "name": name,
        "provenance": {
            "source": source,
            "captured_by": "calib/public_models.py",
            "date": dt.date.today().isoformat(),
            "method": "onnx_graph_walk",
            "model_name": public_key,
            "model_hash": f"sha256:{_sha(onnx_path)} (onnx, random weights)",
            "notes": (f"{stats['onnx_nodes']} ONNX nodes, {stats['kept_ops']} ops kept, "
                      f"{stats['fused']} activations fused. dtype int8 is a deployment assumption. "
                      + notes),
        },
        "release": {"kind": "event", "on_overrun": "queue"},
        "deadline_us": 1_000_000,
        "criticality": "best_effort",
        "qos_class": "best_effort",
        "ops": ops,
        "summary": totals(ops),
    }
    return g


def main() -> None:
    GRAPHS.mkdir(parents=True, exist_ok=True)
    specs = [
        ("T90_resnet50", "ResNet-50 v1.5 224x224 (MLPerf Edge image classification)",
         export_resnet50, "torchvision resnet50, random weights",
         "Matches MLPerf ResNet-50 v1.5 input 224x224, batch 1.", "ResNet-50 v1.5", "rn50"),
        ("T91_retinanet", "RetinaNet ResNeXt50-FPN 800x800 (MLPerf Edge object detection)",
         export_retinanet, "torchvision resnet_fpn_backbone(resnext50_32x4d) + RetinaNetHead(264 classes)",
         "Anchor decode and NMS excluded (plugins on TensorRT, NMS offloaded in NVIDIA's MLPerf "
         "submission).", "RetinaNet (ResNeXt50 FPN)", "retina"),
        ("T92_bert_large", "BERT-Large seq 384 + SQuAD span head (MLPerf Edge NLP)",
         export_bert_large, "transformers BertModel(large config) + Linear(1024, 2), random weights",
         "Sequence length 384, batch 1.", "BERT-Large", "bert"),
    ]
    for tid, name, exp, src, notes, key, prefix in specs:
        p = exp()
        g = build(tid, name, p, src, notes, key, prefix)
        out = GRAPHS / f"{tid}.json"
        out.write_text(json.dumps(g, indent=1) + "\n")
        s = g["summary"]
        print(f"{tid}: {s['num_ops']} ops, {s['macs'] / 1e9:.2f} GMAC, weights "
              f"{s['bytes_w'] / 1e6:.1f} MB -> {out}")


if __name__ == "__main__":
    main()
