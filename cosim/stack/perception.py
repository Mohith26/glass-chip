"""T1 camera ingest and T2 object detection.

There is no rendering here (offscreen OpenGL is unavailable on this machine, and the chip
cost of perception is modeled from the detector's operator graph, not from pixels). A camera
frame is a ground-truth snapshot of the scene taken at the T1 release time, restricted to
what lies inside that camera's frustum. Detection turns a frame into noisy object poses.

Latency is physically real: the frame is captured when T1 is released, detection runs on
that frame, and the result becomes visible to the rest of the stack only when the T2 job
completes on the chip model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cosim.conveyor import ON_BELT, LIFTED, Conveyor
from cosim.scene import Scene

ASPECT = 640 / 480


@dataclass
class Frame:
    """T1 output: what one camera saw at capture time."""

    camera: str
    t_capture: float
    tag: int
    objects: list[dict]               # uid, slot, pos (3,), yaw, kind, half_h
    human_xy: np.ndarray | None       # human proxy position if visible
    robot_points: np.ndarray          # link positions at capture (for the safety monitor)


@dataclass
class Detection:
    uid: int
    slot: int
    pos: np.ndarray
    yaw: float
    kind: str
    half_h: float


@dataclass
class DetectionSet:
    """T2 output."""

    camera: str
    t_capture: float
    tag: int
    detections: list[Detection] = field(default_factory=list)


@dataclass
class PerceptionConfig:
    pos_sigma_overhead: float = 0.003      # m, assumption (RGB-D at ~1.1 m)
    pos_sigma_wrist: float = 0.0015        # m, assumption (RGB-D at ~0.3 m)
    yaw_sigma: float = 0.05                # rad
    miss_prob: float = 0.03                # per object per frame
    max_range: float = 2.5


class Camera:
    """Frustum test against a MuJoCo camera frame."""

    def __init__(self, sc: Scene, name: str, cam_id: int) -> None:
        self.sc = sc
        self.name = name
        self.cam_id = cam_id
        fovy = np.deg2rad(sc.model.cam_fovy[cam_id])
        self.ty = np.tan(fovy / 2)
        self.tx = self.ty * ASPECT

    def visible(self, pts: np.ndarray) -> np.ndarray:
        d = self.sc.data
        c = d.cam_xpos[self.cam_id]
        R = d.cam_xmat[self.cam_id].reshape(3, 3)
        loc = (pts - c) @ R          # camera frame: looks along -z
        z = -loc[:, 2]
        ok = (z > 0.05) & (z < 2.5)
        zz = np.maximum(z, 1e-6)
        ok &= (np.abs(loc[:, 0] / zz) < self.tx) & (np.abs(loc[:, 1] / zz) < self.ty)
        return ok


class Perception:
    def __init__(self, sc: Scene, conveyor: Conveyor, cfg: PerceptionConfig, rng: np.random.Generator) -> None:
        self.sc = sc
        self.conv = conveyor
        self.cfg = cfg
        self.rng = rng
        self.cams = {"overhead": Camera(sc, "overhead", sc.cam_overhead),
                     "wrist": Camera(sc, "wrist", sc.cam_wrist)}
        self._tag = 0

    def capture(self, camera: str, t: float) -> Frame:
        """T1: snapshot of the scene as seen by one camera (inputs taken at release time)."""
        sc = self.sc
        d = sc.data
        st = self.conv.status
        live = np.flatnonzero((st == ON_BELT) | (st == LIFTED))
        objs: list[dict] = []
        if len(live):
            pts = d.xpos[sc.obj_body_ids[live]]
            vis = self.cams[camera].visible(pts)
            for i, v in zip(live, vis):
                if not v:
                    continue
                o = sc.objects[i]
                q = d.xquat[o.body_id]
                yaw = float(np.arctan2(2 * (q[0] * q[3] + q[1] * q[2]), 1 - 2 * (q[2] ** 2 + q[3] ** 2)))
                objs.append({"uid": int(self.conv.uid[i]), "slot": int(i), "pos": d.xpos[o.body_id].copy(),
                             "yaw": yaw, "kind": o.kind, "half_h": o.half_height})
        hpos = d.mocap_pos[sc.human_mocap]
        hp = np.array([[hpos[0], hpos[1], 1.0]])
        human = hpos[:2].copy() if self.cams[camera].visible(hp)[0] or camera == "overhead" else None
        robot_pts = np.vstack([d.xpos[sc.link_bodies], d.site_xpos[sc.tcp_site][None]])
        self._tag += 1
        return Frame(camera, t, self._tag, objs, human, robot_pts.copy())

    def detect(self, frame: Frame) -> DetectionSet:
        """T2: noisy poses for objects in the frame."""
        cfg = self.cfg
        sig = cfg.pos_sigma_wrist if frame.camera == "wrist" else cfg.pos_sigma_overhead
        out = DetectionSet(frame.camera, frame.t_capture, frame.tag)
        for o in frame.objects:
            if self.rng.random() < cfg.miss_prob:
                continue
            noise = self.rng.standard_normal(4)
            pos = o["pos"] + sig * noise[:3] * np.array([1.0, 1.0, 1.3])
            yaw = o["yaw"] + cfg.yaw_sigma * noise[3]
            out.detections.append(Detection(o["uid"], o["slot"], pos, yaw, o["kind"], o["half_h"]))
        return out
