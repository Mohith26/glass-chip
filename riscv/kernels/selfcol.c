// Sphere-sphere self-collision cost over cuRobo's 694 checked Franka sphere pairs.
// Per configuration: sum over pairs of max(0, R_ab - |ca - cb|), where R_ab = ra + rb plus
// cuRobo's per-link self_collision_buffer (precomputed per pair in PAIR_R).
// The RVV version vectorizes over pairs with indexed gathers and keeps per-lane
// partial sums, then reduces once per configuration (vfredusum), so its sums
// are reordered relative to the scalar loop.
#include <math.h>

#include "gc_kern.h"

// Activation distance added to every pair (assumption). With the cuRobo buffers alone no
// pair on these trajectories is closer than 8.5 cm, so every output would be 0; 0.1 m makes
// the hinge active for some pairs so the outputs check the arithmetic. It does not change the
// instruction count (the kernels are branch-free).
#define SELF_ETA 0.1f

#ifdef __riscv_vector
#include <riscv_vector.h>
#endif

void selfcol_scalar(const float* spheres, int n, float* cost) {
  for (int c = 0; c < n; ++c) {
    const float* s = spheres + (long)c * GC_NSPH * 4;
    float acc = 0.0f;
    for (int p = 0; p < GC_NPAIR; ++p) {
      const float* a = s + 4 * PAIR_A[p];
      const float* b = s + 4 * PAIR_B[p];
      float dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
      float d = sqrtf(dx * dx + dy * dy + dz * dz);
      float pen = (PAIR_R[p] + SELF_ETA) - d;
      acc += fmaxf(pen, 0.0f);
    }
    cost[c] = acc;
  }
}

#ifdef __riscv_vector
static uint32_t OFF_A[GC_NPAIR], OFF_B[GC_NPAIR];
static int off_ready = 0;

void selfcol_rvv(const float* spheres, int n, float* cost) {
  if (!off_ready) {  // byte offsets of each pair's spheres (one-time setup)
    for (int p = 0; p < GC_NPAIR; ++p) { OFF_A[p] = 16u * PAIR_A[p]; OFF_B[p] = 16u * PAIR_B[p]; }
    off_ready = 1;
  }
  const size_t vlmax = __riscv_vsetvlmax_e32m2();
  for (int c = 0; c < n; ++c) {
    const float* s = spheres + (long)c * GC_NSPH * 4;
    vfloat32m2_t acc = __riscv_vfmv_v_f_f32m2(0.0f, vlmax);
    for (int p = 0; p < GC_NPAIR;) {
      size_t vl = __riscv_vsetvl_e32m2((size_t)(GC_NPAIR - p));
      vuint32m2_t oa = __riscv_vle32_v_u32m2(OFF_A + p, vl);
      vuint32m2_t ob = __riscv_vle32_v_u32m2(OFF_B + p, vl);
      vfloat32m2_t dx = __riscv_vfsub_vv_f32m2(__riscv_vluxei32_v_f32m2(s, oa, vl), __riscv_vluxei32_v_f32m2(s, ob, vl), vl);
      vfloat32m2_t dy = __riscv_vfsub_vv_f32m2(__riscv_vluxei32_v_f32m2(s + 1, oa, vl), __riscv_vluxei32_v_f32m2(s + 1, ob, vl), vl);
      vfloat32m2_t dz = __riscv_vfsub_vv_f32m2(__riscv_vluxei32_v_f32m2(s + 2, oa, vl), __riscv_vluxei32_v_f32m2(s + 2, ob, vl), vl);
      vfloat32m2_t rr = __riscv_vfadd_vf_f32m2(__riscv_vle32_v_f32m2(PAIR_R + p, vl), SELF_ETA, vl);
      vfloat32m2_t d2 = __riscv_vfadd_vv_f32m2(__riscv_vfadd_vv_f32m2(__riscv_vfmul_vv_f32m2(dx, dx, vl), __riscv_vfmul_vv_f32m2(dy, dy, vl), vl),
                                               __riscv_vfmul_vv_f32m2(dz, dz, vl), vl);
      vfloat32m2_t pen = __riscv_vfsub_vv_f32m2(rr, __riscv_vfsqrt_v_f32m2(d2, vl), vl);
      // tail-undisturbed add so lanes beyond vl keep their partial sums
      acc = __riscv_vfadd_vv_f32m2_tu(acc, acc, __riscv_vfmax_vf_f32m2(pen, 0.0f, vl), vl);
      p += (int)vl;
    }
    vfloat32m1_t z = __riscv_vfmv_v_f_f32m1(0.0f, 1);
    cost[c] = __riscv_vfmv_f_s_f32m1_f32(__riscv_vfredusum_vs_f32m2_f32m1(acc, z, vlmax));
  }
}
#else
void selfcol_rvv(const float* spheres, int n, float* cost) { selfcol_scalar(spheres, n, cost); }
#endif
