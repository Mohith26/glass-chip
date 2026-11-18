# glasschip-surrogate

This is the design-space exploration layer of Glass Chip. I sample the VX-1 design space, run every sample through the full SoC model, train a fast surrogate on the results, wrap every prediction in a conformal interval, and export the whole thing to one ONNX file that runs in the browser so the design-space sliders update instantly.

VX-1 is a fictional reference SoC sized for a mid-range humanoid or arm controller. Nothing here describes a real chip. The area numbers are a rough proxy. The low-voltage arithmetic factor is hypothetical: any value above 1x represents the class of techniques Velaura claims for its Titan Core, not their technology.

## Results

All numbers below come from `results/s11.json` and `results/s12.json`.

- Dataset: 5,600 chip-only samples (5,000 Latin hypercube plus 600 active learning), 0 failures, 20 knobs, 2.98 CPU-hours of simulation. The Latin hypercube took 71.9 minutes of wall time on 10 worker processes on a shared machine. <!-- src: results/s11.json#dataset -->
- Coverage at 90 percent nominal, held-out test split (750 designs): per-output empirical coverage ranges from 0.856 to 0.966 with a median of 0.893 over 61 non-constant outputs. 54 of 61 fall inside the 95 percent band that accounts for both the calibration draw and the test sample (0.870 to 0.930). Over 200 random re-splits of the held-out pool, the mean coverage across outputs is 0.902 (lowest output 0.897), which is the quantity split conformal guarantees. <!-- src: results/s11.json#summary -->
- Accuracy, test split: energy per robot-hour mean absolute error 0.110 Wh with a median 90 percent interval width of 0.479 Wh; T4 VLA p99 median relative error 4.0 percent; T2 detection p99 11.3 percent; T8 safety p99 10.1 percent. LightGBM won 60 of 63 outputs on validation error, the MLP won 3 (including peak temperature). <!-- src: results/s11.json#headline -->
- Where the surrogate defers: the camera-to-plan chain is bimodal (either the VLA keeps up or no plan finishes in the window), so its interval is flagged "uncertain, run full model" for every test design. <!-- src: results/s11.json#headline.chain_camera_to_plan.p99_ms -->
- Speed: the browser model (4.8 MiB ONNX, every output plus its interval) takes 1.24 ms per slider update on average with onnxruntime-web's wasm backend under Node (p95 1.91 ms), against a median 1.59 CPU seconds for one full-model design point, about 1,290x faster. <!-- src: results/s11.json#speed -->
- Distribution shift, measured on purpose: against the all-L1 study mix the surrogate's 90 percent intervals cover 80 to 85 percent of 40 designs for T4 p99 and energy; inside closed-loop co-sim runs they cover 84 percent for T4 p99 but only 29.5 percent for energy, whose 20 s runs and mid-run scenario events sit outside the training distribution. <!-- src: results/s11.json#fidelity_check, results/s11.json#cosim_shift -->
- Robot success (200 co-simulated designs, 20 s each): pick-rate test error 0.022 against 0.047 for predicting the mean; grasp success rate was 0.96 on average and the model does no better than the mean, so I do not use it for decisions. <!-- src: results/s11.json#robot_subset -->
- Design-space exploration over 60,000 candidates on S1, S2, S3 and S6: 16,888 meet the limits (90 percent upper bounds below 0.5 percent safety misses and 5 percent T4/T2 misses on every workload). The three recommendations, each re-run with the full model on all four workloads at 1.2 s and 4.8 s, had zero safety, T4 and T2 misses, and all 16 interval checks per design landed inside the 90 percent intervals: <!-- src: results/s12.json#recommended -->

| Design | NPU | Policy | Area proxy | Energy, surrogate | Energy, full model (1.2 s / 4.8 s) |
|---|---|---|---|---|---|
| Lowest energy | 8 tiles x 120, NPU Vmin 0.456 V | P3 race to idle, EDF, zero-copy | 53.6 mm2 | 3.19 Wh | 3.25 / 3.18 Wh |
| Smallest area | 3 tiles x 88 | P4 pace to deadline, EDF-MC, QoS and partitioning on | 34.9 mm2 | 4.65 Wh | 4.52 / 4.28 Wh |
| Balanced | 4 tiles x 104 | P3 race to idle, EDF-MC | 40.7 mm2 | 3.44 Wh | 3.53 / 3.44 Wh |
| VX-1 default | 4 tiles x 64 | P4, EDF | 47.9 mm2 | 5.80 Wh | 6.12 / 6.02 Wh |

<!-- src: results/s12.json#recommended, results/s12.json#baseline_vx1_p4_edf -->

The pattern in the winners is "wide and slow": a large NPU held at low voltage, little DRAM bandwidth (about 65 GB/s) and a small system cache. The policy comparison inside this dataset should not be read as P3 beating P4: each sample is 1.2 s, so P4 spends much of the run in its warm-up (it keeps a task's domains at or above nominal until it has seen five completions of that task, and T4 needs about 0.6 s for that), P4 never power-gates idle domains while P3 does, and the dataset pins the P4 version from the start of generation, which predates later P4 changes in `power/`. A fair P4 comparison needs runs of several seconds; that is what s06 does.


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
| DRAM bandwidth | 64 to 204 GB/s | data rate on the fixed 128-bit bus |
| App CPU cores | 4 to 16 | split across the two clusters |
| Scheduler | FIFO, rate monotonic, EDF, mixed-criticality EDF | |
| Power policy | P0 nominal, P3 race to idle, P4 pace to deadline | P6 uses this surrogate, so it is not a knob of it |
| Vmin per domain | NPU 0.45 to 0.75 V, CPU, SIMT, DSP 0.50 to 0.75 V | static policies: a static undervolt when below 0.75 V; P4: the DVFS floor |
| Low-voltage arithmetic factor | 1x to 4x, 40 percent of samples at exactly 1x | hypothetical |
| Transport | copy / zero-copy | |
| Workload | S1, S2, S3, S6 as steady-state task sets | S3 runs the escalated 5 ms safety monitor plus a replan burst each second |
| Recording bitrate | 0.2 to 48 Gbit/s, log scale | stands in for S7 |
| Ambient | 25 to 50 C | stands in for S5 |

Outputs per sample: p50, p95 and p99 response time and miss rate for 12 tasks, three end-to-end chains (camera to plan, human detection to control, camera to detection), worst safety-task and worst real-time miss rate, average and peak power, energy per robot-hour, peak junction temperature, and the area proxy. Robot success (grasp success rate, pick rate) comes from a separate co-simulated subset.

## How the dataset is built

- `chiprun.py` drives `glasschip_core` directly with the same release rules as the co-sim bridge (periodic releases on the 1 ms grid, detection chained to camera ingest, planning triggered from VLA completions at the scenario's 2 Hz rate) and the power manager stepping every 1 ms. There is no robot physics in these runs, which is what makes thousands of samples affordable.
- Each sample is 1.2 simulated seconds; jobs released in the first 0.2 s are excluded. The fidelity mix is `fidelity_dse.yaml`, which is the interactive mix (CPU, SIMT and DSP analytic; NPU, NoC, cache and DRAM transaction level). I first tried the all-L1 study mix, but on this shared machine it cost about 5 CPU seconds per simulated second and put 5,000 samples out of reach. s11 measures the gap to the study mix on 40 held-out designs.
- Tail quantiles of slow tasks come from few jobs per run (T4 releases about 8 times in the window), so a p99 for T4 is close to the per-run maximum. When a task in a workload finishes no job in the window, I censor its latency at the window length and its miss rate at 1.
- The dataset pins a snapshot of the core module, the SoC config, the power parameters and the power package (`data/surrogate/snapshot/`), so rebuilds by other parts of the project cannot mix two models inside one dataset. Every row carries the snapshot hashes, the graph hash and the git commit.
- `dataset.py lhs` draws a Latin hypercube; `dataset.py active` adds active-learning points where the current surrogate's interval is widest among intervals that straddle a deadline (or the 1 percent miss line). Active-learning points are used for training only, never for calibration or testing, because they are not drawn from the same distribution. Generation is resumable: finished sample ids are skipped, and `touch data/surrogate/STOP` stops a run cleanly.
- `robot_subset.py` runs a smaller Latin hypercube through the full closed loop (`studies/runner.py`: MuJoCo, the core and the power manager, real scenario files with their events) for 20 simulated seconds each and records grasp success and pick rate.

## Models

`train.py` fits two candidates per output and keeps the one with the lower validation error: LightGBM on the raw knobs plus peak NPU MACs per cycle, and a small MLP (two hidden layers of 64) on scaled knobs with one-hot categorical knobs. Latencies are modeled in log space and miss rates as log(rate + 0.001), so errors are relative. For latency tails (p95, p99 and chain quantiles) I also fit LightGBM quantile models at 5 and 95 percent for conformalized quantile regression. The area proxy is closed form and is not learned.

The Latin-hypercube samples are split 60/10/15/15 into train, validation, calibration and test with a fixed seed. After model selection the chosen model is refit on train plus validation.

## Conformal intervals: what they guarantee and what they do not

`conformal.py` implements split conformal prediction (absolute residual in model space) and conformalized quantile regression (Romano, Patterson and Candes, NeurIPS 2019). For each output I compute q-hat on the calibration split at 80, 90, 95 and 99 percent coverage, and I report the empirical coverage on the separate test split with an exact Clopper-Pearson 95 percent interval.

What it guarantees: if a new design point is exchangeable with the calibration points (drawn the same way, simulated by the same model), the probability that its true value lands inside the interval is at least the nominal level. The probability is over both the new point and the calibration draw. This is marginal coverage.

What it does not guarantee:
- Coverage for a specific design. Some regions of the space get intervals that are too narrow and others too wide; only the average is controlled.
- Anything outside the sampled ranges, or for a different simulator: a new core build, a different fidelity mix, longer runs, or the closed-loop co-sim's release process. s11 measures two such shifts on purpose.
- Independence across outputs. Each output has its own interval; there is no joint guarantee that all of them hold at once.

Many miss rates are exactly zero for most designs. Ties make the coverage of those outputs higher than nominal, which split conformal allows (it guarantees at least the nominal level, and at most nominal plus 1/(n+1) only when there are no ties).

Fallback rule: an interval is marked "uncertain, run full model" when its width exceeds 0.5 x the task's deadline for latencies, or 0.05 for miss rates. The browser panel can show that flag from `meta.json`, and P6 falls back to its cycle estimate when it fires.

## Browser export

`export_onnx.py` builds one ONNX graph by hand with `onnx.helper`: the derived feature, every LightGBM model as a single `ai.onnx.ml` TreeEnsembleRegressor with one target per model, MLPs as MatMul, Add and Relu, the q-hat offsets, the CQR clamping, the inverse transforms and the closed-form area. Input `x` is float32 [N, 20] in the order of `dataset.INPUTS`; each output `o` comes with `o__lo` and `o__hi`. I check it against the Python models (`max relative difference` is in the float32 rounding range) and time it with onnxruntime-web's wasm backend under Node (`bench_ortweb.mjs`). `viz/public/surrogate/meta.json` follows the shape `viz/src/dse/surrogate.ts` reads, plus input ranges, labels for categorical codes, q-hat, achieved test coverage and provenance per output, the Pareto points from s12, and the three recommended designs.

## P6 support

`p6.py` exposes `SurrogateFinishBound`, the callable that the P6 policy (`power/policies/pace.py`) takes as `surrogate`. At construction it precomputes, for every task and power domain on a voltage grid, the conformal upper bound on the task's p95 response time (p99 at 95 percent for safety tasks) when that domain is held at that voltage. A call is then `now + (1 - frac_done) x R_hi(task, domain, V(freq))`, a lookup plus interpolation. It falls back to the cycle estimate (`est_remaining_cycles / freq x 1.25`) for tasks or domains the surrogate does not cover, outside the sampled voltage range, or when the interval is flagged uncertain. This is a calibrated heuristic, not a per-job guarantee: the coverage is marginal over designs and describes a whole run's tail, and holding a voltage for a run is not the same as switching every millisecond.

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
.venv/bin/python -m surrogate.dataset lhs --n 5000 --workers 10      # dataset (resumable)
.venv/bin/python -m surrogate.train                                   # models + conformal bundle
.venv/bin/python -m surrogate.dataset active --n 600 --round 1        # active learning, then train again
.venv/bin/python -m surrogate.robot_subset run --n 200 --workers 8    # co-simulated robot subset
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
