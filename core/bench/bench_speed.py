"""Simulation-speed benchmark for glasschip-core.

Runs a scenario (default S1 nominal pick) with the real task graphs from
workloads/graphs/ at each fidelity mix, stepping the simulator in 1 ms
increments the way the co-simulation bridge does, and reports simulated
seconds per wall-clock second on one core.

Usage: .venv/bin/python core/bench/bench_speed.py [--scenario S1_nominal] [--seconds 2] [--mix interactive study analytic]
Writes core/bench/speed.json.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import platform
import sys
import time
from typing import Any

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "build"))
import glasschip_core as gc  # noqa: E402

PS_PER_US = 1_000_000
PS_PER_MS = 1_000_000_000


def load_scenario(name: str) -> dict[str, Any]:
    """Load configs/scenarios/<name>.yaml."""
    return yaml.safe_load((ROOT / "configs" / "scenarios" / f"{name}.yaml").read_text())


def release_schedule(graphs: dict[str, dict], scenario: dict[str, Any], duration_s: float, seed: int) -> list[tuple[int, str]]:
    """Build a deterministic (t_ps, task_id) release list: periodic tasks at
    offset + k*period with uniform jitter, sporadic tasks as Poisson arrivals
    at `sporadic_rate_hz` (scenario override) separated by at least the period."""
    rng = np.random.default_rng(seed)
    out: list[tuple[int, str]] = []
    end_ps = int(duration_s * 1e12)
    for entry in scenario["tasks"]:
        g = graphs[entry["graph"]]
        rel = g["release"]
        ov = entry.get("overrides") or {}
        start = int(entry.get("enabled_at_s", 0) * 1e12) + int(rel.get("offset_us", 0) * PS_PER_US)
        period = int(ov.get("period_us", rel.get("period_us", g["deadline_us"])) * PS_PER_US)
        jitter = int(rel.get("jitter_us", 0) * PS_PER_US)
        if rel["kind"] == "periodic":
            t = start
            while t < end_ps:
                j = int(rng.integers(0, jitter + 1)) if jitter else 0
                out.append((t + j, g["task_id"]))
                t += period
        else:
            rate = float(ov.get("sporadic_rate_hz", 1e12 / max(period, 1)))
            t = start
            while True:
                t += max(period, int(rng.exponential(1.0 / rate) * 1e12))
                if t >= end_ps:
                    break
                out.append((t, g["task_id"]))
    out.sort()
    return out


def run(mix: str, scenario_name: str, seconds: float, seed: int = 1) -> dict[str, Any]:
    """Run one scenario at one fidelity mix and return speed + deadline stats."""
    soc = yaml.safe_load((ROOT / "configs" / "soc" / "vx1.yaml").read_text())
    fid = yaml.safe_load((ROOT / "configs" / "fidelity" / f"{mix}.yaml").read_text())
    scen = load_scenario(scenario_name)
    gdir = ROOT / scen.get("graph_dir", "workloads/graphs")
    graphs = {e["graph"]: json.loads((gdir / f"{e['graph']}.json").read_text()) for e in scen["tasks"]}
    mw = json.loads((gdir / "middleware.json").read_text())
    sim = gc.Sim(soc, fid, seed)
    t_setup = time.perf_counter()
    for g in graphs.values():
        sim.add_task(g)
    sim.set_middleware(mw, scen.get("transport", "copy"), scen.get("executor", "single_threaded"))
    sched = release_schedule(graphs, scen, seconds, seed)
    step = PS_PER_MS
    t_end = int(seconds * 1e12)
    done: list[dict] = []
    i = 0
    skipped = 0
    t0 = time.perf_counter()
    c0 = time.process_time()
    t = 0
    while t < t_end:
        t += step
        while i < len(sched) and sched[i][0] < t:
            if sim.release_job(sched[i][1], sched[i][0], i) < 0:
                skipped += 1
            i += 1
        done.extend(sim.run_until(t))
        sim.counters(True)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    per_task: dict[str, dict[str, float]] = {}
    for c in done:
        s = per_task.setdefault(c["task_id"], {"jobs": 0, "missed": 0, "resp": []})
        s["jobs"] += 1
        s["missed"] += int(c["missed"])
        s["resp"].append(c["response_ps"] / 1e9)
    summary = {
        k: {
            "jobs": v["jobs"],
            "missed": v["missed"],
            "p50_ms": float(np.percentile(v["resp"], 50)),
            "p99_ms": float(np.percentile(v["resp"], 99)),
        }
        for k, v in sorted(per_task.items())
    }
    info = sim.info()
    return {
        "mix": mix,
        "scenario": scenario_name,
        "sim_seconds": seconds,
        "wall_seconds": wall,
        "setup_seconds": t0 - t_setup,
        "cpu_seconds": cpu,
        "speed_x_realtime": seconds / wall,
        "speed_x_realtime_cpu": seconds / cpu,
        "events_by_kind": info.get("events_by_kind", {}),
        "events": info["events_processed"],
        "events_per_wall_s": info["events_processed"] / wall,
        "releases": len(sched),
        "skipped": skipped,
        "completed": len(done),
        "warnings": info["warnings"],
        "trace_hash": sim.trace_hash(),
        "tasks": summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="S1_nominal")
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--mix", nargs="+", default=["interactive", "study", "analytic"])
    ap.add_argument("--out", default=str(ROOT / "core" / "bench" / "speed.json"))
    args = ap.parse_args()
    results = []
    for mix in args.mix:
        r = run(mix, args.scenario, args.seconds)
        results.append(r)
        print(f"{mix:12s} {r['speed_x_realtime_cpu']:.3f}x real time (cpu {r['cpu_seconds']:.2f}s, wall {r['wall_seconds']:.2f}s)  events {r['events']:,}  "
              f"jobs {r['completed']}  {r['events_by_kind']}")
        for t, s in r["tasks"].items():
            print(f"    {t:22s} jobs {s['jobs']:5d} missed {s['missed']:4d} p50 {s['p50_ms']:8.3f} ms p99 {s['p99_ms']:8.3f} ms")
    doc = {
        "provenance": {
            "script": "core/bench/bench_speed.py",
            "date": _dt.date.today().isoformat(),
            "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
            "configs": ["configs/soc/vx1.yaml"] + [f"configs/fidelity/{m}.yaml" for m in args.mix]
            + [f"configs/scenarios/{args.scenario}.yaml"],
            "seed": 1,
            "note": "single-threaded; simulator stepped in 1 ms increments with counters() read each step",
        },
        "results": results,
    }
    pathlib.Path(args.out).write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
