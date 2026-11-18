"""Per-request DRAM and NoC comparison for study s10 (and s15).

I run small workloads through glasschip_core with its trace exports on, then replay exactly the
same requests through the external simulators:

* DRAM: sim.dram_trace_open() rows (time_ns, addr, rw, bytes, latency_ns). time_ns is the
  request's arrival at the controller; latency_ns is the core's controller-boundary latency
  (queueing, bank timing, refresh, turnaround, data burst; NoC excluded). I replay
  (time_ns, addr, rw, bytes) through Ramulator 2.1 (calib/ramulator/replay.py, derived
  LPDDR5X-8533 config) and compare per request.
* NoC: sim.noc_trace_open() rows (time_ns, src, dst, bytes, latency_ns), one per packet of at
  most 256 B, latency = injection to tail arrival. I replay them through BookSim 2 in trace mode
  (calib/booksim/replay.py) and compare per packet.

Both replays are open loop: the external simulator sees the core's arrival times and does not
feed its own latencies back. Differences in latency therefore do not change later arrivals, which
is the right setup for judging the latency model itself.

Run standalone: .venv/bin/python calib/s10_traces.py  (writes data/calib/s10_traces/*)
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(HERE / "ramulator"))
sys.path.insert(0, str(HERE / "booksim"))
sys.path.insert(0, str(ROOT / "core" / "bench"))

WORK = ROOT / "data" / "calib" / "s10_trace_work"
MB = HERE / "graphs" / "mb"
PS_PER_S = 1_000_000_000_000

MB_CASES = ["stream_copy_dma_8MB", "random_access_cpu_64B", "all_to_one_hotspot_6x4MB",
            "gemm_vit_mlp_1024x768x3072", "attention_vit_12h_1024"]
S1_WINDOW_S = 0.010


def _fid(variant: str) -> dict[str, Any]:
    f = yaml.safe_load((ROOT / "configs" / "fidelity" / "study.yaml").read_text())
    f.setdefault("params", {})["noc_model_requests"] = True
    if variant == "dram_noc_L0":
        f["dram"] = "L0"
        f["noc"] = "L0"
    return f


def export_case(case: str, variant: str, soc_path: Path = ROOT / "configs/soc/vx1.yaml") -> tuple[Path, Path, float]:
    import glasschip_core as gc

    soc = yaml.safe_load(soc_path.read_text())
    sim = gc.Sim(soc, _fid(variant), 1)
    out = WORK / f"{case}__{variant}"
    out.mkdir(parents=True, exist_ok=True)
    dpath, npath = out / "dram.csv", out / "noc.csv"
    t0 = time.perf_counter()
    if case.startswith("S1"):
        from bench_speed import load_scenario, release_schedule

        scen = load_scenario("S1_nominal")
        gdir = ROOT / "workloads" / "graphs"
        graphs = {e["graph"]: json.loads((gdir / f"{e['graph']}.json").read_text()) for e in scen["tasks"]}
        for g in graphs.values():
            sim.add_task(g)
        sim.set_middleware(json.loads((gdir / "middleware.json").read_text()), "copy", "single_threaded")
        sim.dram_trace_open(str(dpath))
        sim.noc_trace_open(str(npath))
        sched = release_schedule(graphs, scen, S1_WINDOW_S, 1)
        for i, (t, tid) in enumerate(sched):
            sim.release_job(tid, t, i)
        sim.run_until(int(S1_WINDOW_S * PS_PER_S))
    else:
        tids = json.loads((MB / "index.json").read_text())[case]
        for t in tids:
            sim.add_task(json.loads((MB / f"{t}.json").read_text()))
        sim.dram_trace_open(str(dpath))
        sim.noc_trace_open(str(npath))
        for t in tids:
            sim.release_job(t, 0, 0)
        sim.run_until(PS_PER_S // 20)
    sim.dram_trace_close()
    sim.noc_trace_close()
    return dpath, npath, time.perf_counter() - t0


def _read_core(path: Path) -> np.ndarray:
    with open(path) as f:
        return np.array([float(r["latency_ns"]) for r in csv.DictReader(f)])


def compare(core: np.ndarray, ref: np.ndarray) -> dict[str, Any]:
    ok = (ref > 0) & np.isfinite(ref)
    c, r = core[ok], ref[ok]
    e = (c - r) / r
    return {
        "n": int(ok.sum()),
        "core_mean_ns": float(c.mean()), "core_p50_ns": float(np.percentile(c, 50)),
        "core_p99_ns": float(np.percentile(c, 99)),
        "ref_mean_ns": float(r.mean()), "ref_p50_ns": float(np.percentile(r, 50)),
        "ref_p99_ns": float(np.percentile(r, 99)),
        "rel_err_mean": float((c.mean() - r.mean()) / r.mean()),
        "rel_err_p99": float((np.percentile(c, 99) - np.percentile(r, 99)) / np.percentile(r, 99)),
        "per_request_abs_rel_err_median": float(np.median(np.abs(e))),
        "per_request_abs_rel_err_p90": float(np.percentile(np.abs(e), 90)),
        "pearson_r": float(np.corrcoef(c, r)[0, 1]) if c.size > 2 and c.std() > 0 and r.std() > 0 else None,
    }


def replay_dram(dpath: Path) -> dict[str, Any]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ram_replay", HERE / "ramulator" / "replay.py")
    ram = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ram)  # type: ignore[union-attr]

    core = _read_core(dpath)
    if core.size == 0:
        return {"n": 0}
    t0 = time.perf_counter()
    res, summ = ram.replay(dpath, variant="lpddr5x_8533_derived", out_csv=dpath.with_name("dram_ramulator.csv"))
    out = compare(core, res["latency_ns"])
    out["ramulator_achieved_gbps"] = summ["achieved_gbps"]
    out["replay_wall_s"] = time.perf_counter() - t0
    return out


def replay_noc(npath: Path) -> dict[str, Any]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("bs_replay", HERE / "booksim" / "replay.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)  # type: ignore[union-attr]
    core = _read_core(npath)
    if core.size == 0:
        return {"n": 0}
    t0 = time.perf_counter()
    rows, _ = bs.replay(npath, npath.with_name("noc_booksim.csv"))
    ref = np.array([r["latency_ns"] for r in rows])
    out = compare(core, ref)
    out["replay_wall_s"] = time.perf_counter() - t0
    return out


def run_trace_replays(cases: list[str] | None = None, variants: tuple[str, ...] = ("study_L1",),
                      keep_csv: bool = False) -> dict[str, Any]:
    """Per-request comparisons. Only the L1 traces are meaningful per request: at L0 the core
    reports a mean-field latency and does not throttle issue, so its arrival stream is not a real
    request stream (I checked: replaying it through Ramulator gives queueing the L0 model never
    sees). L0 is compared at the job level in s10 instead."""
    import os

    rid = ROOT / "data" / "calib" / "s10_run_id"
    if rid.exists() and os.environ.get("S10_RUN_ID") != rid.read_text().strip():
        raise RuntimeError("stale s10 run: aborting (data/calib/s10_run_id guard)")
    cases = cases or (MB_CASES + ["S1_first_10ms"])
    out: dict[str, Any] = {
        "method": __doc__.split("\n\n")[1].strip().replace("\n", " "),
        "dram_reference": "Ramulator 2.1, lpddr5x_8533_derived (calib/ramulator/configs)",
        "noc_reference": "BookSim 2 trace mode, VX-1 4x4 mesh (calib/booksim/booksim_cfg.py)",
        "variants": {"study_L1": "configs/fidelity/study.yaml with noc_model_requests true (DRAM and NoC L1)",
                     "dram_noc_L0": "same with dram and noc at L0 (analytic); its own trace"},
        "cases": {}}
    for case in cases:
        for v in variants:
            key = f"{case}/{v}"
            try:
                d, n, wall = export_case(case, v)
                out["cases"][key] = {"export_wall_s": wall, "dram": replay_dram(d), "noc": replay_noc(n)}
                if not keep_csv:
                    for f in d.parent.glob("*.csv"):
                        f.unlink()
            except Exception as e:  # keep going; report the failure
                out["cases"][key] = {"error": repr(e)[:500]}
            print(key, json.dumps({k: (v2.get("rel_err_mean") if isinstance(v2, dict) else v2)
                                   for k, v2 in out["cases"][key].items()}), flush=True)
    for v in variants:
        for comp in ("dram", "noc"):
            errs = [c[comp]["rel_err_mean"] for k, c in out["cases"].items()
                    if k.endswith("/" + v) and isinstance(c.get(comp), dict) and c[comp].get("n")]
            p99 = [c[comp]["rel_err_p99"] for k, c in out["cases"].items()
                   if k.endswith("/" + v) and isinstance(c.get(comp), dict) and c[comp].get("n")]
            out.setdefault("summary", {})[f"{v}/{comp}"] = {
                "n_cases": len(errs),
                "mean_abs_rel_err_of_mean": float(np.mean(np.abs(errs))) if errs else None,
                "mean_abs_rel_err_of_p99": float(np.mean(np.abs(p99))) if p99 else None}
    return out


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    r = run_trace_replays()
    (WORK / "summary.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r["summary"], indent=1))
