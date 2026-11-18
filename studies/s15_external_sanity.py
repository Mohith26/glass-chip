"""Study s15: external sanity checks (spec 11.3).

1. Orin-like vs public numbers. I run three MLPerf Edge models (calib/public_models.py: ResNet-50
   v1.5, RetinaNet ResNeXt50-FPN 800x800, BERT-Large seq 384; task graphs in calib/graphs/) on
   configs/soc/orin_like.yaml and compare the modeled single-job latency with NVIDIA's published
   MLPerf v3.1 single-stream latency for Jetson AGX Orin (calib/tables/orin_public.json).
   Target: within 30%. This is a sanity check of the modeling approach, not validation of any
   hardware, and nothing here describes VX-1 or any other real chip.

   Mapping choice, fixed before looking at the comparison: on a GPU one kernel occupies every SM,
   so the primary Orin runs set params.npu_max_tiles_per_op = 16 (all SMs). The VX-1 default
   (4 tiles per layer) is reported as a sensitivity case. No other parameter was changed for Orin.

2. NPU model vs SCALE-Sim directly: for every conv/matmul op in the VX-1 NPU tasks I compare the
   core's L1 closed-form systolic cycles for one tile (sim.npu_gemm_cycles, and the packed
   depthwise formula) with SCALE-Sim's cycles for the same layer (calib/tables/npu_scalesim_*.json,
   weight stationary, dtype rate applied). I also check that the L2 path reproduces the table:
   running each task at npu L2 with one tile per op must give compute cycles equal to the table.

3. DRAM L1 vs Ramulator 2 on identical traces: the per-request comparison from s10
   (calib/s10_traces.py) is reported here as well, read from results/s10.json so there is one
   source of truth.

    .venv/bin/python -m studies.s15_external_sanity
writes results/s15.json and results/figures/s15_*.{svg,png}
"""

from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "build"))
import glasschip_core as gc  # noqa: E402

OUT = ROOT / "results" / "s15.json"
FIG = ROOT / "results" / "figures"
PS_PER_MS = 1_000_000_000
MIXES = ["analytic", "interactive", "memory_focus", "study"]
MODELS = {  # task id -> model name in orin_public.json
    "T90_resnet50": "ResNet-50 v1.5",
    "T91_retinanet": "RetinaNet (ResNeXt50 FPN)",
    "T92_bert_large": "BERT-Large",
}
MAPPINGS = {"gpu_wide_primary": {"npu_max_tiles_per_op": 16},
            "vx1_default_sensitivity": {}}
TARGET = 0.30


def orin_check() -> dict[str, Any]:
    soc = yaml.safe_load((ROOT / "configs" / "soc" / "orin_like.yaml").read_text())
    pub = json.loads((ROOT / "calib" / "tables" / "orin_public.json").read_text())
    pub_ms = {r["model"]: r["single_stream_latency_ms"] for r in pub["results"]}
    rows: dict[str, Any] = {}
    for tid, model in MODELS.items():
        g = json.loads((ROOT / "calib" / "graphs" / f"{tid}.json").read_text())
        rows[tid] = {"model": model, "public_single_stream_ms": pub_ms[model],
                     "graph_gmac": g["summary"]["macs"] / 1e9, "runs": {}}
        for mname, params in MAPPINGS.items():
            for mix in MIXES:
                fid = yaml.safe_load((ROOT / "configs" / "fidelity" / f"{mix}.yaml").read_text())
                sim = gc.Sim(soc, fid, 1)
                if params:
                    sim.set_params(params)
                sim.add_task(g)
                t0 = time.perf_counter()
                sim.release_job(tid, 0, 0)
                jobs = sim.run_until(60_000 * PS_PER_MS)
                j = jobs[0]
                ms = j["response_ps"] / PS_PER_MS
                rows[tid]["runs"][f"{mname}/{mix}"] = {
                    "modeled_ms": ms, "rel_err": (ms - pub_ms[model]) / pub_ms[model],
                    "within_30pct": abs(ms - pub_ms[model]) / pub_ms[model] <= TARGET,
                    "attribution_ms": {k: v / PS_PER_MS for k, v in j["attribution"].items() if v},
                    "warnings": sim.info()["warnings"], "wall_s": time.perf_counter() - t0}
    summ = {}
    for mname in MAPPINGS:
        for mix in MIXES:
            errs = [rows[t]["runs"][f"{mname}/{mix}"]["rel_err"] for t in MODELS]
            summ[f"{mname}/{mix}"] = {"n_within_30pct": int(sum(abs(e) <= TARGET for e in errs)),
                                     "n_models": len(errs), "mean_abs_rel_err": float(np.mean(np.abs(errs))),
                                     "rel_err_by_model": dict(zip(MODELS, errs))}
    return {"models": rows, "summary": summ, "target": TARGET,
            "public_source": pub["results"][0]["source_url"],
            "note": "sanity check only; public numbers are MLPerf v3.1 single-stream p90 latency, "
                    "batch 1, precision unverified (see orin_public.json)"}


# ---------------------------------------------------------------------------- NPU vs SCALE-Sim
def npu_vs_scalesim() -> dict[str, Any]:
    soc = yaml.safe_load((ROOT / "configs" / "soc" / "vx1.yaml").read_text())
    npu = soc["blocks"]["npu"]
    rows_, cols = npu["array"]
    fid = yaml.safe_load((ROOT / "configs" / "fidelity" / "study.yaml").read_text())
    sim = gc.Sim(soc, fid, 1)
    # Diagnostic: the same closed form with weight loading NOT overlapped with streaming
    # (params.npu_weight_load_overlap false), which is how SCALE-Sim's WS mode charges each fold.
    sim_no = gc.Sim(soc, fid, 1)
    sim_no.set_params({"npu_weight_load_overlap": False})
    out: dict[str, Any] = {"tasks": {}}
    no_err: list[float] = []
    all_err, all_w = [], []
    for f in sorted((ROOT / "calib" / "tables").glob("npu_scalesim_T*.json")):
        tab = json.loads(f.read_text())
        tid = tab["task_id"]
        g = json.loads((ROOT / "workloads" / "graphs" / f"{tid}.json").read_text())
        ops = {o["op_id"]: o for o in g["ops"]}
        per, errs, w, by_type = {}, [], [], {}
        tot_core = tot_ss = 0.0
        for oid, r in tab["dataflows"]["ws"]["ops"].items():
            o = ops[oid]
            s = o["shape"]
            dt_ = o.get("dtype", "int8")
            if o["type"] in ("conv2d", "dwconv2d"):
                gr = int(s.get("groups", 1))
                C, K = int(s["C"]), int(s["K"])
                Ho, Wo = int(s.get("Ho") or s["out"][2]), int(s.get("Wo") or s["out"][3])
                M, Kd, N, B = Ho * Wo, (C // gr) * int(s["R"]) * int(s["S"]), K // gr, gr
                if gr > 1 and gr >= C:  # packed depthwise, as in core/src/components/engines.cpp
                    rate = 1.0 if dt_ in ("int8",) else npu.get("bf16_rate_fraction", 0.25)
                    core = math.ceil(B / cols) * (M + rows_ + cols - 1) / rate
                    core_no = core
                else:
                    core = sim.npu_gemm_cycles(M, Kd, N, B, 1, dt_)
                    core_no = sim_no.npu_gemm_cycles(M, Kd, N, B, 1, dt_)
            else:
                B = int(s.get("batch", 1)) * int(s.get("heads", 1))
                core = sim.npu_gemm_cycles(int(s["M"]), int(s["K"]), int(s["N"]), B, 1, dt_)
                core_no = sim_no.npu_gemm_cycles(int(s["M"]), int(s["K"]), int(s["N"]), B, 1, dt_)
            ss = r["vx1_cycles"]
            no_err.append((core_no - ss) / ss)
            e = (core - ss) / ss
            per[oid] = {"type": o["type"], "dtype": dt_, "core_l1_cycles": core, "scalesim_cycles": ss,
                        "rel_err": e, "scalesim_utilization": r["utilization"]}
            errs.append(e)
            w.append(ss)
            tot_core += core
            tot_ss += ss
            by_type.setdefault(o["type"], []).append(e)
        out["tasks"][tid] = {
            "n_ops": len(per), "total_core_l1_cycles": tot_core, "total_scalesim_cycles": tot_ss,
            "total_rel_err": (tot_core - tot_ss) / tot_ss,
            "per_op_abs_rel_err_median": float(np.median(np.abs(errs))),
            "per_op_abs_rel_err_p90": float(np.percentile(np.abs(errs), 90)),
            "by_type_median_rel_err": {k: float(np.median(v)) for k, v in by_type.items()},
            "ops": per}
        all_err += errs
        all_w += w
    a = np.abs(np.asarray(all_err))
    out["overall"] = {"n_ops": int(a.size), "per_op_abs_rel_err_median": float(np.median(a)),
                      "per_op_abs_rel_err_p90": float(np.percentile(a, 90)),
                      "cycle_weighted_abs_rel_err": float(np.average(a, weights=all_w)),
                      "no_weight_load_overlap_diagnostic": {
                          "per_op_abs_rel_err_median": float(np.median(np.abs(no_err))),
                          "per_op_abs_rel_err_p90": float(np.percentile(np.abs(no_err), 90)),
                          "cycle_weighted_abs_rel_err": float(np.average(np.abs(no_err), weights=all_w))}}
    out["l2_consistency"] = l2_consistency()
    return out


def l2_consistency() -> dict[str, Any]:
    """Run each NPU task at npu L2 with one tile per op; compute cycles must equal the table."""
    soc = yaml.safe_load((ROOT / "configs" / "soc" / "vx1.yaml").read_text())
    f_npu = float(soc["blocks"]["npu"]["freq_hz"])
    fid = yaml.safe_load((ROOT / "configs" / "fidelity" / "study.yaml").read_text())
    res = {}
    for f in sorted((ROOT / "calib" / "tables").glob("npu_scalesim_T*.json")):
        tab = json.loads(f.read_text())
        tid = tab["task_id"]
        g = json.loads((ROOT / "workloads" / "graphs" / f"{tid}.json").read_text())
        table = {f"{tid}/{k}": v["vx1_cycles"] for k, v in tab["dataflows"]["ws"]["ops"].items()}
        sim = gc.Sim(soc, fid, 1)
        sim.set_params({"npu_max_tiles_per_op": 1})
        sim.set_l2_tables({"npu_cycles": table})
        sim.add_task(g)
        sim.trace_enable(True)
        sim.release_job(tid, 0, 0)
        sim.run_until(10_000 * PS_PER_MS)
        ev = [e for e in sim.trace_drain() if e["kind"] == "op" and e["op_id"] in tab["dataflows"]["ws"]["ops"]]
        info = sim.info()["l2"]
        # op durations include memory stalls; compare the minimum, i.e. op duration >= table cycles
        dur_cycles = {e["op_id"]: e["dur_ps"] * 1e-12 * f_npu for e in ev}
        ratios = [dur_cycles[k] / tab["dataflows"]["ws"]["ops"][k]["vx1_cycles"] for k in dur_cycles]
        res[tid] = {"table_hits": info.get("npu_table_hits"), "fallbacks": info.get("npu_fallbacks"),
                    "ops_traced": len(ev),
                    "op_duration_over_table_min": float(min(ratios)) if ratios else None,
                    "op_duration_over_table_median": float(np.median(ratios)) if ratios else None,
                    "note": "durations include memory stall time, so ratios >= 1 are expected; a "
                            "minimum near 1 shows the L2 path uses the table cycles"}
    return res


def dram_vs_ramulator() -> dict[str, Any]:
    p = ROOT / "results" / "s10.json"
    if not p.exists():
        return {"status": "results/s10.json not found; run s10 first"}
    tr = json.loads(p.read_text()).get("trace_replay", {})
    cases = {k: v.get("dram") for k, v in (tr.get("cases") or {}).items()}
    return {"source": "results/s10.json#trace_replay", "dram_reference": tr.get("dram_reference"),
            "cases": cases, "summary": {k: v for k, v in (tr.get("summary") or {}).items() if "dram" in k}}


def figures(orin: dict[str, Any], npu: dict[str, Any]) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIG.mkdir(parents=True, exist_ok=True)
    files = []
    fig, ax = plt.subplots(figsize=(7, 4))
    tids = list(MODELS)
    x = np.arange(len(tids))
    pub = [orin["models"][t]["public_single_stream_ms"] for t in tids]
    ax.bar(x - 0.3, pub, 0.2, label="public MLPerf v3.1 (AGX Orin)")
    for i, mix in enumerate(["analytic", "interactive", "study"]):
        v = [orin["models"][t]["runs"][f"gpu_wide_primary/{mix}"]["modeled_ms"] for t in tids]
        ax.bar(x - 0.1 + 0.2 * i, v, 0.2, label=f"modeled, {mix}")
    ax.set_yscale("log")
    ax.set_xticks(x, [MODELS[t] for t in tids], rotation=10)
    ax.set_ylabel("single-stream latency (ms, log)")
    ax.set_title("Orin-like model vs public numbers (sanity check)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    for ext in ("svg", "png"):
        p = FIG / f"s15_orin_sanity.{ext}"
        fig.savefig(p, dpi=150)
        files.append(str(p.relative_to(ROOT)))
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for tid, t in npu["tasks"].items():
        c = [o["core_l1_cycles"] for o in t["ops"].values()]
        s = [o["scalesim_cycles"] for o in t["ops"].values()]
        ax.scatter(s, c, s=8, label=tid)
    lo, hi = 1e2, 1e9
    ax.plot([lo, hi], [lo, hi], "k--", lw=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("SCALE-Sim cycles (one 64x64 tile, ws)")
    ax.set_ylabel("core L1 closed-form cycles")
    ax.set_title("NPU L1 vs SCALE-Sim per op")
    ax.legend(fontsize=7)
    fig.tight_layout()
    for ext in ("svg", "png"):
        p = FIG / f"s15_npu_vs_scalesim.{ext}"
        fig.savefig(p, dpi=150)
        files.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    return files


def main() -> None:
    t0 = time.time()
    orin = orin_check()
    npu = npu_vs_scalesim()
    dram = dram_vs_ramulator()
    figs = figures(orin, npu)
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    doc = {"provenance": {"script": "studies/s15_external_sanity.py", "git_sha": sha,
                          "date": dt.date.today().isoformat(), "seed": 1, "wall_s": time.time() - t0,
                          "config_files": ["configs/soc/orin_like.yaml", "configs/soc/vx1.yaml",
                                           "configs/fidelity/*.yaml", "calib/graphs/T9*.json",
                                           "calib/tables/orin_public.json", "calib/tables/npu_scalesim_*.json"]},
           "orin_sanity": orin, "npu_vs_scalesim": npu, "dram_vs_ramulator": dram, "figures": figs}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, default=float))
    print(json.dumps(orin["summary"], indent=1))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "ops"} for k, v in npu["tasks"].items()}, indent=1))
    print(json.dumps(npu["overall"]), json.dumps(npu["l2_consistency"], indent=1))


if __name__ == "__main__":
    main()
