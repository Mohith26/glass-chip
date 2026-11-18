"""Robot behavior metrics (spec Section 9.3) and the oscillation detector."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class OscillationConfig:
    amp_rad_s: float = 0.08        # joint velocity error amplitude that counts as a swing
    window_s: float = 0.25         # sliding window
    min_flips: int = 4             # sign flips inside the window (two full cycles)
    quiet_s: float = 0.3           # no flips for this long ends an episode


class OscillationDetector:
    """Counts controller oscillation episodes from joint velocity tracking error.

    For each joint, a sign flip is recorded when the velocity error (reference minus
    measured) crosses from below -amp to above +amp or back (hysteresis, so noise around zero
    does not count). An episode starts when any joint has at least min_flips flips within
    window_s and ends after quiet_s without flips. Severity is the peak absolute velocity
    error seen during the episode.
    """

    def __init__(self, cfg: OscillationConfig | None = None, n: int = 7) -> None:
        self.cfg = cfg or OscillationConfig()
        self.sign = np.zeros(n, dtype=np.int8)
        self.flips: list[deque] = [deque() for _ in range(n)]
        self.active = False
        self.start = 0.0
        self.last_flip = -1e9
        self.peak = 0.0
        self.episodes: list[dict[str, float]] = []

    def update(self, t: float, ev: np.ndarray) -> None:
        c = self.cfg
        up = (ev > c.amp_rad_s) & (self.sign != 1)
        dn = (ev < -c.amp_rad_s) & (self.sign != -1)
        if up.any() or dn.any():
            for j in np.flatnonzero(up | dn):
                if self.sign[j] != 0:
                    dq = self.flips[j]
                    dq.append(t)
                    while dq and t - dq[0] > c.window_s:
                        dq.popleft()
                    self.last_flip = t
                    if not self.active and len(dq) >= c.min_flips:
                        self.active = True
                        self.start = dq[0]
                        self.peak = 0.0
            self.sign[up] = 1
            self.sign[dn] = -1
        if self.active:
            self.peak = max(self.peak, float(np.max(np.abs(ev))))
            if t - self.last_flip > c.quiet_s:
                self.episodes.append({"t_start": self.start, "t_end": self.last_flip,
                                      "duration_s": self.last_flip - self.start, "peak_vel_err": self.peak})
                self.active = False
                for dq in self.flips:
                    dq.clear()

    def finish(self, t: float) -> None:
        if self.active:
            self.episodes.append({"t_start": self.start, "t_end": t, "duration_s": t - self.start,
                                  "peak_vel_err": self.peak})
            self.active = False


def quantiles(x: list[float] | np.ndarray) -> dict[str, float | None]:
    a = np.asarray(x, dtype=float)
    if a.size == 0:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {"n": int(a.size), "mean": float(a.mean()), "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)), "p99": float(np.percentile(a, 99)), "max": float(a.max())}


@dataclass
class GraspLedger:
    """Matches gripper close commands to object outcomes (lift, placement)."""

    attempts: dict[int, dict[str, Any]] = field(default_factory=dict)   # plan_seq -> record

    def close(self, t: float, plan_id: int, uid: int) -> dict[str, Any]:
        rec = {"t_close": t, "plan_id": plan_id, "uid": uid, "outcome": "pending"}
        self.attempts[plan_id] = rec
        return rec
