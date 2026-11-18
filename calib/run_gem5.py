"""Regenerate calib/tables/cpu_gem5.json (spikes V8/V9, CPU L3 reference).

Kernels (RV64, built by calib/riscv/kernels/Makefile with zig cc, static musl):
* T7_control_step, T6_estimate_step, T8_ssm_step: the real control-stack kernels from
  workloads/capture/control/ (unmodified sources), driven by control_bench.cpp.
* axpy_rvv_4096 / axpy_scalar_4096 and gemm_rvv_64 / gemm_scalar_64 (64x64x64 fp32).

Each kernel runs in gem5 SE mode (calib/gem5/se_run.py: 64 KiB L1I/L1D, 1 MiB L2, DDR4
behind it, 2 GHz core clock, VLEN 256) on three CPU models: TimingSimpleCPU (1 instruction
at a time, blocking memory), MinorCPU (in-order pipeline; my proxy for the VX-1 RT core) and
O3CPU with gem5's default parameters (my proxy for the app cores' out-of-order abstraction).
The kernels read the rdcycle/rdinstret CSRs around their timed loop. I run every
configuration at two iteration counts and report the marginal cost per call
((c2 - c1) / (n2 - n1)), which removes startup and cold-cache effects, plus the cold
per-call figure from the smaller run.

    .venv/bin/python calib/run_gem5.py [--jobs 8]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GEM5 = ROOT / "third_party" / "gem5" / "build" / "RISCV" / "gem5.opt"
SE = HERE / "gem5" / "se_run.py"
BIN = HERE / "riscv" / "build"
OUTD = ROOT / "data" / "calib" / "gem5"

# name -> (binary, extra args, (n1, n2))
KERNELS: dict[str, tuple[str, str, tuple[int, int]]] = {
    "T7_control_step": ("control_bench", "control", (50, 100)),
    "T6_estimate_step": ("control_bench", "estimate", (24, 48)),
    "T8_ssm_step": ("control_bench", "ssm", (5, 10)),
    "axpy_rvv_4096": ("axpy", "", (5, 10)),
    "axpy_scalar_4096": ("axpy", "s", (2, 4)),
    "gemm_rvv_64": ("gemm", "", (1, 2)),
    "gemm_scalar_64": ("gemm", "s", (1, 2)),
}
CPUS = ("timing", "minor", "o3")
BENCH_RE = re.compile(r"BENCH (\S+) iters=(\d+) cycles=(\d+) instret=(\d+) check=(\S+)")


def run_one(args: tuple[str, str, int]) -> dict[str, Any]:
    kname, cpu, n = args
    binary, extra, _ = KERNELS[kname]
    od = OUTD / f"{kname}_{cpu}_{n}"
    cmd = [str(GEM5), f"--outdir={od}", str(SE), "--cpu", cpu, "--binary", str(BIN / binary),
           "--args", f"{n} {extra}".strip()]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.time() - t0
    m = BENCH_RE.search(r.stdout)
    if not m:
        raise RuntimeError(f"{kname} {cpu} {n} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    stats = (od / "stats.txt").read_text()
    sim_insts = int(re.search(r"^simInsts\s+(\d+)", stats, re.M).group(1))
    num_cycles = int(re.search(r"^system\.cpu\.numCycles\s+(\d+)", stats, re.M).group(1))
    return {"kernel": kname, "cpu": cpu, "iters": n, "cycles": int(m.group(3)),
            "instret": int(m.group(4)), "check": float(m.group(5)), "wall_s": wall,
            "whole_run_sim_insts": sim_insts, "whole_run_num_cycles": num_cycles,
            "gem5_cmd": " ".join(cmd[1:]).replace(str(ROOT) + "/", "")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", default=str(HERE / "tables" / "cpu_gem5.json"))
    a = ap.parse_args()
    subprocess.run(["make", "-C", str(HERE / "riscv" / "kernels")], check=True,
                   capture_output=True)
    jobs = [(k, c, n) for k in KERNELS for c in CPUS for n in KERNELS[k][2]]
    with ThreadPoolExecutor(a.jobs) as ex:
        runs = list(ex.map(run_one, jobs))
    with open(ROOT / "configs" / "soc" / "vx1.yaml") as f:
        soc = yaml.safe_load(f)
    f_app = float(soc["blocks"]["cpu_a"]["freq_hz"])
    f_rt = float(soc["blocks"]["rt"]["freq_hz"])
    table: dict[str, Any] = {}
    for k in KERNELS:
        table[k] = {}
        for c in CPUS:
            r1, r2 = sorted([r for r in runs if r["kernel"] == k and r["cpu"] == c],
                            key=lambda r: r["iters"])
            dn = r2["iters"] - r1["iters"]
            cyc = (r2["cycles"] - r1["cycles"]) / dn
            ins = (r2["instret"] - r1["instret"]) / dn
            table[k][c] = {
                "cycles_per_call": cyc,
                "instr_per_call": ins,
                "cpi": cyc / ins if ins else None,
                "ipc": ins / cyc if cyc else None,
                "cold_cycles_per_call": r1["cycles"] / r1["iters"],
                "us_per_call_at_app_cpu_freq": cyc / f_app * 1e6,
                "us_per_call_at_rt_freq": cyc / f_rt * 1e6,
                "runs": [r1, r2],
            }
    # Check that the rdcycle CSR counts core cycles: compare against whole-run numCycles.
    csr_check = [{"kernel": r["kernel"], "cpu": r["cpu"], "iters": r["iters"],
                  "csr_cycles": r["cycles"], "stats_numCycles_whole_run": r["whole_run_num_cycles"],
                  "csr_fraction_of_run": r["cycles"] / r["whole_run_num_cycles"]}
                 for r in runs if r["kernel"] in ("gemm_scalar_64", "axpy_scalar_4096")]
    gem5_sha = subprocess.run(["git", "-C", str(ROOT / "third_party" / "gem5"), "rev-parse",
                               "HEAD"], capture_output=True, text=True).stdout.strip()
    zig = subprocess.run([sys.executable, "-m", "ziglang", "version"], capture_output=True,
                         text=True).stdout.strip()
    out = {
        "provenance": {
            "script": "calib/run_gem5.py",
            "tool": "gem5 26.0.0.0 (RISCV/gem5.opt, SE mode)",
            "tool_commit": gem5_sha,
            "compiler": f"zig cc {zig} (LLVM, ld.lld, musl), -O2 -mcpu=baseline_rv64+v",
            "kernel_sources": ["workloads/capture/control/control.cpp",
                               "workloads/capture/control/estimate.cpp",
                               "workloads/capture/control/ssm.cpp",
                               "calib/riscv/kernels/control_bench.cpp",
                               "calib/riscv/kernels/axpy.c", "calib/riscv/kernels/gemm.c"],
            "config_files": ["configs/soc/vx1.yaml", "calib/gem5/se_run.py"],
            "date": dt.date.today().isoformat(),
        },
        "setup": {
            "system": "1 core, 64 KiB L1I + 64 KiB L1D (4-way, 2-cycle tag/data/response), "
                      "1 MiB L2 (16-way, 12-cycle), DDR4_2400_8x8 memory controller, 2 GHz",
            "vlen_bits": 256,
            "cpu_models": {"timing": "TimingSimpleCPU", "minor": "MinorCPU (RT core proxy)",
                           "o3": "O3CPU, gem5 default parameters (app core proxy)"},
            "method": "marginal per-call cost from two iteration counts, rdcycle/rdinstret CSRs",
            "notes": ["control kernels built with +v in one binary; control_step and ssm_step "
                      "contain no vector code at -O2 unless the compiler auto-vectorizes",
                      "cache latencies and DDR4 backing memory are gem5-style assumptions, not "
                      "VX-1 parameters (vx1.yaml gives sizes, not latencies)"],
        },
        "kernels": table,
        "csr_vs_stats_check": csr_check,
    }
    Path(a.out).write_text(json.dumps(out, indent=1))
    for k, v in table.items():
        print(k, {c: (round(v[c]["cycles_per_call"]), round(v[c]["cpi"], 3)) for c in CPUS})


if __name__ == "__main__":
    main()
