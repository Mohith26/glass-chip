// C[MxN] += A[MxK] * B[KxN], fp32, row-major. RVV kernel vectorizes along N
// (one row of C at a time, broadcast A[i][k], LMUL=4); scalar reference for comparison.
#include <riscv_vector.h>
#include "bench.h"
#define M 64
#define K 64
#define NN 64
static float A[M * K], B[K * NN], C[M * NN];

__attribute__((noinline)) void gemm_rvv(void) {
  for (int i = 0; i < M; ++i) {
    for (size_t j = 0, vl; j < NN; j += vl) {
      vl = __riscv_vsetvl_e32m4(NN - j);
      vfloat32m4_t acc = __riscv_vle32_v_f32m4(&C[i * NN + j], vl);
      for (int k = 0; k < K; ++k) {
        vfloat32m4_t b = __riscv_vle32_v_f32m4(&B[k * NN + j], vl);
        acc = __riscv_vfmacc_vf_f32m4(acc, A[i * K + k], b, vl);
      }
      __riscv_vse32_v_f32m4(&C[i * NN + j], acc, vl);
    }
  }
}

__attribute__((noinline)) void gemm_scalar(void) {
  for (int i = 0; i < M; ++i)
    for (int k = 0; k < K; ++k) {
      float a = A[i * K + k];
#pragma clang loop vectorize(disable) interleave(disable)
      for (int j = 0; j < NN; ++j) C[i * NN + j] += a * B[k * NN + j];
    }
}

int main(int argc, char **argv) {
  int iters = argc > 1 ? atoi(argv[1]) : 2;
  int scalar = argc > 2 && argv[2][0] == 's';
  for (int i = 0; i < M * K; ++i) A[i] = (float)(i % 7) * 0.25f;
  for (int i = 0; i < K * NN; ++i) B[i] = (float)(i % 5) * 0.5f;
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  for (int it = 0; it < iters; ++it) { if (scalar) gemm_scalar(); else gemm_rvv(); }
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  BENCH_REPORT(scalar ? "gemm_scalar_64" : "gemm_rvv_64", iters, c0, c1, i0, i1, C[M * NN - 1]);
  return 0;
}
