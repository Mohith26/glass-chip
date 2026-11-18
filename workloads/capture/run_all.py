"""Regenerate every workload artifact in dependency order, then validate and summarize.

Run from the repo root: .venv/bin/python workloads/capture/run_all.py
Downloads (cached under data/workloads/): yolov8n.pt (~6 MB), LR-ASPP weights (~13 MB),
small Hugging Face config files, the SmolVLA safetensors header (range request only).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = sys.executable

STEPS = [
    ["make", "-C", str(HERE / "control"), "test"],      # native correctness test of the C++ kernels
    [PY, str(HERE / "control" / "count_rv64.py")],     # RV64 static instruction counts
    [PY, str(HERE / "estimate_control.py")],           # T6, T7, T12
    [PY, str(HERE / "detect.py")],                     # T2 (+ T2_detect_overhead)
    [PY, str(HERE / "safety.py")],                     # T8
    [PY, str(HERE / "vla.py")],                        # T4
    [PY, str(HERE / "curobo.py")],                     # T3, T5
    [PY, str(HERE / "ingest.py")],                     # T1 x2
    [PY, str(HERE / "record.py")],                     # T10 (+ 4K variant)
    [PY, str(HERE / "extras.py")],                     # T11, T13, T14, T15
    [PY, str(HERE / "middleware.py")],                 # T9 middleware.json
    [PY, str(HERE / "scenarios.py")],                  # S1..S8
    [PY, str(ROOT / "workloads" / "validate.py")],
    [PY, str(ROOT / "workloads" / "summary.py")],
]


def main() -> int:
    for cmd in STEPS:
        t0 = time.time()
        name = Path(cmd[-1]).name if cmd[0] != "make" else "control/make test"
        print(f"==> {name}", flush=True)
        r = subprocess.run(cmd, cwd=str(HERE))
        if r.returncode != 0:
            print(f"FAILED: {' '.join(cmd)}", flush=True)
            return r.returncode
        print(f"    ({time.time() - t0:.1f} s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
