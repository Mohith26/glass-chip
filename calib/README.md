# calib: external simulators and calibration data

I use established open-source simulators as reference models for the Glass Chip SoC model.
They provide the L2 (cycle-approximate) and L3 (reference) fidelity levels and the data the
in-house L0/L1 models are checked against. Everything here runs locally on an Apple Silicon
Mac. VX-1 is a fictional reference SoC; these runs say nothing about any real chip.

Every table in `tables/` is written by a script in this folder and carries a `provenance`
block (script, tool commit, config files, date).

## What each tool is for

| Tool | Version | Used for | Script | Output |
|---|---|---|---|---|
| Ramulator 2.1 | CMU-SAFARI/ramulator2 @ 7bead6c (2.1.0) | DRAM L2: LPDDR5 bandwidth and latency vs load, per-request trace replay | `run_ramulator.py`, `ramulator/replay.py` | `tables/dram_ramulator.json` |
| BookSim 2 | booksim/booksim2 @ 28f4329 | NoC L2: 4x4 mesh latency vs load, saturation, packet trace replay | `run_booksim.py`, `booksim/replay.py` | `tables/noc_booksim.json` |
| SCALE-Sim | scale-sim-v2 @ 87fd03d (package 3.0.0) | NPU L2: per-op cycles, utilization, SRAM and DRAM traffic for one 64x64 tile | `run_scalesim.py` | `tables/npu_scalesim_<task>.json`, `tables/npu_scalesim_standard.json` |
| gem5 | 26.0.0.0 (stable @ 9d36889) | CPU L3: cycles and CPI of RV64 kernels in SE mode | `run_gem5.py`, `gem5/se_run.py` | `tables/cpu_gem5.json` |
| zig cc | 0.17.1 (LLVM, ld.lld, musl) | RV64GCV cross compiler and linker for the gem5 binaries | `riscv/kernels/Makefile` | `riscv/build/*` |
| Public Jetson AGX Orin data | NVIDIA spec pages, MLPerf v3.1 | External sanity check only | `orin_public.py`, `../configs/soc/orin_like.yaml` | `tables/orin_public.json` |

## How to regenerate

All commands run from the repo root with the project venv.

```bash
# Ramulator: build once (see "Build notes"), then
.venv/bin/python calib/run_ramulator.py          # about 10 s
# BookSim
.venv/bin/python calib/run_booksim.py            # about 40 s
# SCALE-Sim (layer results are cached in data/calib/scalesim_cache/)
.venv/bin/python calib/run_scalesim.py --standard --tasks T2_detect T8_safety T4_vla
# gem5 (needs third_party/gem5/build/RISCV/gem5.opt)
.venv/bin/python calib/run_gem5.py               # about 1 min on 6 workers
# Orin public numbers
.venv/bin/python calib/orin_public.py
```

Trace replay for cross-fidelity checks:

```bash
# DRAM: CSV columns time_ns,addr,rw,bytes
.venv/bin/python calib/ramulator/replay.py trace.csv out.csv
# NoC: CSV columns time_ns,src_x,src_y,dst_x,dst_y,bytes
cd calib/booksim && ../../.venv/bin/python replay.py trace.csv out.csv
```

## How VX-1 maps onto each tool

- DRAM: Ramulator 2.1 has LPDDR5 presets only up to 6400 MT/s and no LPDDR5X. I run the stock
  LPDDR5-6400 preset and a derived 8533 MT/s config (tCK 938 ps, core timings converted from
  `vx1.yaml` timing_ns, the rest scaled from the 6400 preset at constant nanoseconds). The derived
  config is an extrapolation, not a JEDEC LPDDR5X preset. The 128-bit bus is eight x16 channels.
  LPDDR5 bank-group mode has 4 x 4 banks, not the 4 x 8 in `vx1.yaml`.
- NoC: dimension-order (XY) routing, 4 VCs, 32 B flits (256-bit links). I matched the 2-cycle
  router and 1-cycle link with routing_delay 0 and speculative allocation, then checked with
  single packets that every extra hop costs exactly 3 cycles.
- NPU: one tile, 64x64, weight- and output-stationary, 2 MiB SRAM split 768/768/512 KiB
  (ifmap/filter/ofmap; my assumption). Depthwise convs use a "packed" mapping (channels across
  columns); the strict one-group-at-a-time alternative is stored next to it.
- CPU: one core with 64 KiB L1I/L1D and 1 MiB L2 at 2 GHz, VLEN 256. TimingSimpleCPU, MinorCPU
  (my in-order RT core proxy) and O3CPU with gem5 defaults (my app core proxy). Cache latencies and
  the DDR4 backing memory are gem5-style assumptions.

## Key results (from the tables)

- DRAM, derived LPDDR5X-8533, 136.5 GB/s peak: sequential reads reach 102.6 GB/s (75% of peak),
  random 64 B reads 46.2 GB/s, random 2:1 read/write 37.7 GB/s, row-hit-heavy random 78.7 GB/s.
  Low-load mean read latency is 33.5 ns sequential and 81.6 ns random (controller plus device).
- NoC: zero-load latency for 2-flit packets is 13.4 cycles (8.9 ns). Saturation (accepted
  flits/node/cycle): uniform 0.75, transpose 0.34, all-to-DRAM hotspot 0.124. The hotspot limit is
  95.5 GB/s total, set by the two DRAM-node ejection ports, which is below the DRAM peak.
- NPU and CPU: see `tables/npu_scalesim_*.json` and `tables/cpu_gem5.json`.

## Build notes and what failed

- The Aside sandbox on this Mac quarantines files it writes, and a freshly linked binary that loads
  a non-system dylib (libpython, libramulator) is killed at launch with no dyld message. I wrap
  clang in `third_party/wrap/clang-noq` and `clang++-noq`, which clear the xattr on the output.
- Ramulator 2.1 did not compile with Apple clang 21. Fixes: build with `-DFMT_CONSTEVAL=` (bundled
  fmt 10.2.1 consteval errors) and add the `template` keyword in `src/ramulator/base/param.h`.
- Ramulator's Python bindings only run whole simulations, so I wrote a small C++ driver
  (`ramulator/trace_driver.cpp`) on its External frontend to get per-request latency.
- BookSim 2 has no trace input. I added a trace mode (`trace_file`, `trace_out`) to
  `src/trafficmanager.cpp`. BookSim's `main()` returns 255 on success, so scripts check the output.
  The hotspot syntax is `hotspot({{12,14},{1,1}})`; the obvious `hotspot({12,14},{1,1})` silently
  makes a single hotspot at node 12. The trace-mode cross-check caught this: before the fix the
  native hotspot saturated at half the rate the trace run sustained.
- SCALE-Sim needed three numpy 2 fixes (`int(max(array))` calls). Its USER bandwidth mode took
  minutes per large layer and takes per-operand bandwidths from the layout section, so I run CALC
  mode and report stalls from a stated bandwidth model.
- gem5 needs an embedded Python 3.10 or newer; the Command Line Tools Python is 3.9, so I link the
  project's Python 3.12 and point `LIBRARY_PATH` and rpath at it. Build time was about 10 minutes.
  `Checking Python version` failed until the quarantine fix above.
- Apple clang 21 has no RISC-V backend (`clang -print-targets` lists only AArch64, ARM, x86 and
  WebAssembly), and `ld.lld` is not installed, so `clang --target=riscv64-linux-gnu` does not work
  here. zig cc (pip `ziglang`) provides clang with RISC-V, ld.lld and a static musl libc.
- gem5's RVV timing is coarse: vector instructions are split into per-register micro-ops on the
  default functional units, with no explicit lane-width limit, so O3 vector results are optimistic.
  I use the scalar and in-order numbers as the more conservative reference.
- Spike was not needed because gem5 built.

## Validation studies (phase B)

- `studies/s10_cross_fidelity.py` compares L0, L1, the named fidelity mixes and one-component
  swaps against the L2 models on 21 microbenchmarks (`microbench.py`, graphs in
  `graphs/mb/`) and scenarios S1-S8, checks the CPU model against gem5, replays the core's own
  DRAM and NoC traces through Ramulator 2 and BookSim 2 per request (`s10_traces.py`), and
  measures simulation speed. Output: `results/s10.json`.
- `studies/s15_external_sanity.py` runs ResNet-50, RetinaNet and BERT-Large (graphs from
  `public_models.py`, in `graphs/T9*.json`) on `orin_like.yaml` against the public MLPerf numbers,
  and compares the core's NPU model with SCALE-Sim per op. Output: `results/s15.json`.
- `VALIDATION.md` summarizes both, with every number taken from those two files.

The core now runs Ramulator 2 and BookSim 2 in-process as its DRAM and NoC L2 backends (the
core worker built them on the libraries in `third_party/`). They are about four orders of
magnitude slower than real time, so s10 uses them on microbenchmarks and a short S1 window only.

## Honesty notes

- Accuracy statements made with these tables are relative to these reference simulators, not
  silicon.
- `orin_like.yaml` uses only public Jetson AGX Orin specifications, tagged per value as cited,
  derived, mapping or assumption. The Orin benchmark numbers are a sanity check, not validation.
