"""Cross-fidelity microbenchmarks for study s10 (spec 11.2).

Each microbenchmark is one or more small task graphs in the Glass Chip IR, written to
calib/graphs/mb/<name>.json. A microbenchmark with several graphs releases all of them at
the same instant (used for the all-to-one hotspot). Op sizes are taken from the real
workloads where possible (VLA and detector layer shapes, the control kernels' instruction
counts) so the benchmarks exercise the same paths as the scenarios.

    .venv/bin/python calib/microbench.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "graphs" / "mb"

# The control-kernel microbenchmarks copy the instruction mix of the matching op in the workload
# graphs (T7_control, T6_estimate, T8_safety), which now carry gem5-measured dynamic counts, so
# L0/L1/L2 see the same inputs as the scenarios.
GRAPHS = ROOT / "workloads" / "graphs"
KERNEL_OPS = {"control_step": ("T7_control", "arm1.control_step"),
              "estimate_step": ("T6_estimate", "est.step"),
              "ssm_step": ("T8_safety", "rt.ssm")}


def graph(task_id: str, name: str, ops: list[dict[str, Any]], period_us: float = 100_000,
          crit: str = "high") -> dict[str, Any]:
    prev = None
    for o in ops:
        o.setdefault("dtype", "int8")
        for k in ("macs", "flops", "bytes_in", "bytes_w", "bytes_out"):
            o.setdefault(k, 0)
        if prev and "depends_on" not in o:
            o["depends_on"] = [prev]
        prev = o["op_id"]
    return {
        "schema_version": "1.0", "task_id": task_id, "name": name,
        "provenance": {"source": "synthetic microbenchmark", "captured_by": "calib/microbench.py",
                       "date": "generated", "method": "config_derived"},
        "release": {"kind": "periodic", "period_us": period_us, "on_overrun": "queue"},
        "deadline_us": period_us, "criticality": crit,
        "qos_class": {"safety": "safety", "high": "realtime"}.get(crit, "best_effort"),
        "ops": ops,
    }


def mm(op_id: str, M: int, K: int, N: int, heads: int = 1, dtype: str = "int8",
       weights: bool = True) -> dict[str, Any]:
    b = 1 if dtype in ("int8", "fp8") else 2
    return {"op_id": op_id, "type": "matmul", "dtype": dtype,
            "shape": {"M": M, "K": K, "N": N, "heads": heads},
            "macs": M * K * N * heads, "bytes_in": M * K * heads * b,
            "bytes_w": K * N * heads * b if weights else 0,
            "bytes_out": M * N * heads * b, "eligible": ["npu"], "preempt_boundary": True}


def conv(op_id: str, C: int, H: int, W: int, K: int, R: int, stride: int = 1,
         groups: int = 1) -> dict[str, Any]:
    Ho, Wo = H // stride, W // stride
    t = "dwconv2d" if groups > 1 and groups == C else "conv2d"
    macs = Ho * Wo * K * R * R * C // groups
    return {"op_id": op_id, "type": t, "dtype": "int8",
            "shape": {"N": 1, "C": C, "H": H, "W": W, "K": K, "R": R, "S": R, "stride": stride,
                      "groups": groups, "Ho": Ho, "Wo": Wo},
            "macs": macs, "bytes_in": C * H * W, "bytes_w": K * R * R * C // groups,
            "bytes_out": K * Ho * Wo, "eligible": ["npu"], "preempt_boundary": True}


def build() -> dict[str, list[dict[str, Any]]]:
    mbs: dict[str, list[dict[str, Any]]] = {}

    # Memory system
    mbs["stream_copy_dma_8MB"] = [graph("T60_copy_dma", "DMA streaming copy 8 MiB", [
        {"op_id": "copy", "type": "memcopy", "dtype": "int8", "bytes_in": 8 << 20,
         "bytes_out": 8 << 20, "eligible": ["dma"]}])]
    mbs["stream_copy_cpu_2MB"] = [graph("T61_copy_cpu", "CPU streaming copy 2 MiB", [
        {"op_id": "copy", "type": "memcopy", "dtype": "int8", "bytes_in": 2 << 20,
         "bytes_out": 2 << 20, "eligible": ["cpu"]}])]
    mbs["random_access_cpu_64B"] = [graph("T62_random_cpu", "CPU pointer-chase style 64 B misses over 16 MiB", [
        {"op_id": "chase", "type": "cpu_kernel", "dtype": "fp32", "bytes_in": 16 << 20,
         "bytes_out": 0, "instr": {"scalar": 262144, "vector": 0, "mem": 262144}, "reuse": 0.0,
         "eligible": ["cpu"], "notes": "one 64 B line per load, no reuse; the core has no address "
                                       "pattern field, so randomness is not represented"}])]
    hot = []
    for i, (eng, typ) in enumerate([("dma", "memcopy"), ("simt", "elementwise"), ("dsp", "elementwise"),
                                    ("npu", "elementwise"), ("cpu", "memcopy"), ("dma", "memcopy")]):
        hot.append(graph(f"T63_hot{i}", f"hotspot stream {i} on {eng}", [
            {"op_id": f"hot{i}", "type": typ, "dtype": "int8", "bytes_in": 4 << 20,
             "bytes_out": 4 << 20, "flops": 4 << 20, "eligible": [eng]}]))
    mbs["all_to_one_hotspot_6x4MB"] = hot
    mbs["npu_elementwise_16MB"] = [graph("T64_npu_vec", "NPU vector add 16 MiB (bandwidth bound)", [
        {"op_id": "add", "type": "elementwise", "dtype": "int8", "bytes_in": 32 << 20,
         "bytes_out": 16 << 20, "flops": 16 << 20, "eligible": ["npu"]}])]

    # GEMM sizes (VLA and LM shapes from T4_vla)
    for name, (M, K, N, dt) in {
        "gemm_64": (64, 64, 64, "int8"),
        "gemm_256": (256, 256, 256, "int8"),
        "gemm_vit_1024x768x768": (1024, 768, 768, "int8"),
        "gemm_vit_mlp_1024x768x3072": (1024, 768, 3072, "int8"),
        "gemm_lm_177x960x5120": (177, 960, 5120, "int8"),
        "gemm_lm_decode_1x960x5120_bf16": (1, 960, 5120, "bf16"),
    }.items():
        mbs[name] = [graph(f"T65_{name}", f"GEMM {M}x{K}x{N} {dt}", [mm("mm", M, K, N, dtype=dt)])]

    # Attention (ViT layer shapes: 12 heads, 1024 tokens, head dim 64)
    mbs["attention_vit_12h_1024"] = [graph("T66_attn", "attention QK^T, softmax, AV", [
        mm("qk", 1024, 64, 1024, heads=12, weights=False),
        {"op_id": "softmax", "type": "softmax", "dtype": "bf16", "bytes_in": 12 * 1024 * 1024 * 2,
         "bytes_out": 12 * 1024 * 1024 * 2, "flops": 5 * 12 * 1024 * 1024, "eligible": ["npu"]},
        mm("av", 1024, 1024, 64, heads=12, weights=False)])]

    # Convolutions (detector and segmentation layer shapes)
    mbs["conv_stem_640_s2"] = [graph("T67_conv_stem", "conv 3->16 3x3 s2 at 640x640",
                                     [conv("c", 3, 640, 640, 16, 3, 2)])]
    mbs["conv3x3_64_160"] = [graph("T67_conv33", "conv 64->64 3x3 at 160x160",
                                   [conv("c", 64, 160, 160, 64, 3)])]
    mbs["conv1x1_960_15x20"] = [graph("T67_conv11", "conv 960->160 1x1 at 15x20",
                                      [conv("c", 960, 15, 20, 160, 1)])]
    mbs["dwconv3x3_72_120x160"] = [graph("T67_dw", "depthwise 3x3, 72 ch at 120x160",
                                         [conv("c", 72, 120, 160, 72, 3, groups=72)])]

    # cuRobo-style collision kernel: 64 seeds x 32 timesteps x 61 spheres against an ESDF.
    q = 64 * 32 * 61
    mbs["curobo_collision_simt"] = [graph("T68_collision", "sphere-vs-ESDF collision cost (SIMT)", [
        {"op_id": "collide", "type": "simt_kernel", "dtype": "fp32", "flops": q * 60,
         "bytes_in": q * 16 + q * 8 * 4, "bytes_out": q * 4,
         "shape": {"threads": q, "kernel_launches": 1}, "eligible": ["simt"],
         "notes": "per sphere: trilinear ESDF lookup (8 voxels x 4 B), distance, cost; 60 flops"}])]

    # Control loop and friends (instruction mixes from the compiled kernels)
    def instr(fn: str) -> dict[str, int]:
        tid, oid = KERNEL_OPS[fn]
        g = json.loads((GRAPHS / f"{tid}.json").read_text())
        op = next(o for o in g["ops"] if o["op_id"] == oid)
        return {k: int(v) for k, v in op["instr"].items()}

    mbs["control_step_rt"] = [graph("T7_mb_ctrl_rt", "T7 control_step on the RT core", [
        {"op_id": "control_step", "type": "control_law", "dtype": "fp32",
         "instr": instr("control_step"), "bytes_in": 640, "bytes_out": 168, "eligible": ["rt"]}],
        period_us=1000, crit="safety")]
    mbs["control_step_cpu"] = [graph("T7_mb_ctrl_cpu", "T7 control_step on an app core", [
        {"op_id": "control_step", "type": "control_law", "dtype": "fp32",
         "instr": instr("control_step"), "bytes_in": 640, "bytes_out": 168, "reuse": 1.0,
         "eligible": ["cpu"]}], period_us=1000)]
    mbs["estimate_step_cpu"] = [graph("T6_mb_est_cpu", "T6 estimate_step on an app core", [
        {"op_id": "estimate_step", "type": "cpu_kernel", "dtype": "fp32",
         "instr": instr("estimate_step"), "bytes_in": 4096, "bytes_out": 4096, "reuse": 1.0,
         "eligible": ["cpu"]}], period_us=5000)]
    mbs["ssm_step_rt"] = [graph("T8_mb_ssm_rt", "T8 SSM on the RT core", [
        {"op_id": "ssm_step", "type": "control_law", "dtype": "fp32",
         "instr": instr("ssm_step"), "bytes_in": 1744, "bytes_out": 64, "eligible": ["rt"]}],
        period_us=10000, crit="safety")]
    return mbs


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mbs = build()
    index = {}
    for name, gs in mbs.items():
        index[name] = [g["task_id"] for g in gs]
        for g in gs:
            (OUT / f"{g['task_id']}.json").write_text(json.dumps(g, indent=1) + "\n")
    (OUT / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    print(f"{len(mbs)} microbenchmarks, {sum(len(v) for v in mbs.values())} graphs -> {OUT}")


if __name__ == "__main__":
    main()
