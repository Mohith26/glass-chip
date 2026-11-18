# Co-sim scene export format

`cosim/export_scene.py` writes the compiled MuJoCo conveyor scene for the web renderer:

```
.venv/bin/python -m cosim.export_scene --out data/cosim/scene
```

Output directory:

| File | Content |
|---|---|
| `scene.json` | manifest (bodies, geoms, meshes with byte ranges, cameras, layout) |
| `scene.bin` | raw little-endian arrays referenced by the manifest, each 4-byte aligned |

Units: metres, radians. Quaternions are `(w, x, y, z)`. World frame is z up, robot base at the origin, conveyor running along +y at x = 0.56.

## Manifest fields

- `bodies[]`: `id`, `name` (`world` for body 0), `parent`, `mocap` (true for slats and the human proxy), and the pose at reset (`xpos`, `xquat`). Body ids match the order of poses in co-sim recordings.
- `geoms[]`: one entry per drawable geom.
  - `body_id`, `body`: the body the geom is attached to.
  - `type`: `plane | sphere | capsule | ellipsoid | cylinder | box | mesh`.
  - `size`: MuJoCo half sizes. sphere `[r]`, capsule and cylinder `[r, half_length]` along local z, box `[hx, hy, hz]`, plane `[hx, hy, spacing]`.
  - `pos`, `quat`: geom pose relative to its body.
  - `rgba`: material colour if the geom has a material, else the geom colour.
  - `group`: MuJoCo geom group. Panda visual meshes are group 2; collision meshes (group 3) are left out unless `--all-groups`.
  - `role`: `robot | object | slat | conveyor | bin | human | floor | static`.
  - `mesh`: index into `meshes[]` (mesh geoms only).
- `meshes[]`: `name`, `n_vert`, `n_face`, and three array descriptors `vertices` (float32 `[n_vert, 3]`, in the mesh frame, which the geom `pos`/`quat` place on the body), `normals` (float32 `[n_vert, 3]`, area-weighted vertex normals computed at export), `faces` (uint32 `[n_face, 3]`, counter-clockwise). Each descriptor is `{offset, bytes, dtype, shape}` into `scene.bin`.
- `cameras[]`: `cam_overhead` and `cam_wrist` with local `pos`/`quat` on their body, `fovy_deg`, and world pose at reset. MuJoCo cameras look along their local -z.
- `layout`: conveyor `{x, width, top_z, y0, y1}`, bin `{x, y, half, wall_h}`, pool size and slat count.

## Placing geometry at a frame

World pose of a geom = body pose at that frame composed with the geom's local `pos`/`quat`:

```
p_world = body_xpos + rotate(body_xquat, geom.pos)
q_world = body_xquat * geom.quat
```

Body poses per frame come from a co-sim recording (`<name>.npz`: `xpos [F, nbody, 3]`, `xquat [F, nbody, 4]`; body order = `bodies[]`), or from `tools/replay_writer.py` robot chunks (`[F, B, 7]`, body names listed in the replay manifest).

Unused pool objects are parked on the floor behind the robot at x = -1.6. The recording's `obj_status` array (`0 parked, 1 on belt, 2 lifted, 3 in bin, 4 dropped`) lets a renderer hide parked objects.
