// C++ unit tests for glasschip-core with analytic expectations (spec 11.1).
// Usage: test_core <path to vx1.json>   (the Makefile converts configs/soc/vx1.yaml)
#include <cmath>
#include <cstring>
#include <cstdio>
#include <fstream>
#include <functional>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "glasschip/sim.hpp"

using namespace gc;

static int g_fail = 0, g_checks = 0;
static std::string g_test;
#define CHECK(cond)                                                                       \
  do {                                                                                    \
    ++g_checks;                                                                           \
    if (!(cond)) {                                                                        \
      ++g_fail;                                                                           \
      std::printf("  FAIL [%s] %s:%d: %s\n", g_test.c_str(), __FILE__, __LINE__, #cond); \
    }                                                                                     \
  } while (0)
#define CHECK_NEAR(a, b, tol)                                                                                         \
  do {                                                                                                                \
    ++g_checks;                                                                                                       \
    double _a = (a), _b = (b);                                                                                        \
    if (!(std::fabs(_a - _b) <= (tol))) {                                                                             \
      ++g_fail;                                                                                                       \
      std::printf("  FAIL [%s] %s:%d: %s=%.6g vs %s=%.6g (tol %.3g)\n", g_test.c_str(), __FILE__, __LINE__, #a, _a, #b, _b, \
                  static_cast<double>(tol));                                                                          \
    }                                                                                                                 \
  } while (0)

static Json g_soc;

static Json fid(const char* cpu, const char* npu, const char* other_engines, const char* mem) {
  Json f = Json::object();
  f.set("cpu", Json::string(cpu));
  f.set("npu", Json::string(npu));
  f.set("simt", Json::string(other_engines));
  f.set("dsp", Json::string(other_engines));
  f.set("noc", Json::string(mem));
  f.set("dram", Json::string(mem));
  f.set("cache", Json::string(mem));
  return f;
}

struct OpSpec {
  std::string id, type = "cpu_kernel", dtype = "int8";
  std::vector<std::string> elig = {"cpu"};
  double macs = 0, flops = 0, bin = 0, bw = 0, bout = 0, scalar = 0;
  std::vector<std::string> deps;
  bool has_deps = false;
  std::map<std::string, double> shape;
  bool boundary = true;
};

static Json task(const std::string& id, double period_us, double deadline_us, const std::string& crit, const std::vector<OpSpec>& ops,
                 const std::string& qos = "realtime", const std::string& overrun = "queue", const std::vector<std::string>& affinity = {}) {
  Json g = Json::object();
  g.set("schema_version", Json::string("1.0"));
  g.set("task_id", Json::string(id));
  g.set("name", Json::string(id));
  Json rel = Json::object();
  rel.set("kind", Json::string("periodic"));
  rel.set("period_us", Json::number(period_us));
  rel.set("on_overrun", Json::string(overrun));
  g.set("release", rel);
  g.set("deadline_us", Json::number(deadline_us));
  g.set("criticality", Json::string(crit));
  g.set("qos_class", Json::string(qos));
  if (!affinity.empty()) {
    Json a = Json::array();
    for (const auto& s : affinity) a.push(Json::string(s));
    g.set("affinity", a);
  }
  Json arr = Json::array();
  for (const auto& o : ops) {
    Json j = Json::object();
    j.set("op_id", Json::string(o.id));
    j.set("type", Json::string(o.type));
    j.set("dtype", Json::string(o.dtype));
    Json el = Json::array();
    for (const auto& e : o.elig) el.push(Json::string(e));
    j.set("eligible", el);
    j.set("macs", Json::number(o.macs));
    j.set("flops", Json::number(o.flops));
    j.set("bytes_in", Json::number(o.bin));
    j.set("bytes_w", Json::number(o.bw));
    j.set("bytes_out", Json::number(o.bout));
    if (o.scalar > 0) {
      Json ins = Json::object();
      ins.set("scalar", Json::number(o.scalar));
      j.set("instr", ins);
    }
    if (o.has_deps) {
      Json d = Json::array();
      for (const auto& s : o.deps) d.push(Json::string(s));
      j.set("depends_on", d);
    }
    if (!o.shape.empty()) {
      Json s = Json::object();
      for (const auto& kv : o.shape) s.set(kv.first, Json::number(kv.second));
      j.set("shape", s);
    }
    j.set("preempt_boundary", Json::boolean(o.boundary));
    arr.push(j);
  }
  g.set("ops", arr);
  return g;
}

static OpSpec cpu_op(const std::string& id, double cycles_at_ipc2) {
  OpSpec o;
  o.id = id;
  o.scalar = cycles_at_ipc2 * 2.0;  // ipc_scalar 2.0 in vx1.yaml
  return o;
}

static Json soc_with_cpus(int a, int b) {
  Json s = g_soc;
  Json* blocks = const_cast<Json*>(s.get("blocks"));
  const_cast<Json*>(blocks->get("cpu_a"))->set("cores", Json::number(a));
  const_cast<Json*>(blocks->get("cpu_b"))->set("cores", Json::number(b));
  return s;
}

static std::map<std::string, Completion> by_task(const std::vector<Completion>& v) {
  std::map<std::string, Completion> m;
  for (const auto& c : v) m[c.task_id] = c;
  return m;
}

static const char* g_filter = nullptr;
static void test(const char* name, const std::function<void()>& fn) {
  if (g_filter && !std::strstr(name, g_filter)) return;
  g_test = name;
  int before = g_fail;
  try {
    fn();
  } catch (const std::exception& e) {
    ++g_fail;
    std::printf("  FAIL [%s] exception: %s\n", name, e.what());
  }
  std::printf("%s %s\n", g_fail == before ? "ok  " : "FAIL", name);
}

int main(int argc, char** argv) {
  if (argc < 2) {
    std::printf("usage: test_core vx1.json [test-name-filter]\n");
    return 2;
  }
  std::setvbuf(stdout, nullptr, _IONBF, 0);
  if (argc >= 3) g_filter = argv[2];
  std::ifstream f(argv[1]);
  std::stringstream ss;
  ss << f.rdbuf();
  g_soc = parse_json(ss.str());
  SocCfg soc = parse_soc(g_soc);

  auto fid_p = [&](const char* mem, std::vector<std::pair<std::string, double>> kv) {
    Json f = fid("L0", "L1", "L0", mem);
    Json p = Json::object();
    for (auto& e : kv) p.set(e.first, Json::number(e.second));
    f.set("params", p);
    return f;
  };

  test("noc_single_flow_zero_load", [&] {
    Sim sim(g_soc, fid_p("L1", {{"noc_rr_tail", 0}}), 1);  // pure queueing model
    Noc& n = sim.noc();
    XY a{0, 0}, b{3, 3};
    ps_t lat = n.send(a, b, 4096, Q_REALTIME, 0, nullptr);
    // hops * (router + link cycles) + ceil(bytes / link_bytes) cycles at 1.5 GHz
    ps_t hop = cycles_to_ps(soc.router_cycles + soc.link_cycles, soc.noc_freq);
    ps_t ser = cycles_to_ps(std::ceil(4096.0 / soc.link_bytes), soc.noc_freq);
    // + network interface: source router + injection and ejection channels
    ps_t ni = cycles_to_ps(soc.router_cycles + 2 * soc.link_cycles, soc.noc_freq);
    CHECK(lat == 6 * hop + ni + ser);
    // A second identical flow injected at the same time queues behind the first one.
    ps_t lat2 = n.send(a, b, 4096, Q_REALTIME, 0, nullptr);
    CHECK(lat2 == lat + ser);
    // Default model: the waiting message also pays the round-robin tail term
    // (params.noc_rr_tail = 0.75 of its wait, latency only).
    Sim s2(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    ps_t l1 = s2.noc().send(a, b, 4096, Q_REALTIME, 0, nullptr);
    ps_t l2 = s2.noc().send(a, b, 4096, Q_REALTIME, 0, nullptr);
    CHECK(l1 == lat);
    CHECK(l2 == lat + ser + static_cast<ps_t>(0.75 * static_cast<double>(ser)));
    // ...and a third message injected later, after the queue drained, sees zero load.
    CHECK(s2.noc().send(a, b, 4096, Q_REALTIME, PS_PER_US, nullptr) - PS_PER_US == lat);
  });

  test("noc_calendar_fills_gaps", [&] {
    // A reservation made for the future must not block earlier traffic (the
    // legacy train model did), and concurrent messages interleave.
    for (int arb = 0; arb < 2; ++arb) {
      Sim sim(g_soc, fid_p("L1", {{"noc_l1_arbitration", arb}, {"noc_rr_tail", 0}}), 1);
      Noc& n = sim.noc();
      XY a{0, 0}, b{1, 0};
      ps_t later = n.send(a, b, 4096, Q_REALTIME, 10 * PS_PER_US, nullptr) - 10 * PS_PER_US;  // future reservation
      ps_t now = n.send(a, b, 256, Q_REALTIME, 0, nullptr);                                 // earlier traffic
      ps_t zero = n.hops(a, b) * n.hop_ps() + n.ni_ps() + n.ser_ps(256);
      if (arb == 1) CHECK(now == zero && later == n.hops(a, b) * n.hop_ps() + n.ni_ps() + n.ser_ps(4096));
      else CHECK(now > 10 * PS_PER_US);  // legacy: blocked behind the future reservation
    }
  });

  test("noc_l0_matches_zero_load_when_idle", [&] {
    Sim sim(g_soc, fid("L0", "L0", "L0", "L0"), 1);
    ps_t lat = sim.noc().send(XY{0, 0}, XY{2, 1}, 256, Q_REALTIME, 0, nullptr);
    ps_t hop = cycles_to_ps(soc.router_cycles + soc.link_cycles, soc.noc_freq);
    CHECK(lat == 3 * hop + cycles_to_ps(soc.router_cycles + 2 * soc.link_cycles, soc.noc_freq) + cycles_to_ps(8, soc.noc_freq));
  });

  test("noc_qos_priority", [&] {
    for (int qos_on = 0; qos_on < 2; ++qos_on) {
      Sim sim(g_soc, fid_p("L1", {{"noc_rr_tail", 0}}), 1);
      sim.set_qos(qos_on);
      Noc& n = sim.noc();
      for (int i = 0; i < 64; ++i) n.send(XY{0, 0}, XY{3, 0}, 4096, Q_BEST_EFFORT, 0, nullptr);
      ps_t s = n.send(XY{0, 0}, XY{3, 0}, 64, Q_SAFETY, 1000, nullptr) - 1000;
      ps_t pkt = n.ser_ps(soc.max_packet_bytes);
      // QoS: at most one lower-class packet of blocking per link (injection + 3 links + ejection).
      if (qos_on) CHECK(s <= 3 * n.hop_ps() + n.ni_ps() + n.ser_ps(64) + 5 * pkt + 1);
      else CHECK(s > 60 * n.ser_ps(4096));                               // FIFO: waits behind the burst
    }
  });

  test("dram_row_hit_stream_efficiency", [&] {
    Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    Dram& d = sim.dram();
    // Sequential addresses that map to channel 0: mostly row hits.
    std::vector<uint64_t> addrs;
    for (uint64_t a = 0; addrs.size() < 2000; a += 4096)
      if (d.channel_of(a) == 0) addrs.push_back(a);
    for (size_t i = 0; i < addrs.size(); ++i) {
      DramReq r;
      r.txn = static_cast<int32_t>(i);
      r.arrive = 0;
      r.seq = i;
      d.map(addrs[i], &r.bank, &r.row);
      r.addr = addrs[i];
      r.bytes = 4096;
      d.enqueue(0, r);
    }
    ps_t now = 0, first = -1, last = 0;
    for (;;) {
      DramDecision dd = d.decide(0, now, false);
      if (!dd.issued) break;
      if (first < 0) first = dd.data_end;
      last = dd.data_end;
      if (!dd.more) break;
      now = dd.next_decide;
    }
    double bw = 4096.0 * addrs.size() / static_cast<double>(last);  // bytes per ps
    double peak = d.channel_bytes_per_ps();
    double refresh_eff = 1.0 - soc.dram.tRFC / soc.dram.tREFI;
    const DramChannel& c = d.channel(0);
    double hit_rate = static_cast<double>(c.row_hits) / (c.row_hits + c.row_empty + c.row_conflicts);
    std::printf("    stream: %.1f GB/s of %.1f peak (%.3f), refresh bound %.3f, row-hit rate %.2f\n", bw * 1e3, peak * 1e3, bw / peak,
                refresh_eff, hit_rate);
    CHECK(bw <= peak * 1.0001);
    CHECK(bw >= 0.85 * peak * refresh_eff);
    CHECK(hit_rate > 0.4);
  });

  test("dram_random_rows_slower_than_stream", [&] {
    Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    Dram& d = sim.dram();
    Rng rng;
    rng.seed(7, "t");
    std::vector<uint64_t> addrs;
    while (addrs.size() < 1000) {
      uint64_t a = (rng.next() % (1ull << 32)) & ~uint64_t{63};
      if (d.channel_of(a) == 0) addrs.push_back(a);
    }
    for (size_t i = 0; i < addrs.size(); ++i) {
      DramReq r;
      r.arrive = 0;
      r.seq = i;
      d.map(addrs[i], &r.bank, &r.row);
      r.addr = addrs[i];
      r.bytes = 64;
      d.enqueue(0, r);
    }
    ps_t now = 0, last = 0;
    for (;;) {
      DramDecision dd = d.decide(0, now, false);
      if (!dd.issued) break;
      last = dd.data_end;
      if (!dd.more) break;
      now = dd.next_decide;
    }
    double bw = 64.0 * addrs.size() / static_cast<double>(last);
    CHECK(bw < 0.5 * d.channel_bytes_per_ps());  // random 64 B accesses are activation bound
    CHECK(d.channel(0).row_conflicts > 500);
  });

  test("systolic_gemm_closed_form", [&] {
    Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    // WS on 64x64: folds = ceil(K/64)*ceil(N/64) = 8*4 = 32; fold = M + 64 + 64 - 1
    CHECK_NEAR(sim.npu_gemm_cycles(1024, 512, 256, 1, 1, DT_INT8), 32.0 * (1024 + 127), 1e-6);
    CHECK_NEAR(sim.npu_gemm_cycles(1024, 512, 256, 1, 4, DT_INT8), 8.0 * (1024 + 127), 1e-6);
    CHECK_NEAR(sim.npu_gemm_cycles(1024, 512, 256, 1, 1, DT_BF16), 4 * 32.0 * (1024 + 127), 1e-6);
    CHECK_NEAR(sim.npu_gemm_cycles(1024, 512, 256, 1, 1, DT_FP8), 2 * 32.0 * (1024 + 127), 1e-6);
    // End to end: a compute-only GEMM job takes cycles / f_npu.
    OpSpec g;
    g.id = "gemm";
    g.type = "matmul";
    g.elig = {"npu"};
    g.macs = 1024.0 * 512 * 256;
    g.shape = {{"M", 1024}, {"K", 512}, {"N", 256}};
    Json fl = fid("L0", "L1", "L0", "L1");
    Json p = Json::object();
    p.set("npu_max_tiles_per_op", Json::number(1));
    fl.set("params", p);
    Sim s2(g_soc, fl, 1);
    s2.add_task(task("T90_gemm", 1000, 1000, "high", {g}));
    s2.release_job("T90_gemm", 0, 0);
    auto done = s2.run_until(10 * PS_PER_MS);
    CHECK(done.size() == 1);
    // + completion interrupt to the host at job end (params.irq_latency_ns default 2000)
    double expect = 32.0 * (1024 + 127) / soc.npu.freq * 1e12 + 2000e3;
    CHECK_NEAR(static_cast<double>(done[0].response), expect, expect * 0.001 + 10);
  });

  test("edf_vs_fifo_order", [&] {
    for (int pol = 0; pol < 2; ++pol) {
      Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
      sim.set_scheduler("cpu", pol == 0 ? "fifo" : "edf");
      sim.add_task(task("T91_x", 100000, 50000, "medium", {cpu_op("x", 2e6)}));  // 1 ms at 2 GHz
      sim.add_task(task("T92_p", 100000, 10000, "medium", {cpu_op("p", 2e5)}));
      sim.add_task(task("T93_q", 100000, 2000, "medium", {cpu_op("q", 2e5)}));
      sim.release_job("T91_x", 0, 0);
      sim.run_until(100 * PS_PER_US);
      sim.release_job("T92_p", 100 * PS_PER_US, 0);
      sim.release_job("T93_q", 100 * PS_PER_US, 0);
      auto m = by_task(sim.run_until(20 * PS_PER_MS));
      CHECK(m.size() == 3);
      if (pol == 0) {
        CHECK(m["T92_p"].finish < m["T93_q"].finish);  // FIFO: arrival order
        CHECK(m["T91_x"].finish < m["T92_p"].finish);  // no preemption
      } else {
        CHECK(m["T93_q"].finish < m["T92_p"].finish);  // EDF: earliest deadline first
        CHECK(m["T93_q"].finish < m["T91_x"].finish);  // and it preempts the long job
        CHECK(!m["T93_q"].missed);
      }
    }
  });

  test("edf_mc_protects_safety", [&] {
    // Plain EDF lets an earlier-deadline best-effort job delay a safety job;
    // edf_mc never lets lower criticality preempt or overtake safety work.
    for (int pol = 0; pol < 2; ++pol) {
      Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
      sim.set_scheduler("cpu", pol == 0 ? "edf" : "edf_mc");
      sim.add_task(task("T94_be", 100000, 1500, "low", {cpu_op("be", 2e6)}, "best_effort"));
      sim.add_task(task("T95_safe", 100000, 3000, "safety", {cpu_op("s", 2e5)}, "safety"));
      sim.release_job("T95_safe", 0, 0);
      sim.release_job("T94_be", 10 * PS_PER_US, 0);
      auto m = by_task(sim.run_until(20 * PS_PER_MS));
      if (pol == 0) CHECK(m["T95_safe"].finish > m["T94_be"].finish);
      else CHECK(m["T95_safe"].finish < m["T94_be"].finish);
    }
  });

  test("preemption_cost_cpu", [&] {
    Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
    sim.set_scheduler("cpu", "fixed_priority");
    sim.add_task(task("T96_low", 100000, 100000, "low", {cpu_op("l", 1e7)}));   // 5 ms
    sim.add_task(task("T97_high", 1000, 1000, "high", {cpu_op("h", 2e5)}));     // 0.1 ms
    sim.release_job("T96_low", 0, 0);
    sim.run_until(1 * PS_PER_MS);
    sim.release_job("T97_high", 1 * PS_PER_MS, 0);
    auto m = by_task(sim.run_until(20 * PS_PER_MS));
    ps_t ctx = ns_to_ps(soc.cpu[0].ctx_ns);
    // low: 5 ms work + 0.1 ms of high + context switches (low start, high start, low restore)
    ps_t extra = m["T96_low"].response - 5 * PS_PER_MS - 100 * PS_PER_US;
    CHECK(extra >= 2 * ctx);
    CHECK(extra <= 4 * ctx);
    CHECK(m["T96_low"].attribution[A_PREEMPTION] >= 100 * PS_PER_US);
    CHECK(m["T97_high"].response <= 100 * PS_PER_US + 2 * ctx);
  });

  test("npu_layer_preemption_save_restore", [&] {
    Json fl = fid("L0", "L0", "L0", "L0");
    Json p = Json::object();
    p.set("npu_max_tiles_per_op", Json::number(4));
    p.set("npu_min_macs_per_tile", Json::number(1));
    fl.set("params", p);
    Sim sim(g_soc, fl, 1);
    sim.set_scheduler("npu", "fixed_priority");
    std::vector<OpSpec> layers;
    for (int i = 0; i < 4; ++i) {
      OpSpec o;
      o.id = "l" + std::to_string(i);
      o.type = "matmul";
      o.elig = {"npu"};
      o.macs = 4e9;  // ~0.25 ms per layer on 4 tiles at L0
      layers.push_back(o);
    }
    OpSpec h;
    h.id = "h";
    h.type = "matmul";
    h.elig = {"npu"};
    h.macs = 1e8;
    sim.add_task(task("T98_big", 100000, 100000, "medium", layers));
    sim.add_task(task("T99_urgent", 1000, 1000, "safety", {h}));
    sim.release_job("T98_big", 0, 0);
    sim.run_until(100 * PS_PER_US);
    sim.release_job("T99_urgent", 100 * PS_PER_US, 0);
    auto m = by_task(sim.run_until(20 * PS_PER_MS));
    ps_t half = us_to_ps(soc.npu.preempt_us / 2);
    // Urgent waits for the current layer (layer-level preemption) and pays the save.
    CHECK(m["T99_urgent"].attribution[A_PREEMPTION] == half);
    CHECK(m["T99_urgent"].attribution[A_ENGINE_QUEUE] > 0);
    // The big job pays the restore when it resumes.
    CHECK(m["T98_big"].attribution[A_PREEMPTION] >= half);
  });

  test("dvfs_scaling_and_transition_stall", [&] {
    // Whole op at half frequency: 1M cycles at 1 GHz after a 20 us stall.
    Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
    sim.add_task(task("T80_cpu", 100000, 100000, "high", {cpu_op("c", 1e6)}));
    sim.set_domain_freq("cpu_a", 1e9);
    sim.release_job("T80_cpu", 0, 0);
    auto d = sim.run_until(10 * PS_PER_MS);
    ps_t stall = us_to_ps(soc.dvfs_transition_us);
    ps_t ctx = ns_to_ps(soc.cpu[0].ctx_ns);
    CHECK(d.size() == 1);
    CHECK_NEAR(static_cast<double>(d[0].response), static_cast<double>(stall + PS_PER_MS + ctx), 2000.0);
    // Mid-op change: half the work at 2 GHz, then stall, then the rest at 1 GHz.
    Sim s2(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
    s2.add_task(task("T80_cpu", 100000, 100000, "high", {cpu_op("c", 1e6)}));
    s2.release_job("T80_cpu", 0, 0);
    s2.run_until(ctx + 250 * PS_PER_US);
    s2.set_domain_freq("cpu_a", 1e9);
    auto d2 = s2.run_until(10 * PS_PER_MS);
    CHECK(d2.size() == 1);
    CHECK_NEAR(static_cast<double>(d2[0].response), static_cast<double>(ctx + 250 * PS_PER_US + stall + 500 * PS_PER_US), 2000.0);
    CHECK_NEAR(static_cast<double>(d2[0].attribution[A_DVFS]), static_cast<double>(stall), 1000.0);
  });

  // A mixed workload touching every engine and the full memory system.
  auto mixed = [&](Sim& sim) {
    OpSpec conv;
    conv.id = "conv";
    conv.type = "conv2d";
    conv.elig = {"npu"};
    conv.macs = 2e8;
    conv.bin = 2e6;
    conv.bw = 5e5;
    conv.bout = 1e6;
    conv.shape = {{"N", 1}, {"C", 64}, {"H", 80}, {"W", 80}, {"K", 128}, {"R", 3}, {"S", 3}, {"stride", 1}};
    OpSpec post = cpu_op("post", 2e5);
    post.bin = 1e6;
    sim.add_task(task("T61_det", 33333, 33000, "high", {conv, post}));
    OpSpec sk;
    sk.id = "esdf";
    sk.type = "simt_kernel";
    sk.elig = {"simt"};
    sk.flops = 4e8;
    sk.bin = 8e6;
    sk.bout = 8e6;
    sim.add_task(task("T62_esdf", 66666, 66000, "medium", {sk}));
    OpSpec dsp;
    dsp.id = "isp";
    dsp.type = "resize";
    dsp.elig = {"dsp"};
    dsp.flops = 5e6;
    dsp.bin = 2.7e6;
    dsp.bout = 1.2e6;
    OpSpec dma;
    dma.id = "copy";
    dma.type = "memcopy";
    dma.elig = {"dma"};
    dma.bin = 1.2e6;
    dma.bout = 1.2e6;
    sim.add_task(task("T63_ingest", 33333, 15000, "high", {dsp, dma}));
    OpSpec ctl = cpu_op("ctl", 2e3);
    ctl.elig = {"rt"};
    sim.add_task(task("T64_ctl", 1000, 1000, "safety", {ctl}, "safety", "skip", {"rt"}));
  };
  auto drive = [&](Sim& sim, ps_t from, ps_t to, std::vector<Completion>* out) {
    for (ps_t t = from; t < to; t += PS_PER_MS) {
      int64_t k = t / PS_PER_MS;
      sim.release_job("T64_ctl", t, k);
      if (k % 33 == 0) {
        sim.release_job("T61_det", t, k);
        sim.release_job("T63_ingest", t, k);
      }
      if (k % 66 == 5) sim.release_job("T62_esdf", t, k);
      auto d = sim.run_until(t + PS_PER_MS);
      if (out) out->insert(out->end(), d.begin(), d.end());
    }
  };

  test("attribution_sums_to_response", [&] {
    for (const char* lvl : {"L0", "L1"}) {
      Sim sim(g_soc, fid(lvl, "L1", lvl, "L1"), 3);
      mixed(sim);
      std::vector<Completion> all;
      drive(sim, 0, 200 * PS_PER_MS, &all);
      CHECK(all.size() > 200);
      bool ok = true;
      for (const auto& c : all) {
        ps_t s = 0;
        for (ps_t v : c.attribution) {
          s += v;
          if (v < 0) ok = false;
        }
        if (s != c.response) ok = false;
        if (c.critical_path.empty()) ok = false;
      }
      CHECK(ok);
    }
  });

  test("determinism_same_seed_same_hash", [&] {
    std::string h[3];
    for (int i = 0; i < 3; ++i) {
      Json fl = fid("L1", "L1", "L1", "L1");
      Json p = Json::object();
      p.set("exec_cv", Json::number(0.1));
      fl.set("params", p);
      Sim sim(g_soc, fl, i < 2 ? 11 : 12);
      mixed(sim);
      drive(sim, 0, 120 * PS_PER_MS, nullptr);
      h[i] = sim.trace_hash();
    }
    CHECK(h[0] == h[1]);
    CHECK(h[0] != h[2]);
  });

  test("checkpoint_restore_identical_future", [&] {
    for (const char* lvl : {"L0", "L1"}) {
      Sim a(g_soc, fid(lvl, "L1", lvl, "L1"), 5);
      mixed(a);
      a.set_qos(true);
      drive(a, 0, 70 * PS_PER_MS, nullptr);
      std::vector<uint8_t> blob = a.checkpoint();
      std::vector<Completion> ra, rb, rc;
      drive(a, 70 * PS_PER_MS, 150 * PS_PER_MS, &ra);
      std::string ha = a.trace_hash();
      a.restore(blob);
      drive(a, 70 * PS_PER_MS, 150 * PS_PER_MS, &rb);
      CHECK(a.trace_hash() == ha);
      // Restore into a fresh simulator object.
      Sim b(g_soc, fid(lvl, "L1", lvl, "L1"), 999);
      b.restore(blob);
      drive(b, 70 * PS_PER_MS, 150 * PS_PER_MS, &rc);
      CHECK(b.trace_hash() == ha);
      CHECK(ra.size() == rb.size() && ra.size() == rc.size() && !ra.empty());
      bool same = true;
      for (size_t i = 0; i < ra.size() && i < rc.size(); ++i)
        if (ra[i].finish != rc[i].finish || ra[i].job_id != rc[i].job_id) same = false;
      CHECK(same);
    }
  });

  test("cache_partition_protects_safety_lines", [&] {
    for (int part = 0; part < 2; ++part) {
      Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
      if (part) {
        std::map<std::string, int> w = {{"safety", 4}, {"realtime", 8}, {"best_effort", 4}};
        sim.set_cache_partition(&w);
      }
      SysCache& c = sim.cache();
      // Safety working set of 512 KB, then a 16 MB best-effort sweep.
      for (uint64_t a = 0; a < (512u << 10); a += 1024) c.access(a, 1024, Q_SAFETY, false, true, 0, 0, nullptr);
      for (uint64_t a = 1ull << 30; a < (1ull << 30) + (16u << 20); a += 1024) c.access(a, 1024, Q_BEST_EFFORT, false, true, 0, 0, nullptr);
      int hit = 0;
      for (uint64_t a = 0; a < (512u << 10); a += 1024) hit += c.access(a, 1024, Q_SAFETY, false, true, 0, 0, nullptr);
      double frac = hit / static_cast<double>(512u << 10);
      if (part) CHECK(frac > 0.95);
      else CHECK(frac < 0.2);
    }
  });

  test("cache_streaming_bypass", [&] {
    Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    SysCache& c = sim.cache();
    for (uint64_t a = 0; a < (256u << 10); a += 1024) c.access(a, 1024, Q_REALTIME, false, true, 0, 0, nullptr);
    for (uint64_t a = 1ull << 30; a < (1ull << 30) + (32u << 20); a += 1024) c.access(a, 1024, Q_REALTIME, false, false, 0, 0, nullptr);
    int hit = 0;
    for (uint64_t a = 0; a < (256u << 10); a += 1024) hit += c.access(a, 1024, Q_REALTIME, false, true, 0, 0, nullptr);
    CHECK(hit == (256 << 10));  // the non-allocating stream evicted nothing
  });

  test("on_overrun_skip", [&] {
    Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
    sim.add_task(task("T70_s", 1000, 1000, "high", {cpu_op("c", 4e6)}, "realtime", "skip"));
    sim.add_task(task("T71_q", 1000, 1000, "high", {cpu_op("c", 4e6)}, "realtime", "queue"));
    CHECK(sim.release_job("T70_s", 0, 0) > 0);
    sim.run_until(PS_PER_MS);
    CHECK(sim.release_job("T70_s", PS_PER_MS, 1) == -1);
    CHECK(sim.release_job("T71_q", PS_PER_MS, 1) > 0);
    CHECK(sim.release_job("T71_q", PS_PER_MS, 2) > 0);
  });

  test("dma_descriptor_and_2d_strides", [&] {
    // 1 MB copy: contiguous vs a 2D gather of 256 B rows every 1024 B.
    OpSpec flat;
    flat.id = "flat";
    flat.type = "memcopy";
    flat.elig = {"dma"};
    flat.bin = 1 << 20;
    flat.bout = 1 << 20;
    OpSpec strided = flat;
    strided.id = "strided";
    strided.shape = {{"rows", 4096}, {"row_bytes", 256}, {"stride", 1024}};
    Completion r[2];
    double acts[2];
    for (int k = 0; k < 2; ++k) {
      Sim sim(g_soc, fid("L0", "L1", "L1", "L1"), 1);
      sim.add_task(task("T72_dma", 100000, 100000, "high", {k ? strided : flat}));
      sim.release_job("T72_dma", 0, 0);
      auto d = sim.run_until(20 * PS_PER_MS);
      CHECK(d.size() == 1);
      r[k] = d[0];
      acts[k] = sim.counters(false)[D_DRAM_IO].act[K_DRAM_ACTIVATES];
    }
    CHECK(r[0].response >= ns_to_ps(soc.dma.desc_ns));
    CHECK(r[0].attribution[A_DMA] > 0);
    CHECK(acts[1] > 3 * acts[0]);                          // strided rows open ~4x more DRAM rows
    CHECK(r[1].response >= r[0].response * 95 / 100);     // and never make the copy faster
    // Completion interrupt is charged when the next consumer is a CPU (job end).
    CHECK(r[0].attribution[A_MIDDLEWARE] >= ns_to_ps(2000) - 1);
  });

  test("dram_contention_shows_in_attribution", [&] {
    // A heavy NPU stream plus a CPU-light job: L1 DRAM queueing delays the NPU op.
    Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
    OpSpec big;
    big.id = "stream";
    big.type = "matmul";
    big.elig = {"npu"};
    big.macs = 1e8;
    big.bw = 64e6;
    big.shape = {{"M", 16}, {"K", 4096}, {"N", 1526}};
    sim.add_task(task("T74_vla", 100000, 100000, "high", {big}));
    sim.release_job("T74_vla", 0, 0);
    auto d = sim.run_until(100 * PS_PER_MS);
    CHECK(d.size() == 1);
    double gbps = 64e6 / (static_cast<double>(d[0].response) / 1e12) / 1e9;
    std::printf("    64 MB weight stream: %.2f ms, %.1f GB/s effective\n", d[0].response / 1e9, gbps);
    CHECK(d[0].attribution[A_DRAM] + d[0].attribution[A_NOC] > d[0].attribution[A_COMPUTE]);
    CHECK(gbps < sim.dram().peak_bytes_per_ps() * 1e3);
    CHECK(gbps <= 2.0 * soc.link_bytes * soc.noc_freq / 1e9);  // DRAM-node injection ports cap reads at 96 GB/s
    CHECK(gbps > 0.3 * sim.dram().peak_bytes_per_ps() * 1e3);
  });

  test("power_gating_wake_latency_and_counters", [&] {
    Sim sim(soc_with_cpus(1, 0), fid("L0", "L0", "L0", "L0"), 1);
    sim.add_task(task("T81_g", 100000, 100000, "high", {cpu_op("c", 1e6)}));
    ps_t wake = 50 * PS_PER_US;
    sim.set_power_gating("cpu_a", true, wake);
    sim.release_job("T81_g", PS_PER_MS, 0);
    auto d = sim.run_until(5 * PS_PER_MS);
    ps_t ctx = ns_to_ps(soc.cpu[0].ctx_ns);
    CHECK(d.size() == 1);
    CHECK_NEAR(static_cast<double>(d[0].response), static_cast<double>(wake + ctx + 500 * PS_PER_US), 2000.0);
    CHECK(d[0].attribution[A_DVFS] == wake);
    sim.release_job("T81_g", 6 * PS_PER_MS, 1);
    sim.run_until(10 * PS_PER_MS);
    auto c = sim.counters(true);
    CHECK(c[D_CPU_A].wakeups == 2);
    // gated whenever idle: 10 ms minus two (wake + ctx + 0.5 ms) busy periods
    ps_t busy_each = wake + ctx + 500 * PS_PER_US;
    CHECK_NEAR(static_cast<double>(c[D_CPU_A].gated), static_cast<double>(10 * PS_PER_MS - 2 * busy_each), 4000.0);
    CHECK(c[D_NPU].gated == 0);  // not enabled there
    sim.set_power_gating("cpu_a", false, 0);
    sim.release_job("T81_g", 11 * PS_PER_MS, 2);
    auto d3 = sim.run_until(15 * PS_PER_MS);
    CHECK(d3.size() == 1 && d3[0].attribution[A_DVFS] == 0);
    bool threw = false;
    try { sim.set_power_gating("noc_cache", true, 1); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
  });

  test("cycles_by_domain_matches_estimate", [&] {
    Sim sim(soc_with_cpus(1, 0), fid("L0", "L1", "L0", "L1"), 1);
    OpSpec g;
    g.id = "gemm";
    g.type = "matmul";
    g.elig = {"npu"};
    g.macs = 1024.0 * 512 * 256;
    g.shape = {{"M", 1024}, {"K", 512}, {"N", 256}};
    sim.add_task(task("T82_mix", 100000, 100000, "high", {cpu_op("pre", 1e6), g, cpu_op("post", 2e5)}));
    sim.release_job("T82_mix", 0, 0);
    auto pend = sim.pending_jobs();
    CHECK(pend.size() == 1);
    auto est = pend[0].est_remaining_cycles;
    auto d = sim.run_until(20 * PS_PER_MS);
    CHECK(d.size() == 1);
    CHECK_NEAR(d[0].cycles_by_domain[D_CPU_A], 1.2e6, 1.0);
    CHECK_NEAR(est[D_CPU_A], d[0].cycles_by_domain[D_CPU_A], 1.0);
    CHECK_NEAR(est[D_NPU], d[0].cycles_by_domain[D_NPU], 1.0);  // L1 GEMM cycles on 4 tiles
    CHECK(d[0].busy_by_domain[D_NPU] >= cycles_to_ps(d[0].cycles_by_domain[D_NPU], soc.npu.freq));
  });

  test("noc_dram_node_port_cap_matches_booksim", [&] {
    // BookSim (calib/tables/noc_booksim.json) saturates all-to-DRAM traffic at
    // ~95.5 GB/s: two DRAM nodes x one 32 B/cycle port x 1.5 GHz = 96 GB/s.
    double cap = 2.0 * soc.link_bytes * soc.noc_freq / 1e9;  // GB/s
    for (int dir = 0; dir < 2; ++dir) {
      Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 1);
      Noc& n = sim.noc();
      std::vector<XY> dram = soc.dram.nodes;
      double bytes = 0;
      ps_t last = 0;
      for (int k = 0; k < 400; ++k)
        for (int x = 0; x < 4; ++x)
          for (int y = 0; y < 4; ++y) {
            XY node{static_cast<int8_t>(x), static_cast<int8_t>(y)};
            XY d = dram[(k + x + y) % dram.size()];
            if (node.x == d.x && node.y == d.y) continue;
            ps_t a = dir == 0 ? n.send(node, d, 256, Q_REALTIME, 0, nullptr) : n.send(d, node, 256, Q_REALTIME, 0, nullptr);
            last = std::max(last, a);
            bytes += 256;
          }
      double gbps = bytes / (static_cast<double>(last) / 1e12) / 1e9;
      std::printf("    %s DRAM nodes: %.1f GB/s (port cap %.1f)\n", dir == 0 ? "to" : "from", gbps, cap);
      CHECK(gbps <= cap * 1.001);
      if (dir == 0) CHECK(gbps >= cap * 0.95);  // reverse direction is also limited by row-3 XY route overlap
    }
  });

  test("npu_fusion_keeps_intermediates_on_chip", [&] {
    // QK^T (1024x64x1024, 12 heads, scores 12.6 MB) -> scale -> softmax -> AV, then a
    // matmul with an 8 MB output and an elementwise epilogue.
    auto mk = [&](const std::string& id, const std::string& type, double in, double out, std::vector<std::string> deps,
                  std::map<std::string, double> shape, double macs) {
      OpSpec o;
      o.id = id;
      o.type = type;
      o.dtype = "bf16";
      o.elig = {"npu", "cpu"};
      o.bin = in;
      o.bout = out;
      o.deps = deps;
      o.has_deps = true;
      o.shape = shape;
      o.macs = macs;
      return o;
    };
    double S = 12.0 * 1024 * 1024;
    std::vector<OpSpec> ops = {
        mk("qk", "matmul", 1.5e6, S, {}, {{"M", 1024}, {"K", 64}, {"N", 1024}, {"heads", 12}}, 12.0 * 1024 * 64 * 1024),
        mk("scale", "elementwise", S, S, {"qk"}, {}, 0),
        mk("sm", "softmax", S, S, {"scale"}, {}, 0),
        mk("av", "matmul", S + 0.75e6, 0.75e6, {"sm"}, {{"M", 1024}, {"K", 1024}, {"N", 64}, {"heads", 12}}, 12.0 * 1024 * 1024 * 64),
        mk("up", "matmul", 0.75e6, 8e6, {"av"}, {{"M", 1024}, {"K", 768}, {"N", 4096}}, 1024.0 * 768 * 4096),
        mk("gelu", "elementwise", 8e6, 8e6, {"up"}, {}, 0),
    };
    double bytes[2];
    ps_t resp[2];
    for (int on = 0; on < 2; ++on) {
      Json fl = fid("L0", "L1", "L0", "L1");
      Json p = Json::object();
      p.set("fuse_attention", Json::boolean(on));
      p.set("fuse_elementwise", Json::boolean(on));
      fl.set("params", p);
      Sim sim(g_soc, fl, 1);
      sim.add_task(task("T83_attn", 100000, 100000, "high", ops));
      sim.release_job("T83_attn", 0, 0);
      auto d = sim.run_until(100 * PS_PER_MS);
      CHECK(d.size() == 1);
      resp[on] = d[0].response;
      bytes[on] = sim.counters(false)[D_DRAM_IO].act[K_DRAM_BYTES];
    }
    std::printf("    DRAM bytes: %.1f MB unfused, %.1f MB fused; response %.3f -> %.3f ms\n", bytes[0] / 1e6, bytes[1] / 1e6, resp[0] / 1e9,
                resp[1] / 1e9);
    // Unfused: scores written and read twice more (3 x 2 x 12.6 MB) plus the 8 MB epilogue round trip.
    CHECK(bytes[0] - bytes[1] > 6 * S + 2 * 8e6 - 4e6);
    CHECK(bytes[1] < 16e6);
    CHECK(resp[1] < resp[0]);
  });

  test("fault_injection_deterministic", [&] {
    auto run = [&](double flip, double drop, uint64_t seed, std::vector<Completion>* out) {
      Sim sim(g_soc, fid("L0", "L1", "L0", "L1"), 3);
      mixed(sim);
      sim.set_fault("npu_bitflip", flip, seed, -1);
      sim.set_fault("noc_drop", drop, seed, 200 * PS_PER_NS);
      drive(sim, 0, 140 * PS_PER_MS, out);
      return sim.trace_hash();
    };
    std::vector<Completion> clean, a, b;
    run(0, 0, 1, &clean);
    std::string h1 = run(0.5, 0.01, 7, &a), h2 = run(0.5, 0.01, 7, &b);
    CHECK(h1 == h2);
    CHECK(a.size() == b.size());
    int corrupted = 0, faulted = 0;
    for (size_t i = 0; i < a.size(); ++i) {
      corrupted += a[i].corrupted;
      faulted += a[i].faults > 0;
      CHECK(a[i].corrupted == b[i].corrupted && a[i].faults == b[i].faults);
    }
    int det = 0, det_corrupt = 0;
    for (const auto& c : a)
      if (c.task_id == "T61_det") { ++det; det_corrupt += c.corrupted; }
    CHECK(det_corrupt > 0 && det_corrupt < det);  // one NPU op per job at p = 0.5
    for (const auto& c : a)
      if (c.task_id != "T61_det") CHECK(!c.corrupted);  // only NPU work can be corrupted
    // Drop-and-retry only delays: no job is lost, and the average response rises.
    CHECK(clean.size() == a.size());
    double r0 = 0, r1 = 0;
    for (const auto& c : clean) if (c.task_id == "T61_det") r0 += c.response;
    for (const auto& c : a) if (c.task_id == "T61_det") r1 += c.response;
    CHECK(r1 > r0);
    CHECK(faulted > corrupted);
  });

  std::printf("\n%d checks, %d failures\n", g_checks, g_fail);
  return g_fail ? 1 : 0;
}
