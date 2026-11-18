"""Feed the measured RISC-V kernel costs into glasschip_core (CPU L2 path).

The VX-1 planner graph (workloads/graphs/T5_plan.json) runs TrajOpt's collision step on the SIMT
cluster. For the s13 study I build "what if the collision step ran on the RV64 app cores" variants
of that graph:

- Each TrajOpt block's `collision` op is replaced by `shards` parallel CPU shards (default 8, one per
  app core across both clusters). Each shard has two ops: `.esdf` (swept-sphere ESDF cost, 61 spheres
  x 4 sub-samples per state) and `.selfcol` (694 sphere pairs per state).
- Instruction counts per op = queries in the shard x measured instructions per query from gem5
  (riscv/tables/rvv_kernels.json). Cycles come from the core's CPU L2 hook: `set_l2_tables({"cpu_cpi":
  {op_id: CPI}})` with the measured gem5 CPI of that kernel variant, so cycles = instr x CPI.
- Variants: `scalar` (scalar ESDF + scalar self-collision), `rvv` (RVV both), `xcol` (xcol.sdf ESDF +
  RVV self-collision), `simt` (the unchanged graph).

The CPU ops keep the original op's memory traffic, split across shards, with the original reuse
factor, so the core still charges cache and DRAM traffic on top of the gem5 CPI (gem5's CPI already
includes its own cache behavior for the kernel's data; this double counts a little, on all CPU
variants alike).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "riscv/tables/rvv_kernels.json"

VARIANT_KERNELS = {"scalar": ("esdf_scalar", "selfcol_scalar"), "rvv": ("esdf_rvv", "selfcol_rvv"),
                   "xcol": ("esdf_xcol", "selfcol_rvv")}


def load_table(path: Path = TABLE) -> dict[str, Any]:
    return json.loads(path.read_text())


def t5_variant(base: dict[str, Any], table: dict[str, Any], variant: str, cpu: str = "o3",
               shards: int = 8) -> tuple[dict[str, Any], dict[str, float], dict[str, Any]]:
    """Return (graph, cpu_cpi table, summary) for a T5 variant. variant in scalar|rvv|xcol|simt."""
    g = copy.deepcopy(base)
    if variant == "simt":
        return g, {}, {"variant": "simt"}
    k_esdf, k_self = VARIANT_KERNELS[variant]
    ke, ks = table["kernels"][k_esdf][cpu], table["kernels"][k_self][cpu]
    p = g["params"]
    nsph = p["cuRobo_defaults"]["collision_spheres"]
    npair = p["cuRobo_defaults"]["self_collision_pairs"]
    sub = p["assumptions"]["sweep_subsamples"]
    cpi: dict[str, float] = {}
    ops_out: list[dict[str, Any]] = []
    rename: dict[str, list[str]] = {}
    summary: dict[str, Any] = {"variant": variant, "cpu_model": cpu, "shards": shards, "kernels": [k_esdf, k_self],
                               "blocks": []}
    vec = variant != "scalar"
    for o in g["ops"]:
        if not o["op_id"].endswith(".collision"):
            ops_out.append(o)
            continue
        # States evaluated in this block: threads per launch = seeds x line-search x horizon x spheres,
        # and the block runs two launches per iteration (curobo.py: launches = iterations x 2).
        states = o["shape"]["threads"] / nsph * o["shape"]["kernel_launches"] / 2
        q_esdf = states * nsph * sub
        q_self = states * npair
        deps = o.get("depends_on")
        tails = []
        for s in range(shards):
            e_id, s_id = f"{o['op_id']}.esdf.s{s}", f"{o['op_id']}.selfcol.s{s}"
            ie = q_esdf / shards * ke["instr_per_item"]
            is_ = q_self / shards * ks["instr_per_item"]
            share_e = q_esdf * 60 / (q_esdf * 60 + q_self * 15)  # flop share used for traffic split (curobo.py)
            common = {"dtype": "fp32", "eligible": ["cpu"], "reuse": o.get("reuse", 0.95), "macs": 0, "bytes_w": 0}
            ops_out.append(dict(common, op_id=e_id, type="cpu_kernel", flops=o["flops"] * share_e / shards,
                                bytes_in=o["bytes_in"] / shards, bytes_out=o["bytes_out"] / shards,
                                instr={"scalar": 0 if vec else ie, "vector": ie if vec else 0, "mem": 0},
                                depends_on=deps if deps else [],
                                notes=f"{k_esdf} on an app core (gem5 {cpu}): {q_esdf / shards:.0f} sphere queries, "
                                      f"{ke['instr_per_item']:.2f} instr and {ke['cycles_per_item']:.2f} cycles per query"))
            ops_out.append(dict(common, op_id=s_id, type="cpu_kernel", flops=o["flops"] * (1 - share_e) / shards,
                                bytes_in=0, bytes_out=0,
                                instr={"scalar": 0 if variant == "scalar" else 0, "vector": 0, "mem": 0} | (
                                    {"scalar": is_} if variant == "scalar" else {"vector": is_}),
                                depends_on=[e_id],
                                notes=f"{k_self} on an app core (gem5 {cpu}): {q_self / shards:.0f} sphere pairs"))
            cpi[e_id] = ke["cpi"]
            cpi[s_id] = ks["cpi"]
            tails.append(s_id)
        rename[o["op_id"]] = tails
        summary["blocks"].append({"op": o["op_id"], "states": states, "esdf_queries": q_esdf, "self_pairs": q_self,
                                  "esdf_cycles_total": q_esdf * ke["cycles_per_item"],
                                  "self_cycles_total": q_self * ks["cycles_per_item"]})
    # Rewire consumers of the replaced collision ops (explicit depends_on, or implicit "previous op").
    fixed: list[dict[str, Any]] = []
    prev_orig: str | None = None
    for o in ops_out:
        if "depends_on" in o and o["depends_on"]:
            new = []
            for d in o["depends_on"]:
                new.extend(rename.get(d, [d]))
            o["depends_on"] = new
        fixed.append(o)
    g["ops"] = fixed
    g["provenance"] = dict(g["provenance"], notes=g["provenance"].get("notes", "") +
                           f" | s13 variant '{variant}': TrajOpt collision on {shards} RV64 app cores, "
                           f"costs from riscv/tables/rvv_kernels.json ({cpu}); built by riscv/core_hook.py")
    tot = sum(b["esdf_cycles_total"] + b["self_cycles_total"] for b in summary["blocks"])
    summary["collision_cycles_total_one_core"] = tot
    summary["collision_ms_ideal_shards_at_2ghz"] = tot / shards / 2.0e9 * 1e3
    return g, cpi, summary


def fidelity_with_cpu_l2(fid: dict[str, Any]) -> dict[str, Any]:
    f = copy.deepcopy(fid)
    f["cpu"] = "L2"
    return f
