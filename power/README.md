# glasschip-power

This is the power and thermal layer of Glass Chip. It turns the activity counters that the SoC simulator reports every millisecond into per-domain power, energy and temperature, and it runs the power policies (P0 to P6) that pick a voltage and frequency for each power domain.

VX-1 is a fictional reference SoC sized for a mid-range humanoid or arm controller. Every parameter in this layer is an assumption. I took the equations from the literature and seeded the per-operation energies from published tables, then scaled them to a generic 5 nm class node with a rule I state below. None of it is a measurement of any real chip.

## Do not overclaim

- The low-voltage arithmetic toggle represents the class of techniques Velaura says its Titan Core uses. The 2 to 4x arithmetic power reduction is Velaura's own claim, not a measurement, and this model does not implement or reverse-engineer their technology. The factor defaults to 1x. Any other value, and any NPU operating point below the configured table, is labeled "hypothetical" in `PowerManager.summary()`.
- Energy numbers are only comparable between runs that use the same parameter file. The P5 bound is a lower bound under this model, not a physical limit.
- Results from `demo_fake.py` come from a hand-made synthetic job set on a stand-in simulator (FakeSim). They exercise the code; they are not VX-1 findings.

## Files

| File | What it does |
|---|---|
| `params_vx1.yaml` | All power and thermal parameters, each commented as an assumption with its seed source |
| `vf.py` | Alpha-power-law V/f curve, OPP tables, inverse (minimum V for a target f) |
| `energy.py` | Per-class switching energy, clock-tree power, peak power estimate, low-voltage toggle |
| `leakage.py` | Temperature-dependent leakage per domain |
| `dvfs.py` | Regulator efficiency curve, DVFS transition energy, power-gating parameters |
| `thermal.py` | Lumped RC network (implicit Euler) and throttling with hysteresis |
| `predictor.py` | Online per-task demand and estimate-error statistics for P4 and P6 |
| `policies/` | P0 to P6 (`make_policy` reads `configs/policies/*.yaml`) |
| `offline_opt.py` | P5: minimum-energy OPP assignment for a recorded job trace (HiGHS MILP) |
| `manager.py` | `PowerManager`, the per-step loop the co-sim bridge calls |
| `cosim_hooks.py` | `ObservedChip` (feeds completions to the manager) and the `set_ambient` / `set_vmin` event hook |
| `thermal_analysis.py` | Steady state with temperature-dependent leakage (runaway detection) and open-loop throttle-onset time |
| `run_cosim.py` | Minimal co-sim harness; the studies use the shared `studies/runner.py` instead |
| `demo_fake.py` | Synthetic comparison of P0, P2, P3, P4 and the P5 bound on FakeSim |
| `tests/` | pytest suite, `FakeSim`, and a guarded integration test against the real core |

## Models and equations

### Voltage and frequency

Alpha-power law (Sakurai and Newton, "Alpha-power law MOSFET model and its applications to CMOS inverter delay and other formulas", IEEE JSSC 1990):

    f(V) = k * (V - Vth)^alpha / V

with Vth = 0.30 V, alpha = 1.3 and v_nominal = 0.75 V from `configs/soc/vx1.yaml` (assumptions). I fit k per domain so that f(0.75 V) equals the nominal frequency of the blocks in that domain (the NoC frequency for `noc_cache`). OPPs are the voltages in `power_domains.<d>.opp_volts`; each OPP frequency is f(V). `min_voltage_for(f)` inverts the curve by bisection (f is monotonic above Vth when alpha > 1). `dram_io` has a single OPP and a fixed PHY rate, so the policies never change it.

### Dynamic energy

    E_op(V) = activity * C_eff * V^2
    C_eff   = E_seed * node_scaling[kind] / V_ref^2

V_ref is 0.75 V for on-die classes. DRAM classes use the `dram_io` rail voltage, so DRAM energy does not follow SoC DVFS. Seeds (all assumptions; sources listed under References):

| Class | Seed | 5 nm class value at V_ref |
|---|---|---|
| INT8 MAC | 0.10 pJ: int8 multiply 0.07 + int32 add 0.03 at 7 nm [J21] | 0.08 pJ |
| BF16 MAC | 0.59 pJ: bf16 multiply 0.21 + fp32 add 0.38 at 7 nm [J21] | 0.472 pJ |
| FP8 MAC | 0.265 pJ: my assumption, fp8 multiply about 1.5x int8 multiply plus fp16 add 0.16 [J21] | 0.212 pJ |
| SRAM byte | 32 KB SRAM 8.5 pJ per 64-bit access at 7 nm [J21] / 8 | 0.956 pJ |
| Cache byte | 1 MB banked SRAM 14 pJ per 64-bit access at 7 nm [J21] / 8 | 1.575 pJ |
| NoC flit-hop | 40 fJ/bit-mm [D18] x 256 bit x 2.5 mm hop, router assumed equal to the link | 51.2 pJ |
| DRAM byte (incl. I/O) | 2 pJ/bit interface + 2.5 pJ/bit access [D17] | 36 pJ |
| DRAM activate | my assumption, no published seed used | 800 pJ |
| CPU instruction | 70 pJ per instruction at 45 nm [H14] / 2.6 (45 to 7 nm geomean gain [J21]) | 21.5 pJ, x3 for out-of-order app cores, x2 for the lockstep RT pair (assumptions) |
| Vector op (fp32 FMA element) | (0.38 + 1.31) pJ at 7 nm [J21] x 1.5 register-file overhead (assumption) | 2.03 pJ |
| SIMT FLOP | fp32 FMA at 7 nm [J21] / 2 x 2.0 issue and register-file overhead (assumption) | 1.35 pJ |
| DSP op | int8 MAC at 7 nm [J21] x 2.0 SIMD overhead (assumption) | 0.16 pJ |
| DMA byte | 8 KB SRAM read + write at 7 nm [J21] / 8 | 1.69 pJ |

Scaling rule from the 7 nm seeds to a generic 5 nm class node (assumption): logic x0.8, SRAM x0.9, wires x1.0, DRAM x1.0. The unequal factors follow Jouppi et al.'s observation that logic, SRAM, wires and DRAM improve unequally [J21] and Dally et al.'s point that on-chip wire energy per bit-mm is not improving [D18]. I treat the 7 nm table as if it were measured near 0.75 V.

Clock tree and other always-on switching while a domain is clocked (busy or idle, not gated):

    P_clk = fraction * P_peak_dyn_nominal * (V / V_nom)^2 * (f / f_nom)

with fraction 0.10 (0.04 for `dram_io`), assumptions. P_peak_dyn_nominal is computed from `peak_activity_per_cycle` (derived from the block widths in vx1.yaml) times the per-op energies.

### Leakage

    P_leak = V * I0 * exp(cT * (T - T0))

T0 = 25 C and cT = 0.025 per C (leakage roughly doubles every 28 C), assumptions. I0 per domain is set so that leakage at (0.75 V, 25 C) is 5 percent of the domain's estimated peak dynamic power (assumption). The form ignores DIBL, so leakage falls only linearly with V here; that understates low-voltage savings. Temperatures above 175 C are clamped inside the leakage fit so a runaway stays finite (the fit means nothing there). <!-- numcheck: ignore -->

### Regulator, DVFS and power gating

    x = P_out / P_rated,  P_loss = P_rated * (a0 + a1 x + a2 x^2),  P_in = P_out + P_loss

a0 = 0.01, a1 = 0.06, a2 = 0.03, and P_rated = 1.2 x (peak dynamic at the top OPP + leakage at 105 C), all assumptions chosen to give the usual buck-converter shape (poor at very light load, peak about 0.9, slight droop at heavy load). Regulator loss heats the enclosure node (the PMIC is off die).

- OPPs are discrete (the table in vx1.yaml). A change takes `dvfs_transition_us` (20 us, assumption from vx1.yaml), during which the simulator stalls the domain, and costs 0.5 * C_rail * |V1^2 - V0^2| with C_rail = 200 nF (assumption). <!-- numcheck: ignore -->
- Power gating (P3, and the P4G variant): gated domains draw 5 percent of their leakage (retention), each wake costs C_rail * V^2 and 10 us of wake latency, and a domain gates only after 100 us with no work (idle timer, all assumptions). <!-- numcheck: ignore -->
 The core applies the wake latency and the idle timer in its timing and reports `gated_ps` and `wakeups`. Gateable domains are cpu_a, cpu_b, npu, simt and dsp. The RT safety island, NoC/cache and DRAM I/O stay on. In an early S1 pilot without the idle timer, the NPU woke so often that the wake stalls broke deadline pacing (a pilot observation, not a committed result).

### Thermal

One junction node per power domain, a package node, an enclosure node and ambient:

    C_i dT_i/dt = P_i - sum_j G_ij (T_i - T_j) - G_i,amb (T_i - T_amb)

integrated with backward Euler, which is unconditionally stable at 1 ms steps. Junction R = 30 K mm^2/W divided by the domain's floorplan area on a 100 mm^2 die; junction C = 4.9e-4 J/K per mm^2 (silicon, 0.3 mm thick); package 5 J/K and 2 K/W to the enclosure; enclosure 60 J/K and 4.5 K/W to ambient. All are assumptions for a sealed fanless module. I picked the 4.5 K/W from a design rule, not from a target outcome: the enclosure is sized so the S1 nominal (P0) operating point keeps the hottest junction at 85 C at a 40 C rated ambient, steady state with leakage feedback, solved with `thermal_analysis.py` on a measured S1 run (phase A used 3 K/W, which never throttled at S5's 50 C). Leakage feedback matters: above a few watts the steady state stops existing (thermal runaway) unless throttling cuts power, and lateral coupling between domains is ignored. The Tj limit is 105 C. Throttling steps a domain's OPP cap down by one every 10 ms above 100 C, holds between 95 C and 100 C, steps back up below 95 C, and drops straight to the lowest OPP at 105 C. Because the enclosure time constant is about 3 minutes, short runs should start from a warm steady state (`thermal_init="steady"`). <!-- numcheck: ignore -->

## Policies

| ID | Config | Behavior |
|---|---|---|
| P0 | `P0_nominal.yaml` | Nominal OPP everywhere, FIFO scheduler |
| P1 | `P1_fixed_priority.yaml` | Nominal OPP, fixed-priority (rate monotonic) scheduler |
| P2 | `P2_edf.yaml` | Nominal OPP, EDF scheduler |
| P3 | `P3_race_to_idle.yaml` | Highest OPP everywhere, power-gate idle gateable domains, EDF |
| P4 | `P4_pace_to_deadline.yaml` | Per domain, the lowest OPP predicted to meet every pending deadline with margin, EDF |
| P5 | `P5_offline_optimal.yaml` | Offline lower bound from a recorded trace (online run behaves like P0 and records the trace) |
| P6 | `P6_surrogate_guided.yaml` | P4 with a surrogate upper bound on finish time; P4 when no surrogate is passed |

P4 in detail. Per DVFS domain, every 1 ms:

1. Time model per task, learned from completions (the core reports critical-path attribution and `cycles_by_domain`): the critical-path compute time, split across the task's domains by cycle-time share and converted to cycles at the job's time-averaged frequency on each domain (the manager integrates its OPP history), so it rescales as cycles / f; and a frequency-independent time M = response - compute - queueing (memory and NoC stalls, middleware, DVFS and wake stalls). I use the 0.95 quantile of each (0.999 for safety-critical tasks). Compute attribution still holds some stall time that does not shrink with frequency, so samples taken at a low clock understate demand at a higher one; a pacing policy that mostly runs low then drifts optimistic as its window fills with low-clock samples (I saw T4 misses appear after about 50 s). Samples taken at or above nominal clock therefore also go into an anchor window that low-clock samples never evict, and the demand used is the larger of the two quantiles. Domain occupancy cycles from the core are summed over tiles and cores, so they overstate time; the critical-path split avoids that. Before a task has 5 completions its domains stay at or above nominal (warm-up), and its demand falls back to `est_remaining_cycles` times a 1.25 prior.
2. Each pending job's remaining window is its deadline minus now, minus M (scaled by the fraction left), minus a margin of 50 us (100 us safety) plus the DVFS transition, minus 8 percent of its relative deadline (10 percent for safety tasks). Slack feedback raises a task's fraction when the 5th percentile of its last 40 slacks falls below it (capped at 30 percent). <!-- numcheck: ignore -->
   I set the 8 percent after S1 pilot runs on the previous core build showed T4 misses at 5 percent; with 8 percent the same pilots had none (noted in `results/s06.json`, `policy_params_note`; the held-out seeds in s06 check it). What is left is split across the job's domains in proportion to their remaining time at nominal frequency.
3. Future periodic releases whose deadlines can fall before a pending deadline on the domain are added as jobs (EDF processor-demand view).
4. Required frequency: `max_k sum_{dl_i <= dl_k} R_i / (dl_k - now)` with 10 percent headroom. A domain that hosts a safety-critical task never drops below the frequency that task's high-quantile demand needs within its deadline. Pick the lowest OPP at or above it.
5. Infrastructure domains (`noc_cache`) stay at nominal: the NoC clock sets every task's memory time, and when I let a utilization governor lower it, the VLA missed every deadline on the real core. Compute domains no task uses (cpu_b in S1) follow a utilization governor (target 70 percent). <!-- numcheck: ignore -->

P4G is P4 plus idle power gating (`gate_idle: true`), a labeled variant I added because P4 and P3 win on different domains (P4 on busy ones, P3 on idle ones).

P6 runs P4's machinery with a finish-time upper bound from a surrogate callable: per domain, the lowest OPP at which every pending job's bound is inside its deadline minus margin, never below the safety floor. The conformal surrogate (`surrogate/p6.py`, surrogate worker) is selected with `surrogate_factory: {callable: "surrogate.p6:SurrogateFinishBound", kwargs: {scenario: S1}}`; it is a calibrated heuristic with marginal coverage over design points, not a per-job guarantee. `placeholder_surrogate: true` instead uses a conservative stand-in, finish <= now + 1.5 x (M + remaining critical-path cycles / f), labeled placeholder. <!-- numcheck: ignore -->

P5 in detail (`offline_opt.solve_trace`): per domain, 1 ms slots, one OPP per slot (binary), continuous work per job per slot, every job's cycles placed inside its [release, deadline] window, minimizing switching energy plus static energy at the reference temperature, divided by the regulator's best efficiency. I solve it with HiGHS through `scipy.optimize.milp` and report the solver's dual bound. Slot capacity is units x f x dt because the core's job cycles are summed over a domain's cores or tiles. Problems with more than 20,000 OPP choices are solved as the LP relaxation (OPPs may time-share inside a slot). The simplifications (domains solved independently, whole-slot windows, perfect packing, no transition or wake cost, leakage at ambient, best-case regulator, LP relaxation) can only lower the optimum. One approximation is not a relaxation: switching energy per cycle is the domain's average over the run, because the core reports activity per domain, not per job. On a co-sim run the bound is computed over the accounted window (after the thermal settle) on that run's own job trace. <!-- numcheck: ignore -->

## Using it from the co-sim bridge

The studies go through `studies/runner.py` (co-sim worker), which wires it like this:

```python
from power.manager import PowerManager
from power.params import load_policy, load_soc

pm = PowerManager(load_policy("P4"), load_soc(), ambient_c=25.0, task_info=task_info)
pm.apply_scheduler(sim)                 # sim.set_scheduler(engine, pm.scheduler) for every engine
for t in physics_steps:                 # every 1 ms
    release_jobs_due(t)
    pm.step(sim, t)                     # accounts [t_prev, t], updates thermal, sets OPPs
    pm.observe_completions(sim.run_until(t + 1_000_000_000))   # or wrap the chip in cosim_hooks.ObservedChip
summary = pm.summary()                  # energy, Wh per robot-hour, peak power and temperature, time throttled
series = pm.series_arrays()             # per-step power, V, f, OPP per domain, temperature per node, energy
bound = solve_trace(pm.job_trace, pm)   # P5 lower bound for the same jobs
```

`set_vmin(domain, v)` is the scenario S8 hook: static policies are pinned to the highest OPP at or below v (a static undervolt), while P4 and P6 treat v as their floor. Values below the configured table are extrapolated and labeled. `set_ambient(c)` handles the S5 hot-day event.

## Running

```
.venv/bin/python -m pytest power/tests -q
.venv/bin/python -m power.demo_fake --duration-ms 1000
```

## References

- [H14] M. Horowitz, "Computing's energy problem (and what we can do about it)", ISSCC 2014.
- [J21] N. P. Jouppi et al., "Ten Lessons From Three Generations Shaped Google's TPUv4i", ISCA 2021, Table 2 (Horowitz's 45 nm table updated to 7 nm).
- [D18] W. J. Dally et al., "Hardware-Enabled Artificial Intelligence", Symposium on VLSI Circuits 2018 (on-chip wires 20 to 40 fJ/bit-mm). <!-- numcheck: ignore -->
- [D17] W. J. Dally, NVIDIA keynote slides 2017, "Energy shopping list", citing Keckler et al. (IEEE Micro 2011) and Vogelsang (MICRO 2010): DRAM interface 2 pJ/bit and access 2.5 pJ/bit at the 16 nm generation. <!-- numcheck: ignore -->
- T. Sakurai and A. R. Newton, "Alpha-power law MOSFET model and its applications to CMOS inverter delay and other formulas", IEEE JSSC 1990.
- HiGHS through SciPy (`scipy.optimize.milp`) for the P5 MILP.
