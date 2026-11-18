// T7 control step benchmark: scalar (workloads/capture/control/control.cpp) vs RVV.
//   ctrl_bench <scalar|rvv> <iters> [dump]
// Same inputs as calib/riscv/kernels/control_bench.cpp. With "dump" the last
// torques of every iteration are printed as raw double bits for comparison
// with the host reference.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "control.h"

extern "C" void control_step_rvv(const ControlIn* in, ControlState* st, double* tau_out);

#ifdef __riscv
static inline uint64_t rd_cycle() { uint64_t x; __asm__ volatile("rdcycle %0" : "=r"(x)); return x; }
static inline uint64_t rd_instret() { uint64_t x; __asm__ volatile("rdinstret %0" : "=r"(x)); return x; }
#else
static inline uint64_t rd_cycle() { return 0; }
static inline uint64_t rd_instret() { return 0; }
#endif

int main(int argc, char** argv) {
  if (argc < 3) { std::fprintf(stderr, "usage: ctrl_bench <scalar|rvv> <iters> [dump]\n"); return 2; }
  const bool rvv = !std::strcmp(argv[1], "rvv");
  const int iters = std::atoi(argv[2]);
  const bool dump = argc > 3 && !std::strcmp(argv[3], "dump");
  ControlIn in{};
  ControlState st{};
  st.alpha = 0.2;
  in.seg_T = 0.5; in.speed_scale = 1.0;
  for (int j = 0; j < 7; ++j) { in.q[j] = 0.1 * j; in.q0[j] = 0.0; in.q1[j] = 0.2 * j; in.kp[j] = 100; in.kd[j] = 10;
    in.dq[j] = 0.05 * j; in.v1[j] = 0.01 * j; }
  static double hist[4096][7];
  double tau[7], chk = 0;
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  for (int it = 0; it < iters; ++it) {
    in.phase = (it % 100) * 0.01;
    if (rvv) control_step_rvv(&in, &st, tau); else control_step(&in, &st, tau);
    chk += tau[1];
    if (it < 4096) std::memcpy(hist[it], tau, sizeof tau);
  }
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  std::printf("BENCH kernel=T7_control variant=%s iters=%d cycles=%llu instret=%llu check=%.12g\n", rvv ? "rvv" : "scalar",
              iters, (unsigned long long)(c1 - c0), (unsigned long long)(i1 - i0), chk);
  if (dump)
    for (int it = 0; it < iters && it < 4096; ++it)
      for (int j = 0; j < 7; ++j) { uint64_t b; std::memcpy(&b, &hist[it][j], 8); std::printf("D %d %016llx\n", it * 7 + j, (unsigned long long)b); }
  return 0;
}
