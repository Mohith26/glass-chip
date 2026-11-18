"""Regenerate the NPU L2 tables calib/tables/npu_scalesim_<task>.json (spike V7).

I configure SCALE-Sim like one VX-1 NPU tile (configs/soc/vx1.yaml blocks.npu): a 64x64
array, weight-stationary and output-stationary dataflows, and the tile's 2 MiB SRAM split
768 KiB ifmap / 768 KiB filter / 512 KiB ofmap (the split is my assumption; vx1.yaml only
gives the total). Every conv2d, dwconv2d and matmul op in a task graph becomes one
SCALE-Sim layer:

* conv: SCALE-Sim has no padding, so I set IFMAP H/W = (Ho-1)*stride + R (resp. S) so its
  output size matches the graph's Ho/Wo. Grouped convs with more than one channel per group
  run one group (C/g in, K/g out) and multiply cycles and accesses by g.
* depthwise (one channel per group): SCALE-Sim has no depthwise mode. Default "packed"
  mapping: one layer with C = 1 and K = g, i.e. the g channels side by side across the array
  columns (same MAC count; ifmap reads multiplied by min(g, array columns) because SCALE-Sim
  already re-reads the ifmap per column fold, and within a fold each column needs its own
  channel). I also record the strict alternative (g single-channel layers back to back,
  which leaves 63 of 64 columns idle) as `strict_sequential_groups` so the gap is visible.
* matmul MxK by KxN: a 1x1 conv with IFMAP H = M, W = 1, C = K, filters = N. Batched or
  multi-head matmuls run one slice and multiply by macs / (M*K*N).

SCALE-Sim runs in CALC bandwidth mode (no DRAM stalls; it reports the bandwidth it needed).
Its USER mode simulates stalls but took minutes per large layer here and takes per-operand
backing bandwidths from the [layout] section, so I report stalls from a simple bandwidth
model instead and label them: stall = max(0, dram_bytes / per_tile_bw - compute_cycles),
with per_tile_bw = DRAM peak / NPU clock / tiles (vx1.yaml).

Compute cycles assume one MAC per PE per cycle (INT8). `vx1_cycles` divides by the dtype
rate fraction from vx1.yaml (BF16 0.25, FP8 0.5; FP32 ops are treated as BF16, labeled).

Results are cached per unique layer shape in data/calib/scalesim_cache/ so reruns are cheap.

    .venv/bin/python calib/run_scalesim.py [--tasks T2_detect T8_safety T4_vla] [--standard]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import threading
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
CACHE = ROOT / "data" / "calib" / "scalesim_cache"
WORKER = HERE / "scalesim" / "run_layer.py"
SS_SRC = ROOT / "third_party" / "scale-sim-v2"

SRAM_SPLIT_KB = {"ifmap": 768, "filter": 768, "ofmap": 512}  # assumption, sums to 2048
ARRAY_COLS = 64  # overwritten from vx1.yaml in main()


def load_npu() -> tuple[dict[str, Any], dict[str, Any]]:
    with open(ROOT / "configs" / "soc" / "vx1.yaml") as f:
        soc = yaml.safe_load(f)
    return soc["blocks"]["npu"], soc["blocks"]["dram"]


def write_cfg(df: str, npu: dict[str, Any]) -> Path:
    rows, cols = npu["array"]
    assert sum(SRAM_SPLIT_KB.values()) == npu["sram_per_tile_kb"]
    p = HERE / "scalesim" / f"vx1_tile_{df}.cfg"
    p.write_text(f"""[general]
run_name = vx1_tile_{df}

[architecture_presets]
ArrayHeight:    {rows}
ArrayWidth:     {cols}
IfmapSramSzkB:   {SRAM_SPLIT_KB['ifmap']}
FilterSramSzkB:  {SRAM_SPLIT_KB['filter']}
OfmapSramSzkB:   {SRAM_SPLIT_KB['ofmap']}
IfmapOffset:    0
FilterOffset:   10000000
OfmapOffset:    20000000
Bandwidth : 28
Dataflow : {df}
MemoryBanks:   1
ReadRequestBuffer: 32
WriteRequestBuffer: 32

[layout]
IfmapCustomLayout: False
IfmapSRAMBankBandwidth: 10
IfmapSRAMBankNum: 10
IfmapSRAMBankPort: 2
FilterCustomLayout: False
FilterSRAMBankBandwidth: 10
FilterSRAMBankNum: 10
FilterSRAMBankPort: 2

[sparsity]
SparsitySupport : false
SparseRep : ellpack_block
OptimizedMapping : false
BlockSize : 8
RandomNumberGeneratorSeed : 40

[run_presets]
InterfaceBandwidth: CALC
UseRamulatorTrace: False
""")
    return p


def layer_of(op: dict[str, Any], strict_dw: bool = False
             ) -> tuple[tuple[int, ...], int, int] | None:
    """Return ((ifh, ifw, R, S, C, K, stride), multiplier, ifmap_scale) or None."""
    s = op.get("shape", {})
    t = op["type"]
    if t in ("conv2d", "dwconv2d"):
        g = int(s.get("groups", 1))
        st = int(s.get("stride", 1))
        R, S = int(s["R"]), int(s["S"])
        Ho = int(s.get("Ho") or (s["out"][2]))
        Wo = int(s.get("Wo") or (s["out"][3]))
        C, K = int(s["C"]) // g, int(s["K"]) // g
        ifh, ifw = (Ho - 1) * st + R, (Wo - 1) * st + S
        if g > 1 and C == 1 and K == 1 and not strict_dw:
            return (ifh, ifw, R, S, 1, g, st), 1, min(g, ARRAY_COLS)  # packed depthwise
        return (ifh, ifw, R, S, max(C, 1), max(K, 1), st), g, 1
    if t == "matmul":
        M, K, N = int(s["M"]), int(s["K"]), int(s["N"])
        base = M * K * N
        macs = int(op.get("macs") or 0)
        mult = round(macs / base) if macs and base else int(s.get("batch", 1)) * int(s.get("heads", 1))
        return (M, 1, 1, 1, K, N, 1), max(mult, 1), 1
    return None


def run_layer(df: str, cfg: Path, key: tuple[int, ...]) -> dict[str, Any]:
    h = hashlib.sha1(f"{df}:{key}:{SRAM_SPLIT_KB}".encode()).hexdigest()[:16]
    cp = CACHE / f"{df}_{h}.json"
    if cp.exists():
        return json.loads(cp.read_text())
    # unique per process so two concurrent runs never share (or delete) a workdir
    wd = CACHE / "work" / f"{df}_{h}_{os.getpid()}_{threading.get_ident()}"
    t0 = time.time()
    r = subprocess.run([PY, str(WORKER), str(cfg), str(wd), f"L{h}", *map(str, key)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"SCALE-Sim failed for {df} {key}: {r.stderr[-1500:]}")
    res = json.loads(r.stdout.strip().splitlines()[-1])
    res["key"] = list(key)
    res["wall_s"] = time.time() - t0
    CACHE.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(res))
    subprocess.run(["rm", "-rf", str(wd)])
    return res


def f(d: dict[str, str], k: str) -> float:
    return float(d[k])


def row_for(op: dict[str, Any], res: dict[str, Any], mult: int, npu: dict[str, Any],
            tile_bw: float, ifmap_scale: int = 1) -> dict[str, Any]:
    c, det = res["compute"], res["detail"]
    dtype = op.get("dtype", "int8")
    wb = {"int8": 1, "fp8": 1, "bf16": 2, "fp16": 2, "fp32": 2}.get(dtype, 1)
    rate = {"int8": 1.0, "fp8": npu.get("fp8_rate_fraction", 0.5),
            "bf16": npu.get("bf16_rate_fraction", 0.25),
            "fp32": npu.get("bf16_rate_fraction", 0.25)}.get(dtype, 1.0)
    cyc = f(c, "Total Cycles") * mult
    cyc_pf = f(c, "Total Cycles (incl. prefetch)") * mult
    sram_r = (f(det, "SRAM IFMAP Reads") * ifmap_scale + f(det, "SRAM Filter Reads")) * mult
    sram_w = f(det, "SRAM OFMAP Writes") * mult
    dram_r = (f(det, "DRAM IFMAP Reads") * ifmap_scale + f(det, "DRAM Filter Reads")) * mult * wb
    dram_w = f(det, "DRAM OFMAP Writes") * mult * wb
    vx1_cyc = cyc / rate
    stall = max(0.0, (dram_r + dram_w) / tile_bw - vx1_cyc)
    key = res["key"]
    return {
        "type": op["type"], "dtype": dtype,
        "scalesim_layer": {"ifmap_h": key[0], "ifmap_w": key[1], "R": key[2], "S": key[3],
                           "C": key[4], "K": key[5], "stride": key[6]},
        "multiplier": mult,
        "ifmap_read_scale": ifmap_scale,
        "macs": op.get("macs"),
        "compute_cycles": cyc,
        "compute_cycles_incl_prefetch": cyc_pf,
        "stall_cycles_scalesim": f(c, "Stall Cycles") * mult,
        "stall_cycles_bw_model": stall,
        "vx1_cycles": vx1_cyc,
        "vx1_cycles_with_stalls": vx1_cyc + stall,
        "utilization": f(c, "Overall Util %") / 100.0,
        "mapping_efficiency": f(c, "Mapping Efficiency %") / 100.0,
        "compute_util": f(c, "Compute Util %") / 100.0,
        "sram_reads": sram_r, "sram_writes": sram_w,
        "dram_read_bytes": dram_r, "dram_write_bytes": dram_w,
        "dram_bytes": dram_r + dram_w,
    }


def provenance(extra: dict[str, Any]) -> dict[str, Any]:
    sha = subprocess.run(["git", "-C", str(SS_SRC), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return {
        "script": "calib/run_scalesim.py",
        "tool": "SCALE-Sim (github.com/scalesim-project/scale-sim-v2, package version 3.0.0)",
        "tool_commit": sha,
        "local_patches": ["numpy 2 fixes: int(np.max(...)) in scalesim/memory/read_buffer.py "
                          "and double_buffered_scratchpad_mem.py"],
        "config_files": ["configs/soc/vx1.yaml", "calib/scalesim/vx1_tile_ws.cfg",
                         "calib/scalesim/vx1_tile_os.cfg"],
        "date": dt.date.today().isoformat(),
        **extra,
    }


def setup_block(npu: dict[str, Any], tile_bw: float) -> dict[str, Any]:
    return {
        "array": npu["array"], "dataflows": ["ws", "os"],
        "sram_split_kb_assumption": SRAM_SPLIT_KB,
        "npu_freq_hz": float(npu["freq_hz"]),
        "bandwidth_mode": "CALC (SCALE-Sim stall-free; stall_cycles_scalesim is 0 by construction)",
        "stall_model": "stall_cycles_bw_model = max(0, dram_bytes / per_tile_bw - vx1_cycles), "
                       f"per_tile_bw = {tile_bw:.2f} B/cycle (DRAM peak / NPU clock / tiles)",
        "counts": "SRAM reads/writes in words; DRAM in bytes (int8 1 B, bf16/fp32 2 B words)",
        "not_covered": "elementwise, norm, softmax, pool, resize, memcopy, cpu/dsp kernels are not "
                       "systolic GEMMs and stay on the L0/L1 NPU model",
    }


def run_task(task: str, cfgs: dict[str, Path], npu: dict[str, Any], tile_bw: float,
             jobs: int) -> Path:
    gpath = ROOT / "workloads" / "graphs" / f"{task}.json"
    g = json.loads(gpath.read_text())
    ops = [(op, *lo) for op in g["ops"] if (lo := layer_of(op)) is not None]
    strict = {op["op_id"]: layer_of(op, strict_dw=True) for op, _, _, sc in ops if sc > 1}
    uniq = {(df, key) for df in cfgs for _, key, _, _ in ops}
    uniq |= {(df, v[0]) for df in cfgs for v in strict.values()}
    t0 = time.time()
    with ThreadPoolExecutor(jobs) as ex:
        futs = {u: ex.submit(run_layer, u[0], cfgs[u[0]], u[1]) for u in sorted(uniq)}
        results = {u: fu.result() for u, fu in futs.items()}
    tables: dict[str, Any] = {}
    for df in cfgs:
        tab = {op["op_id"]: row_for(op, results[(df, key)], mult, npu, tile_bw, sc)
               for op, key, mult, sc in ops}
        for oid, (skey, smult, _) in strict.items():
            op = next(o for o, *_ in ops if o["op_id"] == oid)
            sr = row_for(op, results[(df, skey)], smult, npu, tile_bw)
            tab[oid]["strict_sequential_groups"] = {
                k: sr[k] for k in ("compute_cycles", "vx1_cycles", "utilization", "dram_bytes",
                                   "multiplier", "scalesim_layer")}
        tot = {k: sum(r[k] for r in tab.values()) for k in
               ("compute_cycles", "vx1_cycles", "vx1_cycles_with_stalls", "dram_bytes",
                "sram_reads", "stall_cycles_bw_model")}
        macs = sum((r["macs"] or 0) for r in tab.values())
        tot["macs"] = macs
        tot["mac_utilization_vs_peak"] = macs / (tot["compute_cycles"] * npu["int8_macs_per_cycle_per_tile"])
        tot["single_tile_ms_at_nominal"] = tot["vx1_cycles_with_stalls"] / float(npu["freq_hz"]) * 1e3
        tables[df] = {"totals": tot, "ops": tab}
    out = {
        "provenance": provenance({"task_graph": f"workloads/graphs/{task}.json",
                                  "task_graph_sha256": hashlib.sha256(gpath.read_bytes()).hexdigest(),
                                  "graph_provenance": g.get("provenance", {}),
                                  "wall_s": time.time() - t0}),
        "task_id": task,
        "setup": setup_block(npu, tile_bw),
        "n_ops": len(ops), "n_unique_layers": len({k for _, k, _, _ in ops}),
        "dataflows": tables,
    }
    p = HERE / "tables" / f"npu_scalesim_{task}.json"
    p.write_text(json.dumps(out, indent=1))
    return p


def run_standard(cfgs: dict[str, Path], npu: dict[str, Any], tile_bw: float, jobs: int) -> Path:
    """Shipped SCALE-Sim topologies (conv format), for an ungated reference."""
    files = ["topologies/conv_nets/Resnet18.csv", "topologies/conv_nets/yolo_tiny.csv"]
    nets: dict[str, list[tuple[str, tuple[int, ...]]]] = {}
    for fn in files:
        rows = []
        for line in (SS_SRC / fn).read_text().splitlines()[1:]:
            el = [x.strip() for x in line.split(",") if x.strip()]
            if len(el) < 8:
                continue
            rows.append((el[0], tuple(int(x) for x in el[1:8])))
        nets[Path(fn).stem] = rows
    uniq = {(df, k) for df in cfgs for rows in nets.values() for _, k in rows}
    with ThreadPoolExecutor(jobs) as ex:
        futs = {u: ex.submit(run_layer, u[0], cfgs[u[0]], u[1]) for u in sorted(uniq)}
        results = {u: fu.result() for u, fu in futs.items()}
    out_nets: dict[str, Any] = {}
    for net, rows in nets.items():
        out_nets[net] = {}
        for df in cfgs:
            layers = []
            for name, k in rows:
                ifh, ifw, R, S, C, K, st = k
                macs = ((ifh - R) // st + 1) * ((ifw - S) // st + 1) * R * S * C * K
                r = row_for({"type": "conv2d", "dtype": "int8", "macs": macs}, results[(df, k)],
                            1, npu, tile_bw)
                r["layer"] = name
                layers.append(r)
            out_nets[net][df] = {
                "total_compute_cycles": sum(x["compute_cycles"] for x in layers),
                "total_dram_bytes": sum(x["dram_bytes"] for x in layers),
                "layers": layers,
            }
    out = {"provenance": provenance({"topologies": files}), "setup": setup_block(npu, tile_bw),
           "networks": out_nets}
    p = HERE / "tables" / "npu_scalesim_standard.json"
    p.write_text(json.dumps(out, indent=1))
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="*", default=["T2_detect", "T8_safety", "T4_vla"])
    ap.add_argument("--standard", action="store_true")
    ap.add_argument("--jobs", type=int, default=12)
    a = ap.parse_args()
    npu, dram = load_npu()
    global ARRAY_COLS
    ARRAY_COLS = int(npu["array"][1])
    peak = dram["data_rate_mts"] * 1e6 * dram["bus_bits_total"] / 8
    tile_bw = peak / float(npu["freq_hz"]) / npu["tiles"]
    cfgs = {df: write_cfg(df, npu) for df in ("ws", "os")}
    (HERE / "tables").mkdir(exist_ok=True)
    if a.standard:
        print(run_standard(cfgs, npu, tile_bw, a.jobs), flush=True)
    for t in a.tasks:
        if not (ROOT / "workloads" / "graphs" / f"{t}.json").exists():
            print(f"skip {t}: graph not found", flush=True)
            continue
        print(run_task(t, cfgs, npu, tile_bw, a.jobs), flush=True)


if __name__ == "__main__":
    main()
