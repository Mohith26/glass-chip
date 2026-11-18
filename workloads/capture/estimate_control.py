"""T6_estimate, T7_control and T12_arm2_control from the C++ kernels in
workloads/capture/control/, using the RV64 static instruction counts written by
control/count_rv64.py.

Run: .venv/bin/python workloads/capture/estimate_control.py
"""
from __future__ import annotations

import json

from common import CPU, RT, WORKLOADS, chain, dynamic_instr, make_graph, op, provenance, write_graph

SCRIPT = "workloads/capture/estimate_control.py"
COUNTS = WORKLOADS / "capture" / "control" / "rv64_counts.json"


def load_counts() -> dict:
    if not COUNTS.exists():
        raise SystemExit("run workloads/capture/control/count_rv64.py first")
    return json.loads(COUNTS.read_text())


def control_graph(task_id: str, name: str, c: dict, meta: dict, arm: str) -> dict:
    cs = c["control_step"]
    instr, total, inote = dynamic_instr(cs, "T7_control_step")
    ops = chain([
        op(f"{arm}.read_sensors", "memcopy", RT, dtype="fp32", bytes_in=7 * 8 * 2 + 32, bytes_out=7 * 8 * 2 + 32,
           instr={"scalar": 20, "vector": 0, "mem": 40},
           notes="read 7 joint positions and velocities plus status words from memory-mapped encoder registers (estimate)"),
        op(f"{arm}.control_step", "control_law", RT, dtype="fp32", instr=instr, flops=cs["flops_est"],
           bytes_in=ControlInBytes + 3 * 7 * 8, bytes_out=7 * 8 + 14 * 8, preempt_boundary=False,
           notes=(f"joint impedance + RNEA gravity/Coriolis (control.cpp), {total} RV64 instr per tick; {inote}; "
                  f"{cs['static_instructions']} static instructions in the function body")),
        op(f"{arm}.write_torque", "memcopy", RT, dtype="fp32", bytes_in=7 * 8, bytes_out=7 * 8,
           instr={"scalar": 10, "vector": 0, "mem": 14}, notes="write 7 torque commands to the motor drive registers (estimate)"),
    ])
    prov = provenance(source="Glass Chip C++ joint impedance controller for Franka Panda (control.cpp)",
                      script=SCRIPT, method="mixed" if "gem5" in inote else "analytic_count",
                      model_name="panda_joint_impedance_rnea", model_version=f"control.cpp sha256 {cs['source_sha256'][:16]}",
                      model_hash=f"sha256:{cs['source_sha256']}",
                      notes=(f"Instruction counts: compiled with {meta['compiler']}, {meta['flags']}, -march={cs['march']}; {inote}. "
                             "(RT core has no vector unit). Static count of the compiled step function; "
                             "flops_est counts FP arithmetic instructions with FMA as 2. Period/deadline are spec defaults."),
                      references=["Franka Emika Panda modified DH parameters (Franka docs)",
                                  "Gaz, Cognetti, Oliva, Robuffo Giordano, De Luca, Dynamic Identification of the Franka Emika Panda Robot, RA-L 2019"])
    msgs = [{"topic": f"/{arm}/joint_states", "bytes": 512, "consumer": "T6_estimate"}]
    return make_graph(task_id, name, prov, ops, messages=msgs, affinity=["rt"],
                      footprint={"weights_bytes": 0, "working_set_bytes": 4096})


ControlInBytes = (7 * 2 + 7 * 4 + 2 + 14 + 1) * 8


def estimate_graph(c: dict, meta: dict) -> dict:
    es = c["estimate_step"]
    instr, total, inote = dynamic_instr(es, "T6_estimate_step")
    ops = chain([
        op("est.step", "cpu_kernel", CPU, dtype="fp32", instr=instr, flops=es["flops_est"],
           bytes_in=5 * 7 * 8 + 8 * 3 * 8 + 7 * 5 * 8 + 8 * 42 * 8, bytes_out=7 * 5 * 8 + 8 * 42 * 8,
           notes=(f"joint KF (7 joints x 5 samples) + 8-track 6-state object KF with Mahalanobis gating and greedy "
                  f"association (estimate.cpp); tick with detections; {total} RV64GCV instr; {inote}")),
    ])
    prov = provenance(source="Glass Chip C++ state estimator (estimate.cpp)", script=SCRIPT,
                      method="mixed" if "gem5" in inote else "analytic_count",
                      model_name="joint_kf_plus_object_tracker",
                      model_version=f"estimate.cpp sha256 {es['source_sha256'][:16]}",
                      model_hash=f"sha256:{es['source_sha256']}",
                      notes=f"Instruction counts: compiled with {meta['compiler']}, -march={es['march']}, {meta['flags']}; {inote}.")
    msgs = [{"topic": "/state_estimate", "bytes": 7 * 16 + 8 * 48 + 64, "consumer": "T5_plan"},
            {"topic": "/state_estimate", "bytes": 7 * 16 + 8 * 48 + 64, "consumer": "T4_vla"}]
    return make_graph("T6_estimate", "State estimation: joint KF + object tracking", prov, ops, messages=msgs,
                      affinity=["cpu_a"], footprint={"weights_bytes": 0, "working_set_bytes": 16384})


def main() -> None:
    c = load_counts()
    meta = c["_meta"]
    for g in (estimate_graph(c, meta),
              control_graph("T7_control", "Joint impedance control, arm 1 (1 kHz)", c, meta, "arm1"),
              control_graph("T12_arm2_control", "Joint impedance control, arm 2 (1 kHz, S6)", c, meta, "arm2")):
        write_graph(g)
        print(f"{g['task_id']}: instr/job {sum(g['summary']['instr'].values())}")


if __name__ == "__main__":
    main()
