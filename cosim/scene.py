"""Loads the conveyor scene and exposes the ids and index slices the co-sim needs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import mujoco
import numpy as np

from cosim.envs import build_scene as layout

SCENE_XML = Path(__file__).resolve().parent / "envs" / "conveyor_scene.xml"

# Home posture: elbow up, hand pointing down, above the upstream half of the belt.
HOME_Q = np.array([0.0, -0.35, 0.0, -2.30, 0.0, 1.95, 0.785398], dtype=float)
FINGER_OPEN = 0.04


@dataclass
class ObjectSlot:
    """One pooled conveyor object."""

    index: int
    body_id: int
    geom_id: int
    qpos_adr: int
    dof_adr: int
    kind: str
    size: np.ndarray
    half_height: float
    mass: float


@dataclass
class Scene:
    """Compiled model plus handles. One Scene per simulation run."""

    model: mujoco.MjModel
    data: mujoco.MjData
    arm_qpos: np.ndarray            # qpos indices of joint1..7
    arm_dof: np.ndarray             # dof indices of joint1..7
    finger_qpos: np.ndarray         # qpos indices of the two finger slides
    tcp_site: int
    hand_body: int
    link_bodies: np.ndarray         # bodies used for human-robot distance
    objects: list[ObjectSlot]
    obj_body_ids: np.ndarray
    obj_half_h: np.ndarray
    obj_mass: np.ndarray
    obj_qpos_adr: np.ndarray
    obj_dof_adr: np.ndarray
    slat_mocap: np.ndarray
    human_mocap: int
    cam_overhead: int
    cam_wrist: int
    belt_geom: int
    robot_geoms: set[int] = field(default_factory=set)

    @property
    def conveyor(self) -> dict:
        return layout.CONVEYOR

    @property
    def bin(self) -> dict:
        return layout.BIN

    def body_names(self) -> list[str]:
        return [self.model.body(i).name or "world" for i in range(self.model.nbody)]


def load_scene(xml_path: Path | str = SCENE_XML) -> Scene:
    """Compile the MJCF, put the arm at HOME_Q and park all objects."""
    m = mujoco.MjModel.from_xml_path(str(xml_path))
    d = mujoco.MjData(m)
    jid = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, f"joint{i}") for i in range(1, 8)]
    arm_qpos = np.array([m.jnt_qposadr[j] for j in jid])
    arm_dof = np.array([m.jnt_dofadr[j] for j in jid])
    fj = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, n) for n in ("finger_joint1", "finger_joint2")]
    finger_qpos = np.array([m.jnt_qposadr[j] for j in fj])
    objects: list[ObjectSlot] = []
    for i in range(layout.N_OBJECTS):
        b = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f"obj_{i}")
        g = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, f"obj_{i}_geom")
        j = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, f"obj_{i}_free")
        kind, size, mass, _ = layout.object_kind(i)
        objects.append(ObjectSlot(i, b, g, int(m.jnt_qposadr[j]), int(m.jnt_dofadr[j]), kind,
                                  np.array(size, dtype=float), layout.object_half_height(kind, size), mass))
    slat_mocap = np.array([m.body_mocapid[mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f"slat_{k}")]
                           for k in range(layout.N_SLATS)])
    human_body = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "human")
    link_names = ["link2", "link3", "link4", "link5", "link6", "link7", "hand"]
    links = np.array([mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in link_names])
    robot_bodies = {mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in
                    ["link0", "link1", *link_names, "left_finger", "right_finger"]}
    robot_geoms = {g for g in range(m.ngeom) if int(m.geom_bodyid[g]) in robot_bodies}
    sc = Scene(
        model=m, data=d, arm_qpos=arm_qpos, arm_dof=arm_dof, finger_qpos=finger_qpos,
        tcp_site=mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, "tcp"),
        hand_body=mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "hand"),
        link_bodies=links, objects=objects,
        obj_body_ids=np.array([o.body_id for o in objects]),
        obj_half_h=np.array([o.half_height for o in objects]),
        obj_mass=np.array([o.mass for o in objects]),
        obj_qpos_adr=np.array([o.qpos_adr for o in objects]),
        obj_dof_adr=np.array([o.dof_adr for o in objects]),
        slat_mocap=slat_mocap, human_mocap=int(m.body_mocapid[human_body]),
        cam_overhead=mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, "cam_overhead"),
        cam_wrist=mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, "cam_wrist"),
        belt_geom=mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "belt"),
        robot_geoms=robot_geoms,
    )
    reset_robot(sc)
    return sc


def reset_robot(sc: Scene) -> None:
    """Arm at HOME_Q with the gripper open; all objects parked."""
    d = sc.data
    mujoco.mj_resetData(sc.model, d)
    d.qpos[sc.arm_qpos] = HOME_Q
    d.qpos[sc.finger_qpos] = FINGER_OPEN
    d.ctrl[7] = 255.0
    for o in sc.objects:
        park_object(sc, o.index)
    mujoco.mj_forward(sc.model, d)


def park_object(sc: Scene, i: int) -> None:
    o = sc.objects[i]
    d = sc.data
    d.qpos[o.qpos_adr:o.qpos_adr + 3] = layout.parked_pos(i)
    d.qpos[o.qpos_adr + 3:o.qpos_adr + 7] = (1, 0, 0, 0)
    d.qvel[o.dof_adr:o.dof_adr + 6] = 0.0


def place_object(sc: Scene, i: int, pos: np.ndarray, yaw: float) -> None:
    o = sc.objects[i]
    d = sc.data
    d.qpos[o.qpos_adr:o.qpos_adr + 3] = pos
    d.qpos[o.qpos_adr + 3:o.qpos_adr + 7] = (np.cos(yaw / 2), 0, 0, np.sin(yaw / 2))
    d.qvel[o.dof_adr:o.dof_adr + 6] = 0.0
