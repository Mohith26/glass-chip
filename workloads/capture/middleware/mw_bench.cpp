// T9 middleware micro-benchmark (fallback for spike V4: no Docker/ROS 2 here).
//
// Implements CDR (XCDR1, little-endian) serialization and deserialization for
// the message types this stack publishes, with ROS 2 message layouts:
// sensor_msgs/Image, sensor_msgs/PointCloud2, sensor_msgs/JointState,
// trajectory_msgs/JointTrajectory, std_msgs/Float32MultiArray (VLA action
// chunk) and a vision_msgs/Detection3DArray-like detection list.
// Also measures plain memcpy of the serialized buffer, a lock-free pointer
// hand-off (the CPU part of a zero-copy loaned-message publish), and a
// condition-variable thread wake-up (the floor of executor dispatch).
//
// Output: one JSON object on stdout.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

// ---------------- CDR writer / reader ----------------
struct Writer {
  std::vector<uint8_t> buf;
  size_t pos = 0;
  void reset() { pos = 4; std::memset(buf.data(), 0, 4); buf[1] = 1; }  // encapsulation: CDR_LE
  void ensure(size_t n) {
    if (pos + n > buf.size()) buf.resize((pos + n) * 2);
  }
  void align(size_t a) {
    size_t off = (pos - 4) % a;
    if (off) { ensure(a - off); std::memset(buf.data() + pos, 0, a - off); pos += a - off; }
  }
  template <class T> void put(T v) { align(sizeof(T)); ensure(sizeof(T)); std::memcpy(buf.data() + pos, &v, sizeof(T)); pos += sizeof(T); }
  void str(const std::string& s) {
    put<uint32_t>(static_cast<uint32_t>(s.size() + 1));
    ensure(s.size() + 1);
    std::memcpy(buf.data() + pos, s.c_str(), s.size() + 1);
    pos += s.size() + 1;
  }
  template <class T> void seq(const std::vector<T>& v) {
    put<uint32_t>(static_cast<uint32_t>(v.size()));
    if (v.empty()) return;
    align(sizeof(T));
    ensure(v.size() * sizeof(T));
    std::memcpy(buf.data() + pos, v.data(), v.size() * sizeof(T));
    pos += v.size() * sizeof(T);
  }
};

struct Reader {
  const uint8_t* b;
  size_t pos = 4;
  void align(size_t a) { size_t off = (pos - 4) % a; if (off) pos += a - off; }
  template <class T> T get() { align(sizeof(T)); T v; std::memcpy(&v, b + pos, sizeof(T)); pos += sizeof(T); return v; }
  std::string str() { uint32_t n = get<uint32_t>(); std::string s(reinterpret_cast<const char*>(b + pos), n ? n - 1 : 0); pos += n; return s; }
  template <class T> std::vector<T> seq() {
    uint32_t n = get<uint32_t>();
    std::vector<T> v(n);
    if (n) { align(sizeof(T)); std::memcpy(v.data(), b + pos, n * sizeof(T)); pos += n * sizeof(T); }
    return v;
  }
};

// ---------------- message types ----------------
struct Header { int32_t sec = 0; uint32_t nanosec = 0; std::string frame_id; };
void w(Writer& o, const Header& h) { o.put(h.sec); o.put(h.nanosec); o.str(h.frame_id); }
Header rh(Reader& r) { Header h; h.sec = r.get<int32_t>(); h.nanosec = r.get<uint32_t>(); h.frame_id = r.str(); return h; }

struct Image { Header header; uint32_t height = 0, width = 0; std::string encoding; uint8_t is_bigendian = 0; uint32_t step = 0; std::vector<uint8_t> data; };
void w(Writer& o, const Image& m) { w(o, m.header); o.put(m.height); o.put(m.width); o.str(m.encoding); o.put(m.is_bigendian); o.put(m.step); o.seq(m.data); }
Image r_image(Reader& r) { Image m; m.header = rh(r); m.height = r.get<uint32_t>(); m.width = r.get<uint32_t>(); m.encoding = r.str(); m.is_bigendian = r.get<uint8_t>(); m.step = r.get<uint32_t>(); m.data = r.seq<uint8_t>(); return m; }

struct PointField { std::string name; uint32_t offset; uint8_t datatype; uint32_t count; };
struct PointCloud2 { Header header; uint32_t height = 0, width = 0; std::vector<PointField> fields; uint8_t is_bigendian = 0; uint32_t point_step = 0, row_step = 0; std::vector<uint8_t> data; uint8_t is_dense = 0; };
void w(Writer& o, const PointCloud2& m) {
  w(o, m.header); o.put(m.height); o.put(m.width);
  o.put<uint32_t>(static_cast<uint32_t>(m.fields.size()));
  for (auto& f : m.fields) { o.str(f.name); o.put(f.offset); o.put(f.datatype); o.put(f.count); }
  o.put(m.is_bigendian); o.put(m.point_step); o.put(m.row_step); o.seq(m.data); o.put(m.is_dense);
}
PointCloud2 r_pc2(Reader& r) {
  PointCloud2 m; m.header = rh(r); m.height = r.get<uint32_t>(); m.width = r.get<uint32_t>();
  uint32_t n = r.get<uint32_t>();
  for (uint32_t i = 0; i < n; ++i) { PointField f; f.name = r.str(); f.offset = r.get<uint32_t>(); f.datatype = r.get<uint8_t>(); f.count = r.get<uint32_t>(); m.fields.push_back(f); }
  m.is_bigendian = r.get<uint8_t>(); m.point_step = r.get<uint32_t>(); m.row_step = r.get<uint32_t>(); m.data = r.seq<uint8_t>(); m.is_dense = r.get<uint8_t>();
  return m;
}

struct JointState { Header header; std::vector<std::string> name; std::vector<double> position, velocity, effort; };
void w(Writer& o, const JointState& m) {
  w(o, m.header); o.put<uint32_t>(static_cast<uint32_t>(m.name.size())); for (auto& s : m.name) o.str(s);
  o.seq(m.position); o.seq(m.velocity); o.seq(m.effort);
}
JointState r_js(Reader& r) {
  JointState m; m.header = rh(r); uint32_t n = r.get<uint32_t>(); for (uint32_t i = 0; i < n; ++i) m.name.push_back(r.str());
  m.position = r.seq<double>(); m.velocity = r.seq<double>(); m.effort = r.seq<double>(); return m;
}

struct TrajPoint { std::vector<double> pos, vel, acc, eff; int32_t sec = 0; uint32_t nsec = 0; };
struct JointTrajectory { Header header; std::vector<std::string> joint_names; std::vector<TrajPoint> points; };
void w(Writer& o, const JointTrajectory& m) {
  w(o, m.header); o.put<uint32_t>(static_cast<uint32_t>(m.joint_names.size())); for (auto& s : m.joint_names) o.str(s);
  o.put<uint32_t>(static_cast<uint32_t>(m.points.size()));
  for (auto& p : m.points) { o.seq(p.pos); o.seq(p.vel); o.seq(p.acc); o.seq(p.eff); o.put(p.sec); o.put(p.nsec); }
}
JointTrajectory r_jt(Reader& r) {
  JointTrajectory m; m.header = rh(r); uint32_t n = r.get<uint32_t>(); for (uint32_t i = 0; i < n; ++i) m.joint_names.push_back(r.str());
  uint32_t np = r.get<uint32_t>(); m.points.resize(np);
  for (auto& p : m.points) { p.pos = r.seq<double>(); p.vel = r.seq<double>(); p.acc = r.seq<double>(); p.eff = r.seq<double>(); p.sec = r.get<int32_t>(); p.nsec = r.get<uint32_t>(); }
  return m;
}

struct Dim { std::string label; uint32_t size, stride; };
struct F32MultiArray { std::vector<Dim> dims; uint32_t data_offset = 0; std::vector<float> data; };
void w(Writer& o, const F32MultiArray& m) {
  o.put<uint32_t>(static_cast<uint32_t>(m.dims.size())); for (auto& d : m.dims) { o.str(d.label); o.put(d.size); o.put(d.stride); }
  o.put(m.data_offset); o.seq(m.data);
}
F32MultiArray r_fma(Reader& r) {
  F32MultiArray m; uint32_t n = r.get<uint32_t>(); for (uint32_t i = 0; i < n; ++i) { Dim d; d.label = r.str(); d.size = r.get<uint32_t>(); d.stride = r.get<uint32_t>(); m.dims.push_back(d); }
  m.data_offset = r.get<uint32_t>(); m.data = r.seq<float>(); return m;
}

struct Det { Header header; std::string class_id; double score; double pose[7]; double cov[36]; double center[7]; double size[3]; std::string id; };
struct DetArray { Header header; std::vector<Det> dets; };
void w(Writer& o, const DetArray& m) {
  w(o, m.header); o.put<uint32_t>(static_cast<uint32_t>(m.dets.size()));
  for (auto& d : m.dets) {
    w(o, d.header); o.put<uint32_t>(1); o.str(d.class_id); o.put(d.score);
    for (double v : d.pose) o.put(v); for (double v : d.cov) o.put(v); for (double v : d.center) o.put(v); for (double v : d.size) o.put(v);
    o.str(d.id);
  }
}
DetArray r_det(Reader& r) {
  DetArray m; m.header = rh(r); uint32_t n = r.get<uint32_t>(); m.dets.resize(n);
  for (auto& d : m.dets) {
    d.header = rh(r); r.get<uint32_t>(); d.class_id = r.str(); d.score = r.get<double>();
    for (double& v : d.pose) v = r.get<double>(); for (double& v : d.cov) v = r.get<double>();
    for (double& v : d.center) v = r.get<double>(); for (double& v : d.size) v = r.get<double>(); d.id = r.str();
  }
  return m;
}

volatile uint64_t sink = 0;

// Median of `reps` samples; each sample times a batch of calls long enough
// (>= 20 us) that the ~42 ns macOS steady_clock tick does not matter.
template <class F> double median_ns(F&& f, int reps) {
  int batch = 1;
  for (;;) {
    auto a = Clock::now();
    for (int k = 0; k < batch; ++k) f();
    double dt = std::chrono::duration<double, std::nano>(Clock::now() - a).count();
    if (dt >= 20000.0 || batch >= (1 << 20)) break;
    batch *= 2;
  }
  std::vector<double> t(reps);
  for (int i = 0; i < reps; ++i) {
    auto a = Clock::now();
    for (int k = 0; k < batch; ++k) f();
    t[i] = std::chrono::duration<double, std::nano>(Clock::now() - a).count() / batch;
  }
  std::sort(t.begin(), t.end());
  return t[reps / 2];
}

template <class M, class R> void bench(const char* name, const M& msg, R reader, bool first) {
  Writer wr;
  wr.buf.resize(1 << 12);
  wr.reset(); w(wr, msg);
  const size_t bytes = wr.pos;
  const int reps = bytes > (1 << 20) ? 101 : 301;
  double ser = median_ns([&] { wr.reset(); w(wr, msg); sink += wr.pos; }, reps);
  double de = median_ns([&] { Reader rd{wr.buf.data()}; auto m = reader(rd); sink += rd.pos; (void)m; }, reps);
  std::vector<uint8_t> dst(bytes);
  double cp = median_ns([&] { std::memcpy(dst.data(), wr.buf.data(), bytes); sink += dst[bytes / 2]; }, reps);
  std::printf("%s\n    {\"name\": \"%s\", \"bytes\": %zu, \"serialize_ns\": %.1f, \"deserialize_ns\": %.1f, \"memcpy_ns\": %.1f, \"reps\": %d}",
              first ? "" : ",", name, bytes, ser, de, cp, reps);
}

}  // namespace

int main() {
  Header h{1, 2, "camera_wrist_optical_frame"};
  Image rgb{h, 480, 640, "rgb8", 0, 640 * 3, std::vector<uint8_t>(640 * 480 * 3, 7)};
  Image depth{h, 480, 640, "16UC1", 0, 640 * 2, std::vector<uint8_t>(640 * 480 * 2, 3)};
  PointCloud2 pc{h, 480, 640, {{"x", 0, 7, 1}, {"y", 4, 7, 1}, {"z", 8, 7, 1}, {"rgb", 12, 7, 1}}, 0, 16, 640 * 16,
                 std::vector<uint8_t>(640 * 480 * 16, 1), 0};
  std::vector<std::string> jn;
  for (int i = 1; i <= 7; ++i) jn.push_back("panda_joint" + std::to_string(i));
  JointState js{h, jn, std::vector<double>(7, 0.1), std::vector<double>(7, 0.2), std::vector<double>(7, 0.3)};
  JointTrajectory jt{h, jn, {}};
  for (int i = 0; i < 100; ++i) jt.points.push_back({std::vector<double>(7, 0.1), std::vector<double>(7, 0.2), std::vector<double>(7, 0.3), {}, 0, static_cast<uint32_t>(i * 10000000)});
  F32MultiArray ac{{{"chunk", 50, 400}, {"action", 8, 8}}, 0, std::vector<float>(400, 0.5f)};
  DetArray da{h, {}};
  for (int i = 0; i < 8; ++i) { Det d{}; d.header = h; d.class_id = "object"; d.score = 0.9; d.id = "trk" + std::to_string(i); da.dets.push_back(d); }

  std::printf("{\n  \"messages\": [");
  bench("JointState", js, r_js, true);
  bench("Float32MultiArray_action_chunk", ac, r_fma, false);
  bench("Detection3DArray_8", da, r_det, false);
  bench("JointTrajectory_100pts", jt, r_jt, false);
  bench("Image_16UC1_640x480", depth, r_image, false);
  bench("Image_rgb8_640x480", rgb, r_image, false);
  bench("PointCloud2_640x480_xyzrgb", pc, r_pc2, false);
  std::printf("\n  ],\n");

  // Zero-copy CPU path: refcount + pointer through a single-producer/single-consumer ring.
  std::atomic<uint64_t> ring[64];
  std::atomic<uint32_t> head{0}, tail{0}, refc{0};
  uint64_t payload = 42;
  double handoff = median_ns([&] {
    refc.fetch_add(1, std::memory_order_acq_rel);
    uint32_t hd = head.load(std::memory_order_relaxed);
    ring[hd & 63].store(reinterpret_cast<uint64_t>(&payload), std::memory_order_release);
    head.store(hd + 1, std::memory_order_release);
    uint32_t tl = tail.load(std::memory_order_relaxed);
    sink += ring[tl & 63].load(std::memory_order_acquire);
    tail.store(tl + 1, std::memory_order_release);
    refc.fetch_sub(1, std::memory_order_acq_rel);
  }, 301);

  // Thread wake-up through a condition variable (round trip / 2).
  std::mutex mu;
  std::condition_variable cv;
  int turn = 0;
  bool stop = false;
  std::thread peer([&] {
    std::unique_lock<std::mutex> lk(mu);
    while (!stop) {
      cv.wait(lk, [&] { return turn == 1 || stop; });
      if (stop) break;
      turn = 0;
      cv.notify_all();
    }
  });
  double rt = median_ns([&] {
    std::unique_lock<std::mutex> lk(mu);
    turn = 1;
    cv.notify_all();
    cv.wait(lk, [&] { return turn == 0; });
  }, 301);
  {
    std::lock_guard<std::mutex> lk(mu);
    stop = true;
  }
  cv.notify_all();
  peer.join();
  std::printf("  \"zero_copy_handoff_ns\": %.1f,\n  \"thread_wakeup_ns\": %.1f,\n  \"sink\": %llu\n}\n", handoff, rt / 2,
              static_cast<unsigned long long>(sink % 10));
  return 0;
}
