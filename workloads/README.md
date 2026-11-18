# Workloads

This folder turns the robot software stack into task graphs that the Glass Chip SoC model executes. Each task is one JSON file in `graphs/`, validated against `schema/taskgraph.schema.json`. Every graph records where it came from (script, model name and version, hash, date, machine, method) in its `provenance` block, and most also carry a `params` block with the assumptions and cross-checks behind the numbers.

Regenerate everything with one command from the repo root:

```
.venv/bin/python workloads/capture/run_all.py
```

That builds and tests the C++ kernels, exports the ONNX models, writes all graphs, `graphs/middleware.json` and the scenario files in `configs/scenarios/`, then runs `validate.py` and `summary.py`. The per-task table is in `graphs/SUMMARY.md`. Tests: `.venv/bin/python -m pytest -q workloads/tests`.

## What each graph came from, and how faithful it is

The `method` field says how a graph was produced: `onnx_graph_walk` (a real model graph walked node by node), `config_derived` (operators derived from the real model config and source code), `analytic_count` (operations counted from an algorithm), `published_numbers` (scaled from published timings), or `mixed`.

| Task | Source | Method | How faithful |
|---|---|---|---|
| T1_ingest_wrist, T1_ingest_overhead | ISP-like preprocessing I wrote out per pixel | analytic_count | Frame sizes and output sizes are exact (they match each model's real input); per-pixel op counts are my estimates. |
| T2_detect | Ultralytics YOLOv8n exported to ONNX (opset 17, 640x640) | mixed | The NPU part is the real exported graph, walked op by op with shape inference. Activations are fused into their convolutions the way an NPU compiler would. NMS and depth-based pose refinement on the CPU are counted analytically with stated assumptions. |
| T3_esdf | cuRoboV2 block-sparse TSDF plus dense PBA+ ESDF | mixed (analytic + published sanity check) | Fallback, see below. |
| T4_vla | SmolVLA, `lerobot/smolvla_base` | mixed | See the VLA section below. |
| T5_plan | cuRoboV2 IK, TrajOpt, TimeOpt | mixed (analytic + published sanity check) | Fallback, see below. Event-triggered (sporadic). |
| T6_estimate | `capture/control/estimate.cpp`: joint Kalman filter plus 8-track object tracker | mixed | Compiled to RV64 and counted statically; when the gem5 calibration table exists, the instruction total is the gem5-measured dynamic count per call (the static count counts both sides of every branch and stays in the op notes). |
| T7_control, T12_arm2_control | `capture/control/control.cpp`: Panda joint impedance control with RNEA gravity and Coriolis | mixed | Compiled for rv64gc (the RT core has no vector unit), counted statically, and replaced by the gem5 dynamic count when available. A native test checks the dynamics (gravity invariant to joint 1, zero-error control equals gravity compensation). |
| T8_safety | torchvision LR-ASPP MobileNetV3-Large at 320x240, plus speed and separation monitoring in `ssm.cpp` | mixed | NPU part is the real exported graph; the mask and depth fusion on the DSP is analytic; the SSM math is compiled RV64 code. |
| T10_record, T10_record_4k | Logging as DMA and DRAM streams | analytic_count | Bytes scale with `params.bitrate_bps` (ops carry `bytes_scale_param`). The default 200 Mbit/s is an assumption; the 4K graph uses raw 4K30 rgb8. |
| T11_speech | Whisper-tiny (`openai/whisper-tiny`) | mixed | The encoder is the real architecture built from the real config, exported to ONNX and walked; the mel front end and a 12-token greedy decode are config derived. The decode loop is written out per token and per layer, so each op's shape is the exact GEMM it performs. |
| T13_map_save, T14_ota, T15_diag | Background tasks for scenario S6 | analytic_count | Rough estimates, labeled in each op's notes. |

Deployment dtypes are assumptions, not properties of the models: int8 weights and activations on the NPU for convolution and linear layers, bf16 for attention scores, softmax and the KV cache. The exported ONNX files themselves are fp32.

Periods and deadlines follow spec Section 6.1. They are reasonable defaults for a pick-from-conveyor task, not measured requirements.

## Fallbacks I used, and why

**cuRobo (T3, T5).** I had no GPU reachable from this machine, so I did not profile cuRoboV2. Instead I counted kernel FLOPs and bytes from cuRoboV2's algorithms and its own defaults, read directly from the repository: 61 Franka collision spheres and the self-collision pairs from `franka.yml`, IK and TrajOpt seeds, iterations, L-BFGS history, line-search points and knot count from the C++ runtime headers and task YAMLs, and the mapper's block size and truncation distance. The RNEA FLOPs per pass come from my compiled controller. I then compared the totals with cuRoboV2's published timings (the repository's benchmark table for RTX 5090 and Jetson AGX Orin, and the paper's RTX 4090 kernel breakdown and ESDF timings). The implied GPU utilization is low (a few percent of FP32 peak), which fits latency-bound kernels with many dependent iterations, so the graphs have one op per kernel launch and per optimizer iteration. The core charges the SIMT launch overhead once per op, and the iterations stay serial. The planner scene is the ESDF from T3 rather than the benchmark's box obstacles, and the workspace size and visible-block count are my assumptions.

**Middleware (T9).** There is no Docker or ROS 2 here, so I wrote a CDR serializer for the stack's real ROS 2 message layouts (Image, PointCloud2, JointState, JointTrajectory, the VLA action chunk as Float32MultiArray, a detection array) and measured serialize, deserialize and copy times on this Mac. `middleware.py` converts them to per-KB and fixed costs and multiplies by a Mac-to-target factor of 3.0. That factor is an assumption, explained in `middleware.json`. Executor dispatch per hop comes from a published ROS 2 executor study on the Autoware reference system, cited in the file; the events-executor value uses the static single-threaded figure as a proxy. These costs were measured on a Mac and scaled, not measured on a robot SoC.

**VLA (T4).** SmolVLA's operator graph is mixed. The vision encoder and connector are the real SmolVLM2-500M vision architecture, built from the real config with random weights (weights do not change the operator graph), exported to ONNX and walked once per camera, which matches lerobot's per-camera loop. The language model prefill and the flow-matching action expert are derived from the config and lerobot's source: 16 kept VLM layers, 16 expert layers with cross-attention on odd layers and self-attention every second layer, 10 Euler steps, a 50-step action chunk, and a cached prefix of 2 x 64 image tokens + 48 language tokens + 1 state token. Attention is explicit (RoPE, QK^T, softmax, AV, with grouped KV heads), and the KV cache size is recorded. Every parameter group I derived matches the real `model.safetensors` header exactly, which I read with an HTTP range request without downloading the weights. I did not run SmolVLA closed loop.

## Other files

- `capture/onnx_walk.py`: the ONNX walker (shape inference, metadata stripping, activation fusion, MAC and byte counting, layer grouping).
- `capture/control/count_rv64.py`: compiles the C++ kernels to RV64 assembly with clang and counts instructions per call (scalar, vector, memory), with loops weighted by known trip counts. Apple's clang has no RISC-V backend, so it uses Homebrew LLVM's clang when present.
- `capture/scenarios.py`: writes S1 to S8. Event `args` carry scenario details such as the replan burst in S3 and the bitrate steps in S7.
- `validate.py`: schema, provenance, dependency DAG and list order, non-empty compute, declared MACs against the MACs implied by each op's shape (the same GEMM view the core builds; more than 2x apart fails), summary totals, middleware fields and scenarios.
- `summary.py`: the per-task table in `graphs/SUMMARY.md`.

Large downloads and exported models are cached in `data/workloads/` (not tracked).
