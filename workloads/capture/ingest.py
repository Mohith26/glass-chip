"""T1_ingest_wrist and T1_ingest_overhead: camera ingest and ISP-like preprocessing.

Each camera delivers 640x480 rgb8 + 640x480 16UC1 depth at 30 Hz. The DSP kernels
are counted per pixel analytically (method analytic_count); the DMA ops move the
frames into DRAM. Output sizes match the model inputs of the consumers:
YOLOv8n 640x640 (T2), SmolVLA 512x512 (T4), LR-ASPP 320x240 (T8, overhead only).

Run: .venv/bin/python workloads/capture/ingest.py
"""
from __future__ import annotations

from common import DMA, DSP, make_graph, op, provenance, write_graph

SCRIPT = "workloads/capture/ingest.py"
W, H = 640, 480
RGB = W * H * 3
DEPTH = W * H * 2

# Per-pixel op counts (labeled assumptions, simple arithmetic of each kernel).
ALIGN_OPS = 40          # deproject (6) + 3x4 rigid transform (21) + project with 2 divides (10) + z-buffer (3)
BILINEAR_OPS = 10       # 4 taps x (mul+add) + 2 normalize/quantize ops per output channel value
PAD_NORM_OPS = 2        # scale + round per output value (letterbox pad region is a constant fill)
BOX2_OPS = 6            # 2x2 average + normalize per output value


def graph(cam: str, feeds_detect: bool, feeds_safety: bool) -> dict:
    p = f"in.{cam}"
    ops = [
        op(f"{p}.dma_rgb", "memcopy", DMA, dtype="int8", bytes_in=RGB, bytes_out=RGB,
           notes="camera interface -> DRAM, 640x480 rgb8", depends_on=[]),
        op(f"{p}.dma_depth", "memcopy", DMA, dtype="int8", bytes_in=DEPTH, bytes_out=DEPTH,
           notes="camera interface -> DRAM, 640x480 16UC1", depends_on=[]),
        op(f"{p}.depth_align", "dsp_kernel", DSP, dtype="fp32", flops=W * H * ALIGN_OPS, bytes_in=DEPTH,
           bytes_out=DEPTH, shape={"in": [H, W], "out": [H, W], "ops_per_px": ALIGN_OPS},
           notes="register depth to the color camera (deproject, transform, project, z-buffer)",
           depends_on=[f"{p}.dma_depth"]),
    ]
    out_deps = [f"{p}.depth_align"]
    if feeds_detect:
        n = 640 * 640 * 3
        ops.append(op(f"{p}.prep_detect", "dsp_kernel", DSP, dtype="int8", flops=n * PAD_NORM_OPS, bytes_in=RGB,
                      bytes_out=n, shape={"in": [H, W, 3], "out": [640, 640, 3], "ops_per_value": PAD_NORM_OPS},
                      notes="letterbox to 640x640 (scale 1.0, pad 80 rows each side), normalize + int8 quantize",
                      depends_on=[f"{p}.dma_rgb"]))
        out_deps.append(f"{p}.prep_detect")
    n = 512 * 384 * 3
    ops.append(op(f"{p}.prep_vla", "dsp_kernel", DSP, dtype="int8", flops=n * BILINEAR_OPS + 512 * 128 * 3, bytes_in=RGB,
                  bytes_out=512 * 512 * 3, shape={"in": [H, W, 3], "out": [512, 512, 3], "ops_per_value": BILINEAR_OPS},
                  notes="SmolVLA resize_with_pad: bilinear 640x480 -> 512x384, pad to 512x512, x*2-1, quantize",
                  depends_on=[f"{p}.dma_rgb"]))
    out_deps.append(f"{p}.prep_vla")
    if feeds_safety:
        n = 320 * 240 * 3
        ops.append(op(f"{p}.prep_safety", "dsp_kernel", DSP, dtype="int8", flops=n * BOX2_OPS, bytes_in=RGB,
                      bytes_out=n, shape={"in": [H, W, 3], "out": [240, 320, 3]},
                      notes="2x2 downsample to 320x240 + ImageNet normalize + quantize for LR-ASPP",
                      depends_on=[f"{p}.dma_rgb"]))
        ops.append(op(f"{p}.depth_down", "dsp_kernel", DSP, dtype="int8", flops=320 * 240 * 4, bytes_in=DEPTH,
                      bytes_out=320 * 240 * 2, notes="2x2 min-pool of aligned depth for the safety monitor",
                      depends_on=[f"{p}.depth_align"]))
        out_deps += [f"{p}.prep_safety", f"{p}.depth_down"]
    ops.append(op(f"{p}.publish", "memcopy", DMA, dtype="int8", bytes_in=0, bytes_out=0,
                  notes="completion: hand buffers to consumers (zero-byte join; transport cost is modeled by middleware.json)",
                  depends_on=out_deps, preempt_boundary=True))
    det = "T2_detect" if cam == "wrist" else "T2_detect_overhead"
    msgs = [{"topic": f"/camera/{cam}/image", "bytes": RGB + 72, "consumer": "T4_vla"},
            {"topic": f"/camera/{cam}/depth", "bytes": DEPTH + 72, "consumer": "T3_esdf"}]
    if feeds_detect:
        msgs.append({"topic": f"/camera/{cam}/image", "bytes": RGB + 72, "consumer": det})
    if feeds_safety:
        msgs.append({"topic": f"/camera/{cam}/depth", "bytes": DEPTH + 72, "consumer": "T8_safety"})
    prov = provenance(source="ISP-like preprocessing for a 640x480 RGB-D camera", script=SCRIPT, method="analytic_count",
                      notes=(f"Per-pixel op counts are analytic assumptions (align {ALIGN_OPS}, bilinear {BILINEAR_OPS}, "
                             f"pad/normalize {PAD_NORM_OPS}, 2x2 box {BOX2_OPS} ops per value). Output sizes match the real "
                             "model inputs (YOLOv8n 640, SmolVLA resize_with_pad 512, LR-ASPP 320x240)."))
    return make_graph(f"T1_ingest_{cam}", f"Camera ingest + preprocess ({cam})", prov, ops, messages=msgs,
                      footprint={"weights_bytes": 0, "working_set_bytes": 2 * (RGB + DEPTH)})


def main() -> None:
    for g in (graph("wrist", True, False), graph("overhead", True, True)):
        write_graph(g)
        s = g["summary"]
        print(f"{g['task_id']}: {s['num_ops']} ops, {s['flops']/1e6:.1f} MOPs, {(s['bytes_in']+s['bytes_out'])/1e6:.1f} MB")


if __name__ == "__main__":
    main()
