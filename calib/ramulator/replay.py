"""Replay a memory request trace through Ramulator 2.1 and get per-request latency.

This is the cross-fidelity hook for DRAM: export a trace from the Glass Chip core
(CSV with columns time_ns, addr, rw, bytes; rw is R or W), run it here, and compare
the per-request latencies with what the L1 DRAM model predicted for the same trace.

    python calib/ramulator/replay.py trace.csv out.csv --variant lpddr5x_8533_derived

Python use:

    from calib.ramulator.replay import replay
    rows, summary = replay("trace.csv", variant="lpddr5x_8533_derived")
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DRIVER = HERE / "trace_driver"
CONFIG_DIR = HERE / "configs"


def ensure_built() -> None:
    """Build the C++ driver and YAML configs if they are missing."""
    if not DRIVER.exists():
        subprocess.run(["make", "-C", str(HERE)], check=True)
    for v in ("lpddr5_6400", "lpddr5x_8533_derived"):
        if not (CONFIG_DIR / f"{v}.yaml").exists():
            from lpddr5_config import write_yaml  # type: ignore

            write_yaml(v, CONFIG_DIR / f"{v}.yaml")


def config_path(variant: str) -> Path:
    return CONFIG_DIR / f"{variant}.yaml"


def run_driver(trace_csv: Path, out_csv: Path, config_yaml: Path) -> None:
    """Run the C++ driver; raises if Ramulator did not finish every request."""
    res = subprocess.run([str(DRIVER), str(config_yaml), str(trace_csv), str(out_csv)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"trace_driver failed: {res.stderr.strip()}")


def load_result(out_csv: Path) -> dict[str, np.ndarray]:
    with open(out_csv) as f:
        r = list(csv.DictReader(f))
    return {
        "time_ns": np.array([float(x["time_ns"]) for x in r]),
        "rw": np.array([x["rw"] for x in r]),
        "bytes": np.array([int(x["bytes"]) for x in r]),
        "done_ns": np.array([float(x["done_ns"]) for x in r]),
        "latency_ns": np.array([float(x["latency_ns"]) for x in r]),
    }


def summarize(res: dict[str, np.ndarray]) -> dict[str, Any]:
    """Bandwidth and latency summary for a replayed trace."""
    lat = res["latency_ns"]
    rd = res["rw"] == "R"
    t0 = float(res["time_ns"].min())
    t1 = float(res["done_ns"].max())
    total = int(res["bytes"].sum())
    out: dict[str, Any] = {
        "requests": int(lat.size),
        "bytes": total,
        "span_ns": t1 - t0,
        "achieved_gbps": total / (t1 - t0) if t1 > t0 else 0.0,
        "lat_mean_ns": float(lat.mean()),
        "lat_p50_ns": float(np.percentile(lat, 50)),
        "lat_p99_ns": float(np.percentile(lat, 99)),
    }
    if rd.any():
        out["read_lat_mean_ns"] = float(lat[rd].mean())
        out["read_lat_p99_ns"] = float(np.percentile(lat[rd], 99))
    if (~rd).any():
        out["write_lat_mean_ns"] = float(lat[~rd].mean())
        out["write_lat_p99_ns"] = float(np.percentile(lat[~rd], 99))
    return out


def replay(trace_csv: str | Path, variant: str = "lpddr5x_8533_derived",
           out_csv: str | Path | None = None) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Replay ``trace_csv`` and return (per-request arrays, summary)."""
    ensure_built()
    trace_csv = Path(trace_csv)
    if out_csv is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        out_csv = Path(tmp.name)
        tmp.close()
    out_csv = Path(out_csv)
    run_driver(trace_csv, out_csv, config_path(variant))
    res = load_result(out_csv)
    return res, summarize(res)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace")
    ap.add_argument("out")
    ap.add_argument("--variant", default="lpddr5x_8533_derived",
                    choices=["lpddr5_6400", "lpddr5x_8533_derived"])
    a = ap.parse_args()
    _, s = replay(a.trace, a.variant, a.out)
    print(json.dumps(s, indent=2))


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(HERE))
    main()
