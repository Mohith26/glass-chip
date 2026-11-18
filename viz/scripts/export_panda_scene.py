"""Export the Franka Panda (MuJoCo Menagerie) visual geometry plus conveyor props for the viewer.

This is the viewer-side fallback until cosim/export_scene.py exists. It reads the MjModel
mesh arrays (no rendering needed) and writes a small "gcscene" directory:

    scene.json      bodies, meshes, geoms (body-relative pose, type, size, rgba)
    meshes.bin.gz   per mesh: uint16 quantized positions (bbox-relative), then uint32 indices

Usage: .venv/bin/python viz/scripts/export_panda_scene.py [out_dir]
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PANDA_XML = ROOT / "third_party/mujoco_menagerie/franka_emika_panda/scene.xml"
DEFAULT_OUT = ROOT / "viz/public/scenes/panda_conveyor"

GEOM_TYPES = {0: "plane", 2: "sphere", 3: "capsule", 5: "cylinder", 6: "box", 7: "mesh"}

# Props in world coordinates. The conveyor runs along +y in front of the robot.
CONVEYOR = {"x": 0.56, "width": 0.34, "top_z": 0.16, "y0": -0.95, "y1": 0.95}
BIN = {"x": 0.08, "y": -0.56, "size": [0.13, 0.11, 0.06]}
OBJECT_KINDS = [
    {"type": "box", "size": [0.025, 0.025, 0.025], "rgba": [0.93, 0.36, 0.30, 1]},
    {"type": "cylinder", "size": [0.022, 0.035, 0], "rgba": [0.98, 0.76, 0.27, 1]},
    {"type": "box", "size": [0.035, 0.02, 0.02], "rgba": [0.36, 0.72, 0.95, 1]},
    {"type": "sphere", "size": [0.026, 0, 0], "rgba": [0.55, 0.85, 0.52, 1]},
]


def prop_geoms(n_objects: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Static conveyor/bin geoms (attached to 'world') and one body per conveyor object."""
    c = CONVEYOR
    length = c["y1"] - c["y0"]
    cy = (c["y0"] + c["y1"]) / 2
    q = [1, 0, 0, 0]
    geoms: list[dict[str, Any]] = [
        {"body": "world", "type": "box", "size": [c["width"] / 2, length / 2, 0.012],
         "pos": [c["x"], cy, c["top_z"] - 0.012], "quat": q, "rgba": [0.16, 0.17, 0.19, 1], "role": "belt"},
        {"body": "world", "type": "box", "size": [c["width"] / 2 + 0.02, length / 2 + 0.01, 0.05],
         "pos": [c["x"], cy, c["top_z"] - 0.075], "quat": q, "rgba": [0.23, 0.26, 0.31, 1], "role": "frame"},
    ]
    for sx in (-1, 1):
        geoms.append({"body": "world", "type": "box", "size": [0.008, length / 2 + 0.01, 0.018],
                      "pos": [c["x"] + sx * (c["width"] / 2 + 0.012), cy, c["top_z"] + 0.004], "quat": q,
                      "rgba": [0.55, 0.59, 0.66, 1], "role": "rail"})
    for yy in (c["y0"] + 0.08, c["y1"] - 0.08):
        for sx in (-1, 1):
            geoms.append({"body": "world", "type": "box", "size": [0.02, 0.02, (c["top_z"] - 0.12) / 2],
                          "pos": [c["x"] + sx * (c["width"] / 2 - 0.01), yy, (c["top_z"] - 0.12) / 2], "quat": q,
                          "rgba": [0.30, 0.33, 0.38, 1], "role": "leg"})
    b = BIN
    sx_, sy_, sz_ = b["size"]
    t = 0.006
    for dx, dy, hx, hy in ((sx_, 0, t, sy_), (-sx_, 0, t, sy_), (0, sy_, sx_, t), (0, -sy_, sx_, t)):
        geoms.append({"body": "world", "type": "box", "size": [hx, hy, sz_],
                      "pos": [b["x"] + dx, b["y"] + dy, sz_], "quat": q, "rgba": [0.20, 0.36, 0.62, 1], "role": "bin"})
    geoms.append({"body": "world", "type": "box", "size": [sx_, sy_, t], "pos": [b["x"], b["y"], t], "quat": q,
                  "rgba": [0.16, 0.28, 0.50, 1], "role": "bin"})
    bodies = []
    for i in range(n_objects):
        k = OBJECT_KINDS[i % len(OBJECT_KINDS)]
        name = f"obj_{i}"
        bodies.append(name)
        geoms.append({"body": name, "type": k["type"], "size": k["size"], "pos": [0, 0, 0], "quat": q,
                      "rgba": k["rgba"], "role": "object"})
    bodies.append("human")
    geoms.append({"body": "human", "type": "capsule", "size": [0.16, 0.6, 0], "pos": [0, 0, 0.85], "quat": q,
                  "rgba": [0.85, 0.88, 0.92, 0.35], "role": "human"})
    return geoms, bodies


def export_scene(out_dir: Path = DEFAULT_OUT, n_objects: int = 10) -> dict[str, Any]:
    """Write scene.json + meshes.bin.gz and return the scene dict."""
    m = mujoco.MjModel.from_xml_path(str(PANDA_XML))
    out_dir.mkdir(parents=True, exist_ok=True)
    used_meshes: dict[int, int] = {}
    meshes: list[dict[str, Any]] = []
    blob = bytearray()
    geoms: list[dict[str, Any]] = []
    for g in range(m.ngeom):
        if int(m.geom_group[g]) != 2:  # visual group only (collision geoms are group 3)
            continue
        gtype = GEOM_TYPES.get(int(m.geom_type[g]))
        if gtype is None:
            continue
        mat = int(m.geom_matid[g])
        rgba = (m.mat_rgba[mat] if mat >= 0 else m.geom_rgba[g]).tolist()
        entry: dict[str, Any] = {
            "body": m.body(int(m.geom_bodyid[g])).name,
            "type": gtype,
            "size": m.geom_size[g].tolist(),
            "pos": m.geom_pos[g].tolist(),
            "quat": m.geom_quat[g].tolist(),
            "rgba": rgba,
            "role": "robot",
        }
        if gtype == "mesh":
            mid = int(m.geom_dataid[g])
            if mid not in used_meshes:
                va, vn = int(m.mesh_vertadr[mid]), int(m.mesh_vertnum[mid])
                fa, fn = int(m.mesh_faceadr[mid]), int(m.mesh_facenum[mid])
                v = m.mesh_vert[va : va + vn].astype(np.float64)
                f = m.mesh_face[fa : fa + fn].astype("<u4")
                lo, hi = v.min(axis=0), v.max(axis=0)
                span = np.maximum(hi - lo, 1e-9)
                qv = np.round((v - lo) / span * 65535).astype("<u2")
                pos_off = len(blob)
                blob += qv.tobytes()
                blob += b"\x00" * ((-len(blob)) % 4)
                idx_off = len(blob)
                blob += f.tobytes()
                used_meshes[mid] = len(meshes)
                meshes.append({"name": m.mesh(mid).name, "vcount": vn, "icount": int(fn * 3),
                               "pos_offset": pos_off, "idx_offset": idx_off,
                               "bbox_min": lo.tolist(), "bbox_max": hi.tolist()})
            entry["mesh"] = used_meshes[mid]
        geoms.append(entry)
    props, prop_bodies = prop_geoms(n_objects)
    geoms.extend(props)
    robot_bodies = [m.body(i).name for i in range(1, m.nbody)]
    scene = {
        "format": "gcscene",
        "version": 1,
        "provenance": {
            "script": "viz/scripts/export_panda_scene.py",
            "source": "MuJoCo Menagerie franka_emika_panda/scene.xml (visual group 2 meshes)",
            "mujoco": mujoco.__version__,
        },
        "up": "z",
        "bodies": ["world"] + robot_bodies + prop_bodies,
        "meshes": meshes,
        "mesh_file": "meshes.bin.gz",
        "geoms": geoms,
        "conveyor": CONVEYOR,
        "bin": BIN,
    }
    (out_dir / "meshes.bin.gz").write_bytes(gzip.compress(bytes(blob), 6, mtime=0))
    (out_dir / "scene.json").write_text(json.dumps(scene, separators=(",", ":")))
    return scene


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    s = export_scene(target)
    size = (target / "meshes.bin.gz").stat().st_size
    print(f"wrote {target}: {len(s['meshes'])} meshes, {len(s['geoms'])} geoms, meshes.bin.gz {size/1e6:.2f} MB")
