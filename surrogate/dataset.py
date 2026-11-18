"""Design-space dataset for the Glass Chip surrogate (spec Section 10.1-10.2).

What this does
--------------
1. Defines the design space (SPACE): NPU tiles, array size and SRAM per tile, system cache size
   and partitioning, QoS arbitration, NoC link width, DRAM bandwidth, CPU core count, scheduler,
   power policy, Vmin per domain, the hypothetical low-voltage arithmetic factor, transport mode,
   and the workload (scenario, recording bitrate, ambient temperature).
2. Draws a Latin hypercube over it (phase "lhs"), runs every point chip-only with
   surrogate/chiprun.py at the DSE fidelity mix, and appends results to Parquet part files in
   data/surrogate/ (gitignored). Interrupted runs resume: finished sample ids are skipped.
3. Active learning (phase "al<k>"): `extend_active` proposes new points where the current
   surrogate's conformal interval is widest relative to the deadline of the output it bounds
   (i.e. near deadline boundaries). These points are used for training only, never for
   calibration or testing, because they are not drawn from the same distribution.
4. The area proxy is computed in closed form from the configuration (area_proxy below).

Run:  .venv/bin/python -m surrogate.dataset lhs --n 5000 --workers 10
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "surrogate"
PARTS = DATA / "parts"

DURATION_S = 2.0        # simulated seconds per sample
WARMUP_S = 1.0          # releases before this are excluded from statistics; the power manager's
                        # thermal "settle" jump and energy restart happen at the same time (settle_s = 1.0)
SCHEDULERS = ["fifo", "fixed_priority", "edf", "edf_mc"]
POWER_POLICIES = ["nominal", "race_to_idle", "pace_to_deadline"]
TRANSPORTS = ["copy", "zero_copy"]
SCENARIOS = ["S1", "S2", "S3", "S6"]
NOC_BITS = [128, 256, 512]

# name: (kind, lo, hi, label, unit). kind: int, float, logfloat, cat (lo..hi are codes), choice.
SPACE: dict[str, tuple[str, float, float, str, str]] = {
    "npu_tiles": ("int", 2, 8, "NPU tiles", ""),
    "npu_array": ("int8", 32, 128, "NPU array size (rows = cols)", "PEs"),
    "npu_sram_mb": ("float", 0.5, 4.0, "NPU SRAM per tile", "MB"),
    "cache_mb": ("int", 2, 16, "System cache", "MB"),
    "cache_partition": ("cat", 0, 1, "Cache way partitioning", "off/on"),
    "qos": ("cat", 0, 1, "NoC/DRAM QoS arbitration", "off/on"),
    "noc_bits": ("choice", 0, 2, "NoC link width", "bits"),
    "dram_gbps": ("float", 64.0, 204.0, "DRAM bandwidth", "GB/s"),
    "cpu_cores": ("int", 4, 16, "App CPU cores", ""),
    "scheduler": ("cat", 0, 3, "Scheduler (FIFO, RM, EDF, EDF-MC)", "code"),
    "power_policy": ("cat", 0, 2, "Power policy (P0, P3, P4)", "code"),
    "vmin_npu": ("float", 0.45, 0.75, "NPU Vmin", "V"),
    "vmin_cpu": ("float", 0.50, 0.75, "CPU Vmin", "V"),
    "vmin_simt": ("float", 0.50, 0.75, "SIMT Vmin", "V"),
    "vmin_dsp": ("float", 0.50, 0.75, "DSP Vmin", "V"),
    "lv_arith_factor": ("lv", 1.0, 4.0, "Low-voltage arithmetic factor (hypothetical)", "x"),
    "transport": ("cat", 0, 1, "Transport (copy, zero-copy)", "code"),
    "scenario": ("cat", 0, 3, "Workload (S1, S2, S3, S6)", "code"),
    "bitrate_gbps": ("logfloat", 0.2, 48.0, "Recording bitrate", "Gbit/s"),
    "ambient_c": ("float", 25.0, 50.0, "Ambient temperature", "C"),
}
INPUTS = list(SPACE)

# Outputs (per spec 10.1). Per-task quantiles and miss rates for the tasks that produce
# enough jobs inside the measured window; T11 speech and T13 map save release less than
# once per window and are left out.
TASKS_OUT = ["T1_ingest_wrist", "T1_ingest_overhead", "T2_detect", "T2_detect_overhead", "T3_esdf", "T4_vla",
             "T5_plan", "T6_estimate", "T7_control", "T8_safety", "T10_record_4k", "T12_arm2_control"]
CHAINS = ["chain_camera_to_plan", "chain_human_to_control", "chain_camera_to_detection"]
SCALAR_OUT = ["worst_safety_miss_rate", "worst_rt_miss_rate", "avg_power_w", "peak_power_w",
              "energy_per_robot_hour_wh", "peak_temp_c"]


def latency_outputs() -> list[str]:
    out = []
    for t in TASKS_OUT + CHAINS:
        out += [f"{t}.p50_ms", f"{t}.p95_ms", f"{t}.p99_ms"]
    return out


def rate_outputs() -> list[str]:
    return [f"{t}.miss_rate" for t in TASKS_OUT]


def all_outputs() -> list[str]:
    return latency_outputs() + rate_outputs() + SCALAR_OUT + ["area_mm2"]


# --------------------------------------------------------------------------- encoding
def decode(u: Mapping[str, float]) -> dict[str, Any]:
    """Map a point in [0,1)^d to a run config (cfg for chiprun) plus its numeric encoding."""
    num: dict[str, float] = {}
    for k, (kind, lo, hi, _, _) in SPACE.items():
        x = float(u[k])
        if kind == "int":
            v = float(min(hi, lo + math.floor(x * (hi - lo + 1))))
        elif kind == "int8":
            steps = int((hi - lo) // 8)
            v = float(lo + 8 * min(steps, math.floor(x * (steps + 1))))
        elif kind in ("cat", "choice"):
            v = float(min(hi, lo + math.floor(x * (hi - lo + 1))))
        elif kind == "logfloat":
            v = float(math.exp(math.log(lo) + x * (math.log(hi) - math.log(lo))))
        elif kind == "lv":
            # 40 percent of samples at exactly 1x (feature off), the rest uniform on (1, 4].
            v = 1.0 if x < 0.4 else float(lo + (hi - lo) * (x - 0.4) / 0.6)
        else:
            v = float(lo + x * (hi - lo))
        num[k] = v
    return num


def to_cfg(num: Mapping[str, float]) -> dict[str, Any]:
    """Numeric encoding -> chiprun config."""
    cfg = dict(num)
    cfg["npu_tiles"] = int(num["npu_tiles"])
    cfg["npu_array"] = int(num["npu_array"])
    cfg["cache_mb"] = int(num["cache_mb"])
    cfg["cache_partition"] = int(num["cache_partition"])
    cfg["qos"] = int(num["qos"])
    cfg["noc_bits"] = NOC_BITS[int(num["noc_bits"])]
    cfg["cpu_cores"] = int(num["cpu_cores"])
    cfg["scheduler"] = SCHEDULERS[int(num["scheduler"])]
    cfg["power_policy"] = POWER_POLICIES[int(num["power_policy"])]
    cfg["transport"] = TRANSPORTS[int(num["transport"])]
    cfg["scenario"] = SCENARIOS[int(num["scenario"])]
    return cfg


def latin_hypercube(n: int, d: int, rng: np.random.Generator) -> np.ndarray:
    """Plain Latin hypercube: each column is a random permutation of n strata, jittered."""
    u = (np.argsort(rng.random((n, d)), axis=0) + rng.random((n, d))) / n
    return u


# --------------------------------------------------------------------------- area proxy
# Rough per-block area model (mm^2) for a generic 5 nm class node. This is a ROUGH PROXY for
# ranking design points, not a floorplan estimate. Sources for the density figures:
#  [Y19] Yeap et al., "5nm CMOS Production Technology Platform featuring full-fledged EUV, and
#        High Mobility Channel FinFETs with densest 0.021 um^2 SRAM cells", IEDM 2019.
#  [J17] Jouppi et al., "In-Datacenter Performance Analysis of a Tensor Processing Unit",
#        ISCA 2017: 65,536 8-bit MACs, 28 nm die under 331 mm^2, matrix unit 24 percent of the die.
#  App CPU core area is an ASSUMPTION (1.33 mm^2 per out-of-order Cortex-A78-class core on a 5 nm
#        class node). I did not find a published per-core mm^2 figure I could verify; Arm only states
#        relative numbers (A78 about 5 percent smaller than A77, Arm blog, May 2020).
AREA = {
    # SRAM: 0.021 um^2 bitcell [Y19] at an assumed 50 percent macro efficiency -> 0.042 um^2/bit.
    "sram_mm2_per_mb": 0.042e-6 * 8 * 1024 * 1024,
    # MAC: TPUv1 matrix unit 0.24 * 331 mm^2 / 65,536 at 28 nm [J17], scaled by an assumed 12x
    # logic density gain from 28 nm to 5 nm class (assumption).
    "mac_mm2": 0.24 * 331.0 / 65536 / 12.0,
    "npu_tile_overhead_mm2": 0.15,        # vector unit, sequencer, DMA port per tile (assumption)
    "cpu_core_mm2": 1.33,                 # assumption (see above)
    "cpu_l2_mb_per_4_cores": 1.0,         # vx1.yaml: 1 MB L2 per cluster of 4
    "router_mm2_per_256b": 0.05,          # 5-port router incl. buffers at 256 bit (assumption)
    "noc_routers": 16,
    "dram_phy_mm2_per_100gbps": 6.0,      # LPDDR5X PHY + controller scaled with bandwidth (assumption)
    "fixed_mm2": 18.0,                    # RT pair, SIMT, DSP, DMA, I/O, misc at VX-1 sizes (assumption)
}


def area_proxy(num: Mapping[str, float]) -> float:
    """Closed-form area proxy in mm^2 (see AREA for sources and assumptions)."""
    a = AREA
    tiles = float(num["npu_tiles"])
    arr = float(num["npu_array"])
    npu = tiles * (arr * arr * a["mac_mm2"] + a["npu_tile_overhead_mm2"] + float(num["npu_sram_mb"]) * a["sram_mm2_per_mb"])
    npu += 4.0 * a["sram_mm2_per_mb"]                     # 4 MB shared NPU SRAM (vx1.yaml)
    cores = float(num["cpu_cores"])
    cpu = cores * a["cpu_core_mm2"] + math.ceil(cores / 4.0) * a["cpu_l2_mb_per_4_cores"] * a["sram_mm2_per_mb"]
    cache = float(num["cache_mb"]) * a["sram_mm2_per_mb"] * 1.1     # 10 percent tags (assumption)
    noc_bits = NOC_BITS[int(num["noc_bits"])]
    noc = a["noc_routers"] * a["router_mm2_per_256b"] * noc_bits / 256.0
    dram = float(num["dram_gbps"]) / 100.0 * a["dram_phy_mm2_per_100gbps"]
    return float(npu + cpu + cache + noc + dram + a["fixed_mm2"])


# --------------------------------------------------------------------------- provenance
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _core_build_id() -> str | None:
    """The core's own build id (sim.info()["build_id"]) for the module chiprun imports."""
    try:
        import yaml
        import glasschip_core as gc  # noqa: F401  (chiprun put the snapshot first on sys.path)
        from surrogate.chiprun import SOC_PATH, DSE_FIDELITY_PATH
        sim = gc.Sim(yaml.safe_load(SOC_PATH.read_text()), yaml.safe_load(DSE_FIDELITY_PATH.read_text()), 0)
        return sim.info().get("build_id")
    except Exception:
        return None


def provenance() -> dict[str, Any]:
    import glob
    from surrogate.chiprun import SOC_PATH, POWER_PARAMS_PATH, _core_dir
    core = sorted(glob.glob(str(Path(_core_dir) / "glasschip_core*.so")))
    from surrogate.chiprun import GRAPH_DIR
    graphs = sorted(Path(GRAPH_DIR).glob("*.json"))
    gh = hashlib.sha256()
    for g in graphs:
        gh.update(g.read_bytes())
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except Exception:
        sha = ""
    return {
        "script": "surrogate/dataset.py", "driver": "surrogate/chiprun.py", "git_sha": sha,
        "core_so_sha256": _sha256(Path(core[0])) if core else None,
        "graphs_sha256": gh.hexdigest()[:16],
        "core_so_path": str(Path(core[0]).relative_to(ROOT)) if core else None,
        "core_build_id": _core_build_id(),
        "graph_dir": str(Path(GRAPH_DIR).relative_to(ROOT)),
        "soc": str(SOC_PATH.relative_to(ROOT)), "soc_sha256": _sha256(SOC_PATH),
        "power_params_sha256": _sha256(POWER_PARAMS_PATH),
        "fidelity": "surrogate/fidelity_dse.yaml", "fidelity_sha256": _sha256(Path(__file__).parent / "fidelity_dse.yaml"),
        "duration_s": DURATION_S, "warmup_s": WARMUP_S,
        "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# --------------------------------------------------------------------------- running
def _run_one(args: tuple[int, str, dict[str, float]]) -> dict[str, Any]:
    sid, phase, num = args
    from surrogate.chiprun import run_chip
    row: dict[str, Any] = {"sample_id": sid, "phase": phase}
    row.update({f"in.{k}": v for k, v in num.items()})
    try:
        r = run_chip(to_cfg(num), seed=1 + sid % 1000, duration_s=DURATION_S, warmup_s=WARMUP_S)
        row.update({f"out.{k}": v for k, v in r.items()})
        row["ok"] = True
        row["error"] = ""
    except Exception as e:  # keep going; record the failure
        row["ok"] = False
        row["error"] = f"{type(e).__name__}: {e}"[:500]
    row["out.area_mm2"] = area_proxy(num)
    row["finished_utc"] = time.time()
    return row


def done_ids() -> set[int]:
    ids: set[int] = set()
    for p in sorted(PARTS.glob("part-*.parquet")):
        ids.update(pd.read_parquet(p, columns=["sample_id"])["sample_id"].astype(int).tolist())
    return ids


def _flush(rows: list[dict[str, Any]], prov: Mapping[str, Any]) -> None:
    if not rows:
        return
    PARTS.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    for k, v in prov.items():
        df[f"prov.{k}"] = json.dumps(v) if not isinstance(v, (str, int, float)) or v is None else v
    name = PARTS / f"part-{int(time.time() * 1000)}-{os.getpid()}.parquet"
    df.to_parquet(name, index=False)


def run_points(points: list[tuple[int, str, dict[str, float]]], workers: int, log_every: int = 50) -> None:
    """Run (sample_id, phase, numeric config) points in a process pool; resumable."""
    import multiprocessing as mp
    have = done_ids()
    todo = [p for p in points if p[0] not in have]
    print(f"{len(points)} planned, {len(points) - len(todo)} done, {len(todo)} to run on {workers} workers", flush=True)
    if not todo:
        return
    prov = provenance()
    t0 = time.time()
    rows: list[dict[str, Any]] = []
    n = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers, maxtasksperchild=200) as pool:
        for row in pool.imap_unordered(_run_one, todo, chunksize=1):
            if (DATA / "STOP").exists():
                print("STOP file found; terminating the pool", flush=True)
                pool.terminate()
                break
            rows.append(row)
            n += 1
            if len(rows) >= log_every:
                _flush(rows, prov)
                rows = []
                el = time.time() - t0
                print(f"{n}/{len(todo)} done, {el / 60:.1f} min elapsed, eta {(len(todo) - n) * el / n / 60:.1f} min",
                      flush=True)
    _flush(rows, prov)
    print(f"finished {n} in {(time.time() - t0) / 60:.1f} min", flush=True)


def lhs_plan(n: int, seed: int = 20261117) -> list[tuple[int, str, dict[str, float]]]:
    rng = np.random.default_rng(seed)
    u = latin_hypercube(n, len(INPUTS), rng)
    return [(i, "lhs", decode(dict(zip(INPUTS, u[i])))) for i in range(n)]


def load_dataset() -> pd.DataFrame:
    parts = sorted(PARTS.glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no dataset parts in {PARTS}")
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df = df.drop_duplicates("sample_id", keep="last").sort_values("sample_id").reset_index(drop=True)
    return df


def consolidate() -> Path:
    """Write one dataset.parquet with every part (convenience for loading elsewhere)."""
    df = load_dataset()
    out = DATA / "dataset.parquet"
    df.to_parquet(out, index=False)
    return out


def extend_active(n_new: int, round_id: int, workers: int, pool_size: int = 40000, seed: int = 7) -> None:
    """Active learning: add n_new training points where the current surrogate's normalized
    conformal interval width is largest near deadline boundaries.

    Score for a candidate x: max over latency outputs o with a deadline D_o of
        width_o(x) / D_o   if the predicted interval [lo, hi] contains D_o (straddles it)
    plus the same for miss-rate outputs that straddle 0 / small (interval upper > 0.01 and
    lower < 0.01). Candidates come from a large random pool; the top n_new by score are run.
    """
    from surrogate.conformal import load_bundle
    bundle = load_bundle()
    rng = np.random.default_rng(seed + 1000 * round_id)
    u = rng.random((pool_size, len(INPUTS)))
    nums = [decode(dict(zip(INPUTS, row))) for row in u]
    X = pd.DataFrame(nums)[INPUTS].to_numpy(float)
    score = bundle.boundary_score(X)
    top = np.argsort(-score)[:n_new]
    base = 1_000_000 * round_id
    pts = [(base + i, f"al{round_id}", nums[j]) for i, j in enumerate(top)]
    print(f"active learning round {round_id}: score range of chosen {score[top].min():.3f}..{score[top].max():.3f}",
          flush=True)
    run_points(pts, workers)


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("lhs")
    a1.add_argument("--n", type=int, default=5000)
    a1.add_argument("--workers", type=int, default=10)
    a2 = sub.add_parser("active")
    a2.add_argument("--n", type=int, default=600)
    a2.add_argument("--round", type=int, default=1)
    a2.add_argument("--workers", type=int, default=10)
    sub.add_parser("consolidate")
    a = ap.parse_args(list(argv) if argv is not None else None)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if a.cmd == "lhs":
        run_points(lhs_plan(a.n), a.workers)
    elif a.cmd == "active":
        extend_active(a.n, a.round, a.workers)
    else:
        print(consolidate())


if __name__ == "__main__":
    main()
