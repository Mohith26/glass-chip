# glasschip-core

This is the simulation kernel of Glass Chip: a deterministic discrete-event model of the VX-1 reference SoC (fictional, see `configs/soc/vx1.yaml`) that runs robot task graphs, schedules them on engines, moves their data through a mesh NoC, a shared system cache and LPDDR5X-style DRAM, and explains every job's response time.

I wrote it in C++20 with a pybind11 module (`glasschip_core`) so the co-simulation, power manager and studies can drive it from Python.

## Build and test

```
make -C core            # core/build/glasschip_core.cpython-*.so + L2 plugins
make -C core test       # C++ unit tests (analytic expectations) + pytest
make -C core debug      # C++ unit tests with UBSan (trap mode) + libc++ bounds-checked containers
make -C core debug-asan # AddressSanitizer + UBSan (needs a sanitizer runtime; does not start in the Aside sandbox)
make -C core bench      # simulation speed on scenario S1 with the real task graphs
```

There is no CMake dependency. The module is linked to a temporary file and renamed into place, so processes that already imported it keep working while it is rebuilt. The L2 plugins (`libgc_ramulator.dylib`, `libgc_booksim.dylib`) are separate libraries loaded with `dlopen` only when a fidelity mix asks for them; they are linked with `third_party/wrap/clang++-noq`, which clears the quarantine attribute that otherwise gets freshly linked libraries killed in the sandbox.

```python
import sys, yaml, json
sys.path.insert(0, "core/build")
import glasschip_core as gc

sim = gc.Sim(yaml.safe_load(open("configs/soc/vx1.yaml")),
             yaml.safe_load(open("configs/fidelity/interactive.yaml")), seed=1)
sim.add_task(json.load(open("workloads/graphs/T2_detect.json")))
sim.release_job("T2_detect", 0, tag=0)
for job in sim.run_until(50_000_000_000):          # 50 ms in picoseconds
    print(job["response_ps"], job["attribution"], job["critical_path"][:3])
```

The full API is in `notes/CONTRACTS.md`. Additions beyond it are listed at the end of this file.

## How the kernel works

- Virtual time is an int64 count of picoseconds. Events are plain structs ordered by (time, sequence number), so ties resolve in insertion order and a run is fully determined by its configs and seed.
- Each component has its own PCG64 random stream derived from the master seed and the component name.
- Events carry a component id and integer payloads, never closures. Every piece of state lists its fields once (`GC_IO`), which drives both `checkpoint()` and `restore()`. Restoring a checkpoint and running forward produces the same trace hash as the original run; a test checks this.
- Components exchange timestamped `MemTxn` records. An engine issues a transaction; the NoC adds its latency; the cache decides hit or miss; the DRAM controller schedules the miss and returns completion time. Each transaction carries its latency split (noc, cache, dram) back to the requester for attribution.

## Fidelity levels

| Component | L0 (analytic) | L1 (transaction level) | L2 |
|---|---|---|---|
| CPU | instruction-mix cycles, roofline memory time | misses at `cpu_miss_bytes` through cache and DRAM, `cpu_mlp` outstanding | gem5 O3 cycles per call for the real kernels, CPI for other kernel classes (`calib/tables/cpu_gem5.json`), or `set_l2_tables({"cpu_cpi": ...})` |
| RT CPU | instruction cycles from TCM, no DRAM traffic | same | gem5 Minor cycles per call or CPI (in-order proxy) when `cpu: L2` |
| NPU | MACs / (peak x utilization), roofline memory | closed-form systolic tiles (weight or output stationary), SRAM tiling with double buffering, weight and activation streaming over the NoC, INT8/FP8/BF16 rates, per-layer utilization, compiler fusion (below) | SCALE-Sim `vx1_cycles` per op (`calib/tables/npu_scalesim_<task>.json`), op field `l2_cycles`, or `set_l2_tables({"npu_cycles": ...})`; memory traffic still comes from the simulator |
| SIMT | flops / (peak x efficiency) + launch overhead | occupancy from threads (lanes covered, warps to hide latency), launch overhead, coalescing inflates traffic | none |
| DSP | ops / SIMD throughput | SIMD efficiency, pipeline fill, line-buffer sized steps | none |
| NoC | zero-load latency + M/D/1 wait on the busiest link | 4x4 mesh, XY routing, per-link reservation calendars (messages fill gaps, concurrent messages interleave), injection and ejection ports, VC flow control, QoS priority with per-class VCs, round-robin tail term; validated against BookSim (below) | BookSim 2 in-process (plugin) |
| System cache | hit-rate model per stream | set-associative tags, LRU, way partitioning per QoS class, no-allocate for large streams, dirty write-backs | none |
| DRAM | base latency + transfer at sustained efficiency + M/D/1 wait | channels x bank groups x banks, open page, tRCD/tRP/CL/tRAS, all-bank refresh, read/write turnaround, FR-FCFS within a bounded window, per-class queues with QoS, row-hit accounting per 64 B burst | Ramulator 2 in-process (plugin) | <!-- numcheck: ignore -->
| DMA | descriptor latency + bytes / bandwidth | descriptor latency, read then write streams, 2D descriptors with short rows become small transactions, completion interrupt | none |

Mixes live in `configs/fidelity/` (`interactive`, `study`, `analytic`, `memory_focus`, `reference`). L0 engines do not issue transactions; their traffic is added as fluid load on the DRAM channels and NoC links they use. A level with no implementation, or an L2 plugin that cannot be loaded, falls back to L1 and `sim.info()["warnings"]` says why. `info()["backends"]` and `info()["l2"]` show what is actually in use.

### L1 NoC model and its validation against BookSim

Each link (mesh links plus each node's injection and ejection ports) keeps a calendar of busy intervals. A message (up to a 4 KB chunk) fills the earliest gaps at or after its head arrival, so packets of concurrent messages interleave and a reservation made for the future never blocks earlier traffic. Cut-through pipelining: the next link's head can start one hop after this link's first fragment; its tail cannot finish before this link's tail plus one hop. Zero-load latency is `hops x (router + link) + NI + serialization`, where NI (source router, injection and ejection channels, `router + 2 x link` cycles) matches BookSim's measured `3h + 4 + flits` cycles. Two effects of BookSim's input-queued routers are approximated:

- VC flow control: entering a link needs a free input VC at the router it feeds (4 VCs); the message holds it until its tail has left that router. With QoS on, safety and realtime get their own VC and best effort the rest.
- Round-robin output arbitration interleaves flits of competing packets, which delays tails compared with first-come-first-served. A message that waited at a link gets `noc_rr_tail` (0.75, fitted) of its wait, capped at its own serialization, added to its arrival. This is latency only; link and VC occupancy are unchanged. <!-- numcheck: ignore -->

Validation numbers (build in `core/build/BUILD_ID`):

| Open-loop traffic | mean abs error below BookSim saturation, phase B | now | saturation (3x zero load) BookSim | phase B | now | <!-- numcheck: ignore -->
|---|---|---|---|---|---|
| uniform/2flit | 10577.1% | 13.7% | beyond tested | 0.35 | 0.72 |
| uniform/8flit | 2839.2% | 4.7% | 0.61 | 0.32 | 0.72 |
| transpose/2flit | 22.8% | 2.2% | beyond tested | 0.32 | beyond tested |
| transpose/8flit | 20.8% | 1.0% | 0.31 | 0.31 | 0.31 |
| hotspot_dram/2flit | 42.4% | 5.4% | beyond tested | 0.11 | beyond tested |
| hotspot_dram/8flit | 26.6% | 1.1% | 0.11 | 0.10 | 0.11 |

The phase B model saturated uniform traffic at half BookSim's load because a reservation made for the future blocked earlier traffic, and missed BookSim's fixed 4-cycle NI latency. Most remaining error for uniform 2-flit traffic is the single point at 0.75 offered load, at BookSim's knee. <!-- numcheck: ignore -->

| Closed loop (core run, packet or message trace replayed through BookSim) | packet mean ratio, phase B | now | message mean ratio, phase B | now |
|---|---|---|---|---|
| all_to_one_hotspot_6x4MB | 2.07 | 1.38 | 2.50 | 1.67 |
| stream_copy_dma_8MB | 1.02 | 1.00 | 1.01 | 1.06 |
| gemm_vit_mlp_1024x768x3072 | 1.15 | 0.93 | 1.18 | 0.94 |
| attention_vit_12h_1024 | 0.99 | 0.95 | 1.01 | 0.97 |

Ratios are core mean latency over BookSim mean latency on identical injection times. The all-to-one hotspot is still pessimistic: its packets queue at one ejection port and closed-loop replay feeds the model's own timing back into the trace.

| S1, 40 ms, study mix | mean response with NoC L2 (BookSim), ms | NoC L1 phase B, error | now | <!-- numcheck: ignore -->
|---|---|---|---|
| T10_record | 0.627 | +8.9% | +2.2% |
| T1_ingest_overhead | 1.506 | -7.2% | -7.7% |
| T1_ingest_wrist | 2.058 | +2.0% | -0.0% |
| T2_detect | 3.257 | -0.7% | -2.7% |
| T3_esdf | 3.379 | -1.4% | -3.0% |
| T6_estimate | 0.836 | +4.7% | -3.3% |
| T8_safety | 1.503 | +11.3% | +11.3% |

T8_safety's +11.3% is the same with both L1 models, so it does not come from the L1 NoC; with NoC L2, request headers and writes run at zero-load timing (see L2 backends), which shortens T8's NPU-side path.

`core/bench/noc_validate.py` replays identical open-loop Bernoulli traces (the calibration generator, seed 11, 20k cycles) through BookSim 2 and through L1, for the phase B model ("before": whole-train FIFO reservation, upstream hold, no NI cycles) and the current one ("after"). Results are in `core/bench/noc_validation.json`, and closed-loop checks (microbenchmark packet traces replayed through BookSim, scenario S1 against NoC L2) in `core/bench/noc_closed_loop.json`. The remaining known gap: under uniform traffic with 8-flit packets L1 saturates later than BookSim, because head-of-line blocking inside input-queued routers is not modeled. The legacy model stays available with `noc_l1_arbitration: 0`.

### The NoC caps DRAM traffic at about 96 GB/s

Each node's network interface has one link-width injection port and one ejection port (32 B per cycle at 1.5 GHz, 48 GB/s). The two DRAM controller nodes therefore cap DRAM traffic at 96 GB/s in each direction, below the 136.5 GB/s DRAM peak (all derived from `vx1.yaml`). <!-- numcheck: ignore -->
BookSim shows the same cap for all-to-DRAM traffic: its two-node hotspot saturates at 95.5 GB/s total <!-- src: calib/tables/noc_booksim.json#curves["hotspot_dram/2flit"].max_stable_accepted_gbps_total -->, and the unit test `noc_dram_node_port_cap_matches_booksim` checks that the L1 model reaches the port cap. Read data leaving the DRAM nodes gets a little less because XY routes from both DRAM nodes share row-3 links. This is a property of the VX-1 floorplan in `vx1.yaml`, not of the model.

## L2 backends and time-quantum synchronization

`dram: L2` and `noc: L2` load the plugins and run Ramulator 2 and BookSim 2 inside the simulation:

- **Ramulator 2** (`src/adapters/ramulator_backend.cpp`): External frontend, one tick per tCK (0.938 ns for `calib/ramulator/configs/lpddr5x_8533_derived.yaml`), requests split into 32 B transactions, at most 128 offered per cycle. Physical addresses are folded into 16 GiB (the config's capacity).
- **BookSim 2** (`src/adapters/booksim_backend.cpp`): built from BookSim's object files with the calibration worker's trace-mode patch, configured from `vx1.yaml` with the mapping in `calib/booksim/booksim_cfg.py`. Messages are split into 256 B packets. Every message is injected into BookSim, so all contention is simulated there. Read data returns (cache hits and DRAM responses), which decide when compute can continue, wait for BookSim's arrival time. Request headers and writes keep zero-load timing on their own path, because the cache and DRAM decisions downstream of them are made synchronously.

Both backends are advanced in steps of `quantum_ns` (default 100 ns) while they have work. Completion times are exact, but the simulator only observes them at the next quantum boundary, so a requester can react up to one quantum late when its data arrived just before a boundary. `core/bench/quantum.json` measures this on one detector job: with Ramulator, response time changes by less than 0.2% from 1 ns to 1000 ns quanta (double buffering hides it); with BookSim it is within 0.2% up to 100 ns and grows by about 6% at 1000 ns.

Limits: one BookSim instance per process (it keeps global state); BookSim's cycle counter is 32-bit, so NoC L2 runs stop at about 1.4 s of simulated time; BookSim calls `exit()` on a configuration error, so `params.booksim_config` must be valid BookSim syntax; DRAM activates are not counted at DRAM L2; the BookSim clock does not follow `noc_cache` DVFS; `checkpoint()` raises while a backend is attached. Both backends are thousands of times slower than real time. <!-- numcheck: ignore -->

## Declared MACs vs shapes

When an op's declared `macs` and the MACs implied by its `shape` differ by more than 2x, `info()["warnings"]` names the task and ops. I trust the declared MACs for the amount of work, because they come from the full graph walk (for example T11_speech decoder steps include attention over a 1,500-entry cross-attention cache that the summary shape `1 x 384 x 384` leaves out), and use the shape only for array utilization: shape-based NPU cycles are multiplied by `declared / shape`. Shape fields `layers` and `repeat` count as multipliers. <!-- numcheck: ignore -->

## NPU compiler assumptions

The NPU model includes a compiler pass (`fuse_plan` in `src/runtime/runtime.cpp`), on by default:

- `fuse_attention`: QK^T, optional elementwise scaling, softmax, optional elementwise, AV runs as one flash-attention style tiled loop. When a Br x Bc tile (`flash_block`, 64) with its Q, K and V blocks fits in half a tile's SRAM, the score tensors never leave NPU SRAM.
- `fuse_elementwise`: elementwise ops read their conv/matmul (or already fused elementwise) producer's output on-chip.

Fused edges skip the producer's DRAM write and the consumer's DRAM read; fused ops are pinned to the NPU. The ops still run and their vector work is still costed. `core/bench/fusion.json` (rerun on the frozen build) has the effect on T4_vla: DRAM traffic per inference falls from 2.874 GB to 1.345 GB (about its weights), and its S1 p50 response from 69.76 ms to 56.33 ms at the interactive mix. <!-- src: core/bench/fusion.json#results.no_fusion.alone_dram_bytes_per_inference|scale=1e-9 --> <!-- src: core/bench/fusion.json#results["both (default)"].alone_dram_bytes_per_inference|scale=1e-9 --> <!-- src: core/bench/fusion.json#results.no_fusion.s1_p50_ms --> <!-- src: core/bench/fusion.json#results["both (default)"].s1_p50_ms -->

## Runtime model

- `release_job` creates a job with an absolute deadline. With `on_overrun: skip`, a release is dropped (returns -1) while a previous job of that task is still active.
- Each op maps to an engine when its dependencies finish: `static` takes the first eligible engine present (task affinity first); `eft` estimates earliest finish time.
- Per-engine schedulers: `fifo`, `fixed_priority` (rate monotonic by period), `edf`, `edf_mc` (criticality level first, then EDF, so best-effort work never preempts safety work). CPU and RT cores preempt (immediately at L0, at the next step boundary at L1) and pay `context_switch_ns`. The NPU preempts only at layer boundaries (`preempt_boundary` in the graph) and pays `preempt_save_restore_us`, split between the save and the later restore.
- Middleware: with `set_middleware`, each job gets a receive op (executor dispatch plus deserialization) and a publish op (serialization plus copies; copies also become memory traffic). The single-threaded executor runs all of them on one core. Zero-copy transport makes the consumer read the producer's output buffer directly.
- Buffer manager: data stays in shared memory between engines (zero copy). Data crossing into or out of RT TCM costs a DMA copy. Accelerator completions pay an interrupt latency before the host continues.

## Why did this miss?

Every completed job carries `attribution` (compute, runqueue, engine_queue, noc, dram, cache, dma, preemption, dvfs, middleware) and a `critical_path`. I walk back from the final op through the predecessor that determined each op's ready time and assign every interval on that path to exactly one category, so the categories sum to the response time exactly. Memory stalls inside an op are split by the latency components of the transaction that arrived last. Power-gate wake-ups and DVFS transition stalls count as `dvfs`; completion interrupts and executor overhead as `middleware`.

## Power-manager interface

- `pending_jobs()[i]["est_remaining_cycles"][domain]`: compute cycles at that domain's clock that the job still needs if it ran alone. Each op is costed at its engine's fidelity level on the number of units it would get (a GEMM split over 4 NPU tiles counts its per-tile cycles once), so it is wall-clock cycles, not summed over cores or tiles. A running op contributes its unfinished fraction. Memory stalls are not included because they do not scale with the domain clock. Ops not yet mapped are assumed to run on their first eligible engine.
- Completion `cycles_by_domain[domain]`: compute cycles the job actually executed in each domain, with the same meaning, so `actual / estimate` measures estimation error (equal when `exec_cv` is 0). `busy_ps_by_domain[domain]` is the time the job held units in that domain, including memory stalls, setup and wake-ups.
- `set_domain_freq(domain, hz)` accepts any positive frequency. The domain stalls for `dvfs_transition_us` on every change; work already done is kept in cycles.
- `set_power_gating(domain, enabled, wake_latency_ps=0, idle_before_gate_ps=0)` for cpu_a, cpu_b, rt, npu, simt, dsp. When enabled, the domain gates once none of its units has had work for `idle_before_gate_ps` (0 = immediately; the waiting time counts as idle, not gated). The next op to start in a gated domain waits `wake_latency_ps`. Concurrent starts during a wake-up wait for the same wake-up; work arriving before the timer expires cancels it.
- `counters()` is keyed by power domain: busy and idle time, `gated_ps` (subset of idle), `wakeups`, and activity counts credited as work progresses (MACs by type, SRAM bytes, CPU instructions, vector ops, SIMT flops, DSP ops per compute domain; flit-hops, cache bytes and DMA bytes under `noc_cache`; DRAM bytes and activates under `dram_io`).

## Fault hooks

`set_fault(kind, rate, seed, param_ns=None)`, each kind with its own seeded stream so runs stay deterministic:

- `npu_bitflip`: probability per NPU op execution that its result is corrupted. The job's completion has `corrupted: True` and a `faults` count; timing is unchanged.
- `noc_delay`: probability per NoC data message of an extra `param_ns` (default 50 ns).
- `noc_drop`: probability per NoC data message that it is dropped and resent after a `param_ns` timeout (default 200 ns), reserving the path again.

Faults appear as `fault` trace events and in `info()["faults"]`.

## Tracing and exports

`trace_enable(True)` buffers events for `trace_drain()` (list of dicts, or a numpy structured array with `as_numpy=True` plus `trace_names()`). `trace_open(path)` streams 56-byte records to a `.gct` file with a JSON string table in the footer. `trace_hash()` covers every event emitted, whether or not tracing is on. `tools/perfetto_export.py` converts either form to Chrome JSON for ui.perfetto.dev with one track per engine unit and per task, DVFS and DRAM queue counters, and fault markers.

For external-simulator replay:

- `dram_trace_open(path)` (alias `export_dram_trace`): `time_ns,addr,rw,bytes,latency_ns`, one row per request at the DRAM controller. `time_ns` is the arrival at the controller; `latency_ns` is arrival to the end of the data burst plus the controller pipeline, NoC excluded. Addresses are physical (16 GiB fold of the simulator's synthetic address space). <!-- numcheck: ignore -->
- `noc_trace_open(path)` (alias `export_noc_trace`): `time_ns,src_x,src_y,dst_x,dst_y,bytes,latency_ns`, one row per 256 B packet (messages are split), `latency_ns` from injection to that packet's tail arrival. Includes 16 B request headers. <!-- numcheck: ignore -->

Rows are written in time order once they are 1 ms older than the simulation clock, and all on close.

## Measured speed

The calendar NoC costs CPU time: the study mix models every request header on the NoC and runs about half as fast as with the phase B NoC model. For large sweeps, `noc_model_requests: false` (as in the interactive mix) saves about 25% at the study mix, at the cost of zero-load timing for 16 B request headers.

`core/bench/bench_speed.py` runs scenario S1 with the real task graphs in `workloads/graphs/`, stepped in 1 ms increments the way the co-simulation drives it, on one thread, and writes `core/bench/speed.json`. Speed is simulated seconds per second of process CPU time, because other jobs were sharing the machine when I measured and wall-clock time was noisy.

## Assumptions and limits

- The cache lookup for a read happens when the request is issued rather than when it reaches the cache slice, so tag state can update up to one NoC traversal early.
- The NoC response for a DRAM read is reserved when the controller schedules it, at most `tRP + tRCD + CL` ahead of the current time.
- With `noc_model_requests: false` (interactive mix), 16 B read request headers use zero-load latency; all data messages still reserve links. <!-- numcheck: ignore -->
- L1 NoC reservations are made when a message is sent (in call order), not cycle by cycle; switch allocation and head-of-line blocking inside routers are approximated by VC flow control and the round-robin tail term, so L1 saturates later than BookSim for uniform traffic with 8-flit packets.
- DMA 2D descriptors use their `stride` to place source rows, so strided gathers open more DRAM rows.
- Bank groups only affect address mapping; tCCD_L/tCCD_S are not in the config.
- The L0 cache model assumes a stream shares its class's capacity with about one other stream of equal size. CPU L1 uses the graph's `reuse` field as its private-cache hit rate instead of a stack-distance model.
- CPU L2 maps the real kernels by task (T7/T12 control step, T6 estimator, T8 SSM): the task's ops mapped to that kernel cost gem5's measured `cycles_per_call` in total (O3 for app cores, Minor for the RT core), split by instruction count, because the graphs' static instruction counts overstate dynamic counts (T6 12,436 static vs 5,461 dynamic). Other ops map to an axpy or GEMM kernel class by instruction mix and use the graph's instruction count times that kernel's CPI.
- Executor dispatch and serialization costs come from `workloads/graphs/middleware.json`, measured on a Mac and scaled; they are not measurements of a robot SoC.
- VX-1 is fictional. Every model parameter is an assumption with a config key; see `configs/fidelity/README.md`.

## API additions beyond notes/CONTRACTS.md

- `Sim(soc, fidelity, seed=0, l2_table_dir=None)`; `load_l2_tables(dir)`.
- `set_scheduler("all", policy)`, `set_mapping("static" | "eft")`, `set_task_priority(task_id, priority)`, `set_params(dict)`, `set_l2_tables(dict)`.
- `set_power_gating(domain, enabled, wake_latency_ps=0, idle_before_gate_ps=0)`; `set_fault(kind, rate, seed=0, param_ns=None)`.
- `trace_drain(as_numpy=False)`, `trace_names()`, `trace_open(path)`, `trace_close()`, `dram_trace_open/close`, `noc_trace_open/close` (aliases `export_dram_trace`, `export_noc_trace`).
- `info()` returns levels in use, fallback warnings, backends, L2 table hits and fallbacks and table sources, fault counts, units per engine, schedulers, event counts.
- `npu_gemm_cycles(M, K, N, B=1, tiles=1, dtype="int8")` exposes the closed-form systolic model; `noc_replay(records)` replays an open-loop NoC trace through the NoC model (validation).
- Completion dicts also have `cycles_by_domain`, `busy_ps_by_domain`, `corrupted`, `faults`; `critical_path` entries also have `unit`, `gap_ps` and `gap_cause`. Counters also have `gated_ps`, `wakeups`, `unit_busy_ps` and `units`. `queue_depths()` also has `dram_channels` and `engines_busy`. Op trace dicts also have `units` (tiles or CUs used). Trace kinds include `fault`.
