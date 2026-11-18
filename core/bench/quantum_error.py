"""Time-quantum synchronization error of the L2 backends.

Runs one fixture detector job (T91_det) with dram: L2 (Ramulator 2) and/or
noc: L2 (BookSim 2) at several quantum_ns values and reports response time,
DRAM/NoC attribution and CPU time. Writes core/bench/quantum.json.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "build"))
import glasschip_core as gc  # noqa: E402


def main() -> None:
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    g = json.loads((ROOT / "core/tests/fixtures/T91_det.json").read_text())
    rows = []
    for noc, dram in [("L1", "L2"), ("L2", "L1")]:
        for q in [1, 10, 100, 1000]:
            fid = dict(yaml.safe_load((ROOT / "configs/fidelity/study.yaml").read_text()), noc=noc, dram=dram, quantum_ns=q)
            sim = gc.Sim(soc, fid, 1)
            sim.add_task(g)
            sim.release_job("T91_det", 0, 0)
            c0 = time.process_time()
            d = sim.run_until(20_000_000_000)[0]
            cpu = time.process_time() - c0
            rows.append({"noc": noc, "dram": dram, "quantum_ns": q, "response_us": d["response_ps"] / 1e6,
                         "dram_us": d["attribution"]["dram"] / 1e6, "noc_us": d["attribution"]["noc"] / 1e6, "cpu_s": cpu,
                         "backends": sim.info()["backends"]})
            r = rows[-1]
            print(f"noc {noc} dram {dram} quantum {q:5d} ns: response {r['response_us']:.2f} us (dram {r['dram_us']:.2f}, noc {r['noc_us']:.2f}) cpu {cpu:.2f}s")
            del sim
    doc = {"provenance": {"script": "core/bench/quantum_error.py", "date": _dt.date.today().isoformat(),
                          "configs": ["configs/soc/vx1.yaml", "configs/fidelity/study.yaml", "core/tests/fixtures/T91_det.json"], "seed": 1},
           "results": rows}
    (ROOT / "core/bench/quantum.json").write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
