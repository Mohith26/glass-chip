"""Closed-loop L1 NoC validation against BookSim 2.

(1) Per-packet: the calibration worker's microbenchmarks (calib/s10_traces.py
    export_case: core run at the study mix with noc_model_requests on, NoC CSV
    exported) replayed through BookSim 2 on identical injection times
    (replay_noc), for the legacy L1 model ("before") and the calendar ("after").
(2) Job level: scenario S1 over a 40 ms window at the study mix, task mean
    response with NoC L1 before, L1 after, and NoC L2 (BookSim in the loop).
Writes core/bench/noc_closed_loop.json.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import time
from typing import Any

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(ROOT / "core" / "bench"))
sys.path.insert(0, str(ROOT / "calib"))
import glasschip_core as gc  # noqa: E402
import s10_traces as st  # noqa: E402  (calib/s10_traces.py)
from bench_speed import load_scenario, release_schedule  # noqa: E402

LEGACY = {"noc_l1_arbitration": 0, "noc_backpressure_hold": True, "noc_ni_cycles": 0, "noc_vc_flow_control": False, "noc_rr_tail": 0}
CASES = ["all_to_one_hotspot_6x4MB", "stream_copy_dma_8MB", "gemm_vit_mlp_1024x768x3072", "attention_vit_12h_1024"]
PS_MS = 1_000_000_000


def per_packet() -> dict[str, Any]:
    base_fid = st._fid
    st.WORK = ROOT / "core" / "build" / "noc_closed_loop"
    out: dict[str, Any] = {}
    for model, params in (("before", LEGACY), ("after", {})):
        def fid(variant: str, _p: dict = params) -> dict[str, Any]:
            f = base_fid(variant)
            f["params"].update(_p)
            return f

        st._fid = fid
        for case in CASES:
            _, npath, _ = st.export_case(case, "study_L1")
            r = st.replay_noc(npath)
            out[f"{case}/{model}"] = r
            print(f"{case:28s} {model:6s} core mean {r['core_mean_ns']:.1f} ns  booksim {r['ref_mean_ns']:.1f} ns  "
                  f"ratio {r['core_mean_ns'] / r['ref_mean_ns']:.2f}  p99 ratio {r['core_p99_ns'] / r['ref_p99_ns']:.2f}", flush=True)
    st._fid = base_fid
    return out


def per_message() -> dict[str, Any]:
    """One CSV row per message (up to 4 KB); BookSim splits it into packets created
    at the same cycle and reports the last packet's arrival (calib/booksim/replay.py)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bs_replay", ROOT / "calib/booksim/replay.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)  # type: ignore[union-attr]
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    mb = ROOT / "calib" / "graphs" / "mb"
    index = json.loads((mb / "index.json").read_text())
    work = ROOT / "core" / "build" / "noc_closed_loop"
    work.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}
    for model, params in (("before", LEGACY), ("after", {})):
        for case in CASES:
            fid = st._fid("study_L1")
            fid["params"].update(params)
            sim = gc.Sim(soc, fid, 1)
            for t in index[case]:
                sim.add_task(json.loads((mb / f"{t}.json").read_text()))
            path = work / f"{case}__{model}__msg.csv"
            sim.noc_trace_open(str(path), per_message=True)
            for t in index[case]:
                sim.release_job(t, 0, 0)
            sim.run_until(PS_MS * 50)
            sim.noc_trace_close()
            core = st._read_core(path)
            rows, _ = bs.replay(path, None)
            r = st.compare(core, np.array([x["latency_ns"] for x in rows]))
            out[f"{case}/{model}"] = r
            print(f"[msg] {case:28s} {model:6s} core mean {r['core_mean_ns']:.1f} ns  booksim {r['ref_mean_ns']:.1f} ns  "
                  f"ratio {r['core_mean_ns'] / r['ref_mean_ns']:.2f}  p99 ratio {r['core_p99_ns'] / r['ref_p99_ns']:.2f}", flush=True)
            del sim
    return out


def s1_window(window_ms: int = 40) -> dict[str, Any]:
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    scen = load_scenario("S1_nominal")
    gdir = ROOT / "workloads" / "graphs"
    graphs = {e["graph"]: json.loads((gdir / f"{e['graph']}.json").read_text()) for e in scen["tasks"]}
    mw = json.loads((gdir / "middleware.json").read_text())
    sched = release_schedule(graphs, scen, window_ms / 1000, 1)
    out: dict[str, Any] = {}
    for name, noc, params in (("L1_before", "L1", LEGACY), ("L1_after", "L1", {}), ("L2_booksim", "L2", {})):
        fid = yaml.safe_load((ROOT / "configs/fidelity/study.yaml").read_text())
        fid["noc"] = noc
        fid.setdefault("params", {}).update(params)
        fid["params"]["noc_model_requests"] = True
        sim = gc.Sim(soc, fid, 1)
        for g in graphs.values():
            sim.add_task(g)
        sim.set_middleware(mw, scen.get("transport", "copy"), scen.get("executor", "single_threaded"))
        c0 = time.process_time()
        done, i = [], 0
        for ms in range(window_ms):
            while i < len(sched) and sched[i][0] < (ms + 1) * PS_MS:
                sim.release_job(sched[i][1], sched[i][0], i)
                i += 1
            done += sim.run_until((ms + 1) * PS_MS)
        per: dict[str, list[float]] = {}
        for d in done:
            per.setdefault(d["task_id"], []).append(d["response_ps"] / 1e9)
        out[name] = {"cpu_s": time.process_time() - c0, "backends": sim.info()["backends"],
                     "tasks": {t: {"jobs": len(v), "mean_ms": float(np.mean(v))} for t, v in sorted(per.items())}}
        print(name, {t: round(v["mean_ms"], 3) for t, v in out[name]["tasks"].items()}, f"cpu {out[name]['cpu_s']:.1f}s", flush=True)
        del sim
    l2 = out["L2_booksim"]["tasks"]
    for name in ("L1_before", "L1_after"):
        out[name]["rel_err_vs_booksim"] = {t: (v["mean_ms"] - l2[t]["mean_ms"]) / l2[t]["mean_ms"] for t, v in out[name]["tasks"].items() if t in l2}
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", nargs="+", default=["per_packet", "per_message", "s1_40ms"])
    args = ap.parse_args()
    path = ROOT / "core/bench/noc_closed_loop.json"
    doc = json.loads(path.read_text()) if path.exists() else {}
    doc["provenance"] = {"script": "core/bench/noc_closed_loop.py", "date": _dt.date.today().isoformat(),
                         "configs": ["configs/soc/vx1.yaml", "configs/fidelity/study.yaml", "configs/scenarios/S1_nominal.yaml"],
                         "seed": 1, "legacy_params": LEGACY, "build_id": gc.__build_id__}
    fns = {"per_packet": per_packet, "per_message": per_message, "s1_40ms": s1_window}
    for part in args.parts:
        doc[part] = fns[part]()
        doc.setdefault("build_ids", {})[part] = gc.__build_id__
        path.write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
