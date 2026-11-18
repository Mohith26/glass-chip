"""Replay a NoC packet trace through BookSim 2 (patched trace mode) for cross-fidelity.

Stock BookSim 2 has no trace input. I added a small trace mode to
third_party/booksim2/src/trafficmanager.cpp (config keys `trace_file`, `trace_out`):
each trace line is `cycle,src,dst,size_flits`; packets are created at `cycle`
(so source queueing counts toward latency) and every delivered packet is written to
`trace_out` with create/inject/arrive cycles, hops, packet latency (plat) and network
latency (nlat). Synthetic traffic generation is disabled in trace mode.

Input CSV for this harness (header required), as exported from the Glass Chip core:
    time_ns,src_x,src_y,dst_x,dst_y,bytes
Time is converted to NoC cycles at noc.freq_hz; bytes become ceil(bytes/flit_bytes)
flits (a transfer larger than max_packet_bytes is split into several packets that are
all created at the same cycle; its latency is the last packet's arrival).

    python calib/booksim/replay.py trace.csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

import booksim_cfg as bc


def run_raw_trace(records: list[tuple[int, int, int, int]], workdir: Path,
                  extra: dict[str, Any] | None = None) -> list[dict[str, int]]:
    """Run (cycle, src, dst, flits) records sorted by cycle; return per-packet rows."""
    tr = workdir / "noc.trace"
    out = workdir / "noc_out.csv"
    with open(tr, "w") as f:
        for c, s, d, n in records:
            f.write(f"{c},{s},{d},{n}\n")
    cfg = bc.base_config()
    cfg.update({"trace_file": str(tr), "trace_out": str(out)})
    if extra:
        cfg.update(extra)
    cfg_path = bc.write_cfg(workdir / "trace.cfg", cfg)
    res = subprocess.run([str(bc.BOOKSIM), str(cfg_path)], capture_output=True, text=True)
    # BookSim main() returns -1 (255) on success, so check the output instead of the code.
    if "Trace finished" not in res.stdout:
        raise RuntimeError("booksim trace run failed:\n" + res.stdout[-2000:] + res.stderr[-2000:])
    with open(out) as f:
        rows = [{k: int(v) for k, v in r.items()} for r in csv.DictReader(f)]
    return sorted(rows, key=lambda r: r["id"])


def replay(trace_csv: str | Path, out_csv: str | Path | None = None) -> tuple[list[dict], dict]:
    """Replay a core-exported trace; returns (per-transfer rows, summary)."""
    noc = bc.load_noc()
    k = noc["dims"][0]
    fb = bc.flit_bytes(noc)
    max_flits = max(1, noc.get("max_packet_bytes", 256) // fb)
    cyc_ns = 1e9 / noc["freq_hz"]
    with open(trace_csv) as f:
        xfers = list(csv.DictReader(f))
    recs: list[tuple[int, int, int, int]] = []
    owner: list[int] = []
    for i, x in enumerate(xfers):
        c = int(math.floor(float(x["time_ns"]) / cyc_ns))
        s = bc.node_id(int(x["src_x"]), int(x["src_y"]), k)
        d = bc.node_id(int(x["dst_x"]), int(x["dst_y"]), k)
        fl = max(1, math.ceil(int(x["bytes"]) / fb))
        while fl > 0:
            n = min(fl, max_flits)
            recs.append((c, s, d, n))
            owner.append(i)
            fl -= n
    order = sorted(range(len(recs)), key=lambda j: recs[j][0])
    with tempfile.TemporaryDirectory() as td:
        rows = run_raw_trace([recs[j] for j in order], Path(td))
    # trace ids are positions in the sorted record list
    done = [-1] * len(xfers)
    for r in rows:
        i = owner[order[r["id"]]]
        done[i] = max(done[i], r["arrive_cycle"])
    out_rows = []
    for i, x in enumerate(xfers):
        c0 = math.floor(float(x["time_ns"]) / cyc_ns)
        out_rows.append({**x, "done_ns": done[i] * cyc_ns,
                         "latency_ns": (done[i] - c0) * cyc_ns})
    lat = np.array([r["latency_ns"] for r in out_rows])
    summary = {"transfers": len(out_rows), "packets": len(rows),
               "lat_mean_ns": float(lat.mean()), "lat_p50_ns": float(np.percentile(lat, 50)),
               "lat_p99_ns": float(np.percentile(lat, 99)), "cycle_ns": cyc_ns}
    if out_csv:
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
    return out_rows, summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BookSim 2 trace replay")
    ap.add_argument("trace")
    ap.add_argument("out")
    a = ap.parse_args()
    _, s = replay(a.trace, a.out)
    print(json.dumps(s, indent=2))
