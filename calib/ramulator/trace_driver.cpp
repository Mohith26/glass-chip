// trace_driver: replay a memory request trace through Ramulator 2.1 and report
// per-request latency.
//
// Input CSV (header required): time_ns,addr,rw,bytes
//   rw is R/W (or 0/1). bytes may exceed the DRAM transaction size; the request is
//   split into consecutive tx-sized chunks and completes when its last chunk does.
// Output CSV: idx,time_ns,addr,rw,bytes,accept_ns,done_ns,latency_ns
//   latency_ns = done_ns - time_ns, so it includes time spent waiting for a free
//   controller queue slot (backpressure), not just in-controller latency.
//   Reads complete when data returns (Ramulator's depart); writes complete when
//   the WR command is issued (Ramulator calls the write callback at issue).
//
// Usage: trace_driver <config.yaml> <trace.csv> <out.csv> [max_attempts_per_cycle]
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "ramulator/base/config.h"
#include "ramulator/base/factory.h"
#include "ramulator/base/request.h"
#include "ramulator/frontend/i_frontend.h"
#include "ramulator/memory_system/i_memory_system.h"

struct Req {
  double time_ns;
  uint64_t addr;
  int rw;
  int bytes;
  int chunks_left = 0;
  int64_t accept_clk = -1;
  int64_t done_clk = -1;
};

struct Chunk {
  uint32_t req;
  uint64_t addr;
  int64_t ready_clk;
};

int main(int argc, char** argv) {
  if (argc < 4) {
    std::fprintf(stderr, "usage: %s config.yaml trace.csv out.csv [max_attempts]\n", argv[0]);
    return 2;
  }
  const int max_attempts = argc > 4 ? std::atoi(argv[4]) : 128;
  auto config = Ramulator::Config::parse_config_file(argv[1]);
  auto* fe = Ramulator::Factory::create_frontend(config);
  auto* ms = Ramulator::Factory::create_memory_system(config);
  fe->connect_memory_system(ms);
  ms->connect_frontend(fe);
  const double tck_ns = ms->get_tCK();
  const int tx = ms->get_tx_bytes();

  std::vector<Req> reqs;
  {
    std::ifstream in(argv[2]);
    std::string line;
    std::getline(in, line);  // header
    while (std::getline(in, line)) {
      if (line.empty()) continue;
      std::stringstream ss(line);
      std::string a, b, c, d;
      std::getline(ss, a, ',');
      std::getline(ss, b, ',');
      std::getline(ss, c, ',');
      std::getline(ss, d, ',');
      Req r;
      r.time_ns = std::stod(a);
      r.addr = std::stoull(b, nullptr, 0);
      r.rw = (c == "W" || c == "w" || c == "1") ? 1 : 0;
      r.bytes = d.empty() ? 64 : std::stoi(d);
      if (r.bytes <= 0) r.bytes = tx;
      reqs.push_back(r);
    }
  }
  // Expand into chunks in time order (the trace is assumed sorted by time).
  std::deque<Chunk> future;
  for (uint32_t i = 0; i < reqs.size(); ++i) {
    auto& r = reqs[i];
    uint64_t base = r.addr / tx * tx;
    uint64_t end = r.addr + r.bytes;
    int n = 0;
    for (uint64_t a = base; a < end; a += tx) {
      future.push_back({i, a, (int64_t)(r.time_ns / tck_ns)});
      ++n;
    }
    r.chunks_left = n;
  }

  std::vector<Chunk> ready;
  ready.reserve(4096);
  int64_t clk = 0;
  size_t remaining = reqs.size();
  const int64_t clk_limit = (int64_t)4e9;
  while (remaining > 0 && clk < clk_limit) {
    while (!future.empty() && future.front().ready_clk <= clk) {
      ready.push_back(future.front());
      future.pop_front();
    }
    // Try to send ready chunks in arrival order; failed ones keep their order.
    size_t w = 0;
    int attempts = 0;
    for (size_t i = 0; i < ready.size(); ++i) {
      Chunk ch = ready[i];
      bool ok = false;
      if (attempts < max_attempts) {
        ++attempts;
        uint32_t ri = ch.req;
        ok = fe->receive_external_requests(
            reqs[ri].rw, (Ramulator::Addr_t)ch.addr, 0,
            [&reqs, &remaining, &clk, ri](Ramulator::Request&) {
              auto& rr = reqs[ri];
              if (--rr.chunks_left == 0) {
                rr.done_clk = clk;
                --remaining;
              }
            },
            tx);
        if (ok && reqs[ri].accept_clk < 0) reqs[ri].accept_clk = clk;
      }
      if (!ok) ready[w++] = ch;
    }
    ready.resize(w);
    ms->tick();
    ++clk;
  }
  fe->finalize();
  ms->finalize();

  std::ofstream out(argv[3]);
  out << "idx,time_ns,addr,rw,bytes,accept_ns,done_ns,latency_ns\n";
  char buf[256];
  for (size_t i = 0; i < reqs.size(); ++i) {
    const auto& r = reqs[i];
    double acc = r.accept_clk >= 0 ? r.accept_clk * tck_ns : -1;
    double done = r.done_clk >= 0 ? (r.done_clk + 1) * tck_ns : -1;
    std::snprintf(buf, sizeof(buf), "%zu,%.3f,0x%llx,%c,%d,%.3f,%.3f,%.3f\n", i, r.time_ns,
                  (unsigned long long)r.addr, r.rw ? 'W' : 'R', r.bytes, acc, done,
                  done >= 0 ? done - r.time_ns : -1.0);
    out << buf;
  }
  std::fprintf(stderr, "tck_ns=%.4f tx_bytes=%d cycles=%lld unfinished=%zu\n", tck_ns, tx,
               (long long)clk, remaining);
  return remaining == 0 ? 0 : 1;
}
