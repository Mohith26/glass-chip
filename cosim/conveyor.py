"""Conveyor belt, object spawning and object bookkeeping, plus the human proxy walker.

Belt model: MuJoCo has no moving-surface contact, so the belt geom is nearly frictionless
and each object resting on it receives a horizontal force that drives its velocity toward
the belt velocity, capped at mu_belt * m * g. That is kinetic belt friction applied at the
centre of mass. It keeps objects on the belt robustly while still letting the gripper pull
them off (the cap is a few newtons).

Disturbances (assumptions, both Ornstein-Uhlenbeck processes): the belt speed follows
v_nom * (1 + r(t)) with r of sigma 5 % and time constant 0.6 s (drive regulation), and each
object moves at belt speed * (1 + s_i(t)) with s_i of sigma 15 % and time constant 0.3 s
(slip between product and belt). These are what make stale observations costly:
extrapolating an old detection with an estimated velocity accumulates error that grows with
the observation age and with conveyor speed. With no disturbance, a latency-compensating
policy would be almost insensitive to latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cosim.envs import build_scene as layout
from cosim.scene import Scene, park_object, place_object

PARKED, ON_BELT, LIFTED, IN_BIN, DROPPED = 0, 1, 2, 3, 4
STATUS_NAMES = {PARKED: "parked", ON_BELT: "on_belt", LIFTED: "lifted", IN_BIN: "in_bin", DROPPED: "dropped"}


@dataclass
class ConveyorConfig:
    speed_mps: float = 0.2
    spacing_m: float = 0.30
    ripple_sigma: float = 0.05       # assumption, see module docstring
    ripple_tau_s: float = 0.6        # assumption
    slip_sigma: float = 0.15         # assumption: per-object speed variation (slip), fraction of belt speed
    slip_tau_s: float = 0.3          # assumption: slip correlation time
    mu_belt: float = 0.8             # assumption: rubber belt
    lateral_jitter_m: float = 0.03
    max_yaw_rad: float = 0.5
    first_spawn_y: float | None = None  # pre-load the belt so the first object arrives sooner


@dataclass
class ObjectEvent:
    t: float
    kind: str
    obj: int
    data: dict = field(default_factory=dict)


class Conveyor:
    """Owns belt speed, slats, the object pool and per-object status."""

    def __init__(self, sc: Scene, cfg: ConveyorConfig, rng: np.random.Generator) -> None:
        self.sc = sc
        self.cfg = cfg
        self.rng = rng
        c = layout.CONVEYOR
        self.cx, self.half_w, self.top = c["x"], c["width"] / 2, c["top_z"]
        self.y0, self.y1 = c["y0"], c["y1"]
        self.length = self.y1 - self.y0
        self.speed_nom = cfg.speed_mps
        self.r = 0.0
        self.v = cfg.speed_mps
        self.s = 0.0                 # belt displacement (m)
        self.s_last_spawn = -1e9
        n = len(sc.objects)
        self.status = np.zeros(n, dtype=np.int8)
        self.spawn_t = np.full(n, np.nan)
        self.status_t = np.zeros(n)
        self.offered = 0
        self.events: list[ObjectEvent] = []
        self._noise = np.empty(0)
        self._noise_i = 0
        self.slat_base = (np.arange(layout.N_SLATS) + 0.5) * layout.SLAT_PITCH
        self.slat_pos = np.zeros((layout.N_SLATS, 3))
        self.slat_pos[:, 0] = self.cx
        self.slat_pos[:, 2] = self.top + 0.0006
        self.uid = np.full(n, -1, dtype=np.int64)  # unique object instance id per spawn
        self.slip = np.zeros(n)
        self._next_uid = 0
        if cfg.first_spawn_y is not None:
            self._preload(cfg.first_spawn_y)

    # ------------------------------------------------------------------ helpers
    def _randn(self) -> float:
        if self._noise_i >= len(self._noise):
            self._noise = self.rng.standard_normal(4096)
            self._noise_i = 0
        x = self._noise[self._noise_i]
        self._noise_i += 1
        return float(x)

    def set_speed(self, mps: float) -> None:
        self.speed_nom = float(mps)

    def _spawn(self, t: float, y: float) -> int | None:
        free = np.flatnonzero(self.status == PARKED)
        if len(free) == 0:
            # Recycle the oldest object sitting in the bin.
            in_bin = np.flatnonzero(self.status == IN_BIN)
            if len(in_bin) == 0:
                return None
            oldest = in_bin[np.argmin(self.status_t[in_bin])]
            park_object(self.sc, int(oldest))
            self.status[oldest] = PARKED
            free = np.array([oldest])
        i = int(free[self.rng.integers(len(free))])
        o = self.sc.objects[i]
        x = self.cx + self.rng.uniform(-1, 1) * self.cfg.lateral_jitter_m
        yaw = self.rng.uniform(-1, 1) * self.cfg.max_yaw_rad if o.kind == "box" else 0.0
        place_object(self.sc, i, np.array([x, y, self.top + o.half_height + 0.0005]), yaw)
        self.slip[i] = self.cfg.slip_sigma * self.rng.standard_normal()
        self.sc.data.qvel[o.dof_adr + 1] = self.v * (1 + self.slip[i])
        self.status[i] = ON_BELT
        self.spawn_t[i] = t
        self.status_t[i] = t
        self.uid[i] = self._next_uid
        self._next_uid += 1
        self.offered += 1
        self.events.append(ObjectEvent(t, "spawn", i, {"uid": int(self.uid[i])}))
        return i

    def _preload(self, y_first: float) -> None:
        y = y_first
        while y > self.y0 + 0.04:
            self._spawn(0.0, y)
            y -= self.cfg.spacing_m
        self.s_last_spawn = self.s - (y + self.cfg.spacing_m - (self.y0 + 0.04))

    # ------------------------------------------------------------------ per step
    def step(self, t: float, dt: float) -> None:
        """Advance belt speed and displacement, spawn, apply belt forces, update statuses."""
        cfg = self.cfg
        tau = cfg.ripple_tau_s
        self.r += (-self.r / tau) * dt + cfg.ripple_sigma * np.sqrt(2.0 * dt / tau) * self._randn()
        self.v = max(0.0, self.speed_nom * (1.0 + self.r))
        self.s += self.v * dt
        if cfg.slip_sigma > 0:
            ts = cfg.slip_tau_s
            self.slip += (-self.slip / ts) * dt + cfg.slip_sigma * np.sqrt(2.0 * dt / ts) * self.rng.standard_normal(len(self.slip))
        if self.s - self.s_last_spawn >= cfg.spacing_m and self.speed_nom > 0:
            if self._spawn(t, self.y0 + 0.04) is not None:
                self.s_last_spawn = self.s
        d = self.sc.data
        self.slat_pos[:, 1] = self.y0 + np.mod(self.slat_base + self.s, self.length)
        d.mocap_pos[self.sc.slat_mocap] = self.slat_pos

        sc = self.sc
        pos = d.xpos[sc.obj_body_ids]
        st = self.status
        on = st == ON_BELT
        bottom = pos[:, 2] - sc.obj_half_h
        touching = on & (bottom < self.top + 0.006) & (np.abs(pos[:, 0] - self.cx) < self.half_w) \
            & (pos[:, 1] > self.y0) & (pos[:, 1] < self.y1)
        xf = d.xfrc_applied
        xf[sc.obj_body_ids] = 0.0
        if touching.any():
            idx = np.flatnonzero(touching)
            dof = sc.obj_dof_adr[idx]
            vx = d.qvel[dof]
            vy = d.qvel[dof + 1]
            m = sc.obj_mass[idx]
            fx = m * (0.0 - vx) / 0.01
            fy = m * (self.v * (1.0 + self.slip[idx]) - vy) / 0.01
            cap = cfg.mu_belt * m * 9.81
            mag = np.hypot(fx, fy)
            scale = np.minimum(1.0, cap / np.maximum(mag, 1e-9))
            xf[sc.obj_body_ids[idx], 0] = fx * scale
            xf[sc.obj_body_ids[idx], 1] = fy * scale
            # Belt friction also resists spinning about the vertical axis. Free-joint angular
            # velocity is in the body frame, so rotate it to world before damping.
            R = d.xmat[sc.obj_body_ids[idx]].reshape(-1, 3, 3)
            w_local = np.stack([d.qvel[dof + 3], d.qvel[dof + 4], d.qvel[dof + 5]], axis=1)
            wz = np.einsum("nj,nj->n", R[:, 2, :], w_local)
            xf[sc.obj_body_ids[idx], 5] = -0.001 * wz

        self._update_status(t, pos)

    def _update_status(self, t: float, pos: np.ndarray) -> None:
        sc = self.sc
        d = sc.data
        tcp = d.site_xpos[sc.tcp_site]
        b = layout.BIN
        for i in np.flatnonzero(self.status != PARKED):
            if self.status_t[i] == t and self.status[i] == ON_BELT:
                continue  # spawned this step; xpos is from before the teleport
            s = self.status[i]
            p = pos[i]
            hh = sc.obj_half_h[i]
            in_bin_xy = abs(p[0] - b["x"]) < b["half"][0] and abs(p[1] - b["y"]) < b["half"][1]
            if s == ON_BELT:
                if p[1] > self.y1 - 0.03:
                    self.events.append(ObjectEvent(t, "exited", int(i), {"uid": int(self.uid[i])}))
                    park_object(sc, int(i))
                    self.status[i] = PARKED
                elif p[2] - hh > self.top + 0.03 and np.linalg.norm(p - tcp) < 0.06:
                    self._set(i, LIFTED, t, "lifted")
                elif p[2] < self.top - 0.04:
                    self._set(i, DROPPED, t, "dropped")
            elif s == LIFTED:
                if np.linalg.norm(p - tcp) > 0.09:
                    if in_bin_xy and p[2] < 0.2:
                        pass  # released over the bin; wait until it settles
                    elif p[2] < self.top + hh + 0.01 and abs(p[0] - self.cx) < self.half_w:
                        self._set(i, ON_BELT, t, "dropped_on_belt")
                        continue
                    elif p[2] < 0.03 + hh:
                        self._set(i, DROPPED, t, "dropped")
                        continue
                o = sc.objects[i]
                if in_bin_xy and p[2] < 0.25 and np.linalg.norm(p - tcp) > 0.06 \
                        and np.linalg.norm(d.qvel[o.dof_adr:o.dof_adr + 3]) < 0.05:
                    err = float(np.hypot(p[0] - b["x"], p[1] - b["y"]))
                    self._set(i, IN_BIN, t, "placed", {"placement_error_m": err})
            elif s == DROPPED:
                if t - self.status_t[i] > 0.5:
                    park_object(sc, int(i))
                    self.status[i] = PARKED
            elif s == IN_BIN:
                pass
        in_bin = np.flatnonzero(self.status == IN_BIN)
        if len(in_bin) > 4:
            oldest = int(in_bin[np.argmin(self.status_t[in_bin])])
            park_object(sc, oldest)
            self.status[oldest] = PARKED

    def _set(self, i: int, s: int, t: float, kind: str, extra: dict | None = None) -> None:
        self.status[i] = s
        self.status_t[i] = t
        data = {"uid": int(self.uid[i])}
        if extra:
            data.update(extra)
        self.events.append(ObjectEvent(t, kind, int(i), data))


@dataclass
class HumanPath:
    """Waypoints (x, y) walked at a fixed speed starting at t_start."""

    t_start: float
    waypoints: list[tuple[float, float]]
    speed_mps: float = 1.0
    dwell_s: float = 2.0             # time spent at the last-but-one waypoint before leaving


class HumanProxy:
    """Moves the human mocap body along scripted paths."""

    def __init__(self, sc: Scene, start_xy: tuple[float, float] = (2.2, 0.0)) -> None:
        self.sc = sc
        self.xy = np.array(start_xy, dtype=float)
        self.paths: list[HumanPath] = []
        self._segs: list[tuple[float, np.ndarray, np.ndarray, float]] = []

    def add_path(self, path: HumanPath) -> None:
        """Convert a path into timed straight segments (start time, from, to, duration)."""
        t = path.t_start
        prev = np.array(path.waypoints[0], dtype=float)
        pts = [np.array(w, dtype=float) for w in path.waypoints[1:]]
        for k, p in enumerate(pts):
            dur = float(np.linalg.norm(p - prev)) / max(path.speed_mps, 1e-6)
            self._segs.append((t, prev, p, dur))
            t += dur
            if k == len(pts) - 2:
                t += path.dwell_s
            prev = p
        self._segs.sort(key=lambda s: s[0])

    def step(self, t: float) -> None:
        xy = None
        for t0, a, b, dur in self._segs:
            if t < t0:
                break
            u = 1.0 if dur <= 0 else min(1.0, (t - t0) / dur)
            xy = a + (b - a) * u
        if xy is not None:
            self.xy = xy
        self.sc.data.mocap_pos[self.sc.human_mocap] = (self.xy[0], self.xy[1], 0.0)
