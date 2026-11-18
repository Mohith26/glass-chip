// Runtime model (spec 7.5): job release, op-to-engine mapping, per-engine
// schedulers with preemption, buffer handoff, interrupt latency, ROS 2-style
// middleware ops, and per-job delay attribution with a critical path.
#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "glasschip/sim.hpp"

namespace gc {

static bool is_accel(int e) { return e == E_NPU || e == E_SIMT || e == E_DSP || e == E_DMA; }

Job* Sim::job_ptr(int64_t id) {
  auto it = job_slot_.find(id);
  if (it == job_slot_.end()) return nullptr;
  return &jobs_[it->second];
}

void Sim::build_plan(int t) {
  const TaskDef& td = tasks_[t];
  TaskPlan& pl = plans_[t];
  int n = static_cast<int>(td.ops.size());
  pl.ops.assign(static_cast<size_t>(n + 2), PlanOp{});
  pl.ops[0].def = -1;
  pl.ops[n + 1].def = -2;
  double woff = 0, aoff = 0;
  auto align = [](double v) { return std::ceil(v / 4096.0) * 4096.0; };
  for (int i = 0; i < n; ++i) {
    PlanOp& p = pl.ops[i + 1];
    const OpDef& d = td.ops[i];
    p.def = i;
    if (d.deps.empty()) {
      p.preds.push_back(0);
    } else {
      for (int dp : d.deps) p.preds.push_back(dp + 1);
    }
    p.w_off = woff;
    woff += align(d.bytes_w);
    p.out_off = aoff;
    aoff += align(std::max(64.0, d.bytes_out));
    // An op is NPU-bound if static mapping would put it there (or NPU is its only option).
    p.npu_only = (d.eligible == (1u << E_NPU)) ||
                 (fid_.p.mapping == MAP_STATIC && !d.elig_order.empty() && d.elig_order[0] == E_NPU && !eng_[E_NPU].units.empty());
  }
  for (int i = 1; i <= n; ++i)
    for (int32_t p : pl.ops[i].preds) pl.ops[p].succs.push_back(i);
  for (int i = 1; i <= n; ++i)
    if (pl.ops[i].succs.empty()) {
      pl.ops[i].succs.push_back(n + 1);
      pl.ops[n + 1].preds.push_back(i);
    }
  pl.act_region = aoff;
  // Synthetic address map (buffer manager): weights persist across jobs,
  // activations alternate between two buffers by job parity.
  pl.w_base = (1ull << 44) + static_cast<uint64_t>(t) * (1ull << 38);
  pl.a_base = (2ull << 44) + static_cast<uint64_t>(t) * (1ull << 38);
  pl.in_base = (3ull << 44) + static_cast<uint64_t>(t) * (1ull << 36);
  // Middleware message volumes.
  pl.rx_bytes = 0; pl.n_rx = 0; pl.tx_bytes = 0; pl.n_tx = 0; pl.input_task = -1;
  for (size_t o = 0; o < tasks_.size(); ++o)
    for (const MsgDef& m : tasks_[o].msgs)
      if (m.consumer == td.id) {
        pl.rx_bytes += m.bytes;
        pl.n_rx++;
        if (pl.input_task < 0) pl.input_task = static_cast<int>(o);
      }
  for (const MsgDef& m : td.msgs) { pl.tx_bytes += m.bytes; pl.n_tx++; }
  // Where rx/tx run: the executor's CPU core(s), or the RT core for RT tasks.
  int home = td.ops[0].elig_order.empty() ? E_CPU : td.ops[0].elig_order[0];
  if (td.affinity_engines & (1u << E_RT)) home = E_RT;
  pl.home_cpu_cluster = td.cpu_cluster;
  if (mw_.transport == TR_NONE) {
    pl.mw_engine = -1;
  } else if (home == E_RT) {
    pl.mw_engine = fid_.p.middleware_on_rt ? E_RT : -1;  // params.middleware_on_rt
    pl.mw_mask = ~0ull;
  } else {
    pl.mw_engine = E_CPU;
    uint64_t m = 0;
    const auto& cu = eng_[E_CPU].units;
    for (size_t i = 0; i < cu.size(); ++i) {
      if (mw_.executor == EX_SINGLE && i > 0) break;  // single-threaded executor: one core for all callbacks
      if (td.cpu_cluster >= 0 && units_[cu[i]].cluster != td.cpu_cluster && mw_.executor != EX_SINGLE) continue;
      m |= 1ull << i;
    }
    pl.mw_mask = m ? m : 1ull;
  }
}

void Sim::rebuild_plans() {
  for (size_t t = 0; t < tasks_.size(); ++t) build_plan(static_cast<int>(t));
}

void Sim::add_task(const Json& graph) {
  TaskDef td = parse_task(graph);
  if (task_index_.count(td.id)) throw std::runtime_error("task already added: " + td.id);
  int t = static_cast<int>(tasks_.size());
  task_index_[td.id] = t;
  tasks_.push_back(std::move(td));
  plans_.emplace_back();
  active_count_.push_back(0);
  job_seq_.push_back(0);
  rebuild_plans();
}

void Sim::set_middleware(const Json& params, const std::string& transport, const std::string& executor) {
  MiddlewareCfg m;
  m.transport = transport == "zero_copy" ? TR_ZERO_COPY : (transport == "copy" ? TR_COPY : TR_NONE);
  if (transport != "zero_copy" && transport != "copy" && transport != "none" && !transport.empty())
    throw std::runtime_error("unknown transport: " + transport);
  m.executor = executor == "multi_threaded" ? EX_MULTI : (executor == "events" ? EX_EVENTS : EX_SINGLE);
  if (const Json* tp = params.get(m.transport == TR_ZERO_COPY ? "zero_copy" : "copy"); tp && m.transport != TR_NONE) {
    m.ser_ns_kb = tp->num("serialize_ns_per_kb", 0);    // middleware.json <transport>.serialize_ns_per_kb
    m.deser_ns_kb = tp->num("deserialize_ns_per_kb", 0);
    m.fixed_ns = tp->num("fixed_ns_per_msg", 0);
    m.copies = tp->num("copies", m.transport == TR_COPY ? 1 : 0);
    m.copy_ns_kb = tp->num("copy_ns_per_kb", 0);  // CPU time per KB per extra copy
  }
  if (const Json* ex = params.get("executor"); ex && m.transport != TR_NONE) {
    const char* key = m.executor == EX_MULTI ? "multi_threaded_dispatch_ns" : (m.executor == EX_EVENTS ? "events_dispatch_ns" : "single_threaded_dispatch_ns");
    m.dispatch_ns = ex->num(key, 0);  // middleware.json executor.*_dispatch_ns
  }
  mw_ = m;
  rebuild_plans();
}

void Sim::set_scheduler(const std::string& engine, const std::string& policy) {
  int p = parse_sched(policy);
  if (p < 0) throw std::runtime_error("unknown scheduler policy: " + policy);
  int e = parse_engine(engine);
  if (engine == "all") {
    for (auto& es : eng_) es.policy = p;
    return;
  }
  if (e < 0) throw std::runtime_error("unknown engine: " + engine);
  eng_[e].policy = p;
}

void Sim::set_mapping(const std::string& policy) {
  if (policy == "static") fid_.p.mapping = MAP_STATIC;
  else if (policy == "eft" || policy == "greedy_eft") fid_.p.mapping = MAP_EFT;
  else throw std::runtime_error("unknown mapping policy: " + policy);
  rebuild_plans();
}

void Sim::set_task_priority(const std::string& task_id, int64_t priority) {
  auto it = task_index_.find(task_id);
  if (it == task_index_.end()) throw std::runtime_error("unknown task: " + task_id);
  tasks_[it->second].priority_override = priority;
}

void Sim::set_qos(bool enabled) {
  if (enabled == qos_on_) return;
  qos_on_ = enabled;
  noc_.set_qos(enabled);
  dram_.requeue_for_qos(enabled);
}

void Sim::set_cache_partition(const std::map<std::string, int>* ways) {
  if (!ways || ways->empty()) {
    cache_.set_partition(nullptr);
    return;
  }
  std::array<int, N_QOS> w{};
  for (const auto& kv : *ways) {
    int q = parse_qos(kv.first);
    if (q < 0) throw std::runtime_error("unknown qos class: " + kv.first);
    w[q] = kv.second;
  }
  cache_.set_partition(&w);
}

// ---------------------------------------------------------------------------
int64_t Sim::release_job(const std::string& task_id, ps_t t_ps, int64_t tag) {
  auto it = task_index_.find(task_id);
  if (it == task_index_.end()) throw std::runtime_error("unknown task: " + task_id);
  int t = it->second;
  if (t_ps < now_) t_ps = now_;
  // on_overrun: skip drops this release if a previous job is still active
  // (decided with the state at call time).
  if (tasks_[t].skip_on_overrun && active_count_[t] > 0) return -1;
  int32_t slot;
  if (!free_jobs_.empty()) {
    slot = free_jobs_.back();
    free_jobs_.pop_back();
  } else {
    slot = static_cast<int32_t>(jobs_.size());
    jobs_.emplace_back();
  }
  Job& j = jobs_[slot];
  j = Job{};
  j.id = next_job_id_++;
  j.task = t;
  j.tag = tag;
  j.release = t_ps;
  j.deadline = t_ps + tasks_[t].deadline_ps;
  j.parity = static_cast<int>(job_seq_[t]++ % 2);
  j.ops.assign(plans_[t].ops.size(), JobOp{});
  j.remaining = static_cast<int32_t>(j.ops.size());
  job_slot_[j.id] = slot;
  active_count_[t]++;
  q_.push(t_ps, CMP_RUNTIME, EV_JOB_RELEASE, 0, j.id);
  return j.id;
}

void Sim::on_job_release(int64_t id) {
  Job* j = job_ptr(id);
  if (!j) return;
  j->released = true;
  const TaskPlan& pl = plans_[j->task];
  for (size_t i = 0; i < pl.ops.size(); ++i) j->ops[i].deps_left = static_cast<int32_t>(pl.ops[i].preds.size());
  TraceRec r;
  r.t = now_;
  r.kind = TK_JOB_RELEASE;
  r.block = TB_RUNTIME;
  r.job = id;
  r.task = j->task;
  r.value = static_cast<double>(j->deadline);
  emit(r);
  op_deps_done(*j, 0);
}

int Sim::map_op(const Job& j, int32_t op, uint64_t* mask) {
  const TaskDef& td = tasks_[j.task];
  const TaskPlan& pl = plans_[j.task];
  const PlanOp& po = pl.ops[op];
  *mask = ~0ull;
  if (po.def < 0) {
    *mask = pl.mw_mask;
    return pl.mw_engine;
  }
  const OpDef& d = td.ops[po.def];
  std::vector<int> cand;
  for (int e : d.elig_order)
    if (!eng_[e].units.empty()) cand.push_back(e);
  if (cand.empty()) throw std::runtime_error("task " + td.id + " op " + d.id + ": no eligible engine present in SoC");
  if (td.affinity_engines) {
    std::vector<int> pref;
    for (int e : cand)
      if (td.affinity_engines & (1u << e)) pref.push_back(e);
    if (!pref.empty()) cand = pref;
  }
  int chosen = cand[0];
  if (fid_.p.mapping == MAP_EFT && cand.size() > 1) {
    // Greedy earliest-finish-time: estimated engine availability + estimated duration.
    ps_t best = PS_NEVER;
    for (int e : cand) {
      const EngineState& es = eng_[e];
      ps_t avail = PS_NEVER;
      for (int32_t u : es.units) {
        const Unit& un = units_[u];
        ps_t a = un.exec < 0 ? now_ : std::max(now_, execs_[un.exec].est_end);
        avail = std::min(avail, a);
      }
      double queued = 0;
      for (const auto& it : es.ready) queued += est_cycles(*job_ptr(it.job), it.op, e);
      int dom = units_[es.units[0]].domain;
      ps_t qps = cycles_to_ps(queued / es.units.size(), dom_[dom].hz);
      ps_t fin = avail + qps + cycles_to_ps(est_cycles(j, op, e), dom_[dom].hz);
      if (fin < best) { best = fin; chosen = e; }
    }
  }
  if (chosen == E_CPU && td.cpu_cluster >= 0) {
    uint64_t m = 0;
    const auto& cu = eng_[E_CPU].units;
    for (size_t i = 0; i < cu.size(); ++i)
      if (units_[cu[i]].cluster == td.cpu_cluster) m |= 1ull << i;
    if (m) *mask = m;
  }
  return chosen;
}

void Sim::op_deps_done(Job& j, int32_t op) {
  JobOp& o = j.ops[op];
  const TaskPlan& pl = plans_[j.task];
  const PlanOp& po = pl.ops[op];
  uint64_t mask = ~0ull;
  int engine = map_op(j, op, &mask);
  // Middleware ops with no cost run nowhere.
  if (po.def < 0 && engine >= 0) {
    bool rx = po.def == -1;
    double ns = rx ? (mw_.dispatch_ns + mw_.deser_ns_kb * pl.rx_bytes / 1024 + mw_.fixed_ns * pl.n_rx)
                   : ((mw_.ser_ns_kb + mw_.copies * mw_.copy_ns_kb) * pl.tx_bytes / 1024 + mw_.fixed_ns * pl.n_tx);
    if (ns <= 0) engine = -1;
  }
  o.engine = engine;
  o.est_domain = -1;
  ps_t ready = j.release;
  o.crit_pred = -1;
  o.gap = 0;
  o.gap_cat = A_MIDDLEWARE;
  o.gap_cause.clear();
  for (int32_t pi : po.preds) {
    const JobOp& p = j.ops[pi];
    ps_t g = 0;
    int cat = A_MIDDLEWARE;
    std::string cause;
    int pe = p.engine;
    bool host_next = engine == E_CPU || engine == E_RT || (po.def == -2 && engine < 0);
    if (pe >= 0 && is_accel(pe) && host_next) {
      // Completion interrupt to the host core (params.irq_latency_ns / rt_irq_latency_ns).
      g += ns_to_ps(engine == E_RT ? fid_.p.rt_irq_ns : fid_.p.irq_ns);
      cause = "irq";
    }
    if (pe >= 0 && engine >= 0 && ((pe == E_RT) != (engine == E_RT))) {
      // Buffer manager: data crossing into or out of RT TCM needs a DMA copy;
      // everything else is a zero-copy handoff through shared memory.
      const PlanOp& pp = pl.ops[pi];
      double bytes = pp.def >= 0 ? tasks_[j.task].ops[pp.def].bytes_out : (pi == 0 ? pl.rx_bytes : 0);
      if (bytes > 0) {
        g += ns_to_ps(soc_.dma.desc_ns) + ns_to_ps(bytes / fid_.p.rt_copy_bytes_per_ns);  // params.rt_copy_bytes_per_ns
        dom_[D_NOC_CACHE].act[K_DMA_BYTES] += 2 * bytes;
        cat = A_DMA;
        cause = cause.empty() ? "tcm_copy" : cause + "+tcm_copy";
      }
    }
    ps_t cand = p.end + g;
    if (o.crit_pred < 0 || cand > ready) {
      ready = cand;
      o.crit_pred = pi;
      o.gap = g;
      o.gap_cat = static_cast<int8_t>(cat);
      o.gap_cause = cause;
    }
  }
  if (po.preds.empty()) ready = j.release;
  o.ready = ready;
  if (ready > now_) {
    o.state = 1;
    q_.push(ready, CMP_RUNTIME, EV_OP_READY, op, j.id);
  } else {
    on_op_ready(j.id, op);
  }
}

void Sim::on_op_ready(int64_t job_id, int32_t op) {
  Job* j = job_ptr(job_id);
  if (!j) return;
  JobOp& o = j->ops[op];
  if (o.engine < 0) {
    o.state = 3;
    o.start = now_;
    complete_op(*j, op, now_);
    return;
  }
  enqueue_op(*j, op);
}

void Sim::priority_key(const Job& j, int engine, int64_t* k1, int64_t* k2) const {
  const TaskDef& td = tasks_[j.task];
  switch (eng_[engine].policy) {
    case SP_FIXED_PRIORITY:  // rate monotonic: shorter period runs first
      *k1 = td.priority_override >= 0 ? td.priority_override : td.period_ps;
      *k2 = j.release;
      break;
    case SP_EDF:
      *k1 = j.deadline;
      *k2 = j.release;
      break;
    case SP_EDF_MC: {
      // Mixed criticality: safety > high/medium > low, EDF inside a level, so
      // best-effort work never preempts safety work.
      int lvl = td.crit == C_SAFETY ? 0 : (td.crit == C_LOW || td.qos == Q_BEST_EFFORT) ? 2 : 1;
      *k1 = lvl;
      *k2 = j.deadline;
      break;
    }
    default:  // FIFO by arrival at the engine
      *k1 = 0;
      *k2 = now_;
      break;
  }
}

void Sim::enqueue_op(Job& j, int32_t op) {
  JobOp& o = j.ops[op];
  uint64_t mask = ~0ull;
  int engine = map_op(j, op, &mask);
  if (engine != o.engine) engine = o.engine;  // mapping fixed at deps-done time
  o.state = 2;
  ReadyItem it;
  priority_key(j, engine, &it.k1, &it.k2);
  it.seq = ready_seq_++;
  it.job = j.id;
  it.op = op;
  it.unit_mask = mask;
  it.ready = now_;
  push_ready(engine, it);
  dispatch(engine);
}

void Sim::push_ready(int engine, const ReadyItem& it) { eng_[engine].ready.push_back(it); }

int Sim::want_units(const Job& j, int32_t op, int engine) const {
  const PlanOp& po = plans_[j.task].ops[op];
  if (po.def < 0) return 1;
  const OpDef& d = tasks_[j.task].ops[po.def];
  int n = static_cast<int>(eng_[engine].units.size());
  if (engine == E_NPU) {
    // Large layers spread across tiles (params.npu_min_macs_per_tile, npu_max_tiles_per_op).
    double work = d.macs > 0 ? d.macs : std::max(d.flops, d.bytes_in);
    int cap = std::max(1, std::min(n, fid_.p.npu_max_tiles_per_op));
    double k = std::ceil(work / std::max(1.0, fid_.p.npu_min_macs_per_tile));
    return static_cast<int>(std::clamp(k, 1.0, static_cast<double>(cap)));
  }
  if (engine == E_SIMT) {
    if (d.threads > 0) return static_cast<int>(std::clamp(std::ceil(d.threads / soc_.simt.lanes), 1.0, static_cast<double>(std::max(1, n))));
    return n;
  }
  return 1;
}

bool Sim::try_preempt(int engine, const ReadyItem& it) {
  EngineState& es = eng_[engine];
  if (!es.preemptive || es.policy == SP_FIFO) return false;
  int victim = -1;
  int64_t vk1 = 0, vk2 = 0;
  for (size_t i = 0; i < es.units.size(); ++i) {
    if (!(it.unit_mask & (1ull << i))) continue;
    const Unit& u = units_[es.units[i]];
    if (u.exec < 0) continue;
    const Exec& e = execs_[u.exec];
    if (e.preempt_req || e.units.size() != 1 || e.in_tail) continue;
    bool worse = e.k1 > it.k1 || (e.k1 == it.k1 && e.k2 > it.k2);
    if (!worse) continue;
    if (victim < 0 || e.k1 > vk1 || (e.k1 == vk1 && e.k2 > vk2)) {
      victim = static_cast<int>(u.exec);
      vk1 = e.k1;
      vk2 = e.k2;
    }
  }
  if (victim < 0) return false;
  Exec& v = execs_[victim];
  v.preempt_req = true;
  // L0 (fluid) and RT execs, or L1 execs stalled on memory, stop immediately;
  // L1 execs in a compute step stop at the step boundary.
  if (v.level == 0 || v.engine == E_RT || (!v.computing && !v.in_setup)) {
    exec_suspend(victim);
    return true;
  }
  return false;
}

void Sim::dispatch(int engine) {
  if (engine < 0) return;
  EngineState& es = eng_[engine];
  for (int guard = 0; guard < 4096 && !es.ready.empty(); ++guard) {
    std::stable_sort(es.ready.begin(), es.ready.end(), [](const ReadyItem& a, const ReadyItem& b) {
      if (a.k1 != b.k1) return a.k1 < b.k1;
      if (a.k2 != b.k2) return a.k2 < b.k2;
      return a.seq < b.seq;
    });
    bool progress = false;
    for (size_t i = 0; i < es.ready.size(); ++i) {
      ReadyItem it = es.ready[i];
      std::vector<int32_t> idle;
      int32_t pref = -1;
      for (size_t k = 0; k < es.units.size(); ++k) {
        if (!(it.unit_mask & (1ull << k))) continue;
        const Unit& u = units_[es.units[k]];
        if (u.exec >= 0) continue;
        if (u.reserved_job >= 0 && u.reserved_job != it.job) continue;
        idle.push_back(es.units[k]);
        if (u.last_job == it.job && pref < 0) pref = es.units[k];
      }
      if (idle.empty()) {
        if (i == 0 && try_preempt(engine, it)) { progress = true; break; }
        continue;
      }
      int want = 1;
      if (it.exec < 0) {
        Job* j = job_ptr(it.job);
        want = j ? want_units(*j, it.op, engine) : 1;
      }
      std::vector<int32_t> take;
      if (pref >= 0) take.push_back(pref);
      for (int32_t u : idle) {
        if (static_cast<int>(take.size()) >= want) break;
        if (u != pref) take.push_back(u);
      }
      es.ready.erase(es.ready.begin() + static_cast<std::ptrdiff_t>(i));
      start_exec(engine, it, take, false);
      progress = true;
      break;
    }
    if (!progress) break;
  }
}

void Sim::start_exec(int engine, const ReadyItem& it, const std::vector<int32_t>& units, bool by_preemption) {
  if (it.exec >= 0) {
    exec_resume(it.exec, units, true);
    return;
  }
  Job* jp = job_ptr(it.job);
  if (!jp) return;
  Job& j = *jp;
  JobOp& o = j.ops[it.op];
  const TaskPlan& pl = plans_[j.task];
  const PlanOp& po = pl.ops[it.op];
  o.state = 3;
  o.start = now_;
  o.wait = now_ - o.ready;
  o.wait_cat = static_cast<int8_t>((engine == E_CPU || engine == E_RT) ? A_RUNQUEUE : A_ENGINE_QUEUE);
  if (engine == E_NPU && j.npu_restore) o.wait_cat = A_PREEMPTION;
  o.unit = units_[units[0]].index;
  o.nunits = static_cast<int>(units.size());
  o.block = units_[units[0]].block;
  if (j.start < 0) {
    j.start = now_;
    TraceRec r;
    r.t = now_;
    r.kind = TK_JOB_START;
    r.block = TB_RUNTIME;
    r.job = j.id;
    r.task = j.task;
    emit(r);
  }
  int level = eng_[engine].level;
  OpCost c;
  compute_cost(j, it.op, engine, static_cast<int>(units.size()), level, c);
  if (c.l2) {
    if (engine == E_NPU) l2_hits_npu_++;
    else l2_hits_cpu_++;
  }
  if (c.l2_fallback) {
    if (engine == E_NPU) l2_fallbacks_npu_++;
    else l2_fallbacks_cpu_++;
  }
  if (fid_.p.exec_cv > 0 && po.def >= 0) {
    // Execution-time noise (params.exec_cv): lognormal with mean 1.
    double s = std::sqrt(std::log(1 + fid_.p.exec_cv * fid_.p.exec_cv));
    c.cycles *= std::exp(s * rng_[2 + engine].normal() - 0.5 * s * s);
  }
  int32_t x = alloc_exec();
  Exec& e = execs_[x];
  e.job = j.id;
  e.op = it.op;
  e.task = j.task;
  e.engine = engine;
  e.level = (engine == E_RT) ? 0 : level;
  e.units = units;
  e.unit_mask = it.unit_mask;
  e.domain = units_[units[0]].domain;
  e.k1 = it.k1;
  e.k2 = it.k2;
  e.qos = tasks_[j.task].qos;
  e.cycles_total = c.cycles;
  e.nsteps = (e.level == 0) ? 1 : std::max(1, c.nsteps);
  e.depth = std::max(1, std::min(c.depth, e.nsteps));
  e.chunk = c.chunk;
  e.cyc_per_step = c.cycles / e.nsteps;
  e.act = c.act;
  e.exec_cat = c.exec_cat;
  e.util = c.util;
  e.start_ps = now_;
  o.resident_out = c.resident_out;
  // Streams (buffer manager addresses).
  const TaskDef& td = tasks_[j.task];
  uint64_t abase = pl.a_base + static_cast<uint64_t>(j.parity) * (1ull << 34);
  const SysCache& cache = cache_;
  const ModelParams& p = fid_.p;
  e.rd_w.base = pl.w_base + static_cast<uint64_t>(po.w_off);
  e.rd_w.size = po.def >= 0 ? std::max(64.0, td.ops[po.def].bytes_w) : 64.0;
  e.rd_w.total = c.rd_w;
  e.rd_w.alloc = e.rd_w.size <= p.stream_bypass_bytes;  // params.stream_bypass_bytes
  e.rd_w.l0_hit = hit_hint(cache, p, e.qos, e.rd_w.size);
  // Inputs: the first predecessor's output buffer; first ops read the task's
  // input buffer, or (zero-copy transport) the producer task's output.
  int32_t pred = -1;
  for (int32_t pi : po.preds)
    if (pl.ops[pi].def >= 0) { pred = pi; break; }
  if (pred >= 0) {
    e.rd_in.base = abase + static_cast<uint64_t>(pl.ops[pred].out_off);
    e.rd_in.size = std::max(64.0, td.ops[pl.ops[pred].def].bytes_out);
  } else if (pl.input_task >= 0 && mw_.transport == TR_ZERO_COPY) {
    const TaskPlan& pp = plans_[pl.input_task];
    const PlanOp& last = pp.ops[pp.ops.size() - 2];
    e.rd_in.base = pp.a_base + static_cast<uint64_t>(last.out_off);
    e.rd_in.size = std::max(64.0, last.def >= 0 ? tasks_[pl.input_task].ops[last.def].bytes_out : 64.0);
  } else {
    e.rd_in.base = pl.in_base;
    e.rd_in.size = std::max(64.0, po.def >= 0 ? td.ops[po.def].bytes_in : pl.rx_bytes);
  }
  e.rd_in.total = c.rd_in;
  e.rd_in.alloc = e.rd_in.size <= p.stream_bypass_bytes;
  e.rd_in.l0_hit = hit_hint(cache, p, e.qos, e.rd_in.size);
  e.wr.base = abase + static_cast<uint64_t>(po.def >= 0 ? po.out_off : 0) + (po.def == -2 ? (1ull << 33) : 0);
  e.wr.size = std::max(64.0, c.wr);
  e.wr.total = c.wr;
  e.wr.alloc = c.wr <= p.stream_bypass_bytes;
  e.wr.l0_hit = hit_hint(cache, p, e.qos, c.wr);
  e.slots.assign(static_cast<size_t>(e.depth), StepSlot{});
  // Setup: engine overhead + context switch (CPU) + NPU save/restore.
  ps_t setup = c.setup_ps;
  int setup_cat = engine == E_DMA ? A_DMA : A_COMPUTE;
  if (engine == E_CPU && units_[units[0]].last_job != j.id) {
    setup += ns_to_ps(soc_.cpu[units_[units[0]].cluster].ctx_ns);  // blocks.cpu_*.context_switch_ns
    setup_cat = by_preemption ? A_PREEMPTION : A_RUNQUEUE;
  }
  if (engine == E_NPU) {
    ps_t half = us_to_ps(soc_.npu.preempt_us / 2.0);  // blocks.npu.preempt_save_restore_us (save + restore)
    for (int32_t u : units) {
      int64_t lj = units_[u].last_job;
      if (lj >= 0 && lj != j.id) {
        Job* other = job_ptr(lj);
        if (other && other->remaining > 0) {
          setup += half;
          setup_cat = A_PREEMPTION;
          other->npu_restore = true;
          break;
        }
      }
    }
    if (j.npu_restore) {
      setup += half;
      setup_cat = A_PREEMPTION;
      j.npu_restore = false;
    }
  }
  for (int32_t u : units) {
    units_[u].exec = x;
    if (units_[u].reserved_job == j.id) units_[u].reserved_job = -1;
    unit_busy(u, true);
  }
  // L0 memory: roofline tail with fluid load on NoC and DRAM.
  if (e.level == 0 || engine == E_RT) {
    e.l0mem = true;
    e.slots[0].issued = true;
    e.slots[0].ready = now_;
    double reads[3] = {c.rd_w, c.rd_in, 0};
    float hits[3] = {e.rd_w.l0_hit, e.rd_in.l0_hit, e.wr.l0_hit};
    double total = c.rd_w + c.rd_in + c.wr;
    double dram_b = reads[0] * (1 - hits[0]) + reads[1] * (1 - hits[1]) + c.wr * (1 - hits[2]);
    if (total > 0) {
      ps_t t0 = now_;
      XY src = units_[units[0]].node;
      double link_bpp = noc_.bytes_per_ps() * std::max<size_t>(1, units.size());
      double rho_n = noc_.path_util(soc_.cache.nodes[0], src, t0);
      double noc_t = total / (link_bpp * (1 - rho_n));
      if (c.node_bw_bytes_per_ps > 0) noc_t = std::max(noc_t, total / c.node_bw_bytes_per_ps);
      double dram_t = 0;
      if (dram_b > 0) {
        double rho = dram_.avg_util(t0);
        double bw = dram_.peak_bytes_per_ps() * p.dram_l0_eff * (1 - rho);  // params.dram_l0_eff
        double xfer4k = 4096.0 / (dram_.channel_bytes_per_ps() * p.dram_l0_eff);
        dram_t = dram_b / bw + static_cast<double>(dram_.base_latency_ps()) + rho / (2 * (1 - rho)) * xfer4k;
      }
      e.mem_time = static_cast<ps_t>(std::max(noc_t, dram_t) + static_cast<double>(noc_.hop_ps()) * 4);
      e.mem_noc_frac = noc_t >= dram_t ? 1.0 : 0.0;  // the binding resource gets the tail
      double hops = 0;
      for (const XY& cn : soc_.cache.nodes) hops += noc_.hops(src, cn);
      hops = std::max(1.0, hops / soc_.cache.nodes.size());
      e.act[K_DRAM_BYTES] += dram_b;
      e.act[K_DRAM_ACTIVATES] += 0.5 * dram_b / soc_.dram.row_bytes;  // L0: 50% row-hit assumption
      e.act[K_CACHE_BYTES] += total;
      e.act[K_FLIT_HOPS] += total / soc_.flit_bytes * hops;
      ps_t comp = cycles_to_ps(c.cycles, unit_hz(units[0]));
      l0_register_fluid(e, dram_b, total, std::max(comp, e.mem_time));
    }
  }
  // Setup work (context switch, save/restore, launch) cannot run while the
  // unit's power domain is stalled by a DVFS transition.
  ps_t stall_wait = std::max<ps_t>(0, dom_[e.domain].stall_until - now_);
  if (setup > 0 && stall_wait > 0) {
    e.cat[A_DVFS] += stall_wait;
    setup += stall_wait;
  }
  e.est_end = now_ + setup + cycles_to_ps(c.cycles, unit_hz(units[0]));
  o.exec = x;
  if (setup > 0) {
    e.in_setup = true;
    e.setup_ps = setup;
    e.setup_cat = setup_cat;
    e.cat[setup_cat] += setup - (stall_wait > 0 ? stall_wait : 0);
    e.gen++;
    q_.push(now_ + setup, CMP_EXEC, EV_SETUP_DONE, x, e.uid, e.gen);
  } else {
    exec_begin(x);
  }
}

void Sim::complete_op(Job& j, int32_t op, ps_t end) {
  JobOp& o = j.ops[op];
  o.state = 4;
  o.end = end;
  o.frac = 1;
  // Only the call that completes the last op finishes the job (successor
  // completion can recurse into this function for zero-cost ops).
  int32_t remaining_after = --j.remaining;
  const TaskPlan& pl = plans_[j.task];
  for (int32_t s : pl.ops[op].succs) {
    JobOp& so = j.ops[s];
    if (--so.deps_left == 0) op_deps_done(j, s);
  }
  if (remaining_after == 0) finish_job(j);
}

void Sim::finish_job(Job& j) {
  const TaskPlan& pl = plans_[j.task];
  const TaskDef& td = tasks_[j.task];
  int32_t last = static_cast<int32_t>(pl.ops.size()) - 1;
  j.finish = j.ops[last].end;
  Completion c;
  c.job_id = j.id;
  c.tag = j.tag;
  c.task_id = td.id;
  c.release = j.release;
  c.start = j.start >= 0 ? j.start : j.finish;
  c.finish = j.finish;
  c.deadline = j.deadline;
  c.response = j.finish - j.release;
  c.missed = j.finish > j.deadline;
  // Critical path: walk back from the final op through the predecessor that
  // determined each op's ready time. Every interval on the path is assigned
  // to exactly one category, so the attribution sums to the response time.
  std::vector<CritEntry> rev;
  int32_t cur = last;
  while (cur >= 0) {
    const JobOp& o = j.ops[cur];
    for (int k = 0; k < N_CAT; ++k) c.attribution[k] += o.cat[k];
    c.attribution[o.wait_cat] += o.wait;
    const PlanOp& po = pl.ops[cur];
    if (po.def >= 0 || o.end > o.start || o.wait > 0) {
      CritEntry ce;
      ce.op_id = po.def >= 0 ? td.ops[po.def].id : (po.def == -1 ? "__rx" : "__tx");
      ce.engine = o.engine < 0 ? "none" : (o.engine == E_CPU ? (o.block == TB_CPU_B ? "cpu_b" : "cpu_a") : engine_name(o.engine));
      ce.start = o.start;
      ce.end = o.end;
      ce.wait = o.wait;
      ce.wait_cause = o.wait > 0 ? cat_name(o.wait_cat) : "";
      ce.gap = o.gap;
      ce.gap_cause = o.gap_cause;
      ce.unit = o.unit;
      rev.push_back(ce);
    }
    if (o.crit_pred >= 0) {
      c.attribution[o.gap_cat] += o.gap;
      cur = o.crit_pred;
    } else {
      c.attribution[A_MIDDLEWARE] += o.ready - j.release;
      cur = -1;
    }
  }
  ps_t sum = 0;
  for (ps_t v : c.attribution) sum += v;
  if (sum != c.response) c.attribution[A_COMPUTE] += c.response - sum;  // rounding residue
  c.critical_path.assign(rev.rbegin(), rev.rend());
  TraceRec r;
  r.t = j.finish;
  r.dur = c.response;
  r.kind = TK_JOB_FINISH;
  r.block = TB_RUNTIME;
  r.job = j.id;
  r.task = j.task;
  r.value = static_cast<double>(j.deadline);
  emit(r);
  if (c.missed) {
    r.kind = TK_JOB_MISS;
    r.value = static_cast<double>(j.finish - j.deadline);
    emit(r);
  }
  done_.push_back(std::move(c));
  active_count_[j.task]--;
  auto it = job_slot_.find(j.id);
  if (it != job_slot_.end()) {
    free_jobs_.push_back(it->second);
    job_slot_.erase(it);
  }
  j.ops.clear();
  j.id = -1;
}

}  // namespace gc
