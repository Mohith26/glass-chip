// NoC L2 backend: BookSim 2 (third_party/booksim2, with the calibration
// worker's trace-mode patch) running in-process behind gc::NocBackend.
//
// Built as a plugin (build/libgc_booksim.dylib) from BookSim's object files
// (everything except main.o) plus this file, which provides BookSim's globals.
// A TrafficManager subclass queues packets into the patch's per-node trace
// queues (_trace_q, injected by _InjectTrace at their creation cycle), steps
// the network one cycle at a time (_Step), and catches tail flits in
// _RetireFlit. BookSim keeps global state, so one backend per process.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "glasschip/memsys.hpp"

#include "booksim.hpp"
#include "booksim_config.hpp"
#include "flit.hpp"
#include "globals.hpp"
#include "network.hpp"
#include "routefunc.hpp"
#include "trafficmanager.hpp"

// ---- BookSim globals normally defined in main.cpp
TrafficManager* trafficManager = NULL;
int GetSimTime() { return trafficManager ? trafficManager->getTime() : 0; }
class Stats;
Stats* GetStats(const std::string& name) { return trafficManager ? trafficManager->getStats(name) : NULL; }
bool gPrintActivity = false;
int gK, gN, gC, gNodes;
bool gTrace = false;
std::ostream* gWatchOut = NULL;

namespace {

class GcTrafficManager : public TrafficManager {
 public:
  GcTrafficManager(const Configuration& cfg, const std::vector<Network*>& net) : TrafficManager(cfg, net) {
    _time = 0;
    _sim_state = running;
    _ClearStats();
    _trace_q.assign(_nodes, std::deque<TraceRec>());
  }
  void queue(long long cycle, int src, int dst, int flits, long long id) {
    std::deque<TraceRec>& q = _trace_q[src];
    TraceRec r;
    r.cycle = cycle;
    r.src = src;
    r.dst = dst;
    r.size = flits;
    r.id = id;
    auto it = q.end();
    while (it != q.begin() && (it - 1)->cycle > cycle) --it;  // keep each source queue sorted by cycle
    q.insert(it, r);
  }
  void step() {
    _InjectTrace();
    _Step();
  }
  int time() const { return _time; }
  void jump(int t) { _time = t; }
  bool queued() const {
    for (const auto& q : _trace_q)
      if (!q.empty()) return true;
    return false;
  }
  std::vector<std::pair<long long, int>> retired;  // (packet id, arrival cycle)

 protected:
  void _RetireFlit(Flit* f, int dest) override {
    if (f->tail) {
      auto it = _trace_pid_to_id.find(f->pid);
      if (it != _trace_pid_to_id.end()) {
        retired.emplace_back(it->second, _time);
        _trace_pid_to_id.erase(it);
      }
    }
    TrafficManager::_RetireFlit(f, dest);
  }
};

class BookSimBackend final : public gc::NocBackend {
 public:
  BookSimBackend(const std::string& cfg_text, double freq_hz, int kx, int flit_bytes, int max_packet_bytes)
      : cycle_ps_(1e12 / freq_hz), kx_(kx), flit_(flit_bytes), max_pkt_(max_packet_bytes) {
    if (trafficManager) throw std::runtime_error("only one BookSim backend per process");
    // ParseString appends its own ';' and BookSim's parser rejects empty
    // statements, so strip trailing separators. NOTE: BookSim calls exit() on a
    // config error, which ends the host process; the Sim only passes text it
    // generates plus params.booksim_config.
    std::string text = cfg_text;
    while (!text.empty() && (text.back() == ';' || text.back() == ' ' || text.back() == '\n')) text.pop_back();
    config_.ParseString(text);
    InitializeRoutingMap(config_);
    gPrintActivity = false;
    gTrace = false;
    gWatchOut = NULL;
    net_.push_back(Network::New(config_, "network_0"));
    tm_ = new GcTrafficManager(config_, net_);
    trafficManager = tm_;
  }
  ~BookSimBackend() override {
    // BookSim's destructors assume a completed run; leave the objects to process
    // exit and only drop the global pointer.
    trafficManager = NULL;
  }
  const char* name() const override { return "booksim2"; }
  bool busy() const override { return in_flight_ > 0; }

  void inject(uint64_t tag, gc::XY a, gc::XY b, int bytes, gc::ps_t t) override {
    int src = a.x + kx_ * a.y, dst = b.x + kx_ * b.y;
    long long cyc = static_cast<long long>(std::ceil(static_cast<double>(t) / cycle_ps_));
    if (cyc < tm_->time()) cyc = tm_->time();
    int npk = std::max(1, (bytes + max_pkt_ - 1) / max_pkt_);
    long long msg = next_msg_++;
    msgs_[msg] = Msg{tag, npk};
    for (int k = 0; k < npk; ++k) {
      int nb = std::min(max_pkt_, bytes - k * max_pkt_);
      int flits = std::max(1, (nb + flit_ - 1) / flit_);
      long long pid = next_pkt_++;
      pkt_msg_[pid] = msg;
      tm_->queue(cyc, src, dst, flits, pid);
      in_flight_++;
    }
  }

  void tick(gc::ps_t t, std::vector<std::pair<uint64_t, gc::ps_t>>& done) override {
    long long target = static_cast<long long>(std::floor(static_cast<double>(t) / cycle_ps_));
    // BookSim's clock is a 32-bit int (about 1.43 s of simulated time at 1.5 GHz).
    if (target > 2000000000LL) throw std::runtime_error("BookSim backend: simulated time beyond its 32-bit cycle counter");
    while (tm_->time() < target) {
      if (in_flight_ == 0 && !tm_->queued()) {
        tm_->jump(static_cast<int>(target));  // network idle: skip ahead
        break;
      }
      tm_->step();
      for (const auto& r : tm_->retired) {
        auto pm = pkt_msg_.find(r.first);
        if (pm == pkt_msg_.end()) continue;
        long long msg = pm->second;
        pkt_msg_.erase(pm);
        in_flight_--;
        auto it = msgs_.find(msg);
        if (it == msgs_.end()) continue;
        if (--it->second.left == 0) {
          if (it->second.tag) done.emplace_back(it->second.tag, static_cast<gc::ps_t>(std::llround((r.second + 1) * cycle_ps_)));
          msgs_.erase(it);
        }
      }
      tm_->retired.clear();
    }
  }

 private:
  struct Msg {
    uint64_t tag;
    int left;
  };
  BookSimConfig config_;
  std::vector<Network*> net_;
  GcTrafficManager* tm_ = nullptr;
  double cycle_ps_;
  int kx_, flit_, max_pkt_;
  long long next_msg_ = 1, next_pkt_ = 0;
  long long in_flight_ = 0;
  std::map<long long, Msg> msgs_;
  std::map<long long, long long> pkt_msg_;
};

}  // namespace

// config: "freq_hz=<f>;kx=<k>;flit_bytes=<b>;max_packet_bytes=<m>|<booksim config text>"
extern "C" __attribute__((visibility("default"))) gc::NocBackend* gc_make_noc_backend(const char* config, char* err, int errlen) {
  try {
    std::string s(config);
    size_t bar = s.find('|');
    std::string head = s.substr(0, bar), body = bar == std::string::npos ? "" : s.substr(bar + 1);
    std::map<std::string, double> h;
    std::stringstream hs(head);
    std::string kv;
    while (std::getline(hs, kv, ';')) {
      size_t eq = kv.find('=');
      if (eq != std::string::npos) h[kv.substr(0, eq)] = std::stod(kv.substr(eq + 1));
    }
    return new BookSimBackend(body, h["freq_hz"], static_cast<int>(h["kx"]), static_cast<int>(h["flit_bytes"]),
                              static_cast<int>(h["max_packet_bytes"]));
  } catch (const std::exception& e) {
    std::snprintf(err, static_cast<size_t>(errlen), "%s", e.what());
  } catch (...) {
    std::snprintf(err, static_cast<size_t>(errlen), "unknown error constructing BookSim");
  }
  return nullptr;
}
