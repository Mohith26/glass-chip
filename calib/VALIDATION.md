# Validation summary

This page summarizes two studies: `studies/s10_cross_fidelity.py` (output `results/s10.json`)
and `studies/s15_external_sanity.py` (output `results/s15.json`). Every number below is copied
from those files, and I name the JSON path for each group. Accuracy here means agreement with my
own reference models (SCALE-Sim, gem5, Ramulator 2, BookSim 2) and, for s15, with public
benchmark numbers. None of it is agreement with silicon. VX-1 is a fictional SoC.

## 1. How I measured

- Fidelity configurations: all-L0, all-L1, and "L2t". L2t puts the NPU at L2 (SCALE-Sim cycle
  tables) and the CPU at L2 (gem5 CPI tables), with NoC and DRAM at L1. I also ran
  one-component swaps on the L1 base and the four named mixes in `configs/fidelity/`.
- Error is (config minus reference) divided by reference. I compute it on each task's mean and
  p99 response time, then average the absolute values over every (scenario, task) pair.
- Tier A: microbenchmarks plus scenarios S1-S8, chip only, policy P0. Windows are S1 2.0 s,
  S2 5.5 s and S3-S8 8.5 s (`setup.scenario_windows_s`). The reference is L2t. That gives 87
  scenario-task pairs.
- Tier B: the core runs Ramulator 2 and BookSim 2 in-process as its DRAM and NoC L2. That path is
  about 16,000 times slower than real time (BookSim on: 6.18e-5 simulated s per CPU s), so tier B
  covers the microbenchmarks plus only the first 40 ms of S1. The reference is full L2
  (`configs/fidelity/reference.yaml`). Only 9 tasks finish inside 40 ms, and T4_vla is not one of
  them. Treat tier B as a spot check, not a scenario-wide result.
- The core reports the RT core as L0 in `info()` for every configuration. Its L2 CPI for the RT
  core follows the `cpu` level, so the "cpu" rows below include the RT core.

## 2. Acceptance targets (spec 11.2), tier A (`analysis.acceptance`)

| Target | Measured | Result |
|---|---|---|
| L1 within 10% of L2 on mean task latency | 11.6% mean absolute error | missed |
| L1 within 20% of L2 on p99 | 17.3% | met |
| L0 within 30% of L2 on means | 26.9% | met |

55.2% of the L1 task means fall within 10%, and 85.1% within 30%. For L0 the same shares are
25.3% and 59.8% (`analysis.scenarios.L1`, `.L0`).

Why L1 misses on the mean. The swaps locate the error. Moving only the CPU to L2 cuts the L1 mean
error from 11.6% to 2.8%. Moving only the NPU to L2 cuts it to 10.5%, and the p99 error to 13.8%
(`analysis.scenarios["L1+cpu@L2"]`, `["L1+npu@L2"]`). The largest single errors come from two
places:

- The RT core. T7_control is 38.7% to 41.5% too fast at L1 in S1, S2, S6, S7 and S8
  (`analysis.worst.L1`).
  L1 assumes the in-order RT core sustains an IPC of 1.0, but gem5's MinorCPU runs control_step
  at a CPI of 1.77.
- Two S6 tasks: T14_ota at -45.7% and T11_speech at -40.2%.

Where L0 loses accuracy:

- T5_plan comes out 55% to 59% too fast in every scenario. The L0 SIMT model assumes a fixed 60%
  efficiency. L1 derives efficiency from thread occupancy, and the cuRobo-style kernels in T5 sit
  below that.
- T1_ingest_overhead in S6 is 102.4% too slow.
- Of the single components, the NoC costs the most when dropped to L0: 24.1% mean error, against
  15.3% to 21.2% for NPU, CPU and SIMT (`analysis.scenarios["L1+*@L0"]`).

Named mixes against L2t:

| Mix | Mean error | p99 error |
|---|---|---|
| study | 10.5% | 13.8% |
| memory_focus | 15.6% | 29.9% |
| interactive | 23.7% | 38.0% |
| analytic | 26.9% | 84.3% |

Microbenchmarks (26 cases, single job each; `analysis.microbench`): L1 14.0% mean absolute error
(median 3.0%), study 3.0%, L0 130.8% (median 42.9%).

## 3. Tier B: Ramulator 2 and BookSim 2 in the loop (`analysis_tier_b`)

On the first 40 ms of S1 against full L2:

| Configuration | Mean error | p99 error |
|---|---|---|
| L1 | 25.8% | 35.0% |
| L2t | 20.0% | 22.8% |
| L1 with DRAM at L2 | 27.6% | 38.9% |
| L1 with NoC at L2 | 9.7% | 8.9% |

So the NoC model drives most of the gap, and the DRAM model hardly matters at the job level:
Ramulator in the loop moves the S1 task means by about 6% or less.

The clearest single case is T1_ingest_overhead: 3.26 ms at L1, 1.54 ms at full L2, 1.59 ms with
only the NoC at L2. The L1 NoC reserves a whole packet train on each link, which is pessimistic
when several streams share links.

On the microbenchmarks, the mean absolute error against full L2 is 17.3% for L1 and 8.7% for L2t.

## 4. Per-request DRAM and NoC against the same traces (`trace_replay`)

I exported the core's L1 DRAM and NoC traces and replayed exactly those requests open loop through
Ramulator 2 and BookSim 2.

| Case | DRAM mean | DRAM p99 | NoC mean | NoC p99 |
|---|---|---|---|---|
| DMA stream 8 MiB | -9.6% | -40.6% | +2.1% | -0.2% |
| attention | -2.1% | -7.7% | -1.4% | 0.0% |
| GEMM 1024x768x3072 | -10.5% | -12.5% | +15.3% | +19.8% |
| all-to-DRAM hotspot | -21.4% | -35.6% | +106.6% | +80.7% |
| CPU 64 B misses | +67.9% | +16.0% | +15.7% | +301.8% |
| S1, first 10 ms | -85.7% | -83.9% | +6.0% | -0.2% |

Averaged over the six cases, the absolute error of the mean is 32.9% for DRAM and 24.5% for NoC.

What this shows:

- The L1 NoC matches BookSim on streaming traffic (+2.1% mean on the DMA stream) and is about
  2x pessimistic under the hotspot. That agrees with the closed-loop result in section 3.
- The S1 DRAM gap is mostly an artifact of open-loop replay. Each 4 KB chunk arrives as 128
  simultaneous 32 B requests, Ramulator queues them all without backpressure, and it sustains only
  9.9 GB/s on that trace.
- Closed loop is the fairer test, and there Ramulator changes S1 task latency only slightly
  (section 3).
- L0 issues no transactions, so there is no per-request L0 trace to compare.

## 5. CPU against gem5 (`cpu_vs_gem5`)

Op compute cycles in the core compared with gem5 cycles per call. The app core is compared with
O3, the RT core with Minor.

| Kernel | L0 / L1 | Core L2 (in-sample) | L2 leave-one-out |
|---|---|---|---|
| control_step, app core | +92.8% | +0.0% | +261.7% |
| estimate_step, app core | +5.6% | +0.0% | +17.1% |
| control_step, RT core | -43.9% | +0.0% | -4.6% |
| ssm_step, RT core | -15.2% | +0.0% | +42.1% |

- The core's CPU L2 now charges gem5's cycles per call for these matched kernels, so it is exact
  by construction. My first run multiplied the graph's static instruction count by gem5's CPI.
  That over-predicted estimate_step by 128% and ssm_step by 54%, because the static counts include
  both sides of every branch (12,436 and 76,982 instructions where gem5 executes 5,461 and 49,971).
  The core worker fixed it and I reran everything.
- gem5 O3 CPI ranges from 0.26 to 1.57 across the five scalar kernels. A single CPI per core class
  generalizes poorly: the leave-one-out row is off by +262% on control_step.

## 6. Simulation speed (`speed`, S1, simulated seconds per CPU second, chip only)

| Configuration | Speed |
|---|---|
| analytic / L0 | 21.7 / 21.9 |
| interactive | 1.27 |
| study | 0.254 |
| L1 | 0.258 |
| L2t | 0.254 |
| memory_focus | 0.205 |
| L1 with DRAM at L2 | 0.00198 |
| L1 with NoC at L2 | 6.42e-5 |
| full L2 | 6.18e-5 |

I report CPU time because other jobs were sharing the machine; wall-clock numbers are in the same
JSON. The study mix gets within 10.5% of L2t on means at 0.254x real time. The interactive mix is
5x faster at 23.7% error.

## 7. External sanity checks (`results/s15.json`)

### Orin-like configuration against public numbers (`orin_sanity`)

I ran ResNet-50, RetinaNet (ResNeXt50-FPN, 800x800) and BERT-Large (sequence length 384) on
`orin_like.yaml` and compared with NVIDIA's MLPerf v3.1 single-stream latencies for Jetson AGX
Orin.

Before looking at any result, I fixed the mapping that one GPU kernel uses all 16 SMs
(`npu_max_tiles_per_op` = 16). With that mapping, the study mix gives:

| Model | Modeled | Public | Error |
|---|---|---|---|
| ResNet-50 | 0.435 ms | 0.64 ms | -32.0% |
| RetinaNet | 11.654 ms | 11.67 ms | -0.1% |
| BERT-Large | 6.426 ms | 5.71 ms | +12.5% |

Two of three models fall within the 30% target in every mix. ResNet-50 is the miss. My guess is
that at batch 1 a GPU pays per-kernel launch and synchronization costs that my NPU-style model
leaves out, but I have not tested that.

With the VX-1 default of 4 tiles per layer, ResNet-50 lands at +7.3% while RetinaNet and BERT go
to +92.6% and +133% (study mix). The outcome depends heavily on that one mapping choice, which is
why I fixed it in advance and report both.

This is a sanity check of the modeling approach, not validation of any hardware. The public
precision per model is unverified.

### NPU model against SCALE-Sim (`npu_vs_scalesim`)

Across 1,614 conv and matmul ops in the five VX-1 NPU tasks, the core's L1 closed-form systolic
model differs from SCALE-Sim by a median of 26.2% per op (21.2% weighted by cycles).

Almost all of that comes from one assumption. The core hides each weight-tile load behind
streaming; SCALE-Sim's weight-stationary mode charges it every fold. With weight-load overlap
turned off, the median per-op error falls to 0.42% and the 90th percentile to 0.56% (8.5% cycle
weighted).

T11_speech totals -64%, and the cause is a graph encoding problem rather than the model. Each
decoder token step is one matmul whose shape is 1x384x384, but whose MAC count covers four layers
and cross-attention (223 times the shape). SCALE-Sim scales by the MAC count; the core's L1 uses
the shape.

The L2 path reproduces the tables. Running each task at NPU L2 with one tile per op, the shortest
op duration equals its table value (ratio 1.00 for T4_vla and T11_speech, 1.003 for T2_detect,
1.006 for T8_safety). Longer ops include memory stalls on top.

### DRAM L1 against Ramulator 2 on identical traces (`dram_vs_ramulator`)

These are the per-request results from section 4, read from `results/s10.json`.

## 8. What the models cannot capture yet

- L1 RT and app CPU timing uses fixed IPC assumptions. gem5 shows a wide CPI spread across
  kernels (0.26 to 1.57 on O3).
- The L1 NoC is pessimistic under link sharing because it reserves packet trains.
- L0 SIMT efficiency is a constant, so occupancy-limited kernels come out too fast.
- Graph ops whose shape does not match their MAC count (T11 decoder steps) are under-counted by
  the L1 NPU model.
- Tier B covers only 40 ms of S1, because BookSim in the loop is about 16,000 times slower than
  real time.
