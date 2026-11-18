// y += a*x over N floats: RVV (LMUL=8) and scalar versions.
#include <riscv_vector.h>
#include "bench.h"
#define N 4096
static float x[N], y[N];

__attribute__((noinline)) void axpy_rvv(size_t n, float a, const float *xp, float *yp) {
  for (size_t vl; n > 0; n -= vl, xp += vl, yp += vl) {
    vl = __riscv_vsetvl_e32m8(n);
    vfloat32m8_t vx = __riscv_vle32_v_f32m8(xp, vl);
    vfloat32m8_t vy = __riscv_vle32_v_f32m8(yp, vl);
    vy = __riscv_vfmacc_vf_f32m8(vy, a, vx, vl);
    __riscv_vse32_v_f32m8(yp, vy, vl);
  }
}

__attribute__((noinline)) void axpy_scalar(size_t n, float a, const float *xp, float *yp) {
#pragma clang loop vectorize(disable) interleave(disable)
  for (size_t i = 0; i < n; ++i) yp[i] += a * xp[i];
}

int main(int argc, char **argv) {
  int iters = argc > 1 ? atoi(argv[1]) : 10;
  int scalar = argc > 2 && argv[2][0] == 's';
  for (int i = 0; i < N; ++i) { x[i] = (float)i * 1e-3f; y[i] = 1.0f; }
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  for (int it = 0; it < iters; ++it) {
    if (scalar) axpy_scalar(N, 0.5f, x, y); else axpy_rvv(N, 0.5f, x, y);
  }
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  BENCH_REPORT(scalar ? "axpy_scalar_4096" : "axpy_rvv_4096", iters, c0, c1, i0, i1, y[N - 1]);
  return 0;
}
