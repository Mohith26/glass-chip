"""T7 joint impedance controller (1 kHz) and the speed-scaled trajectory clock.

Control law (joint torques, sent to the torque motors defined in cosim/envs/panda_torque.xml):

    tau = M(q) * (qdd_ref + Kp * (q_ref - q) + Kd * (qd_ref - qd)) + c(q, qd)

M and c (Coriolis, centrifugal, gravity) come from MuJoCo for the state snapshotted when
the job was released. This is an impedance law with inertia shaping (a computed-torque
form), so the closed loop behaves like a decoupled mass-spring-damper per joint with natural
frequency sqrt(Kp). The gripper is the stock Menagerie position actuator (0 = closed,
255 = open).

Timing semantics: the command is computed from the snapshot at release and only reaches the
actuators when the job completes. Between completions the actuators hold the last command
(zero-order hold). A late or jittery controller therefore acts on stale state, which is what
produces oscillation when the control loop is slow.

Speed scaling: the reference is evaluated at a trajectory clock tau that advances at rate
alpha in [0, 1] (alpha comes from the safety monitor, rate limited). alpha = 0 freezes the
reference, which is a controlled (category 2 style) stop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cosim.stack.planner import GRIP_OPEN, Trajectory


@dataclass
class ControllerConfig:
    wn: float = 40.0            # rad/s closed-loop natural frequency (assumption)
    zeta: float = 0.9
    decel_per_s: float = 5.0    # alpha slew when slowing (full speed to stop in 0.2 s)
    accel_per_s: float = 2.0    # alpha slew when resuming
    tau_lim: tuple = (87, 87, 87, 87, 12, 12, 12)


@dataclass
class ControlOutput:
    tau: np.ndarray
    grip: float
    q_ref: np.ndarray
    plan_id: int
    safety_id: int
    alpha: float
    v_ref: np.ndarray
    traj_seq: int = -1


class JointImpedanceController:
    """Stateful T7 controller. step() is called once per released T7 job."""

    def __init__(self, cfg: ControllerConfig | None = None) -> None:
        self.cfg = cfg or ControllerConfig()
        self.kp = self.cfg.wn ** 2
        self.kd = 2 * self.cfg.zeta * self.cfg.wn
        self.tau_lim = np.array(self.cfg.tau_lim, dtype=float)
        self.traj: Trajectory | None = None
        self.alpha = 1.0
        self.tau_clock = 0.0      # trajectory time
        self.last_t = None
        self.last_ref = (np.zeros(7), np.zeros(7))
        self.last_err = np.zeros(7)
        self.last_verr = np.zeros(7)
        self.grip = GRIP_OPEN
        self.q_lo: np.ndarray | None = None   # joint limits (set by the bridge)
        self.q_hi: np.ndarray | None = None

    @property
    def lag(self) -> float:
        """How far the trajectory clock trails real time (s), from safety slowdowns."""
        return 0.0 if self.last_t is None else self.last_t - self.tau_clock

    def install(self, traj: Trajectory, t: float) -> None:
        """Activate a new trajectory at time t, blending from the current reference."""
        if self.traj is None:
            prev_p, prev_v = traj._raw(t)[0], np.zeros(7)
        else:
            prev_p, prev_v = self.last_ref
        # The trajectory clock is re-synchronised to real time on activation.
        self.tau_clock = t
        traj.activate(t, prev_p, prev_v)
        self.traj = traj

    def step(self, t: float, q: np.ndarray, qd: np.ndarray, M: np.ndarray, bias: np.ndarray,
             alpha_target: float, safety_id: int) -> ControlOutput:
        cfg = self.cfg
        dt = 0.001 if self.last_t is None else max(0.0, t - self.last_t)
        self.last_t = t
        if alpha_target < self.alpha:
            self.alpha = max(alpha_target, self.alpha - cfg.decel_per_s * dt)
        else:
            self.alpha = min(alpha_target, self.alpha + cfg.accel_per_s * dt)
        self.tau_clock += self.alpha * dt
        if self.traj is None:
            q_ref, v_ref, a_ref = q.copy(), np.zeros(7), np.zeros(7)
            grip = self.grip
            pid = -1
        else:
            q_ref, v_ref, a_ref = self.traj.eval(self.tau_clock)
            v_ref = v_ref * self.alpha
            a_ref = a_ref * self.alpha * self.alpha
            grip = self.traj.grip_at(self.tau_clock, self.grip)
            pid = self.traj.plan_id
        self.grip = grip
        if self.q_lo is not None:
            out = (q_ref < self.q_lo) | (q_ref > self.q_hi)
            if out.any():
                q_ref = np.clip(q_ref, self.q_lo, self.q_hi)
                v_ref = np.where(out, 0.0, v_ref)
                a_ref = np.where(out, 0.0, a_ref)
        e = q_ref - q
        ev = v_ref - qd
        self.last_ref = (q_ref, v_ref)
        self.last_err = e
        self.last_verr = ev
        tau = M @ (a_ref + self.kp * e + self.kd * ev) + bias
        tau = np.clip(tau, -self.tau_lim, self.tau_lim)
        seq = -1 if self.traj is None else getattr(self.traj, "seq", -1)
        return ControlOutput(tau, grip, q_ref, pid, safety_id, self.alpha, v_ref, seq)
