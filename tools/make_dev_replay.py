"""Generate SYNTHETIC development replays for the web viewer.

Nothing here comes from the Glass Chip simulator. The chip schedule is produced by a small
hand-written list scheduler with made-up op costs and a toy DRAM contention factor, and the
robot motion is a scripted pick cycle solved with MuJoCo kinematics. Every manifest written
by this script has synthetic: true, and the viewer shows a "SYNTHETIC DEV DATA" badge.

Usage: .venv/bin/python tools/make_dev_replay.py [--seconds 20] [--only S2_P0]
Output: viz/public/replays/<id>/ plus viz/public/replays/index.json
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mujoco
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "viz" / "scripts"))

from replay_writer import ReplayWriter, stage_scene, update_index  # noqa: E402
from export_panda_scene import BIN, CONVEYOR, OBJECT_KINDS, export_scene  # noqa: E402

PS_PER_MS = 1_000_000_000
PS_PER_US = 1_000_000
OUT = ROOT / "viz" / "public" / "replays"
SCENE_REL = "../../scenes/panda_conveyor"
COSIM_SCENE = ROOT / "data" / "cosim" / "scene"
COSIM_SCENE_REL = "../../scenes/cosim_conveyor"


def load_palette() -> dict[str, str]:
    """Parse viz/src/palette.ts so Python and the viewer share one palette."""
    text = (ROOT / "viz/src/palette.ts").read_text()
    return dict(re.findall(r"^\s*(T\w+):\s*'(#[0-9A-Fa-f]{6})'", text, re.M))


TASKS: list[dict[str, Any]] = [
    {"id": "T1_ingest_wrist", "name": "Wrist camera ingest", "period_us": 33333, "deadline_us": 15000, "criticality": "high"},
    {"id": "T1_ingest_overhead", "name": "Overhead camera ingest", "period_us": 33333, "deadline_us": 15000, "criticality": "high"},
    {"id": "T2_detect", "name": "Detection + pose", "period_us": 33333, "deadline_us": 33000, "criticality": "high"},
    {"id": "T3_esdf", "name": "ESDF map update", "period_us": 66667, "deadline_us": 66000, "criticality": "medium"},
    {"id": "T4_vla", "name": "VLA policy", "period_us": 125000, "deadline_us": 100000, "criticality": "high"},
    {"id": "T5_plan", "name": "Motion planning", "period_us": None, "deadline_us": 50000, "criticality": "high"},
    {"id": "T6_estimate", "name": "State estimation", "period_us": 5000, "deadline_us": 5000, "criticality": "high"},
    {"id": "T7_control", "name": "Joint control 1 kHz", "period_us": 1000, "deadline_us": 1000, "criticality": "safety"},
    {"id": "T8_safety", "name": "Safety monitor", "period_us": 10000, "deadline_us": 10000, "criticality": "safety"},
    {"id": "T10_record", "name": "Recording", "period_us": 5000, "deadline_us": None, "criticality": "low"},
]

ALPHA, VTH, VNOM = 1.3, 0.30, 0.75


def freq_scale(v: float) -> float:
    """Alpha-power-law frequency relative to nominal voltage."""
    return ((v - VTH) ** ALPHA / v) / ((VNOM - VTH) ** ALPHA / VNOM)


@dataclass
class Variant:
    id: str
    scenario: str
    scenario_label: str
    policy: str
    policy_label: str
    second_camera_at_s: float | None = None
    human_at_s: float | None = None
    record_4k_at_s: float | None = None
    qos: bool = False
    volts: dict[str, float] = field(default_factory=dict)
    default: bool = False


VARIANTS = [
    Variant("S1_P0", "S1_nominal", "Nominal pick", "P0_nominal", "P0 Nominal", default=True),
    Variant("S2_P0", "S2_second_camera", "Second camera", "P0_nominal", "P0 Nominal", second_camera_at_s=6.0),
    Variant("S2_P2Q", "S2_second_camera", "Second camera", "P2_edf_qos", "P2 EDF + QoS", second_camera_at_s=6.0, qos=True),
    Variant("S2_P4Q", "S2_second_camera", "Second camera", "P4_pace_to_deadline", "P4 Pace to deadline", second_camera_at_s=6.0,
            qos=True, volts={"npu": 0.62, "cpu_a": 0.60, "cpu_b": 0.62, "simt": 0.60, "dsp": 0.62}),
    Variant("S3_P0", "S3_human_enters", "Human enters", "P0_nominal", "P0 Nominal", human_at_s=7.0),
    Variant("S7_P0", "S7_bandwidth_hog", "4K recording", "P0_nominal", "P0 Nominal", record_4k_at_s=6.0),
]

# Node placement (mesh [x, y]) for engine units, following configs/soc/vx1.yaml.
NODE = {
    ("cpu_a", None): (0, 0), ("cpu_b", None): (1, 0), ("rt", None): (2, 0),
    ("dsp", 0): (3, 0), ("dsp", 1): (3, 1),
    ("npu", 0): (0, 1), ("npu", 1): (1, 1), ("npu", 2): (0, 2), ("npu", 3): (1, 2),
    ("simt", 0): (3, 2), ("simt", 1): (3, 2), ("simt", 2): (3, 3), ("simt", 3): (3, 3),
    ("dma", None): (1, 3), ("cache", 0): (2, 1), ("cache", 1): (2, 2),
}
DRAM_NODES = [(0, 3), (2, 3)]
UNITS = {"cpu_a": 4, "cpu_b": 4, "rt": 2, "dsp": 2, "npu": 4, "simt": 4, "dma": 8}
DOMAIN_OF = {"cpu_a": "cpu_a", "cpu_b": "cpu_b", "rt": "rt", "dsp": "dsp", "npu": "npu", "simt": "simt", "dma": "noc_cache"}


def node_of(block: str, unit: int) -> tuple[int, int]:
    return NODE.get((block, unit)) or NODE[(block, None)]


class Sched:
    """Tiny list scheduler: one FIFO per engine unit, ops run back to back per job."""

    def __init__(self, var: Variant, dur_ps: int, rng: np.random.Generator) -> None:
        self.var = var
        self.dur_ps = dur_ps
        self.rng = rng
        self.free: dict[tuple[str, int], int] = {(b, u): 0 for b, n in UNITS.items() for u in range(n)}
        self.events: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.job_id = 0
        nb = dur_ps // PS_PER_MS + 1
        self.busy = {d: np.zeros(nb) for d in set(DOMAIN_OF.values())}
        self.dram_load = np.zeros(nb)

    def fscale(self, block: str) -> float:
        v = self.var.volts.get(block)
        return freq_scale(v) if v else 1.0

    def dram_util(self, t_ps: int) -> float:
        """Toy DRAM utilization at time t from the scenario's background streams."""
        t = t_ps / 1e12
        u = 0.42
        if self.var.second_camera_at_s is not None and t >= self.var.second_camera_at_s:
            u += 0.30
        if self.var.record_4k_at_s is not None and t >= self.var.record_4k_at_s:
            u += 0.42
        if self.var.human_at_s is not None and self.var.human_at_s <= t < self.var.human_at_s + 2.5:
            u += 0.12
        u += 0.03 * math.sin(t * 7.1) + 0.02 * math.sin(t * 23.0)
        return min(u, 0.93)

    def pick_unit(self, block: str, t: int, prefer: int | None = None) -> int:
        n = UNITS[block]
        if prefer is not None:
            return prefer
        return min(range(n), key=lambda u: max(self.free[(block, u)], t))

    def run_job(self, task: str, release: int, deadline_us: float | None, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Schedule a chain of ops; returns the completion dict (CONTRACTS.md shape)."""
        jid = self.job_id
        self.job_id += 1
        t = release + int(self.rng.integers(20, 120)) * PS_PER_US // 10  # dispatch latency
        attr = dict.fromkeys(["compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware"], 0.0)
        attr["middleware"] += t - release
        start = None
        crit: list[dict[str, Any]] = []
        job_events: list[dict[str, Any]] = []
        for op in ops:
            block = op["engine"]
            unit = self.pick_unit(block, t, op.get("unit"))
            ready = t
            begin = max(ready, self.free[(block, unit)])
            wait = begin - ready
            if wait:
                attr["engine_queue" if block in ("npu", "simt", "dsp", "dma") else "runqueue"] += wait
            base = op["us"] * PS_PER_US / self.fscale(block)
            base *= float(self.rng.normal(1.0, 0.06))
            mem_frac = op.get("mem", 0.0)
            u = self.dram_util(begin)
            if self.var.qos and task in ("T4_vla", "T2_detect", "T8_safety", "T7_control"):
                u = min(u, 0.55)
            infl = 1.0 / max(0.07, 1.0 - u) - 1.0 / (1.0 - 0.42)
            infl = max(infl, 0.0)
            dram_extra = base * mem_frac * infl
            noc_extra = base * 0.02 * (1 + 2 * max(u - 0.6, 0))
            dur = int(max(base + dram_extra + noc_extra, 1000))
            attr["compute"] += base * (1 - mem_frac)
            attr["dram"] += base * mem_frac + dram_extra
            attr["noc"] += noc_extra
            if start is None:
                start = begin
            end = begin + dur
            self.free[(block, unit)] = end
            dom = DOMAIN_OF[block]
            b0, b1 = begin // PS_PER_MS, min(end // PS_PER_MS, len(self.busy[dom]) - 1)
            for b in range(b0, b1 + 1):
                lo, hi = max(begin, b * PS_PER_MS), min(end, (b + 1) * PS_PER_MS)
                if hi > lo:
                    self.busy[dom][b] += (hi - lo) / PS_PER_MS / UNITS[block]
            ev = {"t_ps": begin, "dur_ps": dur, "kind": "op", "block": block, "unit": unit, "task_id": task,
                  "job_id": jid, "op_id": op["id"], "value": op.get("layer", 0), "aux": op.get("layer", 0)}
            job_events.append(ev)
            crit.append({"op_id": op["id"], "engine": f"{block}{unit}", "start_ps": begin, "end_ps": end,
                         "wait_ps": wait, "wait_cause": ("engine_queue" if wait else ("dram" if dram_extra > base * 0.3 else "none"))})
            src = op.get("src")
            nbytes = op.get("bytes", 0)
            if nbytes and src is not None:
                dst_node = node_of(block, unit)
                src_node = DRAM_NODES[jid % 2] if src == "dram" else node_of(src[0], src[1])
                if op.get("reverse"):
                    src_node, dst_node = dst_node, src_node
                nx = max(1, min(24, round(dur / (250 * PS_PER_US))))
                for k in range(nx):
                    xs = begin + int(dur * k / nx)
                    xd = max(int(dur / nx * 0.92), 2000)
                    s_node = src_node
                    if src == "dram":
                        s_node = DRAM_NODES[(jid + k) % 2]
                    job_events.append({"t_ps": xs, "dur_ps": xd, "kind": "xfer", "block": "dma" if op.get("dma") else None,
                                       "unit": 0, "task_id": task, "job_id": jid, "op_id": op["id"],
                                       "src_node": list(s_node), "dst_node": list(dst_node), "bytes": nbytes / nx})
                    bi = min(xs // PS_PER_MS, len(self.dram_load) - 1)
                    self.dram_load[bi] += nbytes / nx
            t = end
        finish = t
        dl = int(deadline_us * PS_PER_US) if deadline_us else int(1e18)
        missed = finish > release + dl
        for e in job_events:
            e["critical"] = True
            e["missed"] = missed
        self.events.extend(job_events)
        comp = {"job_id": jid, "task_id": task, "tag": 0, "release_ps": release, "start_ps": start, "finish_ps": finish,
                "deadline_ps": release + dl if deadline_us else release + 10**15, "missed": missed,
                "response_ps": finish - release, "attribution": attr}
        if task not in ("T7_control", "T6_estimate", "T8_safety", "T10_record"):
            comp["critical_path"] = crit
        self.jobs.append(comp)
        return comp


def task_ops(task: str, rng: np.random.Generator, second_cam: bool, record_4k: bool) -> list[dict[str, Any]]:
    """Synthetic op chains (costs in microseconds at nominal frequency). Made-up numbers."""
    if task.startswith("T1_"):
        return [
            {"id": "isp.dma_in", "engine": "dma", "us": 380, "mem": 0.6, "bytes": 1.8e6, "src": ("dsp", 0), "dma": True},
            {"id": "isp.resize_norm", "engine": "dsp", "us": 2100, "mem": 0.2, "unit": 0 if task.endswith("wrist") else 1},
            {"id": "isp.depth_align", "engine": "dsp", "us": 1300, "mem": 0.25, "bytes": 0.9e6, "src": "dram",
             "unit": 0 if task.endswith("wrist") else 1},
        ]
    if task == "T2_detect":
        ops: list[dict[str, Any]] = []
        n_layers = 14
        for li in range(n_layers):
            ops.append({"id": f"det.layer{li}", "engine": "npu", "us": 420 + 30 * (li % 4) * (2 if second_cam else 1),
                        "mem": 0.35, "bytes": 0.6e6, "src": "dram", "layer": li})
        ops.append({"id": "det.nms_pose", "engine": "cpu_a", "us": 1700, "mem": 0.1, "bytes": 0.2e6, "src": ("npu", 0)})
        return ops
    if task == "T3_esdf":
        return [{"id": f"esdf.k{k}", "engine": "simt", "us": 1900, "mem": 0.4, "bytes": 1.2e6, "src": "dram", "layer": k} for k in range(6)]
    if task == "T4_vla":
        ops = []
        for li in range(10):
            ops.append({"id": f"vla.vit{li}", "engine": "npu", "us": 1450, "mem": 0.3, "bytes": 2.0e6, "src": "dram", "layer": li})
        for li in range(18):
            ops.append({"id": f"vla.lm{li}", "engine": "npu", "us": 1550, "mem": 0.72, "bytes": 6.0e6, "src": "dram", "layer": 10 + li})
        ops.append({"id": "vla.action_head", "engine": "cpu_a", "us": 1200, "mem": 0.2, "bytes": 0.1e6, "src": ("npu", 1)})
        return ops
    if task == "T5_plan":
        ops = [{"id": "plan.ik", "engine": "simt", "us": 2600, "mem": 0.2, "bytes": 0.4e6, "src": "dram"}]
        ops += [{"id": f"plan.collide{k}", "engine": "simt", "us": 3100, "mem": 0.35, "bytes": 0.8e6, "src": ("cache", 1)} for k in range(3)]
        ops += [{"id": "plan.trajopt", "engine": "simt", "us": 6200, "mem": 0.3, "bytes": 0.6e6, "src": "dram"},
                {"id": "plan.select", "engine": "cpu_b", "us": 900, "mem": 0.1}]
        return ops
    if task == "T6_estimate":
        return [{"id": "est.filter", "engine": "cpu_b", "us": 380, "mem": 0.1, "bytes": 0.03e6, "src": ("cache", 0)}]
    if task == "T7_control":
        return [{"id": "ctrl.impedance", "engine": "rt", "us": 170, "mem": 0.0, "unit": 0}]
    if task == "T8_safety":
        return [{"id": "safety.proximity", "engine": "dsp", "us": 900, "mem": 0.2, "bytes": 0.3e6, "src": "dram", "unit": 1},
                {"id": "safety.ssm", "engine": "rt", "us": 110, "mem": 0.0, "unit": 1, "bytes": 0.01e6, "src": ("dsp", 1)}]
    if task == "T10_record":
        b = 2.6e6 if record_4k else 0.35e6
        return [{"id": "rec.write", "engine": "dma", "us": 900 if record_4k else 150, "mem": 0.8, "bytes": b,
                 "src": ("cpu_b", 0), "dma": True, "reverse": False}]
    raise KeyError(task)


# ------------------------------------------------------------------ robot motion
R_DES = np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], dtype=float)
FINGER_DROP = 0.103


def smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * x * (x * (6 * x - 15) + 10)


class Arm:
    def __init__(self) -> None:
        self.m = mujoco.MjModel.from_xml_path(str(ROOT / "third_party/mujoco_menagerie/franka_emika_panda/scene.xml"))
        self.d = mujoco.MjData(self.m)
        self.d.qpos[:] = self.m.key_qpos[0]
        mujoco.mj_kinematics(self.m, self.d)
        self.hand = self.m.body("hand").id
        self.q_home = self.m.key_qpos[0].copy()
        self.jacp = np.zeros((3, self.m.nv))
        self.jacr = np.zeros((3, self.m.nv))

    def ik(self, target: np.ndarray, q0: np.ndarray, iters: int = 40) -> np.ndarray:
        m, d = self.m, self.d
        d.qpos[:] = q0
        for _ in range(iters):
            mujoco.mj_kinematics(m, d)
            mujoco.mj_comPos(m, d)
            p = d.xpos[self.hand]
            R = d.xmat[self.hand].reshape(3, 3)
            ep = target - p
            Re = R_DES @ R.T
            er = 0.5 * np.array([Re[2, 1] - Re[1, 2], Re[0, 2] - Re[2, 0], Re[1, 0] - Re[0, 1]])
            if np.linalg.norm(ep) < 1e-4 and np.linalg.norm(er) < 1e-3:
                break
            mujoco.mj_jacBody(m, d, self.jacp, self.jacr, self.hand)
            J = np.vstack([self.jacp[:, :7], self.jacr[:, :7]])
            e = np.concatenate([ep, er * 0.6])
            dq = J.T @ np.linalg.solve(J @ J.T + 1e-3 * np.eye(6), e)
            d.qpos[:7] += dq
            d.qpos[:7] = np.clip(d.qpos[:7], m.jnt_range[:7, 0], m.jnt_range[:7, 1])
        return d.qpos.copy()

    def poses(self, q: np.ndarray) -> np.ndarray:
        self.d.qpos[:] = q
        mujoco.mj_kinematics(self.m, self.d)
        return np.concatenate([self.d.xpos[1:], self.d.xquat[1:]], axis=1)

    def body_names(self) -> list[str]:
        return [self.m.body(i).name for i in range(1, self.m.nbody)]


def obj_half_height(i: int) -> float:
    k = OBJECT_KINDS[i % len(OBJECT_KINDS)]
    if k["type"] == "box":
        return float(k["size"][2])
    if k["type"] == "cylinder":
        return float(k["size"][1])
    return float(k["size"][0])


def robot_track(duration_s: float, fps: int, n_obj: int, late_ms: dict[int, float], human_at: float | None,
                speed: float = 0.2, spacing_s: float = 3.0) -> tuple[list[str], np.ndarray, list[dict[str, Any]], list[float]]:
    """Scripted pick-and-place. late_ms[i] delays the grasp of object i (chip-induced latency)."""
    arm = Arm()
    names = arm.body_names() + [f"obj_{i}" for i in range(n_obj)] + ["human"]
    nf = int(duration_s * fps)
    out = np.zeros((nf, len(names), 7), dtype=np.float32)
    c = CONVEYOR
    pick_y = 0.0
    spawn = [0.4 + i * spacing_s for i in range(n_obj)]
    t_reach = [s + (pick_y - c["y0"]) / speed for s in spawn]  # time each object passes y = pick_y
    events: list[dict[str, Any]] = []
    state: dict[int, dict[str, Any]] = {}
    q = arm.q_home.copy()
    home_pos = np.array([0.45, 0.0, 0.48])
    bin_pos = np.array([BIN["x"], BIN["y"], 0.36])
    plan_times: list[float] = []
    for i, tr in enumerate(t_reach):
        lat = late_ms.get(i, 0.0) / 1000.0
        miss = lat > 0.12
        state[i] = {"t_grasp": tr + 0.5, "miss": miss, "lat": lat}
        plan_times.append(tr - 1.2)
        events.append({"t_ps": int((tr + 0.5 + min(lat, 0.6)) * 1e12), "kind": "grasp_miss" if miss else "grasp_success",
                       "object": f"obj_{i}", "latency_ms": round(lat * 1000, 1)})
    for f in range(nf):
        t = f / fps
        # which pick cycle are we in: approach [tg-1.1, tg-0.25], descend [tg-0.25, tg], lift+carry [tg, tg+1.2], release, return
        target = home_pos.copy()
        grip = 0.04
        holding = None
        for i, st in state.items():
            tg = st["t_grasp"] + min(st["lat"], 0.6)
            oy = c["y0"] + (tg - spawn[i]) * speed
            ztop = c["top_z"] + obj_half_height(i)
            if tg - 1.1 <= t < tg - 0.25:
                a = smoothstep((t - (tg - 1.1)) / 0.85)
                obj_y = c["y0"] + (t - spawn[i]) * speed
                pre = np.array([c["x"], obj_y + 0.05 + 0.25 * speed, ztop + FINGER_DROP + 0.12])
                target = home_pos * (1 - a) + pre * a
            elif tg - 0.25 <= t < tg:
                a = smoothstep((t - (tg - 0.25)) / 0.25)
                obj_y = c["y0"] + (t - spawn[i]) * speed
                target = np.array([c["x"], obj_y, ztop + FINGER_DROP + 0.12 * (1 - a)])
            elif tg <= t < tg + 0.15:
                target = np.array([c["x"], oy + (t - tg) * speed, ztop + FINGER_DROP])
                grip = 0.04 - (0.04 - (0.022 if not st["miss"] else 0.0)) * smoothstep((t - tg) / 0.15)
            elif tg + 0.15 <= t < tg + 1.25:
                a = smoothstep((t - tg - 0.15) / 1.1)
                p0 = np.array([c["x"], oy + 0.03, ztop + FINGER_DROP])
                mid = np.array([0.35, (oy + bin_pos[1]) / 2, 0.55])
                p1 = bin_pos
                target = (1 - a) ** 2 * p0 + 2 * a * (1 - a) * mid + a**2 * p1
                grip = 0.022 if not st["miss"] else 0.0
                holding = None if st["miss"] else i
            elif tg + 1.25 <= t < tg + 1.45:
                target = bin_pos
                grip = 0.022 + 0.018 * smoothstep((t - tg - 1.25) / 0.2) if not st["miss"] else 0.0
            elif tg + 1.45 <= t < tg + 2.1:
                a = smoothstep((t - tg - 1.45) / 0.65)
                target = bin_pos * (1 - a) + home_pos * a
        q = arm.ik(target, q, iters=25)
        q[7] = q[8] = grip
        pose = arm.poses(q)
        out[f, : len(pose)] = pose
        hand_p = pose[arm.hand - 1, :3]
        for i, st in state.items():
            tg = st["t_grasp"] + min(st["lat"], 0.6)
            ztop = c["top_z"] + obj_half_height(i)
            bi = len(pose) + i
            if t < spawn[i]:
                p = np.array([c["x"], c["y0"] - 0.2, -1.0])
            elif st["miss"] or t < tg + 0.15:
                y = c["y0"] + (t - spawn[i]) * speed
                p = np.array([c["x"], y, ztop if y < c["y1"] else -1.0])
            elif t < tg + 1.35:
                p = hand_p - np.array([0, 0, FINGER_DROP])
            else:
                rest = np.array([BIN["x"] + 0.03 * ((i % 3) - 1), BIN["y"] + 0.025 * ((i % 2) * 2 - 1), 0.012 + obj_half_height(i)])
                drop = min(1.0, (t - tg - 1.35) / 0.25)
                start = bin_pos - np.array([0, 0, FINGER_DROP])
                p = start * (1 - drop**2) + rest * drop**2
            out[f, bi, :3] = p
            yaw = 0.3 * i
            out[f, bi, 3:] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
        hb = len(names) - 1
        if human_at is not None and t >= human_at - 1.5:
            a = smoothstep((t - (human_at - 1.5)) / 1.5) - smoothstep((t - (human_at + 3.0)) / 1.8)
            out[f, hb, :3] = [1.6 - 0.65 * a, 0.45, 0.0]
        else:
            out[f, hb, :3] = [3.0, 0.45, -5.0]
        out[f, hb, 3:] = [1, 0, 0, 0]
    return names, out, events, plan_times


# ------------------------------------------------------------------ assemble one variant
def build(var: Variant, seconds: float, soc: dict[str, Any], palette: dict[str, str], scene_rel: str = SCENE_REL) -> Path:
    seed = 7
    rng = np.random.default_rng(seed)
    dur_ps = int(seconds * 1e12)
    sch = Sched(var, dur_ps, rng)
    releases: list[tuple[int, str]] = []
    for t in TASKS:
        p = t["period_us"]
        if p is None:
            continue
        if t["id"] == "T1_ingest_overhead" and var.second_camera_at_s is None:
            continue
        start_s = var.second_camera_at_s if t["id"] == "T1_ingest_overhead" else 0.0
        k = 0
        while True:
            r = int(start_s * 1e12) + k * p * PS_PER_US + (int(rng.integers(0, 40)) * PS_PER_US if p > 2000 else 0)
            if r >= dur_ps:
                break
            releases.append((r, t["id"]))
            k += 1
    # second camera: detection runs twice per frame period after it is enabled
    if var.second_camera_at_s is not None:
        k = 0
        while True:
            r = int(var.second_camera_at_s * 1e12) + k * 33333 * PS_PER_US + 9 * PS_PER_MS
            if r >= dur_ps:
                break
            releases.append((r, "T2_detect"))
            k += 1
    # first pass to find T4 misses before each grasp, then robot track decides late grasps
    n_obj = int((seconds - 3.5) / 3.0) + 1
    spawn = [0.4 + i * 3.0 for i in range(n_obj)]
    t_grasp = [s + (0.0 - CONVEYOR["y0"]) / 0.2 + 0.5 for s in spawn]
    for i, tg in enumerate(t_grasp):
        releases.append((int((tg - 1.25) * 1e12), "T5_plan"))
    if var.human_at_s is not None:
        for k in range(5):
            releases.append((int((var.human_at_s + 0.04 * k) * 1e12), "T5_plan"))
    releases.sort()
    dl = {t["id"]: t["deadline_us"] for t in TASKS}
    for r, tid in releases:
        sc = var.second_camera_at_s is not None and r >= var.second_camera_at_s * 1e12
        rec4k = var.record_4k_at_s is not None and r >= var.record_4k_at_s * 1e12
        ops = task_ops(tid, rng, sc, rec4k)
        if tid == "T8_safety" and var.human_at_s is not None and var.human_at_s <= r / 1e12 < var.human_at_s + 3:
            ops[0] = dict(ops[0], us=1700)
        sch.run_job(tid, r, dl[tid], ops)

    jobs = sch.jobs
    late: dict[int, float] = {}
    t4 = sorted((j for j in jobs if j["task_id"] == "T4_vla"), key=lambda j: j["release_ps"])
    for i, tg in enumerate(t_grasp):
        window = [j for j in t4 if (tg - 0.45) * 1e12 <= j["release_ps"] < tg * 1e12]
        over = [(j["response_ps"] - (j["deadline_ps"] - j["release_ps"])) / 1e9 for j in window if j["missed"]]
        if over:
            late[i] = 150.0 + 4.0 * max(over)
    names, poses, robot_events, _ = robot_track(seconds, 60, n_obj, late, var.human_at_s)

    # power, voltage, frequency, temperature series at 1 kHz (toy model, synthetic)
    nb = dur_ps // PS_PER_MS
    pd = soc["power_domains"]
    nominal_w = {"cpu_a": 2.6, "cpu_b": 2.4, "rt": 0.35, "npu": 7.5, "simt": 4.2, "dsp": 1.4, "noc_cache": 1.6, "dram_io": 1.9}
    leak_w = {"cpu_a": 0.25, "cpu_b": 0.25, "rt": 0.04, "npu": 0.6, "simt": 0.4, "dsp": 0.12, "noc_cache": 0.2, "dram_io": 0.25}
    w = ReplayWriter(OUT / var.id, name=f"{var.scenario_label} / {var.policy_label}", soc=soc,
                     tasks=[dict(t, color=palette.get(t["id"], "#C8D2DE")) for t in TASKS], duration_ps=dur_ps,
                     scenario=var.scenario, policy=var.policy, seed=seed, synthetic=True,
                     scenario_label=var.scenario_label, policy_label=var.policy_label,
                     provenance={"script": "tools/make_dev_replay.py", "note": "synthetic dev data, not simulator output",
                                 "soc_config": "configs/soc/vx1.yaml"},
                     scene=scene_rel)
    kern = np.exp(-np.arange(60) / 12.0)
    kern /= kern.sum()
    total = np.zeros(nb)
    for dom in pd:
        if not isinstance(pd[dom], dict):
            continue
        v = var.volts.get(dom, VNOM if dom != "dram_io" else 0.5)
        if dom == "noc_cache" and var.volts:
            v = 0.70
        busy = sch.busy.get(dom, np.zeros(nb + 1))[:nb]
        if dom == "noc_cache":
            busy = np.clip(sch.dram_load[:nb] / 1.6e8, 0, 1)
        if dom == "dram_io":
            busy = np.clip(sch.dram_load[:nb] / 1.4e8, 0, 1)
        busy = np.convolve(np.clip(busy, 0, 1), kern, mode="same")
        vr = v / (VNOM if dom != "dram_io" else 0.5)
        fs = freq_scale(v) if dom != "dram_io" else 1.0
        p_dyn = nominal_w[dom] * busy * vr**2 * fs
        temp = np.zeros(nb)
        tc = 25.0
        p_leak0 = leak_w[dom] * vr
        p = np.zeros(nb)
        for i in range(nb):
            pl = p_leak0 * math.exp(0.018 * (tc - 25))
            p[i] = p_dyn[i] + pl
            tc += (25 + 4.2 * p[i] + 6.0 - tc) * (1 / 2500)
            temp[i] = tc
        total += p
        w.add_series(f"power.{dom}", t0_ps=0, rate_hz=1000, values=p, unit="W", group="power")
        w.add_series(f"volt.{dom}", t0_ps=0, rate_hz=1000, values=np.full(nb, v), unit="V", group="voltage")
        base_f = {"cpu_a": 2.0e9, "cpu_b": 2.0e9, "rt": 1.2e9, "npu": 1.2e9, "simt": 1.3e9, "dsp": 1.0e9, "noc_cache": 1.5e9, "dram_io": 4.27e9}[dom]
        w.add_series(f"freq.{dom}", t0_ps=0, rate_hz=1000, values=np.full(nb, base_f * fs), unit="Hz", group="frequency")
        w.add_series(f"temp.{dom}", t0_ps=0, rate_hz=1000, values=temp, unit="C", group="temperature")
        w.add_series(f"throttle.{dom}", t0_ps=0, rate_hz=1000, values=np.zeros(nb), unit="", group="throttle")
    for ci in range(2):
        u = np.array([sch.dram_util(i * PS_PER_MS) for i in range(nb)])
        if var.qos:
            u = u * 0.97
        q = 2 + 46 * u**3 / np.maximum(1.0 - u, 0.06) * (1.0 + 0.15 * np.sin(np.arange(nb) / 37.0 + ci))
        q += rng.normal(0, 0.6, nb)
        w.add_series(f"dram_q.{ci}", t0_ps=0, rate_hz=1000, values=np.clip(q, 0, 64), unit="req", group="dram")

    w.add_events(sch.events)
    w.add_jobs(jobs)
    w.set_robot(60, names, poses, robot_events)
    scen_events = []
    if var.second_camera_at_s is not None:
        scen_events.append({"t_s": var.second_camera_at_s, "action": "enable_task", "label": "Second camera on"})
    if var.human_at_s is not None:
        scen_events.append({"t_s": var.human_at_s, "action": "human_enter", "label": "Human enters"})
    if var.record_4k_at_s is not None:
        scen_events.append({"t_s": var.record_4k_at_s, "action": "set_bitrate", "label": "4K recording on"})
    w.set_extra("scenario_events", scen_events)
    w.set_extra("latency_tolerance", {
        "synthetic": True,
        "conveyor_speed_mps": 0.2,
        "note": "synthetic placeholder curve until the co-sim latency sweep exists",
        "latency_ms": [20, 60, 100, 140, 180, 220, 300, 400],
        "success": [0.99, 0.98, 0.96, 0.9, 0.78, 0.6, 0.32, 0.1],
    })
    path = w.write()
    update_index(OUT / "index.json", {
        "id": var.id, "path": f"{var.id}/manifest.json", "scenario": var.scenario, "scenario_label": var.scenario_label,
        "policy": var.policy, "policy_label": var.policy_label, "seed": seed, "synthetic": True, "default": var.default,
    })
    nmiss = sum(1 for j in jobs if j["missed"])
    print(f"{var.id}: {len(sch.events)} events, {len(jobs)} jobs, {nmiss} misses, late grasps {sorted(late)}")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--skip-scene", action="store_true")
    ap.add_argument("--own-scene", action="store_true", help="use the viewer-side Panda export even if the co-sim scene exists")
    a = ap.parse_args()
    soc = yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text())
    palette = load_palette()
    if not a.skip_scene:
        export_scene(ROOT / "viz/public/scenes/panda_conveyor", n_objects=int((a.seconds - 3.5) / 3.0) + 1)
    scene_rel = SCENE_REL
    if (COSIM_SCENE / "scene.json").exists() and not a.own_scene:
        stage_scene(COSIM_SCENE, ROOT / "viz/public/scenes/cosim_conveyor")
        scene_rel = COSIM_SCENE_REL
        print("using co-sim scene export from data/cosim/scene")
    for v in VARIANTS:
        if a.only and v.id != a.only:
            continue
        build(v, a.seconds, soc, palette, scene_rel)


if __name__ == "__main__":
    main()
