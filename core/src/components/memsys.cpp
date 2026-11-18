// NoC, system cache and DRAM models. See memsys.hpp for the interfaces and
// configs/fidelity/README.md for the parameters.
#include "glasschip/memsys.hpp"

#include <algorithm>
#include <bit>
#include <cmath>

namespace gc {

// ============================================================================
// NoC
void Noc::init(const SocCfg& soc, const ModelParams& p, int level) {
  dimx_ = soc.dimx;                         // noc.dims
  dimy_ = soc.dimy;
  link_bytes_ = std::max(1, soc.link_bytes);  // noc.link_bits
  router_cycles_ = soc.router_cycles;       // noc.router_latency_cycles
  link_cycles_ = soc.link_cycles;           // noc.link_latency_cycles
  flit_bytes_ = std::max(1, soc.flit_bytes);  // noc.flit_bytes
  max_packet_ = std::max(flit_bytes_, soc.max_packet_bytes);  // noc.max_packet_bytes
  freq_ = soc.noc_freq;                     // noc.freq_hz (scaled by DVFS of noc_cache)
  level_ = level;
  buf_flits_ = p.noc_vc_buffer_flits * std::max(1, soc.vcs) / 4.0;  // params.noc_vc_buffer_flits, noc.virtual_channels
  window_ps_ = ns_to_ps(p.noc_window_ns);   // params.noc_util_window_ns
  links_.assign(static_cast<size_t>(dimx_ * dimy_ * 6), NocLink{});
}

void Noc::set_qos(bool on) { qos_on_ = on; }

ps_t Noc::hop_ps() const { return cycles_to_ps(router_cycles_ + link_cycles_, freq_); }

ps_t Noc::ser_ps(int bytes) const {
  double cyc = std::ceil(static_cast<double>(bytes) / link_bytes_);
  return cycles_to_ps(cyc, freq_);
}

void Noc::path(XY a, XY b, int* out, int* n) const {
  int k = 0, x = a.x, y = a.y;
  out[k++] = idx(a.x, a.y, 5);  // injection port
  while (x != b.x) {
    int dir = b.x > x ? 0 : 1;
    out[k++] = idx(x, y, dir);
    x += b.x > x ? 1 : -1;
  }
  while (y != b.y) {
    int dir = b.y > y ? 2 : 3;
    out[k++] = idx(x, y, dir);
    y += b.y > y ? 1 : -1;
  }
  out[k++] = idx(b.x, b.y, 4);  // ejection port
  *n = k;
}

void Noc::touch_window(NocLink& l, ps_t t, int bytes) {
  if (t >= l.win_start + window_ps_) {
    ps_t k = (t - l.win_start) / window_ps_;
    l.prev_win_bytes = (k >= 2) ? 0 : l.win_bytes;
    l.win_start += k * window_ps_;
    l.win_bytes = 0;
  }
  l.win_bytes += static_cast<uint64_t>(bytes);
}

double Noc::link_util(const NocLink& l, ps_t t) const {
  double cap = bytes_per_ps() * static_cast<double>(window_ps_);
  double u = 0;
  if (t < l.win_start + 2 * window_ps_) u = static_cast<double>(t < l.win_start + window_ps_ ? l.prev_win_bytes : l.win_bytes) / cap;
  u += l.bg_rate / bytes_per_ps();
  return u;
}

double Noc::path_util(XY a, XY b, ps_t t) {
  int p[64], n = 0;
  path(a, b, p, &n);
  double u = 0;
  for (int i = 0; i < n; ++i) u = std::max(u, link_util(links_[p[i]], t));
  return std::min(u, 0.95);
}

ps_t Noc::send(XY a, XY b, int bytes, int qos, ps_t t, uint64_t* flit_hops) {
  int p[64], n = 0;
  path(a, b, p, &n);
  int h = hops(a, b);
  int flits = (bytes + flit_bytes_ - 1) / flit_bytes_;
  if (flit_hops) *flit_hops += static_cast<uint64_t>(flits) * static_cast<uint64_t>(std::max(1, h));
  const ps_t hop = hop_ps();
  const ps_t ser = ser_ps(bytes);
  const double bpp = bytes_per_ps();
  if (level_ == 0) {
    // L0: zero-load latency plus M/D/1 waiting time on the most utilized
    // link of the path: W = rho / (2 (1 - rho)) * service(max packet).
    double rho = 0;
    for (int i = 0; i < n; ++i) {
      NocLink& l = links_[p[i]];
      rho = std::max(rho, link_util(l, t));
      touch_window(l, t, bytes);
      l.bytes += static_cast<uint64_t>(bytes);
      l.busy_ps += ser;
    }
    rho = std::min(rho, 0.95);
    double svc = static_cast<double>(ser_ps(std::min(bytes, max_packet_)));
    ps_t w = static_cast<ps_t>(rho / (2.0 * (1.0 - rho)) * svc);
    return t + h * hop + ser + w;
  }
  // L1: packet-train reservation per link (virtual cut-through). QoS uses
  // separate reservation timelines so higher classes only see equal/higher
  // class occupancy (plus one packet of non-preemptive blocking), and lower
  // classes are pushed back by higher-class traffic.
  const ps_t pkt_ser = ser_ps(max_packet_);
  const ps_t buf_ps = cycles_to_ps(buf_flits_ * flit_bytes_ / link_bytes_, freq_);
  ps_t head = t, last_ser = ser, prev_start = 0;
  int prev = -1;
  for (int i = 0; i < n; ++i) {
    NocLink& l = links_[p[i]];
    double bg = std::min(0.9, l.bg_rate / bpp);
    ps_t s = bg > 0 ? static_cast<ps_t>(static_cast<double>(ser) / (1.0 - bg)) : ser;
    ps_t avail = 0, lower = 0;
    if (qos_on_) {
      for (int k = 0; k <= qos; ++k) avail = std::max(avail, l.free_at[k]);
      for (int k = qos + 1; k < N_QOS; ++k) lower = std::max(lower, l.free_at[k]);
    } else {
      avail = l.free_at[0];
    }
    ps_t start = std::max(head, avail);
    if (qos_on_ && lower > start) start += std::min(lower - start, pkt_ser);
    ps_t wait = start - head;
    if (prev >= 0 && wait > buf_ps) {
      // Credit/backpressure approximation: once the downstream buffer is
      // full, the upstream link stays held by this packet train.
      NocLink& pl = links_[prev];
      ps_t hold = prev_start + last_ser + (wait - buf_ps);
      if (qos_on_) {
        for (int k = qos; k < N_QOS; ++k) pl.free_at[k] = std::max(pl.free_at[k], hold);
      } else {
        pl.free_at[0] = std::max(pl.free_at[0], hold);
      }
    }
    if (qos_on_) {
      l.free_at[qos] = start + s;
      for (int k = qos + 1; k < N_QOS; ++k) l.free_at[k] = std::max(l.free_at[k], start) + s;
    } else {
      l.free_at[0] = start + s;
    }
    l.busy_ps += s;
    l.bytes += static_cast<uint64_t>(bytes);
    touch_window(l, start, bytes);
    prev = p[i];
    prev_start = start;
    last_ser = s;
    head = start + (is_mesh_link(p[i]) ? hop : 0);  // ports add occupancy, not hop latency
  }
  return head + last_ser;
}

ps_t Noc::header_latency(XY a, XY b, int bytes, uint64_t* flit_hops) const {
  int h = hops(a, b);
  if (flit_hops) *flit_hops += static_cast<uint64_t>((bytes + flit_bytes_ - 1) / flit_bytes_) * static_cast<uint64_t>(std::max(1, h));
  return h * hop_ps() + ser_ps(bytes);
}

void Noc::add_fluid(XY a, XY b, double rate, std::vector<int32_t>* touched) {
  int p[64], n = 0;
  path(a, b, p, &n);
  for (int i = 0; i < n; ++i) {
    links_[p[i]].bg_rate += rate;
    if (touched) touched->push_back(p[i]);
  }
}

void Noc::remove_fluid(const std::vector<int32_t>& links, double rate) {
  for (int32_t i : links) {
    links_[i].bg_rate -= rate;
    if (links_[i].bg_rate < 1e-15) links_[i].bg_rate = 0;
  }
}

std::string Noc::link_name(int i) const {
  int node = i / 6, dir = i % 6;
  int x = node % dimx_, y = node / dimx_;
  std::string s = std::to_string(x) + "," + std::to_string(y);
  if (dir == 4) return s + ":eject";
  if (dir == 5) return s + ":inject";
  int nx = x + (dir == 0 ? 1 : dir == 1 ? -1 : 0), ny = y + (dir == 2 ? 1 : dir == 3 ? -1 : 0);
  return s + "->" + std::to_string(nx) + "," + std::to_string(ny);
}

ps_t Noc::total_busy_ps() const {
  ps_t s = 0;
  for (size_t i = 0; i < links_.size(); ++i)
    if (is_mesh_link(static_cast<int>(i))) s += links_[i].busy_ps;
  return s;
}

// ============================================================================
// System cache
void SysCache::init(const SocCfg& soc, const ModelParams& p, int level) {
  level_ = level;
  ways_ = std::max(1, soc.cache.ways);                 // blocks.sys_cache.ways
  block_ = std::max(64, p.cache_block_bytes);          // params.cache_block_bytes
  size_bytes_ = soc.cache.size_kb * 1024.0;            // blocks.sys_cache.size_kb
  sets_ = std::max(1, static_cast<int>(size_bytes_ / (static_cast<double>(block_) * ways_)));
  tags_.assign(static_cast<size_t>(sets_) * ways_, UINT64_MAX);
  lru_.assign(tags_.size(), 0);
  dirty_.assign(tags_.size(), 0);
  uint32_t all = ways_ >= 32 ? 0xffffffffu : ((1u << ways_) - 1u);
  way_mask_.fill(all);
  part_on_ = false;
}

void SysCache::set_partition(const std::array<int, N_QOS>* w) {
  uint32_t all = ways_ >= 32 ? 0xffffffffu : ((1u << ways_) - 1u);
  if (!w) {
    part_on_ = false;
    way_mask_.fill(all);
    return;
  }
  part_on_ = true;
  int base = 0;
  for (int q = 0; q < N_QOS; ++q) {
    int n = std::clamp((*w)[q], 0, ways_ - base);
    uint32_t m = 0;
    for (int i = 0; i < n; ++i) m |= 1u << (base + i);
    way_mask_[q] = m;
    base += n;
  }
}

double SysCache::class_capacity(int qos) const {
  if (!part_on_) return size_bytes_;
  return size_bytes_ * std::popcount(way_mask_[qos]) / static_cast<double>(ways_);
}

static inline uint64_t mix_block(uint64_t b) {
  b ^= b >> 33;
  b *= 0xff51afd7ed558ccdull;
  b ^= b >> 29;
  return b;
}

int SysCache::access(uint64_t addr, int bytes, int qos, bool write, bool alloc, float l0_hit, uint64_t /*salt*/,
                     std::vector<uint64_t>* wb) {
  if (bytes <= 0) return 0;
  if (level_ == 0) {
    // L0 hit-rate model: the requester supplies the expected hit fraction.
    int h = static_cast<int>(std::lround(bytes * std::clamp<double>(l0_hit, 0.0, 1.0)));
    hits += static_cast<uint64_t>(h);
    misses += static_cast<uint64_t>(bytes - h);
    return write && alloc ? bytes : h;
  }
  int absorbed = 0;
  uint64_t first = addr / static_cast<uint64_t>(block_);
  uint64_t last = (addr + static_cast<uint64_t>(bytes) - 1) / static_cast<uint64_t>(block_);
  const uint32_t mask = way_mask_[qos];
  for (uint64_t blk = first; blk <= last; ++blk) {
    uint64_t lo = std::max<uint64_t>(addr, blk * block_);
    uint64_t hi = std::min<uint64_t>(addr + bytes, (blk + 1) * block_);
    int part = static_cast<int>(hi - lo);
    size_t set = static_cast<size_t>(mix_block(blk) % static_cast<uint64_t>(sets_));
    size_t base = set * static_cast<size_t>(ways_);
    int hit_way = -1;
    for (int w = 0; w < ways_; ++w)
      if (tags_[base + w] == blk) { hit_way = w; break; }
    if (hit_way >= 0) {
      lru_[base + hit_way] = ++clock_;
      if (write) dirty_[base + hit_way] = 1;
      absorbed += part;
      hits += static_cast<uint64_t>(part);
      continue;
    }
    misses += static_cast<uint64_t>(part);
    if (!alloc || mask == 0) continue;
    int victim = -1;
    uint32_t best = UINT32_MAX;
    for (int w = 0; w < ways_; ++w) {
      if (!(mask & (1u << w))) continue;
      if (tags_[base + w] == UINT64_MAX) { victim = w; break; }
      if (lru_[base + w] < best) { best = lru_[base + w]; victim = w; }
    }
    if (victim < 0) continue;
    if (tags_[base + victim] != UINT64_MAX && dirty_[base + victim] && wb) wb->push_back(tags_[base + victim] * block_);
    tags_[base + victim] = blk;
    lru_[base + victim] = ++clock_;
    dirty_[base + victim] = write ? 1 : 0;
    if (write) absorbed += part;  // write-allocate: the write is absorbed
  }
  return absorbed;
}

// ============================================================================
// DRAM
void Dram::init(const SocCfg& soc, const ModelParams& p, int level, Rng& rng) {
  level_ = level;
  const auto& d = soc.dram;
  int nch = std::max(1, d.ctrls * d.ch_per_ctrl);  // blocks.dram.controllers x channels_per_controller
  ch_per_ctrl_ = std::max(1, d.ch_per_ctrl);
  nbanks_ = std::max(1, d.bgs * d.banks_per_bg);   // bank_groups x banks_per_group
  row_bytes_ = std::max(64, d.row_bytes);          // blocks.dram.row_bytes
  il_ = std::max(row_bytes_, p.dram_interleave_bytes);  // params.dram_interleave_bytes
  qdepth_ = p.dram_queue_depth;                    // params.dram_queue_depth
  peak_bpp_ = d.bus_bits / 8.0 * d.mts * 1e6 / 1e12;  // data_rate_mts x bus_bits_total
  tRCD_ = ns_to_ps(d.tRCD);                        // timing_ns.*
  tRP_ = ns_to_ps(d.tRP);
  CL_ = ns_to_ps(d.CL);
  tRAS_ = ns_to_ps(d.tRAS);
  tRFC_ = ns_to_ps(d.tRFC);
  tREFI_ = ns_to_ps(d.tREFI);
  tWTR_ = ns_to_ps(d.tWTR);
  tRTW_ = ns_to_ps(d.tRTW);
  ctrl_ps_ = ns_to_ps(p.dram_ctrl_ns);             // params.dram_ctrl_ns
  window_ps_ = ns_to_ps(p.dram_window_ns);         // params.dram_util_window_ns
  l0_eff_ = p.dram_l0_eff;                         // params.dram_l0_eff
  auto lg = [](uint64_t v) { return (v && !(v & (v - 1))) ? std::countr_zero(v) : -1; };
  il_sh_ = lg(static_cast<uint64_t>(il_));
  nch_sh_ = lg(static_cast<uint64_t>(nch));
  row_sh_ = lg(static_cast<uint64_t>(row_bytes_));
  nb_sh_ = lg(static_cast<uint64_t>(nbanks_));
  ch_.assign(static_cast<size_t>(nch), DramChannel{});
  for (auto& c : ch_) {
    c.banks.assign(static_cast<size_t>(nbanks_), DramBank{});
    // Stagger refresh phases across channels (deterministic, seeded).
    c.next_refresh = tREFI_ > 0 ? static_cast<ps_t>(rng.uniform() * static_cast<double>(tREFI_)) : PS_NEVER;
  }
}

int Dram::channel_of(uint64_t addr) const {
  if (il_sh_ >= 0 && nch_sh_ >= 0)
    return static_cast<int>(((addr >> il_sh_) ^ (addr >> 33) ^ (addr >> 41)) & ((1ull << nch_sh_) - 1));
  uint64_t a = addr / static_cast<uint64_t>(il_);
  return static_cast<int>((a ^ (addr >> 33) ^ (addr >> 41)) % ch_.size());
}

void Dram::map(uint64_t addr, int* bank, int64_t* row) const {
  if (il_sh_ >= 0 && nch_sh_ >= 0 && row_sh_ >= 0 && nb_sh_ >= 0) {
    uint64_t local = ((addr >> (il_sh_ + nch_sh_)) << il_sh_) | (addr & ((1ull << il_sh_) - 1));
    uint64_t ru = local >> row_sh_;
    *bank = static_cast<int>((ru ^ (addr >> 34) ^ (addr >> 38)) & ((1ull << nb_sh_) - 1));
    *row = static_cast<int64_t>(ru >> nb_sh_);
    return;
  }
  uint64_t nch = ch_.size();
  uint64_t local = (addr / (static_cast<uint64_t>(il_) * nch)) * static_cast<uint64_t>(il_) + addr % static_cast<uint64_t>(il_);
  uint64_t ru = local / static_cast<uint64_t>(row_bytes_);
  *bank = static_cast<int>((ru ^ (addr >> 34) ^ (addr >> 38)) % static_cast<uint64_t>(nbanks_));
  *row = static_cast<int64_t>(ru / static_cast<uint64_t>(nbanks_));
}

void Dram::enqueue(int ch, const DramReq& r) {
  DramChannel& c = ch_[ch];
  c.q[r.qos].push_back(r);
  c.qlen++;
}

static bool later_req(const DramReq& a, const DramReq& b) { return a.arrive > b.arrive || (a.arrive == b.arrive && a.seq > b.seq); }

void Dram::add_pending(int ch, const DramReq& r) {
  auto& p = ch_[ch].pending;
  p.push_back(r);
  std::push_heap(p.begin(), p.end(), later_req);
}

void Dram::admit(int ch, ps_t now, bool qos_on) {
  auto& p = ch_[ch].pending;
  while (!p.empty() && p.front().arrive <= now) {
    std::pop_heap(p.begin(), p.end(), later_req);
    DramReq r = p.back();
    p.pop_back();
    if (!qos_on) r.qos = 0;
    enqueue(ch, r);
  }
}

ps_t Dram::next_arrival(int ch) const {
  const auto& p = ch_[ch].pending;
  return p.empty() ? PS_NEVER : p.front().arrive;
}

void Dram::requeue_for_qos(bool qos_on) {
  for (auto& c : ch_) {
    std::vector<DramReq> all;
    for (auto& q : c.q) {
      for (auto& r : q) all.push_back(r);
      q.clear();
    }
    std::sort(all.begin(), all.end(), [](const DramReq& a, const DramReq& b) { return a.seq < b.seq; });
    for (auto& r : all) c.q[qos_on ? r.qos : 0].push_back(r);
  }
}

ps_t Dram::base_latency_ps() const { return ctrl_ps_ + tRCD_ + CL_; }

void Dram::note_bytes(int ch, ps_t t, int bytes) {
  DramChannel& c = ch_[ch];
  if (t >= c.win_start + window_ps_) {
    ps_t k = (t - c.win_start) / window_ps_;
    c.prev_win_bytes = k >= 2 ? 0 : c.win_bytes;
    c.win_start += k * window_ps_;
    c.win_bytes = 0;
  }
  c.win_bytes += static_cast<uint64_t>(bytes);
}

double Dram::util(int ch, ps_t t) const {
  const DramChannel& c = ch_[ch];
  double cbpp = channel_bytes_per_ps();
  double u = 0;
  if (t < c.win_start + 2 * window_ps_)
    u = static_cast<double>(t < c.win_start + window_ps_ ? c.prev_win_bytes : c.win_bytes) / (cbpp * static_cast<double>(window_ps_));
  u += c.bg_rate / cbpp;
  return std::min(u, 0.95);
}

double Dram::avg_util(ps_t t) const {
  double s = 0;
  for (int i = 0; i < n_channels(); ++i) s += util(i, t);
  return s / n_channels();
}

void Dram::add_fluid(double rate_total) {
  for (auto& c : ch_) {
    c.bg_rate += rate_total / static_cast<double>(ch_.size());
    if (c.bg_rate < 1e-15) c.bg_rate = 0;
  }
}

ps_t Dram::l0_latency(int ch, int bytes, ps_t t, ps_t* queue_ps) {
  // L0: base latency + transfer at sustained efficiency + M/D/1 wait.
  double cbpp = channel_bytes_per_ps() * l0_eff_;
  ps_t xfer = static_cast<ps_t>(bytes / cbpp);
  double rho = util(ch, t);
  ps_t w = static_cast<ps_t>(rho / (2.0 * (1.0 - rho)) * static_cast<double>(xfer));
  if (queue_ps) *queue_ps = w;
  note_bytes(ch, t, bytes);
  DramChannel& c = ch_[ch];
  c.bytes += static_cast<uint64_t>(bytes);
  c.busy_ps += xfer;
  return base_latency_ps() + xfer + w;
}

DramDecision Dram::decide(int chi, ps_t now, bool qos_on) {
  DramDecision d;
  DramChannel& c = ch_[chi];
  admit(chi, now, qos_on);
  if (c.qlen == 0) {
    ps_t na = next_arrival(chi);
    d.idle = na == PS_NEVER;
    d.more = !d.idle;
    d.next_decide = na;
    return d;
  }
  // Refresh (all-bank): blocks the data bus for tRFC and closes all rows.
  while (c.next_refresh <= std::max(now, c.bus_free)) {
    ps_t rs = std::max(c.next_refresh, c.bus_free);
    c.bus_free = rs + tRFC_;
    for (auto& b : c.banks) {
      b.open_row = -1;
      b.ready_ps = std::max(b.ready_ps, c.bus_free);
    }
    c.next_refresh += tREFI_;
    c.refreshes++;
  }
  std::deque<DramReq>* q = nullptr;
  if (qos_on) {
    for (int k = 0; k < N_QOS; ++k)
      if (!c.q[k].empty()) { q = &c.q[k]; break; }
  } else {
    for (int k = 0; k < N_QOS; ++k)
      if (!c.q[k].empty()) { q = &c.q[k]; break; }
  }
  if (!q) return d;
  // FR-FCFS: oldest row hit within the queue window, else oldest.
  size_t window = std::min<size_t>(static_cast<size_t>(qdepth_), q->size());
  size_t best = 0;
  for (size_t i = 0; i < window; ++i) {
    const DramReq& r = (*q)[i];
    if (c.banks[r.bank].open_row == r.row) { best = i; break; }
  }
  DramReq r = (*q)[best];
  q->erase(q->begin() + static_cast<std::ptrdiff_t>(best));
  c.qlen--;

  ps_t bus = c.bus_free;
  if (c.bus_free > 0 && r.write != c.last_write) bus += r.write ? tRTW_ : tWTR_;  // timing_ns.tRTW / tWTR
  double cbpp = channel_bytes_per_ps();
  double bg = std::min(0.9, c.bg_rate / cbpp);
  uint64_t addr = r.addr;
  int left = r.bytes;
  ps_t data_end = bus;
  while (left > 0) {
    int seg = std::min(left, row_bytes_ - static_cast<int>(addr % static_cast<uint64_t>(row_bytes_)));
    int bank;
    int64_t row;
    map(addr, &bank, &row);
    DramBank& b = c.banks[bank];
    ps_t cas;
    // Row-buffer accounting is per 64 B column burst (assumption): a segment
    // that opens a row counts one miss and (bursts - 1) hits.
    const uint64_t bursts = static_cast<uint64_t>((seg + 63) / 64);
    if (b.open_row == row) {
      cas = std::max(now, b.ready_ps);
      c.row_hits += bursts;
    } else if (b.open_row < 0) {
      ps_t act = std::max(now, b.ready_ps);
      cas = act + tRCD_;
      b.act_ps = act;
      c.acts++;
      d.acts++;
      c.row_empty++;
      c.row_hits += bursts - 1;
    } else {
      ps_t pre = std::max({now, b.ready_ps, b.act_ps + tRAS_});
      ps_t act = pre + tRP_;
      cas = act + tRCD_;
      b.act_ps = act;
      c.acts++;
      d.acts++;
      c.row_conflicts++;
      c.row_hits += bursts - 1;
    }
    b.open_row = row;
    b.ready_ps = cas;
    ps_t ds = std::max(cas + CL_, bus);
    ps_t dur = static_cast<ps_t>(std::ceil(seg / (cbpp * (1.0 - bg))));
    data_end = ds + dur;
    bus = data_end;
    c.busy_ps += dur;
    addr += static_cast<uint64_t>(seg);
    left -= seg;
  }
  c.bus_free = bus;
  c.last_write = r.write;
  c.bytes += static_cast<uint64_t>(r.bytes);
  note_bytes(chi, now, r.bytes);
  d.issued = true;
  d.txn = r.txn;
  d.data_end = data_end + ctrl_ps_;
  // Decide the next request early enough that its activation overlaps the
  // current burst (lookahead = tRP + tRCD + CL).
  d.next_decide = std::max(now, c.bus_free - (tRP_ + tRCD_ + CL_));
  if (c.qlen == 0) {
    ps_t na = next_arrival(chi);
    if (na == PS_NEVER) d.idle = true;
    else d.next_decide = std::max(d.next_decide, na);
  }
  d.more = !d.idle;
  return d;
}

}  // namespace gc
