# Fidelity mixes

Each file picks a fidelity level per component and can override model
parameters under `params:`. Unknown or unavailable levels fall back (for
example `noc: L2` falls back to L1 until a BookSim 2 backend is attached) and
`sim.info()["warnings"]` says so.

| File | cpu | npu | simt | dsp | noc | dram | cache | Use |
|---|---|---|---|---|---|---|---|---|
| interactive.yaml | L0 | L1 | L0 | L0 | L1 | L1 | L1 | live co-simulation |
| study.yaml | L1 | L2 (L1 fallback) | L1 | L1 | L1 | L1 | L1 | studies, surrogate data |
| analytic.yaml | L0 | L0 | L0 | L0 | L0 | L0 | L0 | sweeps |
| memory_focus.yaml | L0 | L1 | L1 | L1 | L1 | L1 | L1 | NoC and DRAM questions, 1 KB transactions |
| reference.yaml | L2 | L2 | L1 | L1 | L2 | L2 | L1 | calibration: gem5 CPI, SCALE-Sim, BookSim 2, Ramulator 2 |

L2 details: NPU and CPU L2 read `calib/tables/` automatically (SCALE-Sim `vx1_cycles` per op for the SoC's dataflow, gem5 O3/Minor CPI per kernel). DRAM L2 and NoC L2 load plugins next to the module and run Ramulator 2 and BookSim 2 in-process, synchronized every `quantum_ns`; see core/README.md for what that approximates.

The RT core is always analytic (it runs from TCM). DMA engines run at L1
whenever the NoC or DRAM is L1.

## Parameters (`params:`), all assumptions

| Key | Default | Meaning |
|---|---|---|
| bulk_chunk_bytes | 4096 | transaction size for NPU, SIMT, DSP and DMA streams at L1 |
| cpu_miss_bytes | 64 | CPU miss transaction size at L1 |
| noc_req_bytes | 16 | read request header size |
| noc_model_requests | true | reserve links for request headers (false: zero-load latency for headers, data still reserved) |
| npu_l0_util | 0.8 | MAC utilization at NPU L0 |
| npu_vec_ops_per_cycle | 256 | per-tile vector unit throughput (elementwise, softmax, norm, memcopy) |
| npu_buffer_depth | 2 | pipeline depth of SRAM tiles (2 = double buffering) |
| npu_weight_load_overlap | true | weight-tile load hidden behind compute (WS dataflow) |
| npu_min_macs_per_tile | 2e6 | layers below this use one tile; larger layers spread across tiles |
| npu_max_tiles_per_op | 4 | cap on tiles per layer |
| npu_fp32_rate_fraction | 0.0625 | FP32 rate relative to INT8 |
| simt_l0_eff | 0.6 | fraction of SIMT peak at L0 |
| simt_warps_needed_per_cu | 8 | resident warps needed to hide latency at L1 |
| simt_warp_size | 32 | threads per warp |
| cpu_vector_ipc, cpu_mem_ipc | 1.0, 1.0 | vector and memory instruction throughput per core |
| cpu_mlp | 8 | outstanding misses per core at L1 |
| cpu_l0_mem_bw_gbs | 20 | per-core sustainable miss bandwidth at L0 |
| dsp_line_buffer_kb | 64 | DSP line buffer (step size at L1) |
| dsp_pipeline_fill_cycles | 64 | per-op pipeline fill at L1 |
| dsp_l1_simd_eff | 0.85 | SIMD efficiency at L1 |
| dma_bytes_per_cycle | 64 | DMA engine datapath at the noc_cache clock |
| noc_vc_buffer_flits | 8 | legacy model only: buffered flits before the upstream hold |
| noc_l1_arbitration | 1 | 1 = reservation calendar (default), 0 = legacy whole-train FIFO (phase B) |
| noc_vc_flow_control | true | entering a link needs a free downstream input VC, held until the tail leaves that router |
| noc_rr_tail | 0.75 | round-robin output arbitration tail delay, fraction of a message's wait (fitted to BookSim) |
| noc_contention_bubble | 0 | idle cycles per packet on contended mesh links (fit found 0 best) |
| noc_ni_cycles | -1 | NI latency per message; -1 = router + 2 x link cycles (calendar), 0 (legacy) |
| noc_backpressure_hold | false | legacy upstream-hold rule |
| noc_calendar_horizon_ns | 20000 | reservations older than this are pruned |
| noc_util_window_ns | 10000 | utilization window for L0 queueing formulas |
| cache_block_bytes | 1024 | tag granularity of the set-associative cache model |
| stream_bypass_bytes | 2e6 | streams larger than this do not allocate in the system cache |
| dram_queue_depth | 32 | FR-FCFS window per channel |
| dram_l0_eff | 0.8 | sustained DRAM efficiency at L0 |
| dram_ctrl_ns | 10 | controller pipeline latency |
| dram_interleave_bytes | 4096 | channel interleave granularity |
| dram_util_window_ns | 20000 | utilization window for L0 queueing |
| irq_latency_ns | 2000 | accelerator completion interrupt to an application core |
| rt_irq_latency_ns | 300 | interrupt to the RT core |
| exec_cv | 0.0 | per-op execution-time noise (lognormal coefficient of variation) |
| mapping | static | `static` (first eligible engine, honoring affinity) or `eft` (greedy earliest finish time) |
| middleware_on_rt | false | run ROS 2 middleware cost on the RT core for RT tasks |
| rt_copy_bytes_per_ns | 16 | DMA copy rate into and out of RT TCM |
| fuse_attention | true | NPU compiler: flash-attention style fusion of QK^T, softmax, AV (scores stay in SRAM) |
| fuse_elementwise | true | NPU compiler: elementwise ops consume their conv/matmul producer's output on-chip |
| flash_block | 64 | Br = Bc tile size for the fused-attention SRAM fit check |
| dram_backend_plugin | core/build/libgc_ramulator.dylib | DRAM L2 plugin path (string) |
| ramulator_config | calib/ramulator/configs/lpddr5x_8533_derived.yaml | Ramulator 2 config (string) |
| noc_backend_plugin | core/build/libgc_booksim.dylib | NoC L2 plugin path (string) |
| booksim_config | (empty) | extra BookSim config text appended to the generated one, `key = value;` (string) |
