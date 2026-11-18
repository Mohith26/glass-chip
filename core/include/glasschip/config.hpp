// Parsed configuration: SoC description (configs/soc/*.yaml), fidelity mix
// (configs/fidelity/*.yaml) with model parameters, and task graphs
// (workloads/schema/taskgraph.schema.json).
#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include "json.hpp"
#include "kernel.hpp"
#include "serial.hpp"

namespace gc {

enum Domain : int { D_CPU_A, D_CPU_B, D_RT, D_NPU, D_SIMT, D_DSP, D_NOC_CACHE, D_DRAM_IO, N_DOMAINS };
enum Engine : int { E_NPU, E_SIMT, E_CPU, E_RT, E_DSP, E_DMA, N_ENGINES };
enum Qos : int { Q_SAFETY, Q_REALTIME, Q_BEST_EFFORT, N_QOS };
enum Crit : int { C_SAFETY, C_HIGH, C_MEDIUM, C_LOW };
enum SchedPolicy : int { SP_FIFO, SP_FIXED_PRIORITY, SP_EDF, SP_EDF_MC };
enum MapPolicy : int { MAP_STATIC, MAP_EFT };
enum Transport : int { TR_NONE, TR_COPY, TR_ZERO_COPY };
enum ExecutorKind : int { EX_SINGLE, EX_MULTI, EX_EVENTS };

// Delay attribution categories (order is the public order in result dicts).
enum Cat : int { A_COMPUTE, A_RUNQUEUE, A_ENGINE_QUEUE, A_NOC, A_DRAM, A_CACHE, A_DMA, A_PREEMPTION, A_DVFS, A_MIDDLEWARE, N_CAT };

// Activity counters per power domain (names match CONTRACTS.md).
enum Act : int {
  K_INT8_MACS, K_BF16_MACS, K_FP8_MACS, K_SRAM_BYTES, K_FLIT_HOPS, K_CACHE_BYTES, K_DRAM_BYTES,
  K_DRAM_ACTIVATES, K_CPU_INSTR, K_VEC_OPS, K_SIMT_FLOPS, K_DSP_OPS, K_DMA_BYTES, N_ACT
};

enum OpType : int {
  OT_CONV2D, OT_DWCONV2D, OT_MATMUL, OT_ATTENTION, OT_ELEMENTWISE, OT_SOFTMAX, OT_NORM, OT_POOL, OT_RESIZE,
  OT_REDUCE, OT_MEMCOPY, OT_CPU_KERNEL, OT_SIMT_KERNEL, OT_DSP_KERNEL, OT_CONTROL_LAW, OT_SERIALIZE, OT_OTHER,
  OT_RX, OT_TX  // synthetic middleware ops inserted by the runtime
};
enum DType : int { DT_INT8, DT_FP8, DT_BF16, DT_FP16, DT_FP32, DT_INT32, DT_MIXED };

const char* domain_name(int d);
const char* engine_name(int e);
const char* cat_name(int c);
const char* act_name(int k);
const char* qos_name(int q);
int parse_domain(const std::string& s);   // -1 if unknown
int parse_engine(const std::string& s);   // -1 if unknown
int parse_qos(const std::string& s);
int parse_sched(const std::string& s);

struct XY {
  int8_t x = 0, y = 0;
  GC_IO(x, y)
};

// Fixed hardware description from configs/soc/<name>.yaml.
struct SocCfg {
  std::string name;
  // noc.*
  int dimx = 4, dimy = 4;
  double noc_freq = 1.5e9;       // noc.freq_hz
  int link_bytes = 32;           // noc.link_bits / 8
  int vcs = 4;                   // noc.virtual_channels
  int router_cycles = 2;         // noc.router_latency_cycles
  int link_cycles = 1;           // noc.link_latency_cycles
  int flit_bytes = 32;           // noc.flit_bytes
  int max_packet_bytes = 256;    // noc.max_packet_bytes
  // blocks.cpu_a / cpu_b (index 0/1)
  struct Cpu {
    int cores = 4; double freq = 2e9; double ipc = 2.0; double vflops = 16; double ctx_ns = 2000; int l2_kb = 1024;
    std::vector<XY> nodes;
    GC_IO(cores, freq, ipc, vflops, ctx_ns, l2_kb, nodes)
  } cpu[2];
  struct Rt {
    int cores = 2; bool lockstep = true; double freq = 1.2e9; double ipc = 1.0; int tcm_kb = 256; std::vector<XY> nodes;
    GC_IO(cores, lockstep, freq, ipc, tcm_kb, nodes)
  } rt;
  struct Dsp {
    int units = 2; double freq = 1e9; double ops_int8 = 128; int simd_bits = 1024; std::vector<XY> nodes;
    GC_IO(units, freq, ops_int8, simd_bits, nodes)
  } dsp;
  struct Npu {
    int tiles = 4, rows = 64, cols = 64; bool weight_stationary = true; double freq = 1.2e9; double macs_tile = 4096;
    double bf16_frac = 0.25, fp8_frac = 0.5; double sram_tile_kb = 2048, shared_kb = 4096; double preempt_us = 20;
    std::vector<XY> nodes;
    GC_IO(tiles, rows, cols, weight_stationary, freq, macs_tile, bf16_frac, fp8_frac, sram_tile_kb, shared_kb, preempt_us, nodes)
  } npu;
  struct Simt {
    int cus = 4, lanes = 128; double freq = 1.3e9; double smem_kb = 256; double launch_us = 5; double coalesce = 0.7;
    std::vector<XY> nodes;
    GC_IO(cus, lanes, freq, smem_kb, launch_us, coalesce, nodes)
  } simt;
  struct Cache {
    int size_kb = 8192, ways = 16, line = 64; int hit_cycles = 30; bool partitionable = true; std::vector<XY> nodes;
    GC_IO(size_kb, ways, line, hit_cycles, partitionable, nodes)
  } cache;
  struct Dram {
    double mts = 8533; int bus_bits = 128; int ctrls = 2, ch_per_ctrl = 2, bgs = 4, banks_per_bg = 8, row_bytes = 2048;
    double tRCD = 18, tRP = 18, CL = 17, tRAS = 42, tRFC = 210, tREFI = 3900, tWTR = 10, tRTW = 8;
    std::vector<XY> nodes;
    GC_IO(mts, bus_bits, ctrls, ch_per_ctrl, bgs, banks_per_bg, row_bytes, tRCD, tRP, CL, tRAS, tRFC, tREFI, tWTR, tRTW, nodes)
  } dram;
  struct Dma {
    int engines = 8; double desc_ns = 200; std::vector<XY> nodes;
    GC_IO(engines, desc_ns, nodes)
  } dma;
  double dvfs_transition_us = 20;  // power_domains.dvfs_transition_us
  std::array<double, N_DOMAINS> nominal_hz{};

  GC_IO(name, dimx, dimy, noc_freq, link_bytes, vcs, router_cycles, link_cycles, flit_bytes, max_packet_bytes, cpu[0], cpu[1],
        rt, dsp, npu, simt, cache, dram, dma, dvfs_transition_us, nominal_hz)
};

SocCfg parse_soc(const Json& soc);

// Fidelity mix plus tunable model parameters. Every parameter is an
// assumption; the key name under `params:` in configs/fidelity/*.yaml is
// given in the comment and defaults are listed in configs/fidelity/README.md.
struct ModelParams {
  // --- transaction granularity (params.bulk_chunk_bytes / params.cpu_miss_bytes)
  int bulk_chunk_bytes = 4096;
  int cpu_miss_bytes = 64;
  int noc_req_bytes = 16;            // params.noc_req_bytes: read request header size
  bool noc_model_requests = true;    // params.noc_model_requests: reserve links for request headers (false = zero-load latency)
  // --- NPU
  double npu_l0_util = 0.80;         // params.npu_l0_util: roofline MAC utilization at L0
  double npu_vec_ops = 256;          // params.npu_vec_ops_per_cycle: per-tile vector unit throughput
  int npu_depth = 2;                 // params.npu_buffer_depth: 2 = double buffering
  bool npu_wload_overlap = true;     // params.npu_weight_load_overlap
  double npu_min_macs_per_tile = 2e6;// params.npu_min_macs_per_tile: below this an op uses one tile
  int npu_max_tiles_per_op = 4;      // params.npu_max_tiles_per_op
  double npu_fp32_frac = 0.0625;     // params.npu_fp32_rate_fraction (assumption, not in soc yaml)
  // --- SIMT
  double simt_l0_eff = 0.60;         // params.simt_l0_eff: fraction of peak at L0
  double simt_warps_needed = 8;      // params.simt_warps_needed_per_cu: warps to hide latency
  int simt_warp = 32;                // params.simt_warp_size
  // --- CPU
  double cpu_vec_ipc = 1.0;          // params.cpu_vector_ipc
  double cpu_mem_ipc = 1.0;          // params.cpu_mem_ipc
  int cpu_mlp = 8;                   // params.cpu_mlp: outstanding misses per core at L1
  double cpu_l0_mem_bw_gbs = 20;     // params.cpu_l0_mem_bw_gbs: per-core sustainable miss bandwidth
  // --- DSP
  double dsp_line_buffer_kb = 64;    // params.dsp_line_buffer_kb
  double dsp_fill_cycles = 64;       // params.dsp_pipeline_fill_cycles
  double dsp_l1_eff = 0.85;          // params.dsp_l1_simd_eff
  // --- DMA
  double dma_bytes_per_cycle = 64;   // params.dma_bytes_per_cycle (noc_cache clock)
  // --- NoC
  double noc_vc_buffer_flits = 8;    // params.noc_vc_buffer_flits: buffer before backpressure
  double noc_window_ns = 10000;      // params.noc_util_window_ns: L0 utilization window
  // --- cache
  int cache_block_bytes = 1024;      // params.cache_block_bytes: tag granularity at L1
  double stream_bypass_bytes = 2.0e6;// params.stream_bypass_bytes: streams larger than this do not allocate
  // --- DRAM
  int dram_queue_depth = 32;         // params.dram_queue_depth: FR-FCFS window per channel
  double dram_l0_eff = 0.80;         // params.dram_l0_eff: L0 sustained fraction of peak
  double dram_ctrl_ns = 10;          // params.dram_ctrl_ns: controller pipeline latency
  int dram_interleave_bytes = 4096;  // params.dram_interleave_bytes: channel interleave
  double dram_window_ns = 20000;     // params.dram_util_window_ns
  // --- runtime
  double irq_ns = 2000;              // params.irq_latency_ns: completion interrupt to host
  double rt_irq_ns = 300;            // params.rt_irq_latency_ns: interrupt to RT core
  double exec_cv = 0.0;              // params.exec_cv: per-op execution-time noise (lognormal cv)
  int mapping = MAP_STATIC;          // params.mapping: static | eft
  bool middleware_on_rt = false;     // params.middleware_on_rt
  double rt_copy_bytes_per_ns = 16;  // params.rt_copy_bytes_per_ns: DMA copy into/out of TCM

  GC_IO(bulk_chunk_bytes, cpu_miss_bytes, noc_req_bytes, noc_model_requests, npu_l0_util, npu_vec_ops, npu_depth, npu_wload_overlap,
        npu_min_macs_per_tile, npu_max_tiles_per_op, npu_fp32_frac, simt_l0_eff, simt_warps_needed, simt_warp, cpu_vec_ipc,
        cpu_mem_ipc, cpu_mlp, cpu_l0_mem_bw_gbs, dsp_line_buffer_kb, dsp_fill_cycles, dsp_l1_eff, dma_bytes_per_cycle,
        noc_vc_buffer_flits, noc_window_ns, cache_block_bytes, stream_bypass_bytes, dram_queue_depth, dram_l0_eff, dram_ctrl_ns,
        dram_interleave_bytes, dram_window_ns, irq_ns, rt_irq_ns, exec_cv, mapping, middleware_on_rt, rt_copy_bytes_per_ns)
};

struct FidelityCfg {
  std::string name;
  int cpu = 0, npu = 1, simt = 0, dsp = 0, noc = 1, dram = 1, cache = 1;  // requested levels
  double quantum_ns = 100;
  ModelParams p;
  std::vector<std::string> warnings;
  GC_IO(name, cpu, npu, simt, dsp, noc, dram, cache, quantum_ns, p, warnings)
};

FidelityCfg parse_fidelity(const Json& f);
void apply_params(ModelParams& p, const Json& params, std::vector<std::string>* warnings);

// ----------------------------------------------------------------------------
// Task graphs
struct OpDef {
  std::string id;
  std::string layer;
  int type = OT_OTHER;
  int dtype = DT_INT8;
  double macs = 0, flops = 0, bytes_in = 0, bytes_w = 0, bytes_out = 0;
  double i_scalar = 0, i_vector = 0, i_mem = 0;
  double reuse = 0;
  uint32_t eligible = 0;  // bit per Engine
  std::vector<int> elig_order;  // eligible engines in graph order (static mapping picks the first)
  std::vector<int> deps;  // indices into TaskDef::ops
  bool preempt_boundary = true;
  // GEMM view (filled from shape): B x [M x K] * [K x N]
  double gM = 0, gK = 0, gN = 0, gB = 1;
  bool has_gemm = false;
  double groups = 1;
  double threads = 0;                     // SIMT threads if given
  double rows2d = 0, row_bytes2d = 0;     // DMA 2D descriptor
  double descriptors = 1;
  double l2_cycles = -1;                  // SCALE-Sim per-layer cycles if present
  GC_IO(id, layer, type, dtype, macs, flops, bytes_in, bytes_w, bytes_out, i_scalar, i_vector, i_mem, reuse, eligible, elig_order, deps,
        preempt_boundary, gM, gK, gN, gB, has_gemm, groups, threads, rows2d, row_bytes2d, descriptors, l2_cycles)
};

struct MsgDef {
  std::string topic;
  double bytes = 0;
  std::string consumer;
  GC_IO(topic, bytes, consumer)
};

struct TaskDef {
  std::string id;
  int release_kind = 0;    // 0 periodic, 1 sporadic, 2 event
  ps_t period_ps = 0;
  ps_t deadline_ps = 0;
  int crit = C_MEDIUM;
  int qos = Q_REALTIME;
  bool skip_on_overrun = false;  // release.on_overrun (default: queue)
  uint32_t affinity_engines = 0; // engine bits named in affinity
  int cpu_cluster = -1;          // affinity cpu_a -> 0, cpu_b -> 1
  double weights_bytes = 0, ws_bytes = 0;
  std::vector<OpDef> ops;
  std::vector<MsgDef> msgs;
  int64_t priority_override = -1;  // set_task_priority (lower = higher priority)
  GC_IO(id, release_kind, period_ps, deadline_ps, crit, qos, skip_on_overrun, affinity_engines, cpu_cluster, weights_bytes,
        ws_bytes, ops, msgs, priority_override)
};

TaskDef parse_task(const Json& g);

}  // namespace gc
