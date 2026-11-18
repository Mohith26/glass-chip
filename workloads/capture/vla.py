"""T4_vla: SmolVLA (lerobot/smolvla_base) operator graph.

Method "mixed":
- Vision encoder + connector: the real SmolVLM2-500M vision tower architecture
  (SmolVLMVisionTransformer + SmolVLMConnector from transformers, built from the
  real config.json with random init; weights do not change the op graph),
  exported to ONNX at 512x512 and walked op by op. Run once per camera, which is
  what lerobot's embed_prefix loop does.
- Language model prefill and the flow-matching action expert: config_derived,
  following lerobot's smolvlm_with_expert.py / modeling_smolvla.py exactly:
  16 retained VLM layers, 16 expert layers (hidden 720, MLP 2048), cross-attention
  on odd layers and self-attention every 2nd layer, 10 Euler denoising steps,
  50-step action chunk, KV cache of the prefix reused across steps.
- Parameter counts are cross-checked against the real model.safetensors header,
  read with HTTP range requests (no weights downloaded).

Run: .venv/bin/python workloads/capture/vla.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from common import CPU, MODELS, NPU_MAC, NPU_MOVE, NPU_VEC, SRC_CACHE, make_graph, op, provenance, sha256_file, write_graph
from onnx_walk import WalkConfig, walk

SCRIPT = "workloads/capture/vla.py"
REPO = "lerobot/smolvla_base"
VLM_REPO = "HuggingFaceTB/SmolVLM2-500M-Video-Instruct"
NUM_CAMERAS = 2          # wrist + overhead in this stack (smolvla_base lists 3 slots; lerobot embeds only present ones)
ACTION_DIM_USED = 8      # Panda 7 joints + gripper, padded to max_action_dim inside the model
ACT_B = 1                # int8 activations for linear layers (deployment assumption)
ATT_B = 2                # bf16 for attention scores, softmax, KV cache (deployment assumption)
W_B = 1                  # int8 weights (deployment assumption; checkpoint is bf16 + f32)


def fetch_json(repo: str, fname: str) -> tuple[dict, str]:
    """Download a small JSON file from the Hub (cached under data/workloads/src/smolvla)."""
    from huggingface_hub import hf_hub_download, model_info
    d = SRC_CACHE / "smolvla"
    d.mkdir(parents=True, exist_ok=True)
    p = hf_hub_download(repo, fname, cache_dir=str(d / "hf"))
    sha = model_info(repo).sha
    return json.loads(Path(p).read_text()), sha


def header_params() -> dict[str, Any]:
    """Read the real safetensors header via HTTP range requests and group parameter counts."""
    from huggingface_hub import get_safetensors_metadata
    m = get_safetensors_metadata(REPO)
    tensors = next(iter(m.files_metadata.values())).tensors
    groups: dict[str, int] = {}
    for name, t in tensors.items():
        n = math.prod(t.shape)
        if ".vision_model." in name:
            g = "vision"
        elif ".connector." in name:
            g = "connector"
        elif ".text_model.layers." in name or name.endswith("text_model.norm.weight"):
            g = "vlm_layers"
        elif "embed_tokens" in name:
            g = "embed_tokens"
        elif "lm_head" in name:
            g = "lm_head"
        elif ".lm_expert." in name:
            g = "expert"
        else:
            g = "projections"
        groups[g] = groups.get(g, 0) + n
    return {"total": sum(m.parameter_count.values()), "by_dtype": dict(m.parameter_count), "groups": groups,
            "num_tensors": len(tensors)}


def export_vision(vlm_cfg: dict) -> Path:
    out = MODELS / "smolvlm2_vision_connector_512.onnx"
    if out.exists():
        return out
    import torch
    from transformers import SmolVLMConfig
    from transformers.models.smolvlm import modeling_smolvlm as M
    cfg = SmolVLMConfig(**vlm_cfg)
    cfg.vision_config._attn_implementation = "eager"
    torch.manual_seed(0)
    vis = M.SmolVLMVisionTransformer(cfg.vision_config).eval()
    con = M.SmolVLMConnector(cfg).eval()

    class VisionPlusConnector(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.v, self.c = vis, con

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            # Full (unpadded) images: position ids are exactly arange(num_patches), so the
            # embedding is written out directly; this keeps every shape static for the walk.
            e = self.v.embeddings
            h = e.patch_embedding(x).flatten(2).transpose(1, 2) + e.position_embedding.weight[None]
            h = self.v.encoder(inputs_embeds=h).last_hidden_state
            return self.c(self.v.post_layernorm(h))

    size = cfg.vision_config.image_size
    torch.onnx.export(VisionPlusConnector().eval(), (torch.zeros(1, 3, size, size),), str(out), opset_version=17,
                      input_names=["pixel_values"], output_names=["image_tokens"], dynamo=False)
    return out


def vis_layer(name: str) -> str:
    p = [x for x in name.strip("/").split("/") if x]
    if len(p) >= 2 and p[0] == "encoder":
        return p[1]                  # layers.N
    if p and p[0] in ("post_layernorm", "c"):
        return "connector" if p[0] == "c" else "post_layernorm"
    return "embeddings"


class Builder:
    """Accumulates config-derived ops with explicit dependencies."""

    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []
        self.last: str | None = None

    def add(self, o: dict[str, Any], deps: list[str] | None = None) -> str:
        o["depends_on"] = deps if deps is not None else ([self.last] if self.last else [])
        self.ops.append(o)
        self.last = o["op_id"]
        return o["op_id"]

    def linear(self, oid: str, layer: str, M: int, K: int, N: int, *, bias: bool = False, extra_flops: float = 0,
               bytes_in: float | None = None, bytes_out: float | None = None, notes: str = "",
               deps: list[str] | None = None, dtype: str = "int8") -> str:
        return self.add(op(oid, "matmul", NPU_MAC, layer=layer, dtype=dtype,
                           shape={"M": M, "K": K, "N": N}, macs=M * K * N, flops=2 * M * K * N + extra_flops,
                           bytes_in=M * K * ACT_B if bytes_in is None else bytes_in,
                           bytes_w=K * N * W_B + (4 * N if bias else 0),
                           bytes_out=M * N * ACT_B if bytes_out is None else bytes_out, notes=notes), deps)

    def norm(self, oid: str, layer: str, M: int, D: int) -> str:
        return self.add(op(oid, "norm", NPU_VEC, layer=layer, dtype="bf16", shape={"M": M, "D": D, "kind": "rmsnorm"},
                           flops=4 * M * D, bytes_in=M * D * ACT_B, bytes_w=D * 2, bytes_out=M * D * ACT_B))

    def attention(self, pre: str, layer: str, Lq: int, Lk: int, heads: int, kv_heads: int, hd: int,
                  kv_note: str) -> None:
        """Explicit attention: RoPE, QK^T, softmax, AV (GQA: kv_heads shared by heads)."""
        self.add(op(f"{pre}.rope", "elementwise", NPU_VEC, layer=layer, dtype="bf16",
                    shape={"Lq": Lq, "heads": heads, "head_dim": hd},
                    flops=3 * Lq * heads * hd + (3 * Lq * kv_heads * hd if Lq == Lk or "self" in kv_note else 0),
                    bytes_in=Lq * heads * hd * ACT_B, bytes_out=Lq * heads * hd * ATT_B))
        self.add(op(f"{pre}.qk", "matmul", NPU_MAC, layer=layer, dtype="bf16",
                    shape={"M": Lq, "K": hd, "N": Lk, "heads": heads, "kv_heads": kv_heads, "act_x_act": True},
                    macs=heads * Lq * Lk * hd,
                    bytes_in=Lq * heads * hd * ATT_B + Lk * kv_heads * hd * ATT_B,
                    bytes_out=heads * Lq * Lk * ATT_B, notes=f"QK^T, K: {kv_note}"))
        self.add(op(f"{pre}.softmax", "softmax", NPU_VEC, layer=layer, dtype="bf16",
                    shape={"rows": heads * Lq, "cols": Lk}, flops=5 * heads * Lq * Lk,
                    bytes_in=heads * Lq * Lk * ATT_B, bytes_out=heads * Lq * Lk * ATT_B))
        self.add(op(f"{pre}.av", "matmul", NPU_MAC, layer=layer, dtype="bf16",
                    shape={"M": Lq, "K": Lk, "N": hd, "heads": heads, "kv_heads": kv_heads, "act_x_act": True},
                    macs=heads * Lq * Lk * hd,
                    bytes_in=heads * Lq * Lk * ATT_B + Lk * kv_heads * hd * ATT_B,
                    bytes_out=Lq * heads * hd * ACT_B, notes=f"AV, V: {kv_note}"))

    def mlp(self, pre: str, layer: str, M: int, D: int, I: int) -> None:
        self.linear(f"{pre}.gate_up", layer, M, D, 2 * I, extra_flops=6 * M * I, bytes_out=M * I * ACT_B,
                    notes="gate_proj and up_proj fused, SiLU(gate)*up fused")
        self.linear(f"{pre}.down", layer, M, I, D, extra_flops=M * D, notes="down_proj + residual add")
        self.ops[-1]["preempt_boundary"] = True


def build() -> dict:
    cfg, sha = fetch_json(REPO, "config.json")
    vlm_cfg, vlm_sha = fetch_json(VLM_REPO, "config.json")
    t = vlm_cfg["text_config"]
    D, H, KV, HD, I = t["hidden_size"], t["num_attention_heads"], t["num_key_value_heads"], t["head_dim"], t["intermediate_size"]
    n_layers = cfg["num_vlm_layers"]
    De = int(D * cfg["expert_width_multiplier"])
    hd2 = int(2 * De / 3) * 4
    Ie = 256 * ((hd2 + 255) // 256)            # get_intermediate_size() in smolvlm_with_expert.py
    n_exp = n_layers if cfg["num_expert_layers"] <= 0 else cfg["num_expert_layers"]
    every = cfg["self_attn_every_n_layers"]
    chunk, steps = cfg["chunk_size"], cfg["num_steps"]
    A = cfg["max_action_dim"]
    S_in = cfg["max_state_dim"]
    vc = vlm_cfg["vision_config"]
    img = cfg["resize_imgs_with_padding"][0]
    patches = (img // vc["patch_size"]) ** 2
    img_tokens = patches // (vlm_cfg["scale_factor"] ** 2)
    lang = cfg["tokenizer_max_length"]          # pad_language_to = max_length
    prefix = NUM_CAMERAS * img_tokens + lang + 1
    kv_dim = KV * HD

    b = Builder()
    # ---- vision encoder + connector, walked from ONNX, once per camera ----
    onnx_path = export_vision(vlm_cfg)
    vops, vstats = walk(str(onnx_path), WalkConfig(prefix="vis", layer_fn=vis_layer))
    cam_outs = []
    for c in range(NUM_CAMERAS):
        ren = {}
        for o in vops:
            oo = json.loads(json.dumps(o))
            oo["op_id"] = f"vla.cam{c}." + o["op_id"]
            oo["layer"] = f"vla.cam{c}." + o["layer"]
            ren[o["op_id"]] = oo["op_id"]
            oo["depends_on"] = [ren[d] for d in o["depends_on"]]
            if oo.get("shape", {}).get("act_x_act") or oo["type"] in ("softmax",):
                oo["dtype"] = "bf16"
            b.ops.append(oo)
        cam_outs.append(b.ops[-1]["op_id"])
    vis_macs = sum(o["macs"] for o in vops)
    # ---- prefix embeddings ----
    lang_id = b.add(op("vla.prefix.lang_embed", "memcopy", NPU_MOVE, layer="vla.prefix", dtype="int8",
                       shape={"tokens": lang, "D": D}, bytes_in=lang * 4, bytes_w=lang * D * W_B,
                       bytes_out=lang * D * ACT_B, notes="embedding gather of 48 instruction tokens (rows touched only)"), [])
    st_id = b.linear("vla.prefix.state_proj", "vla.prefix", 1, S_in, D, bias=True, deps=[], dtype="fp32")
    b.add(op("vla.prefix.assemble", "memcopy", NPU_MOVE, layer="vla.prefix", dtype="int8",
             shape={"prefix_len": prefix}, flops=prefix * D, bytes_in=prefix * D * ACT_B, bytes_out=prefix * D * ACT_B,
             notes="scale image/lang embeddings by sqrt(D), concatenate prefix", preempt_boundary=True),
          cam_outs + [lang_id, st_id])
    # ---- VLM prefill over the prefix (fills the KV cache) ----
    for l in range(n_layers):
        L = f"vla.lm.l{l}"
        b.norm(f"{L}.in_norm", L, prefix, D)
        b.linear(f"{L}.qkv", L, prefix, D, D + 2 * kv_dim,
                 bytes_out=prefix * D * ACT_B + 2 * prefix * kv_dim * ATT_B, notes="q,k,v projections fused; K,V written to KV cache (bf16)")
        b.attention(f"{L}.attn", L, prefix, prefix, H, KV, HD, kv_note="self, prefix")
        b.linear(f"{L}.o", L, prefix, D, D, extra_flops=prefix * D, notes="o_proj + residual add")
        b.norm(f"{L}.post_norm", L, prefix, D)
        b.mlp(f"{L}.mlp", L, prefix, D, I)
    b.norm("vla.lm.final_norm", "vla.lm.final", prefix, D)
    # ---- flow-matching action expert, Euler integration ----
    for s in range(steps):
        P = f"vla.exp.s{s}"
        b.linear(f"{P}.action_in", f"{P}.embed", chunk, A, De, bias=True)
        b.linear(f"{P}.time_mlp_in", f"{P}.embed", chunk, 2 * De, De, bias=True, extra_flops=chunk * De * 4 + De * 8,
                 notes="sinusoidal time embedding concat + Linear + SiLU")
        b.linear(f"{P}.time_mlp_out", f"{P}.embed", chunk, De, De, bias=True)
        b.ops[-1]["preempt_boundary"] = True
        for l in range(n_exp):
            L = f"{P}.l{l}"
            self_attn = every > 0 and l % every == 0
            b.norm(f"{L}.in_norm", L, chunk, De)
            if self_attn:
                b.linear(f"{L}.qkv", L, chunk, De, H * HD + 2 * kv_dim, notes="expert q,k,v; K,V appended to cache then cropped")
                b.attention(f"{L}.attn", L, chunk, prefix + chunk, H, KV, HD,
                            kv_note=f"self, cached prefix ({prefix}) + action tokens ({chunk})")
            else:
                b.linear(f"{L}.q", L, chunk, De, H * HD)
                b.linear(f"{L}.kv_from_cache", L, 2 * prefix, kv_dim, kv_dim,
                         bytes_in=2 * prefix * kv_dim * ATT_B, bytes_out=2 * prefix * kv_dim * ATT_B,
                         notes="cross-attn: expert k_proj/v_proj re-project the VLM layer's cached K and V every step")
                b.attention(f"{L}.attn", L, chunk, prefix, H, KV, HD, kv_note=f"cross, projected VLM cache ({prefix})")
            b.linear(f"{L}.o", L, chunk, H * HD, De, extra_flops=chunk * De, notes="o_proj + residual")
            b.norm(f"{L}.post_norm", L, chunk, De)
            b.mlp(f"{L}.mlp", L, chunk, De, Ie)
        b.norm(f"{P}.final_norm", f"{P}.out", chunk, De)
        b.linear(f"{P}.action_out", f"{P}.out", chunk, De, A, bias=True)
        b.add(op(f"{P}.euler", "elementwise", NPU_VEC, layer=f"{P}.out", dtype="fp32", shape={"N": chunk * A},
                 flops=2 * chunk * A, bytes_in=2 * chunk * A * 4, bytes_out=chunk * A * 4,
                 notes="x_t += dt * v_t", preempt_boundary=True))
    b.add(op("vla.post.unnormalize", "cpu_kernel", CPU, dtype="fp32", flops=2 * chunk * ACTION_DIM_USED,
             bytes_in=chunk * A * 4, bytes_out=chunk * ACTION_DIM_USED * 4,
             instr={"scalar": 40, "vector": chunk * ACTION_DIM_USED * 2 // 8, "mem": chunk * ACTION_DIM_USED // 4},
             notes="crop to 8 used action dims and unnormalize (mean/std)"))

    # ---- parameter cross-check against the safetensors header ----
    hp = header_params()
    vis_params = None
    try:
        import onnx
        m = onnx.load(str(onnx_path))
        vis_params = sum(math.prod(i.dims) for i in m.graph.initializer if len(i.dims) >= 1 and math.prod(i.dims) > 1)
    except Exception:  # pragma: no cover
        pass
    Dv, Iv, pv = vc["hidden_size"], vc.get("intermediate_size", 3072), vc["patch_size"]
    Lv = vc.get("num_hidden_layers", 12)
    derived = {
        "vision": (3 * pv * pv * Dv + Dv) + patches * Dv
                  + Lv * (4 * (Dv * Dv + Dv) + 2 * Dv * Iv + Iv + Dv + 4 * Dv) + 2 * Dv,
        "vlm_layers": n_layers * (D * (D + 2 * kv_dim) + D * D + 3 * D * I + 2 * D) + D,
        "expert": sum((De * (H * HD + 2 * kv_dim) if (every > 0 and l % every == 0) else (De * H * HD + 2 * kv_dim * kv_dim))
                      + H * HD * De + 3 * De * Ie + 2 * De for l in range(n_exp)) + De,
        "connector": (vc["hidden_size"] * vlm_cfg["scale_factor"] ** 2) * D,
        "embed_tokens": vlm_cfg["vocab_size"] * D,
        "lm_head": vlm_cfg["vocab_size"] * D,
        "projections": (S_in * D + D) + (A * De + De) + (De * A + A) + (2 * De * De + De) + (De * De + De),
    }
    check = {g: {"derived": derived.get(g), "header": hp["groups"].get(g)} for g in hp["groups"]}
    if vis_params is not None:
        check["vision"]["onnx_initializers_incl_connector"] = vis_params
    derived_total = sum(derived.values())
    mismatches = [g for g, v in check.items() if v.get("derived") is not None and v["derived"] != v["header"]]

    kv_bytes = n_layers * 2 * prefix * kv_dim * ATT_B
    used_params = hp["total"] - hp["groups"].get("lm_head", 0)
    prov = provenance(
        source="lerobot/smolvla_base (SmolVLA, backbone HuggingFaceTB/SmolVLM2-500M-Video-Instruct)",
        script=SCRIPT, method="mixed", model_name="smolvla_base",
        model_version=f"hub sha {sha}; VLM hub sha {vlm_sha}; lerobot smolvla source as of fetch date",
        model_hash=f"hub:{REPO}@{sha}; vision onnx sha256:{sha256_file(onnx_path)}",
        notes=(f"Vision tower + connector: onnx_graph_walk of the real architecture built from the real config with random "
               f"init ({vstats['onnx_nodes']} ONNX nodes -> {vstats['kept_ops']} ops per camera, {vis_macs/1e9:.2f} GMAC per "
               f"{img}x{img} image, {img_tokens} tokens after pixel shuffle x{vlm_cfg['scale_factor']}), run for "
               f"{NUM_CAMERAS} cameras as lerobot embeds each camera separately. LM prefill and action expert: config_derived "
               f"from lerobot smolvlm_with_expert.py/modeling_smolvla.py: prefix = {NUM_CAMERAS}x{img_tokens} image + {lang} lang "
               f"+ 1 state = {prefix} tokens; {n_layers} VLM layers (D={D}, {H} heads, {KV} KV heads, head_dim {HD}, MLP {I}); "
               f"expert {n_exp} layers (D={De}, MLP {Ie}), self-attn every {every} layers else cross-attn on the cached VLM K/V "
               f"re-projected by the expert each step; {steps} Euler steps; chunk {chunk} x {A} (padded) actions. "
               f"Deployment dtypes are assumptions: int8 weights and linear activations, bf16 attention and KV cache. "
               f"Parameter cross-check vs safetensors header ({hp['num_tensors']} tensors, total {hp['total']:,}): "
               f"derived total {derived_total:,}; group mismatches: {mismatches or 'none'}. lm_head is unused at inference. "
               f"The exported ONNX stores fewer distinct initializers than parameters because the exporter deduplicates "
               f"identical random-init constants (zero biases, unit norm scales); op bytes_w still counts each use."),
        references=["https://huggingface.co/lerobot/smolvla_base",
                    "https://github.com/huggingface/lerobot/tree/main/src/lerobot/policies/smolvla",
                    "Shukor et al., SmolVLA: A Vision-Language-Action Model for Affordable and Efficient Robotics, 2025"],
    )
    params = {"param_check": check, "header_total_params": hp["total"], "header_by_dtype": hp["by_dtype"],
              "derived_total_params": derived_total, "prefix_tokens": prefix, "image_tokens_per_camera": img_tokens,
              "num_cameras": NUM_CAMERAS, "denoise_steps": steps, "chunk_size": chunk,
              "kv_cache": {"layers": n_layers, "tokens": prefix, "kv_dim": kv_dim, "dtype": "bf16", "bytes": kv_bytes,
                           "transient_suffix_bytes": (n_exp // max(every, 1)) * 2 * chunk * kv_dim * ATT_B}}
    msgs = [{"topic": "/vla/action_chunk", "bytes": 64 + chunk * ACTION_DIM_USED * 4, "consumer": "T5_plan"}]
    g = make_graph("T4_vla", "VLA policy: SmolVLA (SigLIP-style vision, SmolLM2 prefill, flow-matching expert)", prov,
                   b.ops, messages=msgs, params=params,
                   footprint={"weights_bytes": used_params * W_B, "kv_cache_bytes": kv_bytes,
                              "working_set_bytes": max(o.get("bytes_in", 0) + o.get("bytes_out", 0) for o in b.ops)})
    return g


def main() -> None:
    g = build()
    write_graph(g)
    s = g["summary"]
    print(f"T4_vla: {s['num_ops']} ops, {s['macs']/1e9:.1f} GMAC, weight traffic {s['bytes_w']/1e6:.0f} MB/inference, "
          f"params header {g['params']['header_total_params']:,} derived {g['params']['derived_total_params']:,}")
    for k, v in g["params"]["param_check"].items():
        print(f"  {k:14s} {v}")


if __name__ == "__main__":
    main()
