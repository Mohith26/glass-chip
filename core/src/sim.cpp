// Sim construction, the event loop, DVFS, counters, queue depths, tracing,
// checkpoint/restore and info().
#include "glasschip/sim.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <filesystem>
#include <dlfcn.h>
#include <fstream>
#include <sstream>

namespace gc {

static const char* kTraceKind[] = {"job_release", "job_start", "job_finish", "job_miss", "op", "xfer", "dram_q", "freq", "preempt", "fault"};
static const char* kTraceBlock[] = {"cpu_a", "cpu_b", "rt", "dsp", "npu", "simt", "sys_cache", "dram", "dma", "noc", "noc_cache", "dram_io", "runtime"};
const char* trace_kind_name(int k) { return (k >= 0 && k < N_TK) ? kTraceKind[k] : "?"; }
const char* trace_block_name(int b) { return (b >= 0 && b < N_TB) ? kTraceBlock[b] : "?"; }

static int domain_block(int d) {
  switch (d) {
    case D_CPU_A: return TB_CPU_A;
    case D_CPU_B: return TB_CPU_B;
    case D_RT: return TB_RT;
    case D_NPU: return TB_NPU;
    case D_SIMT: return TB_SIMT;
    case D_DSP: return TB_DSP;
    case D_NOC_CACHE: return TB_NOC_CACHE;
    default: return TB_DRAM_IO;
  }
}

Sim::Sim(const Json& soc, const Json& fidelity, uint64_t seed) {
  soc_ = parse_soc(soc);
  fid_ = parse_fidelity(fidelity);
  seed_ = seed;
  static const char* streams[] = {"runtime", "dram", "engine.npu", "engine.simt", "engine.cpu", "engine.rt", "engine.dsp", "engine.dma"};
  rng_.resize(2 + N_ENGINES);
  for (size_t i = 0; i < rng_.size(); ++i) rng_[i].seed(seed, streams[i]);
  for (int e = 0; e < N_ENGINES; ++e) eng_[e].kind = e;
  auto add_unit = [&](int engine, int domain, XY node, int index, int cluster, int block) {
    Unit u;
    u.engine = engine;
    u.domain = domain;
    u.node = node;
    u.index = index;
    u.cluster = cluster;
    u.block = block;
    eng_[engine].units.push_back(static_cast<int32_t>(units_.size()));
    units_.push_back(u);
    dom_[domain].units++;
  };
  for (int i = 0; i < soc_.npu.tiles; ++i) add_unit(E_NPU, D_NPU, soc_.npu.nodes[i % soc_.npu.nodes.size()], i, 0, TB_NPU);
  for (int i = 0; i < soc_.simt.cus; ++i) add_unit(E_SIMT, D_SIMT, soc_.simt.nodes[i % soc_.simt.nodes.size()], i, 0, TB_SIMT);
  for (int c = 0; c < 2; ++c)
    for (int i = 0; i < soc_.cpu[c].cores; ++i)
      add_unit(E_CPU, c == 0 ? D_CPU_A : D_CPU_B, soc_.cpu[c].nodes[0], i, c, c == 0 ? TB_CPU_A : TB_CPU_B);
  int rt_units = soc_.rt.lockstep ? std::max(1, soc_.rt.cores / 2) : soc_.rt.cores;  // blocks.rt.lockstep pairs cores
  for (int i = 0; i < rt_units; ++i) add_unit(E_RT, D_RT, soc_.rt.nodes[0], i, 0, TB_RT);
  for (int i = 0; i < soc_.dsp.units; ++i) add_unit(E_DSP, D_DSP, soc_.dsp.nodes[i % soc_.dsp.nodes.size()], i, 0, TB_DSP);
  for (int i = 0; i < soc_.dma.engines; ++i) add_unit(E_DMA, D_NOC_CACHE, soc_.dma.nodes[0], i, 0, TB_DMA);
  if (units_.size() > 0) {
    for (int e = 0; e < N_ENGINES; ++e)
      if (eng_[e].units.size() > 64) throw std::runtime_error("at most 64 units per engine are supported");
  }
  eng_[E_NPU].level = fid_.npu;
  eng_[E_SIMT].level = fid_.simt;
  eng_[E_CPU].level = fid_.cpu;
  eng_[E_RT].level = 0;
  eng_[E_DSP].level = fid_.dsp;
  eng_[E_DMA].level = std::max(fid_.noc, fid_.dram) >= 1 ? 1 : 0;
  eng_[E_CPU].preemptive = true;
  eng_[E_RT].preemptive = true;
  for (int d = 0; d < N_DOMAINS; ++d) {
    dom_[d].hz = soc_.nominal_hz[d];
    dom_[d].nominal = soc_.nominal_hz[d];
  }
  noc_.init(soc_, fid_.p, fid_.noc);
  cache_.init(soc_, fid_.p, fid_.cache);
  dram_.init(soc_, fid_.p, fid_.dram, rng_[1]);
  last_dram_q_.assign(static_cast<size_t>(dram_.n_channels()), 0);
  if (fid_.noc >= 2) {
    if (soc_.dimx != soc_.dimy) noc_l2_note_ = "BookSim mesh mapping assumes a square mesh";
    else if (!fid_.noc_plugin.empty()) {
      std::string err = attach_noc_backend(fid_.noc_plugin, fid_.booksim_config);
      if (!err.empty()) noc_l2_note_ = err;
    } else {
      noc_l2_note_ = "no plugin path given";
    }
  }
  if (fid_.dram >= 2 && !fid_.dram_plugin.empty()) {
    std::string err = attach_dram_backend(fid_.dram_plugin, fid_.ramulator_config);
    if (!err.empty()) dram_attach_error_ = err;
  }
}

std::string Sim::attach_dram_backend(const std::string& plugin, const std::string& config) {
  if (dram_backend_) return "";
  void* h = dlopen(plugin.c_str(), RTLD_NOW | RTLD_LOCAL);
  if (!h) {
    const char* e = dlerror();
    return std::string("dlopen failed: ") + (e ? e : "?");
  }
  auto fn = reinterpret_cast<DramBackendFactory>(dlsym(h, "gc_make_dram_backend"));
  if (!fn) {
    dlclose(h);
    return "plugin has no gc_make_dram_backend";
  }
  char err[512] = {0};
  DramBackend* b = fn(config.c_str(), err, sizeof err);
  if (!b) {
    dlclose(h);
    return std::string("backend construction failed: ") + err;
  }
  dram_dl_ = h;
  dram_backend_ = b;
  return "";
}

std::string Sim::attach_noc_backend(const std::string& plugin, const std::string& extra) {
  if (noc_backend_) return "";
  void* h = dlopen(plugin.c_str(), RTLD_NOW | RTLD_LOCAL);
  if (!h) {
    const char* e = dlerror();
    return std::string("dlopen failed: ") + (e ? e : "?");
  }
  auto fn = reinterpret_cast<NocBackendFactory>(dlsym(h, "gc_make_noc_backend"));
  if (!fn) return "plugin has no gc_make_noc_backend";
  // BookSim configuration derived from the SoC noc: section (same mapping as
  // calib/booksim/booksim_cfg.py: 2-cycle routers + 1-cycle links with
  // speculative single-cycle VC/switch allocation, dim-order = XY routing).
  std::ostringstream c;
  c << "freq_hz=" << soc_.noc_freq << ";kx=" << soc_.dimx << ";flit_bytes=" << soc_.flit_bytes << ";max_packet_bytes=" << soc_.max_packet_bytes
    << "|topology = mesh; k = " << soc_.dimx << "; n = 2; routing_function = dim_order; num_vcs = " << soc_.vcs
    << "; vc_buf_size = 8; wait_for_tail_credit = 0; vc_allocator = separable_input_first; sw_allocator = separable_input_first;"
    << " alloc_iters = 1; credit_delay = 1; routing_delay = 0; vc_alloc_delay = 1; sw_alloc_delay = 1; speculative = 1;"
    << " st_final_delay = 1; input_speedup = 1; output_speedup = 1; internal_speedup = 1.0; seed = " << (seed_ % 100000) << ";" << extra;
  char err[512] = {0};
  NocBackend* b = fn(c.str().c_str(), err, sizeof err);
  if (!b) return std::string("backend construction failed: ") + err;
  noc_dl_ = h;
  noc_backend_ = b;
  return "";
}

Sim::~Sim() {
  delete noc_backend_;
  noc_backend_ = nullptr;
  delete dram_backend_;
  dram_backend_ = nullptr;  // the plugin handle stays loaded (Ramulator keeps static registries)
  trace_close();
  dram_trace_close();
  noc_trace_close();
}

bool Sim::dram_trace_open(const std::string& path) {
  dram_trace_close();
  dram_csv_ = CsvTrace{};
  dram_csv_.f = std::fopen(path.c_str(), "w");
  if (!dram_csv_.f) return false;
  std::fprintf(dram_csv_.f, "time_ns,addr,rw,bytes,latency_ns\n");
  return true;
}
void Sim::dram_trace_close() {
  if (!dram_csv_.f) return;
  csv_flush(dram_csv_, PS_NEVER, true);
  std::fclose(dram_csv_.f);
  dram_csv_.f = nullptr;
}
bool Sim::noc_trace_open(const std::string& path) {
  noc_trace_close();
  noc_csv_ = CsvTrace{};
  noc_csv_.f = std::fopen(path.c_str(), "w");
  if (!noc_csv_.f) return false;
  std::fprintf(noc_csv_.f, "time_ns,src_x,src_y,dst_x,dst_y,bytes,latency_ns\n");
  return true;
}
void Sim::noc_trace_close() {
  if (!noc_csv_.f) return;
  csv_flush(noc_csv_, PS_NEVER, false);
  std::fclose(noc_csv_.f);
  noc_csv_.f = nullptr;
}

void Sim::set_params(const Json& params) {
  apply_params(fid_.p, params, &fid_.warnings);
  if (events_ == 0 && job_slot_.empty()) {
    noc_.init(soc_, fid_.p, fid_.noc);
    cache_.init(soc_, fid_.p, fid_.cache);
    rng_[1].seed(seed_, "dram");
    dram_.init(soc_, fid_.p, fid_.dram, rng_[1]);
  }
  rebuild_plans();
}

void Sim::set_l2_tables(const Json& tables) {
  if (const Json* n = tables.get("npu_cycles"); n && n->is_obj())
    for (const auto& kv : n->o) npu_l2_[kv.first] = kv.second.as_num(-1);
  if (const Json* c = tables.get("cpu_cpi"); c && c->is_obj())
    for (const auto& kv : c->o) cpu_cpi_[kv.first] = kv.second.as_num(1);
}

void Sim::set_fault(const std::string& kind, double rate, uint64_t seed, ps_t param_ps) {
  int k = kind == "npu_bitflip" ? F_NPU_BITFLIP : kind == "noc_delay" ? F_NOC_DELAY : kind == "noc_drop" ? F_NOC_DROP : -1;
  if (k < 0) throw std::runtime_error("unknown fault kind: " + kind + " (npu_bitflip | noc_delay | noc_drop)");
  if (rate < 0 || rate > 1) throw std::runtime_error("fault rate must be a probability in [0, 1]");
  FaultCfg& f = faults_[k];
  f.rate = rate;
  f.param = param_ps >= 0 ? param_ps : (k == F_NOC_DROP ? 200 * PS_PER_NS : 50 * PS_PER_NS);
  f.rng.seed(seed, std::string("fault.") + kind);
}

void Sim::note_fault(int kind, int64_t job, int task, int op, ps_t t, double value) {
  faults_[kind].count++;
  if (Job* j = job_ptr(job)) {
    j->faults++;
    if (kind == F_NPU_BITFLIP) j->corrupted = true;
  }
  TraceRec r;
  r.t = t;
  r.kind = TK_FAULT;
  r.block = kind == F_NPU_BITFLIP ? TB_NPU : TB_NOC;
  r.unit = static_cast<uint16_t>(kind);
  r.job = job;
  r.task = task;
  r.op = op;
  r.value = value;
  emit(r);
}

int Sim::load_l2_tables(const std::string& dir) {
  namespace fs = std::filesystem;
  int n = 0;
  if (!fs::is_directory(dir)) return 0;
  std::vector<fs::path> files;
  for (const auto& e : fs::directory_iterator(dir)) files.push_back(e.path());
  std::sort(files.begin(), files.end());
  const char* df = soc_.npu.weight_stationary ? "ws" : "os";
  for (const auto& path : files) {
    std::string name = path.filename().string();
    bool npu = name.rfind("npu_scalesim_", 0) == 0 && path.extension() == ".json";
    bool cpu = name == "cpu_gem5.json";
    if (!npu && !cpu) continue;
    std::ifstream f(path);
    std::stringstream ss;
    ss << f.rdbuf();
    Json j = parse_json(ss.str());
    if (npu) {
      std::string task = j.str("task_id", "");
      const Json* d = j.get("dataflows");
      const Json* ops = (d && d->get(df)) ? d->get(df)->get("ops") : nullptr;
      if (task.empty() || !ops || !ops->is_obj()) continue;
      int k = 0;
      for (const auto& kv : ops->o) {
        // vx1_cycles: compute-only cycles on one tile at the VX-1 clock with the
        // dtype rate applied; DRAM/NoC stalls come from the simulator itself.
        double cyc = kv.second.num("vx1_cycles", -1);
        if (cyc >= 0) { npu_l2_[task + "/" + kv.first] = cyc; ++k; }
      }
      n += k;
      l2_sources_.push_back(name + " (" + df + ", " + std::to_string(k) + " ops)");
    } else {
      const Json* ks = j.get("kernels");
      if (!ks || !ks->is_obj()) continue;
      int k = 0;
      for (const auto& kv : ks->o) {
        double o3 = kv.second.get("o3") ? kv.second.get("o3")->num("cpi", -1) : -1;
        double mi = kv.second.get("minor") ? kv.second.get("minor")->num("cpi", -1) : -1;
        if (o3 > 0 && mi > 0) { cpu_kernel_cpi_[kv.first] = {o3, mi}; ++k; }
      }
      n += k;
      l2_sources_.push_back(name + " (" + std::to_string(k) + " kernels)");
    }
  }
  return n;
}

std::string Sim::cpu_kernel_for(const TaskDef& td, const OpDef& d) const {
  if (cpu_kernel_cpi_.empty()) return "";
  auto has = [&](const char* k) { return cpu_kernel_cpi_.count(k) > 0; };
  // The real kernels measured in gem5, by task.
  if ((td.id.rfind("T7_", 0) == 0 || td.id.rfind("T12_", 0) == 0) && has("T7_control_step")) return "T7_control_step";
  if (td.id.rfind("T6_", 0) == 0 && has("T6_estimate_step")) return "T6_estimate_step";
  if (td.id.rfind("T8_", 0) == 0 && (d.type == OT_CPU_KERNEL || d.type == OT_CONTROL_LAW) && has("T8_ssm_step")) return "T8_ssm_step";
  // Otherwise a kernel class by instruction mix (assumption): vector vs scalar,
  // streaming (memory-instruction heavy, axpy) vs compute (gemm).
  double instr = d.i_scalar + d.i_vector + d.i_mem;
  if (instr <= 0) return "";
  bool vec = d.i_vector > 0.1 * instr;
  bool stream = d.i_mem > 0.3 * instr;
  const char* k = vec ? (stream ? "axpy_rvv_4096" : "gemm_rvv_64") : (stream ? "axpy_scalar_4096" : "gemm_scalar_64");
  return has(k) ? k : "";
}

void Sim::unit_busy(int32_t u, bool busy) {
  Unit& un = units_[u];
  DomainState& d = dom_[un.domain];
  if (busy) {
    un.busy_since = now_;
    d.busy_units++;
  } else {
    un.busy_acc += now_ - std::max(un.busy_since, counters_since_);
    if (--d.busy_units <= 0) {
      d.busy_units = 0;
      domain_went_idle(un.domain);
    }
  }
}

void Sim::domain_went_idle(int di) {
  DomainState& d = dom_[di];
  if (!d.gate_enabled || d.gated) return;
  ++d.gate_token;
  if (d.idle_gate_ps <= 0) {
    d.gated = true;
    d.gated_since = now_;
  } else {
    // Gate only if the domain stays idle for idle_before_gate_ps.
    q_.push(now_ + d.idle_gate_ps, CMP_RUNTIME, EV_GATE, di, d.gate_token);
  }
}

ps_t Sim::domain_wake(int di) {
  DomainState& d = dom_[di];
  ++d.gate_token;  // new work cancels a pending gate timer
  if (d.gated) {
    d.gated = false;
    d.gated_acc += now_ - std::max(d.gated_since, counters_since_);
    d.wakeups++;
    d.waking_until = now_ + d.wake_ps;
  }
  return std::max<ps_t>(0, d.waking_until - now_);
}

void Sim::set_power_gating(const std::string& domain, bool enabled, ps_t wake_latency_ps, ps_t idle_before_gate_ps) {
  int di = parse_domain(domain);
  if (di < 0) throw std::runtime_error("unknown power domain: " + domain);
  if (di == D_NOC_CACHE || di == D_DRAM_IO) throw std::runtime_error("power gating is not modeled for " + domain + " (shared fabric)");
  if (wake_latency_ps < 0) throw std::runtime_error("wake latency must be >= 0");
  if (idle_before_gate_ps < 0) throw std::runtime_error("idle_before_gate_ps must be >= 0");
  DomainState& d = dom_[di];
  d.wake_ps = wake_latency_ps;
  d.idle_gate_ps = idle_before_gate_ps;
  if (enabled && !d.gate_enabled) {
    d.gate_enabled = true;
    if (d.busy_units == 0) domain_went_idle(di);
  } else if (!enabled && d.gate_enabled) {
    d.gate_enabled = false;
    ++d.gate_token;
    if (d.gated) {
      d.gated = false;
      d.gated_acc += now_ - std::max(d.gated_since, counters_since_);
    }
  }
}

void Sim::credit_job_cycles(const Exec& e, double cycles) {
  if (cycles <= 0) return;
  if (Job* j = job_ptr(e.job)) j->cycles[e.domain] += cycles;
}

void Sim::credit_job_busy(Exec& e) {
  if (Job* j = job_ptr(e.job)) j->busy[e.domain] += now_ - e.run_since;
  e.run_since = now_;
}

// ---------------------------------------------------------------------------
void Sim::dispatch_event(const Event& e) {
  switch (e.comp) {
    case CMP_RUNTIME:
      if (e.kind == EV_JOB_RELEASE) {
        on_job_release(e.b);
      } else if (e.kind == EV_GATE) {
        DomainState& d = dom_[e.a];
        if (e.b == d.gate_token && d.gate_enabled && !d.gated && d.busy_units == 0) {
          d.gated = true;
          d.gated_since = now_;
        }
      } else {
        on_op_ready(e.b, e.a);
      }
      break;
    case CMP_EXEC: {
      int32_t x = e.a;
      if (x < 0 || static_cast<size_t>(x) >= execs_.size()) break;
      Exec& ex = execs_[x];
      if (!ex.live || ex.uid != static_cast<uint32_t>(e.b)) break;
      switch (e.kind) {
        case EV_SETUP_DONE:
          if (static_cast<uint32_t>(e.c) != ex.gen) break;
          ex.in_setup = false;
          if (ex.preempt_req) {
            int eng = ex.engine;
            exec_suspend(x);
            dispatch(eng);
          } else if (!ex.begun) {
            exec_begin(x);
          } else {
            exec_try_compute(x);
          }
          break;
        case EV_COMPUTE_END:
          if (static_cast<uint32_t>(e.c) != ex.gen || !ex.computing) break;
          exec_compute_end(x);
          break;
        case EV_DATA_READY:
          exec_try_compute(x);
          break;
        case EV_MEM_DONE:
          if (static_cast<uint32_t>(e.c) != ex.gen) break;
          ex.in_tail = false;
          exec_finish(x);
          break;
        default: break;
      }
      break;
    }
    case CMP_DRAM:
      if (e.kind == EV_DRAM_ARRIVE) on_dram_arrive(e.a);
      else if (e.kind == EV_DRAM_BACKEND_TICK) on_dram_backend_tick();
      else if (e.kind == EV_NOC_BACKEND_TICK) on_noc_backend_tick();
      else on_dram_decide(e.a, e.b);
      break;
    default: break;
  }
}

std::vector<Completion> Sim::run_until(ps_t t_ps) {
  while (!q_.empty() && q_.top().t <= t_ps) {
    Event e = q_.pop();
    now_ = e.t;
    dispatch_event(e);
    ++events_;
    ev_kind_[e.kind & 31]++;
  }
  if (t_ps > now_) now_ = t_ps;
  // Write CSV trace rows older than the flush horizon (1 ms) in time order.
  csv_flush(dram_csv_, now_ - PS_PER_MS, true);
  csv_flush(noc_csv_, now_ - PS_PER_MS, false);
  std::vector<Completion> out;
  out.swap(done_);
  return out;
}

// ---------------------------------------------------------------------------
void Sim::set_domain_freq(const std::string& domain, double hz) {
  int d = parse_domain(domain);
  if (d < 0) throw std::runtime_error("unknown power domain: " + domain);
  if (!(hz > 0)) throw std::runtime_error("frequency must be positive");
  DomainState& ds = dom_[d];
  if (ds.hz == hz) return;
  // Credit progress at the old frequency, then stall the domain for the
  // transition latency (power_domains.dvfs_transition_us).
  std::vector<int32_t> running;
  for (size_t u = 0; u < units_.size(); ++u) {
    const Unit& un = units_[u];
    if (un.domain != d || un.exec < 0) continue;
    if (std::find(running.begin(), running.end(), un.exec) == running.end()) running.push_back(un.exec);
  }
  for (int32_t x : running) {
    Exec& e = execs_[x];
    if (!e.computing) continue;
    exec_credit(e);
    double el = static_cast<double>(std::max<ps_t>(0, now_ - std::max(e.seg_last, ds.stall_until)));
    e.seg_rem = std::max(0.0, e.seg_rem - el * ds.hz / 1e12);
    e.seg_last = now_;
    // keep act_credited consistent with the new seg_rem baseline
  }
  ps_t old_stall = ds.stall_until;
  ps_t trans = us_to_ps(soc_.dvfs_transition_us);
  ds.stall_until = std::max(old_stall, now_ + trans);
  ds.hz = hz;
  ds.transitions++;
  ds.stall_total += ds.stall_until - std::max(old_stall, now_);
  if (d == D_NOC_CACHE) noc_.set_freq(hz);
  for (int32_t x : running) {
    Exec& e = execs_[x];
    if (!e.computing) continue;
    e.seg_dvfs += ds.stall_until - std::max(old_stall, now_);
    exec_reschedule_segment(x);
  }
  TraceRec r;
  r.t = now_;
  r.dur = ds.stall_until - now_;
  r.kind = TK_FREQ;
  r.block = static_cast<uint8_t>(domain_block(d));
  r.unit = static_cast<uint16_t>(d);
  r.value = hz;
  emit(r);
}

double Sim::domain_freq(const std::string& domain) const {
  int d = parse_domain(domain);
  if (d < 0) throw std::runtime_error("unknown power domain: " + domain);
  return dom_[d].hz;
}

std::array<Sim::CounterSet, N_DOMAINS> Sim::counters(bool reset) {
  for (auto& e : execs_)
    if (e.live) exec_credit(e);
  std::array<CounterSet, N_DOMAINS> out{};
  ps_t elapsed = now_ - counters_since_;
  for (size_t u = 0; u < units_.size(); ++u) {
    const Unit& un = units_[u];
    ps_t b = un.busy_acc;
    if (un.exec >= 0) b += now_ - std::max(un.busy_since, counters_since_);
    out[un.domain].unit_busy += b;
  }
  for (int d = 0; d < N_DOMAINS; ++d) {
    out[d].units = dom_[d].units;
    out[d].act = dom_[d].act;
  }
  // NoC+cache domain: mesh link occupancy plus DMA engines; DRAM I/O: data bus.
  int nlinks = 0;
  for (int i = 0; i < noc_.n_links(); ++i)
    if (noc_.is_mesh_link(i)) nlinks++;
  ps_t noc_busy = noc_.total_busy_ps();
  out[D_NOC_CACHE].unit_busy += noc_busy - noc_busy_base_;
  out[D_NOC_CACHE].units += nlinks;
  ps_t dram_busy = 0;
  for (int c = 0; c < dram_.n_channels(); ++c) dram_busy += dram_.channel(c).busy_ps;
  out[D_DRAM_IO].unit_busy += dram_busy - dram_busy_base_;
  out[D_DRAM_IO].units += dram_.n_channels();
  for (int d = 0; d < N_DOMAINS; ++d) {
    CounterSet& c = out[d];
    c.busy = c.units > 0 ? std::min(elapsed, c.unit_busy / c.units) : 0;
    c.idle = elapsed - c.busy;
    const DomainState& ds = dom_[d];
    c.gated = ds.gated_acc + (ds.gated ? now_ - std::max(ds.gated_since, counters_since_) : 0);
    c.gated = std::min(c.gated, c.idle);
    c.wakeups = ds.wakeups;
  }
  if (reset) {
    for (auto& d : dom_) {
      d.act.fill(0);
      d.gated_acc = 0;
      d.wakeups = 0;
    }
    for (auto& un : units_) {
      un.busy_acc = 0;
      if (un.exec >= 0) un.busy_since = now_;
    }
    noc_busy_base_ = noc_busy;
    dram_busy_base_ = dram_busy;
    counters_since_ = now_;
  }
  return out;
}

static int engine_domain_guess(int engine, int cluster) {
  switch (engine) {
    case E_NPU: return D_NPU;
    case E_SIMT: return D_SIMT;
    case E_RT: return D_RT;
    case E_DSP: return D_DSP;
    case E_DMA: return D_NOC_CACHE;
    default: return cluster == 1 ? D_CPU_B : D_CPU_A;
  }
}

std::vector<Sim::PendingInfo> Sim::pending_jobs() {
  std::vector<PendingInfo> out;
  for (const auto& kv : job_slot_) {
    Job& j = jobs_[kv.second];
    const TaskPlan& pl = plans_[j.task];
    const TaskDef& td = tasks_[j.task];
    PendingInfo pi{};
    pi.job_id = j.id;
    pi.task_id = td.id;
    pi.release = j.release;
    pi.deadline = j.deadline;
    pi.started = j.start >= 0;
    double total = 0, done = 0;
    for (size_t op = 0; op < pl.ops.size(); ++op) {
      const JobOp& o = j.ops[op];
      const PlanOp& po = pl.ops[op];
      int engine = o.state >= 1 ? o.engine : -1;
      if (o.state == 0) {
        if (po.def < 0) {
          engine = mw_.transport == TR_NONE ? -1 : pl.mw_engine;
        } else {
          for (int e : td.ops[po.def].elig_order)
            if (!eng_[e].units.empty()) { engine = e; break; }
          if (td.affinity_engines)
            for (int e : td.ops[po.def].elig_order)
              if ((td.affinity_engines & (1u << e)) && !eng_[e].units.empty()) { engine = e; break; }
        }
      }
      if (engine < 0) continue;
      double est = est_cycles(j, static_cast<int32_t>(op), engine);
      int dom = engine_domain_guess(engine, pl.home_cpu_cluster);
      double frac = 0;
      if (o.state == 4) {
        frac = 1;
      } else if (o.state == 3 && o.exec >= 0 && execs_[o.exec].live) {
        frac = exec_progress(execs_[o.exec]);
        dom = execs_[o.exec].domain;
      }
      total += est;
      done += est * frac;
      pi.est_remaining_cycles[dom] += est * (1 - frac);
    }
    pi.frac_done = total > 0 ? done / total : 0;
    out.push_back(pi);
  }
  return out;
}

Sim::QueueDepths Sim::queue_depths(int top_n) {
  QueueDepths q;
  q.dram_ch.resize(static_cast<size_t>(dram_.n_channels()));
  q.dram_ctrl.assign(static_cast<size_t>(soc_.dram.ctrls), 0);
  for (int c = 0; c < dram_.n_channels(); ++c) {
    q.dram_ch[c] = dram_.queue_len(c);
    q.dram_ctrl[static_cast<size_t>(dram_.controller_of(c)) % q.dram_ctrl.size()] += dram_.queue_len(c);
  }
  std::vector<std::pair<double, int>> links;
  for (int i = 0; i < noc_.n_links(); ++i) {
    const NocLink& l = noc_.link(i);
    ps_t f = *std::max_element(l.free_at.begin(), l.free_at.end());
    if (f > now_) links.emplace_back(static_cast<double>(f - now_) / 1000.0, i);
  }
  std::sort(links.begin(), links.end(), [](const auto& a, const auto& b) { return a.first > b.first || (a.first == b.first && a.second < b.second); });
  for (size_t k = 0; k < links.size() && static_cast<int>(k) < top_n; ++k) q.noc_links.emplace_back(noc_.link_name(links[k].second), links[k].first);
  for (int e = 0; e < N_ENGINES; ++e) {
    q.engine_ready[e] = static_cast<int>(eng_[e].ready.size());
    int busy = 0;
    for (int32_t u : eng_[e].units)
      if (units_[u].exec >= 0) busy++;
    q.engine_busy[e] = busy;
  }
  return q;
}

// ---------------------------------------------------------------------------
// Tracing
static void pack_rec(const TraceRec& r, uint8_t* b) {
  std::memcpy(b + 0, &r.t, 8);
  std::memcpy(b + 8, &r.dur, 8);
  std::memcpy(b + 16, &r.job, 8);
  std::memcpy(b + 24, &r.bytes, 8);
  std::memcpy(b + 32, &r.value, 8);
  std::memcpy(b + 40, &r.task, 4);
  std::memcpy(b + 44, &r.op, 4);
  b[48] = r.kind;
  b[49] = r.block;
  std::memcpy(b + 50, &r.unit, 2);
  b[52] = static_cast<uint8_t>(r.sx);
  b[53] = static_cast<uint8_t>(r.sy);
  b[54] = static_cast<uint8_t>(r.dx);
  b[55] = static_cast<uint8_t>(r.dy);
}

void Sim::emit(const TraceRec& r) {
  hash_.add(static_cast<uint64_t>(r.t));
  hash_.add(static_cast<uint64_t>(r.dur));
  hash_.add(static_cast<uint64_t>(r.job));
  hash_.add(static_cast<uint64_t>(r.bytes));
  uint64_t vb;
  std::memcpy(&vb, &r.value, 8);
  hash_.add(vb);
  hash_.add((static_cast<uint64_t>(static_cast<uint32_t>(r.task)) << 32) | static_cast<uint32_t>(r.op));
  hash_.add(static_cast<uint64_t>(r.kind) | (static_cast<uint64_t>(r.block) << 8) | (static_cast<uint64_t>(r.unit) << 16) |
            (static_cast<uint64_t>(static_cast<uint8_t>(r.sx)) << 32) | (static_cast<uint64_t>(static_cast<uint8_t>(r.sy)) << 40) |
            (static_cast<uint64_t>(static_cast<uint8_t>(r.dx)) << 48) | (static_cast<uint64_t>(static_cast<uint8_t>(r.dy)) << 56));
  hash_.count++;
  if (trace_on_) trace_buf_.push_back(r);
  if (gct_) {
    uint8_t b[56];
    pack_rec(r, b);
    std::fwrite(b, 1, sizeof b, gct_);
  }
}

std::vector<TraceRec> Sim::trace_drain() {
  std::vector<TraceRec> out;
  out.swap(trace_buf_);
  return out;
}

bool Sim::trace_open(const std::string& path) {
  trace_close();
  gct_ = std::fopen(path.c_str(), "wb");
  if (!gct_) return false;
  gct_path_ = path;
  // Header: magic, version, record size.
  const char magic[4] = {'G', 'C', 'T', '1'};
  uint32_t ver = 1, rs = 56;
  std::fwrite(magic, 1, 4, gct_);
  std::fwrite(&ver, 4, 1, gct_);
  std::fwrite(&rs, 4, 1, gct_);
  return true;
}

void Sim::trace_close() {
  if (!gct_) return;
  // Footer: JSON string tables, then the footer length and an end marker,
  // so readers can decode task/op/kind/block indices.
  Json f = Json::object();
  Json tasks = Json::array(), ops = Json::array(), kinds = Json::array(), blocks = Json::array();
  for (size_t t = 0; t < tasks_.size(); ++t) {
    tasks.push(Json::string(tasks_[t].id));
    Json o = Json::array();
    for (const auto& n : op_names(static_cast<int>(t))) o.push(Json::string(n));
    ops.push(o);
  }
  for (int k = 0; k < N_TK; ++k) kinds.push(Json::string(trace_kind_name(k)));
  for (int b = 0; b < N_TB; ++b) blocks.push(Json::string(trace_block_name(b)));
  f.set("tasks", tasks);
  f.set("ops", ops);
  f.set("kinds", kinds);
  f.set("blocks", blocks);
  f.set("trace_hash", Json::string(hash_.hex()));
  std::string s = dump_json(f);
  uint64_t n = s.size();
  std::fwrite(s.data(), 1, s.size(), gct_);
  std::fwrite(&n, 8, 1, gct_);
  const char end[4] = {'G', 'C', 'T', 'E'};
  std::fwrite(end, 1, 4, gct_);
  std::fclose(gct_);
  gct_ = nullptr;
}

std::vector<std::string> Sim::task_names() const {
  std::vector<std::string> v;
  for (const auto& t : tasks_) v.push_back(t.id);
  return v;
}

std::vector<std::string> Sim::op_names(int task) const {
  std::vector<std::string> v;
  if (task < 0 || static_cast<size_t>(task) >= plans_.size()) return v;
  for (const auto& p : plans_[task].ops)
    v.push_back(p.def >= 0 ? tasks_[task].ops[p.def].id : (p.def == -1 ? "__rx" : "__tx"));
  return v;
}

// ---------------------------------------------------------------------------
// Checkpoint / restore. The whole simulator state (configs, tasks, event
// queue, component state, RNGs, counters, trace hash) is written with the
// GC_IO field lists. Trace buffers and open files are not part of the state.
static const uint32_t kCkptMagic = 0x4B434347;  // "GCCK"
static const uint32_t kCkptVersion = 2;

#define GC_SIM_STATE                                                                                                           \
  soc_, fid_, seed_, now_, q_, rng_, tasks_, plans_, task_index_, jobs_, free_jobs_, job_slot_, active_count_, job_seq_,       \
      next_job_id_, units_, eng_, execs_, free_execs_, next_uid_, txns_, free_txns_, dram_seq_, noc_, cache_, dram_, dom_,   \
      counters_since_, noc_busy_base_, dram_busy_base_, mw_, qos_on_, ready_seq_, npu_l2_, cpu_cpi_, l2_fallbacks_npu_,         \
      l2_fallbacks_cpu_, l2_hits_npu_, l2_hits_cpu_, events_, hash_, last_dram_q_, cpu_kernel_cpi_, l2_sources_, faults_

std::vector<uint8_t> Sim::checkpoint() {
  if (dram_backend_ || noc_backend_)
    throw std::runtime_error("checkpoint() is not supported while an external L2 backend (Ramulator / BookSim) is attached");
  Writer w;
  uint32_t m = kCkptMagic, v = kCkptVersion;
  sv(w, m);
  sv(w, v);
  sv_all(w, GC_SIM_STATE);
  return std::move(w.buf);
}

void Sim::restore(const std::vector<uint8_t>& blob) {
  Reader r;
  r.p = blob.data();
  r.n = blob.size();
  uint32_t m = 0, v = 0;
  sv(r, m);
  sv(r, v);
  if (m != kCkptMagic || v != kCkptVersion) throw std::runtime_error("not a glasschip checkpoint (or wrong version)");
  sv_all(r, GC_SIM_STATE);
  if (r.pos != r.n) throw std::runtime_error("checkpoint blob has trailing bytes");
  done_.clear();
  trace_buf_.clear();
}

// ---------------------------------------------------------------------------
Json Sim::info() const {
  Json j = Json::object();
  j.set("module", Json::string("glasschip_core"));
  j.set("version", Json::string("0.1.0"));
  j.set("soc", Json::string(soc_.name));
  Json f = Json::object();
  auto lv = [](int l) { return Json::string("L" + std::to_string(l)); };
  f.set("name", Json::string(fid_.name));
  f.set("cpu", lv(fid_.cpu));
  f.set("npu", lv(fid_.npu));
  f.set("simt", lv(fid_.simt));
  f.set("dsp", lv(fid_.dsp));
  f.set("noc", lv(fid_.noc));
  f.set("dram", lv(fid_.dram));
  f.set("cache", lv(fid_.cache));
  f.set("rt", lv(0));
  f.set("dma", lv(eng_[E_DMA].level));
  f.set("quantum_ns", Json::number(fid_.quantum_ns));
  j.set("fidelity", f);
  Json w = Json::array();
  for (const auto& s : fid_.warnings) w.push(Json::string(s));
  if (fid_.npu >= 2 && l2_fallbacks_npu_ > 0)
    w.push(Json::string("npu: L2 requested but " + std::to_string(l2_fallbacks_npu_) + " op executions had no SCALE-Sim cycle table; used L1"));
  if (fid_.dram >= 2 && !dram_backend_) w.push(Json::string("dram: L2 requested but no DramBackend attached (" + dram_attach_error_ + "); using L1"));
  if (fid_.noc >= 2 && !noc_backend_) w.push(Json::string("noc: L2 requested but " + noc_l2_note_ + "; using L1"));
  if (fid_.cpu >= 2 && l2_fallbacks_cpu_ > 0)
    w.push(Json::string("cpu: L2 requested but " + std::to_string(l2_fallbacks_cpu_) + " op executions had no CPI table entry; used L1"));
  j.set("warnings", w);
  Json l2 = Json::object();
  l2.set("npu_table_hits", Json::number(static_cast<double>(l2_hits_npu_)));
  l2.set("npu_fallbacks", Json::number(static_cast<double>(l2_fallbacks_npu_)));
  l2.set("cpu_table_hits", Json::number(static_cast<double>(l2_hits_cpu_)));
  l2.set("cpu_fallbacks", Json::number(static_cast<double>(l2_fallbacks_cpu_)));
  Json srcs = Json::array();
  for (const auto& s2 : l2_sources_) srcs.push(Json::string(s2));
  l2.set("tables", srcs);
  j.set("l2", l2);
  Json fj = Json::object();
  static const char* fk[] = {"npu_bitflip", "noc_delay", "noc_drop"};
  for (int k = 0; k < N_FAULT; ++k) {
    Json o = Json::object();
    o.set("rate", Json::number(faults_[k].rate));
    o.set("param_ns", Json::number(static_cast<double>(faults_[k].param) / 1000.0));
    o.set("count", Json::number(static_cast<double>(faults_[k].count)));
    fj.set(fk[k], o);
  }
  j.set("faults", fj);
  Json units = Json::object();
  for (int e = 0; e < N_ENGINES; ++e) units.set(engine_name(e), Json::number(static_cast<double>(eng_[e].units.size())));
  j.set("units", units);
  Json sched = Json::object();
  static const char* pn[] = {"fifo", "fixed_priority", "edf", "edf_mc"};
  for (int e = 0; e < N_ENGINES; ++e) sched.set(engine_name(e), Json::string(pn[eng_[e].policy]));
  j.set("schedulers", sched);
  j.set("mapping", Json::string(fid_.p.mapping == MAP_EFT ? "eft" : "static"));
  j.set("qos", Json::boolean(qos_on_));
  j.set("cache_partitioned", Json::boolean(cache_.partitioned()));
  static const char* tr[] = {"none", "copy", "zero_copy"};
  static const char* ex[] = {"single_threaded", "multi_threaded", "events"};
  j.set("transport", Json::string(tr[mw_.transport]));
  j.set("executor", Json::string(ex[mw_.executor]));
  j.set("events_processed", Json::number(static_cast<double>(events_)));
  j.set("trace_records", Json::number(static_cast<double>(hash_.count)));
  Json ek = Json::object();
  static const std::pair<int, const char*> kn[] = {{EV_JOB_RELEASE, "job_release"}, {EV_OP_READY, "op_ready"}, {EV_SETUP_DONE, "setup_done"},
                                                   {EV_COMPUTE_END, "compute_end"}, {EV_DATA_READY, "data_ready"}, {EV_MEM_DONE, "mem_done"},
                                                   {EV_DRAM_ARRIVE, "dram_arrive"}, {EV_DRAM_DECIDE, "dram_decide"}};
  for (const auto& k : kn) ek.set(k.second, Json::number(static_cast<double>(ev_kind_[k.first])));
  j.set("events_by_kind", ek);
  j.set("now_ps", Json::number(static_cast<double>(now_)));
  Json tasks = Json::array();
  for (const auto& t : tasks_) tasks.push(Json::string(t.id));
  j.set("tasks", tasks);
  Json backends = Json::object();
  backends.set("noc", Json::string(noc_backend_ ? std::string(noc_backend_->name()) + " (quantum " + std::to_string(fid_.quantum_ns) + " ns)"
                                                : "builtin L" + std::to_string(std::min(1, fid_.noc))));
  backends.set("dram", Json::string(dram_backend_ ? std::string(dram_backend_->name()) + " (quantum " + std::to_string(fid_.quantum_ns) + " ns)"
                                                  : "builtin L" + std::to_string(std::min(1, fid_.dram))));
  j.set("backends", backends);
  Json mp = Json::object();
  mp.set("bulk_chunk_bytes", Json::number(fid_.p.bulk_chunk_bytes));
  mp.set("cpu_miss_bytes", Json::number(fid_.p.cpu_miss_bytes));
  mp.set("cache_block_bytes", Json::number(fid_.p.cache_block_bytes));
  mp.set("stream_bypass_bytes", Json::number(fid_.p.stream_bypass_bytes));
  mp.set("exec_cv", Json::number(fid_.p.exec_cv));
  j.set("params", mp);
  return j;
}

}  // namespace gc
