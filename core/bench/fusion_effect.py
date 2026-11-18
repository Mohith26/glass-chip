"""Effect of the NPU compiler fusion assumptions on T4_vla.

Measures (a) DRAM bytes per T4_vla inference with the task running alone and
(b) T4_vla response time inside scenario S1, with fusion off and on
(params fuse_attention / fuse_elementwise), at the interactive mix.
Writes core/bench/fusion.json.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
from typing import Any

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(ROOT / "core" / "bench"))
import glasschip_core as gc  # noqa: E402
from bench_speed import load_scenario, release_schedule  # noqa: E402

PS_MS = 1_000_000_000


def make(params: dict[str, Any], graphs: dict[str, dict], mix: str = "interactive") -> Any:
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    fid = yaml.safe_load((ROOT / f"configs/fidelity/{mix}.yaml").read_text())
    fid.setdefault("params", {}).update(params)
    sim = gc.Sim(soc, fid, 1)
    for g in graphs.values():
        sim.add_task(g)
    sim.set_middleware(json.loads((ROOT / "workloads/graphs/middleware.json").read_text()), "copy", "single_threaded")
    return sim


def main() -> None:
    scen = load_scenario("S1_nominal")
    graphs = {e["graph"]: json.loads((ROOT / "workloads/graphs" / f"{e['graph']}.json").read_text()) for e in scen["tasks"]}
    variants = {
        "no_fusion": {"fuse_attention": False, "fuse_elementwise": False},
        "attention_only": {"fuse_attention": True, "fuse_elementwise": False},
        "elementwise_only": {"fuse_attention": False, "fuse_elementwise": True},
        "both (default)": {"fuse_attention": True, "fuse_elementwise": True},
    }
    out: dict[str, Any] = {}
    for name, params in variants.items():
        alone = make(params, {"T4_vla": graphs["T4_vla"]})
        alone.release_job("T4_vla", 0, 0)
        done = alone.run_until(500 * PS_MS)
        c = alone.counters(False)
        sim = make(params, graphs)
        sched = release_schedule(graphs, scen, 2.0, 1)
        i, resp = 0, []
        for ms in range(2000):
            while i < len(sched) and sched[i][0] < (ms + 1) * PS_MS:
                sim.release_job(sched[i][1], sched[i][0], i)
                i += 1
            resp += [d["response_ps"] / 1e9 for d in sim.run_until((ms + 1) * PS_MS) if d["task_id"] == "T4_vla"]
        out[name] = {
            "params": params,
            "alone_dram_bytes_per_inference": c["dram_io"]["dram_bytes"],
            "alone_response_ms": done[0]["response_ps"] / 1e9,
            "alone_attribution_ms": {k: v / 1e9 for k, v in done[0]["attribution"].items() if v},
            "s1_jobs": len(resp),
            "s1_p50_ms": float(np.percentile(resp, 50)),
            "s1_p99_ms": float(np.percentile(resp, 99)),
        }
        r = out[name]
        print(f"{name:18s} DRAM/inference {r['alone_dram_bytes_per_inference'] / 1e9:.3f} GB  alone {r['alone_response_ms']:.2f} ms  "
              f"S1 p50 {r['s1_p50_ms']:.2f} ms p99 {r['s1_p99_ms']:.2f} ms")
    doc = {
        "provenance": {
            "script": "core/bench/fusion_effect.py",
            "date": _dt.date.today().isoformat(),
            "configs": ["configs/soc/vx1.yaml", "configs/fidelity/interactive.yaml", "configs/scenarios/S1_nominal.yaml"],
            "seed": 1,
            "note": "S1 run for 2 s simulated; 'alone' = one T4_vla job with no other tasks",
        },
        "results": out,
    }
    (ROOT / "core/bench/fusion.json").write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
