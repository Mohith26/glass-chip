# glasschip-surrogate

This is the design-space exploration layer of Glass Chip. I sample the VX-1 design space, run every sample through the full SoC model, train a fast surrogate on the results, wrap every prediction in a conformal interval, and export the whole thing to one ONNX file that runs in the browser so the design-space sliders update instantly.

VX-1 is a fictional reference SoC sized for a mid-range humanoid or arm controller. Nothing here describes a real chip. The area numbers are a rough proxy. The low-voltage arithmetic factor is hypothetical: any value above 1x represents the class of techniques Velaura claims for its Titan Core, not their technology.

## Results

All numbers below come from `results/s11.json` and `results/s12.json`. The dataset and every model were built against core build 3e9cf32325432d0f.

- Dataset: 3,600 chip-only samples (3,000 Latin hypercube plus 600 active learning), 0 failures, 20 knobs, 4.8 CPU-hours of simulation. <!-- src: results/s11.json#dataset.n_total --> <!-- src: results/s11.json#dataset.n_lhs --> <!-- src: results/s11.json#dataset.n_active_learning --> <!-- src: results/s11.json#dataset.n_failed --> <!-- src: results/s11.json#dataset.n_inputs --> <!-- src: results/s11.json#dataset.total_cpu_hours -->
  The Latin hypercube phase took 102 minutes from first to last finish on a shared machine (up to 10 worker processes, other jobs running); I stopped at 3,000 rather than 5,000 because each 2 s settled sample costs a median 4.3 CPU seconds. <!-- src: results/s11.json#dataset.by_phase.lhs.first_to_last_finish_min --> <!-- src: results/s11.json#dataset.n_lhs --> <!-- src: results/s11.json#dataset.sim_seconds_per_sample --> <!-- src: results/s11.json#dataset.sample_cpu_s_median --> <!-- numcheck: ignore (5,000 is the spec's target, not a result) -->
- Run length: on the VX-1 default design, energy per robot-hour from a 2 s run with a 1 s burn-in is within 0.43 percent of a 6 s run for P4 and within 0.01 percent for P0 and P3. <!-- src: results/s11.json#run_length_check.rel_diff_energy_2s_vs_6s.pace_to_deadline|pct --> <!-- src: results/s11.json#run_length_check.rel_diff_energy_2s_vs_6s.nominal|pct --> <!-- src: results/s11.json#run_length_check.rel_diff_energy_2s_vs_6s.race_to_idle|pct --> <!-- src: results/s11.json#dataset.sim_seconds_per_sample --> <!-- src: results/s11.json#dataset.warmup_s -->
- Coverage at 90 percent nominal, held-out test split (450 designs): per-output empirical coverage ranges from 0.868 to 0.933 with a median of 0.902 over 61 non-constant outputs. <!-- src: results/s11.json#summary.nominal|pct --> <!-- src: results/s11.json#dataset.split_sizes.test --> <!-- src: results/s11.json#summary.coverage_min --> <!-- src: results/s11.json#summary.coverage_max --> <!-- src: results/s11.json#summary.coverage_median --> <!-- src: results/s11.json#summary.n_outputs -->
  For 59 of 61 outputs the nominal level lies inside their exact binomial (Clopper-Pearson) interval, and all 61 fall inside the band that also accounts for the calibration draw (0.861 to 0.939). <!-- src: results/s11.json#summary.n_nominal_inside_ci95 --> <!-- src: results/s11.json#summary.n_outputs --> <!-- src: results/s11.json#summary.n_inside_theory_band --> <!-- src: results/s11.json#summary.single_split_band_95_theory[0] --> <!-- src: results/s11.json#summary.single_split_band_95_theory[1] -->
  Over repeated random re-splits of the held-out pool, the mean coverage across outputs is 0.901 (lowest output 0.897, highest 0.910), which is the quantity split conformal guarantees. <!-- src: results/s11.json#summary.resplit.mean_of_means --> <!-- src: results/s11.json#summary.resplit.min_mean --> <!-- src: results/s11.json#summary.resplit.max_mean -->
- Accuracy, test split: energy per robot-hour mean absolute error 0.18 Wh with a median 90 percent interval width of 0.81 Wh. <!-- src: results/s11.json#headline.energy_per_robot_hour_wh.test_mae --> <!-- src: results/s11.json#summary.nominal|pct --> <!-- src: results/s11.json#headline.energy_per_robot_hour_wh.median_width_90 -->
  Median relative error on p99 response time is 7.9 percent for T4 VLA, 14.8 percent for T2 detection and 15.7 percent for T8 safety. <!-- src: results/s11.json#headline["T4_vla.p99_ms"].test_median_rel_err|pct --> <!-- src: results/s11.json#headline["T2_detect.p99_ms"].test_median_rel_err|pct --> <!-- src: results/s11.json#headline["T8_safety.p99_ms"].test_median_rel_err|pct -->
  LightGBM won 60 of 63 fitted outputs on validation error (the area proxy is closed form). <!-- src: results/s11.json#summary.n_chosen_lgb --> <!-- src: results/s11.json#speed.n_outputs_python -->
- Where the surrogate defers: the camera-to-plan chain is bimodal (either the VLA keeps up or no plan finishes in the window), so its interval is flagged "uncertain, run full model" for every test design; T4's p99 interval is flagged for 61 percent of test designs. <!-- src: results/s11.json#headline["T4_vla.p99_ms"].frac_uncertain_90|pct -->
- Speed: the browser model (a 4.4 MiB ONNX file with 36 output tensors: each learned output and the area proxy, with its interval) takes 0.33 ms per slider update on average with onnxruntime-web's wasm backend under Node. <!-- src: results/s11.json#speed.onnx_bytes|scale=9.5367431640625e-07 --> <!-- src: results/s11.json#speed.onnxruntime_web_wasm_node.n_output_tensors --> <!-- src: results/s11.json#speed.onnxruntime_web_wasm_node.ms_per_run -->
  The p95 is 0.57 ms and the worst run 1.8 ms, against a median 4.3 CPU seconds for one full-model design point. <!-- src: results/s11.json#speed.onnxruntime_web_wasm_node.p95_ms --> <!-- src: results/s11.json#speed.onnxruntime_web_wasm_node.max_ms --> <!-- src: results/s11.json#speed.full_sim_cpu_s_per_design_median -->
- Distribution shift, measured on purpose: against the all-L1 study mix the 90 percent intervals cover 80 to 92.5 percent of 40 held-out designs depending on the output. <!-- src: results/s11.json#summary.nominal|pct --> <!-- src: results/s11.json#fidelity_check.per_output["T8_safety.p99_ms"].surrogate_coverage_of_study_truth_90|pct --> <!-- src: results/s11.json#fidelity_check.per_output["T2_detect.p99_ms"].surrogate_coverage_of_study_truth_90|pct --> <!-- src: results/s11.json#fidelity_check.n_designs -->
  Inside closed-loop co-sim runs (20 s, scenario events mid-run) they cover 86 to 96 percent. Neither setting is covered by the guarantee. <!-- src: results/s11.json#robot_subset.duration_s --> <!-- src: results/s11.json#cosim_shift["energy_per_robot_hour_wh"].coverage_90|pct --> <!-- src: results/s11.json#cosim_shift["T2_detect.p99_ms"].coverage_90|pct -->
- Robot success (160 co-simulated designs, 20 s each): pick-rate test error 0.018 against 0.049 for predicting the mean. <!-- src: results/s11.json#robot_subset.n --> <!-- src: results/s11.json#robot_subset.duration_s --> <!-- src: results/s11.json#robot_subset.report.pick_rate.test_mae --> <!-- src: results/s11.json#robot_subset.report.pick_rate.baseline_mae_predict_mean -->
  Grasp success rate averaged 0.97 and the model does no better than the mean, so I do not use it for decisions. With 32 test designs, the coverage intervals are wide. <!-- src: results/s11.json#robot_subset.report.grasp_success_rate.y_mean --> <!-- src: results/s11.json#robot_subset.n_test -->
- Design-space exploration over 60,000 candidates on S1, S2, S3 and S6: 10,678 meet the limits. <!-- src: results/s12.json#setup.n_candidates --> <!-- src: results/s12.json#counts.feasible -->
  The limits are 90 percent upper bounds below 0.5 percent safety misses and 5 percent T4/T2 misses on every workload. <!-- src: results/s11.json#summary.nominal|pct --> <!-- src: results/s12.json#setup.safety_limit|pct --> <!-- src: results/s12.json#setup.rt_limit|pct -->
  The three recommendations were re-run with the full model on all four workloads at 2 s and 6 s. All had zero safety, T4 and T2 misses. Every interval check landed inside its 90 percent interval except one: the lowest-energy design's T4 p99 on S6 (90.9 ms against an upper bound of 90.0 ms). <!-- src: results/s11.json#summary.nominal|pct --> <!-- src: results/s12.json#recommended[0].verified.interval_checks[15].full_model --> <!-- src: results/s12.json#recommended[0].verified.interval_checks[15].hi -->

| Design | NPU | Policy | Area proxy | Energy, surrogate | Energy, full model (2 s / 6 s) |
|---|---|---|---|---|---|
| Lowest energy | 4 tiles x 128, NPU Vmin 0.468 V | P3 race to idle, EDF, QoS, zero-copy | 50.8 mm2 | 2.61 Wh | 2.67 / 2.67 Wh | <!-- src: results/s12.json#recommended[0].design.npu_array --> <!-- src: results/s12.json#recommended[0].predicted.area_mm2 --> <!-- src: results/s12.json#recommended[0].predicted.energy_wh_mean --> <!-- src: results/s12.json#recommended[0].verified.energy_wh_mean_2s --> <!-- src: results/s12.json#recommended[0].verified.energy_wh_mean_6s --> <!-- src: results/s12.json#recommended[0].design.vmin_npu -->
| Smallest area | 3 tiles x 88 | P3 race to idle, rate monotonic, QoS, zero-copy | 36.6 mm2 | 3.39 Wh | 3.19 / 3.19 Wh | <!-- src: results/s12.json#recommended[1].design.npu_array --> <!-- src: results/s12.json#recommended[1].predicted.area_mm2 --> <!-- src: results/s12.json#recommended[1].predicted.energy_wh_mean --> <!-- src: results/s12.json#recommended[1].verified.energy_wh_mean_2s --> <!-- src: results/s12.json#recommended[1].verified.energy_wh_mean_6s -->
| Balanced | 4 tiles x 120, 2 MB system cache | P3 race to idle, EDF-MC, QoS, zero-copy | 40.1 mm2 | 2.85 Wh | 2.84 / 2.84 Wh | <!-- src: results/s12.json#recommended[2].design.npu_array --> <!-- src: results/s12.json#recommended[2].predicted.area_mm2 --> <!-- src: results/s12.json#recommended[2].predicted.energy_wh_mean --> <!-- src: results/s12.json#recommended[2].verified.energy_wh_mean_2s --> <!-- src: results/s12.json#recommended[2].verified.energy_wh_mean_6s -->
| VX-1 default | 4 tiles x 64 | P4, EDF | 47.9 mm2 | 5.14 Wh | 5.51 / 5.51 Wh | <!-- src: results/s12.json#baseline_vx1_p4_edf.design.npu_array --> <!-- src: results/s12.json#baseline_vx1_p4_edf.area_mm2 --> <!-- src: results/s12.json#baseline_vx1_p4_edf.predicted_energy_wh_mean --> <!-- src: results/s12.json#baseline_vx1_p4_edf.verified_energy_wh_mean_2s --> <!-- src: results/s12.json#baseline_vx1_p4_edf.verified_energy_wh_mean_6s -->

All three winners run a wide NPU at reduced voltage, use about 72 to 78 GB/s of DRAM bandwidth, turn QoS on and pick race to idle. <!-- src: results/s12.json#recommended[0].design.dram_gbps --> <!-- src: results/s12.json#recommended[1].design.dram_gbps -->
The VX-1 default row keeps its Vmin floors at 0.75 V, so P4 cannot scale below nominal there; it is a reference point, not a tuned P4 design. <!-- src: results/s12.json#baseline_vx1_p4_edf.design.vmin_npu -->
With the hypothetical low-voltage arithmetic factor at 2x the lowest-energy design is predicted at 2.39 Wh, and the prediction at 4x is not lower, which says the surrogate cannot resolve that knob at this energy level. <!-- src: results/s12.json#hypothetical_low_voltage_arith[0].lv_arith_factor --> <!-- src: results/s12.json#hypothetical_low_voltage_arith[0].predicted_energy_wh_mean --> <!-- src: results/s12.json#hypothetical_low_voltage_arith[1].lv_arith_factor -->

## What I sample

| Knob | Range | Notes |
|---|---|---|
| NPU tiles | 2 to 8 | extra tiles share the four NPU mesh nodes |
| NPU array | 32 to 128 (steps of 8) | square systolic array; MACs per cycle per tile = array squared |
| NPU SRAM per tile | 0.5 to 4 MB | |
| System cache | 2 to 16 MB | |
| Cache partitioning | off / on | ways: safety 2, real-time 10, best effort 4 |
| QoS arbitration | off / on | NoC and DRAM priority by QoS class |
| NoC link width | 128 / 256 / 512 bits | topology is fixed to the 4x4 mesh: the core does not model ring or express links, so I dropped that knob |
| DRAM bandwidth | 64 to 204 GB/s | data rate on the fixed 128-bit bus | <!-- numcheck: ignore -->
| App CPU cores | 4 to 16 | split across the two clusters |
| Scheduler | FIFO, rate monotonic, EDF, mixed-criticality EDF | |
| Power policy | P0 nominal, P3 race to idle, P4 pace to deadline | P6 uses this surrogate, so it is not a knob of it |
| Vmin per domain | NPU 0.45 to 0.75 V, CPU, SIMT, DSP 0.50 to 0.75 V | static policies: a static undervolt when below 0.75 V; P4: the DVFS floor |
| Low-voltage arithmetic factor | 1x to 4x, 40 percent of samples at exactly 1x | hypothetical |
| Transport | copy / zero-copy | |
| Workload | S1, S2, S3, S6 as steady-state task sets | S3 runs the escalated 5 ms safety monitor plus a replan burst each second |
| Recording bitrate | 0.2 to 48 Gbit/s, log scale | stands in for S7 |
| Ambient | 25 to 50 C | stands in for S5 |

Outputs per sample: p50, p95 and p99 response time and miss rate for 12 tasks, three end-to-end chains (camera to plan, human detection to control, camera to detection), worst safety-task and worst real-time miss rate, average and peak power, energy per robot-hour, peak junction temperature, and the area proxy. Robot success (grasp success rate, pick rate) comes from a separate co-simulated subset. <!-- numcheck: ignore -->

## How the dataset is built

- `chiprun.py` drives `glasschip_core` directly with the same release rules as the co-sim bridge (periodic releases on the 1 ms grid, detection chained to camera ingest, planning triggered from VLA completions at the scenario's 2 Hz rate) and the power manager stepping every 1 ms. There is no robot physics in these runs, which is what makes thousands of samples affordable.
- Each sample is 2.0 simulated seconds. The first second is a burn-in: jobs released in it are excluded from the statistics, and the power manager's thermal "settle" step jumps the die to its steady-state temperature for the power seen so far and restarts the energy accounting there. The burn-in matters for P4: its predictor holds a task's domains at or above nominal until it has seen enough completions of that task, so in a short run without a burn-in P4 looks like P0. s11 checks this by comparing 2 s and 6 s runs of the VX-1 default design under P0, P3 and P4 (`run_length_check`). <!-- src: results/s11.json#dataset.sim_seconds_per_sample -->
- The fidelity mix is `fidelity_dse.yaml`, which is the interactive mix (CPU, SIMT and DSP analytic; NPU, NoC, cache and DRAM transaction level). The all-L1 study mix costs several times more per simulated second, which would have put a few thousand samples out of reach on a shared machine capped at 4 processes. s11 measures the gap to the study mix on 40 held-out designs.
- Tail quantiles of slow tasks come from few jobs per run (T4 releases about 8 times in the 1 s window), so a p99 for T4 is close to the per-run maximum. When a task in a workload finishes no job in the window, I censor its latency at the window length and its miss rate at 1.
- The dataset pins a snapshot of the core module, the SoC config, the power parameters, the power package and the workload graphs (`data/surrogate/snapshot/`), so rebuilds by other parts of the project cannot mix two models inside one dataset. Every row carries the core's own build id, the snapshot hashes, the graph hash and the git commit.
- `dataset.py lhs` draws a Latin hypercube; `dataset.py active` adds active-learning points where the current surrogate's interval is widest among intervals that straddle a deadline (or the 1 percent miss line). Active-learning points are used for training only, never for calibration or testing, because they are not drawn from the same distribution. Generation is resumable: finished sample ids are skipped, and `touch data/surrogate/STOP` stops a run cleanly.
- `robot_subset.py` runs a smaller Latin hypercube through the full closed loop (`studies/runner.py`: MuJoCo, the core and the power manager, real scenario files with their events) for 20 simulated seconds each and records grasp success and pick rate. These runs use the same frozen core build as the snapshot.

## Models

`train.py` fits two candidates per output and keeps the one with the lower validation error: LightGBM on the raw knobs plus peak NPU MACs per cycle, and a small MLP (two hidden layers of 64) on scaled knobs with one-hot categorical knobs. Latencies are modeled in log space and miss rates as log(rate + 0.001), so errors are relative. For latency tails (p95, p99 and chain quantiles) I also fit LightGBM quantile models at 5 and 95 percent for conformalized quantile regression. The area proxy is closed form and is not learned. <!-- numcheck: ignore -->

The Latin-hypercube samples are split 60/10/15/15 into train, validation, calibration and test with a fixed seed. After model selection the chosen model is refit on train plus validation.

## Conformal intervals: what they guarantee and what they do not

`conformal.py` implements split conformal prediction (absolute residual in model space) and conformalized quantile regression (Romano, Patterson and Candes, NeurIPS 2019). For each output I compute q-hat on the calibration split at 80, 90, 95 and 99 percent coverage, and I report the empirical coverage on the separate test split with an exact Clopper-Pearson 95 percent interval. <!-- numcheck: ignore -->

What it guarantees: if a new design point is exchangeable with the calibration points (drawn the same way, simulated by the same model), the probability that its true value lands inside the interval is at least the nominal level. The probability is over both the new point and the calibration draw. This is marginal coverage.

What it does not guarantee:
- Coverage for a specific design. Some regions of the space get intervals that are too narrow and others too wide; only the average is controlled.
- Anything outside the sampled ranges, or for a different simulator: a new core build, a different fidelity mix, longer runs, or the closed-loop co-sim's release process. s11 measures two such shifts on purpose.
- Independence across outputs. Each output has its own interval; there is no joint guarantee that all of them hold at once.

Many miss rates are exactly zero for most designs. Ties make the coverage of those outputs higher than nominal, which split conformal allows (it guarantees at least the nominal level, and at most nominal plus 1/(n+1) only when there are no ties).

Fallback rule: an interval is marked "uncertain, run full model" when its width exceeds 0.5 x the task's deadline for latencies, or 0.05 for miss rates. The browser panel can show that flag from `meta.json`, and P6 falls back to its cycle estimate when it fires. <!-- numcheck: ignore -->

## Browser export

`export_onnx.py` builds one ONNX graph by hand with `onnx.helper`: the derived feature, every LightGBM model as a single `ai.onnx.ml` TreeEnsembleRegressor with one target per model, MLPs as MatMul, Add and Relu, the q-hat offsets, the CQR clamping, the inverse transforms and the closed-form area. Input `x` is float32 [N, 20] in the order of `dataset.INPUTS`; each output `o` comes with `o__lo` and `o__hi`. I check it against the Python models (`max relative difference` is in the float32 rounding range) and time it with onnxruntime-web's wasm backend under Node (`bench_ortweb.mjs`). `viz/public/surrogate/meta.json` follows the shape `viz/src/dse/surrogate.ts` reads, plus input ranges, labels for categorical codes, q-hat, achieved test coverage and provenance per output, the Pareto points from s12, and the three recommended designs. <!-- numcheck: ignore -->

## P6 support

`p6.py` exposes `SurrogateFinishBound`, the callable that the P6 policy (`power/policies/pace.py`) takes as `surrogate`. At construction it precomputes, for every task and power domain on a voltage grid, the conformal upper bound on the task's p95 response time (p99 at 95 percent for safety tasks) when that domain is held at that voltage. A call is then `now + (1 - frac_done) x R_hi(task, domain, V(freq))`, a lookup plus interpolation. It falls back to the cycle estimate (`est_remaining_cycles / freq x 1.25`) for tasks or domains the surrogate does not cover, outside the sampled voltage range, or when the interval is flagged uncertain. This is a calibrated heuristic, not a per-job guarantee: the coverage is marginal over designs and describes a whole run's tail, and holding a voltage for a run is not the same as switching every millisecond. <!-- numcheck: ignore -->

```python
from power.manager import PowerManager
from power.params import load_policy
from surrogate.p6 import SurrogateFinishBound

bound = SurrogateFinishBound(scenario="S1")          # loads data/surrogate/bundle.pkl
pm = PowerManager(load_policy("P6"), soc, surrogate=bound, task_info=...)
...
print(bound.stats())                                 # calls and how often it fell back
```

## Run it

```
.venv/bin/python -m surrogate.dataset lhs --n 3000 --workers 4       # dataset (resumable)
.venv/bin/python -m surrogate.train                                   # models + conformal bundle
.venv/bin/python -m surrogate.dataset active --n 600 --round 1 --workers 4   # active learning, then train again
.venv/bin/python -m surrogate.robot_subset run --n 160 --workers 4    # co-simulated robot subset
.venv/bin/python -m surrogate.robot_subset fit
.venv/bin/python -m surrogate.export_onnx                             # viz/public/surrogate/
.venv/bin/python studies/s11_surrogate_quality.py
.venv/bin/python studies/s12_dse.py
.venv/bin/python -m surrogate.export_onnx                             # again, to pick up the s12 Pareto points
.venv/bin/python -m pytest surrogate/tests -q
```

## Area proxy sources

The area model is a rough proxy for ranking designs, built from public density figures and labeled assumptions (`dataset.AREA`):
- SRAM: 0.021 um^2 high-density bitcell on TSMC's 5 nm platform (Yeap et al., IEDM 2019), with an assumed 50 percent macro efficiency.
- MACs: the TPUv1 matrix unit (65,536 8-bit MACs, about 24 percent of a die under 331 mm^2 at 28 nm; Jouppi et al., ISCA 2017), scaled by an assumed 12x logic density gain to a 5 nm class node.
- App CPU cores: 1.33 mm^2 per Cortex-A78-class core, an assumption. Arm publishes only relative figures (the A78 is about 5 percent smaller than the A77), so this one has no published seed.
- NoC routers, DRAM PHY and the fixed blocks are assumptions with no published seed.
