"""T8 safety monitor: speed and separation monitoring against the human proxy.

Separation S is the minimum distance between the human (a vertical capsule) and the robot
(spheres around link origins and the TCP), using the human position from the latest camera
frame and robot link positions from the same frame. Zones (assumptions, ISO/TS 15066 style):

    S >= slow_m:             alpha = 1 (full speed)
    stop_m < S < slow_m:     alpha scales linearly from 0 to 1
    S <= stop_m:             protective stop (alpha = 0)

The output alpha reaches the robot only through the next T7 controller job after the T8
job completes, so a slow or late T8 makes the stop late.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cosim.stack.perception import Frame

HUMAN_RADIUS = 0.18
HUMAN_Z = (0.2, 1.66)
LINK_RADIUS = 0.08


def human_robot_distance(human_xy: np.ndarray | None, robot_pts: np.ndarray) -> float:
    """Minimum surface distance between the human capsule and robot link spheres (m)."""
    if human_xy is None:
        return float("inf")
    z = np.clip(robot_pts[:, 2], HUMAN_Z[0], HUMAN_Z[1])
    dxy = robot_pts[:, :2] - human_xy[None, :]
    d = np.sqrt((dxy ** 2).sum(1) + (robot_pts[:, 2] - z) ** 2)
    return float(d.min() - HUMAN_RADIUS - LINK_RADIUS)


@dataclass
class SafetyConfig:
    stop_m: float = 0.45
    slow_m: float = 1.0
    alpha_floor: float = 0.0     # minimum speed scale inside the slow band (stop zone is always 0)


@dataclass
class SafetyOutput:
    alpha: float
    separation: float
    state: str            # ok | slow | stop
    origin: tuple[int, int] | None
    seq: int


class SafetyMonitor:
    def __init__(self, cfg: SafetyConfig | None = None) -> None:
        self.cfg = cfg or SafetyConfig()
        self.seq = 0

    def evaluate(self, frame: Frame | None) -> SafetyOutput:
        self.seq += 1
        if frame is None:
            return SafetyOutput(1.0, float("inf"), "ok", None, self.seq)
        s = human_robot_distance(frame.human_xy, frame.robot_points)
        c = self.cfg
        if s <= c.stop_m:
            a, st = 0.0, "stop"
        elif s < c.slow_m:
            a, st = max(c.alpha_floor, (s - c.stop_m) / (c.slow_m - c.stop_m)), "slow"
        else:
            a, st = 1.0, "ok"
        return SafetyOutput(a, s, st, (frame.tag, int(round(frame.t_capture * 1e12))), self.seq)
