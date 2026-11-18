"""Demo: run the PowerManager against the SYNTHETIC FakeSim job set and compare P0, P3, P4, P5.

Everything printed here is synthetic: the job set is hand-made (power/tests/fakesim.py), the
chip is the FakeSim stand-in, and every power parameter is an assumption. These numbers are
not VX-1 results and must not be quoted as findings.

    .venv/bin/python -m power.demo_fake [--duration-ms 1000] [--scale 1.0] [--seed 0] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from power.manager import PowerManager  # noqa: E402
from power.offline_opt import solve_trace  # noqa: E402
from power.params import load_policy, load_soc  # noqa: E402
from power.tests.fakesim import MS, make_fakesim, run_closed_loop, synthetic_tasks, task_info  # noqa: E402


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duration-ms", type=int, default=1000)
    ap.add_argument("--scale", type=float, default=1.0, help="multiplies every synthetic task's cycles")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ambient", type=float, default=25.0)
    ap.add_argument("--json", type=str, default=None, help="optional path for the synthetic results")
    args = ap.parse_args(argv)

    soc = load_soc()
    tasks = synthetic_tasks(args.scale)
    rows = []
    managers = {}
    for pid in ("P0", "P2", "P3", "P4"):
        pm = PowerManager(load_policy(pid), soc, args.ambient, task_info=task_info(tasks))
        sim = make_fakesim(pm, tasks, seed=args.seed)
        s = run_closed_loop(sim, pm, args.duration_ms * MS)
        managers[pid] = pm
        rows.append((pid, pm.scheduler, s["energy_j"], s["energy_per_robot_hour_wh"], s["peak_power_w"],
                     s["peak_temp_c"], s["misses_total"], s["safety_misses"], s["jobs_completed"]))
    t0 = time.time()
    bound = solve_trace(managers["P0"].job_trace, managers["P0"])
    solve_s = time.time() - t0
    rows.append(("P5", "bound", bound["energy_j"], bound["energy_per_robot_hour_wh"], float("nan"), float("nan"),
                 0, 0, len(managers["P0"].job_trace)))

    print("SYNTHETIC DEMO (FakeSim, hand-made job set, assumed power parameters; not VX-1 results)")
    print(f"duration {args.duration_ms} ms, load scale {args.scale}, seed {args.seed}, ambient {args.ambient} C")
    hdr = f"{'policy':<7}{'sched':<7}{'energy J':>10}{'Wh/robot-h':>12}{'peak W':>9}{'peak C':>8}{'misses':>8}{'safety':>8}{'jobs':>7}"
    print(hdr)
    print("-" * len(hdr))
    base = rows[0][2]
    for pid, sch, e, wh, pk, tc, m, sm, n in rows:
        pk_s = f"{pk:9.2f}" if pk == pk else f"{'-':>9}"
        tc_s = f"{tc:8.1f}" if tc == tc else f"{'-':>8}"
        print(f"{pid:<7}{sch:<7}{e:10.3f}{wh:12.3f}{pk_s}{tc_s}{m:8d}{sm:8d}{n:7d}   ({e / base:5.1%} of P0)")
    print(f"P5 = HiGHS lower bound on the P0 job trace (solve {solve_s:.1f} s); per-domain status: "
          + ", ".join(f"{d}:{v['status']}" for d, v in bound["per_domain"].items()))
    out = {"synthetic": True, "note": "FakeSim synthetic job set and assumed parameters; not VX-1 results",
           "args": vars(args), "rows": [dict(zip(("policy", "scheduler", "energy_j", "wh_per_robot_hour", "peak_w",
                                                  "peak_c", "misses", "safety_misses", "jobs"), r)) for r in rows],
           "p5": {k: v for k, v in bound.items() if k != "opp_by_slot"}}
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    main()
