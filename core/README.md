# glasschip-core

This is the simulation kernel of Glass Chip: a deterministic discrete-event model of the VX-1 reference SoC (fictional, see `configs/soc/vx1.yaml`) that runs robot task graphs, schedules them on engines, moves their data through a mesh NoC, a shared system cache and LPDDR5X-style DRAM, and explains every job's response time.

I wrote it in C++20 with a pybind11 module (`glasschip_core`) so the co-simulation, power manager and studies can drive it from Python.

## Build and test

```
make -C core            # builds core/build/glasschip_core.cpython-*.so
make -C core test       # C++ unit tests (analytic expectations) + pytest
make -C core debug      # C++ unit tests under AddressSanitizer + UBSan
make -C core bench      # simulation speed on scenario S1 with the real task graphs
```

There is no CMake dependency. The Makefile takes include paths from `.venv/bin/python -m pybind11 --includes` and builds with `-O3 -Wall -Wextra -Wpedantic`.

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

| Component | L0 (analytic) | L1 (transaction level) | L2 hook |
|---|---|---|---|
| CPU | instruction-mix cycles, roofline memory time | misses at `cpu_miss_bytes` through cache and DRAM, `cpu_mlp` outstanding | per-op CPI tables via `set_l2_tables({"cpu_cpi": ...})` |
| RT CPU | instruction cycles from TCM, no DRAM traffic | same | none |
| NPU | MACs / (peak x utilization), roofline memory | closed-form systolic tiles (weight or output stationary), SRAM tiling with double buffering, weight and activation streaming over the NoC, activations kept in shared SRAM between NPU layers, INT8/FP8/BF16 rates, per-layer utilization | SCALE-Sim cycles from op field `l2_cycles` or `set_l2_tables({"npu_cycles": ...})` |
| SIMT | flops / (peak x efficiency) + launch overhead | occupancy from threads (lanes covered, warps to hide latency), launch overhead, coalescing inflates traffic | none |
| DSP | ops / SIMD throughput | SIMD efficiency, pipeline fill, line-buffer sized steps | none |
| NoC | zero-load latency + M/D/1 wait on the busiest link | 4x4 mesh, XY routing, per-link reservation of packet trains, QoS classes as separate priority timelines (one packet of non-preemptive blocking per link), VC buffer backpressure that holds the upstream link | `NocBackend` interface for BookSim 2 (not attached) |
| System cache | hit-rate model per stream | set-associative tags, LRU, way partitioning per QoS class, no-allocate for large streams, dirty write-backs | none |
| DRAM | base latency + transfer at sustained efficiency + M/D/1 wait | channels x bank groups x banks, open page, tRCD/tRP/CL/tRAS, all-bank refresh (tRFC every tREFI), read/write turnaround, FR-FCFS within a bounded window, per-class queues when QoS is on, row-hit accounting per 64 B burst | `DramBackend` interface for Ramulator 2 (not attached) |
| DMA | descriptor latency + bytes / bandwidth | descriptor latency, read then write streams, 2D descriptors with short rows become small transactions, completion interrupt to the host | none |

Mixes live in `configs/fidelity/` (`interactive`, `study`, `analytic`, `memory_focus`). L0 engines do not issue transactions; their traffic is added as fluid load on the DRAM channels and NoC links they use, so L1 traffic from other engines still sees them. A level with no implementation falls back to the next lower level and `sim.info()["warnings"]` says so.

## Runtime model

- `release_job` creates a job with an absolute deadline. With `on_overrun: skip`, a release is dropped (returns -1) while a previous job of that task is still active.
- Each op maps to an engine when its dependencies finish: `static` takes the first eligible engine present (task affinity first); `eft` estimates earliest finish time.
- Per-engine schedulers: `fifo`, `fixed_priority` (rate monotonic by period), `edf`, `edf_mc` (criticality level first, then EDF, so best-effort work never preempts safety work). CPU and RT cores preempt (immediately at L0, at the next step boundary at L1) and pay `context_switch_ns`. The NPU preempts only at layer boundaries (`preempt_boundary` in the graph) and pays `preempt_save_restore_us`, split between the save and the later restore.
- Middleware: with `set_middleware`, each job gets a receive op (executor dispatch plus deserialization of its input messages) and a publish op (serialization plus copies, copies also become memory traffic). The single-threaded executor runs all of them on one core. Zero-copy transport makes the consumer read the producer's output buffer directly.
- Buffer manager: data stays in shared memory between engines (zero copy). Data crossing into or out of RT TCM costs a DMA copy. Accelerator completions pay an interrupt latency before the host continues.

## Why did this miss?

Every completed job carries `attribution` (compute, runqueue, engine_queue, noc, dram, cache, dma, preemption, dvfs, middleware) and a `critical_path`. I walk back from the final op through the predecessor that determined each op's ready time and assign every interval on that path to exactly one category, so the categories sum to the response time exactly. Memory stalls inside an op are split by the latency components of the transaction that arrived last.

## DVFS and counters

`set_domain_freq(domain, hz)` stalls that domain for `dvfs_transition_us`, then running work continues at the new clock. Compute time is tracked in cycles, so progress made before the change is kept. NoC and cache latencies follow the `noc_cache` clock. `counters()` reports per-domain busy and idle time and activity counts (MACs by type, SRAM bytes, flit-hops, cache bytes, DRAM bytes and activates, CPU instructions, vector ops, SIMT flops, DSP ops, DMA bytes), credited as work progresses rather than at op end.

## Tracing

`trace_enable(True)` buffers events for `trace_drain()` (list of dicts, or a numpy structured array with `as_numpy=True` plus `trace_names()`). `trace_open(path)` streams 56-byte records to a `.gct` file with a JSON string table in the footer. `trace_hash()` covers every event emitted, whether or not tracing is on. `tools/perfetto_export.py` converts either form to Chrome JSON for ui.perfetto.dev with one track per engine unit and per task, plus DVFS and DRAM queue counters.

## Measured speed

Numbers come from `core/bench/bench_speed.py` (output in `core/bench/speed.json`): scenario S1 with the real task graphs in `workloads/graphs/`, stepped in 1 ms increments the way the co-simulation drives it, one thread. Speed is simulated seconds per second of process CPU time; I report CPU time because other jobs were sharing the machine when I measured, which made wall-clock time noisy.

## Assumptions and limits

- The cache lookup for a read happens when the request is issued rather than when it reaches the cache slice, so tag state can update up to one NoC traversal early.
- The NoC response for a DRAM read is reserved when the controller schedules it, which is at most `tRP + tRCD + CL` ahead of the current time.
- With `noc_model_requests: false` (interactive mix), 16 B read request headers use zero-load latency; all data-carrying messages still reserve links.
- NoC arbitration is reservation based: a packet train reserves each link in order at injection rather than flit by flit, and QoS is modeled with priority timelines. BookSim 2 would be the reference for this.
- Bank groups only affect address mapping; tCCD_L/tCCD_S are not in the config, so same-bank-group burst spacing is not modeled.
- The L0 cache model assumes a stream shares its class's capacity with about one other stream of equal size.
- CPU L1 uses the graph's `reuse` field as its private-cache hit rate instead of a stack-distance model.
- Executor dispatch and serialization costs come from `workloads/graphs/middleware.json`, which were measured on a Mac and scaled; they are not measurements of a robot SoC.
- VX-1 is fictional. Every model parameter is an assumption with a config key; see `configs/fidelity/README.md`.

## API additions beyond notes/CONTRACTS.md

- `Sim(soc, fidelity, seed=0)`: seed has a default.
- `set_scheduler("all", policy)` sets every engine.
- `set_mapping("static" | "eft")`, `set_task_priority(task_id, priority)`, `set_params(dict)`, `set_l2_tables(dict)`.
- `trace_drain(as_numpy=False)`, `trace_names()`, `trace_open(path)`, `trace_close()`.
- `info()` returns levels in use, fallback warnings, L2 table hits and fallbacks, units per engine, schedulers, event counts.
- `npu_gemm_cycles(M, K, N, B=1, tiles=1, dtype="int8")` exposes the closed-form systolic model.
- Completion `critical_path` entries also have `unit`, `gap_ps` and `gap_cause` (interrupt or TCM copy before the op). Counters also have `unit_busy_ps` and `units`. `queue_depths()` also has `dram_channels` and `engines_busy`. Op trace dicts also have `units` (tiles or CUs used).
