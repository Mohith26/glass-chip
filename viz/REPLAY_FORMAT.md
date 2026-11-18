# Glass Chip replay format (version 1)

A replay is a directory that a static web host can serve as is. The writer is `tools/replay_writer.py`; the reader is `viz/src/replay/` (decoding runs in a Web Worker). All binary data is little endian and every `*.gz` file is plain gzip, decoded in the browser with `DecompressionStream('gzip')`. If a host transparently decompresses `.gz` files, the reader detects the missing gzip magic bytes and uses the data directly.

```
<replay>/
  manifest.json          metadata, floorplan, tasks, chunk index, results summary
  events/e0000.bin.gz    chip events, 48-byte records, one file per time chunk (default 250 ms)
  jobs.bin.gz            one 80-byte record per job (whole run)
  critpath.json.gz       {job_id: [critical path ops]} for jobs that have one
  series.bin.gz          float32 sample series, concatenated
  lod.bin.gz             per-millisecond aggregates for zoomed-out views
  robot/r0000.bin.gz     float32 body poses, [frames, bodies, 7], chunked by frame
```

Times are int64 picoseconds everywhere (`*_ps`). The reader converts them to float64, which is exact below 2^53 ps (about 2.5 hours).

## manifest.json

| Field | Meaning |
|---|---|
| `format`, `version` | `"glasschip-replay"`, `1` |
| `name`, `scenario`, `scenario_label`, `policy`, `policy_label`, `seed` | identity; replays meant to be compared share `seed` |
| `synthetic` | `true` for generated dev data. The viewer shows a "Synthetic dev data" badge |
| `duration_ps`, `created`, `provenance` | provenance must name the producing script |
| `soc` | `name`, `process`, `noc` (`dims`, link width, ...), `blocks[]` (id, kind, unit counts, `nodes`, `power_domain`, `floorplan {x,y,w,h}` in 0..1 die coordinates), `power_domains {id: {v_min, v_max, opp_volts}}` |
| `tasks[]` | `{id, name, period_us, deadline_us, criticality, color}`. The order defines task indices used in binary records |
| `events` | `record_bytes` (48), `kinds[]`, `chunk_ps`, `chunks[] {t0_ps, t1_ps, max_end_ps, file, count}`, `strings[]` (op id table), `count` |
| `jobs` | `file`, `record_bytes` (80), `count`, `attribution_keys[]`, `critpath_file` |
| `series` | `file`, `items[] {name, label, unit, group, t0_ps, rate_hz, offset, count}`; `offset` is a byte offset into the decompressed file |
| `lod` | `file`, `bin_ps` (1 ms), `bins`, `units[] {block, block_index, unit}`, `links[] [src_node, dst_node]`, `busy_offset`, `busy_bytes`, `link_offset` |
| `robot` | `fps`, `frames`, `bodies[]` (names), `chunk_frames`, `chunks[]`, `events[]` (robot-side events such as `grasp_success`, `grasp_miss`, `oscillation`, `safety_stop`, each with `t_ps`), `scene` (path to the scene directory, relative to the replay directory) |
| `results` | summary metrics for the drawer. If the producer does not supply them, the writer derives per-task miss rates and response quantiles, average and peak power, energy per robot-hour and peak temperature from the replay itself and says so in `results.source` |
| `scenario_events[]` | optional `{t_s, action, label}` markers drawn on the timeline |
| `latency_tolerance` | optional `{latency_ms[], success[], conveyor_speed_mps, synthetic, note}` for the tolerance band |

## Event record (48 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | int64 | `t_ps` start time |
| 8 | int64 | `dur_ps` |
| 16 | uint8 | `kind`: 0 job_release, 1 job_start, 2 job_finish, 3 job_miss, 4 op, 5 xfer, 6 dram_q, 7 freq, 8 preempt |
| 17 | uint8 | `block` index into `soc.blocks` (255 = none) |
| 18 | uint16 | `unit` (tile, core, CU or engine index) |
| 20 | uint16 | `task` index into `tasks` (0xFFFF = none) |
| 22 | uint16 | `src` NoC node, flat index `y * dims[0] + x` (0xFFFF = none) |
| 24 | uint16 | `dst` NoC node |
| 26 | uint16 | `flags`: bit 0 on the job's critical path, bit 1 job missed |
| 28 | uint32 | `job` id (0xFFFFFFFF = none) |
| 32 | uint32 | `op` index into `events.strings` (0xFFFFFFFF = none) |
| 36 | float32 | `bytes` |
| 40 | float32 | `value` (kind specific, e.g. layer index or queue depth) |
| 44 | uint32 | `aux` (e.g. NPU layer index) |

Events are sorted by `t_ps` and stored in the chunk containing their start time. `max_end_ps` lets the reader find long events that started in an earlier chunk.

## Job record (80 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | uint32 | `job` id |
| 4 | uint16 | `task` index |
| 6 | uint16 | `flags`: bit 0 missed, bit 1 skipped, bit 2 has critical path |
| 8, 16, 24, 32 | int64 | `release_ps`, `start_ps`, `finish_ps`, `deadline_ps` (-1 if not started or not finished) |
| 40 | float32 x 10 | attribution in nanoseconds, in `jobs.attribution_keys` order: compute, runqueue, engine_queue, noc, dram, cache, dma, preemption, dvfs, middleware |

`critpath.json.gz` maps job ids to `[{op_id, engine, start_ps, end_ps, wait_ps, wait_cause}]`, exactly as returned by the core's completion dicts. `engine` is the block id plus unit index, for example `npu2`.

## Series

Uniformly sampled float32 arrays. Names the viewer understands:

| Name | Unit | Used for |
|---|---|---|
| `power.<domain>` | W | block height, power panel, energy |
| `volt.<domain>` | V | domain tint, labels |
| `freq.<domain>` | Hz | labels, drawer |
| `temp.<domain>` | °C | heat overlay, drawer |
| `throttle.<domain>` | 0 or 1 | red tint when throttled |
| `dram_q.<controller>` | requests | DRAM queue columns |

Any other series is carried through for future panels.

## LOD aggregates

`lod.bin.gz` holds two matrices for zoomed-out views: `busy` is uint8 `[bins, units]` (busy fraction times 255 per 1 ms bin, used for the timeline's utilization area charts) followed, 4-byte aligned, by `links` float32 `[bins, links]` (bytes per 1 ms bin per directed mesh link, using XY routing, used for the NoC flow ribbons).

## Robot frames

`robot/rNNNN.bin.gz` holds float32 `[frames, bodies, 7]` = position xyz then quaternion wxyz, z up, metres. Bodies are named in `robot.bodies` and matched by name against the scene. The viewer accepts two scene formats: the co-sim export (`cosim/SCENE_FORMAT.md`, staged into the site with `stage_scene`) and the viewer's own Panda export (`viz/scripts/export_panda_scene.py`).

## Site index

`viz/public/replays/index.json`:

```json
{"format": "glasschip-replay-index", "version": 1,
 "replays": [{"id": "S2_P0", "path": "S2_P0/manifest.json", "scenario": "S2_second_camera",
              "scenario_label": "Second camera", "policy": "P0_nominal", "policy_label": "P0 Nominal",
              "seed": 7, "synthetic": false, "default": true}],
 "surrogate": {"meta": "../surrogate/meta.json"}}
```

Scenario buttons enable themselves for every scenario prefix (`S1_` ... `S8_`) that has a replay. The policy switcher lists the replays of the current scenario and keeps the playhead time when switching. `surrogate.meta` (optional, relative to `replays/`) points at a JSON file with `{model, input_name, inputs[], outputs[], coverage}`; the design-space panel stays in its disabled state until that file and its ONNX model load.

## Live mode

`?live=ws://host:port` connects `viz/src/live/client.ts`. Messages are msgpack maps: `{type: "hello", manifest}` once, then `{type: "batch", t_ps, events, jobs, series, robot}` where `events` and `jobs` are byte strings of the same 48-byte and 80-byte records, `series` maps names to new samples, and `robot` is float32 frames. Decoding goes through the same functions as replay chunks.

## Writer API

```python
from tools.replay_writer import ReplayWriter, stage_scene, update_index

w = ReplayWriter(out_dir, *, name, soc, tasks, duration_ps, scenario, policy, seed, synthetic,
                 provenance, scene=None, event_chunk_ps=250_000_000_000, robot_chunk_frames=120,
                 scenario_label=None, policy_label=None)
w.set_robot(fps, body_names, poses, events=None)        # poses: [F, B, 7] float32
w.add_events(events)        # list of trace-event dicts (CONTRACTS.md) or a numpy structured array
w.add_jobs(completions)     # completion dicts from sim.run_until(), with attribution and critical_path
w.add_series(name, *, t0_ps, rate_hz, values, unit, group, label=None)
w.set_results(results)      # optional; otherwise derived from the replay
w.set_extra(key, value)     # e.g. "scenario_events", "latency_tolerance"
w.write() -> Path
stage_scene(src_dir, dst_dir)          # copy + gzip a co-sim scene export into the site
update_index(index_path, entry)        # add or replace a replay in the site index
```
