"""S6 extra tasks: T11_speech, T13_map_save, T14_ota, T15_diag.
(T12_arm2_control is written by estimate_control.py from the compiled controller.)

T11_speech: Whisper-tiny. The encoder is the real architecture built from
openai/whisper-tiny config.json (random init; weights do not change the op graph),
exported to ONNX and walked. The log-mel front end (DSP) and a 12-token greedy
decode with KV cache are config_derived. T13-T15 are analytic estimates.

Run: .venv/bin/python workloads/capture/extras.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from common import (CPU, DMA, DSP, MODELS, NPU_MAC, NPU_VEC, SRC_CACHE, chain, cpu_instr_estimate, make_graph, op,
                    provenance, sha256_file, write_graph)
from onnx_walk import WalkConfig, walk

SCRIPT = "workloads/capture/extras.py"
WHISPER = "openai/whisper-tiny"
DECODE_TOKENS = 12        # short spoken command (assumption)


def whisper_cfg() -> tuple[dict, str]:
    from huggingface_hub import hf_hub_download, model_info
    d = SRC_CACHE / "whisper"
    d.mkdir(parents=True, exist_ok=True)
    p = hf_hub_download(WHISPER, "config.json", cache_dir=str(d / "hf"))
    return json.loads(Path(p).read_text()), model_info(WHISPER).sha


def export_encoder(cfgd: dict) -> Path:
    out = MODELS / "whisper_tiny_encoder.onnx"
    if out.exists():
        return out
    import torch
    from transformers import WhisperConfig
    from transformers.models.whisper.modeling_whisper import WhisperEncoder
    cfg = WhisperConfig(**cfgd)
    cfg._attn_implementation = "eager"
    torch.manual_seed(0)
    enc = WhisperEncoder(cfg).eval()

    class Enc(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.e = enc

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.e(input_features=x).last_hidden_state

    x = torch.zeros(1, cfg.num_mel_bins, 2 * cfg.max_source_positions)
    torch.onnx.export(Enc().eval(), (x,), str(out), opset_version=17, input_names=["mel"], output_names=["enc"],
                      dynamo=False)
    return out


def enc_layer(name: str) -> str:
    p = [x for x in name.strip("/").split("/") if x]
    if "layers" in "/".join(p):
        for x in p:
            if x.startswith("layers."):
                return x
    return "frontend" if any(x.startswith("conv") for x in p) else "out"


def speech_graph() -> dict:
    cfg, sha = whisper_cfg()
    path = export_encoder(cfg)
    D, L, Hh, F = cfg["d_model"], cfg["decoder_layers"], cfg["decoder_attention_heads"], cfg["decoder_ffn_dim"]
    V, S = cfg["vocab_size"], cfg["max_source_positions"]
    frames = 2 * S
    n_fft, mels = 400, cfg["num_mel_bins"]
    ops = []
    stft = frames * (5 * n_fft * math.log2(n_fft) + n_fft * 2)
    ops.append(op("asr.mel", "dsp_kernel", DSP, dtype="fp32", flops=stft + frames * (n_fft // 2 + 1) * mels * 2 + frames * mels * 6,
                  bytes_in=frames * 160 * 2, bytes_out=frames * mels * 1,
                  notes=f"log-mel front end: {frames} frames, n_fft {n_fft}, hop 160, {mels} mel bins (30 s padded window)",
                  depends_on=[]))
    eops, st = walk(str(path), WalkConfig(prefix="asr.enc", layer_fn=enc_layer))
    for o in eops:
        if not o["depends_on"]:
            o["depends_on"] = ["asr.mel"]
        if o.get("shape", {}).get("act_x_act") or o["type"] == "softmax":
            o["dtype"] = "bf16"
    ops += eops
    last = eops[-1]["op_id"]
    # Cross-attention K/V over the encoder output, computed once per utterance.
    ops.append(op("asr.dec.cross_kv", "matmul", NPU_MAC, dtype="int8", layer="asr.dec.cross_kv",
                  shape={"M": S, "K": D, "N": 2 * D, "layers": L}, macs=L * S * D * 2 * D,
                  bytes_in=S * D, bytes_w=L * D * 2 * D, bytes_out=L * 2 * S * D * 2, depends_on=[last],
                  preempt_boundary=True, notes="per decoder layer K,V projections of the 1500 encoder states (bf16 cache)"))
    prev = "asr.dec.cross_kv"
    for t in range(DECODE_TOKENS):
        pos = t + 4  # 3-4 forced prompt tokens precede the command (assumption)
        macs = L * (D * 3 * D + D * D + D * D + D * D + 2 * D * F) + D * V
        attn = L * Hh * (D // Hh) * 2 * (pos + S)
        oid = f"asr.dec.t{t}"
        ops.append(op(oid, "matmul", NPU_MAC, dtype="int8", layer=oid,
                      shape={"M": 1, "K": D, "N": D, "layers": L, "self_kv_len": pos, "cross_kv_len": S},
                      macs=macs + attn, flops=2 * (macs + attn) + L * D * 30 + V * 3,
                      bytes_in=D + L * 2 * (pos + S) * D * 2,
                      bytes_w=L * (6 * D * D + 2 * D * F) + D * V,
                      bytes_out=D * 2 + V * 2, depends_on=[prev], preempt_boundary=True,
                      notes=("one greedy decode step, batch 1: 4 layers (self-attn with KV cache, cross-attn over 1500 "
                             "encoder states, MLP) + tied 51865-way logits; weight-streaming bound")))
        prev = oid
    ops.append(op("asr.post.tokens_to_cmd", "cpu_kernel", CPU, dtype="int32", flops=1e4, bytes_in=V * 2, bytes_out=256,
                  instr={"scalar": 120000, "vector": 3000, "mem": 30000}, depends_on=[prev],
                  notes="argmax already on NPU; detokenize and match the command grammar (estimate)"))
    enc_macs = sum(o["macs"] for o in eops)
    prov = provenance(source="openai/whisper-tiny", script=SCRIPT, method="mixed", model_name="whisper-tiny",
                      model_version=f"hub sha {sha}", model_hash=f"hub:{WHISPER}@{sha}; encoder onnx sha256:{sha256_file(path)}",
                      notes=(f"Encoder: onnx_graph_walk of WhisperEncoder built from the real config with random init "
                             f"({st['onnx_nodes']} nodes -> {st['kept_ops']} ops, {enc_macs/1e9:.2f} GMAC for the 30 s window). "
                             f"Mel front end and {DECODE_TOKENS}-token greedy decoder: config_derived. int8 weights are a "
                             "deployment assumption. Period/deadline are reasonable defaults, not measured."))
    return make_graph("T11_speech", "Speech command recognition (Whisper-tiny)", prov, ops,
                      messages=[{"topic": "/speech/command", "bytes": 256, "consumer": "T4_vla"}],
                      footprint={"weights_bytes": sum(o.get("bytes_w", 0) for o in eops) + L * (6 * D * D + 2 * D * F) + D * V,
                                 "kv_cache_bytes": L * 2 * (S + DECODE_TOKENS + 4) * D * 2,
                                 "working_set_bytes": max(o.get("bytes_in", 0) + o.get("bytes_out", 0) for o in ops)})


def map_save_graph() -> dict:
    blocks, vox, vb = 5000, 512, 4
    B = blocks * vox * vb
    C = 0.4 * B
    ops = chain([
        op("map.snapshot", "memcopy", DMA, dtype="int8", bytes_in=B, bytes_out=B,
           notes=f"copy-on-write snapshot of the TSDF block pool ({blocks} blocks x 512 voxels x 4 B, assumption)"),
        op("map.lz4", "cpu_kernel", CPU, dtype="int8", bytes_in=B, bytes_out=C,
           instr={"scalar": int(1.2 * B), "vector": 0, "mem": int(0.4 * B)},
           notes="LZ4 compression, about 1.2 scalar instr and 0.4 memory ops per input byte (estimate), ratio 0.4"),
        op("map.write", "memcopy", DMA, dtype="int8", bytes_in=C, bytes_out=C, notes="compressed map -> storage",
           preempt_boundary=True),
    ])
    prov = provenance(source="map persistence (TSDF snapshot + LZ4 + storage write)", script=SCRIPT, method="analytic_count",
                      notes="All sizes and per-byte costs are labeled estimates.")
    return make_graph("T13_map_save", "Map save (TSDF snapshot, compress, write)", prov, ops,
                      footprint={"weights_bytes": 0, "working_set_bytes": int(B + C)})


def ota_graph() -> dict:
    bps = 50e6
    B = bps / 8 * 0.1
    ops = chain([
        op("ota.net_rx", "memcopy", DMA, dtype="int8", bytes_in=B, bytes_out=B, notes="NIC -> DRAM, 50 Mbit/s download (assumption)"),
        op("ota.tls_decrypt", "cpu_kernel", CPU, dtype="int8", bytes_in=B, bytes_out=B,
           instr={"scalar": int(3 * B), "vector": 0, "mem": int(B / 4)},
           notes="AES-GCM without crypto extensions, about 3 instr/byte (estimate)"),
        op("ota.sha256", "cpu_kernel", CPU, dtype="int32", bytes_in=B, bytes_out=32,
           instr={"scalar": int(12 * B), "vector": 0, "mem": int(B / 8)},
           notes="SHA-256 of the image chunk, about 12 scalar instr/byte (estimate)"),
        op("ota.flash_write", "memcopy", DMA, dtype="int8", bytes_in=B, bytes_out=B, notes="chunk -> flash (inactive slot)",
           preempt_boundary=True),
    ])
    prov = provenance(source="background OTA update (download, decrypt, verify, flash)", script=SCRIPT,
                      method="analytic_count", notes="100 ms chunks at 50 Mbit/s; per-byte costs are estimates.")
    return make_graph("T14_ota", "OTA download + flash (background)", prov, ops, params={"download_bps": bps},
                      footprint={"weights_bytes": 0, "working_set_bytes": int(2 * B)})


def diag_graph() -> dict:
    sources, msg = 50, 8192
    instr = sources * 200 + msg * 20
    ops = chain([
        op("diag.collect", "cpu_kernel", CPU, dtype="int32", bytes_in=sources * 256, bytes_out=sources * 64,
           instr={"scalar": sources * 200, "vector": 0, "mem": sources * 60},
           notes=f"read {sources} health sources (counters, temperatures, rates), estimate"),
        op("diag.aggregate_publish", "cpu_kernel", CPU, dtype="int32", bytes_in=sources * 64, bytes_out=msg,
           instr={"scalar": msg * 20, "vector": 0, "mem": msg * 4},
           notes=f"window statistics + format a {msg} B DiagnosticArray, estimate", preempt_boundary=True),
    ])
    prov = provenance(source="system diagnostics aggregator", script=SCRIPT, method="analytic_count",
                      notes=f"About {instr} instructions per 10 Hz job; estimate.")
    return make_graph("T15_diag", "Diagnostics (10 Hz)", prov, ops,
                      messages=[{"topic": "/diagnostics", "bytes": msg, "consumer": "T10_record"}],
                      footprint={"weights_bytes": 0, "working_set_bytes": 65536})


def main() -> None:
    for g in (speech_graph(), map_save_graph(), ota_graph(), diag_graph()):
        write_graph(g)
        s = g["summary"]
        print(f"{g['task_id']}: {s['num_ops']} ops, {s['macs']/1e9:.3f} GMAC, {s['flops']/1e9:.3f} GFLOP")


if __name__ == "__main__":
    main()
