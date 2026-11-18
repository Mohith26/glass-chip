"""T6 state estimation: per-object constant-velocity Kalman tracks from detections.

Data association uses the object instance id carried by the detection (a simplification:
a real tracker would associate by gating on predicted position). Each track keeps the state
at the capture time of its latest detection, so consumers know the observation age and
extrapolate themselves.

Joint state comes straight from encoders in the controller snapshot (the RT core reads them
from tightly coupled memory), so it is not filtered here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cosim.stack.perception import DetectionSet


@dataclass
class Track:
    uid: int
    slot: int
    t: float                # capture time of the latest detection fused
    x: np.ndarray           # [px, py, vx, vy]
    P: np.ndarray           # 4x4 covariance
    z: float
    yaw: float
    kind: str
    half_h: float
    n: int
    origin: tuple[int, int]  # (frame tag, capture time ps) of the latest detection

    @property
    def pos(self) -> np.ndarray:
        return np.array([self.x[0], self.x[1], self.z])

    @property
    def vel(self) -> np.ndarray:
        return np.array([self.x[2], self.x[3], 0.0])


@dataclass
class EstimatorConfig:
    accel_sigma: float = 0.15      # m/s^2 process noise (belt ripple), assumption
    meas_sigma: float = 0.003      # m
    prior_v_sigma: float = 0.03    # m/s around the conveyor setpoint
    drop_after_s: float = 1.0


class ObjectTracker:
    """Stateful T6 tracker. update() runs once per released T6 job."""

    def __init__(self, cfg: EstimatorConfig | None = None, belt_velocity: float = 0.2) -> None:
        self.cfg = cfg or EstimatorConfig()
        self.tracks: dict[int, Track] = {}
        self.belt_v = belt_velocity

    def set_belt_velocity(self, v: float) -> None:
        self.belt_v = v

    def _predict(self, tr: Track, t: float) -> None:
        dt = t - tr.t
        if dt <= 0:
            return
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = self.cfg.accel_sigma ** 2
        G = np.array([[0.5 * dt * dt, 0], [0, 0.5 * dt * dt], [dt, 0], [0, dt]])
        tr.x = F @ tr.x
        tr.P = F @ tr.P @ F.T + q * (G @ G.T)
        tr.t = t

    def update(self, t_now: float, batches: list[DetectionSet]) -> dict[int, Track]:
        """Fuse detection batches (in capture order) and return a snapshot of live tracks."""
        cfg = self.cfg
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1.0
        R = (cfg.meas_sigma ** 2) * np.eye(2)
        for b in sorted(batches, key=lambda b: (b.t_capture, b.tag)):
            for det in b.detections:
                tr = self.tracks.get(det.uid)
                origin = (b.tag, int(round(b.t_capture * 1e12)))
                if tr is None:
                    x = np.array([det.pos[0], det.pos[1], 0.0, self.belt_v])
                    P = np.diag([cfg.meas_sigma ** 2] * 2 + [cfg.prior_v_sigma ** 2] * 2)
                    self.tracks[det.uid] = Track(det.uid, det.slot, b.t_capture, x, P, det.pos[2], det.yaw,
                                                 det.kind, det.half_h, 1, origin)
                    continue
                if b.t_capture < tr.t:
                    continue  # out-of-order detection older than the track state
                self._predict(tr, b.t_capture)
                y = det.pos[:2] - H @ tr.x
                S = H @ tr.P @ H.T + R
                K = tr.P @ H.T @ np.linalg.inv(S)
                tr.x = tr.x + K @ y
                tr.P = (np.eye(4) - K @ H) @ tr.P
                tr.z = 0.7 * tr.z + 0.3 * det.pos[2]
                dyaw = (det.yaw - tr.yaw + np.pi) % (2 * np.pi) - np.pi
                tr.yaw = tr.yaw + 0.3 * dyaw
                tr.n += 1
                tr.origin = origin
        for uid in [u for u, tr in self.tracks.items() if t_now - tr.t > cfg.drop_after_s]:
            del self.tracks[uid]
        return {u: Track(tr.uid, tr.slot, tr.t, tr.x.copy(), tr.P.copy(), tr.z, tr.yaw, tr.kind, tr.half_h,
                         tr.n, tr.origin) for u, tr in self.tracks.items()}
