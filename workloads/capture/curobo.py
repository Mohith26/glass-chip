"""T3_esdf and T5_plan: cuRoboV2-style ESDF mapping and motion planning.

Fallback (spec V3): no free GPU is reachable from this sandbox, so nothing here
is a measured GPU profile. Kernel op counts are derived analytically from
cuRoboV2's algorithms and its own default parameters, read from the cuRobo
repository (C++ runtime headers, task YAMLs, Franka robot YAML), and the totals
are sanity-checked against cuRoboV2's published timings:
- repo docs/reference/benchmarks.rst (Franka motion planning on RTX 5090 and Jetson AGX Orin),
- cuRoboV2 paper (arXiv 2603.05493) Table 4 kernel breakdown on RTX 4090 and Sec. 7.6 ESDF timings.
Method label: analytic_count for the graph, published_numbers for the sanity check ("mixed").

Run: .venv/bin/python workloads/capture/curobo.py
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from common import CPU, DMA, SIMT, SRC_CACHE, WORKLOADS, chain, cpu_instr_estimate, make_graph, op, provenance, write_graph

SCRIPT = "workloads/capture/curobo.py"
RAW = "https://raw.githubusercontent.com/NVlabs/curobo/main/"
FILES = {
    "franka": "curobo/content/configs/robot/franka.yml",
    "lbfgs_ik": "curobo/content/configs/task/ik/lbfgs_ik.yml",
    "trajopt": "curobo/content/configs/task/trajopt/lbfgs_bspline_trajopt.yml",
    "ik_h": "cpp_runtime/robot/include/curobo_robot_runtime/inverse_kinematics.h",
    "traj_h": "cpp_runtime/robot/include/curobo_robot_runtime/trajectory_optimization.h",
    "mapper_json": "cpp_runtime/mapper/configs/small_mapper.json",
    "bench": "docs/reference/benchmarks.rst",
}
DOF = 7

# ---- published numbers used only for sanity checks ----
PUB = {
    "motion_plan_mean_ms": {"rtx5090": 3.797, "jetson_agx_orin": 25.662,
                            "source": "cuRobo repo docs/reference/benchmarks.rst, Franka, with inverse dynamics"},
    "ik_franka_100_batch_ms_rtx5090": {"pose": 0.556, "collision_free": 0.563, "source": "cuRobo repo docs/reference/benchmarks.rst"},
    "paper_table4_rtx4090": {
        "trajopt_iterations": 162, "per_iter_us": 132.8, "kernel_total_ms": 30.83, "end_to_end_ms": 42,
        "share_pct": {"rnea": 28.6, "swept_collision": 11.1, "lbfgs_linesearch": 10.7, "kinematics": 13.1,
                      "costs": 6.7, "self_collision": 2.7, "bspline": 1.9, "elementwise_reduce": 16.4, "other": 6.4,
                      "seed_ik_lm": 2.4},
        "source": "Sundaralingam, Murali, Birchfield, cuRoboV2, arXiv 2603.05493v2, Table 4 (RTX 4090)"},
    "paper_esdf_rtx4090": {"tsdf_integration_ms": [0.50, 0.55], "esdf_total_ms_20mm": 1.52, "esdf_pba_ms_10mm": 1.15,
                           "source": "arXiv 2603.05493v2 Sec. 7.6 (Redwood bedroom, RTX 4090)"},
    "gpu_fp32_peak_tflops": {"rtx4090": 82.6, "rtx5090": 104.8, "jetson_agx_orin_64gb": 5.32,
                             "source": "NVIDIA public specifications"},
}

# ---- scene assumptions for this stack (labeled) ----
WORKSPACE_M = (1.6, 1.6, 1.0)      # ESDF extent around the arm and conveyor (assumption)
ESDF_VOXEL_M = 0.02                # cuRoboV2 paper uses a fixed 20 mm ESDF voxel
VISIBLE_BLOCKS_PER_CAMERA = 400    # 8^3-voxel blocks near observed surfaces per frame (assumption)
CAMERAS = 2
IMG = (640, 480)
SWEEP_SUBSAMPLES = 4               # swept-sphere sub-samples between native states (assumption)
ESDF_LOOKUP_FLOPS = 60             # trilinear distance + gradient from fp16 ESDF (8 corner reads)
TIMEOPT_NEWTON_ITERS = 20          # assumption
DENSE_SAMPLES = 1024               # benchmark interpolation buffer (benchmarks README)


def fetch(key: str) -> str:
    d = SRC_CACHE / "curobo"
    d.mkdir(parents=True, exist_ok=True)
    p = d / FILES[key].replace("/", "_")
    if not p.exists():
        p.write_bytes(urllib.request.urlopen(RAW + FILES[key], timeout=30).read())
    return p.read_text()


def header_default(text: str, name: str) -> float:
    m = re.search(rf"\b{name}\{{([-0-9.eEF]+)\}}", text)
    if not m:
        raise SystemExit(f"default for {name} not found in cuRobo header")
    return float(m.group(1).rstrip("F"))


def robot_spheres() -> tuple[int, int]:
    """Collision sphere count and the number of self-collision sphere pairs checked."""
    y = yaml.safe_load(fetch("franka"))["robot_cfg"]["kinematics"]
    sph = y["collision_spheres"]
    ignore = y.get("self_collision_ignore", {})
    links = list(sph)
    ign = {(a, b) for a, lst in ignore.items() for b in lst} | {(b, a) for a, lst in ignore.items() for b in lst}
    pairs = 0
    for i, a in enumerate(links):
        for b in links[i + 1:]:
            if (a, b) not in ign:
                pairs += len(sph[a]) * len(sph[b])
    return sum(len(v) for v in sph.values()), pairs


def rnea_flops() -> int:
    c = json.loads((WORKLOADS / "capture" / "control" / "rv64_counts.json").read_text())
    return int(c["rnea_gravity_coriolis"]["flops_est"])


def simt(oid: str, flops: float, bytes_in: float, bytes_out: float, launches: int, notes: str,
         bytes_w: float = 0, threads: int | None = None, reuse: float | None = None) -> dict[str, Any]:
    shape: dict[str, Any] = {"kernel_launches": launches}
    if threads:
        shape["threads"] = threads
    o = op(oid, "simt_kernel", SIMT, dtype="fp32", flops=flops, bytes_in=bytes_in, bytes_w=bytes_w,
           bytes_out=bytes_out, shape=shape, notes=notes)
    if reuse is not None:
        o["reuse"] = reuse
    return o


def plan_graph() -> dict:
    ikh, trh = fetch("ik_h"), fetch("traj_h")
    ik_cfg = yaml.safe_load(fetch("lbfgs_ik"))["optimizer"]
    to_cfg = yaml.safe_load(fetch("trajopt"))["optimizer"]
    n_sph, self_pairs = robot_spheres()
    knots = int(header_default(trh, "num_knots"))
    to_seeds = int(header_default(trh, "num_seeds"))
    to_iters = int(header_default(trh, "max_iterations"))
    to_refine = int(header_default(trh, "refinement_iterations"))
    lm_seeds = int(header_default(ikh, "num_seeds"))
    lm_iters = int(header_default(ikh, "lm_iterations"))
    ik_lbfgs_iters = int(ik_cfg["num_iters"])
    ik_hist = int(ik_cfg["history"])
    to_hist = int(to_cfg["history"])
    ls = len(to_cfg["line_search_scale"])
    ik_lbfgs_seeds = 8   # Franka collision-free L-BFGS seeds, cuRobo benchmarks README table
    T = (knots + 6 - 3) * 4 + 1  # native horizon: (num_knots+6) cubic controls -> spans x 4 interpolation steps + 1
    rf = rnea_flops()

    kin_fwd = 11 * 100 + n_sph * 21          # 11 links incl. hand/fingers, 3x4 transforms + sphere placement
    kin = 3 * kin_fwd                        # forward + backward (adjoint)
    coll_static = n_sph * ESDF_LOOKUP_FLOPS
    coll_swept = n_sph * SWEEP_SUBSAMPLES * ESDF_LOOKUP_FLOPS
    selfc = self_pairs * 15
    rnea = 3 * rf                            # differentiable RNEA forward + backward
    cost = 300
    bspl = 2 * DOF * 32
    per_state_traj = {"kinematics": kin, "rnea": rnea, "swept_collision": coll_swept, "self_collision": selfc,
                      "costs": cost, "bspline": bspl}
    traj_iters = to_iters + to_refine
    evals = to_seeds * traj_iters * ls * T     # state evaluations in TrajOpt
    lbfgs_traj = to_seeds * traj_iters * (4 * to_hist * knots * DOF + ls * knots * DOF * 4)
    reduce_traj = to_seeds * traj_iters * ls * T * 40
    ik_lm = lm_seeds * lm_iters * (kin_fwd + 6 * DOF * 30 + DOF * DOF * 6 * 2 + 400)
    ik_evals = ik_lbfgs_seeds * ik_lbfgs_iters * ls
    ik_state = kin + coll_static * 2 + selfc + cost
    ik_lbfgs = ik_lbfgs_seeds * ik_lbfgs_iters * (4 * ik_hist * DOF)
    timeopt = to_seeds * TIMEOPT_NEWTON_ITERS * (DENSE_SAMPLES * (rf + DOF * 12) + 32 * 16 * 4)
    validate = DENSE_SAMPLES * (kin_fwd + coll_static + selfc)

    state_bytes = DOF * 4 * 4          # q, qd, qdd, qddd fp32
    esdf_bytes = 2                     # fp16 ESDF
    # One op per kernel launch per iteration: the core charges blocks.simt.launch_overhead_us once per op
    # and runs ops in dependency order, so this keeps both the launch overhead and the serial
    # iteration-to-iteration dependency of the optimizers. Each iteration is grouped into a few fused
    # kernels (cuRoboV2 launches more, smaller kernels per iteration; see paper Table 4).
    ops = []
    ops.append(op("plan.host_setup", "cpu_kernel", CPU, dtype="fp32", flops=2e4, bytes_in=8192, bytes_out=8192,
                  instr={"scalar": 60000, "vector": 2000, "mem": 20000},
                  notes="host orchestration: goal/scene update, seed buffers, CUDA-graph-style launch (estimate)"))
    for i in range(lm_iters):
        ops.append(simt(f"plan.ik_lm.i{i}", ik_lm / lm_iters, lm_seeds * DOF * 4 + 64, lm_seeds * DOF * 4, 1,
                        f"seed IK: Levenberg-Marquardt step {i + 1}/{lm_iters}, {lm_seeds} seeds (pose only)",
                        bytes_w=4096, threads=lm_seeds))
    for i in range(ik_lbfgs_iters):
        ev = ik_lbfgs_seeds * ls
        ops.append(simt(f"plan.ik_lbfgs.i{i}.rollout", ev * ik_state, ev * (state_bytes + n_sph * 8 * esdf_bytes), ev * 16, 1,
                        f"collision-free IK L-BFGS iteration {i + 1}/{ik_lbfgs_iters}: {ik_lbfgs_seeds} seeds x {ls} line-search "
                        f"points; kinematics fwd+bwd, ESDF sphere distance ({n_sph} spheres), self-collision ({self_pairs} pairs)",
                        bytes_w=n_sph * 16 + 8192, threads=ev * n_sph))
        ops.append(simt(f"plan.ik_lbfgs.i{i}.update", ik_lbfgs / ik_lbfgs_iters, ik_lbfgs_seeds * ik_hist * DOF * 8,
                        ik_lbfgs_seeds * DOF * 4, 1, f"L-BFGS two-loop recursion, history {ik_hist}", threads=ik_lbfgs_seeds * DOF))
        ops[-1]["preempt_boundary"] = True
    ev_it = to_seeds * ls * T                  # state evaluations per TrajOpt iteration
    for i in range(traj_iters):
        pre = f"plan.trajopt.i{i}"
        ops.append(simt(f"{pre}.rollout", ev_it * (bspl + kin + rnea + cost) + to_seeds * ls * T * 40,
                        ev_it * state_bytes, ev_it * (n_sph * 16 + DOF * 4), 1,
                        f"iteration {i + 1}/{traj_iters}: B-spline eval ({knots} knots -> {T} states), kinematics fwd/bwd, "
                        f"differentiable RNEA ({rf} flops per forward pass, from the compiled RV64 RNEA), costs and reductions; "
                        f"{to_seeds} seeds x {ls} line-search points",
                        bytes_w=n_sph * 16 + DOF * 40 * 4 + 4096, threads=ev_it))
        ops.append(simt(f"{pre}.collision", ev_it * (coll_swept + selfc), ev_it * n_sph * SWEEP_SUBSAMPLES * 8 * esdf_bytes,
                        ev_it * n_sph * 16, 1,
                        f"swept-sphere ESDF collision ({n_sph} spheres x {SWEEP_SUBSAMPLES} sub-samples) + self-collision "
                        f"({self_pairs} pairs)", threads=ev_it * n_sph))
        ops.append(simt(f"{pre}.lbfgs_linesearch", lbfgs_traj / traj_iters, to_seeds * to_hist * knots * DOF * 8,
                        to_seeds * knots * DOF * 4, 1,
                        f"L-BFGS two-loop (history {to_hist}, {knots}x{DOF} variables) + approx-Wolfe selection over {ls} steps",
                        threads=to_seeds * knots * DOF))
        ops[-1]["preempt_boundary"] = True
    for i in range(TIMEOPT_NEWTON_ITERS):
        ops.append(simt(f"plan.timeopt.i{i}", timeopt / TIMEOPT_NEWTON_ITERS, to_seeds * DENSE_SAMPLES * state_bytes,
                        to_seeds * 32 * 4, 1,
                        f"TimeOpt Newton step {i + 1}/{TIMEOPT_NEWTON_ITERS} (assumed count): banded system on 32 timing "
                        f"controls, {DENSE_SAMPLES} dense samples with vel/acc/jerk/torque checks, {to_seeds} seeds",
                        threads=to_seeds * DENSE_SAMPLES))
    ops.append(simt("plan.validate", validate, DENSE_SAMPLES * (state_bytes + n_sph * 8 * esdf_bytes), 64, 1,
                    f"final dense validation of the selected trajectory ({DENSE_SAMPLES} samples)", threads=DENSE_SAMPLES * n_sph))
    ops.append(op("plan.readback", "memcopy", DMA, dtype="fp32", bytes_in=DENSE_SAMPLES * DOF * 4 * 4,
                  bytes_out=DENSE_SAMPLES * DOF * 4 * 4, notes="copy executable trajectory to host-visible buffer"))
    chain(ops)
    # The per-plan working set (seed states, sphere buffers, the 640 KB fp16 ESDF) is re-read every iteration and
    # fits in SIMT shared memory / system cache, so most bytes_in hit on chip (assumption).
    for o in ops:
        if o["type"] == "simt_kernel":
            o["reuse"] = 0.95 if ("collision" in o["op_id"] or "rollout" in o["op_id"] or "validate" in o["op_id"]) else 0.9

    total_flops = sum(o.get("flops", 0) for o in ops)
    shares = {
        "kinematics_bspline": evals * (kin + bspl) + ik_evals * kin,
        "rnea": evals * rnea,
        "collision": evals * (coll_swept + selfc) + ik_evals * (coll_static * 2 + selfc),
        "costs_reduce": evals * cost + reduce_traj + ik_evals * cost,
        "lbfgs_linesearch": lbfgs_traj + ik_lbfgs,
        "seed_ik_lm": ik_lm, "timeopt": timeopt,
    }
    gpu_flops = total_flops - ops[0]["flops"]
    sanity = {
        "total_flops": total_flops,
        "flop_share_pct": {k: round(100 * v / gpu_flops, 1) for k, v in shares.items()},
        "implied_effective_tflops": {
            "jetson_agx_orin": round(gpu_flops / (PUB["motion_plan_mean_ms"]["jetson_agx_orin"] * 1e-3) / 1e12, 3),
            "rtx5090": round(gpu_flops / (PUB["motion_plan_mean_ms"]["rtx5090"] * 1e-3) / 1e12, 3)},
        "implied_fp32_utilization_pct": {
            "jetson_agx_orin": round(100 * gpu_flops / (PUB["motion_plan_mean_ms"]["jetson_agx_orin"] * 1e-3)
                                     / (PUB["gpu_fp32_peak_tflops"]["jetson_agx_orin_64gb"] * 1e12), 2),
            "rtx5090": round(100 * gpu_flops / (PUB["motion_plan_mean_ms"]["rtx5090"] * 1e-3)
                             / (PUB["gpu_fp32_peak_tflops"]["rtx5090"] * 1e12), 2)},
        "published": PUB,
        "reading": ("Low implied utilization is expected: these kernels are latency-bound (small batches, many dependent "
                    "iterations, launch and synchronization overhead), which is why the graph has one op per kernel launch "
                    "and per iteration: the core charges launch overhead per op and keeps the iterations serial."),
    }
    params = {"cuRobo_defaults": {"ik_lm_seeds": lm_seeds, "ik_lm_iterations": lm_iters, "ik_lbfgs_seeds": ik_lbfgs_seeds,
                                  "ik_lbfgs_iterations": ik_lbfgs_iters, "ik_lbfgs_history": ik_hist,
                                  "trajopt_seeds": to_seeds, "trajopt_max_iterations": to_iters,
                                  "trajopt_refinement_iterations": to_refine, "trajopt_history": to_hist,
                                  "line_search_points": ls, "num_knots": knots, "native_horizon": T,
                                  "collision_spheres": n_sph, "self_collision_pairs": self_pairs},
              "assumptions": {"sweep_subsamples": SWEEP_SUBSAMPLES, "esdf_lookup_flops": ESDF_LOOKUP_FLOPS,
                              "timeopt_newton_iterations": TIMEOPT_NEWTON_ITERS, "dense_samples": DENSE_SAMPLES,
                              "attempts": 1, "scene": "ESDF from T3 (not OBB primitives as in the benchmark)"},
              "sanity_check": sanity}
    prov = provenance(source="cuRoboV2 (NVlabs/curobo main) motion planning: IK -> TrajOpt -> TimeOpt",
                      script=SCRIPT, method="mixed", model_name="curobo_v2_plan_pose",
                      model_version="NVlabs/curobo main as fetched (cuRoboV2 / cuRoboV2-Fast C++ runtime defaults)",
                      notes=("FALLBACK (spec V3): no GPU profile. analytic_count of kernel FLOPs/bytes from cuRobo's algorithms "
                             "and default parameters read from the repo; published_numbers (repo benchmarks, paper Table 4) used "
                             "only for the sanity check in params.sanity_check. Event-triggered (sporadic)."),
                      references=["https://github.com/NVlabs/curobo", "https://nvlabs.github.io/curobo/latest/technical_reports.html",
                                  "arXiv 2603.05493 (cuRoboV2)", "arXiv 2611.06913 (cuRoboV2-Fast)"])
    msgs = [{"topic": "/plan/trajectory", "bytes": 64 + 100 * (3 * DOF * 8 + 8), "consumer": "T7_control"}]
    return make_graph("T5_plan", "Motion planning: IK + collision + TrajOpt + TimeOpt (cuRoboV2-style)", prov, ops,
                      messages=msgs, params=params, footprint={"weights_bytes": 64 * 1024, "working_set_bytes": 4 << 20})


def esdf_graph() -> dict:
    mj = json.loads(fetch("mapper_json"))
    block = int(mj["block_size"])
    trunc = float(mj["truncation_distance"])
    W, H = IMG
    px = W * H
    nx, ny, nz = (round(e / ESDF_VOXEL_M) for e in WORKSPACE_M)
    N = nx * ny * nz
    K = VISIBLE_BLOCKS_PER_CAMERA
    vox = K * block ** 3
    ops = []
    for c in range(CAMERAS):
        pre = f"esdf.cam{c}"
        ops.append(op(f"{pre}.dma_depth", "memcopy", DMA, dtype="fp32", bytes_in=px * 2, bytes_out=px * 4,
                      notes="aligned 16UC1 depth from T1 converted to float32 meters", depends_on=[] if c == 0 else None))
        ops.append(simt(f"{pre}.block_discovery", px * 30, px * 4, px * 8, 1,
                        f"per-pixel ray over the +/-{trunc} m truncation band, emit block keys", threads=px))
        ops.append(simt(f"{pre}.sort_keys", px * 2 * 8, px * 8 * 2, px * 8, 1,
                        "radix sort of the per-pixel block keys", threads=px))
        ops.append(simt(f"{pre}.unique_keys", px * 2, px * 8, K * 8, 1, "drop duplicate block keys", threads=px))
        ops.append(simt(f"{pre}.alloc_blocks", K * 40, K * 8 * 2, K * 8, 1,
                        f"CAS insert into the block hash ({K} visible blocks assumed)", threads=K))
        ops.append(op(f"{pre}.sync_K", "cpu_kernel", CPU, dtype="int32", flops=100, bytes_in=8, bytes_out=8,
                      instr={"scalar": 3000, "vector": 0, "mem": 500},
                      notes="device-to-host read of visible block count K (the paper notes this sync)"))
        ops.append(simt(f"{pre}.integrate", vox * 45, vox * (4 + 4), vox * 4, 1,
                        f"voxel-centric TSDF update: {K} blocks x {block}^3 voxels, project, read depth, weighted fp16 update",
                        threads=vox))
    # ESDF over the dense workspace grid.
    ops.append(simt("esdf.seed_gather", N * 20, N * 4, N * 4, 1,
                    f"gather seeding from block-sparse TSDF into dense {nx}x{ny}x{nz} grid ({ESDF_VOXEL_M*1000:.0f} mm)", threads=N))
    for ph, (fl, rd, wr, what) in enumerate([(15, 4, 4, "1D banded nearest site along z"),
                                              (30, 8, 4, "proximate sites along y"),
                                              (25, 8, 6, "along x, write fp16 distance + sign recovery")], start=1):
        for half, sub in enumerate(("band", "merge")):
            ops.append(simt(f"esdf.pba_phase{ph}.{sub}", N * fl / 2, N * rd / 2 * (1 + half), N * wr / 2 * (1 + half), 1,
                            f"PBA+ phase {ph} ({sub} pass): {what}", threads=N))
    ops[-1]["preempt_boundary"] = True
    # cam1's first op depends on nothing; the ESDF waits for both integrations.
    chain(ops)
    cam1_first = f"esdf.cam1.dma_depth"
    for o in ops:
        if o["op_id"] == cam1_first:
            o["depends_on"] = []
        if o["op_id"] == "esdf.seed_gather":
            o["depends_on"] = ["esdf.cam0.integrate", "esdf.cam1.integrate"]
    integ_flops = sum(o.get("flops", 0) for o in ops if o["op_id"].startswith("esdf.cam"))
    esdf_flops = sum(o.get("flops", 0) for o in ops if o["op_id"].startswith("esdf.pba") or o["op_id"] == "esdf.seed_gather")
    sanity = {"integration_flops": integ_flops, "esdf_flops": esdf_flops, "grid_voxels": N,
              "published": PUB["paper_esdf_rtx4090"],
              "implied_rtx4090_tflops_if_paper_time": {
                  "integration": round(integ_flops / CAMERAS / (0.5e-3) / 1e12, 4),
                  "esdf": round(esdf_flops / (1.52e-3 - 0.5e-3) / 1e12, 4)},
              "reading": "Paper times are for a different scene/grid; this only checks order of magnitude (both are "
                         "memory/latency bound, far below FP32 peak)."}
    params = {"cuRobo_mapper": {"block_size": block, "truncation_distance_m": trunc, "tsdf_voxel_m": mj["voxel_size"],
                                "esdf_voxel_m": ESDF_VOXEL_M},
              "assumptions": {"workspace_m": WORKSPACE_M, "visible_blocks_per_camera": K, "cameras": CAMERAS,
                              "image": IMG},
              "sanity_check": sanity}
    prov = provenance(source="cuRoboV2 block-sparse TSDF + dense PBA+ ESDF mapper (NVlabs/curobo main)",
                      script=SCRIPT, method="mixed", model_name="curobo_v2_mapper",
                      model_version="NVlabs/curobo main as fetched; mapper algorithm per arXiv 2603.05493 Sec. 5",
                      notes=("FALLBACK (spec V3): analytic_count of the Voxel-Project integration (4 phases) and PBA+ ESDF "
                             "described in the cuRoboV2 paper, sized for this stack's workspace (assumptions in params); "
                             "published RTX 4090 timings used only as a sanity check."),
                      references=["arXiv 2603.05493 Sec. 5 and 7.6", "cpp_runtime/mapper/README.md"])
    msgs = [{"topic": "/map/esdf_ready", "bytes": 128, "consumer": "T5_plan"}]
    return make_graph("T3_esdf", "Depth to TSDF/ESDF map update (cuRoboV2-style)", prov, ops, messages=msgs, params=params,
                      footprint={"weights_bytes": 0, "working_set_bytes": N * 6 + 2 * K * block ** 3 * 4})


def main() -> None:
    for g in (esdf_graph(), plan_graph()):
        write_graph(g)
        s = g["summary"]
        print(f"{g['task_id']}: {s['num_ops']} ops, {s['flops']/1e9:.3f} GFLOP, bytes {(s['bytes_in']+s['bytes_out'])/1e6:.1f} MB")
        print("  sanity:", json.dumps({k: v for k, v in g["params"]["sanity_check"].items() if k not in ("published", "reading")}))


if __name__ == "__main__":
    main()
