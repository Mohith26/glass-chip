"""Run the RISC-V planner/control kernels in gem5 SE mode and write riscv/tables/rvv_kernels.json.

What it does
- Builds nothing: run `make -C riscv/kernels` first (zig cc for RV64, Apple clang for the host reference).
- Runs every kernel variant (scalar, RVV, and the xcol.sdf study instruction for the ESDF kernel) on
  gem5's MinorCPU (in-order, my RT-core proxy) and O3CPU (my app-core proxy), at two iteration counts,
  and takes the marginal per-call cost (difference / extra calls) from the rdcycle/rdinstret CSRs read
  inside the binary. All runs use the xcol gem5 build (branch glasschip-xcol, build/RISCV_XCOL), so the
  only difference between variants is the code; one scalar ESDF run per CPU also runs on the stock
  calibration build to show the patch does not change timing of ordinary code.
- Checks every single-iteration run's outputs bit for bit (or within a tolerance for reordered
  vector reductions) against the host reference produced by the same C sources on the Mac.
- Sweeps the modeled xcol.sdf latency and issue interval.

Run from the repo root:  .venv/bin/python riscv/run_gem5.py [--jobs 8] [--quick]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GEM5_XCOL = ROOT / "third_party/gem5-xcol/build/RISCV_XCOL/gem5.opt"
GEM5_STOCK = ROOT / "third_party/gem5/build/RISCV/gem5.opt"
CFG = ROOT / "riscv/gem5/se_xcol.py"
KB = ROOT / "riscv/build/kbench_rv64"
CB = ROOT / "riscv/build/ctrl_rv64"
KB_HOST = ROOT / "riscv/build/kbench_host"
CB_HOST = ROOT / "riscv/build/ctrl_host"
GRID = ROOT / "data/riscv/grid.bin"
OUTDIR = ROOT / "data/riscv/gem5_runs"
TABLE = ROOT / "riscv/tables/rvv_kernels.json"
APP_HZ = 2.0e9  # vx1.yaml cpu_a/cpu_b nominal
RT_HZ = 1.2e9   # vx1.yaml rt nominal

# Problem sizes per call (riscv/kernels/gc_kern.h): 128 configurations x 61 spheres, 694 pairs.
NCFG, NSPH, NPAIR = 128, 61, 694
UNITS = {"fk": ("configs", NCFG), "esdf": ("sphere_queries", NCFG * NSPH), "selfcol": ("sphere_pairs", NCFG * NPAIR),
         "T7_control": ("control_steps", 1)}

# xcol.sdf timing model (cycles). Headline assumption, see riscv/README.md "Latency model":
# 4 cycles address generation (sub, mul, clamp, convert, index), 2-cycle L1 hit + 4 cycles for
# 8 corner reads on 2 read ports, 3 levels of lerp at 4 cycles each = 12, 1 cycle hinge, rounded to 24.
# Throughput: one xcol.sdf every 4 cycles per unit (limited by the 8 corner reads on 2 ports).
XCOL_HEAD = {"minor": {"lat": 24, "issue": 4}, "o3": {"lat": 4, "pipelined": 0}}
# O3CPU can only express "pipelined every cycle" or "busy for the whole latency", so the headline O3
# case uses latency 4 + unpipelined (one issue per 4 cycles) as the throughput proxy; the two
# latency-24 cases below bracket it.
XCOL_SWEEP = {
    "minor": [{"lat": 8, "issue": 4}, {"lat": 16, "issue": 4}, {"lat": 48, "issue": 4},
              {"lat": 24, "issue": 1}, {"lat": 24, "issue": 8}],
    "o3": [{"lat": 24, "pipelined": 1}, {"lat": 24, "pipelined": 0}, {"lat": 48, "pipelined": 0},
           {"lat": 8, "pipelined": 0}],
}
STAT_KEYS = {
    "sim_insts": r"^simInsts\s+(\S+)",
    "dcache_accesses": r"^system\.cpu\.dcache\.overallAccesses::total\s+(\S+)",
    "dcache_misses": r"^system\.cpu\.dcache\.overallMisses::total\s+(\S+)",
    "l2_misses": r"^system\.l2cache\.overallMisses::total\s+(\S+)",
    "host_seconds": r"^hostSeconds\s+(\S+)",
}


def xcol_tag(x: dict[str, int] | None) -> str:
    if not x:
        return ""
    return f"_lat{x['lat']}" + (f"_iss{x['issue']}" if "issue" in x else f"_pip{x['pipelined']}")


def run_one(job: dict[str, Any]) -> dict[str, Any]:
    """Run one gem5 simulation and parse the BENCH line, dumped outputs and stats."""
    out = OUTDIR / job["name"]
    out.mkdir(parents=True, exist_ok=True)
    gem5 = GEM5_STOCK if job.get("stock") else GEM5_XCOL
    cmd = [str(gem5), f"--outdir={out}", str(CFG), "--cpu", job["cpu"], "--binary", str(job["binary"]),
           "--args", job["args"]]
    x = job.get("xcol")
    if x:
        cmd += ["--xcol-lat", str(x["lat"])]
        if "issue" in x:
            cmd += ["--xcol-issue", str(x["issue"])]
        if "pipelined" in x:
            cmd += ["--xcol-pipelined", str(x["pipelined"])]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    res = dict(job, binary=str(Path(job["binary"]).relative_to(ROOT)), wall_s=round(time.time() - t0, 2),
               gem5_cmd=" ".join(str(c) for c in cmd[1:]).replace(str(ROOT) + "/", ""))
    m = re.search(r"BENCH (.*)", p.stdout)
    if not m:
        res["error"] = (p.stderr or p.stdout)[-800:]
        return res
    kv = dict(t.split("=", 1) for t in m.group(1).split())
    res.update(cycles=int(kv["cycles"]), instret=int(kv["instret"]), check=float(kv["check"]))
    res["dump"] = [l.split()[2] for l in p.stdout.splitlines() if l.startswith("D ")]
    st = (out / "stats.txt").read_text() if (out / "stats.txt").exists() else ""
    for k, rx in STAT_KEYS.items():
        mm = re.search(rx, st, re.M)
        if mm:
            res[k] = float(mm.group(1))
    return res


def host_ref(kernel: str) -> list[str]:
    """Reference outputs from the host build (scalar C, same sources, -ffp-contract=off)."""
    if kernel == "T7_control":
        out = subprocess.run([str(CB_HOST), "scalar", "50", "dump"], capture_output=True, text=True).stdout
    else:
        out = subprocess.run([str(KB_HOST), kernel, "scalar", "1", "dump"], capture_output=True, text=True).stdout
    return [l.split()[2] for l in out.splitlines() if l.startswith("D ")]


def compare(ref: list[str], got: list[str]) -> dict[str, Any]:
    """Bit-exact match count and the largest relative difference."""
    if len(ref) != len(got) or not ref:
        return {"n": len(got), "n_ref": len(ref), "ok": False}
    fmt = "d" if len(ref[0]) == 16 else "f"
    exact = sum(a == b for a, b in zip(ref, got))
    maxrel = 0.0
    maxabs = 0.0
    for a, b in zip(ref, got):
        if a == b:
            continue
        va = struct.unpack(fmt, bytes.fromhex(a)[::-1])[0]
        vb = struct.unpack(fmt, bytes.fromhex(b)[::-1])[0]
        maxabs = max(maxabs, abs(va - vb))
        maxrel = max(maxrel, abs(va - vb) / max(abs(va), 1e-12))
    return {"n": len(ref), "bit_exact": exact, "max_abs_diff": maxabs, "max_rel_diff": maxrel,
            "ok": exact == len(ref) or maxrel < 1e-5}


def jobs(quick: bool) -> list[dict[str, Any]]:
    """Timing runs at two iteration counts (no output dump, so whole-run cache stats stay clean)
    plus one verification run per variant that dumps every output."""
    js: list[dict[str, Any]] = []
    iters = (2, 4)

    def kb(k: str, v: str, cpu: str, x: dict[str, int] | None, sweep: bool = False) -> None:
        for it in iters:
            js.append({"name": f"{k}_{v}_{cpu}{xcol_tag(x)}_{it}", "kernel": k, "variant": v, "cpu": cpu, "iters": it,
                       "binary": KB, "xcol": x, "sweep": sweep, "args": f"{k} {v} {it} grid={GRID}"})
        js.append({"name": f"{k}_{v}_{cpu}{xcol_tag(x)}_verify", "kernel": k, "variant": v, "cpu": cpu, "iters": 1,
                   "binary": KB, "xcol": x, "verify": True, "args": f"{k} {v} 1 dump grid={GRID}"})

    for cpu in ("minor", "o3"):
        for k, variants in (("fk", ["scalar", "rvv"]), ("esdf", ["scalar", "rvv", "xcol"]), ("selfcol", ["scalar", "rvv"])):
            for v in variants:
                kb(k, v, cpu, XCOL_HEAD[cpu] if v == "xcol" else None)
        for v in ("scalar", "rvv"):
            for it in (50, 100):
                js.append({"name": f"T7_control_{v}_{cpu}_{it}", "kernel": "T7_control", "variant": v, "cpu": cpu,
                           "iters": it, "binary": CB, "args": f"{v} {it}"})
            js.append({"name": f"T7_control_{v}_{cpu}_verify", "kernel": "T7_control", "variant": v, "cpu": cpu,
                       "iters": 50, "binary": CB, "verify": True, "args": f"{v} 50 dump"})
        if not quick:
            for x in XCOL_SWEEP[cpu]:
                kb("esdf", "xcol", cpu, x, sweep=True)
            js.append({"name": f"esdf_scalar_{cpu}_stock_2", "kernel": "esdf", "variant": "scalar", "cpu": cpu, "iters": 2,
                       "binary": KB, "stock": True, "args": f"esdf scalar 2 grid={GRID}"})
    return js


def marginal(rs: list[dict[str, Any]], unit_n: int, freq_hz: float) -> dict[str, Any]:
    """Per-call cost from two iteration counts (removes loop setup and cold-cache effects)."""
    lo, hi = sorted(rs, key=lambda r: r["iters"])
    d_it = hi["iters"] - lo["iters"]
    cyc = (hi["cycles"] - lo["cycles"]) / d_it
    ins = (hi["instret"] - lo["instret"]) / d_it
    out = {"cycles_per_call": cyc, "instr_per_call": ins, "cpi": cyc / ins, "ipc": ins / cyc,
           "cold_cycles_per_call": lo["cycles"] / lo["iters"],
           "cycles_per_item": cyc / unit_n, "instr_per_item": ins / unit_n,
           "us_per_call_at_app_cpu_freq": cyc / APP_HZ * 1e6, "us_per_call_at_rt_freq": cyc / RT_HZ * 1e6}
    if "dcache_misses" in hi and "dcache_misses" in lo:
        out["dcache_misses_per_call"] = (hi["dcache_misses"] - lo["dcache_misses"]) / d_it
        out["dcache_accesses_per_call"] = (hi.get("dcache_accesses", 0) - lo.get("dcache_accesses", 0)) / d_it
    out["runs"] = [{k: r[k] for k in ("kernel", "variant", "cpu", "iters", "cycles", "instret", "check", "wall_s",
                                       "sim_insts", "dcache_misses", "gem5_cmd") if k in r} for r in (lo, hi)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--quick", action="store_true", help="skip the latency sweep and the stock-build cross-check")
    a = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    js = jobs(a.quick)
    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:  # threads + subprocesses (ProcessPool is blocked in this sandbox)
        results = list(ex.map(run_one, js))
    errs = [r for r in results if "error" in r]
    for r in errs:
        print("ERROR", r["name"], r["error"][-300:])
    (ROOT / "data/riscv/gem5_raw.json").write_text(json.dumps([{k: v for k, v in r.items() if k != "dump"} for r in results],
                                                              indent=1, default=str))
    refs = {k: host_ref(k) for k in ("fk", "esdf", "selfcol", "T7_control")}
    verify: dict[str, Any] = {}
    for r in results:
        if r.get("dump"):
            verify[r["name"]] = compare(refs[r["kernel"]], r["dump"])
    kernels: dict[str, Any] = {}
    sweep: dict[str, Any] = {"minor": [], "o3": []}
    groups: dict[tuple, list] = {}
    for r in results:
        if "error" in r or r.get("stock") or r.get("verify"):
            continue
        key = (r["kernel"], r["variant"], r["cpu"], xcol_tag(r.get("xcol")), bool(r.get("sweep")))
        groups.setdefault(key, []).append(r)
    for (k, v, cpu, xt, is_sweep), rs in sorted(groups.items()):
        if len(rs) != 2:
            continue
        unit, n = UNITS[k]
        m = marginal(rs, n, APP_HZ)
        m["work_unit"] = unit
        m["items_per_call"] = n
        if v == "xcol":
            m["xcol_model"] = rs[0]["xcol"]
        if is_sweep:
            sweep[cpu].append({"xcol_model": rs[0]["xcol"], "cycles_per_call": m["cycles_per_call"],
                               "cycles_per_item": m["cycles_per_item"]})
            continue
        kernels.setdefault(f"{k}_{v}", {})[cpu] = m
    # The headline xcol point belongs in each sweep too.
    for cpu in ("minor", "o3"):
        h = kernels.get("esdf_xcol", {}).get(cpu)
        if h:
            sweep[cpu].append({"xcol_model": h["xcol_model"], "cycles_per_call": h["cycles_per_call"],
                               "cycles_per_item": h["cycles_per_item"], "headline": True})
        sweep[cpu].sort(key=lambda s: (s["xcol_model"]["lat"], s["xcol_model"].get("issue", 0), s["xcol_model"].get("pipelined", 0)))
    stock = {r["cpu"]: {"cycles": r.get("cycles"), "instret": r.get("instret")} for r in results if r.get("stock")}
    xcol_build_1it = {r["cpu"]: {"cycles": r.get("cycles"), "instret": r.get("instret")} for r in results
                      if r["kernel"] == "esdf" and r["variant"] == "scalar" and r["iters"] == 2 and not r.get("stock")
                      and not r.get("verify")}
    speedups: dict[str, Any] = {}
    for cpu in ("minor", "o3"):
        for k in ("fk", "esdf", "selfcol", "T7_control"):
            s, vv = kernels.get(f"{k}_scalar", {}).get(cpu), kernels.get(f"{k}_rvv", {}).get(cpu)
            if s and vv:
                speedups.setdefault(k, {})[f"rvv_vs_scalar_{cpu}"] = s["cycles_per_call"] / vv["cycles_per_call"]
        s, vv, xx = (kernels.get(f"esdf_{v}", {}).get(cpu) for v in ("scalar", "rvv", "xcol"))
        if s and xx:
            speedups["esdf"][f"xcol_vs_scalar_{cpu}"] = s["cycles_per_call"] / xx["cycles_per_call"]
        if vv and xx:
            speedups["esdf"][f"xcol_vs_rvv_{cpu}"] = vv["cycles_per_call"] / xx["cycles_per_call"]
    # Core L2 hook: per-op CPI, keyed by a kernel name; riscv/core_hook.py maps T5 ops to these.
    cpu_cpi = {f"{k}": {cpu: kernels[k][cpu]["cpi"] for cpu in kernels[k]} for k in kernels}
    try:
        sha = subprocess.run(["git", "-C", str(ROOT / "third_party/gem5-xcol"), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        sha = ""
    table = {
        "provenance": {
            "script": "riscv/run_gem5.py",
            "tool": "gem5 26.0.0.0 SE mode, xcol variant (worktree third_party/gem5-xcol, branch glasschip-xcol, build/RISCV_XCOL)",
            "tool_commit_base": sha,
            "gem5_patch": "riscv/gem5/xcol_gem5.patch",
            "compiler": "zig cc 0.17.1 (LLVM, ld.lld, musl), -O2 -mcpu=baseline_rv64+v -ffp-contract=off -fno-vectorize -fno-slp-vectorize",
            "kernel_sources": ["riscv/kernels/fk.c", "riscv/kernels/esdf.c", "riscv/kernels/selfcol.c", "riscv/kernels/inputs.c",
                               "riscv/kernels/kbench.c", "riscv/kernels/control_rvv.cpp", "riscv/kernels/ctrl_bench.cpp",
                               "workloads/capture/control/control.cpp", "riscv/kernels/franka_spheres.h"],
            "config_files": ["configs/soc/vx1.yaml", "riscv/gem5/se_xcol.py"],
            "date": dt.date.today().isoformat(),
            "wall_s": round(time.time() - t0, 1),
        },
        "setup": {
            "system": "1 core, 64 KiB L1I + 64 KiB L1D (4-way, 2-cycle), 1 MiB L2 (16-way, 12-cycle), DDR4_2400_8x8, 2 GHz, VLEN 256 (same as calib/gem5/se_run.py)",
            "cpu_models": {"minor": "MinorCPU, gem5 defaults (in-order proxy)", "o3": "O3CPU, gem5 defaults (app core proxy)"},
            "method": "marginal per-call cost from iteration counts 2 and 4 (T7: 50 and 100), rdcycle/rdinstret CSRs read in the binary; cold_cycles_per_call is the 2-iteration run divided by 2; dcache_misses_per_call is the marginal whole-run L1D miss count",
            "problem": {"configs": NCFG, "spheres": NSPH, "sphere_queries": NCFG * NSPH, "self_pairs_per_config": NPAIR,
                        "esdf_grid": "80 x 80 x 50 float32 (1.28 MB), 20 mm, analytic conveyor scene",
                        "spheres_source": "cuRobo franka.yml collision spheres and self_collision_ignore (61 spheres, 694 pairs)"},
            "xcol_model": {"headline": XCOL_HEAD,
                           "assumption": "latency 24 cycles, one issue per 4 cycles per unit; O3 headline uses latency 4 unpipelined as the 1-per-4-cycles throughput proxy; the xcol loop is unrolled 4x in software",
                           "memory": "xcol.sdf reads its sphere, descriptor and 8 corners functionally in gem5 (not through the cache model); the latency parameter stands in for L1 hits"},
            "notes": [
                "gem5 RVV timing is coarse: vector instructions become per-register micro-ops on default functional units with no lane-width limit, so O3 RVV numbers are optimistic (calib/README.md).",
                "Scalar baselines are built with -fno-vectorize -fno-slp-vectorize so they contain no vector code.",
                "The T7 RVV variant is an app-core what-if: VX-1's RT core has no vector unit.",
            ],
        },
        "kernels": kernels,
        "speedups": speedups,
        "xcol_latency_sweep": sweep,
        "cpu_cpi": cpu_cpi,
        "verification": verify,
        "stock_build_crosscheck": {"stock_RISCV": stock, "xcol_RISCV_XCOL": xcol_build_1it,
                                   "note": "scalar ESDF, 2 iterations, same binary and args on both gem5 builds"},
        "errors": [r["name"] for r in errs],
    }
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    TABLE.write_text(json.dumps(table, indent=1))
    print(f"wrote {TABLE} ({len(results)} runs, {len(errs)} errors, {time.time() - t0:.0f} s)")
    for k, s in speedups.items():
        print(k, {kk: round(vv, 2) for kk, vv in s.items()})
    bad = {k: v for k, v in verify.items() if not v.get("ok")}
    print("verification:", len(verify) - len(bad), "ok,", len(bad), "not ok", list(bad)[:5])


if __name__ == "__main__":
    main()
