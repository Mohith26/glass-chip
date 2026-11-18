"""Regenerate calib/tables/dram_ramulator.json (spike V5, DRAM L2 reference).

For each Ramulator config (stock LPDDR5-6400 and the LPDDR5X-8533 derivation that
matches configs/soc/vx1.yaml) and each synthetic access pattern, I sweep the offered
load from 10% to 120% of theoretical peak bandwidth with Poisson arrivals of 64 B
requests and replay the trace through Ramulator 2.1 with calib/ramulator/trace_driver.
Each point reports achieved bandwidth and mean/p50/p99 latency.

Latency is measured from the request's arrival time in the trace to completion, so it
includes waiting for a controller queue slot. Above saturation, latency grows with the
trace length; treat those points as "saturated", not as steady-state latencies.

    .venv/bin/python calib/run_ramulator.py [--requests 20000] [--jobs 12]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor  # work runs in subprocesses
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "ramulator"))

import lpddr5_config  # noqa: E402
import replay  # noqa: E402

PATTERNS = ("seq_read", "random_read", "mixed_rw", "row_hit_heavy")
LOADS = (0.1, 0.25, 0.4, 0.55, 0.7, 0.8, 0.9, 1.0, 1.2)
REQ_BYTES = 64
ADDR_SPACE = 1 << 32  # 4 GiB window


def gen_trace(pattern: str, load: float, peak_gbps: float, n: int, seed: int) -> list[tuple]:
    """Synthetic trace rows (time_ns, addr, rw, bytes)."""
    rng = np.random.default_rng(seed)
    rate_per_ns = load * peak_gbps / REQ_BYTES  # requests per ns (GB/s == B/ns)
    t = np.cumsum(rng.exponential(1.0 / rate_per_ns, size=n))
    lines = ADDR_SPACE // REQ_BYTES
    if pattern == "seq_read":
        addr = (np.arange(n) * REQ_BYTES) % ADDR_SPACE
        rw = np.zeros(n, dtype=int)
    elif pattern == "random_read":
        addr = rng.integers(0, lines, size=n) * REQ_BYTES
        rw = np.zeros(n, dtype=int)
    elif pattern == "mixed_rw":
        # Random addresses, 2 reads per write (a common read:write mix for inference).
        addr = rng.integers(0, lines, size=n) * REQ_BYTES
        rw = (rng.random(n) < 1.0 / 3.0).astype(int)
    elif pattern == "row_hit_heavy":
        # Random order inside 32 hot 16 KiB regions. With 64 B channel interleave over
        # 8 channels, each region is one 2 KiB row per channel, so most accesses hit an
        # open row even though the order is random.
        hot = rng.integers(0, ADDR_SPACE // 16384, size=32) * 16384
        region = rng.integers(0, 32, size=n)
        addr = hot[region] + rng.integers(0, 16384 // REQ_BYTES, size=n) * REQ_BYTES
        rw = np.zeros(n, dtype=int)
    else:
        raise ValueError(pattern)
    return [(float(t[i]), int(addr[i]), "W" if rw[i] else "R", REQ_BYTES) for i in range(n)]


def run_point(args: tuple[str, str, float, int, int]) -> dict[str, Any]:
    variant, pattern, load, n, seed = args
    peak = lpddr5_config.peak_gbps(variant)
    rows = gen_trace(pattern, load, peak, n, seed)
    with tempfile.TemporaryDirectory() as d:
        tr = Path(d) / "trace.csv"
        with open(tr, "w") as f:
            f.write("time_ns,addr,rw,bytes\n")
            for r in rows:
                f.write(f"{r[0]:.3f},{r[1]:#x},{r[2]},{r[3]}\n")
        res, s = replay.replay(tr, variant=variant, out_csv=Path(d) / "out.csv")
    offered = load * peak
    s.update({
        "variant": variant, "pattern": pattern, "offered_load_frac": load,
        "offered_gbps": offered, "peak_gbps": peak,
        "efficiency": s["achieved_gbps"] / peak,
        "saturated": s["achieved_gbps"] < 0.97 * offered,
    })
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=20000)
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--out", default=str(HERE / "tables" / "dram_ramulator.json"))
    a = ap.parse_args()

    subprocess.run([sys.executable, str(HERE / "ramulator" / "lpddr5_config.py")], check=True,
                   capture_output=True)
    replay.ensure_built()
    variants = ("lpddr5x_8533_derived", "lpddr5_6400")
    # Same seed for every load level of a pattern, so a sweep only changes the rate.
    jobs = [(v, p, l, a.requests, 1000 + PATTERNS.index(p))
            for v in variants for p in PATTERNS for l in LOADS]
    with ThreadPoolExecutor(a.jobs) as ex:
        pts = list(ex.map(run_point, jobs))

    summary: dict[str, Any] = {}
    for v in variants:
        summary[v] = {}
        for p in PATTERNS:
            vp = [x for x in pts if x["variant"] == v and x["pattern"] == p]
            idle = min(vp, key=lambda x: x["offered_load_frac"])
            summary[v][p] = {
                "max_achieved_gbps": max(x["achieved_gbps"] for x in vp),
                "max_efficiency": max(x["efficiency"] for x in vp),
                "low_load_read_lat_mean_ns": idle.get("read_lat_mean_ns"),
                "low_load_lat_p99_ns": idle["lat_p99_ns"],
            }
    soc = lpddr5_config.load_vx1()["blocks"]["dram"]
    out = {
        "provenance": {
            "script": "calib/run_ramulator.py",
            "tool": "Ramulator 2.1 (github.com/CMU-SAFARI/ramulator2)",
            "tool_commit": subprocess.run(["git", "-C", str(ROOT / "third_party" / "ramulator2"),
                                           "rev-parse", "HEAD"], capture_output=True,
                                          text=True).stdout.strip(),
            "local_patches": ["src/ramulator/base/param.h: add `template` keyword (clang 21)",
                              "build with -DFMT_CONSTEVAL= (bundled fmt 10.2.1 consteval errors)"],
            "config_files": ["configs/soc/vx1.yaml",
                             "calib/ramulator/configs/lpddr5x_8533_derived.yaml",
                             "calib/ramulator/configs/lpddr5_6400.yaml"],
            "date": dt.date.today().isoformat(),
            "seeds": "1000 + pattern index (same across load levels)",
        },
        "setup": {
            "vx1_dram": soc,
            "model": "8 x16 LPDDR5 channels (128 bits total), FR-FCFS, open-row, RoBaRaCoCh, "
                     "64 B cache-line channel interleave, all-bank refresh, 32-entry read and "
                     "write queues per channel",
            "variants": {
                "lpddr5x_8533_derived": "LPDDR5 state machine at tCK 938 ps (8533 MT/s); core "
                                        "timings from vx1.yaml timing_ns; others scaled from "
                                        "LPDDR5_6400 at constant ns. Extrapolation, not a JEDEC "
                                        "LPDDR5X preset.",
                "lpddr5_6400": "Stock Ramulator LPDDR5_6400 preset (nearest shipped speed bin).",
            },
            "derived_8533_timing_cycles": lpddr5_config.derived_8533_overrides(soc),
            "request_bytes": REQ_BYTES,
            "dram_tx_bytes": 32,
            "requests_per_point": a.requests,
            "arrivals": "Poisson",
            "latency_definition": "trace arrival to completion (read data return; write "
                                  "command issue), includes queue-full backpressure",
        },
        "summary": summary,
        "points": pts,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
