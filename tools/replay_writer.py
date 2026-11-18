"""Write Glass Chip replay directories for the web viewer (glasschip-viz).

The on-disk format is documented in viz/REPLAY_FORMAT.md. In short, a replay is a
directory with a JSON manifest plus gzip-compressed little-endian binary chunks:

    manifest.json              metadata, floorplan, task table, chunk index, results summary
    robot/rNNNN.bin.gz         float32 body poses, [frames, bodies, 7] = px py pz qw qx qy qz
    events/eNNNN.bin.gz        48-byte chip event records, chunked by start time
    jobs.bin.gz                80-byte job records (release/start/finish/deadline/attribution)
    critpath.json.gz           critical paths for jobs that have one
    series.bin.gz              float32 sample series (power, voltage, freq, temperature, dram queues)
    lod.bin.gz                 per-millisecond aggregates (unit busy fraction, per-link bytes)

Callers pass plain Python dicts, lists and numpy arrays. Nothing here depends on the
simulator, so the co-sim, studies and the dev data generator all share one writer.

Typical use::

    from tools.replay_writer import ReplayWriter
    w = ReplayWriter(out_dir, name="S2 second camera / P0", soc=soc_yaml_dict,
                     tasks=tasks, duration_ps=20 * 10**12, scenario="S2_second_camera",
                     policy="P0_nominal", seed=7, synthetic=False,
                     provenance={"script": "studies/s02.py"})
    w.set_robot(fps=60, body_names=names, poses=poses)        # poses: [F, B, 7]
    w.add_events(trace_events)                                 # list of dicts or structured array
    w.add_jobs(completions)                                    # completion dicts from sim.run_until
    w.add_series("power.npu", t0_ps=0, rate_hz=1000, values=arr, unit="W", group="power")
    w.write()
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

FORMAT_NAME = "glasschip-replay"
FORMAT_VERSION = 1

EVENT_KINDS: tuple[str, ...] = (
    "job_release",
    "job_start",
    "job_finish",
    "job_miss",
    "op",
    "xfer",
    "dram_q",
    "freq",
    "preempt",
)
ATTRIBUTION_KEYS: tuple[str, ...] = (
    "compute",
    "runqueue",
    "engine_queue",
    "noc",
    "dram",
    "cache",
    "dma",
    "preemption",
    "dvfs",
    "middleware",
)
NONE16 = 0xFFFF

# 48-byte event record. Times are int64 picoseconds.
EVENT_DTYPE = np.dtype(
    [
        ("t_ps", "<i8"),
        ("dur_ps", "<i8"),
        ("kind", "u1"),
        ("block", "u1"),
        ("unit", "<u2"),
        ("task", "<u2"),
        ("src", "<u2"),
        ("dst", "<u2"),
        ("flags", "<u2"),
        ("job", "<u4"),
        ("op", "<u4"),
        ("bytes", "<f4"),
        ("value", "<f4"),
        ("aux", "<u4"),
    ]
)
assert EVENT_DTYPE.itemsize == 48

# 80-byte job record. Attribution values are float32 nanoseconds.
JOB_DTYPE = np.dtype(
    [
        ("job", "<u4"),
        ("task", "<u2"),
        ("flags", "<u2"),
        ("release_ps", "<i8"),
        ("start_ps", "<i8"),
        ("finish_ps", "<i8"),
        ("deadline_ps", "<i8"),
        ("attr_ns", "<f4", (len(ATTRIBUTION_KEYS),)),
    ]
)
assert JOB_DTYPE.itemsize == 80

FLAG_CRITICAL = 1
FLAG_MISSED = 2
JOB_MISSED = 1
JOB_SKIPPED = 2
JOB_HAS_CRITPATH = 4

LOD_BIN_PS = 1_000_000_000  # 1 ms


def _gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6, mtime=0)


def _node_index(node: Any, dims: Sequence[int]) -> int:
    """Map [x, y] mesh coordinates to a flat node index y * dimx + x (NONE16 if absent)."""
    if node is None:
        return NONE16
    if isinstance(node, (int, np.integer)):
        return int(node)
    x, y = int(node[0]), int(node[1])
    if x < 0 or y < 0:
        return NONE16
    return y * int(dims[0]) + x


def mesh_links(dims: Sequence[int]) -> list[tuple[int, int]]:
    """Directed links of a 2D mesh as (src_node, dst_node) flat indices, in a fixed order."""
    nx, ny = int(dims[0]), int(dims[1])
    links: list[tuple[int, int]] = []
    for y in range(ny):
        for x in range(nx):
            a = y * nx + x
            if x + 1 < nx:
                links.append((a, a + 1))
                links.append((a + 1, a))
            if y + 1 < ny:
                links.append((a, a + nx))
                links.append((a + nx, a))
    return links


def xy_route(src: int, dst: int, dims: Sequence[int]) -> list[tuple[int, int]]:
    """Directed links visited by dimension-ordered XY routing from src to dst."""
    nx = int(dims[0])
    sx, sy = src % nx, src // nx
    dx, dy = dst % nx, dst // nx
    hops: list[tuple[int, int]] = []
    x, y = sx, sy
    while x != dx:
        nxt = x + (1 if dx > x else -1)
        hops.append((y * nx + x, y * nx + nxt))
        x = nxt
    while y != dy:
        nyt = y + (1 if dy > y else -1)
        hops.append((y * nx + x, nyt * nx + x))
        y = nyt
    return hops


def soc_units(soc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Engine units (timeline tracks) derived from the SoC config, in block order."""
    count_key = {
        "cpu": "cores",
        "rtcpu": "cores",
        "dsp": "units",
        "npu": "tiles",
        "simt": "compute_units",
        "dma": "engines",
        "dram": "controllers",
    }
    units: list[dict[str, Any]] = []
    for bi, (bid, b) in enumerate(soc["blocks"].items()):
        key = count_key.get(str(b.get("kind")))
        if key is None:
            continue
        for u in range(int(b.get(key, 1))):
            units.append({"block": bid, "block_index": bi, "unit": u})
    return units


class ReplayWriter:
    """Accumulates one replay in memory and writes it to a directory.

    Args:
        out_dir: target directory (created; existing chunk files are overwritten).
        name: human-readable title shown in the viewer.
        soc: the parsed configs/soc/*.yaml dict (blocks, noc, power_domains, floorplan).
        tasks: list of task dicts: {id, name, period_us, deadline_us, criticality, color?, engines?}.
        duration_ps: replay length in picoseconds.
        scenario, policy: ids such as "S2_second_camera" and "P0_nominal".
        seed: master seed (replays meant to be compared should share it).
        synthetic: True for generated dev data; the viewer shows a badge.
        provenance: free-form dict, should include the producing script.
        scene: path or URL (relative to the replay dir) of the robot scene directory.
        event_chunk_ps: event chunk span (default 250 ms).
        robot_chunk_frames: frames per robot chunk (default 120).
    """

    def __init__(
        self,
        out_dir: str | Path,
        *,
        name: str,
        soc: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
        duration_ps: int,
        scenario: str,
        policy: str,
        seed: int,
        synthetic: bool,
        provenance: Mapping[str, Any],
        scene: str | None = None,
        event_chunk_ps: int = 250_000_000_000,
        robot_chunk_frames: int = 120,
        scenario_label: str | None = None,
        policy_label: str | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.name = name
        self.soc = dict(soc)
        self.tasks = [dict(t) for t in tasks]
        self.task_index = {str(t["id"]): i for i, t in enumerate(self.tasks)}
        self.duration_ps = int(duration_ps)
        self.scenario = scenario
        self.policy = policy
        self.scenario_label = scenario_label or scenario
        self.policy_label = policy_label or policy
        self.seed = int(seed)
        self.synthetic = bool(synthetic)
        self.provenance = dict(provenance)
        self.scene = scene
        self.event_chunk_ps = int(event_chunk_ps)
        self.robot_chunk_frames = int(robot_chunk_frames)
        self.block_ids = list(self.soc["blocks"].keys())
        self.block_index = {b: i for i, b in enumerate(self.block_ids)}
        self.dims = list(self.soc.get("noc", {}).get("dims", [4, 4]))
        self.strings: list[str] = []
        self._string_index: dict[str, int] = {}
        self._events: list[np.ndarray] = []
        self._jobs: list[np.ndarray] = []
        self._critpaths: dict[int, list[dict[str, Any]]] = {}
        self._series: list[dict[str, Any]] = []
        self._robot: dict[str, Any] | None = None
        self._robot_events: list[dict[str, Any]] = []
        self._results: dict[str, Any] | None = None
        self._extra: dict[str, Any] = {}

    # ---------------------------------------------------------------- inputs
    def _intern(self, s: str | None) -> int:
        if s is None or s == "":
            return 0xFFFFFFFF
        idx = self._string_index.get(s)
        if idx is None:
            idx = len(self.strings)
            self.strings.append(s)
            self._string_index[s] = idx
        return idx

    def set_robot(
        self,
        fps: float,
        body_names: Sequence[str],
        poses: np.ndarray,
        events: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        """Robot body poses per frame: poses shape [frames, bodies, 7] (pos xyz, quat wxyz).

        events: optional robot-side events, dicts with t_ps, kind (grasp_success, grasp_miss,
        oscillation, safety_stop, ...), and any extra JSON-serializable fields.
        """
        arr = np.asarray(poses, dtype="<f4")
        if arr.ndim != 3 or arr.shape[1] != len(body_names) or arr.shape[2] != 7:
            raise ValueError(f"poses must be [frames, {len(body_names)}, 7], got {arr.shape}")
        self._robot = {"fps": float(fps), "bodies": list(body_names), "poses": arr}
        if events:
            self._robot_events = [dict(e) for e in events]

    def add_events(self, events: Iterable[Mapping[str, Any]] | np.ndarray) -> None:
        """Add chip trace events (CONTRACTS.md trace event fields).

        Accepts a list of dicts or a numpy structured array with the same field names:
        t_ps, dur_ps, kind, block, unit, task_id, job_id, op_id, src_node, dst_node, bytes,
        value. Optional: critical (bool), missed (bool), aux (int, e.g. NPU layer index).
        """
        if isinstance(events, np.ndarray) and events.dtype.names:
            rows: Iterable[Mapping[str, Any]] = (
                {n: events[n][i] for n in events.dtype.names} for i in range(len(events))
            )
        else:
            rows = events  # type: ignore[assignment]
        buf: list[tuple[Any, ...]] = []
        for e in rows:
            kind = str(e["kind"])
            if kind not in EVENT_KINDS:
                raise ValueError(f"unknown event kind {kind}")
            block = e.get("block")
            bidx = self.block_index.get(str(block), 255) if block is not None else 255
            task = e.get("task_id")
            tidx = self.task_index.get(str(task), NONE16) if task is not None else NONE16
            flags = 0
            if e.get("critical"):
                flags |= FLAG_CRITICAL
            if e.get("missed"):
                flags |= FLAG_MISSED
            job = e.get("job_id")
            buf.append(
                (
                    int(e["t_ps"]),
                    int(e.get("dur_ps") or 0),
                    EVENT_KINDS.index(kind),
                    bidx,
                    int(e.get("unit") or 0) & 0xFFFF,
                    tidx,
                    _node_index(e.get("src_node"), self.dims),
                    _node_index(e.get("dst_node"), self.dims),
                    flags,
                    int(job) & 0xFFFFFFFF if job is not None and int(job) >= 0 else 0xFFFFFFFF,
                    self._intern(e.get("op_id")),
                    float(e.get("bytes") or 0.0),
                    float(e.get("value") or 0.0),
                    int(e.get("aux") or 0),
                )
            )
        if buf:
            self._events.append(np.array(buf, dtype=EVENT_DTYPE))

    def add_jobs(self, jobs: Iterable[Mapping[str, Any]]) -> None:
        """Add completed (or skipped) jobs, using the completion dict from sim.run_until."""
        buf: list[tuple[Any, ...]] = []
        for j in jobs:
            attr = j.get("attribution") or {}
            flags = 0
            if j.get("missed"):
                flags |= JOB_MISSED
            if j.get("skipped"):
                flags |= JOB_SKIPPED
            cp = j.get("critical_path")
            if cp:
                flags |= JOB_HAS_CRITPATH
                self._critpaths[int(j["job_id"])] = [dict(c) for c in cp]
            start = j.get("start_ps")
            finish = j.get("finish_ps")
            buf.append(
                (
                    int(j["job_id"]),
                    self.task_index[str(j["task_id"])],
                    flags,
                    int(j["release_ps"]),
                    int(start) if start is not None else -1,
                    int(finish) if finish is not None else -1,
                    int(j["deadline_ps"]),
                    [float(attr.get(k, 0.0)) / 1000.0 for k in ATTRIBUTION_KEYS],
                )
            )
        if buf:
            self._jobs.append(np.array(buf, dtype=JOB_DTYPE))

    def add_series(
        self,
        name: str,
        *,
        t0_ps: int,
        rate_hz: float,
        values: Sequence[float] | np.ndarray,
        unit: str,
        group: str,
        label: str | None = None,
    ) -> None:
        """Add a uniformly sampled float series (e.g. power.npu in W at 1 kHz).

        Naming convention used by the viewer: power.<domain> (W), volt.<domain> (V),
        freq.<domain> (Hz), temp.<domain> (C), dram_q.<controller> (requests),
        throttle.<domain> (0/1). Other names show up in the metrics drawer as generic series.
        """
        self._series.append(
            {
                "name": name,
                "label": label or name,
                "unit": unit,
                "group": group,
                "t0_ps": int(t0_ps),
                "rate_hz": float(rate_hz),
                "values": np.asarray(values, dtype="<f4"),
            }
        )

    def set_results(self, results: Mapping[str, Any]) -> None:
        """Summary metrics for the drawer. Must come from the sim or a results JSON."""
        self._results = dict(results)

    def set_extra(self, key: str, value: Any) -> None:
        """Attach extra JSON metadata (e.g. latency_tolerance band, scenario events)."""
        self._extra[key] = value

    # ---------------------------------------------------------------- helpers
    def _all_events(self) -> np.ndarray:
        if not self._events:
            return np.zeros(0, dtype=EVENT_DTYPE)
        ev = np.concatenate(self._events)
        order = np.argsort(ev["t_ps"], kind="stable")
        return ev[order]

    def _all_jobs(self) -> np.ndarray:
        if not self._jobs:
            return np.zeros(0, dtype=JOB_DTYPE)
        jb = np.concatenate(self._jobs)
        return jb[np.argsort(jb["release_ps"], kind="stable")]

    def _lod(self, ev: np.ndarray) -> tuple[dict[str, Any], bytes]:
        """Per-millisecond unit busy fractions (uint8 0..255) and directed-link bytes (float32)."""
        units = soc_units(self.soc)
        unit_key = {(u["block_index"], u["unit"]): i for i, u in enumerate(units)}
        links = mesh_links(self.dims)
        link_key = {l: i for i, l in enumerate(links)}
        nbins = max(1, math.ceil(self.duration_ps / LOD_BIN_PS))
        busy = np.zeros((nbins, len(units)), dtype=np.float64)
        lbytes = np.zeros((nbins, len(links)), dtype=np.float64)
        op_kind = EVENT_KINDS.index("op")
        xf_kind = EVENT_KINDS.index("xfer")
        ops = ev[ev["kind"] == op_kind]
        for e in ops:
            k = unit_key.get((int(e["block"]), int(e["unit"])))
            if k is None:
                continue
            t0 = int(e["t_ps"])
            t1 = t0 + max(int(e["dur_ps"]), 1)
            b0 = t0 // LOD_BIN_PS
            b1 = min((t1 - 1) // LOD_BIN_PS, nbins - 1)
            for b in range(b0, b1 + 1):
                lo = max(t0, b * LOD_BIN_PS)
                hi = min(t1, (b + 1) * LOD_BIN_PS)
                if hi > lo:
                    busy[b, k] += (hi - lo) / LOD_BIN_PS
        xf = ev[ev["kind"] == xf_kind]
        route_cache: dict[tuple[int, int], list[int]] = {}
        for e in xf:
            s, d = int(e["src"]), int(e["dst"])
            if s == NONE16 or d == NONE16 or s == d:
                continue
            r = route_cache.get((s, d))
            if r is None:
                r = [link_key[h] for h in xy_route(s, d, self.dims)]
                route_cache[(s, d)] = r
            b = min(int(e["t_ps"]) // LOD_BIN_PS, nbins - 1)
            for li in r:
                lbytes[b, li] += float(e["bytes"])
        busy_u8 = np.clip(np.round(busy * 255.0), 0, 255).astype(np.uint8)
        meta = {
            "file": "lod.bin.gz",
            "bin_ps": LOD_BIN_PS,
            "bins": nbins,
            "units": units,
            "links": [list(l) for l in links],
            "busy_offset": 0,
            "busy_bytes": int(busy_u8.nbytes),
            "link_offset": int(busy_u8.nbytes) + ((-busy_u8.nbytes) % 4),
        }
        pad = b"\x00" * ((-busy_u8.nbytes) % 4)
        return meta, busy_u8.tobytes() + pad + lbytes.astype("<f4").tobytes()

    def _derive_results(self, jobs: np.ndarray) -> dict[str, Any]:
        """Summary computed from the replay itself (used when set_results was not called)."""
        per_task: dict[str, Any] = {}
        for t in self.tasks:
            ti = self.task_index[str(t["id"])]
            jt = jobs[jobs["task"] == ti]
            done = jt[jt["finish_ps"] >= 0]
            resp = (done["finish_ps"] - done["release_ps"]).astype(np.float64) / 1e9  # ms
            per_task[str(t["id"])] = {
                "jobs": int(len(jt)),
                "misses": int(np.count_nonzero(jt["flags"] & JOB_MISSED)),
                "miss_rate": float(np.count_nonzero(jt["flags"] & JOB_MISSED) / len(jt)) if len(jt) else 0.0,
                "p50_ms": float(np.percentile(resp, 50)) if len(resp) else None,
                "p95_ms": float(np.percentile(resp, 95)) if len(resp) else None,
                "p99_ms": float(np.percentile(resp, 99)) if len(resp) else None,
                "max_ms": float(resp.max()) if len(resp) else None,
            }
        out: dict[str, Any] = {"source": "derived by tools/replay_writer.py from this replay", "tasks": per_task}
        power = [s for s in self._series if s["name"].startswith("power.")]
        if power:
            n = min(len(s["values"]) for s in power)
            total = np.sum([s["values"][:n] for s in power], axis=0)
            rate = power[0]["rate_hz"]
            energy_j = float(total.sum() / rate)
            secs = n / rate
            out["avg_power_w"] = float(total.mean())
            out["peak_power_w"] = float(total.max())
            out["energy_j"] = energy_j
            out["energy_per_robot_hour_wh"] = float(energy_j / secs) if secs > 0 else None  # W * 1 h = Wh
        temps = [s for s in self._series if s["name"].startswith("temp.")]
        if temps:
            out["peak_temp_c"] = float(max(float(s["values"].max()) for s in temps))
        return out

    # ---------------------------------------------------------------- output
    def write(self) -> Path:
        """Write the replay directory and return its path."""
        out = self.out_dir
        (out / "events").mkdir(parents=True, exist_ok=True)
        (out / "robot").mkdir(parents=True, exist_ok=True)
        for old in list((out / "events").glob("*.bin.gz")) + list((out / "robot").glob("*.bin.gz")):
            old.unlink()

        ev = self._all_events()
        chunks: list[dict[str, Any]] = []
        nchunks = max(1, math.ceil(self.duration_ps / self.event_chunk_ps))
        idx = np.minimum(ev["t_ps"] // self.event_chunk_ps, nchunks - 1) if len(ev) else np.zeros(0, int)
        for c in range(nchunks):
            part = ev[idx == c]
            fname = f"events/e{c:04d}.bin.gz"
            (out / fname).write_bytes(_gz(part.tobytes()))
            end = int((part["t_ps"] + part["dur_ps"]).max()) if len(part) else (c + 1) * self.event_chunk_ps
            chunks.append(
                {
                    "t0_ps": c * self.event_chunk_ps,
                    "t1_ps": min((c + 1) * self.event_chunk_ps, self.duration_ps),
                    "max_end_ps": max(end, (c + 1) * self.event_chunk_ps),
                    "file": fname,
                    "count": int(len(part)),
                }
            )

        jobs = self._all_jobs()
        (out / "jobs.bin.gz").write_bytes(_gz(jobs.tobytes()))
        (out / "critpath.json.gz").write_bytes(
            _gz(json.dumps({str(k): v for k, v in self._critpaths.items()}, separators=(",", ":")).encode())
        )

        series_meta: list[dict[str, Any]] = []
        blob = bytearray()
        for s in self._series:
            series_meta.append(
                {k: s[k] for k in ("name", "label", "unit", "group", "t0_ps", "rate_hz")}
                | {"offset": len(blob), "count": int(len(s["values"]))}
            )
            blob += s["values"].tobytes()
        (out / "series.bin.gz").write_bytes(_gz(bytes(blob)))

        lod_meta, lod_blob = self._lod(ev)
        (out / "lod.bin.gz").write_bytes(_gz(lod_blob))

        robot_meta: dict[str, Any] | None = None
        if self._robot is not None:
            poses: np.ndarray = self._robot["poses"]
            files = []
            for i, f0 in enumerate(range(0, poses.shape[0], self.robot_chunk_frames)):
                fname = f"robot/r{i:04d}.bin.gz"
                (out / fname).write_bytes(_gz(np.ascontiguousarray(poses[f0 : f0 + self.robot_chunk_frames]).tobytes()))
                files.append(fname)
            robot_meta = {
                "fps": self._robot["fps"],
                "frames": int(poses.shape[0]),
                "bodies": self._robot["bodies"],
                "chunk_frames": self.robot_chunk_frames,
                "chunks": files,
                "events": self._robot_events,
                "scene": self.scene,
            }

        domains = self.soc.get("power_domains", {})
        manifest = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "name": self.name,
            "scenario": self.scenario,
            "scenario_label": self.scenario_label,
            "policy": self.policy,
            "policy_label": self.policy_label,
            "seed": self.seed,
            "synthetic": self.synthetic,
            "duration_ps": self.duration_ps,
            "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "provenance": self.provenance,
            "soc": {
                "name": self.soc.get("name", "SoC"),
                "process": self.soc.get("process", {}),
                "noc": self.soc.get("noc", {}),
                "blocks": [
                    {"id": bid, **{k: v for k, v in b.items() if not isinstance(v, dict) or k == "floorplan"}}
                    for bid, b in self.soc["blocks"].items()
                ],
                "power_domains": {
                    k: v for k, v in domains.items() if isinstance(v, dict)
                },
                "dvfs_transition_us": (domains.get("dvfs_transition_us") if isinstance(domains, dict) else None),
            },
            "tasks": self.tasks,
            "events": {
                "record_bytes": EVENT_DTYPE.itemsize,
                "kinds": list(EVENT_KINDS),
                "chunk_ps": self.event_chunk_ps,
                "chunks": chunks,
                "strings": self.strings,
                "count": int(len(ev)),
            },
            "jobs": {
                "file": "jobs.bin.gz",
                "record_bytes": JOB_DTYPE.itemsize,
                "count": int(len(jobs)),
                "attribution_keys": list(ATTRIBUTION_KEYS),
                "critpath_file": "critpath.json.gz",
            },
            "series": {"file": "series.bin.gz", "items": series_meta},
            "lod": lod_meta,
            "robot": robot_meta,
            "results": self._results if self._results is not None else self._derive_results(jobs),
        }
        manifest.update(self._extra)
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=_json_default))
        return out


def _json_default(o: Any) -> Any:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o)}")


def stage_scene(src_dir: str | Path, dst_dir: str | Path) -> Path:
    """Copy a co-sim scene export (cosim/SCENE_FORMAT.md) into the static site, gzipping the binary.

    Writes dst_dir/scene.json (with bin_file pointing at the gzipped copy) and dst_dir/scene.bin.gz.
    Point ReplayWriter(scene=...) at dst_dir relative to the replay directory.
    """
    src, dst = Path(src_dir), Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    meta = json.loads((src / "scene.json").read_text())
    bin_name = meta.get("bin_file", "scene.bin")
    (dst / (bin_name + ".gz")).write_bytes(_gz((src / bin_name).read_bytes()))
    meta["bin_file"] = bin_name + ".gz"
    (dst / "scene.json").write_text(json.dumps(meta, separators=(",", ":")))
    return dst


def update_index(index_path: str | Path, entry: Mapping[str, Any]) -> None:
    """Add or replace one replay entry in the site index (viz/public/replays/index.json).

    entry: {id, path (relative to the index file), scenario, scenario_label, policy,
    policy_label, seed, synthetic, default?}.
    """
    p = Path(index_path)
    data: dict[str, Any] = {"format": "glasschip-replay-index", "version": 1, "replays": []}
    if p.exists():
        data = json.loads(p.read_text())
    reps = [r for r in data.get("replays", []) if r.get("id") != entry["id"]]
    reps.append(dict(entry))
    data["replays"] = reps
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
