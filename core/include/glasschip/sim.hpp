// glasschip-core simulator: discrete-event SoC model with per-component
// fidelity levels, a runtime/scheduler model and an event tracer.
#pragma once
#include <array>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include "config.hpp"
#include "kernel.hpp"
#include "memsys.hpp"
#include "serial.hpp"

namespace gc {

// Component ids used to dispatch queued events (Event::comp).
enum Comp : uint16_t { CMP_RUNTIME = 0, CMP_EXEC = 1, CMP_DRAM = 2 };
enum EvKind : uint16_t {
  EV_JOB_RELEASE = 0,
  EV_OP_READY = 1,
  EV_SETUP_DONE = 10,
  EV_COMPUTE_END = 11,
  EV_DATA_READY = 12,
  EV_MEM_DONE = 13,
  EV_DRAM_ARRIVE = 20,
  EV_DRAM_DECIDE = 21,
};

// Trace event kinds (public names in trace dicts).
enum TraceKind : uint8_t { TK_JOB_RELEASE, TK_JOB_START, TK_JOB_FINISH, TK_JOB_MISS, TK_OP, TK_XFER, TK_DRAM_Q, TK_FREQ, TK_PREEMPT, N_TK };
// Trace block ids (public names in trace dicts).
enum TraceBlock : uint8_t { TB_CPU_A, TB_CPU_B, TB_RT, TB_DSP, TB_NPU, TB_SIMT, TB_SYS_CACHE, TB_DRAM, TB_DMA, TB_NOC, TB_NOC_CACHE, TB_DRAM_IO, TB_RUNTIME, N_TB };
const char* trace_kind_name(int k);
const char* trace_block_name(int b);

// One fixed-size trace record (56 bytes on disk in .gct files). For op
// records the node fields hold a 32-bit mask of the units (tiles, CUs) used.
struct TraceRec {
  ps_t t = 0, dur = 0;
  int64_t job = -1;
  int64_t bytes = 0;
  double value = 0;
  int32_t task = -1, op = -1;
  uint8_t kind = 0, block = 0;
  uint16_t unit = 0;
  int8_t sx = -1, sy = -1, dx = -1, dy = -1;
};

// A hardware execution unit: one CPU core, RT core (pair), NPU tile, SIMT
// compute unit, DSP unit or DMA engine.
struct Unit {
  int engine = 0, domain = 0, index = 0, cluster = 0, block = 0;
  XY node{};
  int32_t exec = -1;          // running exec (-1 idle)
  int64_t last_job = -1;      // last job that ran here (context switch / NPU save-restore)
  int64_t reserved_job = -1;  // NPU: job holding the tile until its next preemption boundary
  ps_t busy_since = 0;
  ps_t busy_acc = 0;          // accumulated busy time since the counters window started
  GC_IO(engine, domain, index, cluster, block, node, exec, last_job, reserved_job, busy_since, busy_acc)
};

// An op waiting for (or preempted from) an engine.
struct ReadyItem {
  int64_t k1 = 0, k2 = 0;   // scheduler priority key (lower runs first)
  uint64_t seq = 0;
  int64_t job = -1;
  int32_t op = -1;          // plan op index within the job
  int32_t exec = -1;        // >= 0 when this is a suspended exec to resume
  uint64_t unit_mask = ~0ull;
  ps_t ready = 0;
  GC_IO(k1, k2, seq, job, op, exec, unit_mask, ready)
};

struct EngineState {
  int kind = 0;
  int level = 0;
  int policy = SP_FIFO;
  bool preemptive = false;  // CPU and RT preempt at any point (L0) or step boundary (L1)
  std::vector<int32_t> units;
  std::vector<ReadyItem> ready;
  GC_IO(kind, level, policy, preemptive, units, ready)
};

// Per-step buffer slot in the L1 streaming pipeline (double buffering).
struct StepSlot {
  int32_t outstanding = 0;
  ps_t ready = 0;
  bool issued = false;
  std::array<ps_t, 3> crit{};  // latency components (noc, cache, dram) of the last-arriving chunk
  ps_t crit_issue = 0;
  GC_IO(outstanding, ready, issued, crit, crit_issue)
};

// Stream of addresses read or written by an op.
struct Stream {
  uint64_t base = 0;
  double size = 0;        // region size (addresses wrap inside it)
  double total = 0;       // total bytes moved over the whole op
  double cursor = 0;      // bytes already moved
  bool alloc = true;      // allocate in system cache
  float l0_hit = 0;       // hit fraction when the cache is L0
  GC_IO(base, size, total, cursor, alloc, l0_hit)
};

// An op in execution on one or more units.
struct Exec {
  uint32_t uid = 0;
  bool live = false;
  int64_t job = -1;
  int32_t op = -1;
  int engine = 0, domain = 0, level = 0;
  std::vector<int32_t> units;
  int64_t k1 = 0, k2 = 0;  // priority key (for preemption decisions)
  int qos = Q_REALTIME;
  int task = -1;
  // pipeline plan
  int32_t nsteps = 1, depth = 1, chunk = 4096;
  double cycles_total = 0, cyc_per_step = 0;
  Stream rd_w, rd_in, wr;
  int32_t next_issue = 0, next_compute = 0;
  std::vector<StepSlot> slots;
  // compute segment
  bool computing = false;
  ps_t seg_start = 0, seg_last = 0;
  double seg_rem = 0, seg_total = 0;
  ps_t seg_dvfs = 0;
  uint32_t gen = 0;
  bool in_setup = false;
  ps_t setup_ps = 0;
  int setup_cat = A_COMPUTE;
  bool waiting = false;
  ps_t wait_start = 0;
  // L0 memory tail
  bool l0mem = false;
  ps_t mem_time = 0, mem_done = 0;
  double mem_noc_frac = 0;
  bool in_tail = false;
  double fluid_dram = 0, fluid_noc = 0;
  std::vector<int32_t> fluid_links;
  // preemption
  bool suspended = false, preempt_req = false;
  ps_t suspended_at = 0;
  // accounting
  std::array<ps_t, N_CAT> cat{};
  std::array<double, N_ACT> act{};
  double act_credited = 0;  // fraction of act already credited to counters
  ps_t start_ps = 0, est_end = 0;
  double util = 0;
  int exec_cat = A_COMPUTE;  // category for compute time (DMA engine ops -> dma, middleware -> middleware)
  bool partial = false;      // suspended mid-segment; seg_rem cycles remain
  bool begun = false;        // setup finished at least once
  uint64_t unit_mask = ~0ull;
  GC_IO(partial, begun, unit_mask, uid, live, job, op, engine, domain, level, units, k1, k2, qos, task, nsteps, depth, chunk, cycles_total, cyc_per_step, rd_w,
        rd_in, wr, next_issue, next_compute, slots, computing, seg_start, seg_last, seg_rem, seg_total, seg_dvfs, gen, in_setup,
        setup_ps, setup_cat, waiting, wait_start, l0mem, mem_time, mem_done, mem_noc_frac, in_tail, fluid_dram, fluid_noc,
        fluid_links, suspended, preempt_req, suspended_at, cat, act, act_credited, start_ps, est_end, util, exec_cat)
};

// Static plan of a task: op 0 is the synthetic middleware receive (rx), ops
// 1..n are the graph ops, op n+1 is the synthetic publish (tx).
struct PlanOp {
  int32_t def = -1;  // index into TaskDef::ops, -1 = rx, -2 = tx
  std::vector<int32_t> preds, succs;
  bool npu_only = false;
  double out_off = 0;  // offset of this op's output buffer inside the activation region
  double w_off = 0;    // offset of this op's weights inside the task weight region
  GC_IO(def, preds, succs, npu_only, out_off, w_off)
};
struct TaskPlan {
  std::vector<PlanOp> ops;
  double act_region = 0;
  uint64_t w_base = 0, a_base = 0, in_base = 0;
  double rx_bytes = 0, tx_bytes = 0;
  int n_rx = 0, n_tx = 0;
  int input_task = -1;     // producer task whose output this task reads (zero-copy)
  int mw_engine = -1;      // engine for rx/tx (-1 = no middleware cost)
  uint64_t mw_mask = ~0ull;
  int home_cpu_cluster = -1;
  GC_IO(ops, act_region, w_base, a_base, in_base, rx_bytes, tx_bytes, n_rx, n_tx, input_task, mw_engine, mw_mask, home_cpu_cluster)
};

struct JobOp {
  int8_t state = 0;  // 0 waiting deps, 1 ready event pending, 2 queued, 3 running, 4 done
  int32_t deps_left = 0;
  ps_t ready = 0, start = -1, end = -1;
  int engine = -1, unit = -1, nunits = 0, block = 0;
  int32_t crit_pred = -1;
  ps_t gap = 0;
  int8_t gap_cat = A_MIDDLEWARE;
  std::string gap_cause;
  ps_t wait = 0;
  int8_t wait_cat = A_RUNQUEUE;
  std::array<ps_t, N_CAT> cat{};
  int32_t exec = -1;
  double est_cycles = 0;
  int est_domain = -1;
  double frac = 0;
  bool resident_out = false;  // NPU output kept in shared SRAM
  GC_IO(state, deps_left, ready, start, end, engine, unit, nunits, block, crit_pred, gap, gap_cat, gap_cause, wait, wait_cat, cat, exec,
        est_cycles, est_domain, frac, resident_out)
};

struct Job {
  int64_t id = -1;
  int32_t task = -1;
  int64_t tag = 0;
  ps_t release = 0, deadline = 0, start = -1, finish = -1;
  int32_t remaining = 0;
  bool released = false;
  bool npu_restore = false;  // pays NPU context restore on its next NPU op
  int parity = 0;
  std::vector<JobOp> ops;
  GC_IO(id, task, tag, release, deadline, start, finish, remaining, released, npu_restore, parity, ops)
};

// Completed-job record returned by run_until().
struct CritEntry {
  std::string op_id, engine, wait_cause, gap_cause;
  ps_t start = 0, end = 0, wait = 0, gap = 0;
  int unit = -1;
};
struct Completion {
  int64_t job_id = -1, tag = 0;
  std::string task_id;
  ps_t release = 0, start = 0, finish = 0, deadline = 0, response = 0;
  bool missed = false;
  std::array<ps_t, N_CAT> attribution{};
  std::vector<CritEntry> critical_path;
};

struct DomainState {
  double hz = 0, nominal = 0;
  ps_t stall_until = 0;
  std::array<double, N_ACT> act{};
  ps_t unit_busy = 0;
  int units = 0;
  uint64_t transitions = 0;
  ps_t stall_total = 0;
  GC_IO(hz, nominal, stall_until, act, unit_busy, units, transitions, stall_total)
};

struct MiddlewareCfg {
  int transport = TR_NONE;
  int executor = EX_SINGLE;
  // per transport: serialize/deserialize ns per KB, fixed ns per message, copies
  double ser_ns_kb = 0, deser_ns_kb = 0, fixed_ns = 0, copies = 0, copy_ns_kb = 0;
  double dispatch_ns = 0;
  GC_IO(transport, executor, ser_ns_kb, deser_ns_kb, fixed_ns, copies, copy_ns_kb, dispatch_ns)
};

// Cost of one op on one engine at one fidelity level (engines.cpp).
struct OpCost {
  double cycles = 0;     // compute cycles on the allocated units
  ps_t setup_ps = 0;     // fixed engine overhead (launch, descriptors)
  double rd_w = 0, rd_in = 0, wr = 0;  // bytes through the memory system
  double sram = 0;       // on-NPU SRAM traffic
  int nsteps = 1, chunk = 4096, depth = 2;
  std::array<double, N_ACT> act{};
  double util = 0;
  int exec_cat = A_COMPUTE;
  bool l2 = false, l2_fallback = false;
  bool resident_in = false, resident_out = false;
  double node_bw_bytes_per_ps = 0;  // L0 per-requester bandwidth cap (0 = NoC eject)
};

// Expected system-cache hit fraction of a stream (L0 hit-rate model).
float hit_hint(const SysCache& c, const ModelParams& p, int qos, double bytes);

class Sim {
 public:
  Sim(const Json& soc, const Json& fidelity, uint64_t seed);
  ~Sim();
  Sim(const Sim&) = delete;
  Sim& operator=(const Sim&) = delete;

  // ---- configuration
  // Adds a task graph (schema workloads/schema/taskgraph.schema.json).
  void add_task(const Json& graph);
  // ROS 2-style middleware: params from workloads/graphs/middleware.json,
  // transport "copy" | "zero_copy", executor "single_threaded" | "multi_threaded" | "events".
  void set_middleware(const Json& params, const std::string& transport, const std::string& executor);
  void set_scheduler(const std::string& engine, const std::string& policy);
  void set_qos(bool enabled);
  // Ways per QoS class, e.g. {"safety":2,"realtime":10,"best_effort":4}; empty disables.
  void set_cache_partition(const std::map<std::string, int>* ways_by_qos);
  void set_mapping(const std::string& policy);  // "static" | "eft"
  void set_task_priority(const std::string& task_id, int64_t priority);
  // Extra model parameters (same keys as fidelity `params:`).
  void set_params(const Json& params);
  // Per-op tables: {"npu_cycles": {"task/op": cycles}, "cpu_cpi": {"op_id or type": cpi}}.
  void set_l2_tables(const Json& tables);

  // ---- running
  // Schedules a job of `task_id` released at t_ps. Returns the job id, or -1
  // if the task uses on_overrun=skip and a previous job is still active.
  int64_t release_job(const std::string& task_id, ps_t t_ps, int64_t tag);
  // Processes all events up to and including t_ps; returns completed jobs.
  std::vector<Completion> run_until(ps_t t_ps);
  ps_t now() const { return now_; }

  // ---- DVFS / observation
  void set_domain_freq(const std::string& domain, double hz);
  double domain_freq(const std::string& domain) const;
  struct CounterSet {
    ps_t busy = 0, idle = 0, unit_busy = 0;
    int units = 0;
    std::array<double, N_ACT> act{};
  };
  std::array<CounterSet, N_DOMAINS> counters(bool reset);
  struct PendingInfo {
    int64_t job_id;
    std::string task_id;
    ps_t release, deadline;
    bool started;
    double frac_done;
    std::array<double, N_DOMAINS> est_remaining_cycles;
  };
  std::vector<PendingInfo> pending_jobs();
  struct QueueDepths {
    std::vector<int> dram_ctrl, dram_ch;
    std::vector<std::pair<std::string, double>> noc_links;  // (name, backlog_ns), top N
    std::array<int, N_ENGINES> engine_ready{};
    std::array<int, N_ENGINES> engine_busy{};
  };
  QueueDepths queue_depths(int top_n = 8);

  // ---- tracing
  void trace_enable(bool on) { trace_on_ = on; }
  bool trace_enabled() const { return trace_on_; }
  std::vector<TraceRec> trace_drain();
  std::string trace_hash() const { return hash_.hex(); }
  uint64_t trace_count() const { return hash_.count; }
  bool trace_open(const std::string& path);  // .gct writer
  void trace_close();
  // String tables for decoding trace records.
  std::vector<std::string> task_names() const;
  std::vector<std::string> op_names(int task) const;  // plan order incl. __rx/__tx

  // ---- checkpoint
  std::vector<uint8_t> checkpoint();
  void restore(const std::vector<uint8_t>& blob);

  // ---- info
  Json info() const;
  uint64_t events_processed() const { return events_; }
  const SocCfg& soc() const { return soc_; }
  const FidelityCfg& fidelity() const { return fid_; }
  // Closed-form helpers exposed for tests.
  double npu_gemm_cycles(double M, double K, double N, double B, int tiles, int dtype) const;
  Noc& noc() { return noc_; }
  Dram& dram() { return dram_; }
  SysCache& cache() { return cache_; }
  int unit_count(int engine) const { return static_cast<int>(eng_[engine].units.size()); }

 private:
  friend struct SimIO;
  // event handlers
  void dispatch_event(const Event& e);
  void on_job_release(int64_t job_id);
  void on_op_ready(int64_t job_id, int32_t op);
  // runtime
  Job* job_ptr(int64_t id);
  void build_plan(int t);
  void rebuild_plans();
  int map_op(const Job& j, int32_t op, uint64_t* mask);
  void op_deps_done(Job& j, int32_t op);
  void enqueue_op(Job& j, int32_t op);
  void push_ready(int engine, const ReadyItem& it);
  void dispatch(int engine);
  bool try_preempt(int engine, const ReadyItem& it);
  void start_exec(int engine, const ReadyItem& it, const std::vector<int32_t>& units, bool by_preemption);
  void complete_op(Job& j, int32_t op, ps_t end);
  void finish_job(Job& j);
  void priority_key(const Job& j, int engine, int64_t* k1, int64_t* k2) const;
  int want_units(const Job& j, int32_t op, int engine) const;
  double est_cycles(const Job& j, int32_t op, int engine) const;
  // exec machine (engines.cpp)
  void compute_cost(const Job& j, int32_t op, int engine, int nunits, int level, OpCost& c) const;
  int32_t alloc_exec();
  void free_exec(int32_t x);
  void exec_begin(int32_t x);
  void exec_issue_step(int32_t x, int32_t step);
  void exec_issue_writes(int32_t x, double bytes);
  void exec_try_compute(int32_t x);
  void exec_compute_end(int32_t x);
  void exec_finish(int32_t x);
  void exec_suspend(int32_t x);
  void exec_resume(int32_t x, const std::vector<int32_t>& units, bool ctx_switch);
  void exec_credit(Exec& e);
  void exec_start_segment(int32_t x, double cycles);
  void exec_reschedule_segment(int32_t x);
  double exec_progress(const Exec& e) const;
  void l0_register_fluid(Exec& e, double dram_bytes, double noc_bytes, ps_t dur);
  void l0_unregister_fluid(Exec& e);
  // memory path (memsys glue)
  void mem_read(Exec& e, int32_t x, int slot, uint64_t addr, int bytes, bool alloc, float l0_hit);
  void mem_write(int32_t x, uint64_t addr, int bytes, bool alloc, float l0_hit, int qos, XY src, int64_t job, int task, int op);
  void mem_dram(MemTxn& txn, XY from);
  void mem_deliver(const MemTxn& txn, ps_t arrival);
  void on_dram_arrive(int32_t txn_idx);
  void on_dram_decide(int ch, int64_t token);
  int32_t alloc_txn(const MemTxn& t);
  XY cache_node(uint64_t addr) const;
  XY dram_node(int ch) const;
  // units / domains
  void unit_busy(int32_t u, bool busy);
  double unit_hz(int32_t u) const { return dom_[units_[u].domain].hz; }
  // trace
  void emit(const TraceRec& r);
  void emit_xfer(ps_t t, ps_t arrival, XY a, XY b, int bytes, int64_t job, int task, int op);

  // ---- state (everything below is checkpointed)
  SocCfg soc_;
  FidelityCfg fid_;
  uint64_t seed_ = 0;
  ps_t now_ = 0;
  EventQueue q_;
  std::vector<Rng> rng_;  // one per component stream
  std::vector<TaskDef> tasks_;
  std::vector<TaskPlan> plans_;
  std::map<std::string, int> task_index_;
  std::vector<Job> jobs_;  // slots
  std::vector<int32_t> free_jobs_;
  std::map<int64_t, int32_t> job_slot_;
  std::vector<int32_t> active_count_;  // per task: released-but-unfinished jobs
  std::vector<int64_t> job_seq_;       // per task: job counter (activation buffer parity)
  int64_t next_job_id_ = 1;
  std::vector<Unit> units_;
  std::array<EngineState, N_ENGINES> eng_;
  std::vector<Exec> execs_;
  std::vector<int32_t> free_execs_;
  uint32_t next_uid_ = 1;
  std::vector<MemTxn> txns_;
  std::vector<int32_t> free_txns_;
  uint64_t dram_seq_ = 0;
  Noc noc_;
  SysCache cache_;
  Dram dram_;
  std::array<DomainState, N_DOMAINS> dom_;
  ps_t counters_since_ = 0;
  ps_t noc_busy_base_ = 0, dram_busy_base_ = 0;
  MiddlewareCfg mw_;
  bool qos_on_ = false;
  uint64_t ready_seq_ = 0;
  std::map<std::string, double> npu_l2_;  // "task/op" or "op" -> cycles
  std::map<std::string, double> cpu_cpi_; // op_id or type name -> CPI
  uint64_t l2_fallbacks_npu_ = 0, l2_fallbacks_cpu_ = 0, l2_hits_npu_ = 0, l2_hits_cpu_ = 0;
  uint64_t events_ = 0;
  std::array<uint64_t, 32> ev_kind_{};  // events processed per EvKind (diagnostics)
  StreamHash hash_;
  std::vector<Completion> done_;  // completions since last run_until (not checkpointed: drained each call)
  std::vector<int> last_dram_q_;
  // not checkpointed
  bool trace_on_ = false;
  std::vector<TraceRec> trace_buf_;
  std::FILE* gct_ = nullptr;
  std::string gct_path_;
  std::vector<uint64_t> wb_scratch_;
};

}  // namespace gc
