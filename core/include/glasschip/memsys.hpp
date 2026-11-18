// Memory-system components: the on-chip network (NoC), the shared system
// cache and the DRAM controllers. They exchange MemTxn records that carry
// their own timestamps; no component reads another component's state.
#pragma once
#include <array>
#include <cstdint>
#include <deque>
#include <vector>

#include "config.hpp"
#include "kernel.hpp"
#include "serial.hpp"

namespace gc {

// A timestamped memory transaction (one chunk of a bulk stream or one CPU
// miss). Latency components are accumulated as it moves along the path so
// that the requester can attribute stalls to noc / cache / dram.
struct MemTxn {
  ps_t issue_ps = 0;   // requester issue time
  ps_t t = 0;          // time at the current point of the path
  uint64_t addr = 0;
  int32_t bytes = 0;
  int8_t qos = Q_REALTIME;
  bool write = false;
  bool alloc = true;   // allocate in the system cache (false = streaming bypass)
  XY src{};            // requester NoC node
  int32_t exec = -1;   // requester exec context (-1 = posted write / writeback)
  uint32_t exec_uid = 0;
  int16_t slot = 0;
  int32_t task = -1, op = -1;
  int64_t job = -1;
  float l0_hit = 0;    // hit probability used when the cache runs at L0
  ps_t noc_ps = 0, cache_ps = 0, dram_ps = 0;
  int32_t channel = 0;
  GC_IO(issue_ps, t, addr, bytes, qos, write, alloc, src, exec, exec_uid, slot, task, op, job, l0_hit, noc_ps, cache_ps, dram_ps, channel)
};

// Pluggable back-ends for later L2 integrations (BookSim 2, Ramulator 2).
// Not used by L0/L1. A backend receives transactions and reports completion
// times; the Sim falls back to L1 (with a warning in info()) when none is set.
class NocBackend {
 public:
  virtual ~NocBackend() = default;
  // Returns the tail arrival time of `bytes` sent from a to b at time t.
  virtual ps_t send(XY a, XY b, int bytes, int qos, ps_t t) = 0;
  virtual const char* name() const = 0;
};
class DramBackend {
 public:
  virtual ~DramBackend() = default;
  // Enqueue a request arriving at t; the backend must later report completion.
  virtual void enqueue(const MemTxn& txn, ps_t t) = 0;
  // Advance to time t and append (txn, completion time) pairs to `done`.
  virtual void tick(ps_t t, std::vector<std::pair<MemTxn, ps_t>>& done) = 0;
  virtual const char* name() const = 0;
};

// ----------------------------------------------------------------------------
// NoC: 2D mesh with XY routing. Links are directional; each node also has an
// ejection port that models contention at the destination.
struct NocLink {
  std::array<ps_t, N_QOS> free_at{};  // per-QoS-class reservation timelines
  ps_t busy_ps = 0;                   // total serialization time reserved
  uint64_t bytes = 0;
  double bg_rate = 0;                 // fluid load (bytes/ps) from L0 sources
  ps_t win_start = 0;
  uint64_t win_bytes = 0, prev_win_bytes = 0;
  GC_IO(free_at, busy_ps, bytes, bg_rate, win_start, win_bytes, prev_win_bytes)
};

class Noc {
 public:
  void init(const SocCfg& soc, const ModelParams& p, int level);
  void set_freq(double hz) { freq_ = hz; }
  double freq() const { return freq_; }
  void set_qos(bool on);
  bool qos() const { return qos_on_; }
  int hops(XY a, XY b) const { return std::abs(a.x - b.x) + std::abs(a.y - b.y); }
  // Per-hop latency (router + link) at the current NoC frequency.
  ps_t hop_ps() const;
  // Serialization time of `bytes` on one link at the current frequency.
  ps_t ser_ps(int bytes) const;
  // Sends `bytes` from a to b starting at time t (L1: per-link reservation
  // with QoS arbitration and backpressure; L0: zero-load + M/D/1 queueing).
  // Returns the tail arrival time; adds flit-hops to *flit_hops.
  ps_t send(XY a, XY b, int bytes, int qos, ps_t t, uint64_t* flit_hops);
  // Zero-load latency of a small header message (no link reservation).
  ps_t header_latency(XY a, XY b, int bytes, uint64_t* flit_hops) const;
  // Fluid (L0) load registration along the XY path a->b, in bytes per ps.
  void add_fluid(XY a, XY b, double rate, std::vector<int32_t>* touched);
  void remove_fluid(const std::vector<int32_t>& links, double rate);
  // Utilization estimate on the path (window + fluid), used by L0 formulas.
  double path_util(XY a, XY b, ps_t t);
  int level() const { return level_; }
  int n_links() const { return static_cast<int>(links_.size()); }
  const NocLink& link(int i) const { return links_[i]; }
  // Human-readable "x,y->x,y" for link i.
  std::string link_name(int i) const;
  double bytes_per_ps() const { return link_bytes_ * freq_ / 1e12; }
  ps_t total_busy_ps() const;

  GC_IO(dimx_, dimy_, link_bytes_, router_cycles_, link_cycles_, flit_bytes_, max_packet_, freq_, qos_on_, level_, buf_flits_,
        window_ps_, links_)

 private:
  int idx(int x, int y, int dir) const { return (y * dimx_ + x) * 5 + dir; }  // dir: 0 +x, 1 -x, 2 +y, 3 -y, 4 eject
  void path(XY a, XY b, int* out, int* n) const;
  void touch_window(NocLink& l, ps_t t, int bytes);
  double link_util(const NocLink& l, ps_t t) const;

  int dimx_ = 4, dimy_ = 4, link_bytes_ = 32, router_cycles_ = 2, link_cycles_ = 1, flit_bytes_ = 32, max_packet_ = 256;
  double freq_ = 1.5e9;
  bool qos_on_ = false;
  int level_ = 1;
  double buf_flits_ = 8;
  ps_t window_ps_ = 10 * PS_PER_US;
  std::vector<NocLink> links_;
};

// ----------------------------------------------------------------------------
// System cache: set-associative tags at `cache_block_bytes` granularity, LRU
// replacement, way partitioning per QoS class, no-allocate for streams.
class SysCache {
 public:
  void init(const SocCfg& soc, const ModelParams& p, int level);
  // Restricts allocation of each QoS class to its ways (nullptr disables).
  void set_partition(const std::array<int, N_QOS>* ways_by_qos);
  bool partitioned() const { return part_on_; }
  // Looks up [addr, addr+bytes). Returns bytes that hit. Allocates misses if
  // `alloc`. Dirty victims are reported in *wb (address list of block size).
  int access(uint64_t addr, int bytes, int qos, bool write, bool alloc, float l0_hit, uint64_t salt,
             std::vector<uint64_t>* wb);
  int block() const { return block_; }
  int level() const { return level_; }
  // Capacity (bytes) a QoS class can allocate into.
  double class_capacity(int qos) const;
  uint64_t hits = 0, misses = 0;
  GC_IO(sets_, ways_, block_, level_, size_bytes_, part_on_, way_mask_, tags_, lru_, dirty_, clock_, hits, misses)

 private:
  int sets_ = 1, ways_ = 16, block_ = 1024, level_ = 1;
  double size_bytes_ = 8 << 20;
  bool part_on_ = false;
  std::array<uint32_t, N_QOS> way_mask_{};
  std::vector<uint64_t> tags_;
  std::vector<uint32_t> lru_;
  std::vector<uint8_t> dirty_;
  uint32_t clock_ = 0;
};

// ----------------------------------------------------------------------------
// DRAM: channels with bank-group/bank state, open-page policy, FR-FCFS
// within a bounded queue window, refresh, read/write turnaround.
struct DramReq {
  int32_t txn = -1;  // index into the Sim transaction pool
  ps_t arrive = 0;
  uint64_t seq = 0;
  int32_t bank = 0;
  int64_t row = 0;
  bool write = false;
  uint64_t addr = 0;
  int32_t bytes = 0;
  int8_t qos = 0;
  GC_IO(txn, arrive, seq, bank, row, write, addr, bytes, qos)
};
struct DramBank {
  int64_t open_row = -1;
  ps_t act_ps = -PS_NEVER;
  ps_t ready_ps = 0;
  GC_IO(open_row, act_ps, ready_ps)
};
struct DramChannel {
  std::vector<DramBank> banks;
  std::array<std::deque<DramReq>, N_QOS> q;
  ps_t bus_free = 0;
  bool last_write = false;
  ps_t next_refresh = 0;
  bool decide_pending = false;
  ps_t decide_at = 0;                 // time of the authoritative scheduler wakeup
  int64_t decide_token = 0;           // only the wakeup carrying this token is acted on
  std::vector<DramReq> pending;       // min-heap by (arrive, seq): requests still in flight to the controller
  ps_t busy_ps = 0;
  uint64_t bytes = 0, row_hits = 0, row_empty = 0, row_conflicts = 0, acts = 0, refreshes = 0;
  double bg_rate = 0;
  ps_t win_start = 0;
  uint64_t win_bytes = 0, prev_win_bytes = 0;
  int qlen = 0;
  GC_IO(banks, q, bus_free, last_write, next_refresh, decide_pending, decide_at, decide_token, pending, busy_ps, bytes, row_hits, row_empty, row_conflicts, acts,
        refreshes, bg_rate, win_start, win_bytes, prev_win_bytes, qlen)
};

struct DramDecision {
  bool issued = false;
  int32_t txn = -1;
  ps_t data_end = 0;
  ps_t next_decide = 0;
  bool idle = false;  // nothing queued and nothing pending
  int acts = 0;
  bool more = false;
};

class Dram {
 public:
  void init(const SocCfg& soc, const ModelParams& p, int level, Rng& rng);
  int n_channels() const { return static_cast<int>(ch_.size()); }
  int channel_of(uint64_t addr) const;
  int controller_of(int ch) const { return ch / ch_per_ctrl_; }
  void map(uint64_t addr, int* bank, int64_t* row) const;
  // L1: queue a request (arrived at time t).
  void enqueue(int ch, const DramReq& r);
  // L1: register a request that will reach the controller at r.arrive.
  void add_pending(int ch, const DramReq& r);
  // Moves pending requests that have arrived by `now` into the scheduler queues.
  void admit(int ch, ps_t now, bool qos_on);
  // Earliest pending arrival (PS_NEVER if none).
  ps_t next_arrival(int ch) const;
  // L1: FR-FCFS decision at time now. Picks one request, computes its bank
  // and bus timing, and says when the next decision should happen.
  DramDecision decide(int ch, ps_t now, bool qos_on);
  // L0: analytic latency for `bytes` arriving at t on channel ch.
  ps_t l0_latency(int ch, int bytes, ps_t t, ps_t* queue_ps);
  // Peak bandwidth in bytes per ps (all channels).
  double peak_bytes_per_ps() const { return peak_bpp_; }
  double channel_bytes_per_ps() const { return peak_bpp_ / ch_.size(); }
  double util(int ch, ps_t t) const;
  double avg_util(ps_t t) const;
  void add_fluid(double rate_total);
  void requeue_for_qos(bool qos_on);
  const DramChannel& channel(int i) const { return ch_[i]; }
  DramChannel& channel_mut(int i) { return ch_[i]; }
  int level() const { return level_; }
  ps_t base_latency_ps() const;
  int queue_len(int ch) const { return ch_[ch].qlen; }
  void note_bytes(int ch, ps_t t, int bytes);
  GC_IO(level_, ch_per_ctrl_, nbanks_, row_bytes_, il_, qdepth_, il_sh_, nch_sh_, row_sh_, nb_sh_, peak_bpp_, tRCD_, tRP_, CL_, tRAS_, tRFC_, tREFI_, tWTR_, tRTW_,
        ctrl_ps_, window_ps_, l0_eff_, ch_)

 private:
  int level_ = 1, ch_per_ctrl_ = 2, nbanks_ = 32, row_bytes_ = 2048, il_ = 4096, qdepth_ = 32;
  // log2 of power-of-two geometry (-1 if not a power of two) for fast mapping
  int il_sh_ = -1, nch_sh_ = -1, row_sh_ = -1, nb_sh_ = -1;
  double peak_bpp_ = 0.1365;
  ps_t tRCD_ = 0, tRP_ = 0, CL_ = 0, tRAS_ = 0, tRFC_ = 0, tREFI_ = 0, tWTR_ = 0, tRTW_ = 0, ctrl_ps_ = 0, window_ps_ = 0;
  double l0_eff_ = 0.8;
  std::vector<DramChannel> ch_;
};

}  // namespace gc
