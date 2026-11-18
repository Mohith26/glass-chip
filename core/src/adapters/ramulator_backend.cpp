// DRAM L2 backend: Ramulator 2.1 (third_party/ramulator2) behind gc::DramBackend.
//
// Built as a separate plugin (build/libgc_ramulator.dylib) that the simulator
// dlopen()s when the fidelity mix asks for dram: L2, so the main Python module
// never links Ramulator. The embedding follows calib/ramulator/trace_driver.cpp:
// External frontend, receive_external_requests() + memory_system->tick(), with
// a completion callback. One Ramulator tick = tCK (0.938 ns for the derived
// LPDDR5X-8533 config); requests are split into tx-sized (32 B) transactions and
// complete when their last transaction completes. Reads complete when data
// returns; writes complete when the WR command issues (Ramulator semantics).
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <string>
#include <vector>

#include "glasschip/memsys.hpp"
#include "ramulator/base/config.h"
#include "ramulator/base/factory.h"
#include "ramulator/base/request.h"
#include "ramulator/frontend/i_frontend.h"
#include "ramulator/memory_system/i_memory_system.h"

namespace {

class RamulatorBackend final : public gc::DramBackend {
 public:
  explicit RamulatorBackend(const std::string& cfg_path) {
    auto config = Ramulator::Config::parse_config_file(cfg_path);
    fe_ = Ramulator::Factory::create_frontend(config);
    ms_ = Ramulator::Factory::create_memory_system(config);
    fe_->connect_memory_system(ms_);
    ms_->connect_frontend(fe_);
    tck_ps_ = ms_->get_tCK() * 1000.0;
    tx_ = ms_->get_tx_bytes();
  }
  ~RamulatorBackend() override {
    // Ramulator objects are owned by its factory/registry; finalize only.
    if (fe_) fe_->finalize();
    if (ms_) ms_->finalize();
  }
  const char* name() const override { return "ramulator2"; }
  bool busy() const override { return outstanding_ > 0; }

  void enqueue(const gc::MemTxn& txn, gc::ps_t t) override {
    uint32_t slot;
    if (!free_.empty()) {
      slot = free_.back();
      free_.pop_back();
    } else {
      slot = static_cast<uint32_t>(reqs_.size());
      reqs_.emplace_back();
    }
    Req& r = reqs_[slot];
    r.txn = txn;
    r.live = true;
    uint64_t base = txn.addr / tx_ * tx_;
    uint64_t end = txn.addr + static_cast<uint64_t>(std::max(1, txn.bytes));
    int64_t clk = static_cast<int64_t>(std::ceil(static_cast<double>(t) / tck_ps_));
    r.chunks_left = 0;
    for (uint64_t a = base; a < end; a += static_cast<uint64_t>(tx_)) {
      future_.push_back(Chunk{slot, a, clk, seq_++});
      std::push_heap(future_.begin(), future_.end(), later);
      r.chunks_left++;
    }
    outstanding_++;
  }

  void tick(gc::ps_t t, std::vector<std::pair<gc::MemTxn, gc::ps_t>>& done) override {
    done_ = &done;
    while (static_cast<double>(clk_) * tck_ps_ <= static_cast<double>(t)) {
      if (outstanding_ == 0 && future_.empty() && ready_.size() == rd_head_) {
        // Idle: jump the clock (Ramulator still needs ticks for refresh, so we
        // only skip when nothing is outstanding).
        clk_ = static_cast<int64_t>(std::floor(static_cast<double>(t) / tck_ps_)) + 1;
        break;
      }
      while (!future_.empty() && future_.front().clk <= clk_) {
        std::pop_heap(future_.begin(), future_.end(), later);
        ready_.push_back(future_.back());
        future_.pop_back();
      }
      // Offer at most 128 ready transactions per cycle, oldest first; the rest
      // wait (controller queues full = backpressure). Only the scanned prefix
      // is compacted so a long backlog costs O(128) per cycle.
      size_t w = rd_head_, i = rd_head_;
      int attempts = 0, fails = 0;
      for (; i < ready_.size() && attempts < 128 && fails < 16; ++i, ++attempts) {
        Chunk ch = ready_[i];
        uint32_t slot = ch.slot;
        bool ok = fe_->receive_external_requests(reqs_[slot].txn.write ? 1 : 0, static_cast<Ramulator::Addr_t>(ch.addr), 0,
                                                 [this, slot](Ramulator::Request&) { on_chunk(slot); }, tx_);
        if (!ok) {
          ready_[w++] = ch;
          ++fails;  // after 16 consecutive refusals the controllers are full this cycle
        } else {
          fails = 0;
        }
      }
      if (w != i) {
        // shift the failed prefix right so the deque head advances past accepted ones
        size_t accepted = i - w;
        for (size_t k = w; k-- > rd_head_;) ready_[k + accepted] = ready_[k];
        rd_head_ += accepted;
      }
      if (rd_head_ > 4096 && rd_head_ * 2 > ready_.size()) {
        ready_.erase(ready_.begin(), ready_.begin() + static_cast<std::ptrdiff_t>(rd_head_));
        rd_head_ = 0;
      }
      if (rd_head_ == ready_.size()) {
        ready_.clear();
        rd_head_ = 0;
      }
      ms_->tick();
      ++clk_;
    }
    done_ = nullptr;
  }

 private:
  struct Req {
    gc::MemTxn txn;
    int chunks_left = 0;
    bool live = false;
  };
  struct Chunk {
    uint32_t slot;
    uint64_t addr;
    int64_t clk;
    uint64_t seq;
  };
  static bool later(const Chunk& a, const Chunk& b) { return a.clk > b.clk || (a.clk == b.clk && a.seq > b.seq); }

  void on_chunk(uint32_t slot) {
    Req& r = reqs_[slot];
    if (--r.chunks_left == 0) {
      // Completion is observed during tick clk_, i.e. by the end of that cycle.
      gc::ps_t when = static_cast<gc::ps_t>(std::llround(static_cast<double>(clk_ + 1) * tck_ps_));
      if (done_) done_->emplace_back(r.txn, when);
      r.live = false;
      free_.push_back(slot);
      outstanding_--;
    }
  }

  Ramulator::IFrontEnd* fe_ = nullptr;
  Ramulator::IMemorySystem* ms_ = nullptr;
  double tck_ps_ = 938;
  int tx_ = 32;
  int64_t clk_ = 0;
  uint64_t seq_ = 0;
  int64_t outstanding_ = 0;
  std::vector<Req> reqs_;
  std::vector<uint32_t> free_;
  std::vector<Chunk> future_;
  std::vector<Chunk> ready_;
  size_t rd_head_ = 0;  // ready_[rd_head_..] are still waiting
  std::vector<std::pair<gc::MemTxn, gc::ps_t>>* done_ = nullptr;
};

}  // namespace

extern "C" __attribute__((visibility("default"))) gc::DramBackend* gc_make_dram_backend(const char* config_path, char* err, int errlen) {
  try {
    return new RamulatorBackend(config_path);
  } catch (const std::exception& e) {
    std::snprintf(err, static_cast<size_t>(errlen), "%s", e.what());
  } catch (...) {
    std::snprintf(err, static_cast<size_t>(errlen), "unknown error constructing Ramulator");
  }
  return nullptr;
}
