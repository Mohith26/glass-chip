#include "glasschip/config.hpp"

#include <cmath>
#include <map>
#include <stdexcept>

namespace gc {

static const char* kDomain[] = {"cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io"};
static const char* kEngine[] = {"npu", "simt", "cpu", "rt", "dsp", "dma"};
static const char* kCat[] = {"compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs", "middleware"};
static const char* kAct[] = {"int8_macs", "bf16_macs", "fp8_macs", "sram_bytes", "flit_hops", "cache_bytes", "dram_bytes",
                             "dram_activates", "cpu_instr", "vec_ops", "simt_flops", "dsp_ops", "dma_bytes"};
static const char* kQos[] = {"safety", "realtime", "best_effort"};

const char* domain_name(int d) { return (d >= 0 && d < N_DOMAINS) ? kDomain[d] : "?"; }
const char* engine_name(int e) { return (e >= 0 && e < N_ENGINES) ? kEngine[e] : "?"; }
const char* cat_name(int c) { return (c >= 0 && c < N_CAT) ? kCat[c] : "?"; }
const char* act_name(int k) { return (k >= 0 && k < N_ACT) ? kAct[k] : "?"; }
const char* qos_name(int q) { return (q >= 0 && q < N_QOS) ? kQos[q] : "?"; }

int parse_domain(const std::string& s) {
  for (int i = 0; i < N_DOMAINS; ++i) if (s == kDomain[i]) return i;
  return -1;
}
int parse_engine(const std::string& s) {
  for (int i = 0; i < N_ENGINES; ++i) if (s == kEngine[i]) return i;
  if (s == "cpu_a" || s == "cpu_b") return E_CPU;
  return -1;
}
int parse_qos(const std::string& s) {
  for (int i = 0; i < N_QOS; ++i) if (s == kQos[i]) return i;
  return -1;
}
int parse_sched(const std::string& s) {
  if (s == "fifo") return SP_FIFO;
  if (s == "fixed_priority" || s == "rm" || s == "rate_monotonic") return SP_FIXED_PRIORITY;
  if (s == "edf") return SP_EDF;
  if (s == "edf_mc") return SP_EDF_MC;
  return -1;
}

static std::vector<XY> parse_nodes(const Json* b) {
  std::vector<XY> out;
  if (!b) return out;
  const Json* n = b->get("nodes");
  if (!n || !n->is_arr()) return out;
  for (const auto& p : n->a) {
    if (p.is_arr() && p.a.size() >= 2) out.push_back(XY{static_cast<int8_t>(p.a[0].as_num()), static_cast<int8_t>(p.a[1].as_num())});
  }
  return out;
}

SocCfg parse_soc(const Json& soc) {
  SocCfg c;
  c.name = soc.str("name", "soc");
  if (const Json* n = soc.get("noc")) {
    if (const Json* d = n->get("dims"); d && d->is_arr() && d->a.size() >= 2) {
      c.dimx = static_cast<int>(d->a[0].as_num(4));
      c.dimy = static_cast<int>(d->a[1].as_num(4));
    }
    c.noc_freq = n->num("freq_hz", c.noc_freq);
    c.link_bytes = static_cast<int>(n->num("link_bits", 256) / 8);
    c.vcs = static_cast<int>(n->num("virtual_channels", c.vcs));
    c.router_cycles = static_cast<int>(n->num("router_latency_cycles", c.router_cycles));
    c.link_cycles = static_cast<int>(n->num("link_latency_cycles", c.link_cycles));
    c.flit_bytes = static_cast<int>(n->num("flit_bytes", c.flit_bytes));
    c.max_packet_bytes = static_cast<int>(n->num("max_packet_bytes", c.max_packet_bytes));
  }
  const Json* blocks = soc.get("blocks");
  auto blk = [&](const char* k) -> const Json* { return blocks ? blocks->get(k) : nullptr; };
  for (int i = 0; i < 2; ++i) {
    const Json* b = blk(i == 0 ? "cpu_a" : "cpu_b");
    auto& p = c.cpu[i];
    if (b) {
      p.cores = static_cast<int>(b->num("cores", p.cores));
      p.freq = b->num("freq_hz", p.freq);
      p.ipc = b->num("ipc_scalar", p.ipc);
      p.vflops = b->num("vector_flops_per_cycle", p.vflops);
      p.ctx_ns = b->num("context_switch_ns", p.ctx_ns);
      p.l2_kb = static_cast<int>(b->num("l2_kb", p.l2_kb));
      p.nodes = parse_nodes(b);
    } else {
      p.cores = 0;
    }
    if (p.nodes.empty()) p.nodes.push_back(XY{static_cast<int8_t>(i), 0});
  }
  if (const Json* b = blk("rt")) {
    c.rt.cores = static_cast<int>(b->num("cores", c.rt.cores));
    c.rt.lockstep = b->flag("lockstep", c.rt.lockstep);
    c.rt.freq = b->num("freq_hz", c.rt.freq);
    c.rt.ipc = b->num("ipc_scalar", c.rt.ipc);
    c.rt.tcm_kb = static_cast<int>(b->num("tcm_kb", c.rt.tcm_kb));
    c.rt.nodes = parse_nodes(b);
  }
  if (c.rt.nodes.empty()) c.rt.nodes.push_back(XY{2, 0});
  if (const Json* b = blk("dsp")) {
    c.dsp.units = static_cast<int>(b->num("units", c.dsp.units));
    c.dsp.freq = b->num("freq_hz", c.dsp.freq);
    c.dsp.ops_int8 = b->num("ops_per_cycle_int8", c.dsp.ops_int8);
    c.dsp.simd_bits = static_cast<int>(b->num("simd_bits", c.dsp.simd_bits));
    c.dsp.nodes = parse_nodes(b);
  }
  if (c.dsp.nodes.empty()) c.dsp.nodes.push_back(XY{3, 0});
  if (const Json* b = blk("npu")) {
    c.npu.tiles = static_cast<int>(b->num("tiles", c.npu.tiles));
    if (const Json* a = b->get("array"); a && a->is_arr() && a->a.size() >= 2) {
      c.npu.rows = static_cast<int>(a->a[0].as_num(64));
      c.npu.cols = static_cast<int>(a->a[1].as_num(64));
    }
    c.npu.weight_stationary = b->str("dataflow", "weight_stationary") != "output_stationary";
    c.npu.freq = b->num("freq_hz", c.npu.freq);
    c.npu.macs_tile = b->num("int8_macs_per_cycle_per_tile", c.npu.rows * c.npu.cols);
    c.npu.bf16_frac = b->num("bf16_rate_fraction", c.npu.bf16_frac);
    c.npu.fp8_frac = b->num("fp8_rate_fraction", c.npu.fp8_frac);
    c.npu.sram_tile_kb = b->num("sram_per_tile_kb", c.npu.sram_tile_kb);
    c.npu.shared_kb = b->num("shared_sram_kb", c.npu.shared_kb);
    c.npu.preempt_us = b->num("preempt_save_restore_us", c.npu.preempt_us);
    c.npu.nodes = parse_nodes(b);
  }
  if (c.npu.nodes.empty()) c.npu.nodes.push_back(XY{0, 1});
  if (const Json* b = blk("simt")) {
    c.simt.cus = static_cast<int>(b->num("compute_units", c.simt.cus));
    c.simt.lanes = static_cast<int>(b->num("lanes_per_cu", c.simt.lanes));
    c.simt.freq = b->num("freq_hz", c.simt.freq);
    c.simt.smem_kb = b->num("shared_mem_kb_per_cu", c.simt.smem_kb);
    c.simt.launch_us = b->num("launch_overhead_us", c.simt.launch_us);
    c.simt.coalesce = b->num("coalescing_efficiency", c.simt.coalesce);
    c.simt.nodes = parse_nodes(b);
  }
  if (c.simt.nodes.empty()) c.simt.nodes.push_back(XY{3, 2});
  if (const Json* b = blk("sys_cache")) {
    c.cache.size_kb = static_cast<int>(b->num("size_kb", c.cache.size_kb));
    c.cache.ways = static_cast<int>(b->num("ways", c.cache.ways));
    c.cache.line = static_cast<int>(b->num("line_bytes", c.cache.line));
    c.cache.hit_cycles = static_cast<int>(b->num("hit_latency_cycles", c.cache.hit_cycles));
    c.cache.partitionable = b->flag("partitionable", true);
    c.cache.nodes = parse_nodes(b);
  }
  if (c.cache.nodes.empty()) c.cache.nodes.push_back(XY{2, 1});
  if (const Json* b = blk("dram")) {
    c.dram.mts = b->num("data_rate_mts", c.dram.mts);
    c.dram.bus_bits = static_cast<int>(b->num("bus_bits_total", c.dram.bus_bits));
    c.dram.ctrls = static_cast<int>(b->num("controllers", c.dram.ctrls));
    c.dram.ch_per_ctrl = static_cast<int>(b->num("channels_per_controller", c.dram.ch_per_ctrl));
    c.dram.bgs = static_cast<int>(b->num("bank_groups", c.dram.bgs));
    c.dram.banks_per_bg = static_cast<int>(b->num("banks_per_group", c.dram.banks_per_bg));
    c.dram.row_bytes = static_cast<int>(b->num("row_bytes", c.dram.row_bytes));
    if (const Json* t = b->get("timing_ns")) {
      c.dram.tRCD = t->num("tRCD", c.dram.tRCD);
      c.dram.tRP = t->num("tRP", c.dram.tRP);
      c.dram.CL = t->num("CL", c.dram.CL);
      c.dram.tRAS = t->num("tRAS", c.dram.tRAS);
      c.dram.tRFC = t->num("tRFC", c.dram.tRFC);
      c.dram.tREFI = t->num("tREFI", c.dram.tREFI);
      c.dram.tWTR = t->num("tWTR", c.dram.tWTR);
      c.dram.tRTW = t->num("tRTW", c.dram.tRTW);
    }
    c.dram.nodes = parse_nodes(b);
  }
  if (c.dram.nodes.empty()) c.dram.nodes.push_back(XY{0, 3});
  if (const Json* b = blk("dma")) {
    c.dma.engines = static_cast<int>(b->num("engines", c.dma.engines));
    c.dma.desc_ns = b->num("descriptor_latency_ns", c.dma.desc_ns);
    c.dma.nodes = parse_nodes(b);
  }
  if (c.dma.nodes.empty()) c.dma.nodes.push_back(XY{1, 3});
  if (const Json* pd = soc.get("power_domains")) c.dvfs_transition_us = pd->num("dvfs_transition_us", c.dvfs_transition_us);
  c.nominal_hz[D_CPU_A] = c.cpu[0].freq;
  c.nominal_hz[D_CPU_B] = c.cpu[1].freq;
  c.nominal_hz[D_RT] = c.rt.freq;
  c.nominal_hz[D_NPU] = c.npu.freq;
  c.nominal_hz[D_SIMT] = c.simt.freq;
  c.nominal_hz[D_DSP] = c.dsp.freq;
  c.nominal_hz[D_NOC_CACHE] = c.noc_freq;
  c.nominal_hz[D_DRAM_IO] = c.dram.mts * 1e6 / 2.0;  // DRAM clock = data rate / 2
  return c;
}

static int parse_level(const Json* j, int def) {
  if (!j) return def;
  if (j->is_num()) return static_cast<int>(j->as_num(def));
  if (j->is_str() && j->s.size() >= 2 && (j->s[0] == 'L' || j->s[0] == 'l')) return std::atoi(j->s.c_str() + 1);
  return def;
}

void apply_params(ModelParams& p, const Json& params, std::vector<std::string>* warnings) {
  if (!params.is_obj()) return;
  for (const auto& kv : params.o) {
    const std::string& k = kv.first;
    const Json& v = kv.second;
    double x = v.as_num(0);
#define NUM(key, field) if (k == key) { p.field = static_cast<decltype(p.field)>(x); continue; }
    NUM("bulk_chunk_bytes", bulk_chunk_bytes)
    NUM("cpu_miss_bytes", cpu_miss_bytes)
    NUM("noc_req_bytes", noc_req_bytes)
    NUM("noc_model_requests", noc_model_requests)
    NUM("noc_l1_arbitration", noc_l1_arbitration)
    NUM("noc_backpressure_hold", noc_backpressure_hold)
    NUM("noc_calendar_horizon_ns", noc_calendar_horizon_ns)
    NUM("noc_ni_cycles", noc_ni_cycles)
    NUM("noc_contention_bubble", noc_contention_bubble)
    NUM("noc_vc_flow_control", noc_vc_flow_control)
    NUM("noc_rr_tail", noc_rr_tail)
    NUM("npu_l0_util", npu_l0_util)
    NUM("npu_vec_ops_per_cycle", npu_vec_ops)
    NUM("npu_buffer_depth", npu_depth)
    NUM("npu_weight_load_overlap", npu_wload_overlap)
    NUM("npu_min_macs_per_tile", npu_min_macs_per_tile)
    NUM("npu_max_tiles_per_op", npu_max_tiles_per_op)
    NUM("npu_fp32_rate_fraction", npu_fp32_frac)
    NUM("simt_l0_eff", simt_l0_eff)
    NUM("simt_warps_needed_per_cu", simt_warps_needed)
    NUM("simt_warp_size", simt_warp)
    NUM("cpu_vector_ipc", cpu_vec_ipc)
    NUM("cpu_mem_ipc", cpu_mem_ipc)
    NUM("cpu_mlp", cpu_mlp)
    NUM("cpu_l0_mem_bw_gbs", cpu_l0_mem_bw_gbs)
    NUM("dsp_line_buffer_kb", dsp_line_buffer_kb)
    NUM("dsp_pipeline_fill_cycles", dsp_fill_cycles)
    NUM("dsp_l1_simd_eff", dsp_l1_eff)
    NUM("dma_bytes_per_cycle", dma_bytes_per_cycle)
    NUM("noc_vc_buffer_flits", noc_vc_buffer_flits)
    NUM("noc_util_window_ns", noc_window_ns)
    NUM("cache_block_bytes", cache_block_bytes)
    NUM("stream_bypass_bytes", stream_bypass_bytes)
    NUM("dram_queue_depth", dram_queue_depth)
    NUM("dram_l0_eff", dram_l0_eff)
    NUM("dram_ctrl_ns", dram_ctrl_ns)
    NUM("dram_interleave_bytes", dram_interleave_bytes)
    NUM("dram_util_window_ns", dram_window_ns)
    NUM("irq_latency_ns", irq_ns)
    NUM("rt_irq_latency_ns", rt_irq_ns)
    NUM("exec_cv", exec_cv)
    NUM("middleware_on_rt", middleware_on_rt)
    NUM("rt_copy_bytes_per_ns", rt_copy_bytes_per_ns)
    NUM("fuse_attention", fuse_attention)
    NUM("fuse_elementwise", fuse_elementwise)
    NUM("flash_block", flash_block)
#undef NUM
    if (k == "dram_backend_plugin" || k == "ramulator_config" || k == "noc_backend_plugin" || k == "booksim_config") continue;
    if (k == "mapping") {
      std::string s = v.is_str() ? v.s : "static";
      p.mapping = (s == "eft" || s == "greedy_eft" || s == "earliest_finish_time") ? MAP_EFT : MAP_STATIC;
      continue;
    }
    if (warnings) warnings->push_back("unknown model parameter: " + k);
  }
  if (p.bulk_chunk_bytes < 64) p.bulk_chunk_bytes = 64;
  if (p.cpu_miss_bytes < 16) p.cpu_miss_bytes = 16;
  if (p.npu_depth < 1) p.npu_depth = 1;
  if (p.npu_depth > 4) p.npu_depth = 4;
  if (p.cache_block_bytes < 64) p.cache_block_bytes = 64;
  if (p.dram_queue_depth < 1) p.dram_queue_depth = 1;
  if (p.cpu_mlp < 1) p.cpu_mlp = 1;
}

FidelityCfg parse_fidelity(const Json& f) {
  FidelityCfg c;
  c.name = f.str("name", "custom");
  c.cpu = parse_level(f.get("cpu"), c.cpu);
  c.npu = parse_level(f.get("npu"), c.npu);
  c.simt = parse_level(f.get("simt"), c.simt);
  c.dsp = parse_level(f.get("dsp"), c.dsp);
  c.noc = parse_level(f.get("noc"), c.noc);
  c.dram = parse_level(f.get("dram"), c.dram);
  c.cache = parse_level(f.get("cache"), c.cache);
  c.quantum_ns = f.num("quantum_ns", c.quantum_ns);
  // Levels that have no implementation fall back with a warning flag.
  auto clamp = [&](int& lvl, int max_lvl, const char* comp, const char* why) {
    if (lvl > max_lvl) {
      c.warnings.push_back(std::string(comp) + ": L" + std::to_string(lvl) + " unavailable (" + why + "), using L" +
                           std::to_string(max_lvl));
      lvl = max_lvl;
    }
    if (lvl < 0) lvl = 0;
  };
  clamp(c.simt, 1, "simt", "no cycle-approximate SIMT model");
  clamp(c.dsp, 1, "dsp", "no cycle-approximate DSP model");
  clamp(c.cache, 1, "cache", "no cycle-approximate cache model");
  clamp(c.noc, 2, "noc", "no L3 NoC model");
  clamp(c.dram, 2, "dram", "no L3 DRAM model");
  clamp(c.cpu, 2, "cpu", "L3 is offline gem5 only");
  clamp(c.npu, 2, "npu", "L3 not defined");
  auto strings = [&](const Json* p) {
    if (!p || !p->is_obj()) return;
    c.dram_plugin = p->str("dram_backend_plugin", c.dram_plugin);
    c.ramulator_config = p->str("ramulator_config", c.ramulator_config);
    c.noc_plugin = p->str("noc_backend_plugin", c.noc_plugin);
    c.booksim_config = p->str("booksim_config", c.booksim_config);
  };
  strings(f.get("params"));
  if (const Json* p = f.get("params")) apply_params(c.p, *p, &c.warnings);
  if (const Json* r = f.get("runtime")) apply_params(c.p, *r, &c.warnings);
  return c;
}

// ----------------------------------------------------------------------------
static int parse_optype(const std::string& s) {
  static const std::map<std::string, int> m = {
      {"conv2d", OT_CONV2D}, {"dwconv2d", OT_DWCONV2D}, {"matmul", OT_MATMUL}, {"attention", OT_ATTENTION},
      {"elementwise", OT_ELEMENTWISE}, {"softmax", OT_SOFTMAX}, {"norm", OT_NORM}, {"pool", OT_POOL},
      {"resize", OT_RESIZE}, {"reduce", OT_REDUCE}, {"memcopy", OT_MEMCOPY}, {"cpu_kernel", OT_CPU_KERNEL},
      {"simt_kernel", OT_SIMT_KERNEL}, {"dsp_kernel", OT_DSP_KERNEL}, {"control_law", OT_CONTROL_LAW},
      {"serialize", OT_SERIALIZE}, {"other", OT_OTHER}};
  auto it = m.find(s);
  return it == m.end() ? OT_OTHER : it->second;
}

static int parse_dtype(const std::string& s) {
  if (s == "int8") return DT_INT8;
  if (s == "fp8") return DT_FP8;
  if (s == "bf16") return DT_BF16;
  if (s == "fp16") return DT_FP16;
  if (s == "fp32") return DT_FP32;
  if (s == "int32") return DT_INT32;
  if (s == "mixed") return DT_MIXED;
  return DT_INT8;
}

static double shp(const Json* s, std::initializer_list<const char*> keys, double def) {
  if (!s) return def;
  for (const char* k : keys)
    if (const Json* v = s->get(k); v && v->is_num()) return v->as_num(def);
  return def;
}

TaskDef parse_task(const Json& g) {
  TaskDef t;
  t.id = g.str("task_id", "");
  if (t.id.empty()) throw std::runtime_error("task graph missing task_id");
  const Json* rel = g.get("release");
  std::string kind = rel ? rel->str("kind", "periodic") : "periodic";
  t.release_kind = kind == "periodic" ? 0 : (kind == "sporadic" ? 1 : 2);
  double period_us = rel ? rel->num("period_us", 0) : 0;
  t.period_ps = us_to_ps(period_us);
  t.deadline_ps = us_to_ps(g.num("deadline_us", period_us > 0 ? period_us : 1e6));
  if (t.period_ps <= 0) t.period_ps = t.deadline_ps;  // rate-monotonic key for sporadic/event tasks
  t.skip_on_overrun = rel ? rel->str("on_overrun", "queue") == "skip" : false;
  std::string crit = g.str("criticality", "medium");
  t.crit = crit == "safety" ? C_SAFETY : crit == "high" ? C_HIGH : crit == "low" ? C_LOW : C_MEDIUM;
  int q = parse_qos(g.str("qos_class", "realtime"));
  t.qos = q < 0 ? Q_REALTIME : q;
  if (const Json* af = g.get("affinity"); af && af->is_arr()) {
    for (const auto& a : af->a) {
      if (!a.is_str()) continue;
      int e = parse_engine(a.s);
      if (e >= 0) t.affinity_engines |= 1u << e;
      if (a.s == "cpu_a") t.cpu_cluster = 0;
      if (a.s == "cpu_b") t.cpu_cluster = 1;
    }
  }
  if (const Json* fp = g.get("footprint")) {
    t.weights_bytes = fp->num("weights_bytes", 0);
    t.ws_bytes = fp->num("working_set_bytes", 0);
  }
  const Json* ops = g.get("ops");
  if (!ops || !ops->is_arr() || ops->a.empty()) throw std::runtime_error("task " + t.id + " has no ops");
  std::map<std::string, int> idx;
  for (size_t i = 0; i < ops->a.size(); ++i) {
    const Json& o = ops->a[i];
    OpDef d;
    d.id = o.str("op_id", "op" + std::to_string(i));
    d.layer = o.str("layer", "");
    d.type = parse_optype(o.str("type", "other"));
    d.dtype = parse_dtype(o.str("dtype", "int8"));
    d.macs = o.num("macs", 0);
    d.flops = o.num("flops", 0);
    d.bytes_in = o.num("bytes_in", 0);
    d.bytes_w = o.num("bytes_w", 0);
    d.bytes_out = o.num("bytes_out", 0);
    d.reuse = std::clamp(o.num("reuse", 0), 0.0, 1.0);
    if (const Json* ins = o.get("instr")) {
      d.i_scalar = ins->num("scalar", 0);
      d.i_vector = ins->num("vector", 0);
      d.i_mem = ins->num("mem", 0);
    }
    if (const Json* el = o.get("eligible"); el && el->is_arr())
      for (const auto& e : el->a)
        if (e.is_str()) { int k = parse_engine(e.s); if (k >= 0 && !(d.eligible & (1u << k))) { d.eligible |= 1u << k; d.elig_order.push_back(k); } }
    if (!d.eligible) { d.eligible = 1u << E_CPU; d.elig_order.push_back(E_CPU); }
    d.preempt_boundary = o.flag("preempt_boundary", true);
    d.l2_cycles = o.num("l2_cycles", -1);
    const Json* s = o.get("shape");
    if (d.type == OT_CONV2D || d.type == OT_DWCONV2D) {
      double N = shp(s, {"N"}, 1), C = shp(s, {"C"}, 0), H = shp(s, {"H"}, 0), W = shp(s, {"W"}, 0);
      double K = shp(s, {"K"}, 0), R = shp(s, {"R"}, 1), S = shp(s, {"S"}, 1), st = shp(s, {"stride"}, 1);
      double Ho = shp(s, {"Ho", "P", "OH"}, st > 0 ? std::ceil(H / st) : H);
      double Wo = shp(s, {"Wo", "Q", "OW"}, st > 0 ? std::ceil(W / st) : W);
      double g2 = shp(s, {"groups"}, d.type == OT_DWCONV2D ? C : 1);
      if (g2 < 1) g2 = 1;
      if (C > 0 && K > 0 && Ho > 0 && Wo > 0) {
        d.has_gemm = true;
        d.groups = g2;
        if (g2 > 1 && g2 >= C) d.type = OT_DWCONV2D;
        d.gM = N * Ho * Wo;
        d.gK = (C / g2) * R * S;
        d.gN = K / g2;
        d.gB = g2 * shp(s, {"layers"}, 1) * shp(s, {"repeat"}, 1);
      }
    } else if (d.type == OT_MATMUL || d.type == OT_ATTENTION) {
      double M = shp(s, {"M"}, 0), K = shp(s, {"K"}, 0), N = shp(s, {"N"}, 0);
      double B = shp(s, {"B", "batch"}, 1) * shp(s, {"heads"}, 1) * shp(s, {"layers"}, 1) * shp(s, {"repeat"}, 1);
      if (M > 0 && K > 0 && N > 0) { d.has_gemm = true; d.gM = M; d.gK = K; d.gN = N; d.gB = B; }
    }
    d.threads = shp(s, {"threads"}, 0);
    if (d.threads <= 0 && s) {
      double gr = shp(s, {"grid"}, 0), bl = shp(s, {"block"}, 0);
      if (gr > 0 && bl > 0) d.threads = gr * bl;
    }
    d.rows2d = shp(s, {"rows"}, 0);
    d.row_bytes2d = shp(s, {"row_bytes"}, 0);
    if (d.rows2d > 0) d.stride2d = shp(s, {"stride"}, d.row_bytes2d);
    d.descriptors = std::max(1.0, shp(s, {"descriptors"}, 1));
    // Fill MACs from the GEMM view if the graph left them at zero.
    if (d.macs <= 0 && d.has_gemm) d.macs = d.gB * d.gM * d.gK * d.gN;
    // When declared MACs and the shape disagree, the declared count is the
    // amount of work (it comes from the full graph walk, e.g. decoder steps
    // with attention over a KV cache that the summary shape omits); the shape
    // only sets array utilization, so shape-based cycles are scaled.
    if (d.has_gemm && d.macs > 0) {
      double sm = d.gB * d.gM * d.gK * d.gN;
      if (sm > 0) d.work_scale = d.macs / sm;
    }
    idx[d.id] = static_cast<int>(i);
    t.ops.push_back(std::move(d));
  }
  for (size_t i = 0; i < ops->a.size(); ++i) {
    const Json* deps = ops->a[i].get("depends_on");
    if (!deps) {
      if (i > 0) t.ops[i].deps.push_back(static_cast<int>(i) - 1);
    } else if (deps->is_arr()) {
      for (const auto& d : deps->a) {
        if (!d.is_str()) continue;
        auto it = idx.find(d.s);
        if (it == idx.end()) throw std::runtime_error("task " + t.id + ": op " + t.ops[i].id + " depends on unknown " + d.s);
        if (it->second >= static_cast<int>(i)) throw std::runtime_error("task " + t.id + ": dependency must precede op " + t.ops[i].id);
        t.ops[i].deps.push_back(it->second);
      }
    }
  }
  if (const Json* ms = g.get("messages"); ms && ms->is_arr()) {
    for (const auto& m : ms->a) {
      MsgDef md;
      md.topic = m.str("topic", "");
      md.bytes = m.num("bytes", 0);
      md.consumer = m.str("consumer", "");
      t.msgs.push_back(md);
    }
  }
  return t;
}

}  // namespace gc
