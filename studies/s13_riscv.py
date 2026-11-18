"""s13: RISC-V depth study (spec stretch goals S1 and S2).

Question: how do cuRobo-style planner kernels and the T7 control step perform as scalar RV64 code
versus RVV (VLEN 256), what does the xcol.sdf study instruction (custom-0, Xglasscol) buy on the
ESDF collision kernel, and what does that do to replan (T5) latency on VX-1 when TrajOpt's
collision step runs on the RV64 app cores?

Inputs
- riscv/tables/rvv_kernels.json, written by riscv/run_gem5.py (gem5 SE runs, Minor and O3 CPUs).
  Pass --regen-gem5 to rerun it first (about 4 minutes on 8 threads).
- workloads/graphs/T5_plan.json and riscv/core_hook.py (T5 variants with collision on 8 app cores).
- studies/runner.py for scenario S3 (human enters) with glasschip_core in the loop.

Outputs: results/s13.json and results/figures/s13_*.{svg,png}.
Every number in the JSON comes from a run made by this script or by riscv/run_gem5.py.

Run from the repo root:  .venv/bin/python studies/s13_riscv.py [--seeds 0 1] [--duration 10] [--jobs 4]
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core" / "build"))

import yaml  # noqa: E402

from riscv.core_hook import fidelity_with_cpu_l2, load_table, t5_variant  # noqa: E402

OUT = ROOT / "results" / "s13.json"
FIG = ROOT / "results" / "figures"
T5_PATH = ROOT / "workloads/graphs/T5_plan.json"
SOC = ROOT / "configs/soc/vx1.yaml"
FID = ROOT / "configs/fidelity/interactive.yaml"
VARIANTS = ["simt", "scalar", "rvv", "xcol"]
L2_HIT_CYCLES = 14  # gem5 config: 2-cycle L1 miss detection + 12-cycle L2 (riscv/gem5/se_xcol.py)


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- kernels (from gem5 table)
def kernel_section(tab: dict[str, Any]) -> dict[str, Any]:
    """Per-kernel cycles, speedups, and a memory-timing bound for xcol.sdf."""
    K = tab["kernels"]
    rows = []
    for k in ("fk", "esdf", "selfcol", "T7_control"):
        for cpu in ("minor", "o3"):
            s, v = K[f"{k}_scalar"][cpu], K[f"{k}_rvv"][cpu]
            row = {"kernel": k, "cpu": cpu, "work_unit": s["work_unit"], "items_per_call": s["items_per_call"],
                   "scalar_cycles_per_call": s["cycles_per_call"], "rvv_cycles_per_call": v["cycles_per_call"],
                   "scalar_instr_per_call": s["instr_per_call"], "rvv_instr_per_call": v["instr_per_call"],
                   "scalar_cpi": s["cpi"], "rvv_cpi": v["cpi"],
                   "rvv_speedup": s["cycles_per_call"] / v["cycles_per_call"]}
            if k == "esdf":
                x = K["esdf_xcol"][cpu]
                # xcol.sdf reads memory functionally in gem5, so it never misses. Pessimistic bound: charge
                # it every L1D miss the scalar kernel took per call, at the L2 hit latency, with no overlap.
                bound = x["cycles_per_call"] + s.get("dcache_misses_per_call", 0) * L2_HIT_CYCLES
                row.update(xcol_cycles_per_call=x["cycles_per_call"], xcol_instr_per_call=x["instr_per_call"],
                           xcol_cpi=x["cpi"], xcol_model=x["xcol_model"],
                           xcol_speedup_vs_scalar=s["cycles_per_call"] / x["cycles_per_call"],
                           xcol_speedup_vs_rvv=v["cycles_per_call"] / x["cycles_per_call"],
                           xcol_cycles_per_call_memory_bound=bound,
                           xcol_speedup_vs_scalar_memory_bound=s["cycles_per_call"] / bound,
                           xcol_speedup_vs_rvv_memory_bound=v["cycles_per_call"] / bound,
                           scalar_dcache_misses_per_call=s.get("dcache_misses_per_call"),
                           rvv_dcache_misses_per_call=v.get("dcache_misses_per_call"),
                           cycles_per_query={"scalar": s["cycles_per_item"], "rvv": v["cycles_per_item"],
                                             "xcol": x["cycles_per_item"]},
                           instr_per_query={"scalar": s["instr_per_item"], "rvv": v["instr_per_item"],
                                            "xcol": x["instr_per_item"]})
            rows.append(row)
    return {"rows": rows, "xcol_latency_sweep": tab["xcol_latency_sweep"],
            "verification": {"runs_checked": len(tab["verification"]),
                             "all_ok": all(v["ok"] for v in tab["verification"].values()),
                             "all_bit_exact": all(v.get("bit_exact") == v["n"] for v in tab["verification"].values())},
            "stock_build_crosscheck": tab["stock_build_crosscheck"]}


# ---------------------------------------------------------------- T5 alone on the core
def t5_alone(tab: dict[str, Any], variant: str, esdf_override: dict[str, float] | None = None) -> dict[str, Any]:
    import glasschip_core as gc

    soc = yaml.safe_load(SOC.read_text())
    fid = fidelity_with_cpu_l2(yaml.safe_load(FID.read_text()))
    base = json.loads(T5_PATH.read_text())
    t = copy.deepcopy(tab)
    if esdf_override:
        t["kernels"]["esdf_xcol"]["o3"].update(esdf_override)
    g, cpi, summ = t5_variant(base, t, variant)
    sim = gc.Sim(soc, fid, seed=1)
    sim.add_task(g)
    if cpi:
        sim.set_l2_tables({"cpu_cpi": cpi})
    sim.release_job("T5_plan", 0, tag=0)
    j = sim.run_until(5_000_000_000_000)[0]
    return {"variant": variant, "response_ms": j["response_ps"] / 1e9, "deadline_ms": g["deadline_us"] / 1e3,
            "meets_deadline": j["response_ps"] <= g["deadline_us"] * 1e6,
            "attribution_ms": {k: v / 1e9 for k, v in j["attribution"].items() if v},
            "collision_ms_ideal_8_cores": summ.get("collision_ms_ideal_shards_at_2ghz"),
            "collision_blocks": summ.get("blocks", [])[:1]}


def t5_section(tab: dict[str, Any]) -> dict[str, Any]:
    alone = {v: t5_alone(tab, v) for v in VARIANTS}
    # xcol latency sensitivity (O3 sweep points fed through the same path)
    sens = []
    x0 = tab["kernels"]["esdf_xcol"]["o3"]
    for s in tab["xcol_latency_sweep"]["o3"]:
        cyc_item = s["cycles_per_item"]
        cpi = cyc_item / x0["instr_per_item"]
        r = t5_alone(tab, "xcol", {"cpi": cpi, "cycles_per_item": cyc_item})
        sens.append({"xcol_model": s["xcol_model"], "esdf_cycles_per_query": cyc_item, "t5_response_ms": r["response_ms"]})
    return {"alone": alone, "xcol_sensitivity_o3": sens,
            "fidelity": "configs/fidelity/interactive.yaml with cpu: L2 (gem5 CPI tables)",
            "note": "T5 released once on an idle SoC; collision step on 8 app cores (both clusters) for the CPU variants"}


# ---------------------------------------------------------------- S3 end to end (subprocess per run)
def s3_one(variant: str, seed: int, duration: float) -> dict[str, Any]:
    from studies.runner import run

    tab = load_table()
    base = json.loads(T5_PATH.read_text())
    g, cpi, _ = t5_variant(base, tab, variant)
    ov: dict[str, Any] = {"keep_jobs": True, "fidelity": {"cpu": "L2"}, "graphs": {"T5_plan": g}}
    if cpi:
        ov["l2_tables"] = {"cpu_cpi": cpi}
    r = run("S3", "P0", seed=seed, duration_s=duration, overrides=ov)
    t5 = [j for j in r["jobs"] if j["task_id"] == "T5_plan"]
    ev = 3.0  # S3 human_enter event time (configs/scenarios/S3_*.yaml)
    burst = [j for j in t5 if ev * 1e12 <= j["release_ps"] <= (ev + 1.0) * 1e12]
    return {"variant": variant, "seed": seed, "duration_s": duration,
            "t5": r["tasks"]["T5_plan"],
            "t5_jobs": [{"release_ms": j["release_ps"] / 1e9, "response_ms": j["response_ps"] / 1e9, "missed": j["missed"],
                         "attribution_ms": {k: v / 1e9 for k, v in j["attribution"].items() if v > 1e6}} for j in t5],
            "t5_after_human_enter": [{"release_ms": j["release_ps"] / 1e9, "response_ms": j["response_ps"] / 1e9} for j in burst],
            "other_tasks_missed": {k: v.get("missed") for k, v in r["tasks"].items() if k != "T5_plan"},
            "other_tasks_completed": {k: v.get("completed") for k, v in r["tasks"].items() if k != "T5_plan"},
            "chains_ms": r.get("chains"), "robot": r.get("robot"), "wall_s": r.get("wall_s")}


def s3_section(seeds: list[int], duration: float, jobs: int, reuse: bool = False) -> dict[str, Any]:
    tmp = ROOT / "data/riscv/s13_s3"
    tmp.mkdir(parents=True, exist_ok=True)

    def go(vs: tuple[str, int]) -> dict[str, Any]:
        v, s = vs
        out = tmp / f"{v}_{s}.json"
        if reuse and out.exists():
            return json.loads(out.read_text())
        p = subprocess.run([sys.executable, __file__, "--s3-one", v, str(s), str(duration), str(out)],
                           capture_output=True, text=True, cwd=ROOT)
        if not out.exists():
            return {"variant": v, "seed": s, "error": (p.stderr or p.stdout)[-1500:]}
        return json.loads(out.read_text())

    combos = [(v, s) for v in VARIANTS for s in seeds]
    with ThreadPoolExecutor(jobs) as ex:
        runs = list(ex.map(go, combos))
    summary: dict[str, Any] = {}
    for v in VARIANTS:
        rs = [r for r in runs if r["variant"] == v and "error" not in r]
        resp = [j["response_ms"] for r in rs for j in r["t5_jobs"]]
        burst = [j["response_ms"] for r in rs for j in r["t5_after_human_enter"]]
        missed = sum(j["missed"] for r in rs for j in r["t5_jobs"])
        if resp:
            resp_s = sorted(resp)
            summary[v] = {"t5_jobs": len(resp), "t5_mean_ms": sum(resp) / len(resp), "t5_max_ms": resp_s[-1],
                          "t5_p50_ms": resp_s[len(resp_s) // 2], "t5_missed": missed,
                          "t5_after_human_enter_mean_ms": (sum(burst) / len(burst)) if burst else None,
                          "t5_after_human_enter_jobs": len(burst),
                          "t5_completed_by_seed": {r["seed"]: r["t5"].get("completed") for r in rs},
                          "t5_skipped_by_seed": {r["seed"]: r["t5"].get("skipped") for r in rs}}
            other: dict[str, dict[str, int]] = {}
            for r in rs:
                for t, m in r["other_tasks_missed"].items():
                    o = other.setdefault(t, {"missed": 0, "completed": 0})
                    o["missed"] += m or 0
                    o["completed"] += r.get("other_tasks_completed", {}).get(t) or 0
            summary[v]["other_tasks_with_misses"] = {t: o for t, o in other.items() if o["missed"]}
    return {"seeds": seeds, "duration_s": duration, "policy": "P0",
            "fidelity": "interactive + cpu L2 (all variants, including the SIMT baseline)",
            "summary": summary, "runs": runs}


def headline(res: dict[str, Any]) -> dict[str, Any]:
    """Derived numbers quoted in riscv/README.md, so every quoted value has a JSON field."""
    rows = {(r["kernel"], r["cpu"]): r for r in res["kernels"]["rows"]}
    al = res["t5"]["alone"]
    h: dict[str, Any] = {
        "esdf_xcol_vs_rvv_o3": rows[("esdf", "o3")]["xcol_speedup_vs_rvv"],
        "esdf_xcol_vs_scalar_o3": rows[("esdf", "o3")]["xcol_speedup_vs_scalar"],
        "esdf_xcol_vs_rvv_minor": rows[("esdf", "minor")]["xcol_speedup_vs_rvv"],
        "esdf_xcol_vs_scalar_minor": rows[("esdf", "minor")]["xcol_speedup_vs_scalar"],
        "t5_alone_ms": {v: al[v]["response_ms"] for v in al},
        "t5_deadline_ms": al["simt"]["deadline_ms"],
        "t5_alone_xcol_vs_rvv_reduction_pct": 100 * (1 - al["xcol"]["response_ms"] / al["rvv"]["response_ms"]),
    }
    if "s3" in res:
        sm = res["s3"]["summary"]
        h["s3"] = {v: {"t5_mean_ms": sm[v]["t5_mean_ms"], "t5_after_human_enter_mean_ms": sm[v]["t5_after_human_enter_mean_ms"],
                       "t5_jobs": sm[v]["t5_jobs"], "t5_missed": sm[v]["t5_missed"],
                       "t5_skipped_total": sum(x or 0 for x in sm[v]["t5_skipped_by_seed"].values()),
                       "other_tasks_with_misses": sm[v]["other_tasks_with_misses"]} for v in sm}
        h["s3_burst_xcol_vs_rvv_reduction_pct"] = 100 * (1 - sm["xcol"]["t5_after_human_enter_mean_ms"] /
                                                          sm["rvv"]["t5_after_human_enter_mean_ms"])
        h["s3_cpu_vs_simt_slowdown_rvv"] = sm["rvv"]["t5_mean_ms"] / sm["simt"]["t5_mean_ms"]
        h["s3_cpu_vs_simt_slowdown_xcol"] = sm["xcol"]["t5_mean_ms"] / sm["simt"]["t5_mean_ms"]
    return h


# ---------------------------------------------------------------- figures
def figures(res: dict[str, Any]) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIG.mkdir(parents=True, exist_ok=True)
    made = []
    rows = res["kernels"]["rows"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, cpu in zip(axes, ("minor", "o3")):
        ks = [r for r in rows if r["cpu"] == cpu]
        labels = [r["kernel"] for r in ks]
        xs = range(len(ks))
        sc = [r["scalar_cycles_per_call"] for r in ks]
        rv = [r["rvv_cycles_per_call"] for r in ks]
        xc = [r.get("xcol_cycles_per_call", 0) for r in ks]
        w = 0.27
        ax.bar([x - w for x in xs], sc, w, label="scalar", color="#8a8f98")
        ax.bar(list(xs), rv, w, label="RVV (VLEN 256)", color="#3b82f6")
        ax.bar([x + w for x in xs], [c if c else 0 for c in xc], w, label="xcol.sdf (study)", color="#f59e0b")
        ax.set_yscale("log")
        ax.set_xticks(list(xs))
        ax.set_xticklabels(labels)
        ax.set_ylabel("cycles per call (gem5, marginal)")
        ax.set_title(f"gem5 {'MinorCPU (in-order)' if cpu == 'minor' else 'O3CPU (out-of-order)'}")
        for x, r in zip(xs, ks):
            ax.text(x, r["rvv_cycles_per_call"] * 1.15, f"{r['rvv_speedup']:.1f}x", ha="center", fontsize=8, color="#1d4ed8")
            if r.get("xcol_cycles_per_call"):
                ax.text(x + w, r["xcol_cycles_per_call"] * 1.15, f"{r['xcol_speedup_vs_scalar']:.1f}x", ha="center",
                        fontsize=8, color="#b45309")
    axes[0].legend(fontsize=8, loc="upper right")
    fig.suptitle("s13: RV64 planner and control kernels, speedup over scalar labeled")
    fig.tight_layout()
    for ext in ("svg", "png"):
        p = FIG / f"s13_kernels.{ext}"
        fig.savefig(p, dpi=150)
        made.append(str(p.relative_to(ROOT)))
    plt.close(fig)

    # T5 alone + S3
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    al = res["t5"]["alone"]
    names = VARIANTS
    vals = [al[v]["response_ms"] for v in names]
    cols = ["#10b981", "#8a8f98", "#3b82f6", "#f59e0b"]
    axes[0].bar(names, vals, color=cols)
    axes[0].axhline(al["simt"]["deadline_ms"], color="#ef4444", ls="--", lw=1)
    axes[0].text(3.4, al["simt"]["deadline_ms"] * 1.03, "deadline", color="#ef4444", ha="right", fontsize=8)
    for i, v in enumerate(vals):
        axes[0].text(i, v * 1.02, f"{v:.1f} ms", ha="center", fontsize=8)
    axes[0].set_ylabel("T5 response (ms), SoC otherwise idle")
    axes[0].set_title("Where TrajOpt collision runs")
    axes[0].set_xticks(range(len(names)))
    axes[0].set_xticklabels(["SIMT\n(baseline)", "8 app cores\nscalar", "8 app cores\nRVV", "8 app cores\nRVV + xcol.sdf"],
                            fontsize=8)
    s3 = res["s3"]["summary"]
    data = [[j["response_ms"] for r in res["s3"]["runs"] if r.get("variant") == v and "error" not in r for j in r["t5_jobs"]]
            for v in names]
    pos = [i for i, d in enumerate(data) if d]
    axes[1].boxplot([data[i] for i in pos], positions=pos, widths=0.5)
    axes[1].set_xticks(range(len(names)))
    axes[1].set_xticklabels(["SIMT", "scalar", "RVV", "RVV+xcol"], fontsize=8)
    axes[1].axhline(al["simt"]["deadline_ms"], color="#ef4444", ls="--", lw=1)
    axes[1].set_ylabel("T5 response (ms) in S3, all replans")
    axes[1].set_title(f"S3 human enters, seeds {res['s3']['seeds']}")
    for i in pos:
        m = s3[names[i]]
        axes[1].text(i, max(data[i]) * 1.03, f"mean {m['t5_mean_ms']:.1f}", ha="center", fontsize=8)
    fig.tight_layout()
    for ext in ("svg", "png"):
        p = FIG / f"s13_replan.{ext}"
        fig.savefig(p, dpi=150)
        made.append(str(p.relative_to(ROOT)))
    plt.close(fig)

    # xcol latency sweep
    fig, ax = plt.subplots(figsize=(6, 3.8))
    for cpu, mk in (("minor", "o"), ("o3", "s")):
        sw = [s for s in res["kernels"]["xcol_latency_sweep"][cpu]]
        lab = [f"L{s['xcol_model']['lat']}/" + (f"i{s['xcol_model']['issue']}" if "issue" in s["xcol_model"]
                                               else ("pipe" if s["xcol_model"]["pipelined"] else "busy")) for s in sw]
        ax.plot(range(len(sw)), [s["cycles_per_item"] for s in sw], mk + "-", label=cpu)
        for i, (l, s) in enumerate(zip(lab, sw)):
            ax.annotate(l, (i, s["cycles_per_item"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
    rv = {r["cpu"]: r["cycles_per_query"]["rvv"] for r in res["kernels"]["rows"] if r["kernel"] == "esdf"}
    for cpu, ls in (("minor", ":"), ("o3", "--")):
        ax.axhline(rv[cpu], ls=ls, color="#3b82f6", lw=1)
        ax.text(0, rv[cpu] * 1.04, f"RVV {cpu}", color="#3b82f6", fontsize=7)
    ax.set_ylabel("cycles per sphere query")
    ax.set_xlabel("xcol.sdf model (latency / issue)")
    ax.set_title("xcol.sdf latency sensitivity (ESDF kernel)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    for ext in ("svg", "png"):
        p = FIG / f"s13_xcol_sweep.{ext}"
        fig.savefig(p, dpi=150)
        made.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--regen-gem5", action="store_true")
    ap.add_argument("--skip-s3", action="store_true")
    ap.add_argument("--reuse-s3", action="store_true", help="reuse per-run S3 results in data/riscv/s13_s3")
    ap.add_argument("--s3-one", nargs=4, metavar=("VARIANT", "SEED", "DURATION", "OUT"))
    a = ap.parse_args()
    if a.s3_one:
        v, s, d, out = a.s3_one
        Path(out).write_text(json.dumps(s3_one(v, int(s), float(d)), default=str))
        return
    t0 = time.time()
    if a.regen_gem5:
        subprocess.run([sys.executable, str(ROOT / "riscv/run_gem5.py")], check=True, cwd=ROOT)
    tab = load_table()
    res: dict[str, Any] = {
        "provenance": {"script": "studies/s13_riscv.py", "git_sha": git_sha(),
                       "config_files": ["configs/soc/vx1.yaml", "configs/fidelity/interactive.yaml",
                                        "configs/scenarios/S3_human_enters.yaml", "configs/policies/P0_nominal.yaml",
                                        "riscv/gem5/se_xcol.py"],
                       "inputs": ["riscv/tables/rvv_kernels.json", "workloads/graphs/T5_plan.json", "riscv/core_hook.py"],
                       "seed": a.seeds, "date": dt.date.today().isoformat(),
                       "gem5_table_provenance": tab["provenance"]},
        "question": "RVV vs scalar per kernel, xcol.sdf speedup on the ESDF collision kernel, and T5 replan latency on VX-1",
        "honesty": [
            "xcol.sdf is a design-space study instruction in the custom-0 opcode space, not a proposal for any vendor's ISA.",
            "xcol.sdf is functional in gem5 with a modeled latency; its memory reads are functional (not timed through caches). "
            "The memory_bound fields charge it the scalar kernel's L1D misses at L2 latency with no overlap.",
            "gem5 RVV timing is coarse and optimistic on O3 (per-register micro-ops, no lane-width limit).",
            "VX-1 is fictional. T5 normally runs collision on the SIMT cluster; the CPU variants are what-if mappings.",
        ],
        "kernels": kernel_section(tab),
        "t5": t5_section(tab),
    }
    if not a.skip_s3:
        res["s3"] = s3_section(a.seeds, a.duration, a.jobs, reuse=a.reuse_s3)
    res["headline"] = headline(res)
    res["figures"] = figures(res) if "s3" in res else []
    res["provenance"]["wall_s"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
