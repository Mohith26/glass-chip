// Discrete-event kernel primitives: virtual time, POD events, the event
// queue ordered by (time, sequence), per-component PCG64 RNGs and the
// streaming trace hash.
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>

#include "serial.hpp"

namespace gc {

using ps_t = int64_t;  // virtual time in picoseconds
constexpr ps_t PS_PER_NS = 1000;
constexpr ps_t PS_PER_US = 1000 * PS_PER_NS;
constexpr ps_t PS_PER_MS = 1000 * PS_PER_US;
constexpr ps_t PS_PER_S = 1000 * PS_PER_MS;
constexpr ps_t PS_NEVER = INT64_MAX / 4;

inline ps_t ns_to_ps(double ns) { return static_cast<ps_t>(std::llround(ns * 1000.0)); }
inline ps_t us_to_ps(double us) { return static_cast<ps_t>(std::llround(us * 1e6)); }
// Cycles at frequency hz expressed in picoseconds (rounded up so that
// nothing completes in zero time unless it has zero cycles).
inline ps_t cycles_to_ps(double cycles, double hz) {
  if (cycles <= 0) return 0;
  return static_cast<ps_t>(std::ceil(cycles * 1e12 / hz));
}

// A queued event is plain data. `comp` selects the component handler and
// `kind` the handler branch; a/b/c are handler-defined integer payloads.
// Because no closures are stored, the whole queue can be checkpointed.
struct Event {
  ps_t t = 0;
  uint64_t seq = 0;
  uint16_t comp = 0;
  uint16_t kind = 0;
  int32_t a = 0;
  int64_t b = 0;
  int64_t c = 0;
  GC_IO(t, seq, comp, kind, a, b, c)
};

// Binary min-heap keyed by (t, seq). Ties in time resolve in insertion order,
// which makes the simulation fully deterministic.
class EventQueue {
 public:
  void push(ps_t t, uint16_t comp, uint16_t kind, int32_t a = 0, int64_t b = 0, int64_t c = 0) {
    heap_.push_back(Event{t, next_seq_++, comp, kind, a, b, c});
    std::push_heap(heap_.begin(), heap_.end(), Later{});
  }
  bool empty() const { return heap_.empty(); }
  size_t size() const { return heap_.size(); }
  const Event& top() const { return heap_.front(); }
  Event pop() {
    std::pop_heap(heap_.begin(), heap_.end(), Later{});
    Event e = heap_.back();
    heap_.pop_back();
    return e;
  }
  GC_IO(heap_, next_seq_)

 private:
  struct Later {
    bool operator()(const Event& x, const Event& y) const {
      return x.t > y.t || (x.t == y.t && x.seq > y.seq);
    }
  };
  std::vector<Event> heap_;
  uint64_t next_seq_ = 0;
};

inline uint64_t splitmix64(uint64_t& x) {
  uint64_t z = (x += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

inline uint64_t fnv1a(const std::string& s) {
  uint64_t h = 1469598103934665603ull;
  for (unsigned char ch : s) { h ^= ch; h *= 1099511628211ull; }
  return h;
}

// PCG64 (128-bit LCG state, DXSM output permutation). Each component gets its
// own stream derived from the master seed and the component name, so adding
// randomness in one component never perturbs another.
struct Rng {
  __uint128_t state = 0;
  __uint128_t inc = 1;

  void seed(uint64_t master, const std::string& stream_name) {
    uint64_t x = master ^ fnv1a(stream_name);
    uint64_t s0 = splitmix64(x), s1 = splitmix64(x), i0 = splitmix64(x), i1 = splitmix64(x);
    inc = ((static_cast<__uint128_t>(i0) << 64) | i1) | 1u;
    state = 0;
    next();
    state += (static_cast<__uint128_t>(s0) << 64) | s1;
    next();
  }
  uint64_t next() {
    const uint64_t mul = 0xda942042e4dd58b5ull;
    __uint128_t old = state;
    state = old * mul + inc;
    uint64_t hi = static_cast<uint64_t>(old >> 64);
    uint64_t lo = static_cast<uint64_t>(old) | 1u;
    hi ^= hi >> 32;
    hi *= mul;
    hi ^= hi >> 48;
    hi *= lo;
    return hi;
  }
  // Uniform in [0, 1).
  double uniform() { return (next() >> 11) * (1.0 / 9007199254740992.0); }
  // Standard normal via Box-Muller (one value per call, deterministic).
  double normal() {
    double u1 = uniform(), u2 = uniform();
    if (u1 < 1e-300) u1 = 1e-300;
    return std::sqrt(-2.0 * std::log(u1)) * std::cos(6.283185307179586 * u2);
  }
  GC_IO(state, inc)
};

// Order-sensitive 128-bit streaming hash over trace records.
struct StreamHash {
  uint64_t h1 = 0x6a09e667f3bcc908ull, h2 = 0xbb67ae8584caa73bull;
  uint64_t count = 0;
  inline void add(uint64_t v) {
    h1 ^= v + 0x9E3779B97F4A7C15ull + (h1 << 6) + (h1 >> 2);
    h1 *= 0xff51afd7ed558ccdull;
    h2 = ((h2 ^ v) * 0xc4ceb9fe1a85ec53ull);
    h2 ^= h2 >> 29;
    h2 += h1;
  }
  std::string hex() const;
  GC_IO(h1, h2, count)
};

}  // namespace gc
