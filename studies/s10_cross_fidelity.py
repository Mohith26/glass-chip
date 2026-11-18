"""Study s10: cross-fidelity accuracy and speed (spec 11.2).

Question: how far are the fast models (L0 analytic, L1 transaction level, and the named fidelity
mixes) from the most detailed model I have (L2), and how much faster are they? And how close is
the L2 CPU model to gem5 (L3)?

What runs
* 21 microbenchmarks (calib/microbench.py: streaming copies, CPU miss stream, all-to-one DRAM
  hotspot, GEMM sizes, attention, convolutions, a cuRobo-style collision kernel, the control,
  estimation and SSM kernels) and scenarios S1-S8 (chip only, studies/runner.py, policy P0).
* Two tiers, because the in-process BookSim 2 / Ramulator 2 backends are thousands of times
  slower than real time:
  Tier A (all microbenchmarks, scenarios S1-S8 over windows up to 8.5 s): all-L0, all-L1,
  "L2t" (NPU L2 from SCALE-Sim tables + CPU L2 from gem5 CPI tables, NoC/DRAM at L1), one-component
  swaps on the L1 base, and the named mixes in configs/fidelity/. Reference = L2t.
  Tier B (all microbenchmarks, S1 over a short window): L1+dram@L2 (Ramulator 2), L1+noc@L2
  (BookSim 2) and full L2 (configs/fidelity/reference.yaml). Reference = full L2. I record the
  effective level of every component from sim.info().
* Error = (config - reference) / reference on each task's mean and p99 response time.
* CPU L2 vs L3: op compute cycles of the control/estimation/SSM kernels in the core vs gem5
  cycles (calib/tables/cpu_gem5.json), with the CPI table both in-sample and leave-one-out.
* Per-request DRAM and NoC: the core's own traces replayed through Ramulator 2 and BookSim 2
  (calib/ramulator/replay.py, calib/booksim/replay.py) when the core exposes trace export.
* Speed: simulated seconds per wall second for each configuration on S1, measured with a plain
  release loop (no power manager), so it is the chip model's own speed.

    S10_RUN_ID=$(cat data/calib/s10_run_id 2>/dev/null) .venv/bin/python -m studies.s10_cross_fidelity \
        [--jobs 10] [--quick] [--b-window 0.04] [--b-speed-seconds 0.002]
(data/calib/s10_run_id is a guard: worker processes of an older, interrupted run see a different id
and exit instead of running. The published results used --b-window 0.04 --b-speed-seconds 0.002.)
writes results/s10.json and results/figures/s10_*.{svg,png}
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(ROOT / "calib"))

PY = sys.executable
SOC = ROOT / "configs" / "soc" / "vx1.yaml"
MB_DIR = ROOT / "calib" / "graphs" / "mb"
L2_TABLES = ROOT / "data" / "calib" / "s10_l2_tables.json"
OUT = ROOT / "results" / "s10.json"
FIG = ROOT / "results" / "figures"
SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
COMPONENTS = ["cpu", "npu", "simt", "dsp", "noc", "dram", "cache", "rt", "dma"]
L2_CAPABLE = ["cpu", "npu", "noc", "dram"]
TARGETS = {"L1_mean": 0.10, "L1_p99": 0.20, "L0_mean": 0.30}
PS_PER_S = 1_000_000_000_000


def stale_run() -> bool:
    """True for processes of an older s10 run that must stop (data/calib/s10_run_id guard)."""
    import os

    f = ROOT / "data" / "calib" / "s10_run_id"
    return f.exists() and os.environ.get("S10_RUN_ID") != f.read_text().strip()


# ---------------------------------------------------------------------------- configurations
def base_params() -> dict[str, Any]:
    """Shared fidelity params for the level configurations (from configs/fidelity/study.yaml)."""
    p = yaml.safe_load((ROOT / "configs" / "fidelity" / "study.yaml").read_text()).get("params", {})
    return dict(p)


def level_cfg(name: str, levels: dict[str, str]) -> dict[str, Any]:
    d = {"name": name, "quantum_ns": 100, "params": base_params()}
    d.update(levels)
    return d


def configurations() -> dict[str, dict[str, Any]]:
    """Tier A configurations (cheap enough for every scenario)."""
    cfgs: dict[str, dict[str, Any]] = {}
    cfgs["L0"] = level_cfg("L0", {c: "L0" for c in COMPONENTS})
    cfgs["L1"] = level_cfg("L1", {c: "L1" for c in COMPONENTS})
    l2 = {c: "L1" for c in COMPONENTS}
    l2["cpu"] = l2["npu"] = l2["rt"] = "L2"
    cfgs["L2t"] = level_cfg("L2t", l2)
    for c in ["cpu", "npu", "simt", "noc", "dram", "cache"]:
        lv = {k: "L1" for k in COMPONENTS}
        lv[c] = "L0"
        cfgs[f"L1+{c}@L0"] = level_cfg(f"L1+{c}@L0", lv)
    for c in ["cpu", "npu"]:
        lv = {k: "L1" for k in COMPONENTS}
        lv[c] = "L2"
        cfgs[f"L1+{c}@L2"] = level_cfg(f"L1+{c}@L2", lv)
    for m in ["analytic", "interactive", "memory_focus", "study"]:
        cfgs[f"mix:{m}"] = yaml.safe_load((ROOT / "configs" / "fidelity" / f"{m}.yaml").read_text())
    return cfgs


def configurations_b() -> dict[str, dict[str, Any]]:
    """Tier B configurations (external DRAM/NoC simulators in the loop)."""
    cfgs: dict[str, dict[str, Any]] = {}
    for name in ("L1", "L2t"):
        cfgs[name] = configurations()[name]
    for c in ("dram", "noc"):
        lv = {k: "L1" for k in COMPONENTS}
        lv[c] = "L2"
        cfgs[f"L1+{c}@L2"] = level_cfg(f"L1+{c}@L2", lv)
    ref = yaml.safe_load((ROOT / "configs" / "fidelity" / "reference.yaml").read_text())
    cfgs["L2full"] = ref
    return cfgs


# ---------------------------------------------------------------------------- L2 tables
def build_l2_tables() -> dict[str, Any]:
    """NPU cycles (SCALE-Sim, weight stationary, dtype rate applied, no stalls) and CPU CPI (gem5)."""
    import run_scalesim as rs

    # The core loads calib/tables/npu_scalesim_<task>.json and the gem5 CPI table by itself when
    # npu/cpu are at L2. Only the microbenchmark NPU ops need extra entries.
    npu: dict[str, float] = {}
    npu_cfg, _ = rs.load_npu()
    rs.ARRAY_COLS = int(npu_cfg["array"][1])
    dram = rs.load_npu()[1]
    tile_bw = dram["data_rate_mts"] * 1e6 * dram["bus_bits_total"] / 8 / float(npu_cfg["freq_hz"]) / npu_cfg["tiles"]
    cfg = rs.write_cfg("ws", npu_cfg)
    jobs = []
    for f in sorted(MB_DIR.glob("T*.json")):
        g = json.loads(f.read_text())
        for op in g["ops"]:
            lo = rs.layer_of(op)
            if lo:
                jobs.append((g["task_id"], op, lo))
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(lambda j: rs.run_layer("ws", cfg, j[2][0]), jobs))
    for (tid, op, (key, mult, sc)), r in zip(jobs, res):
        npu[f"{tid}/{op['op_id']}"] = rs.row_for(op, r, mult, npu_cfg, tile_bw, sc)["vx1_cycles"]

    gem5 = json.loads((ROOT / "calib" / "tables" / "cpu_gem5.json").read_text())["kernels"]
    o3 = {k: v["o3"]["cycles_per_call"] for k, v in gem5.items()}
    static_instr = {k: graph_instr(k) for k in ("T7_control_step", "T6_estimate_step", "T8_ssm_step")}
    # CPI per graph instruction such that L2 cycles = gem5 O3 cycles for that kernel.
    cpi_kernel = {k: o3[k] / static_instr[k] for k in static_instr}
    scalar_kernels = ["T7_control_step", "T6_estimate_step", "T8_ssm_step", "axpy_scalar_4096",
                      "gemm_scalar_64"]
    o3_cpi = {k: gem5[k]["o3"]["cpi"] for k in scalar_kernels}
    type_cpi = float(np.median(list(o3_cpi.values())))
    cpu = {"cpu_kernel": type_cpi, "control_law": type_cpi, "memcopy": type_cpi, "other": type_cpi,
           # in-sample per-op entries for ops that are the measured kernels
           "est.step": cpi_kernel["T6_estimate_step"],
           "T6_mb_est_cpu/estimate_step": cpi_kernel["T6_estimate_step"],
           "T7_mb_ctrl_cpu/control_step": cpi_kernel["T7_control_step"]}
    return {"npu_cycles": npu,
            "_unused_cpu_cpi_leave_one_out_ref": cpu,
            "_notes": {"cpu_type_cpi": "median gem5 O3 CPI over five scalar kernels "
                                       f"{ {k: round(v, 3) for k, v in o3_cpi.items()} }",
                       "cpu_kernel_cpi": cpi_kernel,
                       "npu": "SCALE-Sim ws vx1_cycles per op (one tile; the core divides by tiles)"}}


def graph_instr(kernel: str) -> float:
    """Instruction count per call carried by the workload graph op for a gem5 kernel."""
    tid, oid = {"T7_control_step": ("T7_control", "arm1.control_step"),
                "T6_estimate_step": ("T6_estimate", "est.step"),
                "T8_ssm_step": ("T8_safety", "rt.ssm")}[kernel]
    g = json.loads((ROOT / "workloads" / "graphs" / f"{tid}.json").read_text())
    op = next(o for o in g["ops"] if o["op_id"] == oid)
    return float(sum(op["instr"].values()))


def build_id() -> str:
    import glasschip_core as gc

    return str(getattr(gc, "__build_id__", "unknown"))


def load_l2() -> dict[str, Any]:
    d = json.loads(L2_TABLES.read_text())
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------- workers
def _effective(info: dict[str, Any]) -> dict[str, Any]:
    f = info.get("fidelity", {})
    return {"build_id": info.get("build_id"),
            "levels": {c: f.get(c) for c in COMPONENTS}, "warnings": info.get("warnings", []),
            "backends": info.get("backends"), "l2": info.get("l2")}


def worker_microbench(cfg_name: str, fid: dict[str, Any]) -> dict[str, Any]:
    import glasschip_core as gc

    soc = yaml.safe_load(SOC.read_text())
    idx = json.loads((MB_DIR / "index.json").read_text())
    l2 = load_l2()
    out: dict[str, Any] = {}
    info = {}
    t0 = time.perf_counter()
    for name, tids in idx.items():
        sim = gc.Sim(soc, fid, 1)
        sim.set_l2_tables(l2)
        for t in tids:
            sim.add_task(json.loads((MB_DIR / f"{t}.json").read_text()))
        for t in tids:
            sim.release_job(t, 0, 0)
        jobs = sim.run_until(50 * PS_PER_S // 1000)
        out[name] = {j["task_id"]: {"response_ps": j["response_ps"],
                                    "compute_ps": j["attribution"]["compute"]} for j in jobs}
        info = sim.info()
    return {"kind": "microbench", "config": cfg_name, "results": out, "effective": _effective(info),
            "wall_s": time.perf_counter() - t0}


def scenario_window(scen: str) -> float:
    from studies.runner import resolve_scenario

    s = yaml.safe_load(resolve_scenario(scen).read_text())
    ev = [e["t_s"] for e in (s.get("events") or [])]
    return float(min(s["duration_s"], max(2.0, (max(ev) if ev else 0.0) + 1.5), 8.5))


def worker_scenario(cfg_name: str, fid: dict[str, Any], scen: str, seed: int,
                    window: float | None = None) -> dict[str, Any]:
    from studies.runner import run

    dur = window or scenario_window(scen)
    r = run(scen, "P0", fid, str(SOC), seed=seed, duration_s=dur,
            overrides={"chip_only": True, "l2_tables": load_l2()}, return_objects=True)
    objs = r.pop("_objects")
    info = objs["chip"].inner.sim.info()
    tasks = {t: {"completed": v["completed"], "mean_ms": v["response_ms"]["mean"],
                 "p99_ms": v["response_ms"]["p99"], "missed": v["missed"]}
             for t, v in r["tasks"].items() if v["completed"]}
    return {"kind": "scenario", "config": cfg_name, "scenario": scen, "duration_s": dur,
            "wall_s": r["wall_s"], "tasks": tasks, "effective": _effective(info)}


def worker_speed(cfg_name: str, fid: dict[str, Any], seconds: float) -> dict[str, Any]:
    """Chip-only speed on S1 with the core's own benchmark schedule (no power manager)."""
    import glasschip_core as gc
    sys.path.insert(0, str(ROOT / "core" / "bench"))
    from bench_speed import load_scenario, release_schedule

    soc = yaml.safe_load(SOC.read_text())
    scen = load_scenario("S1_nominal")
    gdir = ROOT / scen.get("graph_dir", "workloads/graphs")
    graphs = {e["graph"]: json.loads((gdir / f"{e['graph']}.json").read_text()) for e in scen["tasks"]}
    sim = gc.Sim(soc, fid, 1)
    sim.set_l2_tables(load_l2())
    for g in graphs.values():
        sim.add_task(g)
    sim.set_middleware(json.loads((gdir / "middleware.json").read_text()), "copy", "single_threaded")
    sched = release_schedule(graphs, scen, seconds, 1)
    t0 = time.perf_counter()
    c0 = time.process_time()
    i, t, step = 0, 0, PS_PER_S // 1000
    while t < seconds * PS_PER_S:
        t += step
        while i < len(sched) and sched[i][0] < t:
            sim.release_job(sched[i][1], sched[i][0], i)
            i += 1
        sim.run_until(t)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    return {"kind": "speed", "config": cfg_name, "sim_s": seconds, "wall_s": wall, "cpu_s": cpu,
            "sim_s_per_wall_s": seconds / wall, "sim_s_per_cpu_s": seconds / cpu,
            "events": sim.info()["events_processed"], "effective": _effective(sim.info())}


def run_worker(spec: dict[str, Any]) -> dict[str, Any]:
    """Run one job in a subprocess (the core is single threaded; this gives parallelism)."""
    r = subprocess.run([PY, "-m", "studies.s10_cross_fidelity", "--worker", json.dumps(spec)],
                       capture_output=True, text=True, cwd=ROOT)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{\"kind\"")]
    if r.returncode != 0 or not lines:
        return {"kind": spec["kind"], "config": spec["config"], "scenario": spec.get("scenario"),
                "tier": spec.get("tier", "A"), "error": (r.stderr or r.stdout)[-2000:]}
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------- analysis
def rel(a: float, b: float) -> float | None:
    return (a - b) / b if b else None


def err_stats(errs: list[float]) -> dict[str, Any]:
    if not errs:
        return {"n": 0}
    a = np.abs(np.asarray(errs))
    return {"n": int(a.size), "mean_abs": float(a.mean()), "median_abs": float(np.median(a)),
            "p99_abs": float(np.percentile(a, 99)), "max_abs": float(a.max()),
            "mean_signed": float(np.mean(errs))}


def analyze(results: list[dict[str, Any]], cfgs: dict[str, Any], ref: str = "L2t",
            scenarios: list[str] | None = None) -> dict[str, Any]:
    mb = {r["config"]: r for r in results if r["kind"] == "microbench" and "error" not in r}
    sc = {(r["config"], r["scenario"]): r for r in results if r["kind"] == "scenario" and "error" not in r}
    scenarios = scenarios or SCENARIOS
    out: dict[str, Any] = {"reference": ref, "microbench": {}, "scenarios": {}, "summary": {}}
    for cname in cfgs:
        if cname == ref:
            continue
        # microbenchmarks: one job per task, response time
        m_err, rows = [], {}
        if cname in mb and ref in mb:
            for b, tasks in mb[ref]["results"].items():
                for t, rv in tasks.items():
                    cv = mb[cname]["results"].get(b, {}).get(t)
                    if cv:
                        e = rel(cv["response_ps"], rv["response_ps"])
                        m_err.append(e)
                        rows[f"{b}/{t}"] = {"ref_us": rv["response_ps"] / 1e6,
                                            "cfg_us": cv["response_ps"] / 1e6, "rel_err": e}
        out["microbench"][cname] = {"per_case": rows, "stats": err_stats(m_err)}
        # scenarios: per task mean and p99
        e_mean, e_p99, srows = [], [], {}
        for s in scenarios:
            a, b = sc.get((cname, s)), sc.get((ref, s))
            if not a or not b:
                continue
            for t, rv in b["tasks"].items():
                cv = a["tasks"].get(t)
                if not cv:
                    continue
                em, ep = rel(cv["mean_ms"], rv["mean_ms"]), rel(cv["p99_ms"], rv["p99_ms"])
                e_mean.append(em)
                e_p99.append(ep)
                srows[f"{s}/{t}"] = {"ref_mean_ms": rv["mean_ms"], "cfg_mean_ms": cv["mean_ms"],
                                     "ref_p99_ms": rv["p99_ms"], "cfg_p99_ms": cv["p99_ms"],
                                     "rel_err_mean": em, "rel_err_p99": ep}
        out["scenarios"][cname] = {"per_task": srows, "mean": err_stats(e_mean), "p99": err_stats(e_p99),
                                   "frac_mean_within_10pct": float(np.mean(np.abs(e_mean) <= 0.10)) if e_mean else None,
                                   "frac_mean_within_30pct": float(np.mean(np.abs(e_mean) <= 0.30)) if e_mean else None,
                                   "frac_p99_within_20pct": float(np.mean(np.abs(e_p99) <= 0.20)) if e_p99 else None}
    # Acceptance (spec 11.2): mean error over scenario task means
    acc = {}
    if "L1" in out["scenarios"]:
        s1 = out["scenarios"]["L1"]
        acc["L1_mean_target"] = TARGETS["L1_mean"]
        acc["L1_mean_abs_err"] = s1["mean"].get("mean_abs")
        acc["L1_mean_pass"] = (s1["mean"].get("mean_abs") or 9) <= TARGETS["L1_mean"]
        acc["L1_p99_target"] = TARGETS["L1_p99"]
        acc["L1_p99_abs_err"] = s1["p99"].get("mean_abs")
        acc["L1_p99_pass"] = (s1["p99"].get("mean_abs") or 9) <= TARGETS["L1_p99"]
    if "L0" in out["scenarios"]:
        s0 = out["scenarios"]["L0"]
        acc["L0_mean_target"] = TARGETS["L0_mean"]
        acc["L0_mean_abs_err"] = s0["mean"].get("mean_abs")
        acc["L0_mean_pass"] = (s0["mean"].get("mean_abs") or 9) <= TARGETS["L0_mean"]
    out["acceptance"] = acc
    # worst offenders for the write-up
    worst = {}
    for cname in ("L0", "L1"):
        rows = out["scenarios"].get(cname, {}).get("per_task", {})
        worst[cname] = sorted(({"case": k, **v} for k, v in rows.items()),
                              key=lambda r: -abs(r["rel_err_mean"] or 0))[:8]
    out["worst"] = worst
    return out


def cpu_vs_l3(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Core op compute cycles vs gem5 cycles for the measured kernels."""
    gem5 = json.loads((ROOT / "calib" / "tables" / "cpu_gem5.json").read_text())["kernels"]
    soc = yaml.safe_load(SOC.read_text())
    f_app = float(soc["blocks"]["cpu_a"]["freq_hz"])
    f_rt = float(soc["blocks"]["rt"]["freq_hz"])
    l2 = json.loads(L2_TABLES.read_text())
    cases = {  # mb name -> (task, gem5 kernel, gem5 cpu model, core clock)
        "control_step_cpu": ("T7_mb_ctrl_cpu", "T7_control_step", "o3", f_app),
        "estimate_step_cpu": ("T6_mb_est_cpu", "T6_estimate_step", "o3", f_app),
        "control_step_rt": ("T7_mb_ctrl_rt", "T7_control_step", "minor", f_rt),
        "ssm_step_rt": ("T8_mb_ssm_rt", "T8_ssm_step", "minor", f_rt),
    }
    mb = {r["config"]: r for r in results if r["kind"] == "microbench" and "error" not in r}
    out: dict[str, Any] = {"method": "op compute time from the core's attribution x core clock, vs "
                                     "gem5 marginal cycles per call; app core vs O3, RT core vs Minor",
                           "cases": {}}
    o3cpi = {k: gem5[k]["o3"]["cpi"] for k in ["T7_control_step", "T6_estimate_step", "T8_ssm_step",
                                               "axpy_scalar_4096", "gemm_scalar_64"]}
    for name, (tid, kern, model, f) in cases.items():
        g5 = gem5[kern][model]["cycles_per_call"]
        row = {"gem5_model": model, "gem5_cycles": g5, "gem5_instr": gem5[kern][model]["instr_per_call"],
               "graph_instr": graph_instr(kern), "levels": {}}
        for cname in ("L0", "L1", "L2t"):
            if cname in mb and name in mb[cname]["results"]:
                cyc = mb[cname]["results"][name][tid]["compute_ps"] * 1e-12 * f
                row["levels"][cname] = {"core_cycles": cyc, "rel_err_vs_gem5": rel(cyc, g5)}
        cpis = {k: gem5[k][model]["cpi"] for k in o3cpi}
        loo = float(np.median([v for k, v in cpis.items() if k != kern]))
        cyc = loo * row["graph_instr"]
        row["levels"]["L2_leave_one_out"] = {"core_cycles": cyc, "cpi": loo,
                                             "rel_err_vs_gem5": rel(cyc, g5),
                                             "note": "graph instr x median gem5 CPI of the other "
                                                     "four scalar kernels (computed here, not in the core)"}
        out["cases"][name] = row
    out["note"] = ("L2t uses the core's own CPU L2 (graph instruction count x gem5 CPI of the "
                   "matching kernel), which is in-sample for these kernels by construction, so its "
                   "error only reflects static vs dynamic instruction count. The leave-one-out row "
                   "is the honest generalization test.")
    return out


# ---------------------------------------------------------------------------- trace replays
def trace_replays() -> dict[str, Any]:
    """Per-request DRAM/NoC comparison against Ramulator 2 / BookSim 2 using the core's traces."""
    import glasschip_core as gc

    probe = gc.Sim(yaml.safe_load(SOC.read_text()), level_cfg("p", {c: "L1" for c in COMPONENTS}), 1)
    have = [n for n in ("export_dram_trace", "export_noc_trace", "dram_trace_open", "noc_trace_open")
            if hasattr(probe, n)]
    if not have:
        return {"status": "not run: the core does not expose DRAM/NoC trace export yet"}
    from s10_traces import run_trace_replays  # calib/s10_traces.py

    return run_trace_replays()


# ---------------------------------------------------------------------------- figures
def figures(an: dict[str, Any], speed: dict[str, Any], an_b: dict[str, Any] | None = None) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIG.mkdir(parents=True, exist_ok=True)
    files = []
    names = [c for c in an["scenarios"] if an["scenarios"][c]["mean"].get("n")]
    m = [an["scenarios"][c]["mean"]["mean_abs"] * 100 for c in names]
    p = [an["scenarios"][c]["p99"]["mean_abs"] * 100 for c in names]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    x = np.arange(len(names))
    ax.bar(x - 0.2, m, 0.4, label="mean response, mean |error|")
    ax.bar(x + 0.2, p, 0.4, label="p99 response, mean |error|")
    ax.axhline(10, ls="--", lw=1, color="k")
    ax.axhline(30, ls=":", lw=1, color="k")
    ax.set_xticks(x, names, rotation=40, ha="right")
    ax.set_ylabel("error vs L2t (%)")
    ax.set_title("Scenario task latency error vs L2t (S1-S8)")
    ax.legend()
    fig.tight_layout()
    for ext in ("svg", "png"):
        f = FIG / f"s10_error_by_config.{ext}"
        fig.savefig(f, dpi=150)
        files.append(str(f.relative_to(ROOT)))
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    for c, s in speed.items():
        tier, name = c.split(":", 1)
        src = an if tier == "A" else (an_b or {"scenarios": {}})
        e = src["scenarios"].get(name, {}).get("mean", {}).get("mean_abs")
        if e is None:
            e = 0.0  # the reference of its tier
        ax.scatter(s["sim_s_per_cpu_s"], e * 100)
        ax.annotate(c, (s["sim_s_per_cpu_s"], e * 100), fontsize=7)
    ax.set_xscale("log")
    ax.set_xlabel("simulated s per CPU s (S1)")
    ax.set_ylabel("mean |error| of task mean latency vs tier reference (%)")
    ax.set_title("Speed vs accuracy")
    fig.tight_layout()
    for ext in ("svg", "png"):
        f = FIG / f"s10_speed_accuracy.{ext}"
        fig.savefig(f, dpi=150)
        files.append(str(f.relative_to(ROOT)))
    plt.close(fig)
    return files


# ---------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker")
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--quick", action="store_true", help="L0/L1/L2 and mixes only, S1 and S6")
    ap.add_argument("--speed-seconds", type=float, default=1.0)
    ap.add_argument("--rebuild-tables", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    ap.add_argument("--b-window", type=float, default=0.15)
    ap.add_argument("--b-speed-seconds", type=float, default=0.005)
    a = ap.parse_args()
    if a.worker:
        if stale_run():
            print(json.dumps({"kind": "aborted"}))
            return
        spec = json.loads(a.worker)
        if spec["kind"] == "microbench":
            r = worker_microbench(spec["config"], spec["fid"])
        elif spec["kind"] == "scenario":
            r = worker_scenario(spec["config"], spec["fid"], spec["scenario"], spec.get("seed", 1),
                                spec.get("window"))
        else:
            r = worker_speed(spec["config"], spec["fid"], spec["seconds"])
        r["tier"] = spec.get("tier", "A")
        print(json.dumps(r))
        return

    t_start = time.time()
    subprocess.run([PY, str(ROOT / "calib" / "microbench.py")], check=True, capture_output=True)
    if a.rebuild_tables or not L2_TABLES.exists():
        L2_TABLES.parent.mkdir(parents=True, exist_ok=True)
        L2_TABLES.write_text(json.dumps(build_l2_tables(), indent=1))
    cfgs = configurations()
    cfgs_b = configurations_b()
    scen = SCENARIOS
    if a.quick:
        cfgs = {k: v for k, v in cfgs.items() if k in ("L0", "L1", "L2t") or k.startswith("mix:")}
        scen = ["S1", "S6"]
    specs = [{"kind": "microbench", "config": c, "fid": f, "tier": "A"} for c, f in cfgs.items()]
    specs += [{"kind": "speed", "config": c, "fid": f, "seconds": a.speed_seconds, "tier": "A"}
              for c, f in cfgs.items()]
    specs += [{"kind": "scenario", "config": c, "fid": f, "scenario": s, "seed": 1, "tier": "A"}
              for s in scen for c, f in cfgs.items()]
    if not a.skip_b:
        specs += [{"kind": "microbench", "config": c, "fid": f, "tier": "B"} for c, f in cfgs_b.items()]
        specs += [{"kind": "speed", "config": c, "fid": f, "seconds": a.b_speed_seconds, "tier": "B"}
                  for c, f in cfgs_b.items()]
        specs += [{"kind": "scenario", "config": c, "fid": f, "scenario": "S1", "seed": 1, "tier": "B",
                   "window": a.b_window} for c, f in cfgs_b.items()]
    # tier B first (slowest), then longest scenario windows
    specs.sort(key=lambda s: (s["tier"] != "B", s["kind"] != "scenario",
                              -scenario_window(s["scenario"]) if s.get("scenario") else 0))
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(run_worker, specs))
    errors = [r for r in results if "error" in r]
    res_a = [r for r in results if r.get("tier") == "A"]
    res_b = [r for r in results if r.get("tier") == "B"]
    an = analyze(res_a, cfgs, "L2t", scen)
    an_b = analyze(res_b, cfgs_b, "L2full", ["S1"]) if res_b else None
    speed = {f"{r['tier']}:{r['config']}": {k: r[k] for k in ("sim_s", "wall_s", "cpu_s", "sim_s_per_wall_s",
                                                              "sim_s_per_cpu_s", "events")}
             for r in results if r["kind"] == "speed" and "error" not in r}
    effective = {f"{r['tier']}:{r['config']}": r["effective"] for r in results
                 if r["kind"] == "microbench" and "error" not in r}
    cpu = cpu_vs_l3(res_a)
    tr = trace_replays()
    figs = figures(an, speed, an_b)
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    doc = {
        "provenance": {"script": "studies/s10_cross_fidelity.py", "git_sha": sha,
                       "core_build_id": build_id(),
                       "worker_build_ids": sorted({str(r.get("effective", {}).get("build_id"))
                                                   for r in results if r.get("effective")}),
                       "date": dt.date.today().isoformat(), "seed": 1,
                       "config_files": ["configs/soc/vx1.yaml", "configs/fidelity/*.yaml",
                                        "configs/scenarios/S1-S8", "calib/graphs/mb/*.json",
                                        "calib/tables/npu_scalesim_*.json", "calib/tables/cpu_gem5.json",
                                        "data/calib/s10_l2_tables.json"],
                       "wall_s": time.time() - t_start},
        "setup": {"configurations": {k: {c: v.get(c) for c in COMPONENTS} for k, v in cfgs.items()},
                  "configurations_tier_b": {k: {c: v.get(c) for c in COMPONENTS} for k, v in cfgs_b.items()},
                  "tier_b_s1_window_s": a.b_window, "tier_b_speed_seconds": a.b_speed_seconds,
                  "effective_levels": effective, "scenarios": scen,
                  "scenario_windows_s": {s: scenario_window(s) for s in scen},
                  "reference": "tier A: L2t (NPU+CPU L2 tables, NoC/DRAM L1); tier B: L2full "
                               "(configs/fidelity/reference.yaml: + Ramulator 2 and BookSim 2)",
                  "targets": TARGETS, "policy": "P0 (FIFO, nominal V/f), chip only"},
        "analysis": an,
        "analysis_tier_b": an_b,
        "cpu_vs_gem5": cpu,
        "trace_replay": tr,
        "speed": speed,
        "figures": figs,
        "errors": errors,
        "raw": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, default=float))
    print(json.dumps({"acceptance": an.get("acceptance"), "errors": len(errors),
                      "tier_b": (an_b or {}).get("scenarios", {}).keys().__len__(),
                      "speed": {k: round(v["sim_s_per_cpu_s"], 3) for k, v in speed.items()}}, indent=1))


if __name__ == "__main__":
    main()
