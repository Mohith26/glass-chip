"""Lockstep co-simulation of the robot (MuJoCo, 1 ms steps) and a chip model.

Each 1 ms physics step at time t_k:

1. mj_step1 computes the state-dependent quantities at t_k.
2. Scenario events fire, the conveyor and human proxy advance.
3. Periodic jobs that are due are released to the chip at t_k (release times are rounded up
   to the physics step). Each job's inputs are snapshotted now; its output is computed now
   but stays hidden until the chip reports the job complete.
4. The chip runs to t_k. Completed jobs are applied in finish order. Chained jobs (T1 -> T2,
   T4 -> T5) are released at t_k when their upstream completes, and the chip is run to t_k
   again until nothing new completes (so a zero-latency chip resolves a whole chain within
   one step).
5. The optional power manager steps. The actuators get the last applied T7 command
   (zero-order hold) and mj_step2 integrates to t_k + 1 ms.

Stale-data semantics (spec 9.1): a late T7 leaves the previous torque on the motors; a late
T2 means T6/T4 keep using the previous detection; a late T8 delays the stop. Late outputs
are still applied when they arrive unless the task is listed in `drop_late`.

End-to-end chains are tracked by the frame tag and capture time that travel with the data:
camera_to_grasp (T1 -> T2 -> T6 -> T4 -> T5 -> T7, first actuator command that uses the new
plan) and human_to_slowdown (T1 -> T8 -> T7, first actuator command that uses the new safety
output).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import mujoco
import numpy as np

from cosim.conveyor import Conveyor, ConveyorConfig, HumanPath, HumanProxy
from cosim.envs import build_scene as layout
from cosim.fake_chip import ZeroLatencyChip
from cosim.metrics import OscillationConfig, OscillationDetector, quantiles
from cosim.scenario import CAMERA_OF, DETECT_TASK, Scenario, make_scenario, task_spec
from cosim.scene import HOME_Q, Scene, load_scene
from cosim.stack.controller import ControllerConfig, ControlOutput, JointImpedanceController
from cosim.stack.estimator import EstimatorConfig, ObjectTracker
from cosim.stack.perception import DetectionSet, Frame, Perception, PerceptionConfig
from cosim.stack.planner import GRIP_CLOSED, Goal, Planner, PlannerConfig, Trajectory
from cosim.stack.policy import ExpertConfig, PlanStatus, Policy, PolicyObservation, ScriptedExpert
from cosim.stack.safety import SafetyConfig, SafetyMonitor, SafetyOutput, human_robot_distance

PS_PER_S = 1_000_000_000_000
STEP_PS = 1_000_000_000          # 1 ms physics step


class ChipModel(Protocol):
    """What the bridge needs from a chip model (glasschip_core.Sim satisfies it)."""

    def release_job(self, task_id: str, t_ps: int, tag: int) -> int: ...

    def run_until(self, t_ps: int) -> list[dict[str, Any]]: ...


class PowerManager(Protocol):
    def step(self, sim: Any, t_ps: int) -> None: ...


@dataclass
class CoSimConfig:
    """Everything that defines a run besides the chip model."""

    scenario: Scenario = field(default_factory=make_scenario)
    conveyor: ConveyorConfig | None = None
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    estimator: EstimatorConfig = field(default_factory=EstimatorConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    expert: ExpertConfig = field(default_factory=ExpertConfig)
    oscillation: OscillationConfig = field(default_factory=OscillationConfig)
    drop_late: tuple[str, ...] = ()
    record_hz: float = 60.0
    record: bool = True
    stop_speed_threshold: float = 0.05      # rad/s: robot counts as stopped below this joint speed
    first_object_y: float | None = -0.30    # pre-load the belt so picking starts early


@dataclass
class _Pending:
    task: str
    out: Any
    t_release: float
    extra: Any = None


class CoSim:
    """One deterministic co-simulation run."""

    def __init__(self, cfg: CoSimConfig | None = None, chip: ChipModel | None = None,
                 power_manager: PowerManager | None = None, policy: Policy | None = None,
                 event_hook: Callable[[dict[str, Any], Any], None] | None = None) -> None:
        self.cfg = cfg or CoSimConfig()
        sc_cfg = self.cfg.scenario
        self.chip = chip if chip is not None else ZeroLatencyChip()
        self.power_manager = power_manager
        self.event_hook = event_hook
        ss = np.random.SeedSequence(sc_cfg.seed)
        r_conv, r_perc = (np.random.default_rng(s) for s in ss.spawn(2))
        self.sc: Scene = load_scene()
        conv_cfg = self.cfg.conveyor or ConveyorConfig()
        conv_cfg.speed_mps = sc_cfg.conveyor_speed_mps
        conv_cfg.spacing_m = sc_cfg.object_spacing_m
        for k, v in sc_cfg.cosim.get("conveyor", {}).items():
            setattr(conv_cfg, k, v)
        if conv_cfg.first_spawn_y is None:
            conv_cfg.first_spawn_y = self.cfg.first_object_y
        self.conv = Conveyor(self.sc, conv_cfg, r_conv)
        self.human = HumanProxy(self.sc)
        self.perc = Perception(self.sc, self.conv, self.cfg.perception, r_perc)
        self.tracker = ObjectTracker(self.cfg.estimator, belt_velocity=conv_cfg.speed_mps)
        self.safety = SafetyMonitor(self.cfg.safety)
        self.ctrl = JointImpedanceController(self.cfg.controller)
        self.planner = Planner(self.sc, self.cfg.planner)
        self.ctrl.q_lo, self.ctrl.q_hi = self.planner.ik.lo - 0.02, self.planner.ik.hi + 0.02
        self.policy: Policy = policy or ScriptedExpert(self.cfg.planner, self.cfg.expert)
        self.tasks = {k: v for k, v in sc_cfg.tasks.items()}
        self.events_todo = list(sc_cfg.events)
        # Runtime state
        self.pending: dict[int, _Pending] = {}
        self.next_release_ps: dict[str, int] = {t: int(round(s.enabled_at_s * PS_PER_S)) + s.offset_us * 1_000_000
                                                for t, s in self.tasks.items() if s.period_us > 0}
        self._human_start = np.array([2.2, 0.0])
        self._human_auto_leave = not any(e.action == "human_enter" and e.args.get("leave")
                                         for e in self.events_todo)
        self._t8_period_nominal = self.tasks["T8_safety"].period_us
        self.latest_frame: dict[str, Frame] = {}
        self.safety_frame: Frame | None = None
        self.det_queue: list[DetectionSet] = []
        self.latest_tracks: dict = {}
        self.latest_safety = SafetyOutput(1.0, float("inf"), "ok", None, 0)
        self.applied_cmd: ControlOutput | None = None
        self.last_goal_sig: tuple | None = None
        self.rejected: set[int] = set()
        self.traj_seq = 0
        self.map_version = 0
        self.tag_seq = 0
        # Logs
        self.robot_events: list[dict[str, Any]] = []
        self.job_log: list[tuple] = []        # (task, release_ps, start_ps, finish_ps, deadline_ps, missed)
        self.skipped: dict[str, int] = {}
        self.chains: dict[str, list[dict[str, Any]]] = {"camera_to_grasp": [], "human_to_slowdown": [],
                                                        "camera_to_detection": []}
        self._chain_seen_traj: set[int] = set()
        self._chain_seen_safety: set[int] = set()
        self.osc = OscillationDetector(self.cfg.oscillation)
        self.track_sq = 0.0
        self.track_n = 0
        self.min_sep = float("inf")
        self.stop_records: list[dict[str, Any]] = []
        self._in_zone = False
        self._zone_entry: float | None = None
        self._prev_grip = 255.0
        self.attempts: dict[int, dict[str, Any]] = {}
        self._uid_attempt: dict[int, int] = {}
        self.frames_t: list[float] = []
        self.frames_pos: list[np.ndarray] = []
        self.frames_quat: list[np.ndarray] = []
        self.frames_status: list[np.ndarray] = []
        self.frames_alpha: list[float] = []
        self._next_frame_t = 0.0
        self._Mfull = np.zeros((self.sc.model.nv, self.sc.model.nv))
        self.t = 0.0
        self.wall_s = 0.0
        # Start: hold the home posture, then move to the ready pose.
        home = self.planner.plan_home(0.0, HOME_Q, np.zeros(7), plan_id=0)
        self._install(home, 0.0)

    # ------------------------------------------------------------------ helpers
    def _event(self, t: float, kind: str, **data: Any) -> None:
        self.robot_events.append({"t_ps": int(round(t * PS_PER_S)), "t_s": round(t, 6), "kind": kind, **data})

    def _install(self, traj: Trajectory, t: float) -> None:
        self.traj_seq += 1
        traj.seq = self.traj_seq
        self.ctrl.install(traj, t)

    def _task_enabled(self, task: str, t: float) -> bool:
        s = self.tasks.get(task)
        return s is not None and s.enabled and t + 1e-9 >= s.enabled_at_s

    def _plan_status(self) -> PlanStatus | None:
        tr = self.ctrl.traj
        if tr is None:
            return None
        return PlanStatus(tr.kind, tr.plan_id, tr.target_uid, tr.t_grasp, tr.t_release, tr.t_end, tr.t_act,
                          tr.goal_release_t)

    def _gripper_width(self) -> float:
        return float(self.sc.data.qpos[self.sc.finger_qpos].sum())

    # ------------------------------------------------------------------ scenario events
    def _apply_scenario_events(self, t: float) -> None:
        while self.events_todo and self.events_todo[0].t_s <= t + 1e-9:
            e = self.events_todo.pop(0)
            a = e.args
            if e.action == "set_conveyor_speed":
                v = float(a.get("mps", a.get("speed_mps", a.get("conveyor_speed_mps", 0.2))))
                self.conv.set_speed(v)
                self.tracker.set_belt_velocity(v)
            elif e.action == "human_enter":
                self._human_event(t, a)
            elif e.action in ("enable_task", "disable_task"):
                tid = a.get("graph") or a.get("task")
                on = e.action == "enable_task"
                if tid in self.tasks:
                    self.tasks[tid].enabled = on
                    self.tasks[tid].enabled_at_s = min(self.tasks[tid].enabled_at_s, t)
                elif tid is not None:
                    self.tasks[tid] = task_spec(tid, t, a.get("period_us"))
                    self.tasks[tid].enabled = on
                if tid in self.tasks and self.tasks[tid].period_us > 0 and on and tid not in self.next_release_ps:
                    self.next_release_ps[tid] = int(round(t * PS_PER_S))
            if self.event_hook is not None:
                self.event_hook({"t_s": e.t_s, "action": e.action, "args": a}, self.chip)
            self._event(t, "scenario_event", action=e.action, args=a)

    def _human_event(self, t: float, a: dict[str, Any]) -> None:
        """human_enter: walk in (or leave with {leave: true}).

        Accepts explicit waypoints, or {from: left|right|front, closest_approach_m, speed_mps}.
        Optional safety_escalation {task: T8_safety, period_us} speeds up T8 while the person is
        present, and speed_scale_floor sets the minimum speed scale in the slow band.
        """
        speed = float(a.get("speed_mps", 1.6))
        if a.get("leave"):
            self.human.add_path(HumanPath(t, [tuple(self.human.xy), tuple(self._human_start)], speed, 0.0))
            if "T8_safety" in self.tasks:
                self.tasks["T8_safety"].period_us = self._t8_period_nominal
            self.safety.cfg.alpha_floor = 0.0
            return
        starts = {"left": (1.1, 2.2), "right": (1.1, -2.2), "front": (2.2, 0.0)}
        start = np.array(starts.get(str(a.get("from", "front")), (2.2, 0.0)), dtype=float)
        if "waypoints" in a:
            wps = [tuple(w) for w in a["waypoints"]]
        else:
            # Lean in over the far side of the conveyor; closest_approach_m sets how far in.
            ca = float(a.get("closest_approach_m", 0.0))
            stop = (float(a.get("stop_x", 0.75 + max(0.0, ca - 0.3))), float(a.get("stop_y", -0.1)))
            wps = [tuple(start), stop]
            if self._human_auto_leave:
                wps.append(tuple(start))
        self._human_start = np.array(wps[0], dtype=float)
        self.human.xy = self._human_start.copy()
        self.human.add_path(HumanPath(t, wps, speed, float(a.get("dwell_s", 2.0))))
        esc = a.get("safety_escalation")
        if isinstance(esc, dict) and esc.get("period_us") and "T8_safety" in self.tasks:
            self.tasks["T8_safety"].period_us = int(esc["period_us"])
        if a.get("speed_scale_floor") is not None:
            self.safety.cfg.alpha_floor = float(a["speed_scale_floor"])

    # ------------------------------------------------------------------ job release
    def _release(self, task: str, t_ps: int, t: float, inp: Any = None, tag: int | None = None) -> int:
        """Release one job; returns the job id or -1 if the chip skipped it."""
        if tag is None:
            self.tag_seq += 1
            tag = self.tag_seq
        jid = self.chip.release_job(task, t_ps, int(tag))
        if jid is None or jid < 0:
            self.skipped[task] = self.skipped.get(task, 0) + 1
            return -1
        d = self.sc.data
        out: Any = None
        extra: Any = None
        if task in CAMERA_OF:
            out = self.perc.capture(CAMERA_OF[task], t)
        elif task in ("T2_detect", "T2_detect_overhead"):
            out = self.perc.detect(inp)
        elif task == "T3_esdf":
            self.map_version += 1
            out = self.map_version
        elif task == "T6_estimate":
            batches, self.det_queue = self.det_queue, []
            out = self.tracker.update(t, batches)
        elif task == "T4_vla":
            obs = PolicyObservation(
                t=t, tracks=self.latest_tracks, tcp=d.site_xpos[self.sc.tcp_site].copy(),
                gripper_width=self._gripper_width(), plan=self._plan_status(), alpha=self.ctrl.alpha,
                alpha_target=self.latest_safety.alpha, lag=self.ctrl.lag,
                belt_speed_setpoint=self.conv.speed_nom, rejected_plan_ids=frozenset(self.rejected))
            out = self.policy.act(obs)
        elif task == "T5_plan":
            g: Goal = inp
            q0, v0 = self.ctrl.last_ref
            if self.ctrl.traj is None or self.ctrl.last_t is None:
                q0, v0 = d.qpos[self.sc.arm_qpos].copy(), np.zeros(7)
            if g.kind == "pick":
                out = self.planner.plan_pick(g, t, q0, v0)
            elif g.kind == "place":
                out = self.planner.plan_place(t, q0, v0, g.plan_id, g.uid)
                out.goal_release_t = g.release_t
            elif g.kind == "home":
                out = self.planner.plan_home(t, q0, v0, g.plan_id)
                out.goal_release_t = g.release_t
            else:
                out = self.planner.plan_hold(t, q0, g.plan_id, self.ctrl.grip)
            extra = g
        elif task == "T7_control":
            mujoco.mj_fullM(self.sc.model, d, self._Mfull)
            ad = self.sc.arm_dof
            M = self._Mfull[np.ix_(ad, ad)]
            out = self.ctrl.step(t, d.qpos[self.sc.arm_qpos].copy(), d.qvel[ad].copy(), M,
                                 d.qfrc_bias[ad].copy(), self.latest_safety.alpha, self.latest_safety.seq)
        elif task == "T8_safety":
            out = self.safety.evaluate(self.safety_frame)
        self.pending[jid] = _Pending(task, out, t, extra)
        return jid

    # ------------------------------------------------------------------ job completion
    def _complete(self, c: dict[str, Any], t_ps: int, t: float) -> None:
        jid = int(c["job_id"])
        p = self.pending.pop(jid, None)
        task = c["task_id"]
        missed = bool(c.get("missed", False))
        self.job_log.append((task, int(c["release_ps"]), int(c.get("start_ps", c["release_ps"])),
                             int(c["finish_ps"]), int(c.get("deadline_ps", 0)), missed))
        if p is None:
            return
        if missed and task in self.cfg.drop_late:
            return
        out = p.out
        if task in CAMERA_OF:
            fr: Frame = out
            self.latest_frame[fr.camera] = fr
            if fr.human_xy is not None or fr.camera == "overhead":
                self.safety_frame = fr
            det = DETECT_TASK.get(fr.camera)
            if det is not None and self._task_enabled(det, t):
                self._release(det, t_ps, t, inp=fr, tag=fr.tag)
        elif task in ("T2_detect", "T2_detect_overhead"):
            ds: DetectionSet = out
            self.det_queue.append(ds)
            self.chains["camera_to_detection"].append(
                {"tag": ds.tag, "origin_ps": int(round(ds.t_capture * PS_PER_S)), "applied_ps": t_ps,
                 "latency_ps": t_ps - int(round(ds.t_capture * PS_PER_S))})
        elif task == "T6_estimate":
            self.latest_tracks = out
        elif task == "T4_vla":
            g: Goal | None = out
            if g is not None:
                sig = g.signature()
                if sig != self.last_goal_sig:
                    # Only remember the goal if the planner job was accepted (T5 may skip on overrun).
                    if self._release("T5_plan", t_ps, t, inp=g, tag=(g.origin[0] if g.origin else None)) >= 0:
                        self.last_goal_sig = sig
        elif task == "T5_plan":
            g = p.extra
            if out is None:
                self.rejected.add(g.plan_id)
                self._event(t, "plan_rejected", plan_id=g.plan_id, uid=g.uid)
            else:
                if np.isnan(out.goal_release_t):
                    out.goal_release_t = g.release_t
                prev = self.ctrl.traj
                self._install(out, t)
                if prev is None or prev.plan_id != out.plan_id:
                    self._event(t, "plan_start", plan_id=out.plan_id, plan_kind=out.kind, uid=out.target_uid,
                                t_grasp=None if np.isnan(out.t_grasp) else round(out.t_grasp, 4))
        elif task == "T7_control":
            cmd: ControlOutput = out
            self.applied_cmd = cmd
            if cmd.traj_seq not in self._chain_seen_traj:
                self._chain_seen_traj.add(cmd.traj_seq)
                tr = self.ctrl.traj
                if tr is not None and tr.seq == cmd.traj_seq and tr.origin is not None:
                    o_ps = int(tr.origin[1])
                    self.chains["camera_to_grasp"].append(
                        {"tag": int(tr.origin[0]), "origin_ps": o_ps, "applied_ps": t_ps, "latency_ps": t_ps - o_ps,
                         "plan_id": tr.plan_id})
            if cmd.safety_id not in self._chain_seen_safety:
                self._chain_seen_safety.add(cmd.safety_id)
                so = self.latest_safety
                if so.seq == cmd.safety_id and so.origin is not None:
                    self.chains["human_to_slowdown"].append(
                        {"tag": int(so.origin[0]), "origin_ps": int(so.origin[1]), "applied_ps": t_ps,
                         "latency_ps": t_ps - int(so.origin[1]), "state": so.state})
        elif task == "T8_safety":
            so: SafetyOutput = out
            if so.state != self.latest_safety.state:
                self._event(t, "safety_state", state=so.state,
                            separation=None if not np.isfinite(so.separation) else round(so.separation, 4))
            self.latest_safety = so

    # ------------------------------------------------------------------ ground-truth bookkeeping
    def _ground_truth(self, t: float) -> None:
        d = self.sc.data
        sc = self.sc
        cmd = self.applied_cmd
        if cmd is not None:
            q = d.qpos[sc.arm_qpos]
            e = cmd.q_ref - q
            self.track_sq += float(e @ e)
            self.track_n += 7
            self.osc.update(t, cmd.v_ref - d.qvel[sc.arm_dof])
            # Gripper close command on a pick plan = grasp attempt.
            if cmd.grip <= GRIP_CLOSED + 1 and self._prev_grip > 128:
                tr = self.ctrl.traj
                if tr is not None and tr.kind == "pick" and tr.plan_id not in self.attempts:
                    rec = {"t_close": t, "uid": tr.target_uid, "outcome": "pending"}
                    slot = int(np.flatnonzero(self.conv.uid == tr.target_uid)[0]) if (self.conv.uid == tr.target_uid).any() else -1
                    if slot >= 0:
                        # alignment error between the fingertip centre and the object at the close command
                        dxy = d.site_xpos[sc.tcp_site][:2] - d.xpos[sc.objects[slot].body_id][:2]
                        rec["align_err_xy"] = [float(dxy[0]), float(dxy[1])]
                    self.attempts[tr.plan_id] = rec
                    self._uid_attempt[tr.target_uid] = tr.plan_id
                    self._event(t, "grasp_start", plan_id=tr.plan_id, uid=tr.target_uid)
            self._prev_grip = cmd.grip
        # Human separation and safety stop latency.
        hxy = d.mocap_pos[sc.human_mocap][:2]
        if hxy[0] < 2.0:
            pts = np.vstack([d.xpos[sc.link_bodies], d.site_xpos[sc.tcp_site][None]])
            s = human_robot_distance(hxy, pts)
            self.min_sep = min(self.min_sep, s)
            zone = s <= self.cfg.safety.stop_m
            speed = float(np.max(np.abs(d.qvel[sc.arm_dof])))
            if zone and not self._in_zone:
                self._in_zone = True
                self._zone_entry = t
                self._zone_entry_speed = speed
                self._event(t, "protective_zone_entry", separation=round(s, 4), joint_speed=round(speed, 4))
            if self._in_zone and self._zone_entry is not None and speed < self.cfg.stop_speed_threshold:
                lat = t - self._zone_entry
                self.stop_records.append({"t_entry": self._zone_entry, "latency_s": lat,
                                          "speed_at_entry": self._zone_entry_speed,
                                          "already_stopped": self._zone_entry_speed < self.cfg.stop_speed_threshold})
                self._event(t, "safety_stop", latency_s=round(lat, 4))
                self._zone_entry = None
            if not zone and self._in_zone and s > self.cfg.safety.stop_m + 0.05:
                self._in_zone = False
                self._zone_entry = None

    def _object_events(self) -> None:
        for e in self.conv.events:
            uid = e.data.get("uid", -1)
            data = {k: v for k, v in e.data.items()}
            self._event(e.t, e.kind, obj=e.obj, **data)
            pid = self._uid_attempt.get(uid)
            if pid is not None:
                rec = self.attempts[pid]
                if e.kind == "lifted" and rec["outcome"] == "pending":
                    rec["outcome"] = "lifted"
                    rec["t_lift"] = e.t
                    self._event(e.t, "grasp_success", plan_id=pid, uid=uid)
                elif e.kind == "placed":
                    rec["placed"] = True
                    # error relative to the drop point this episode aimed for
                    bx, by = self.planner.bin_points[pid % len(self.planner.bin_points)]
                    p = self.sc.data.xpos[self.sc.objects[e.obj].body_id]
                    rec["placement_error_m"] = float(np.hypot(p[0] - bx, p[1] - by))
                elif e.kind in ("dropped", "dropped_on_belt") and rec["outcome"] == "lifted":
                    rec["dropped"] = True
        self.conv.events.clear()
        for pid, rec in self.attempts.items():
            if rec["outcome"] == "pending" and self.t - rec["t_close"] > 1.0:
                rec["outcome"] = "miss"
                self._event(self.t, "grasp_miss", plan_id=pid, uid=rec["uid"])

    def _record(self, t: float) -> None:
        d = self.sc.data
        self.frames_t.append(t)
        self.frames_pos.append(d.xpos.astype(np.float32))
        self.frames_quat.append(d.xquat.astype(np.float32))
        self.frames_status.append(self.conv.status.copy())
        self.frames_alpha.append(self.ctrl.alpha)

    def _drain(self, t_ps: int, t: float) -> None:
        """Run the chip to t_ps and apply completions until nothing new completes."""
        for _ in range(32):
            comps = self.chip.run_until(t_ps)
            if not comps:
                break
            comps.sort(key=lambda c: (c["finish_ps"], c["job_id"]))
            for c in comps:
                self._complete(c, t_ps, t)

    # ------------------------------------------------------------------ main loop
    def run(self, duration_s: float | None = None) -> dict[str, Any]:
        """Run the co-sim and return the metrics dict."""
        dur = float(duration_s if duration_s is not None else self.cfg.scenario.duration_s)
        m, d = self.sc.model, self.sc.data
        n_steps = int(round(dur * 1000))
        rec_dt = 1.0 / self.cfg.record_hz
        w0 = time.perf_counter()
        for k in range(n_steps):
            t_ps = k * STEP_PS
            t = k * 1e-3
            self.t = t
            mujoco.mj_step1(m, d)
            self._apply_scenario_events(t)
            self.conv.step(t, 1e-3)
            self.human.step(t)
            # Advance the chip to now first, so a job that finished during the last step is not
            # still "running" when its successor is released (matters for skip-on-overrun tasks).
            self._drain(t_ps, t)
            for task in list(self.next_release_ps):
                nr = self.next_release_ps[task]
                if t_ps >= nr:
                    if self._task_enabled(task, t):
                        self._release(task, t_ps, t)
                    per = self.tasks[task].period_us * 1_000_000
                    while nr <= t_ps:
                        nr += per
                    self.next_release_ps[task] = nr
            self._drain(t_ps, t)
            if self.power_manager is not None:
                self.power_manager.step(self.chip, t_ps)
            if self.applied_cmd is not None:
                d.ctrl[:7] = self.applied_cmd.tau
                d.ctrl[7] = self.applied_cmd.grip
            self._ground_truth(t)
            if self.conv.events:
                self._object_events()
            elif k % 50 == 0:
                self._object_events()
            if self.cfg.record and t + 1e-9 >= self._next_frame_t:
                self._record(t)
                self._next_frame_t += rec_dt
            mujoco.mj_step2(m, d)
        self.t = n_steps * 1e-3
        self._object_events()
        self.osc.finish(self.t)
        self.wall_s = time.perf_counter() - w0
        return self.metrics()

    # ------------------------------------------------------------------ outputs
    def metrics(self) -> dict[str, Any]:
        dur = self.t
        att = list(self.attempts.values())
        n_att = len(att)
        n_lift = sum(1 for a in att if a["outcome"] == "lifted")
        n_placed = sum(1 for a in att if a.get("placed"))
        place_err = [a["placement_error_m"] for a in att if a.get("placement_error_m") is not None]
        exited = sum(1 for e in self.robot_events if e["kind"] == "exited")
        offered = self.conv.offered
        per_task: dict[str, Any] = {}
        tasks = sorted({j[0] for j in self.job_log})
        for task in tasks:
            rows = [j for j in self.job_log if j[0] == task]
            resp = [(j[3] - j[1]) / 1e9 for j in rows]
            per_task[task] = {"completed": len(rows), "missed": int(sum(j[5] for j in rows)),
                              "skipped": int(self.skipped.get(task, 0)), "response_ms": quantiles(resp)}
        chains = {k: quantiles([c["latency_ps"] / 1e9 for c in v]) for k, v in self.chains.items()}
        stops = [s for s in self.stop_records if not s["already_stopped"]]
        return {
            "duration_s": dur,
            "wall_s": self.wall_s,
            "wall_per_sim_s": self.wall_s / max(dur, 1e-9),
            "objects_offered": int(offered),
            "grasp_attempts": n_att,
            "grasp_success": n_lift,
            "placed": n_placed,
            "grasp_success_rate": (n_lift / n_att) if n_att else None,
            "place_success_rate": (n_placed / n_att) if n_att else None,
            "objects_missed": int(exited),
            "objects_missed_per_min": exited / dur * 60 if dur > 0 else None,
            "picks_per_min": n_placed / dur * 60 if dur > 0 else None,
            "mean_placement_error_m": float(np.mean(place_err)) if place_err else None,
            "grasp_align_err_m": quantiles([float(np.hypot(*a["align_err_xy"])) for a in att if "align_err_xy" in a]),
            "joint_tracking_rms_rad": float(np.sqrt(self.track_sq / max(self.track_n, 1))),
            "oscillation_episodes": len(self.osc.episodes),
            "oscillation_severity_peak": max((e["peak_vel_err"] for e in self.osc.episodes), default=0.0),
            "oscillation_time_s": float(sum(e["duration_s"] for e in self.osc.episodes)),
            "safety_stop_latency_ms": quantiles([s["latency_s"] * 1e3 for s in stops]),
            "safety_zone_entries": len(self.stop_records),
            "min_human_separation_m": None if not np.isfinite(self.min_sep) else self.min_sep,
            "chain_latency_ms": chains,
            "tasks": per_task,
        }

    def oscillation_events(self) -> list[dict[str, Any]]:
        return [{"t_ps": int(round(e["t_start"] * PS_PER_S)), "t_s": round(e["t_start"], 4), "kind": "oscillation",
                 "duration_s": round(e["duration_s"], 4), "peak_vel_err": round(e["peak_vel_err"], 4)}
                for e in self.osc.episodes]

    def all_robot_events(self) -> list[dict[str, Any]]:
        ev = self.robot_events + self.oscillation_events()
        return sorted(ev, key=lambda e: e["t_ps"])

    def save_recording(self, path: str | Path, extra_meta: dict[str, Any] | None = None) -> tuple[Path, Path]:
        """Write <path>.npz (poses, statuses, jobs) and <path>.json (names, events, metrics)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        npz = path.with_suffix(".npz")
        js = path.with_suffix(".json")
        jobs = np.array([(j[1], j[2], j[3], j[4], j[5]) for j in self.job_log], dtype=np.int64).reshape(-1, 5)
        task_names = sorted({j[0] for j in self.job_log})
        tidx = {n: i for i, n in enumerate(task_names)}
        job_task = np.array([tidx[j[0]] for j in self.job_log], dtype=np.int16)
        np.savez_compressed(
            npz,
            t=np.array(self.frames_t, dtype=np.float64),
            xpos=np.array(self.frames_pos, dtype=np.float32),
            xquat=np.array(self.frames_quat, dtype=np.float32),
            obj_status=np.array(self.frames_status, dtype=np.int8),
            alpha=np.array(self.frames_alpha, dtype=np.float32),
            job_task=job_task, jobs=jobs,
        )
        meta = {
            "format": "glasschip-cosim-recording", "version": 1,
            "fps": self.cfg.record_hz, "n_frames": len(self.frames_t),
            "body_names": self.sc.body_names(),
            "object_bodies": [f"obj_{i}" for i in range(layout.N_OBJECTS)],
            "object_status_codes": {"0": "parked", "1": "on_belt", "2": "lifted", "3": "in_bin", "4": "dropped"},
            "job_tasks": task_names,
            "job_columns": ["release_ps", "start_ps", "finish_ps", "deadline_ps", "missed"],
            "scenario": self.cfg.scenario.name, "seed": self.cfg.scenario.seed,
            "chip": getattr(self.chip, "name", type(self.chip).__name__),
            "events": self.all_robot_events(),
            "chains": self.chains,
            "metrics": self.metrics(),
            "notes": "Body poses are world-frame xpos (m) and xquat (w, x, y, z) per frame. "
                     "Behavior comes from a scripted expert in hybrid mode.",
        }
        if extra_meta:
            meta.update(extra_meta)
        js.write_text(json.dumps(meta, indent=1, default=_json_default))
        return npz, js

    def replay_robot_payload(self) -> dict[str, Any]:
        """Arguments for tools/replay_writer.ReplayWriter.set_robot (poses [F, B, 7])."""
        pos = np.array(self.frames_pos, dtype=np.float32)
        quat = np.array(self.frames_quat, dtype=np.float32)
        return {"fps": self.cfg.record_hz, "body_names": self.sc.body_names(),
                "poses": np.concatenate([pos, quat], axis=2), "events": self.all_robot_events()}

    def completions(self) -> list[dict[str, Any]]:
        """Chip job records in the completion-dict shape (for ReplayWriter.add_jobs)."""
        return [{"job_id": i, "task_id": j[0], "release_ps": j[1], "start_ps": j[2], "finish_ps": j[3],
                 "deadline_ps": j[4], "missed": j[5]} for i, j in enumerate(self.job_log)]


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, float) and not np.isfinite(o):
        return None
    raise TypeError(type(o))


def run_scenario(scenario: Scenario, chip: ChipModel | None = None, record_path: str | Path | None = None,
                 **cfg_kw: Any) -> dict[str, Any]:
    """Convenience wrapper: run one scenario and optionally save the recording."""
    cfg = CoSimConfig(scenario=scenario, record=record_path is not None, **cfg_kw)
    sim = CoSim(cfg, chip=chip)
    m = sim.run()
    if record_path is not None:
        sim.save_recording(record_path)
    return m
