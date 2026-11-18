"""Dump the compiled conveyor scene geometry for a web renderer.

Writes two files (format documented in cosim/SCENE_FORMAT.md):

    <out>/scene.json    manifest: bodies, geoms, meshes (with byte ranges), cameras, layout
    <out>/scene.bin     little-endian float32 vertex/normal arrays and uint32 face arrays

Usage:
    .venv/bin/python -m cosim.export_scene [--out data/cosim/scene] [--all-groups]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from cosim.envs import build_scene as layout
from cosim.scene import SCENE_XML, load_scene

ROOT = Path(__file__).resolve().parents[1]
GEOM_TYPES = {int(mujoco.mjtGeom.mjGEOM_PLANE): "plane", int(mujoco.mjtGeom.mjGEOM_SPHERE): "sphere",
              int(mujoco.mjtGeom.mjGEOM_CAPSULE): "capsule", int(mujoco.mjtGeom.mjGEOM_ELLIPSOID): "ellipsoid",
              int(mujoco.mjtGeom.mjGEOM_CYLINDER): "cylinder", int(mujoco.mjtGeom.mjGEOM_BOX): "box",
              int(mujoco.mjtGeom.mjGEOM_MESH): "mesh"}


def _role(body: str, geom: str) -> str:
    if body.startswith("obj_"):
        return "object"
    if body.startswith("slat_"):
        return "slat"
    if body == "human":
        return "human"
    if geom.startswith("bin"):
        return "bin"
    if geom in ("belt", "conveyor_frame") or geom.startswith(("rail", "leg")):
        return "conveyor"
    if geom == "floor":
        return "floor"
    if body in ("world", ""):
        return "static"
    return "robot"


def _vertex_normals(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Area-weighted per-vertex normals."""
    n = np.zeros_like(v)
    tri = v[f]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    for k in range(3):
        np.add.at(n, f[:, k], fn)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.maximum(ln, 1e-12)


def export(out_dir: Path, all_groups: bool = False) -> dict[str, Any]:
    sc = load_scene()
    m, d = sc.model, sc.data
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = bytearray()

    def put(arr: np.ndarray) -> dict[str, Any]:
        nonlocal blob
        a = np.ascontiguousarray(arr)
        off = len(blob)
        blob += a.tobytes()
        blob += b"\x00" * ((-len(blob)) % 4)
        return {"offset": off, "bytes": a.nbytes, "dtype": a.dtype.str, "shape": list(a.shape)}

    bodies = []
    for b in range(m.nbody):
        bodies.append({"id": b, "name": m.body(b).name or "world", "parent": int(m.body_parentid[b]),
                       "mocap": bool(m.body_mocapid[b] >= 0),
                       "xpos": d.xpos[b].tolist(), "xquat": d.xquat[b].tolist()})
    meshes: list[dict[str, Any]] = []
    mesh_index: dict[int, int] = {}
    geoms = []
    for g in range(m.ngeom):
        grp = int(m.geom_group[g])
        if not all_groups and grp > 2:
            continue
        t = GEOM_TYPES.get(int(m.geom_type[g]))
        if t is None:
            continue
        bid = int(m.geom_bodyid[g])
        bname = m.body(bid).name or "world"
        gname = m.geom(g).name
        mat = int(m.geom_matid[g])
        rgba = (m.mat_rgba[mat] if mat >= 0 else m.geom_rgba[g]).astype(float).tolist()
        entry: dict[str, Any] = {"id": g, "name": gname, "body_id": bid, "body": bname, "type": t,
                                 "size": m.geom_size[g].tolist(), "pos": m.geom_pos[g].tolist(),
                                 "quat": m.geom_quat[g].tolist(), "rgba": rgba, "group": grp,
                                 "role": _role(bname, gname)}
        if t == "mesh":
            mid = int(m.geom_dataid[g])
            if mid not in mesh_index:
                va, vn = int(m.mesh_vertadr[mid]), int(m.mesh_vertnum[mid])
                fa, fn = int(m.mesh_faceadr[mid]), int(m.mesh_facenum[mid])
                v = m.mesh_vert[va:va + vn].astype("<f4")
                f = m.mesh_face[fa:fa + fn].astype("<u4")
                nrm = _vertex_normals(v.astype(np.float64), f.astype(np.int64)).astype("<f4")
                mesh_index[mid] = len(meshes)
                meshes.append({"id": len(meshes), "mujoco_id": mid, "name": m.mesh(mid).name,
                               "n_vert": vn, "n_face": fn,
                               "vertices": put(v), "normals": put(nrm), "faces": put(f)})
            entry["mesh"] = mesh_index[mid]
        geoms.append(entry)
    cams = []
    for c in range(m.ncam):
        cams.append({"name": m.camera(c).name, "body_id": int(m.cam_bodyid[c]), "fovy_deg": float(m.cam_fovy[c]),
                     "pos": m.cam_pos[c].tolist(), "quat": m.cam_quat[c].tolist(),
                     "world_pos": d.cam_xpos[c].tolist(), "world_xmat": d.cam_xmat[c].tolist()})
    data = bytes(blob)
    manifest = {
        "format": "glasschip-scene", "version": 1,
        "units": "metres, quaternions (w, x, y, z), z up",
        "source_xml": str(Path(SCENE_XML).relative_to(ROOT)),
        "bin_file": "scene.bin", "bin_bytes": len(data), "bin_sha256": hashlib.sha256(data).hexdigest(),
        "generated": _dt.date.today().isoformat(),
        "groups_included": "all" if all_groups else [0, 1, 2],
        "layout": {"conveyor": layout.CONVEYOR, "bin": {**layout.BIN, "half": list(layout.BIN["half"])},
                   "n_objects": layout.N_OBJECTS, "n_slats": layout.N_SLATS},
        "bodies": bodies, "geoms": geoms, "meshes": meshes, "cameras": cams,
    }
    (out_dir / "scene.bin").write_bytes(data)
    (out_dir / "scene.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "data/cosim/scene"))
    ap.add_argument("--all-groups", action="store_true", help="also export collision geoms (group 3)")
    a = ap.parse_args()
    man = export(Path(a.out), a.all_groups)
    print(f"wrote {a.out}: {len(man['geoms'])} geoms, {len(man['meshes'])} meshes, {man['bin_bytes'] / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
