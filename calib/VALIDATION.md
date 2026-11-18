# Validation summary

This page summarizes two studies: `studies/s10_cross_fidelity.py` (output `results/s10.json`)
and `studies/s15_external_sanity.py` (output `results/s15.json`). The NoC before/after section
also uses the core's own benchmarks, `core/bench/noc_validation.json` and
`core/bench/noc_closed_loop.json`.

Every number below is copied from those files, and I name the JSON path for each group. All
results were produced with the frozen core build `3e9cf32325432d0f`. Both results files record it
in `provenance.core_build_id`; s10 also records it for every worker process
(`provenance.worker_build_ids`) and for the trace replays (`trace_replay.core_build_id`).

Accuracy here means agreement with my reference models (SCALE-Sim, gem5, Ramulator 2,
BookSim 2) and, in s15, with public benchmark numbers. It does not mean agreement with silicon.
VX-1 is a fictional SoC.

## 1. How I measured

- Fidelity configurations:
  - all-L0 and all-L1.
  - "L2t": NPU at L2 (SCALE-Sim cycle tables) and CPU at L2 (gem5 cycles per kernel call or gem5
    CPI), with NoC and DRAM at L1.
  - One-component swaps on the L1 base.
  - The four named mixes in `configs/fidelity/`.
- Error: (config minus reference) divided by reference, on each task's mean and p99 response time.
  I then average the absolute errors over every (scenario, task) pair.
- Tier A: 21 microbenchmarks (`calib/microbench.py`) and scenarios S1-S8, chip only, policy P0.
  - Windows: S1 2.0 s, S2 5.5 s, S3-S8 8.5 s (`setup.scenario_windows_s`).
  - Reference: L2t. 87 scenario-task pairs.
- Tier B: Ramulator 2 and BookSim 2 run in-process as the core's DRAM and NoC L2.
  - That path runs at 6.21e-5 simulated seconds per CPU second, about 16,000 times slower than
    real time.
  - So tier B covers the microbenchmarks plus only the **first 40 ms of S1** <!-- src: results/s10.json#setup.tier_b_s1_window_s|scale=1000 -->
    (`setup.tier_b_s1_window_s`).
  - Reference: full L2 (`configs/fidelity/reference.yaml`).
  - Only 9 tasks finish inside 40 ms, and T4_vla is not one of them. Treat tier B as a spot check. <!-- src: results/s10.json#setup.tier_b_s1_window_s|scale=1000 -->
- I ran at most 3 worker processes at a time because other jobs shared the machine. Speeds are
  CPU-time based for the same reason.
- `info()` reports the RT core as L0 in every configuration, but the core's RT CPU L2 follows the
  `cpu` level. So the "cpu" rows below include the RT core.

## 2. Acceptance targets (spec 11.2), tier A (`analysis.acceptance`)

| Target | Measured | Result |
|---|---|---|
| L1 within 10% of L2 on mean task latency | 12.1% mean absolute error | missed | <!-- src: results/s10.json#analysis.acceptance.L1_mean_target|pct --> <!-- src: results/s10.json#analysis.acceptance.L1_mean_abs_err|pct -->
| L1 within 20% of L2 on p99 | 14.8% | met | <!-- src: results/s10.json#analysis.acceptance.L1_p99_target|pct --> <!-- src: results/s10.json#analysis.acceptance.L1_p99_abs_err|pct -->
| L0 within 30% of L2 on means | 24.6% | met |

60.9% of the L1 task means fall within 10%, and 83.9% within 30%. For L0 the same shares are
28.7% and 52.9% (`analysis.scenarios.L1`, `.L0`).

**Why L1 misses on the mean: the CPU model.**
- Moving only the CPU to L2 cuts the L1 error to 4.1% on means and 6.3% on p99 <!-- src: results/s10.json#analysis.scenarios.L1+cpu@L2.mean.mean_abs|pct --> <!-- src: results/s10.json#analysis.scenarios.L1+cpu@L2.p99.mean_abs|pct -->
  (`analysis.scenarios["L1+cpu@L2"]`).
- Moving only the NPU to L2 gives 11.2% and 12.7% (`["L1+npu@L2"]`).
- The worst L1 tasks are the RT control loops, 40.2% to 42.1% too fast:
  - T7_control in S3-S8 and T12_arm2_control in S6 (`analysis.worst.L1`).
  - Cause: L1 assumes the in-order RT core sustains IPC 1.0, while gem5's MinorCPU runs
    control_step at CPI 1.77.
- The other large L1 miss is T14_ota in S6, at -46.8%.

**Where L0 loses accuracy** (`analysis.worst.L0`):
- T1_ingest_overhead is about 51% to 57% too slow in S3-S6, and T3_esdf is 51% to 54% too slow in
  S2 and S6. Both depend on NoC and DMA contention that the analytic model represents only as
  fluid load.
- In S6, T11_speech and T14_ota are 54.4% and 52.3% too fast.
- Of the single components, the NoC costs the most when dropped to L0: 22.2% mean error, against <!-- src: results/s10.json#analysis.scenarios.L1+noc@L0.mean.mean_abs|pct -->
  15.2% to 17.7% for CPU, NPU and SIMT (`analysis.scenarios["L1+*@L0"]`).

**Named mixes against L2t:**

| Mix | Mean error | p99 error |
|---|---|---|
| study | 11.2% | 12.7% |
| memory_focus | 15.6% | 24.0% |
| interactive | 20.4% | 28.4% |
| analytic | 24.6% | 52.3% |

**Microbenchmarks** (26 cases, single job each; `analysis.microbench`), mean absolute error: <!-- src: results/s10.json#analysis.microbench.L1.stats.n -->
- L1: 16.8% (median 3.4%)
- study: 5.6%
- L0: 153.1% (median 43.3%)

## 3. Tier B: Ramulator 2 and BookSim 2 in the loop (`analysis_tier_b`, first 40 ms of S1)

Against full L2:

| Configuration | Mean error | p99 error |
|---|---|---|
| L1 | 13.5% | 18.4% |
| L2t | 10.0% | 25.8% |
| L1 with DRAM at L2 | 11.0% | 14.6% |
| L1 with NoC at L2 | 17.5% | 25.0% |

- The biggest single L1 differences are T7_control (-44.8%, the RT CPU effect from section 2) and
  T5_plan (+29.0%).
- The effects do not add up neatly. With only the NoC at L2, T3_esdf and T6_estimate come out
  33.5% and 22.1% faster than full L2. With only the DRAM at L2, task means stay within 8.1% of
  full L2 apart from T7 and T5.
- With nine tasks in 40 ms, I read this as "L1 is within about 14% of the external simulators on <!-- src: results/s10.json#setup.tier_b_s1_window_s|scale=1000 --> <!-- src: results/s10.json#analysis_tier_b.acceptance.L1_mean_abs_err|pct -->
  S1 means", not as a scenario-wide number.

Microbenchmark mean absolute error against full L2: L1 20.9%, L2t 8.3%. <!-- src: results/s10.json#analysis_tier_b.microbench.L1.stats.mean_abs|pct --> <!-- src: results/s10.json#analysis_tier_b.microbench.L2t.stats.mean_abs|pct -->

## 4. NoC L1 before and after the core rework (core benchmarks)

The core worker reworked the L1 NoC: interleaved link calendars, VC-hold backpressure and fixed
per-packet latency. It validated the change against BookSim. Before this rework, my phase-B s10
had found the L1 NoC 2x pessimistic under contention.

**Synthetic traffic, BookSim trace mode as reference** (`core/bench/noc_validation.json`, seed 11,
20,000 cycles):
- Low-load packet latency, uniform 2-flit:

  | Model | Cycles | Error |
  |---|---|---|
  | BookSim | 13.53 | |
  | before | 9.69 | -28.4% |
  | after | 13.57 | +0.3% |

  The other five curves show the same pattern: before was 20% to 29% low, after is within 0.6%.
- Saturation (offered load where latency reaches 3x zero load), in flits/node/cycle: <!-- numcheck: ignore -->

  | Traffic | BookSim | before | after |
  |---|---|---|---|
  | uniform 8-flit | 0.614 | 0.315 | 0.724 |
  | transpose 8-flit | 0.311 | 0.308 | 0.311 |
  | DRAM hotspot 8-flit | 0.110 | 0.101 | 0.111 |

- Mean absolute latency error below BookSim's saturation point:

  | Traffic | before | after |
  |---|---|---|
  | uniform 8-flit | 2,839% (the old model saturated at half the load) | 4.7% |
  | transpose 8-flit | 20.8% | 1.0% |
  | DRAM hotspot 8-flit | 26.6% | 1.1% |
  | DRAM hotspot 2-flit | 42.4% | 5.4% |

**Per packet on the core's own traces, replayed through BookSim** (`core/bench/noc_closed_loop.json`,
`per_packet`):

| Case | Mean, before | Mean, after | p99, before | p99, after |
|---|---|---|---|---|
| all-to-DRAM hotspot | +106.6% | +38.0% | +80.7% | +2.4% |
| GEMM 1024x768x3072 | +15.3% | -6.7% | | |
| DMA stream | +2.1% | -0.4% | | |
| attention | -1.4% | -4.8% | | |

**Closed loop, S1 first 40 ms, L1 vs BookSim in the loop** (`s1_40ms`, excluding T7): <!-- src: results/s10.json#setup.tier_b_s1_window_s|scale=1000 -->
- Per-task error against BookSim in the loop, excluding T7, is in `s1_40ms.L1_before` and `L1_after` (`rel_err_vs_booksim`).
- Worst task: T8_safety, +11.3% both before and after. <!-- src: core/bench/noc_closed_loop.json#s1_40ms.L1_before.rel_err_vs_booksim.T8_safety|pct --> <!-- src: core/bench/noc_closed_loop.json#s1_40ms.L1_after.rel_err_vs_booksim.T8_safety|pct -->
- Cost: the new L1 NoC uses 0.385 CPU s for that window against 0.156 s before, about 2.5 times <!-- src: core/bench/noc_closed_loop.json#s1_40ms.L1_after.cpu_s --> <!-- src: core/bench/noc_closed_loop.json#s1_40ms.L1_before.cpu_s -->
  more. BookSim in the loop needs 605 CPU s. <!-- src: core/bench/noc_closed_loop.json#s1_40ms.L2_booksim.cpu_s -->

My own s10 trace replay on this build matches the core's numbers: hotspot NoC +38.0% mean and
+2.4% p99 (`trace_replay.cases["all_to_one_hotspot_6x4MB/study_L1"]`). <!-- src: results/s10.json#trace_replay.cases["all_to_one_hotspot_6x4MB/study_L1"].noc.rel_err_mean|pct --> <!-- src: results/s10.json#trace_replay.cases["all_to_one_hotspot_6x4MB/study_L1"].noc.rel_err_p99|pct -->

## 5. Per-request DRAM and NoC against the same traces (`trace_replay`)

I exported the core's L1 DRAM and NoC traces and replayed exactly those requests, open loop,
through Ramulator 2 and BookSim 2.

| Case | DRAM mean | DRAM p99 | NoC mean | NoC p99 |
|---|---|---|---|---|
| DMA stream 8 MiB | -5.5% | -40.2% | -0.4% | 0.0% |
| attention | +2.8% | -0.1% | -4.8% | 0.0% |
| GEMM 1024x768x3072 | -1.4% | -3.2% | -6.7% | -2.6% |
| all-to-DRAM hotspot | -28.2% | -31.4% | +38.0% | +2.4% |
| CPU 64 B misses | +91.1% | +20.3% | +3.6% | +0.9% |
| S1, first ten milliseconds | -80.6% | -81.1% | -22.6% | -19.1% | <!-- src: results/s10.json#trace_replay.cases["S1_first_10ms/study_L1"].dram.rel_err_mean|pct --> <!-- src: results/s10.json#trace_replay.cases["S1_first_10ms/study_L1"].dram.rel_err_p99|pct --> <!-- src: results/s10.json#trace_replay.cases["S1_first_10ms/study_L1"].noc.rel_err_mean|pct --> <!-- src: results/s10.json#trace_replay.cases["S1_first_10ms/study_L1"].noc.rel_err_p99|pct -->

Averaged over the six cases, the absolute error of the mean is 35.0% for DRAM and 12.7% for NoC; <!-- src: results/s10.json#trace_replay.summary["study_L1/dram"].mean_abs_rel_err_of_mean|pct --> <!-- src: results/s10.json#trace_replay.summary["study_L1/noc"].mean_abs_rel_err_of_mean|pct -->
for p99 it is 29.4% and 4.2%. <!-- src: results/s10.json#trace_replay.summary["study_L1/dram"].mean_abs_rel_err_of_p99|pct --> <!-- src: results/s10.json#trace_replay.summary["study_L1/noc"].mean_abs_rel_err_of_p99|pct -->

**DRAM:**
- The S1 gap is mostly an artifact of replaying open loop. Each 4 KB chunk reaches Ramulator as one burst of
  simultaneous 32 B requests with no backpressure, and it sustains only 10.1 GB/s on that <!-- src: calib/tables/dram_ramulator.json#setup.dram_tx_bytes --> <!-- src: results/s10.json#trace_replay.cases["S1_first_10ms/study_L1"].dram.ramulator_achieved_gbps -->
  trace.
- In closed loop, the DRAM-at-L2 configuration stays within 8.1% of full L2 on S1 task means,
  apart from T7 and T5 (section 3).
- The 64 B CPU-miss case is +91.1%. The L1 controller is more pessimistic than Ramulator for
  isolated 64 B row misses.

**General:** L0 issues no memory transactions, so there is no per-request L0 trace to compare.

## 6. CPU against gem5 (`cpu_vs_gem5`)

The workload graphs now carry gem5's dynamic instruction counts, and the core's CPU L2 charges
gem5's measured cycles per call for these kernels. The L2t column is therefore exact by
construction. The real tests are the L0/L1 column and the leave-one-out column. The app core is
compared with gem5 O3, the RT core with Minor.

| Kernel | L0 / L1 | L2t | Leave-one-out CPI |
|---|---|---|---|
| control_step, app core | +94.8% | +0.0% | +265.3% |
| estimate_step, app core | -53.6% | +0.0% | -48.6% |
| control_step, RT core | -43.3% | +0.0% | -3.6% |
| ssm_step, RT core | -44.9% | +0.0% | -7.8% |

- With the correct instruction counts, L0/L1 is wrong in both directions. Its fixed IPC
  assumptions (2.0 on the app core, 1.0 on the RT core) are close to neither control_step's
  O3 CPI of 0.35 nor estimate_step's 1.57.
- A single CPI per core class (leave-one-out) works for the RT core (-3.6% and -7.8%) but not for
  the app core.

## 7. Simulation speed (`speed`, S1, simulated seconds per CPU second, chip only)

| Configuration | Speed |
|---|---|
| analytic / L0 | 25.9 / 24.8 |
| interactive | 0.862 |
| L1 / L2t / study | 0.115 / 0.114 / 0.114 |
| memory_focus | 0.0803 |
| L1 with DRAM at L2 | 0.00194 |
| L1 with NoC at L2 | 6.48e-5 |
| full L2 | 6.21e-5 |

- These are slower than in my phase-B run on the older core. The core benchmark above shows the
  new NoC calendars cost about 2.5 times more CPU, and the machine was shared.
- The study mix reaches 11.2% mean error at 0.114 times real time. <!-- src: results/s10.json#analysis.scenarios["mix:study"].mean.mean_abs|pct --> <!-- src: results/s10.json#speed["A:mix:study"].sim_s_per_cpu_s -->
- The interactive mix is 7.6 times faster at 20.4%.

## 8. External sanity checks (`results/s15.json`)

### Orin-like configuration against public numbers (`orin_sanity`)

I ran three MLPerf Edge models on `orin_like.yaml` and compared them with NVIDIA's MLPerf v3.1
single-stream latencies for Jetson AGX Orin. The models are ResNet-50, RetinaNet (ResNeXt50-FPN
at 800x800) and BERT-Large (sequence length 384).

Before looking at any result, I fixed the mapping that one GPU kernel uses all 16 SMs
(`npu_max_tiles_per_op` = 16). Study mix:

| Model | Modeled | Public | Error |
|---|---|---|---|
| ResNet-50 | 0.364 ms | 0.64 ms | -43.1% | <!-- src: results/s15.json#orin_sanity.models.T90_resnet50.runs["gpu_wide_primary/study"].modeled_ms --> <!-- src: results/s15.json#orin_sanity.models.T90_resnet50.public_single_stream_ms --> <!-- src: results/s15.json#orin_sanity.models.T90_resnet50.runs["gpu_wide_primary/study"].rel_err|pct -->
| RetinaNet | 10.792 ms | 11.67 ms | -7.5% | <!-- src: results/s15.json#orin_sanity.models.T91_retinanet.runs["gpu_wide_primary/study"].modeled_ms --> <!-- src: results/s15.json#orin_sanity.models.T91_retinanet.public_single_stream_ms --> <!-- src: results/s15.json#orin_sanity.models.T91_retinanet.runs["gpu_wide_primary/study"].rel_err|pct -->
| BERT-Large | 5.977 ms | 5.71 ms | +4.7% | <!-- src: results/s15.json#orin_sanity.models.T92_bert_large.runs["gpu_wide_primary/study"].modeled_ms --> <!-- src: results/s15.json#orin_sanity.models.T92_bert_large.public_single_stream_ms --> <!-- src: results/s15.json#orin_sanity.models.T92_bert_large.runs["gpu_wide_primary/study"].rel_err|pct -->

- Two of three models fall within the 30% target in every mix.
- ResNet-50 is the miss. My untested guess is per-kernel launch and synchronization overhead at
  batch 1, which the model leaves out.
- With the VX-1 default of 4 tiles per layer, ResNet-50 lands at -0.6%, while RetinaNet and BERT go
  to +86.9% and +129.9%. That one mapping choice dominates the outcome, which is why I fixed it in
  advance and report both.

This is a sanity check of the approach, not hardware validation. Per-model public precision is
unverified.

### NPU model against SCALE-Sim (`npu_vs_scalesim`)

Over 2,097 conv and matmul ops in the five VX-1 NPU tasks, the core's L1 closed-form systolic
model differs from SCALE-Sim by a median of 26.2% per op (16.2% weighted by cycles).

- Cause: the core hides each weight-tile load behind streaming; SCALE-Sim's weight-stationary mode
  charges it on every fold.
- With weight-load overlap turned off, the median per-op error is 0.42% and the cycle-weighted <!-- src: results/s15.json#npu_vs_scalesim.overall.no_weight_load_overlap_diagnostic.per_op_abs_rel_err_median|pct -->
  error 0.29%. <!-- src: results/s15.json#npu_vs_scalesim.overall.no_weight_load_overlap_diagnostic.cycle_weighted_abs_rel_err|pct -->

| Task | Total cycle error |
|---|---|
| T11_speech | -24.2% |
| T4_vla | -14.9% |
| T8_safety | -12.6% |
| T2_detect | -3.0% |

T11's decoder is now explicit (530 ops). Its remaining gap is the same weight-load overlap effect
on many small M = 1 matmuls (median -33.0%). The shape-versus-MAC mismatch from my previous run is
gone.

The L2 path reproduces the tables. Running each task at NPU L2 with one tile per op, the shortest
op duration equals its table value: ratio 1.00 for T4_vla and T11_speech, 1.003 for T2_detect,
1.006 for T8_safety.

### DRAM L1 against Ramulator 2 on identical traces (`dram_vs_ramulator`)

These are the per-request results from section 5, read from `results/s10.json`.

## 9. What the models cannot capture yet

- **CPU at L1:** fixed IPC per core class. gem5 shows CPI from 0.26 to 1.57 across the scalar
  kernels on O3, so L1 errors on these kernels run from -54% to +95%. <!-- src: results/s10.json#cpu_vs_gem5.cases.estimate_step_cpu.levels.L1.rel_err_vs_gem5|pct --> <!-- src: results/s10.json#cpu_vs_gem5.cases.control_step_cpu.levels.L1.rel_err_vs_gem5|pct -->
- **L0 NoC and DMA:** contention is fluid load only, and drops in accuracy under heavy sharing
  (the NoC is the costliest component to run at L0).
- **L1 NPU:** weight loading is fully overlapped. That is optimistic against SCALE-Sim by about
  26% per op, mostly on small-M matmuls.
- **L1 DRAM:** pessimistic for isolated 64 B misses (+91.1% per request against Ramulator).
- **Tier B coverage:** only 40 ms of S1, because the external simulators in the loop are about <!-- src: results/s10.json#setup.tier_b_s1_window_s|scale=1000 -->
  16,000 times slower than real time.
