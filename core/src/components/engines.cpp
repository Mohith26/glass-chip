// Engine cost models (CPU, RT, NPU, SIMT, DSP, DMA at L0/L1/L2) and the
// per-op execution machine: setup, a double-buffered streaming pipeline of
// steps (L1) or a single compute segment with an analytic memory tail (L0),
// DVFS-aware compute segments, preemption and the memory-transaction glue.
#include <algorithm>
#include <cmath>

#include "glasschip/sim.hpp"

namespace gc {


static double npu_rate(const SocCfg& s, const ModelParams& p, int dtype) {
  switch (dtype) {
    case DT_INT8: return 1.0;
    case DT_FP8: return s.npu.fp8_frac;    // blocks.npu.fp8_rate_fraction
    case DT_BF16:
    case DT_FP16:
    case DT_MIXED: return s.npu.bf16_frac; // blocks.npu.bf16_rate_fraction
    default: return p.npu_fp32_frac;       // params.npu_fp32_rate_fraction
  }
}

static int mac_counter(int dtype) {
  if (dtype == DT_INT8 || dtype == DT_INT32) return K_INT8_MACS;
  if (dtype == DT_FP8) return K_FP8_MACS;
  return K_BF16_MACS;
}

double Sim::npu_gemm_cycles(double M, double K, double N, double B, int tiles, int dtype) const {
  // Closed-form systolic model on an R x C array (blocks.npu.array).
  // Weight stationary: one fold per (K/R x N/C) weight tile, each streaming
  // M input rows with R + C - 1 cycles of fill/drain (+R weight load if not
  // overlapped). Output stationary: one fold per (M/R x N/C) output tile,
  // each accumulating K steps. Folds are split evenly across tiles.
  const double R = soc_.npu.rows, C = soc_.npu.cols;
  double folds, fold_cycles;
  if (soc_.npu.weight_stationary) {
    folds = std::ceil(K / R) * std::ceil(N / C) * B;
    fold_cycles = M + R + C - 1 + (fid_.p.npu_wload_overlap ? 0 : R);  // params.npu_weight_load_overlap
  } else {
    folds = std::ceil(M / R) * std::ceil(N / C) * B;
    fold_cycles = K + R + C - 1;
  }
  double per_tile = std::ceil(folds / std::max(1, tiles));
  return per_tile * fold_cycles / npu_rate(soc_, fid_.p, dtype);
}

// Expected hit fraction of a stream in the system cache (L0 hit-rate model):
// streams above params.stream_bypass_bytes bypass; otherwise the class
// capacity is assumed shared with about one other stream of equal size.
float hit_hint(const SysCache& c, const ModelParams& p, int qos, double bytes) {
  if (bytes <= 0) return 0;
  if (bytes > p.stream_bypass_bytes) return 0;
  double cap = c.class_capacity(qos);
  return static_cast<float>(std::clamp(cap / (2.0 * bytes), 0.0, 0.9));
}

void Sim::compute_cost(const Job& j, int32_t op, int engine, int nunits, int level, OpCost& c) const {
  const TaskDef& td = tasks_[j.task];
  const TaskPlan& pl = plans_[j.task];
  const PlanOp& po = pl.ops[op];
  const ModelParams& p = fid_.p;
  nunits = std::max(1, nunits);
  c.chunk = p.bulk_chunk_bytes;  // params.bulk_chunk_bytes
  c.depth = p.npu_depth;         // params.npu_buffer_depth (double buffering for all streaming engines)
  double step_bytes = 1 << 20;

  if (po.def < 0) {
    // Synthetic middleware op (spec 7.5): executor dispatch + (de)serialization
    // + copies, costed in ns at the executing core's nominal clock so DVFS
    // scales it like any other CPU work.
    bool rx = po.def == -1;
    double bytes = rx ? pl.rx_bytes : pl.tx_bytes;
    double n = rx ? pl.n_rx : pl.n_tx;
    double ns = rx ? (mw_.dispatch_ns + mw_.deser_ns_kb * bytes / 1024.0 + mw_.fixed_ns * n)
                   : ((mw_.ser_ns_kb + mw_.copies * mw_.copy_ns_kb) * bytes / 1024.0 + mw_.fixed_ns * n);
    int dom = engine == E_RT ? D_RT : D_CPU_A;
    c.cycles = ns * soc_.nominal_hz[dom] / 1e9;
    c.rd_in = rx ? bytes : bytes * mw_.copies;
    c.wr = rx ? 0 : bytes * mw_.copies;
    if (engine == E_RT) { c.rd_in = 0; c.wr = 0; }
    c.exec_cat = A_MIDDLEWARE;
    c.act[K_CPU_INSTR] = c.cycles * (engine == E_RT ? soc_.rt.ipc : soc_.cpu[0].ipc);
    c.chunk = p.cpu_miss_bytes;
    step_bytes = p.cpu_mlp * p.cpu_miss_bytes;
    c.nsteps = static_cast<int>(std::clamp(std::ceil((c.rd_in) / step_bytes), 1.0, 4096.0));
    return;
  }

  const OpDef& d = td.ops[po.def];
  double bin = d.bytes_in * (1.0 - d.reuse);  // reuse: fraction of inputs hitting private caches/SRAM
  double fused_in = 0;
  if (engine == E_NPU && po.onchip_in > 0) {
    // Inputs produced by fused ops stay in NPU SRAM (compiler fusion pass).
    fused_in = std::min(bin, po.onchip_in);
    bin -= fused_in;
  }
  double bw = d.bytes_w, bout = d.bytes_out;
  double elems = std::max({d.flops, 2.0 * d.macs, d.bytes_in, d.bytes_out, 1.0});

  switch (engine) {
    case E_NPU: {
      double rate = npu_rate(soc_, p, d.dtype);
      const double macs_tile = soc_.npu.macs_tile;  // blocks.npu.int8_macs_per_cycle_per_tile
      bool gemm = d.macs > 0 && (d.type == OT_CONV2D || d.type == OT_DWCONV2D || d.type == OT_MATMUL || d.type == OT_ATTENTION ||
                                 d.type == OT_OTHER);
      double l2c = d.l2_cycles;
      if (l2c < 0 && level >= 2) {
        auto it = npu_l2_.find(td.id + "/" + d.id);
        if (it == npu_l2_.end()) it = npu_l2_.find(d.id);
        if (it != npu_l2_.end()) l2c = it->second;
      }
      if (level >= 2 && l2c >= 0) {
        c.cycles = l2c / nunits;  // SCALE-Sim cycles are for one tile; split across tiles
        c.l2 = true;
      } else {
        // Only systolic ops have SCALE-Sim tables; vector ops are L1 by design.
        if (level >= 2 && gemm) c.l2_fallback = true;
        if (gemm) {
          if (level == 0 || !d.has_gemm) {
            c.cycles = d.macs / (macs_tile * rate * nunits * p.npu_l0_util);  // params.npu_l0_util
          } else if (d.type == OT_DWCONV2D) {
            // Depthwise: channels packed across array columns (assumption),
            // each fold uses R*S rows only.
            double folds = std::ceil(d.gB / soc_.npu.cols);
            double fold_cycles = d.gM + soc_.npu.rows + soc_.npu.cols - 1;
            c.cycles = std::ceil(folds / nunits) * fold_cycles / rate * d.work_scale;
          } else {
            c.cycles = npu_gemm_cycles(d.gM, d.gK, d.gN, d.gB, nunits, d.dtype) * d.work_scale;
          }
        } else {
          // Vector unit for elementwise/softmax/norm/pool/resize/memcopy.
          c.cycles = elems / (p.npu_vec_ops * nunits);  // params.npu_vec_ops_per_cycle
        }
      }
      if (d.macs > 0) {
        c.act[mac_counter(d.dtype)] = d.macs;
        c.util = c.cycles > 0 ? d.macs / (c.cycles * macs_tile * rate * nunits) : 0;
      }
      // Activations produced by the previous NPU layer may stay in shared
      // NPU SRAM (blocks.npu.shared_sram_kb): no NoC traffic for them.
      bool all_pred_resident = !po.preds.empty();
      for (int32_t pi : po.preds) {
        const JobOp& pj = j.ops[pi];
        if (!(pj.resident_out && pj.engine == E_NPU)) all_pred_resident = false;
      }
      double shared = soc_.npu.shared_kb * 1024.0;
      c.sram += fused_in;
      if (all_pred_resident) { c.resident_in = true; c.sram += bin; bin = 0; }
      if (po.out_onchip) { c.resident_out = true; c.sram += bout; bout = 0; }
      bool succ_npu = !po.succs.empty();
      for (int32_t si : po.succs)
        if (!pl.ops[si].npu_only) succ_npu = false;
      if (succ_npu && bout <= shared) { c.resident_out = true; c.sram += bout; bout = 0; }
      // Tiling to SRAM: per-step working set is half the allocated tile SRAM
      // (double buffering). Inputs larger than half that are re-read once per
      // weight block.
      double sram_avail = nunits * soc_.npu.sram_tile_kb * 1024.0 / std::max(1, c.depth);  // sram_per_tile_kb
      double reread = 1;
      if (bin > sram_avail / 2 && bw > 0) reread = std::max(1.0, std::ceil(bw / (sram_avail / 2)));
      c.rd_w = bw;
      c.rd_in = bin * reread;
      c.wr = bout;
      c.sram += 2.0 * (c.rd_w + c.rd_in + c.wr);
      c.act[K_SRAM_BYTES] = c.sram;
      step_bytes = sram_avail;
      break;
    }
    case E_SIMT: {
      double df = (d.dtype == DT_FP32) ? 1.0 : (d.dtype == DT_BF16 || d.dtype == DT_FP16) ? 2.0 : 4.0;
      double flops = d.flops > 0 ? d.flops : (d.macs > 0 ? 2 * d.macs : std::max(d.bytes_in, d.bytes_out));
      double peak = nunits * soc_.simt.lanes * 2.0 * df;  // lanes_per_cu, FMA = 2 flops
      double eff;
      if (level == 0) {
        eff = p.simt_l0_eff;  // params.simt_l0_eff
      } else {
        // Occupancy: lanes covered by threads x latency hiding by resident warps.
        double lane_frac = d.threads > 0 ? std::min(1.0, d.threads / (nunits * soc_.simt.lanes)) : 1.0;
        double warps = d.threads > 0 ? d.threads / (p.simt_warp * nunits) : 1e9;
        double hide = std::min(1.0, warps / p.simt_warps_needed);  // params.simt_warps_needed_per_cu
        eff = std::max(0.02, lane_frac * hide);
      }
      c.cycles = flops / (peak * eff);
      c.setup_ps = us_to_ps(soc_.simt.launch_us);  // blocks.simt.launch_overhead_us
      double co = std::clamp(soc_.simt.coalesce, 0.05, 1.0);  // blocks.simt.coalescing_efficiency
      c.rd_w = bw / co;
      c.rd_in = bin / co;
      c.wr = bout / co;
      c.act[K_SIMT_FLOPS] = flops;
      c.util = eff;
      step_bytes = nunits * soc_.simt.smem_kb * 1024.0 / 2.0;  // shared_mem_kb_per_cu, half for double buffering
      break;
    }
    case E_CPU: {
      const auto& cp = soc_.cpu[0];
      double instr = d.i_scalar + d.i_vector + d.i_mem;
      bool done = false;
      if (level >= 2) {
        auto it = cpu_cpi_.find(d.id);
        if (it == cpu_cpi_.end()) it = cpu_cpi_.find(td.id + "/" + d.id);
        if (it == cpu_cpi_.end()) {
          static const char* tn[] = {"conv2d", "dwconv2d", "matmul", "attention", "elementwise", "softmax", "norm", "pool", "resize",
                                     "reduce", "memcopy", "cpu_kernel", "simt_kernel", "dsp_kernel", "control_law", "serialize", "other",
                                     "rx", "tx"};
          it = cpu_cpi_.find(tn[d.type]);
        }
        if (it != cpu_cpi_.end()) {
          double n = instr > 0 ? instr : std::max(1.0, elems / 8.0);
          c.cycles = n * it->second;  // per-op CPI table (L2 hook)
          c.l2 = true;
          done = true;
        } else if (std::string k = cpu_kernel_for(td, d); !k.empty()) {
          c.cycles = cpu_l2_cycles(td, d, k, 0);  // gem5 O3 (app core proxy), calib/tables/cpu_gem5.json
          c.l2 = true;
          done = true;
        } else {
          c.l2_fallback = true;
        }
      }
      if (!done) {
        if (instr > 0) {
          // Instruction-mix model: blocks.cpu_*.ipc_scalar, params.cpu_vector_ipc, params.cpu_mem_ipc
          c.cycles = d.i_scalar / cp.ipc + d.i_vector / p.cpu_vec_ipc + d.i_mem / p.cpu_mem_ipc;
        } else if (d.type == OT_MEMCOPY) {
          c.cycles = d.bytes_in / 16.0;  // 16 B/cycle memcpy (assumption)
        } else {
          double df = (d.dtype == DT_INT8 || d.dtype == DT_FP8) ? 4.0 : (d.dtype == DT_BF16 || d.dtype == DT_FP16) ? 2.0 : 1.0;
          double flops = d.flops > 0 ? d.flops : 2 * d.macs;
          c.cycles = flops > 0 ? flops / (cp.vflops * df) : std::max(1.0, d.bytes_in / 16.0);  // vector_flops_per_cycle
        }
      }
      c.act[K_CPU_INSTR] = instr > 0 ? instr : c.cycles * cp.ipc;
      c.act[K_VEC_OPS] = d.i_vector > 0 ? d.i_vector : (d.flops > 0 ? d.flops / 8.0 : 0);
      c.rd_w = bw * (1.0 - d.reuse);
      c.rd_in = bin;
      c.wr = bout;
      c.chunk = p.cpu_miss_bytes;  // params.cpu_miss_bytes
      step_bytes = p.cpu_mlp * p.cpu_miss_bytes;  // params.cpu_mlp outstanding misses
      c.node_bw_bytes_per_ps = p.cpu_l0_mem_bw_gbs * 1e9 / 1e12;
      break;
    }
    case E_RT: {
      // RT core runs from TCM (blocks.rt.tcm_kb): no cache misses to DRAM.
      double instr = d.i_scalar + d.i_vector * 4 + d.i_mem;  // no vector unit: 4 scalar ops per vector op (assumption)
      std::string k = fid_.cpu >= 2 ? cpu_kernel_for(td, d) : std::string();
      if (!k.empty()) {
        c.cycles = cpu_l2_cycles(td, d, k, 1);  // gem5 Minor (in-order RT proxy)
        c.l2 = true;
      } else if (instr > 0) {
        if (fid_.cpu >= 2) c.l2_fallback = true;
        c.cycles = instr / soc_.rt.ipc;  // blocks.rt.ipc_scalar
      } else {
        c.cycles = std::max(1.0, std::max(d.flops, 2 * d.macs) / soc_.rt.ipc);
      }
      c.act[K_CPU_INSTR] = instr > 0 ? instr : c.cycles * soc_.rt.ipc;
      c.nsteps = 1;
      return;
    }
    case E_DSP: {
      double df = (d.dtype == DT_INT8 || d.dtype == DT_FP8) ? 1.0 : (d.dtype == DT_BF16 || d.dtype == DT_FP16) ? 0.5 : 0.25;
      double ops = d.macs > 0 ? d.macs : (d.flops > 0 ? d.flops : std::max(d.bytes_in, d.bytes_out));
      c.cycles = ops / (soc_.dsp.ops_int8 * df);  // blocks.dsp.ops_per_cycle_int8
      if (level >= 1) c.cycles /= p.dsp_l1_eff;    // params.dsp_l1_simd_eff
      c.act[K_DSP_OPS] = ops;
      c.rd_w = bw;
      c.rd_in = bin;
      c.wr = bout;
      step_bytes = p.dsp_line_buffer_kb * 1024.0;  // params.dsp_line_buffer_kb
      break;
    }
    case E_DMA: {
      double bytes = std::max(d.bytes_in, d.bytes_out);
      c.cycles = bytes / p.dma_bytes_per_cycle;  // params.dma_bytes_per_cycle at the noc_cache clock
      c.setup_ps = ns_to_ps(soc_.dma.desc_ns * d.descriptors);  // blocks.dma.descriptor_latency_ns per descriptor
      c.rd_in = bytes;
      c.wr = bytes;
      if (d.rows2d > 0 && d.row_bytes2d > 0) c.chunk = static_cast<int>(std::clamp(d.row_bytes2d, 64.0, static_cast<double>(p.bulk_chunk_bytes)));
      c.act[K_DMA_BYTES] = 2 * bytes;
      c.exec_cat = A_DMA;
      step_bytes = 16.0 * p.bulk_chunk_bytes;
      break;
    }
    default: break;
  }
  if (level >= 1 && engine == E_DSP) c.cycles += p.dsp_fill_cycles;  // params.dsp_pipeline_fill_cycles
  double reads = c.rd_w + c.rd_in;
  c.nsteps = static_cast<int>(std::clamp(std::ceil(reads / std::max(1.0, step_bytes)), 1.0, 4096.0));
}

double Sim::est_cycles(const Job& j, int32_t op, int engine) const {
  if (engine < 0) return 0;
  // Compute cycles at the engine's own fidelity level, on the units the op
  // would get, i.e. wall-clock cycles at the domain clock if it ran alone.
  OpCost c;
  compute_cost(j, op, engine, want_units(j, op, engine), engine == E_RT ? 0 : eng_[engine].level, c);
  return c.cycles;
}

// ---------------------------------------------------------------------------
int32_t Sim::alloc_exec() {
  int32_t x;
  if (!free_execs_.empty()) {
    x = free_execs_.back();
    free_execs_.pop_back();
    execs_[x] = Exec{};
  } else {
    x = static_cast<int32_t>(execs_.size());
    execs_.emplace_back();
  }
  execs_[x].live = true;
  execs_[x].uid = next_uid_++;
  return x;
}

void Sim::free_exec(int32_t x) {
  execs_[x].live = false;
  execs_[x].slots.clear();
  execs_[x].fluid_links.clear();
  execs_[x].units.clear();
  free_execs_.push_back(x);
}

static int act_domain(int k, int exec_domain) {
  if (k == K_FLIT_HOPS || k == K_CACHE_BYTES || k == K_DMA_BYTES) return D_NOC_CACHE;
  if (k == K_DRAM_BYTES || k == K_DRAM_ACTIVATES) return D_DRAM_IO;
  return exec_domain;
}

double Sim::exec_progress(const Exec& e) const {
  double done = e.next_compute;
  if (e.computing && e.seg_total > 0) {
    const DomainState& d = dom_[e.domain];
    double el = static_cast<double>(std::max<ps_t>(0, now_ - std::max(e.seg_last, d.stall_until)));
    double rem = std::max(0.0, e.seg_rem - el * d.hz / 1e12);
    done += std::clamp(1.0 - rem / e.seg_total, 0.0, 1.0) * (e.seg_total / std::max(1e-9, e.cyc_per_step));
  } else if (e.partial && e.seg_total > 0) {
    done += std::clamp(1.0 - e.seg_rem / e.seg_total, 0.0, 1.0) * (e.seg_total / std::max(1e-9, e.cyc_per_step));
  }
  return std::clamp(done / std::max(1, e.nsteps), 0.0, 1.0);
}

void Sim::exec_credit(Exec& e) {
  double f = exec_progress(e);
  double delta = f - e.act_credited;
  if (delta <= 0) return;
  for (int k = 0; k < N_ACT; ++k)
    if (e.act[k] != 0) dom_[act_domain(k, e.domain)].act[k] += e.act[k] * delta;
  e.act_credited = f;
}

void Sim::exec_start_segment(int32_t x, double cycles) {
  Exec& e = execs_[x];
  const DomainState& d = dom_[e.domain];
  e.computing = true;
  e.seg_start = now_;
  e.seg_last = now_;
  e.seg_rem = cycles;
  e.seg_total = cycles;
  e.seg_dvfs = d.stall_until > now_ ? d.stall_until - now_ : 0;  // domain stalled by a DVFS transition
  e.gen++;
  ps_t end = std::max(now_, d.stall_until) + cycles_to_ps(cycles, d.hz);
  e.est_end = end;
  q_.push(end, CMP_EXEC, EV_COMPUTE_END, x, e.uid, e.gen);
}

void Sim::exec_reschedule_segment(int32_t x) {
  Exec& e = execs_[x];
  const DomainState& d = dom_[e.domain];
  e.gen++;
  ps_t end = std::max(now_, d.stall_until) + cycles_to_ps(e.seg_rem, d.hz);
  e.est_end = end;
  q_.push(end, CMP_EXEC, EV_COMPUTE_END, x, e.uid, e.gen);
}

XY Sim::cache_node(uint64_t addr) const {
  const auto& n = soc_.cache.nodes;
  uint64_t blk = addr / static_cast<uint64_t>(fid_.p.bulk_chunk_bytes);
  return n[static_cast<size_t>((blk ^ (blk >> 7)) % n.size())];
}

XY Sim::dram_node(int ch) const {
  const auto& n = soc_.dram.nodes;
  return n[static_cast<size_t>(dram_.controller_of(ch)) % n.size()];
}

int32_t Sim::alloc_txn(const MemTxn& t) {
  int32_t i;
  if (!free_txns_.empty()) {
    i = free_txns_.back();
    free_txns_.pop_back();
    txns_[i] = t;
  } else {
    i = static_cast<int32_t>(txns_.size());
    txns_.push_back(t);
  }
  return i;
}

void Sim::emit_xfer(ps_t t, ps_t arrival, XY a, XY b, int bytes, int64_t job, int task, int op) {
  TraceRec r;
  r.t = t;
  r.dur = arrival - t;
  r.kind = TK_XFER;
  r.block = TB_NOC;
  r.job = job;
  r.task = task;
  r.op = op;
  r.sx = a.x; r.sy = a.y; r.dx = b.x; r.dy = b.y;
  r.bytes = bytes;
  emit(r);
}

void Sim::schedule_noc_tick(ps_t t) {
  if (noc_tick_pending_) return;
  ps_t q = quantum_ps();
  noc_tick_at_ = ((t + q - 1) / q) * q;
  noc_tick_pending_ = true;
  q_.push(noc_tick_at_, CMP_DRAM, EV_NOC_BACKEND_TICK);
}

void Sim::noc_deliver(MemTxn txn, XY a, int bytes, ps_t t) {
  if (!noc_backend_) {
    ps_t arr = noc_xfer(a, txn.src, bytes, txn.qos, t, false, txn.job, txn.task, txn.op);
    txn.noc_ps += arr - t;
    mem_deliver(txn, arr);
    return;
  }
  // L2: BookSim decides when the data arrives.
  int32_t i;
  if (!noc_wait_free_.empty()) {
    i = noc_wait_free_.back();
    noc_wait_free_.pop_back();
  } else {
    i = static_cast<int32_t>(noc_wait_.size());
    noc_wait_.emplace_back();
  }
  txn.t = t;
  txn.channel = (a.y << 8) | a.x;  // remember the source node for the trace
  noc_wait_[i] = txn;
  uint64_t fh = 0;
  noc_.header_latency(a, txn.src, bytes, &fh);
  dom_[D_NOC_CACHE].act[K_FLIT_HOPS] += static_cast<double>(fh);
  noc_backend_->inject(static_cast<uint64_t>(i) + 1, a, txn.src, bytes, t);
  schedule_noc_tick(t);
}

void Sim::on_noc_backend_tick() {
  noc_tick_pending_ = false;
  if (!noc_backend_) return;
  noc_done_.clear();
  noc_backend_->tick(now_, noc_done_);
  for (const auto& pr : noc_done_) {
    int32_t i = static_cast<int32_t>(pr.first) - 1;
    MemTxn txn = noc_wait_[i];
    noc_wait_free_.push_back(i);
    XY a{static_cast<int8_t>(txn.channel & 0xff), static_cast<int8_t>(txn.channel >> 8)};
    emit_xfer(txn.t, pr.second, a, txn.src, txn.bytes, txn.job, txn.task, txn.op);
    txn.noc_ps += pr.second - txn.t;
    mem_deliver(txn, pr.second);  // arrival can be up to one quantum before now
  }
  if (noc_backend_->busy()) {
    noc_tick_pending_ = true;
    noc_tick_at_ = now_ + quantum_ps();
    q_.push(noc_tick_at_, CMP_DRAM, EV_NOC_BACKEND_TICK);
  }
}

ps_t Sim::noc_xfer(XY a, XY b, int bytes, int qos, ps_t t, bool header, int64_t job, int task, int op) {
  uint64_t fh = 0;
  ps_t arr;
  if (noc_backend_) {
    // L2: the message loads BookSim (contention for everyone else); its own
    // timing on this synchronous path is the zero-load latency.
    arr = t + noc_.header_latency(a, b, bytes, &fh);
    noc_backend_->inject(0, a, b, bytes, t);
    schedule_noc_tick(t);
  } else {
    arr = (header && !fid_.p.noc_model_requests) ? t + noc_.header_latency(a, b, bytes, &fh) : noc_.send(a, b, bytes, qos, t, &fh);
  }
  dom_[D_NOC_CACHE].act[K_FLIT_HOPS] += static_cast<double>(fh);
  if (!header) {
    // Timing faults on data messages (deterministic per seed).
    FaultCfg& fd = faults_[F_NOC_DELAY];
    if (fd.rate > 0 && fd.rng.uniform() < fd.rate) {
      arr += fd.param;
      note_fault(F_NOC_DELAY, job, task, op, t, static_cast<double>(fd.param));
    }
    FaultCfg& fr = faults_[F_NOC_DROP];
    if (fr.rate > 0 && fr.rng.uniform() < fr.rate) {
      // Dropped: the sender times out and retransmits, reserving the path again.
      ps_t first = arr;
      arr = noc_.send(a, b, bytes, qos, arr + fr.param, &fh);
      note_fault(F_NOC_DROP, job, task, op, t, static_cast<double>(arr - first));
    }
    emit_xfer(t, arr, a, b, bytes, job, task, op);
  }
  if (noc_csv_.f && noc_csv_.per_message) {
    // One row per message: latency to its last packet's arrival (what the simulator waits for).
    CsvRow r;
    r.t = t;
    r.a = static_cast<uint64_t>(bytes);
    r.b = a.x; r.c = a.y; r.d = b.x; r.e = b.y;
    r.lat = arr - t;
    csv_push(noc_csv_, r);
  } else if (noc_csv_.f) {
    // One row per packet (max_packet_bytes); packet k's tail arrives
    // ser(k+1 packets) after the head of the train. The message-level
    // round-robin tail term is excluded: round robin between multi-packet
    // messages delays message tails but not the per-packet mean.
    const int pk = soc_.max_packet_bytes;
    ps_t head = arr - (noc_backend_ ? 0 : noc_.last_rr()) - noc_.ser_ps(bytes);
    for (int off = 0; off < bytes; off += pk) {
      int nb = std::min(pk, bytes - off);
      CsvRow r;
      r.t = t;
      r.a = static_cast<uint64_t>(nb);
      r.b = a.x; r.c = a.y; r.d = b.x; r.e = b.y;
      r.lat = std::max<ps_t>(head + noc_.ser_ps(off + nb), t + noc_.ser_ps(nb)) - t;
      csv_push(noc_csv_, r);
    }
  }
  return arr;
}

void Sim::csv_push(CsvTrace& c, const CsvRow& r0) {
  CsvRow r = r0;
  r.seq = c.seq++;
  if (r.t < c.flushed) c.late++;
  c.heap.push_back(r);
  std::push_heap(c.heap.begin(), c.heap.end(), [](const CsvRow& x, const CsvRow& y) { return x.t > y.t || (x.t == y.t && x.seq > y.seq); });
}

void Sim::csv_flush(CsvTrace& c, ps_t upto, bool dram) {
  if (!c.f) return;
  auto later = [](const CsvRow& x, const CsvRow& y) { return x.t > y.t || (x.t == y.t && x.seq > y.seq); };
  while (!c.heap.empty() && c.heap.front().t <= upto) {
    std::pop_heap(c.heap.begin(), c.heap.end(), later);
    const CsvRow r = c.heap.back();
    c.heap.pop_back();
    if (dram)
      std::fprintf(c.f, "%.3f,0x%llx,%c,%lld,%.3f\n", r.t / 1000.0, static_cast<unsigned long long>(r.a), r.b ? 'W' : 'R',
                   static_cast<long long>(r.c), r.lat / 1000.0);
    else
      std::fprintf(c.f, "%.3f,%lld,%lld,%lld,%lld,%llu,%.3f\n", r.t / 1000.0, static_cast<long long>(r.b), static_cast<long long>(r.c),
                   static_cast<long long>(r.d), static_cast<long long>(r.e), static_cast<unsigned long long>(r.a), r.lat / 1000.0);
    c.rows++;
  }
  c.flushed = std::max(c.flushed, upto);
}

void Sim::dram_csv_row(ps_t arrive, uint64_t addr, bool write, int bytes, ps_t lat) {
  if (!dram_csv_.f) return;
  CsvRow r;
  r.t = arrive;
  r.a = addr;
  r.b = write ? 1 : 0;
  r.c = bytes;
  r.lat = lat;
  csv_push(dram_csv_, r);
}

// Sends a DRAM-bound transaction from node `from` (a cache slice).
void Sim::mem_dram(MemTxn& txn, XY from) {
  txn.addr = phys_addr(txn.addr);
  int ch = dram_.channel_of(txn.addr);
  XY dn = dram_node(ch);
  ps_t t3 = noc_xfer(from, dn, txn.write ? txn.bytes : fid_.p.noc_req_bytes, txn.qos, txn.t, !txn.write, txn.job, txn.task, txn.op);
  txn.noc_ps += t3 - txn.t;
  txn.t = t3;
  txn.channel = ch;
  if (dram_.level() == 0) {
    ps_t qps = 0;
    ps_t lat = dram_.l0_latency(ch, txn.bytes, t3, &qps);
    dom_[D_DRAM_IO].act[K_DRAM_BYTES] += txn.bytes;
    dom_[D_DRAM_IO].act[K_DRAM_ACTIVATES] += 0.5 * txn.bytes / soc_.dram.row_bytes;  // L0: 50% row-hit assumption
    txn.dram_ps = lat;
    dram_csv_row(t3, txn.addr, txn.write, txn.bytes, lat);
    if (!txn.write) noc_deliver(txn, dn, txn.bytes, t3 + lat);
    return;
  }
  if (dram_backend_) {
    // L2: hand the request to the external DRAM model; it is ticked in
    // quantum_ns steps (time-quantum synchronization) while it has work.
    txn.t = t3;
    dram_backend_->enqueue(txn, t3);
    if (!dram_tick_pending_ || dram_tick_at_ < t3) {
      if (!dram_tick_pending_) {
        ps_t q = quantum_ps();
        dram_tick_at_ = ((t3 + q - 1) / q) * q;
        dram_tick_pending_ = true;
        q_.push(dram_tick_at_, CMP_DRAM, EV_DRAM_BACKEND_TICK);
      }
    }
    return;
  }
  // L1: the request joins the controller's arrival heap; the scheduler
  // wakes at the earliest arrival (no separate arrival event).
  int32_t idx = alloc_txn(txn);
  DramReq r;
  r.txn = idx;
  r.arrive = t3;
  r.seq = dram_seq_++;
  dram_.map(txn.addr, &r.bank, &r.row);
  r.write = txn.write;
  r.addr = txn.addr;
  r.bytes = txn.bytes;
  r.qos = static_cast<int8_t>(txn.qos);
  dram_.add_pending(ch, r);
  DramChannel& c = dram_.channel_mut(ch);
  if (!c.decide_pending || t3 < c.decide_at) {
    c.decide_pending = true;
    c.decide_at = t3;
    q_.push(t3, CMP_DRAM, EV_DRAM_DECIDE, ch, ++c.decide_token);
  }
}

void Sim::mem_deliver(const MemTxn& txn, ps_t arrival) {
  if (txn.exec < 0) return;
  Exec& e = execs_[txn.exec];
  if (!e.live || e.uid != txn.exec_uid) return;
  StepSlot& s = e.slots[txn.slot];
  s.outstanding--;
  if (arrival >= s.ready) {
    s.ready = arrival;
    s.crit = {txn.noc_ps, txn.cache_ps, txn.dram_ps};
    s.crit_issue = txn.issue_ps;
  }
  if (s.outstanding == 0 && e.waiting && !e.computing && (e.next_compute % e.depth) == txn.slot)
    q_.push(std::max(now_, s.ready), CMP_EXEC, EV_DATA_READY, txn.exec, e.uid, e.gen);
}

void Sim::mem_read(Exec& e, int32_t x, int slot, uint64_t addr, int bytes, bool alloc, float l0_hit) {
  MemTxn txn;
  txn.issue_ps = now_;
  txn.t = now_;
  txn.addr = addr;
  txn.bytes = bytes;
  txn.qos = static_cast<int8_t>(e.qos);
  txn.alloc = alloc;
  // Multi-tile ops spread their chunks across the tiles' NoC nodes.
  size_t ui = e.units.size() > 1 ? static_cast<size_t>((addr / static_cast<uint64_t>(e.chunk)) % e.units.size()) : 0;
  txn.src = units_[e.units[ui]].node;
  txn.exec = x;
  txn.exec_uid = e.uid;
  txn.slot = static_cast<int16_t>(slot);
  txn.job = e.job;
  txn.task = e.task;
  txn.op = e.op;
  txn.l0_hit = l0_hit;
  XY cn = cache_node(addr);
  ps_t t1 = noc_xfer(txn.src, cn, fid_.p.noc_req_bytes, txn.qos, now_, true, txn.job, txn.task, txn.op);
  txn.noc_ps = t1 - now_;
  ps_t hit_lat = cycles_to_ps(soc_.cache.hit_cycles, noc_.freq());  // blocks.sys_cache.hit_latency_cycles
  wb_scratch_.clear();
  int hb = cache_.access(addr, bytes, txn.qos, false, alloc, l0_hit, static_cast<uint64_t>(x), &wb_scratch_);
  dom_[D_NOC_CACHE].act[K_CACHE_BYTES] += bytes;
  for (uint64_t wa : wb_scratch_) {
    MemTxn w;
    w.issue_ps = now_;
    w.t = t1 + hit_lat;
    w.addr = wa;
    w.bytes = cache_.block();
    w.qos = Q_BEST_EFFORT;
    w.write = true;
    w.src = cn;
    mem_dram(w, cn);
  }
  StepSlot& s = e.slots[slot];
  if (hb > 0) {
    MemTxn h = txn;
    h.bytes = hb;
    h.cache_ps = hit_lat;
    s.outstanding++;
    noc_deliver(h, cn, hb, t1 + hit_lat);
  }
  int miss = bytes - hb;
  if (miss > 0) {
    txn.bytes = miss;
    txn.t = t1 + hit_lat;
    txn.cache_ps = hit_lat;
    execs_[x].slots[slot].outstanding++;
    mem_dram(txn, cn);
  }
}

void Sim::mem_write(int32_t /*x*/, uint64_t addr, int bytes, bool alloc, float l0_hit, int qos, XY src, int64_t job, int task, int op) {
  XY cn = cache_node(addr);
  ps_t t1 = noc_xfer(src, cn, bytes, qos, now_, false, job, task, op);
  wb_scratch_.clear();
  int absorbed = cache_.access(addr, bytes, qos, true, alloc, l0_hit, 0, &wb_scratch_);
  dom_[D_NOC_CACHE].act[K_CACHE_BYTES] += bytes;
  ps_t hit_lat = cycles_to_ps(soc_.cache.hit_cycles, noc_.freq());
  std::vector<uint64_t> wbs = wb_scratch_;
  for (uint64_t wa : wbs) {
    MemTxn w;
    w.issue_ps = now_;
    w.t = t1 + hit_lat;
    w.addr = wa;
    w.bytes = cache_.block();
    w.qos = Q_BEST_EFFORT;
    w.write = true;
    w.src = cn;
    mem_dram(w, cn);
  }
  int rest = bytes - absorbed;
  if (rest > 0) {
    MemTxn w;
    w.issue_ps = now_;
    w.t = t1 + hit_lat;
    w.addr = addr;
    w.bytes = rest;
    w.qos = static_cast<int8_t>(qos);
    w.write = true;
    w.src = src;
    w.job = job;
    w.task = task;
    w.op = op;
    mem_dram(w, cn);
  }
}

void Sim::on_dram_backend_tick() {
  dram_tick_pending_ = false;
  if (!dram_backend_) return;
  backend_done_.clear();
  dram_backend_->tick(now_, backend_done_);
  for (auto& pr : backend_done_) {
    MemTxn& txn = pr.first;
    ps_t done = pr.second + ns_to_ps(fid_.p.dram_ctrl_ns);
    dom_[D_DRAM_IO].act[K_DRAM_BYTES] += txn.bytes;
    txn.dram_ps = done - txn.t;
    dram_csv_row(txn.t, txn.addr, txn.write, txn.bytes, done - txn.t);
    if (!txn.write) {
      XY dn = dram_node(dram_.channel_of(txn.addr));
      // `done` can be up to one quantum in the past: the response is reserved
      // from the true completion time and the requester sees it no earlier than now.
      noc_deliver(txn, dn, txn.bytes, done);
    }
  }
  if (dram_backend_->busy()) {
    dram_tick_pending_ = true;
    dram_tick_at_ = now_ + quantum_ps();
    q_.push(dram_tick_at_, CMP_DRAM, EV_DRAM_BACKEND_TICK);
  }
}

void Sim::on_dram_arrive(int32_t idx) {
  MemTxn& t = txns_[idx];
  DramReq r;
  r.txn = idx;
  r.arrive = now_;
  r.seq = dram_seq_++;
  dram_.map(t.addr, &r.bank, &r.row);
  r.write = t.write;
  r.addr = t.addr;
  r.bytes = t.bytes;
  r.qos = static_cast<int8_t>(qos_on_ ? t.qos : 0);
  dram_.add_pending(t.channel, r);
  DramChannel& c = dram_.channel_mut(t.channel);
  if (!c.decide_pending || now_ < c.decide_at) {
    c.decide_pending = true;
    c.decide_at = now_;
    q_.push(now_, CMP_DRAM, EV_DRAM_DECIDE, t.channel, ++c.decide_token);
  }
}

void Sim::on_dram_decide(int ch, int64_t token) {
  DramChannel& c0 = dram_.channel_mut(ch);
  if (!c0.decide_pending || token != c0.decide_token) return;  // superseded wakeup
  DramDecision d = dram_.decide(ch, now_, qos_on_);
  DramChannel& c = dram_.channel_mut(ch);
  if (!d.issued) {
    if (d.more) {
      c.decide_at = d.next_decide;
      q_.push(d.next_decide, CMP_DRAM, EV_DRAM_DECIDE, ch, ++c.decide_token);
    } else {
      c.decide_pending = false;
    }
    return;
  }
  MemTxn txn = txns_[d.txn];
  free_txns_.push_back(d.txn);
  dom_[D_DRAM_IO].act[K_DRAM_BYTES] += txn.bytes;
  dom_[D_DRAM_IO].act[K_DRAM_ACTIVATES] += d.acts;
  txn.dram_ps = d.data_end - txn.t;
  dram_csv_row(txn.t, txn.addr, txn.write, txn.bytes, d.data_end - txn.t);
  if (!txn.write) {
    noc_deliver(txn, dram_node(ch), txn.bytes, d.data_end);
  }
  // Queue-depth trace sample whenever the depth bucket changes.
  int bucket = c.qlen / 4;
  if (static_cast<size_t>(ch) < last_dram_q_.size() && last_dram_q_[ch] != bucket) {
    last_dram_q_[ch] = bucket;
    TraceRec r;
    r.t = now_;
    r.kind = TK_DRAM_Q;
    r.block = TB_DRAM;
    r.unit = static_cast<uint16_t>(ch);
    r.value = c.qlen;
    emit(r);
  }
  if (d.more) {
    c.decide_at = d.next_decide;
    q_.push(d.next_decide, CMP_DRAM, EV_DRAM_DECIDE, ch, ++c.decide_token);
  } else {
    c.decide_pending = false;
  }
}

// ---------------------------------------------------------------------------
// Exec machine
void Sim::exec_issue_step(int32_t x, int32_t step) {
  Exec& e = execs_[x];
  int si = step % e.depth;
  StepSlot& s = e.slots[si];
  s = StepSlot{};
  s.outstanding = 1;  // sentinel while issuing
  s.ready = now_;
  s.issued = true;
  s.crit_issue = now_;
  bool last = step == e.nsteps - 1;
  for (Stream* st : {&e.rd_w, &e.rd_in}) {
    if (st->total <= 0) continue;
    double want = last ? st->total - st->cursor : st->total / e.nsteps;
    int64_t left = static_cast<int64_t>(std::llround(want));
    while (left > 0) {
      int n = static_cast<int>(std::min<int64_t>(left, e.chunk));
      double off = std::fmod(st->cursor, std::max(64.0, st->size));
      if (st->row > 0 && st->stride > st->row) off = std::floor(off / st->row) * st->stride + std::fmod(off, st->row);
      uint64_t addr = st->base + (static_cast<uint64_t>(off) & ~uint64_t{63});
      mem_read(execs_[x], x, si, addr, n, st->alloc, st->l0_hit);
      st->cursor += n;
      left -= n;
    }
  }
  Exec& e2 = execs_[x];
  StepSlot& s2 = e2.slots[si];
  s2.outstanding--;
  if (s2.outstanding == 0 && e2.waiting && !e2.computing && (e2.next_compute % e2.depth) == si)
    q_.push(std::max(now_, s2.ready), CMP_EXEC, EV_DATA_READY, x, e2.uid, e2.gen);
}

void Sim::exec_issue_writes(int32_t x, double bytes) {
  Exec& e = execs_[x];
  int64_t left = static_cast<int64_t>(std::llround(bytes));
  XY src = units_[e.units[0]].node;
  while (left > 0) {
    int n = static_cast<int>(std::min<int64_t>(left, e.chunk));
    double off = std::fmod(e.wr.cursor, std::max(64.0, e.wr.size));
    uint64_t addr = e.wr.base + (static_cast<uint64_t>(off) & ~uint64_t{63});
    mem_write(x, addr, n, e.wr.alloc, e.wr.l0_hit, e.qos, src, e.job, e.task, e.op);
    e.wr.cursor += n;
    left -= n;
  }
}

void Sim::exec_begin(int32_t x) {
  Exec& e = execs_[x];
  e.in_setup = false;
  e.begun = true;
  if (e.level == 0 || e.engine == E_RT) {
    // Single compute segment; memory handled analytically (tail).
    e.mem_done = now_ + e.mem_time;
    exec_try_compute(x);
    return;
  }
  int n = std::min(e.depth, e.nsteps);
  for (int s = 0; s < n; ++s) {
    e.next_issue = s + 1;
    exec_issue_step(x, s);
  }
  exec_try_compute(x);
}

void Sim::exec_try_compute(int32_t x) {
  Exec& e = execs_[x];
  if (!e.live || e.computing || e.in_setup || e.suspended || e.in_tail) return;
  if (e.preempt_req) {
    exec_suspend(x);
    dispatch(e.engine);
    return;
  }
  if (e.partial) {
    e.partial = false;
    if (e.waiting) e.waiting = false;
    exec_start_segment(x, e.seg_rem);
    return;
  }
  if (e.next_compute >= e.nsteps) {
    exec_finish(x);
    return;
  }
  StepSlot& s = e.slots[e.next_compute % e.depth];
  if (!s.issued || s.outstanding > 0) {
    if (!e.waiting) { e.waiting = true; e.wait_start = now_; }
    return;
  }
  if (s.ready > now_) {
    if (!e.waiting) { e.waiting = true; e.wait_start = now_; }
    q_.push(s.ready, CMP_EXEC, EV_DATA_READY, x, e.uid, e.gen);
    return;
  }
  if (e.waiting) {
    // Memory stall: split by the latency components of the last-arriving chunk.
    ps_t stall = now_ - e.wait_start;
    double n = static_cast<double>(s.crit[0]), c = static_cast<double>(s.crit[1]), d = static_cast<double>(s.crit[2]);
    double sum = n + c + d;
    if (stall > 0) {
      if (sum <= 0) {
        e.cat[A_DRAM] += stall;
      } else {
        ps_t pn = static_cast<ps_t>(stall * n / sum), pc = static_cast<ps_t>(stall * c / sum);
        e.cat[A_NOC] += pn;
        e.cat[A_CACHE] += pc;
        e.cat[A_DRAM] += stall - pn - pc;
      }
    }
    e.waiting = false;
  }
  bool last = e.next_compute == e.nsteps - 1;
  double cyc = last ? std::max(0.0, e.cycles_total - e.cyc_per_step * (e.nsteps - 1)) : e.cyc_per_step;
  exec_start_segment(x, cyc);
}

void Sim::exec_compute_end(int32_t x) {
  Exec& e = execs_[x];
  e.computing = false;
  ps_t wall = now_ - e.seg_start;
  ps_t dv = std::min(e.seg_dvfs, wall);
  e.cat[e.exec_cat] += wall - dv;
  e.cat[A_DVFS] += dv;
  credit_job_cycles(e, e.seg_total);
  e.seg_rem = 0;
  e.next_compute++;
  exec_credit(e);
  if (e.level >= 1 && e.engine != E_RT) {
    if (e.wr.total > 0) {
      bool last = e.next_compute == e.nsteps;
      double w = last ? e.wr.total - e.wr.cursor : e.wr.total / e.nsteps;
      exec_issue_writes(x, w);
    }
    Exec& e2 = execs_[x];
    if (e2.next_issue < e2.nsteps) {
      int32_t s = e2.next_issue++;
      exec_issue_step(x, s);
    }
  }
  Exec& e3 = execs_[x];
  if (e3.next_compute >= e3.nsteps) {
    if (e3.mem_done > now_ && (e3.level == 0 || e3.engine == E_RT)) {
      // L0 roofline: memory-bound ops finish when their traffic completes.
      ps_t tail = e3.mem_done - now_;
      ps_t pn = static_cast<ps_t>(tail * e3.mem_noc_frac);
      e3.cat[A_NOC] += pn;
      e3.cat[A_DRAM] += tail - pn;
      e3.in_tail = true;
      e3.gen++;
      q_.push(e3.mem_done, CMP_EXEC, EV_MEM_DONE, x, e3.uid, e3.gen);
      return;
    }
    exec_finish(x);
    return;
  }
  if (e3.preempt_req) {
    exec_suspend(x);
    dispatch(e3.engine);
    return;
  }
  exec_try_compute(x);
}

void Sim::l0_register_fluid(Exec& e, double dram_bytes, double noc_bytes, ps_t dur) {
  if (dur <= 0) return;
  e.fluid_dram = dram_bytes / static_cast<double>(dur);
  e.fluid_noc = noc_bytes / static_cast<double>(dur) / static_cast<double>(soc_.cache.nodes.size());
  if (e.fluid_dram > 0) dram_.add_fluid(e.fluid_dram);
  if (e.fluid_noc > 0) {
    XY src = units_[e.units[0]].node;
    for (const XY& cn : soc_.cache.nodes) {
      noc_.add_fluid(cn, src, e.fluid_noc, &e.fluid_links);
      noc_.add_fluid(src, cn, e.fluid_noc, &e.fluid_links);
    }
  }
}

void Sim::l0_unregister_fluid(Exec& e) {
  if (e.fluid_dram > 0) dram_.add_fluid(-e.fluid_dram);
  if (e.fluid_noc > 0) noc_.remove_fluid(e.fluid_links, e.fluid_noc);
  e.fluid_dram = 0;
  e.fluid_noc = 0;
  e.fluid_links.clear();
}

void Sim::exec_suspend(int32_t x) {
  Exec& e = execs_[x];
  if (e.computing) {
    exec_credit(e);
    const DomainState& d = dom_[e.domain];
    double el = static_cast<double>(std::max<ps_t>(0, now_ - std::max(e.seg_last, d.stall_until)));
    e.seg_rem = std::max(0.0, e.seg_rem - el * d.hz / 1e12);
    ps_t wall = now_ - e.seg_start;
    ps_t dv = std::min(e.seg_dvfs, wall);
    e.cat[e.exec_cat] += wall - dv;
    e.cat[A_DVFS] += dv;
    credit_job_cycles(e, e.seg_total - e.seg_rem);
    e.computing = false;
    e.partial = true;
    e.gen++;
  }
  if (e.waiting) {
    e.cat[A_DRAM] += now_ - e.wait_start;
    e.waiting = false;
  }
  if (e.level == 0 || e.engine == E_RT) {
    e.mem_time = std::max<ps_t>(0, e.mem_done - now_);
    l0_unregister_fluid(e);
  }
  credit_job_busy(e);
  for (int32_t u : e.units) {
    unit_busy(u, false);
    units_[u].exec = -1;
    units_[u].last_job = e.job;
  }
  e.suspended = true;
  e.suspended_at = now_;
  e.preempt_req = false;
  TraceRec r;
  r.t = now_;
  r.kind = TK_PREEMPT;
  r.block = static_cast<uint8_t>(units_[e.units[0]].block);
  r.unit = static_cast<uint16_t>(units_[e.units[0]].index);
  r.job = e.job;
  r.task = e.task;
  r.op = e.op;
  emit(r);
  ReadyItem it;
  it.k1 = e.k1;
  it.k2 = e.k2;
  it.seq = ready_seq_++;
  it.job = e.job;
  it.op = e.op;
  it.exec = x;
  it.unit_mask = e.unit_mask;
  it.ready = now_;
  eng_[e.engine].ready.push_back(it);
}

void Sim::exec_resume(int32_t x, const std::vector<int32_t>& units, bool ctx_switch) {
  Exec& e = execs_[x];
  e.suspended = false;
  e.cat[A_PREEMPTION] += now_ - e.suspended_at;
  e.units = units;
  e.domain = units_[units[0]].domain;
  ps_t wake = domain_wake(e.domain);
  e.run_since = now_;
  for (int32_t u : units) {
    units_[u].exec = x;
    units_[u].last_job = e.job;
    unit_busy(u, true);
  }
  ps_t setup = 0;
  if (ctx_switch) {
    // Context restore cost: blocks.cpu_*.context_switch_ns (RT: 0, bare-metal assumption).
    setup = e.engine == E_CPU ? ns_to_ps(soc_.cpu[units_[units[0]].cluster].ctx_ns) : 0;
  }
  if (wake > 0) {
    e.cat[A_DVFS] += wake;  // power-gate wake-up
    setup += wake;
    e.cat[A_PREEMPTION] -= wake;  // the setup block below adds it again
  }
  if (e.level == 0 || e.engine == E_RT) {
    e.mem_done = now_ + setup + e.mem_time;
    double rem = 1.0 - exec_progress(e);
    ps_t dur = std::max<ps_t>(1, cycles_to_ps(e.seg_rem, unit_hz(units[0])));
    l0_register_fluid(e, e.act[K_DRAM_BYTES] * rem, e.act[K_CACHE_BYTES] * rem, std::max(dur, e.mem_time));
  }
  if (setup > 0) {
    e.in_setup = true;
    e.cat[A_PREEMPTION] += setup;
    e.gen++;
    q_.push(now_ + setup, CMP_EXEC, EV_SETUP_DONE, x, e.uid, e.gen);
  } else {
    exec_try_compute(x);
  }
}

void Sim::exec_finish(int32_t x) {
  Exec& e = execs_[x];
  e.next_compute = e.nsteps;
  e.computing = false;
  e.partial = false;
  exec_credit(e);
  if (e.act_credited < 1.0) {
    for (int k = 0; k < N_ACT; ++k)
      if (e.act[k] != 0) dom_[act_domain(k, e.domain)].act[k] += e.act[k] * (1.0 - e.act_credited);
    e.act_credited = 1.0;
  }
  l0_unregister_fluid(e);
  credit_job_busy(e);
  Job* j = job_ptr(e.job);
  int engine = e.engine;
  int32_t op = e.op;
  std::vector<int32_t> units = e.units;
  // Close the accounting so categories sum exactly to the op's wall time.
  ps_t total = now_ - e.start_ps;
  ps_t sum = 0;
  for (int k = 0; k < N_CAT; ++k) sum += e.cat[k];
  if (sum != total) {
    e.cat[e.exec_cat] += total - sum;
    if (e.cat[e.exec_cat] < 0) {
      ps_t deficit = -e.cat[e.exec_cat];
      e.cat[e.exec_cat] = 0;
      for (int k = 0; k < N_CAT && deficit > 0; ++k) {
        ps_t take = std::min(deficit, e.cat[k]);
        e.cat[k] -= take;
        deficit -= take;
      }
    }
  }
  if (j && engine == E_NPU) {
    FaultCfg& fb = faults_[F_NPU_BITFLIP];
    if (fb.rate > 0 && fb.rng.uniform() < fb.rate) note_fault(F_NPU_BITFLIP, e.job, e.task, op, now_, 1.0);
  }
  if (j) {
    JobOp& o = j->ops[op];
    o.cat = e.cat;
    TraceRec r;
    r.t = e.start_ps;
    r.dur = total;
    r.kind = TK_OP;
    r.block = static_cast<uint8_t>(units_[units[0]].block);
    r.unit = static_cast<uint16_t>(units_[units[0]].index);
    r.job = e.job;
    r.task = e.task;
    r.op = op;
    r.bytes = static_cast<int64_t>(e.rd_w.total + e.rd_in.total + e.wr.total);
    r.value = e.util;
    // Units used by the op as a 32-bit mask over unit indices, packed into
    // the (unused for op records) node fields sx, sy, dx, dy (little endian).
    uint32_t mask = 0;
    for (int32_t u : units)
      if (units_[u].index < 32) mask |= 1u << units_[u].index;
    r.sx = static_cast<int8_t>(mask & 0xff);
    r.sy = static_cast<int8_t>((mask >> 8) & 0xff);
    r.dx = static_cast<int8_t>((mask >> 16) & 0xff);
    r.dy = static_cast<int8_t>((mask >> 24) & 0xff);
    emit(r);
  }
  for (int32_t u : units) {
    unit_busy(u, false);
    units_[u].exec = -1;
    units_[u].last_job = e.job;
  }
  free_exec(x);
  if (j) {
    // NPU layer-level preemption: without a preemption boundary after this
    // op, the tiles stay reserved for this job's next layer.
    const PlanOp& po = plans_[j->task].ops[op];
    if (engine == E_NPU && po.def >= 0 && !tasks_[j->task].ops[po.def].preempt_boundary && j->remaining > 1)
      for (int32_t u : units) units_[u].reserved_job = j->id;
    int64_t jid = j->id;
    complete_op(*j, op, now_);
    if (engine == E_NPU) {
      bool has_item = false;
      for (const auto& it : eng_[E_NPU].ready)
        if (it.job == jid) has_item = true;
      if (!has_item)
        for (int32_t u : units)
          if (units_[u].reserved_job == jid) units_[u].reserved_job = -1;
    }
  }
  dispatch(engine);
}

}  // namespace gc
