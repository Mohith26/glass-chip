"""Write calib/tables/orin_public.json: public Jetson AGX Orin benchmark latencies.

These are transcribed by hand from public pages (URLs and access dates below) and are used
only as an external sanity check for configs/soc/orin_like.yaml. They are not validation of
any hardware, and nothing here says anything about VX-1 or any other real chip.

MLPerf Inference single-stream latency is the 90th-percentile latency of one-sample (batch 1)
queries. The NVIDIA page does not state per-model precision; NVIDIA's closed-division Jetson
submissions use TensorRT with reduced precision (INT8/FP16 per model). I did not verify the
per-model precision against the submission code, so it is recorded as "unverified".

    .venv/bin/python calib/orin_public.py
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCESSED = "2026-11-17"

NV_BENCH = "https://developer.nvidia.com/embedded/jetson-benchmarks"
MLC_V31 = "https://github.com/mlcommons/inference_results_v3.1/tree/main/closed/NVIDIA"
MLC_CK = "https://github.com/mlcommons/ck/blob/master/docs/mlperf/setup/setup-nvidia-jetson-orin.md"

COMMON_V31 = {
    "source_url": NV_BENCH,
    "reproduction_code": MLC_V31,
    "accessed": ACCESSED,
    "benchmark": "MLPerf Inference v3.1, Edge, Closed division (NVIDIA submission)",
    "system": "NVIDIA Jetson AGX Orin Developer Kit (64GB), MaxN power mode",
    "software": "JetPack 5.1.1, TensorRT 8.5.2, CUDA 11.4 (as stated on the source page)",
    "batch": "1 (single-stream scenario)",
    "latency_statistic": "90th percentile single-stream latency",
    "precision": "unverified; TensorRT reduced precision (INT8/FP16) per NVIDIA closed submissions",
}

ENTRIES = [
    {"model": "ResNet-50 v1.5", "task": "image classification", "input": "ImageNet 224x224",
     "single_stream_latency_ms": 0.64, "offline_samples_per_s": 6423.63,
     "multistream_latency_ms": 2.18, "glass_chip_relevance": "CNN backbone (T2/T8 class of ops)"},
    {"model": "RetinaNet (ResNeXt50 FPN)", "task": "object detection", "input": "OpenImages 800x800",
     "single_stream_latency_ms": 11.67, "offline_samples_per_s": 148.71,
     "multistream_latency_ms": 82.92, "glass_chip_relevance": "detector (closest public match to T2)"},
    {"model": "BERT-Large", "task": "question answering", "input": "SQuAD v1.1, seq len 384",
     "single_stream_latency_ms": 5.71, "offline_samples_per_s": 553.69,
     "glass_chip_relevance": "transformer encoder (closest public match to the T4 VLA backbone)"},
    {"model": "RNN-T", "task": "speech to text", "input": "LibriSpeech",
     "single_stream_latency_ms": 94.01, "offline_samples_per_s": 1169.98,
     "glass_chip_relevance": "speech model (S6 T11_speech class)"},
    {"model": "3D-UNet-99.0", "task": "medical image segmentation", "input": "KiTS 2019",
     "single_stream_latency_ms": 4371.46, "offline_samples_per_s": 0.51,
     "glass_chip_relevance": "large dense 3D conv; stress case only"},
]

# Independent reproduction of the v3.0 NVIDIA submission by the MLCommons taskforce (same board),
# useful as a spread estimate for the same models.
REPRO = [
    {"model": "ResNet-50 v1.5", "single_stream_latency_ms": 0.633479, "who": "NVIDIA submitted (v3.0, r35.3)"},
    {"model": "ResNet-50 v1.5", "single_stream_latency_ms": 0.659, "who": "MLCommons reproducibility taskforce (r35.3, 64k pages)"},
    {"model": "RetinaNet (ResNeXt50 FPN)", "single_stream_latency_ms": 13.924457, "who": "NVIDIA submitted (v3.0, r35.3)"},
    {"model": "RetinaNet (ResNeXt50 FPN)", "single_stream_latency_ms": 14.073, "who": "MLCommons reproducibility taskforce (r35.3, 64k pages)"},
    {"model": "BERT-Large", "single_stream_latency_ms": 5.635431, "who": "NVIDIA submitted (v3.0, r35.3)"},
    {"model": "BERT-Large", "single_stream_latency_ms": 6.31, "who": "MLCommons reproducibility taskforce (r35.3, 64k pages)"},
]


def main() -> None:
    out = {
        "provenance": {"script": "calib/orin_public.py", "date": dt.date.today().isoformat(),
                       "method": "hand transcription from public pages"},
        "purpose": "external sanity check for configs/soc/orin_like.yaml; target within 30% "
                   "(spec 11.3); not hardware validation",
        "results": [{**COMMON_V31, **e} for e in ENTRIES],
        "reproductions": [{**r, "source_url": MLC_CK, "accessed": ACCESSED,
                           "benchmark": "MLPerf Inference v3.0 single-stream, Jetson AGX Orin, MaxN"}
                          for r in REPRO],
    }
    p = HERE / "tables" / "orin_public.json"
    p.write_text(json.dumps(out, indent=1))
    print(p)


if __name__ == "__main__":
    main()
