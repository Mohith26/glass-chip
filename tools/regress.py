"""Determinism and golden-metrics regression check (spec 11.5).

1. Determinism: a canonical short co-sim run (S1, P0, seed 0, 2 s) is executed twice; the
   core's event-stream hash, the robot body-pose hash and the metrics must match exactly
   between the two runs and the recorded golden hashes.
2. Golden metrics: S1 to S8 run chip-only at the interactive fidelity mix (P0, seed 0, 3 s;
   S3 and S8 in closed loop is not needed for timing) and selected metrics (per-task p50/p99
   response, misses, average power, DRAM traffic) must stay within 1 % of the golden file.

Usage:
    .venv/bin/python tools/regress.py --check      # exit 1 on drift
    .venv/bin/python tools/regress.py --update     # rewrite tools/regress_golden.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "tools" / "regress_golden.json"
SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
TOL = 0.01
ABS_TOL_MS = 0.002     # response times below 2 us are compared absolutely


def canonical_run() -> dict:
    import numpy as np

    from studies.runner import run
    r = run("S1", "P0", seed=0, duration_s=2.0, return_objects=True)
    objs = r.pop("_objects")
    core = objs["chip"].inner.sim
    poses = np.asarray(objs["sim"].frames_pos, dtype=np.float32).tobytes() + \
        np.asarray(objs["sim"].frames_quat, dtype=np.float32).tobytes()
    ev = json.dumps(objs["sim"].robot_events, sort_keys=True, default=str).encode()
    return {"core_trace_hash": str(core.trace_hash()),
            "robot_hash": hashlib.sha256(poses + ev).hexdigest()[:16],
            "misses": r["misses"]["total"], "grasp_attempts": r["robot"]["grasp_attempts"]}


def golden_metrics(scenario: str) -> dict:
    from studies.runner import run
    r = run(scenario, "P0", seed=0, duration_s=3.0, overrides={"chip_only": True})
    out = {"misses_total": r["misses"]["total"], "avg_power_w": round(r["power"]["avg_power_w"], 6),
           "dram_gbps": round(r["activity"]["dram_gbps"], 6)}
    for t, v in r["tasks"].items():
        out[f"{t}.completed"] = v["completed"]
        out[f"{t}.missed"] = v["missed"]
        out[f"{t}.p50_ms"] = round(v["response_ms"]["p50"], 6)
        out[f"{t}.p99_ms"] = round(v["response_ms"]["p99"], 6)
    return out


def _gm(s: str) -> tuple[str, dict]:
    return s, golden_metrics(s)


def collect(workers: int) -> dict:
    from multiprocessing import Pool
    a, b = canonical_run(), canonical_run()
    with Pool(workers) as p:
        gm = dict(p.map(_gm, SCENARIOS))
    from studies.runner import core_build_id
    return {"determinism": {"run_a": a, "run_b": b, "identical": a == b}, "metrics": gm,
            "core_build": core_build_id(), "tolerance": TOL}


def compare(cur: dict, gold: dict) -> list[str]:
    errs = []
    if not cur["determinism"]["identical"]:
        errs.append(f"non-deterministic canonical run: {cur['determinism']}")
    ga = gold["determinism"]["run_a"]
    for k in ("core_trace_hash", "robot_hash"):
        if cur["determinism"]["run_a"][k] != ga[k]:
            errs.append(f"{k} changed: {ga[k]} -> {cur['determinism']['run_a'][k]}")
    for s, gm in gold["metrics"].items():
        cm = cur["metrics"].get(s, {})
        for k, g in gm.items():
            c = cm.get(k)
            if c is None:
                errs.append(f"{s}: metric {k} missing")
                continue
            if k.endswith("_ms") and abs(c - g) <= ABS_TOL_MS:
                continue
            denom = max(abs(g), 1e-12)
            if (g == 0 and c != 0) or (g != 0 and abs(c - g) / denom > TOL):
                errs.append(f"{s}: {k} {g} -> {c}")
        for k in cm:
            if k not in gm:
                errs.append(f"{s}: new metric {k}")
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--update", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cur = collect(a.workers)
    if a.update:
        GOLDEN.write_text(json.dumps(cur, indent=1, sort_keys=True))
        print(f"wrote {GOLDEN}; deterministic: {cur['determinism']['identical']}")
        sys.exit(0 if cur["determinism"]["identical"] else 1)
    if not GOLDEN.exists():
        print("no golden file; run with --update first")
        sys.exit(1)
    errs = compare(cur, json.loads(GOLDEN.read_text()))
    if errs:
        print(f"REGRESSION: {len(errs)} differences")
        for e in errs[:60]:
            print("  " + e)
        sys.exit(1)
    print("OK: deterministic and within 1 % of golden metrics for", ", ".join(SCENARIOS))


if __name__ == "__main__":
    main()
