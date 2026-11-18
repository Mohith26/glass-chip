"""T5 motion planner: damped-least-squares IK for the Panda plus time-indexed joint trajectories.

A plan is a uniformly sampled joint trajectory in absolute virtual time. For a pick, the
Cartesian path follows the object's predicted position on the belt (so the hand descends
while moving with the object), closes the gripper, lifts, carries to the bin, releases and
returns to the ready pose. Interpolation between samples is cubic Hermite with
finite-difference velocities, which gives continuous position and velocity references to the
1 kHz controller.

When a new plan becomes active while another is executing, the controller blends from the
old reference to the new one with a quintic offset decay (see Trajectory.activate), so a
replan never produces a step in the reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import mujoco
import numpy as np

from cosim.envs import build_scene as layout
from cosim.scene import HOME_Q, Scene

GRIP_OPEN = 255.0
GRIP_CLOSED = 0.0


def _smooth(u: np.ndarray | float) -> np.ndarray | float:
    """Quintic smoothstep 0 to 1 with zero velocity and acceleration at both ends."""
    u = np.clip(u, 0.0, 1.0)
    return u * u * u * (10 - 15 * u + 6 * u * u)


def _quintic_hermite(p0, v0, p1, v1, T, t):
    """Quintic with given end positions/velocities and zero end accelerations, evaluated at t (array)."""
    s = np.clip(t / T, 0.0, 1.0)[:, None]
    p0, v0, p1, v1 = (np.asarray(x, dtype=float)[None, :] for x in (p0, v0, p1, v1))
    h0 = 1 - 10 * s**3 + 15 * s**4 - 6 * s**5
    h1 = s - 6 * s**3 + 8 * s**4 - 3 * s**5
    h3 = -4 * s**3 + 7 * s**4 - 3 * s**5
    h2 = 10 * s**3 - 15 * s**4 + 6 * s**5
    return h0 * p0 + h1 * T * v0 + h2 * p1 + h3 * T * v1


@dataclass
class Trajectory:
    """Uniformly sampled joint trajectory plus a gripper schedule, in absolute seconds."""

    t0: float
    h: float
    Q: np.ndarray                                    # [N, 7]
    grip: list[tuple[float, float]] = field(default_factory=list)   # (t, ctrl) sorted
    kind: str = "hold"
    plan_id: int = -1
    target_uid: int = -1
    t_grasp: float = np.nan
    t_release: float = np.nan
    origin: tuple[int, int] | None = None           # (frame tag, capture time ps) of the data behind it
    goal_release_t: float = np.nan
    seq: int = -1                                    # unique id assigned by the bridge
    # Blend state (set on activation)
    t_act: float = np.nan
    blend_T: float = 0.0
    off_p: np.ndarray = field(default_factory=lambda: np.zeros(7))
    off_v: np.ndarray = field(default_factory=lambda: np.zeros(7))

    @property
    def t_end(self) -> float:
        return self.t0 + self.h * (len(self.Q) - 1)

    def _raw(self, t: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        Q, h = self.Q, self.h
        n = len(Q)
        if n == 1:
            return Q[0].copy(), np.zeros(7), np.zeros(7)
        if t <= self.t0:
            v0 = (Q[1] - Q[0]) / h
            return Q[0] + v0 * (t - self.t0), v0, np.zeros(7)
        if t >= self.t_end:
            return Q[-1].copy(), np.zeros(7), np.zeros(7)
        x = (t - self.t0) / h
        i = min(int(x), n - 2)
        u = x - i
        p0, p1 = Q[i], Q[i + 1]
        m0 = (Q[min(i + 1, n - 1)] - Q[max(i - 1, 0)]) / (2 if 0 < i else 1)
        m1 = (Q[min(i + 2, n - 1)] - Q[i]) / (2 if i + 2 < n else 1)
        u2, u3 = u * u, u * u * u
        p = (2 * u3 - 3 * u2 + 1) * p0 + (u3 - 2 * u2 + u) * m0 + (-2 * u3 + 3 * u2) * p1 + (u3 - u2) * m1
        v = ((6 * u2 - 6 * u) * p0 + (3 * u2 - 4 * u + 1) * m0 + (-6 * u2 + 6 * u) * p1 + (3 * u2 - 2 * u) * m1) / h
        a = ((12 * u - 6) * p0 + (6 * u - 4) * m0 + (-12 * u + 6) * p1 + (6 * u - 2) * m1) / (h * h)
        return p, v, a

    def eval(self, t: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Reference position, velocity, acceleration at time t including the activation blend."""
        p, v, a = self._raw(t)
        if self.blend_T > 0 and t < self.t_act + self.blend_T:
            T = self.blend_T
            s = max(0.0, (t - self.t_act) / T)
            s2, s3, s4, s5 = s * s, s**3, s**4, s**5
            h0 = 1 - 10 * s3 + 15 * s4 - 6 * s5
            h1 = s - 6 * s3 + 8 * s4 - 3 * s5
            dh0 = (-30 * s2 + 60 * s3 - 30 * s4) / T
            dh1 = (1 - 18 * s2 + 32 * s3 - 15 * s4) / T
            ddh0 = (-60 * s + 180 * s2 - 120 * s3) / (T * T)
            ddh1 = (-36 * s + 96 * s2 - 60 * s3) / (T * T)
            p = p + h0 * self.off_p + h1 * T * self.off_v
            v = v + dh0 * self.off_p + dh1 * T * self.off_v
            a = a + ddh0 * self.off_p + ddh1 * T * self.off_v
        return p, v, a

    def grip_at(self, t: float, default: float = GRIP_OPEN) -> float:
        g = default
        for tt, c in self.grip:
            if t >= tt:
                g = c
            else:
                break
        return g

    def activate(self, t_act: float, prev_p: np.ndarray, prev_v: np.ndarray) -> None:
        """Start blending from the previous reference (prev_p, prev_v) at t_act."""
        p, v, _ = self._raw(t_act)
        self.t_act = t_act
        self.off_p = prev_p - p
        self.off_v = prev_v - v
        mag = float(np.max(np.abs(self.off_p)))
        self.blend_T = float(np.clip(1.875 * mag / 1.0 + 0.06, 0.08, 0.8))


@dataclass
class Goal:
    """Output of the policy (T4). kind: pick | home | hold."""

    kind: str
    uid: int = -1
    p_obs: np.ndarray | None = None     # object position at t_obs
    v_obs: np.ndarray | None = None     # object velocity estimate (world, m/s)
    t_obs: float = np.nan               # capture time of the observation
    yaw: float = 0.0
    kind_obj: str = "box"
    half_h: float = 0.02
    t_grasp: float = np.nan
    latency_budget: float = 0.1         # policy's estimate of goal release -> plan activation
    plan_id: int = -1                   # pick episode id assigned by the policy
    origin: tuple[int, int] | None = None
    release_t: float = np.nan
    refine: bool = False                # same episode, updated prediction: keep the grasp time
    theta: float | None = None          # gripper closing direction (rad); None = from yaw
    place_slot: int = 0                 # which drop point in the bin

    def signature(self) -> tuple:
        if self.kind != "pick":
            return (self.kind, self.plan_id)
        gp = self.p_obs + self.v_obs * (self.t_grasp - self.t_obs)
        return ("pick", self.plan_id, self.uid, round(float(self.t_grasp), 3), tuple(np.round(gp, 3)))


@dataclass
class PlannerConfig:
    h: float = 0.02
    t_hover: float = 0.12
    t_desc: float = 0.30
    t_close: float = 0.22
    t_lift: float = 0.30
    h_pre: float = 0.10
    h_lift: float = 0.16
    v_joint_lim: float = 1.8            # rad/s used for segment timing (Panda limits are 2.175 to 2.61)
    y_min: float = -0.50                # pick window along the belt (reachability)
    y_max: float = 0.42
    ready_tcp: tuple[float, float, float] = (0.50, -0.30, 0.40)
    bin_tcp_z: float = 0.30
    release_hold: float = 0.25
    grasp_axis: str = "along_belt"      # along_belt | across_belt


class IK:
    """Damped least squares IK on the TCP site with a posture null-space term."""

    def __init__(self, sc: Scene) -> None:
        self.m = sc.model
        self.d = mujoco.MjData(sc.model)
        self.sc = sc
        self.site = sc.tcp_site
        self.qadr = sc.arm_qpos
        self.dof = sc.arm_dof
        jid = [mujoco.mj_name2id(self.m, mujoco.mjtObj.mjOBJ_JOINT, f"joint{i}") for i in range(1, 8)]
        rng = self.m.jnt_range[jid]
        self.lo = rng[:, 0] + 0.03
        self.hi = rng[:, 1] - 0.03
        self.jacp = np.zeros((3, self.m.nv))
        self.jacr = np.zeros((3, self.m.nv))
        self.q_rest = HOME_Q.copy()

    def fk(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        self.d.qpos[self.qadr] = q
        mujoco.mj_kinematics(self.m, self.d)
        return self.d.site_xpos[self.site].copy(), self.d.site_xmat[self.site].reshape(3, 3).copy()

    def solve(self, p: np.ndarray, R: np.ndarray, q0: np.ndarray, iters: int = 40, tol: float = 2e-4,
              lam: float = 0.01) -> tuple[np.ndarray, float]:
        """Return (q, residual) for target TCP position p and rotation matrix R."""
        q = q0.copy()
        err_n = np.inf
        eye7 = np.eye(7)
        lam2I = (lam * lam) * np.eye(6)
        e = np.empty(6)
        for _ in range(iters):
            self.d.qpos[self.qadr] = q
            mujoco.mj_kinematics(self.m, self.d)
            x = self.d.site_xpos[self.site]
            Rc = self.d.site_xmat[self.site].reshape(3, 3)
            e[:3] = p - x
            # sum_i rc_i x r_i = vee(R Rc^T - Rc R^T)
            Mr = R @ Rc.T
            e[3] = 0.25 * (Mr[2, 1] - Mr[1, 2])
            e[4] = 0.25 * (Mr[0, 2] - Mr[2, 0])
            e[5] = 0.25 * (Mr[1, 0] - Mr[0, 1])
            err_n = float(np.sqrt(e[0] ** 2 + e[1] ** 2 + e[2] ** 2) + 0.1 * np.sqrt(e[3] ** 2 + e[4] ** 2 + e[5] ** 2))
            if err_n < tol:
                break
            mujoco.mj_comPos(self.m, self.d)
            mujoco.mj_jacSite(self.m, self.d, self.jacp, self.jacr, self.site)
            J = np.vstack([self.jacp[:, self.dof], 0.5 * self.jacr[:, self.dof]])
            A = np.linalg.solve(J @ J.T + lam2I, J)     # (JJ^T + lam^2 I)^-1 J
            dq = A.T @ e + (eye7 - A.T @ J) @ (0.2 * (self.q_rest - q))
            step = np.max(np.abs(dq))
            if step > 0.3:
                dq *= 0.3 / step
            q = np.clip(q + dq, self.lo, self.hi)
        return q, err_n


def solve_robust(ik: "IK", p: np.ndarray, R: np.ndarray, seeds: list[np.ndarray], tol: float = 1e-3,
                 iters: int = 120) -> tuple[np.ndarray, float]:
    """Try several seeds; return the first solution under tol, else the best one."""
    best = (seeds[0], np.inf)
    for sd in seeds:
        q, r = ik.solve(p, R, sd, iters=iters)
        if r < best[1]:
            best = (q, r)
        if r < tol:
            break
    return best


def topdown_R(theta: float) -> np.ndarray:
    """Hand pointing down, fingers closing along world direction (cos theta, sin theta, 0)."""
    c, s = np.cos(theta), np.sin(theta)
    x_h = np.array([-s, c, 0.0])
    y_h = np.array([c, s, 0.0])
    z_h = np.array([0.0, 0.0, -1.0])
    return np.column_stack([x_h, y_h, z_h])


GRASP_AXIS_YAW = {"along_belt": np.pi / 2, "across_belt": 0.0}


def grasp_yaw(kind: str, yaw: float, axis: str = "along_belt") -> float:
    """Closing direction (world yaw, rad).

    along_belt: fingers straddle the object front and back along the direction of travel, so
    an along-belt prediction error lands a finger on the object (tolerance about 1.5 cm).
    across_belt: fingers close sideways; along-belt errors only shift the contact point.
    For boxes the closing direction snaps to the face normal nearest the chosen axis.
    """
    base = GRASP_AXIS_YAW[axis]
    if kind != "box":
        return float(base)
    rel = (yaw - base + np.pi / 4) % (np.pi / 2) - np.pi / 4
    return float(base + rel)


class Planner:
    """Turns goals into joint trajectories. Pure function of (goal, start state, time)."""

    def __init__(self, sc: Scene, cfg: PlannerConfig | None = None) -> None:
        self.sc = sc
        self.cfg = cfg or PlannerConfig()
        self.ik = IK(sc)
        # Panda joint velocity limits (rad/s) scaled by a planning margin.
        self.v_lim = np.array([2.175, 2.175, 2.175, 2.175, 2.61, 2.61, 2.61]) * (self.cfg.v_joint_lim / 2.175)
        q0, _ = self.ik.solve(np.array(self.cfg.ready_tcp), topdown_R(0.0), HOME_Q, iters=200)
        self.q7_center = float(q0[6])          # theta that puts joint 7 mid-range (see wrist_branch)
        self.theta_ready = self.wrist_branch(GRASP_AXIS_YAW[self.cfg.grasp_axis])
        q_seed = q0.copy()
        q_seed[6] = q0[6] - self.theta_ready
        self.q_ready, _ = self.ik.solve(np.array(self.cfg.ready_tcp), topdown_R(self.theta_ready), q_seed, iters=200)
        self.ik.q_rest = self.q_ready.copy()
        b = layout.BIN
        self.q_bins = []
        self.bin_points: list[tuple[float, float]] = []
        for dx, dy in ((-0.05, -0.04), (0.05, 0.04), (0.05, -0.04), (-0.05, 0.04)):
            self.bin_points.append((b["x"] + dx, b["y"] + dy))
            qb, _ = self.ik.solve(np.array([b["x"] + dx, b["y"] + dy, self.cfg.bin_tcp_z]),
                                  topdown_R(self.theta_ready), self.q_ready, iters=200)
            self.q_bins.append(qb)
        self.q_bin = self.q_bins[0]
        self.top = layout.CONVEYOR["top_z"]

    def wrist_branch(self, theta: float) -> float:
        """Pick theta + k*pi (same grasp for a parallel gripper) that keeps joint 7 mid-range.

        With the hand pointing down, closing yaw theta corresponds to q7 = q7_ready - theta,
        so the centre of the joint 7 range maps to theta = q7_ready.
        """
        c = self.q7_center
        return float(theta + np.pi * np.round((c - theta) / np.pi))

    def _seeds(self, q: np.ndarray, theta: float) -> list[np.ndarray]:
        """IK seeds: the given configuration, then the ready pose with the wrist pre-rotated."""
        qr = self.q_ready.copy()
        qr[6] = np.clip(qr[6] - (theta - self.theta_ready), self.ik.lo[6], self.ik.hi[6])
        return [q, qr, self.q_ready]

    def seg_time(self, qa: np.ndarray, qb: np.ndarray, t_min: float = 0.25) -> float:
        """Quintic segment duration that keeps every joint under its velocity budget."""
        return max(t_min, 1.875 * float(np.max(np.abs(qb - qa) / self.v_lim)))

    def _joint_move(self, t0: float, qa, va, qb, T: float) -> np.ndarray:
        n = max(2, int(np.ceil(T / self.cfg.h)) + 1)
        ts = np.arange(n) * self.cfg.h
        return _quintic_hermite(qa, va, qb, np.zeros(7), n * self.cfg.h - self.cfg.h, ts)

    def plan_home(self, t: float, q_start: np.ndarray, v_start: np.ndarray, plan_id: int) -> Trajectory:
        T = self.seg_time(q_start, self.q_ready)
        Q = self._joint_move(t, q_start, v_start, self.q_ready, T)
        return Trajectory(t, self.cfg.h, Q, grip=[(t, GRIP_OPEN)], kind="home", plan_id=plan_id)

    def plan_hold(self, t: float, q_start: np.ndarray, plan_id: int, grip: float) -> Trajectory:
        return Trajectory(t, self.cfg.h, q_start[None, :].copy(), grip=[(t, grip)], kind="hold", plan_id=plan_id)

    def plan_place(self, t: float, q_start: np.ndarray, v_start: np.ndarray, plan_id: int, uid: int) -> Trajectory:
        """Carry whatever is in the hand to the bin, release, return to ready."""
        cfg = self.cfg
        T1 = self.seg_time(q_start, self.q_bin, 0.6)
        Q1 = self._joint_move(t, q_start, v_start, self.q_bin, T1)
        t_rel = t + (len(Q1) - 1) * cfg.h
        nh = int(round(cfg.release_hold / cfg.h))
        T2 = self.seg_time(self.q_bin, self.q_ready, 0.6)
        Q2 = self._joint_move(0, self.q_bin, np.zeros(7), self.q_ready, T2)
        Q = np.vstack([Q1, np.repeat(self.q_bin[None], nh, 0), Q2[1:]])
        return Trajectory(t, cfg.h, Q, grip=[(t, GRIP_CLOSED), (t_rel, GRIP_OPEN)], kind="place",
                          plan_id=plan_id, target_uid=uid, t_release=t_rel)

    def plan_pick(self, g: Goal, t: float, q_start: np.ndarray, v_start: np.ndarray) -> Trajectory | None:
        """Plan an intercept of the object predicted by goal g. Returns None if infeasible.

        t is the planning (release) time; q_start/v_start the reference state at that time.
        The plan assumes it will become active about g.latency_budget later and pushes the
        grasp time back if the arm cannot reach the pre-grasp point in time.
        """
        cfg = self.cfg
        theta = grasp_yaw(g.kind_obj, g.yaw, cfg.grasp_axis) if g.theta is None else g.theta
        theta = self.wrist_branch(theta)
        R = topdown_R(theta)
        z_grasp = max(g.p_obs[2], self.top + 0.018)
        if g.kind_obj != "sphere":
            z_grasp = max(z_grasp, self.top + 2 * g.half_h - 0.022)

        def obj_xy(tt):
            return g.p_obs[:2] + g.v_obs[:2] * (tt - g.t_obs)

        t_g = g.t_grasp
        t_join = t_g - cfg.t_desc - cfg.t_hover
        t_ready = t + g.latency_budget
        # Feasibility: time to reach the hover point from the start state.
        if t_join > t and not g.refine:
            p_join = np.array([*obj_xy(t_join), z_grasp + cfg.h_pre])
            q_join, res = solve_robust(self.ik, p_join, R, self._seeds(q_start, theta))
            if res > 3e-3:
                return None
            need = self.seg_time(q_start, q_join, 0.2)
            if t_join < t_ready + need:
                shift = t_ready + need - t_join
                t_g += shift
                t_join += shift
        y_g = obj_xy(t_g)[1]
        y_join = obj_xy(t_join)[1]
        if y_g > cfg.y_max + (0.04 if g.refine else 0.0) or y_join < cfg.y_min - 0.06:
            return None
        if t_g < t_ready - cfg.t_close:
            return None  # the grasp moment would be over before this plan could take effect
        # Sample the Cartesian path.
        t_lift0 = t_g + cfg.t_close
        t_lift1 = t_lift0 + cfg.t_lift
        t_path0 = max(t_join, t)
        ts = np.arange(t_path0, t_lift1 + 1e-9, cfg.h)
        P = np.zeros((len(ts), 3))
        for k, tt in enumerate(ts):
            if tt < t_lift0:
                xy = obj_xy(tt)
                if tt < t_g - cfg.t_desc:
                    z = z_grasp + cfg.h_pre
                elif tt < t_g:
                    z = z_grasp + cfg.h_pre * (1 - _smooth((tt - (t_g - cfg.t_desc)) / cfg.t_desc))
                else:
                    z = z_grasp
            else:
                u = (tt - t_lift0) / cfg.t_lift
                # keep moving with the belt, decelerating, while lifting
                xy = obj_xy(t_lift0) + g.v_obs[:2] * cfg.t_lift * (u - 0.5 * u * u)
                z = z_grasp + cfg.h_lift * _smooth(u)
            P[k] = (xy[0], xy[1], z)
        Qp = np.zeros((len(ts), 7))
        if t_join <= t:
            q = q_start
        else:
            q, res = solve_robust(self.ik, P[0], R, self._seeds(q_start, theta))
        max_step = self.v_lim * cfg.h * 2.0
        for k in range(len(ts)):
            q_prev = q
            q, res = self.ik.solve(P[k], R, q, iters=20 if k else 120)
            if res > 1e-3:
                q, res = solve_robust(self.ik, P[k], R, self._seeds(q, theta))
                if res > 3e-3:
                    return None
            # A jump between neighbouring samples means IK switched branch: not executable.
            if k > 0 and np.any(np.abs(q - q_prev) > max_step):
                return None
            Qp[k] = q
        # Pre-segment from the start state to the path start.
        if t_path0 > t + 1e-9:
            T_pre = t_path0 - t
            n = int(round(T_pre / cfg.h))
            hh = T_pre / max(n, 1)
            v_path = (Qp[1] - Qp[0]) / cfg.h if len(Qp) > 1 else np.zeros(7)
            Qpre = _quintic_hermite(q_start, v_start, Qp[0], v_path, T_pre, np.arange(n) * hh) if n > 0 else np.zeros((0, 7))
            # resample to the uniform grid starting at t
            Q = np.vstack([Qpre, Qp])
            t0 = t_path0 - n * cfg.h
        else:
            Q = Qp
            t0 = t_path0
        # Carry to the bin, release, return to ready.
        slot = g.place_slot % len(self.q_bins)
        bx, by = self.bin_points[slot]
        seed = self.q_bins[slot].copy()
        seed[6] -= theta - self.theta_ready
        q_bin, res = self.ik.solve(np.array([bx, by, cfg.bin_tcp_z]), R, seed, iters=80)
        if res > 3e-3:
            q_bin = self.q_bins[slot]
        T1 = self.seg_time(Q[-1], q_bin, 0.6)
        Q1 = self._joint_move(0, Q[-1], np.zeros(7), q_bin, T1)
        t_rel = t0 + (len(Q) - 1 + len(Q1) - 1) * cfg.h
        nh = int(round(cfg.release_hold / cfg.h))
        T2 = self.seg_time(q_bin, self.q_ready, 0.6)
        Q2 = self._joint_move(0, q_bin, np.zeros(7), self.q_ready, T2)
        Q = np.vstack([Q, Q1[1:], np.repeat(q_bin[None], nh, 0), Q2[1:]])
        Q = np.clip(Q, self.ik.lo, self.ik.hi)
        grip = [(t0, GRIP_OPEN), (t_g, GRIP_CLOSED), (t_rel, GRIP_OPEN)]
        return Trajectory(t0, cfg.h, Q, grip=grip, kind="pick", plan_id=g.plan_id, target_uid=g.uid,
                          t_grasp=t_g, t_release=t_rel, origin=g.origin, goal_release_t=g.release_t)
