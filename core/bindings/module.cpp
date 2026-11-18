// pybind11 bindings: Python module `glasschip_core` (API in notes/CONTRACTS.md).
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "glasschip/sim.hpp"

namespace py = pybind11;
using namespace gc;

// Converts a Python object (dict/list/str/number/bool/None) to a Json tree.
static Json to_json(const py::handle& h) {
  if (h.is_none()) return Json{};
  if (py::isinstance<py::bool_>(h)) return Json::boolean(h.cast<bool>());
  if (py::isinstance<py::int_>(h)) return Json::number(static_cast<double>(h.cast<long long>()));
  if (py::isinstance<py::float_>(h)) return Json::number(h.cast<double>());
  if (py::isinstance<py::str>(h)) return Json::string(h.cast<std::string>());
  if (py::isinstance<py::dict>(h)) {
    Json j = Json::object();
    for (auto kv : h.cast<py::dict>()) j.o.emplace_back(py::str(kv.first).cast<std::string>(), to_json(kv.second));
    return j;
  }
  if (py::isinstance<py::list>(h) || py::isinstance<py::tuple>(h)) {
    Json j = Json::array();
    for (auto v : h) j.a.push_back(to_json(v));
    return j;
  }
  // numpy scalars and anything float-convertible
  try {
    return Json::number(py::reinterpret_borrow<py::object>(h).cast<double>());
  } catch (...) {
  }
  return Json::string(py::str(h).cast<std::string>());
}

static py::object from_json(const Json& j) {
  switch (j.t) {
    case Json::Null: return py::none();
    case Json::Bool: return py::bool_(j.b);
    case Json::Num:
      if (std::fabs(j.n) < 9e15 && j.n == std::floor(j.n)) return py::int_(static_cast<long long>(j.n));
      return py::float_(j.n);
    case Json::Str: return py::str(j.s);
    case Json::Arr: {
      py::list l;
      for (const auto& v : j.a) l.append(from_json(v));
      return l;
    }
    case Json::Obj: {
      py::dict d;
      for (const auto& kv : j.o) d[py::str(kv.first)] = from_json(kv.second);
      return d;
    }
  }
  return py::none();
}

static py::dict completion_dict(const Completion& c) {
  py::dict d;
  d["job_id"] = c.job_id;
  d["task_id"] = c.task_id;
  d["tag"] = c.tag;
  d["release_ps"] = c.release;
  d["start_ps"] = c.start;
  d["finish_ps"] = c.finish;
  d["deadline_ps"] = c.deadline;
  d["missed"] = c.missed;
  d["response_ps"] = c.response;
  py::dict a;
  for (int k = 0; k < N_CAT; ++k) a[cat_name(k)] = c.attribution[k];
  d["attribution"] = a;
  py::list cp;
  for (const auto& e : c.critical_path) {
    py::dict x;
    x["op_id"] = e.op_id;
    x["engine"] = e.engine;
    x["unit"] = e.unit;
    x["start_ps"] = e.start;
    x["end_ps"] = e.end;
    x["wait_ps"] = e.wait;
    x["wait_cause"] = e.wait_cause;
    x["gap_ps"] = e.gap;
    x["gap_cause"] = e.gap_cause;
    cp.append(x);
  }
  d["critical_path"] = cp;
  return d;
}

// Python wrapper that keeps name tables needed to decode trace records.
struct PySim {
  std::unique_ptr<Sim> s;
  PySim(py::dict soc, py::dict fidelity, uint64_t seed) : s(std::make_unique<Sim>(to_json(soc), to_json(fidelity), seed)) {}

  py::list run_until(int64_t t) {
    std::vector<Completion> v;
    {
      py::gil_scoped_release rel;
      v = s->run_until(t);
    }
    py::list l;
    for (const auto& c : v) l.append(completion_dict(c));
    return l;
  }

  py::object trace_drain(bool as_numpy) {
    std::vector<TraceRec> recs = s->trace_drain();
    auto tasks = s->task_names();
    if (as_numpy) {
      py::module_ np = py::module_::import("numpy");
      py::list fields;
      auto f = [&](const char* n, const char* t) { fields.append(py::make_tuple(n, t)); };
      f("t_ps", "<i8"); f("dur_ps", "<i8"); f("kind", "u1"); f("block", "u1"); f("unit", "<u2"); f("task", "<i4");
      f("job_id", "<i8"); f("op", "<i4"); f("src_x", "i1"); f("src_y", "i1"); f("dst_x", "i1"); f("dst_y", "i1");
      f("bytes", "<i8"); f("value", "<f8");
      py::object dt = np.attr("dtype")(fields);
      py::object arr = np.attr("zeros")(recs.size(), dt);
      if (!recs.empty()) {
        py::buffer_info bi = arr.cast<py::array>().request(true);
        auto* base = static_cast<uint8_t*>(bi.ptr);
        py::object itemsize = dt.attr("itemsize");
        size_t stride = itemsize.cast<size_t>();
        py::dict offs = dt.attr("fields");
        auto off = [&](const char* n) { return py::cast<py::tuple>(offs[n])[1].cast<size_t>(); };
        size_t o_t = off("t_ps"), o_d = off("dur_ps"), o_k = off("kind"), o_b = off("block"), o_u = off("unit"), o_ta = off("task"),
               o_j = off("job_id"), o_o = off("op"), o_sx = off("src_x"), o_sy = off("src_y"), o_dx = off("dst_x"), o_dy = off("dst_y"),
               o_by = off("bytes"), o_v = off("value");
        for (size_t i = 0; i < recs.size(); ++i) {
          uint8_t* p = base + i * stride;
          const TraceRec& r = recs[i];
          std::memcpy(p + o_t, &r.t, 8);
          std::memcpy(p + o_d, &r.dur, 8);
          p[o_k] = r.kind;
          p[o_b] = r.block;
          std::memcpy(p + o_u, &r.unit, 2);
          std::memcpy(p + o_ta, &r.task, 4);
          std::memcpy(p + o_j, &r.job, 8);
          std::memcpy(p + o_o, &r.op, 4);
          p[o_sx] = static_cast<uint8_t>(r.sx);
          p[o_sy] = static_cast<uint8_t>(r.sy);
          p[o_dx] = static_cast<uint8_t>(r.dx);
          p[o_dy] = static_cast<uint8_t>(r.dy);
          std::memcpy(p + o_by, &r.bytes, 8);
          std::memcpy(p + o_v, &r.value, 8);
        }
      }
      return arr;
    }
    std::vector<std::vector<std::string>> ops(tasks.size());
    for (size_t t = 0; t < tasks.size(); ++t) ops[t] = s->op_names(static_cast<int>(t));
    py::list l;
    for (const auto& r : recs) {
      py::dict d;
      d["t_ps"] = r.t;
      d["dur_ps"] = r.dur;
      d["kind"] = trace_kind_name(r.kind);
      d["block"] = trace_block_name(r.block);
      d["unit"] = r.unit;
      d["task_id"] = (r.task >= 0 && static_cast<size_t>(r.task) < tasks.size()) ? py::str(tasks[r.task]) : py::str("");
      d["job_id"] = r.job;
      std::string op;
      if (r.task >= 0 && static_cast<size_t>(r.task) < ops.size() && r.op >= 0 && static_cast<size_t>(r.op) < ops[r.task].size())
        op = ops[r.task][r.op];
      d["op_id"] = op;
      if (r.kind == TK_XFER) {
        d["src_node"] = py::make_tuple(r.sx, r.sy);
        d["dst_node"] = py::make_tuple(r.dx, r.dy);
      } else {
        d["src_node"] = py::none();
        d["dst_node"] = py::none();
      }
      if (r.kind == TK_OP) {
        uint32_t mask = static_cast<uint8_t>(r.sx) | (static_cast<uint32_t>(static_cast<uint8_t>(r.sy)) << 8) |
                        (static_cast<uint32_t>(static_cast<uint8_t>(r.dx)) << 16) | (static_cast<uint32_t>(static_cast<uint8_t>(r.dy)) << 24);
        py::list us;
        for (int b = 0; b < 32; ++b)
          if (mask & (1u << b)) us.append(b);
        d["units"] = us;
      }
      d["bytes"] = r.bytes;
      d["value"] = r.value;
      l.append(d);
    }
    return l;
  }
};

PYBIND11_MODULE(glasschip_core, m) {
  m.doc() = "Glass Chip simulation kernel: deterministic multi-fidelity discrete-event SoC model.";
  m.attr("__version__") = "0.1.0";
  m.attr("ATTRIBUTION_KEYS") = py::make_tuple("compute", "runqueue", "engine_queue", "noc", "dram", "cache", "dma", "preemption", "dvfs",
                                              "middleware");
  py::class_<PySim>(m, "Sim")
      .def(py::init<py::dict, py::dict, uint64_t>(), py::arg("soc"), py::arg("fidelity"), py::arg("seed") = 0,
           "Create a simulator from SoC and fidelity dicts (parsed YAML) and a master seed.")
      .def("add_task", [](PySim& p, py::dict g) { p.s->add_task(to_json(g)); }, py::arg("graph"), "Add a task graph (dict).")
      .def(
          "set_middleware",
          [](PySim& p, py::dict params, const std::string& transport, const std::string& executor) {
            p.s->set_middleware(to_json(params), transport, executor);
          },
          py::arg("params"), py::arg("transport") = "copy", py::arg("executor") = "single_threaded",
          "Enable the ROS 2-style middleware model (transport copy|zero_copy, executor single_threaded|multi_threaded|events).")
      .def("set_scheduler", [](PySim& p, const std::string& e, const std::string& pol) { p.s->set_scheduler(e, pol); },
           py::arg("engine"), py::arg("policy"), "Per-engine scheduler: fifo | fixed_priority | edf | edf_mc (engine 'all' sets every engine).")
      .def("set_qos", [](PySim& p, bool on) { p.s->set_qos(on); }, py::arg("enabled"), "NoC and DRAM priority arbitration by qos_class.")
      .def(
          "set_cache_partition",
          [](PySim& p, py::object ways) {
            if (ways.is_none()) {
              p.s->set_cache_partition(nullptr);
              return;
            }
            std::map<std::string, int> m2;
            for (auto kv : ways.cast<py::dict>()) m2[py::str(kv.first).cast<std::string>()] = kv.second.cast<int>();
            p.s->set_cache_partition(&m2);
          },
          py::arg("ways_by_qos"), "Way partitioning of the system cache per QoS class, or None to disable.")
      .def("set_mapping", [](PySim& p, const std::string& pol) { p.s->set_mapping(pol); }, py::arg("policy"),
           "Op-to-engine mapping: 'static' (first eligible + affinity) or 'eft' (greedy earliest finish time).")
      .def("set_task_priority", [](PySim& p, const std::string& t, int64_t pr) { p.s->set_task_priority(t, pr); }, py::arg("task_id"),
           py::arg("priority"), "Override the fixed-priority key of a task (lower runs first).")
      .def("set_params", [](PySim& p, py::dict d) { p.s->set_params(to_json(d)); }, py::arg("params"),
           "Override model parameters (same keys as the fidelity yaml `params:`).")
      .def("set_l2_tables", [](PySim& p, py::dict d) { p.s->set_l2_tables(to_json(d)); }, py::arg("tables"),
           "L2 side tables: {'npu_cycles': {'task/op' or 'op': cycles}, 'cpu_cpi': {'op_id' or op type: cpi}}.")
      .def("release_job", [](PySim& p, const std::string& t, int64_t tps, int64_t tag) { return p.s->release_job(t, tps, tag); },
           py::arg("task_id"), py::arg("t_ps"), py::arg("tag") = 0, "Release a job; returns job id or -1 if skipped by on_overrun=skip.")
      .def("run_until", &PySim::run_until, py::arg("t_ps"), "Advance to t_ps; returns completed jobs since the last call.")
      .def("pending_jobs",
           [](PySim& p) {
             py::list l;
             for (const auto& j : p.s->pending_jobs()) {
               py::dict d;
               d["job_id"] = j.job_id;
               d["task_id"] = j.task_id;
               d["release_ps"] = j.release;
               d["deadline_ps"] = j.deadline;
               d["started"] = j.started;
               d["frac_done"] = j.frac_done;
               py::dict e;
               for (int k = 0; k < N_DOMAINS; ++k)
                 if (j.est_remaining_cycles[k] > 0) e[domain_name(k)] = j.est_remaining_cycles[k];
               d["est_remaining_cycles"] = e;
               l.append(d);
             }
             return l;
           },
           "Unfinished jobs with progress and estimated remaining cycles per power domain.")
      .def("set_domain_freq", [](PySim& p, const std::string& d, double hz) { p.s->set_domain_freq(d, hz); }, py::arg("domain"),
           py::arg("freq_hz"), "Change a power domain's clock; the domain stalls for the DVFS transition latency.")
      .def("domain_freq", [](PySim& p, const std::string& d) { return p.s->domain_freq(d); }, py::arg("domain"))
      .def(
          "counters",
          [](PySim& p, bool reset) {
            auto cs = p.s->counters(reset);
            py::dict out;
            for (int d = 0; d < N_DOMAINS; ++d) {
              py::dict c;
              c["busy_ps"] = cs[d].busy;
              c["idle_ps"] = cs[d].idle;
              c["unit_busy_ps"] = cs[d].unit_busy;
              c["units"] = cs[d].units;
              for (int k = 0; k < N_ACT; ++k) c[act_name(k)] = cs[d].act[k];
              out[domain_name(d)] = c;
            }
            return out;
          },
          py::arg("reset") = true, "Per power domain busy/idle time and activity counts since the last reset.")
      .def(
          "queue_depths",
          [](PySim& p, int top_n) {
            auto q = p.s->queue_depths(top_n);
            py::dict d;
            d["dram"] = q.dram_ctrl;
            d["dram_channels"] = q.dram_ch;
            py::list links;
            for (const auto& l : q.noc_links) {
              py::dict x;
              x["link"] = l.first;
              x["backlog_ns"] = l.second;
              links.append(x);
            }
            d["noc"] = links;
            py::dict er, eb;
            for (int e = 0; e < N_ENGINES; ++e) {
              er[engine_name(e)] = q.engine_ready[e];
              eb[engine_name(e)] = q.engine_busy[e];
            }
            d["engines"] = er;
            d["engines_busy"] = eb;
            return d;
          },
          py::arg("top_n") = 8, "Instantaneous queue depths: DRAM per controller/channel, busiest NoC links, engine run queues.")
      .def_property_readonly("now_ps", [](PySim& p) { return p.s->now(); })
      .def("trace_enable", [](PySim& p, bool on) { p.s->trace_enable(on); }, py::arg("enabled") = true)
      .def("trace_drain", &PySim::trace_drain, py::arg("as_numpy") = false,
           "Trace events since the last drain: list of dicts, or a numpy structured array with integer codes (see trace_names()).")
      .def("trace_names",
           [](PySim& p) {
             py::dict d;
             py::list kinds, blocks, tasks, ops;
             for (int k = 0; k < N_TK; ++k) kinds.append(trace_kind_name(k));
             for (int b = 0; b < N_TB; ++b) blocks.append(trace_block_name(b));
             auto tn = p.s->task_names();
             for (size_t t = 0; t < tn.size(); ++t) {
               tasks.append(tn[t]);
               ops.append(p.s->op_names(static_cast<int>(t)));
             }
             d["kinds"] = kinds;
             d["blocks"] = blocks;
             d["tasks"] = tasks;
             d["ops"] = ops;
             return d;
           },
           "String tables for decoding numpy trace records.")
      .def("trace_hash", [](PySim& p) { return p.s->trace_hash(); }, "Hash of the full event stream so far (determinism check).")
      .def("trace_open", [](PySim& p, const std::string& path) { return p.s->trace_open(path); }, py::arg("path"),
           "Stream every trace record to a compact binary .gct file.")
      .def("trace_close", [](PySim& p) { p.s->trace_close(); })
      .def("checkpoint",
           [](PySim& p) {
             auto b = p.s->checkpoint();
             return py::bytes(reinterpret_cast<const char*>(b.data()), b.size());
           },
           "Serialize the full simulator state to bytes.")
      .def(
          "restore",
          [](PySim& p, py::bytes blob) {
            std::string s = blob;
            std::vector<uint8_t> v(s.begin(), s.end());
            p.s->restore(v);
          },
          py::arg("blob"), "Restore state saved by checkpoint().")
      .def("info", [](PySim& p) { return from_json(p.s->info()); }, "Fidelity levels in use, warnings, units, policies, counters.")
      .def("npu_gemm_cycles",
           [](PySim& p, double M, double K, double N, double B, int tiles, const std::string& dtype) {
             int dt = dtype == "bf16" ? DT_BF16 : dtype == "fp8" ? DT_FP8 : dtype == "fp16" ? DT_FP16 : dtype == "fp32" ? DT_FP32 : DT_INT8;
             return p.s->npu_gemm_cycles(M, K, N, B, tiles, dt);
           },
           py::arg("M"), py::arg("K"), py::arg("N"), py::arg("B") = 1, py::arg("tiles") = 1, py::arg("dtype") = "int8",
           "Closed-form systolic GEMM cycles used by the NPU L1 model.");
}
