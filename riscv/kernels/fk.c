// Batched forward kinematics of the Franka Panda to its 61 cuRobo collision
// spheres (world center + radius), scalar and RVV versions.
//
// Kinematics: modified DH (Craig), same table as workloads/capture/control/control.cpp.
// Hand: flange 0.107 m along z7, rotated -pi/4 about z. Fingers: fixed open
// gripper, 0.04 m either side along the hand y axis, 0.0584 m along hand z; the
// right finger frame is rotated pi about z as in the Panda URDF.
// sin/cos use a half-angle Taylor polynomial (only + and *), so scalar, RVV and
// host runs compute identical values; its error versus libm is checked on the host.
#include "gc_kern.h"

#ifdef __riscv_vector
#include <riscv_vector.h>
#endif

#define NJ 7
#define NF 11  // frames: link0..link7, hand, left finger, right finger

static const float DH_A[NJ] = {0.0f, 0.0f, 0.0f, 0.0825f, -0.0825f, 0.0f, 0.088f};
static const float DH_D[NJ] = {0.333f, 0.0f, 0.316f, 0.0f, 0.384f, 0.0f, 0.0f};
static const float DH_CA[NJ] = {1, 0, 0, 0, 0, 0, 0};
static const float DH_SA[NJ] = {0, -1, 1, 1, -1, 1, 1};
#define FLANGE_D 0.107f
#define C45 0.70710678f
#define FINGER_Y 0.04f
#define FINGER_Z 0.0584f

static inline void sincos_f(float x, float* s_out, float* c_out) {
  float h = 0.5f * x;
  float h2 = h * h;
  float s = h * (1.0f + h2 * (-1.0f / 6 + h2 * (1.0f / 120 + h2 * (-1.0f / 5040 + h2 * (1.0f / 362880)))));
  float c = 1.0f + h2 * (-0.5f + h2 * (1.0f / 24 + h2 * (-1.0f / 720 + h2 * (1.0f / 40320 + h2 * (-1.0f / 3628800)))));
  *s_out = 2.0f * s * c;
  *c_out = 1.0f - 2.0f * s * s;
}

// Frame = row-major R (9) + p (3).
typedef struct {
  float r[9], p[3];
} Frame;

// child = parent * [Rl | pl]
static inline void compose(const Frame* a, const float* rl, const float* pl, Frame* out) {
  for (int i = 0; i < 3; ++i) {
    for (int j = 0; j < 3; ++j)
      out->r[3 * i + j] = a->r[3 * i + 0] * rl[0 + j] + a->r[3 * i + 1] * rl[3 + j] + a->r[3 * i + 2] * rl[6 + j];
    out->p[i] = a->p[i] + (a->r[3 * i + 0] * pl[0] + a->r[3 * i + 1] * pl[1] + a->r[3 * i + 2] * pl[2]);
  }
}

static void frames_scalar(const float* q, Frame* f) {
  for (int k = 0; k < 9; ++k) f[0].r[k] = (k % 4 == 0) ? 1.0f : 0.0f;
  f[0].p[0] = f[0].p[1] = f[0].p[2] = 0.0f;
  for (int i = 0; i < NJ; ++i) {
    float s, c;
    sincos_f(q[i], &s, &c);
    const float ca = DH_CA[i], sa = DH_SA[i];
    const float rl[9] = {c, -s, 0.0f, s * ca, c * ca, -sa, s * sa, c * sa, ca};
    const float pl[3] = {DH_A[i], -sa * DH_D[i], ca * DH_D[i]};
    compose(&f[i], rl, pl, &f[i + 1]);
  }
  const float rh[9] = {C45, C45, 0.0f, -C45, C45, 0.0f, 0.0f, 0.0f, 1.0f};  // Rz(-pi/4)
  const float ph[3] = {0.0f, 0.0f, FLANGE_D};
  compose(&f[7], rh, ph, &f[8]);
  const float id[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};
  const float rz180[9] = {-1, 0, 0, 0, -1, 0, 0, 0, 1};
  const float pl[3] = {0.0f, FINGER_Y, FINGER_Z};
  const float pr[3] = {0.0f, -FINGER_Y, FINGER_Z};
  compose(&f[8], id, pl, &f[9]);
  compose(&f[8], rz180, pr, &f[10]);
}

void fk_spheres_scalar(const float* q, int n, float* out) {
  Frame f[NF];
  for (int c = 0; c < n; ++c) {
    frames_scalar(q + 7 * c, f);
    float* o = out + (long)c * GC_NSPH * 4;
    for (int s = 0; s < GC_NSPH; ++s) {
      const Frame* fr = &f[SPH_LINK[s]];
      const float* l = SPH_LOCAL[s];
      for (int i = 0; i < 3; ++i)
        o[4 * s + i] = fr->p[i] + (fr->r[3 * i + 0] * l[0] + fr->r[3 * i + 1] * l[1] + fr->r[3 * i + 2] * l[2]);
      o[4 * s + 3] = l[3];
    }
  }
}

#ifdef __riscv_vector
// Vectorized across configurations: each lane is one joint configuration.
// RVV register types cannot live in arrays, so frames are kept in a small
// lane-major scratch buffer: F[frame][element][lane].
typedef vfloat32m1_t vf;
#define LM 64  // max lanes per strip (VLEN up to 2048 at e32m1)
static float F[NF][12][LM];
static float RL[12][LM];

static inline vf ld(const float* p, size_t vl) { return __riscv_vle32_v_f32m1(p, vl); }
static inline void st(float* p, vf v, size_t vl) { __riscv_vse32_v_f32m1(p, v, vl); }

// child = parent * [rl | pl], rl/pl per lane (rows 0..8 = R, 9..11 = p of RL).
static void vcompose_vv(float (*a)[LM], float (*rl)[LM], float (*o)[LM], size_t vl) {
  for (int i = 0; i < 3; ++i) {
    vf a0 = ld(a[3 * i + 0], vl), a1 = ld(a[3 * i + 1], vl), a2 = ld(a[3 * i + 2], vl);
    for (int j = 0; j < 3; ++j) {
      vf t = __riscv_vfmul_vv_f32m1(a0, ld(rl[0 + j], vl), vl);
      t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vv_f32m1(a1, ld(rl[3 + j], vl), vl), vl);
      st(o[3 * i + j], __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vv_f32m1(a2, ld(rl[6 + j], vl), vl), vl), vl);
    }
    vf t = __riscv_vfmul_vv_f32m1(a0, ld(rl[9], vl), vl);
    t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vv_f32m1(a1, ld(rl[10], vl), vl), vl);
    t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vv_f32m1(a2, ld(rl[11], vl), vl), vl);
    st(o[9 + i], __riscv_vfadd_vv_f32m1(ld(a[9 + i], vl), t, vl), vl);
  }
}

// Same with a constant (scalar) local transform.
static void vcompose_vf(float (*a)[LM], const float* rl, const float* pl, float (*o)[LM], size_t vl) {
  for (int i = 0; i < 3; ++i) {
    vf a0 = ld(a[3 * i + 0], vl), a1 = ld(a[3 * i + 1], vl), a2 = ld(a[3 * i + 2], vl);
    for (int j = 0; j < 3; ++j) {
      vf t = __riscv_vfmul_vf_f32m1(a0, rl[0 + j], vl);
      t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(a1, rl[3 + j], vl), vl);
      st(o[3 * i + j], __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(a2, rl[6 + j], vl), vl), vl);
    }
    vf t = __riscv_vfmul_vf_f32m1(a0, pl[0], vl);
    t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(a1, pl[1], vl), vl);
    t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(a2, pl[2], vl), vl);
    st(o[9 + i], __riscv_vfadd_vv_f32m1(ld(a[9 + i], vl), t, vl), vl);
  }
}

static inline vf vpoly(vf h2, const float* cf, int n, size_t vl) {
  // Horner from the innermost coefficient, same association as sincos_f.
  vf acc = __riscv_vfmv_v_f_f32m1(cf[n - 1], vl);
  for (int k = n - 2; k >= 0; --k) acc = __riscv_vfadd_vf_f32m1(__riscv_vfmul_vv_f32m1(h2, acc, vl), cf[k], vl);
  return acc;
}

void fk_spheres_rvv(const float* q, int n, float* out) {
  static const float SC[5] = {1.0f, -1.0f / 6, 1.0f / 120, -1.0f / 5040, 1.0f / 362880};
  static const float CC[6] = {1.0f, -0.5f, 1.0f / 24, -1.0f / 720, 1.0f / 40320, -1.0f / 3628800};
  static const float RH[9] = {C45, C45, 0.0f, -C45, C45, 0.0f, 0.0f, 0.0f, 1.0f};
  static const float ID[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};
  static const float RZ[9] = {-1, 0, 0, 0, -1, 0, 0, 0, 1};
  static const float PH[3] = {0.0f, 0.0f, FLANGE_D};
  static const float PLF[3] = {0.0f, FINGER_Y, FINGER_Z};
  static const float PRF[3] = {0.0f, -FINGER_Y, FINGER_Z};
  const ptrdiff_t qstride = 7 * sizeof(float);
  const ptrdiff_t ostride = GC_NSPH * 4 * sizeof(float);
  for (int c0 = 0; c0 < n;) {
    size_t vl = __riscv_vsetvl_e32m1((size_t)(n - c0));
    if (vl > LM) vl = __riscv_vsetvl_e32m1(LM);
    vf one = __riscv_vfmv_v_f_f32m1(1.0f, vl), zero = __riscv_vfmv_v_f_f32m1(0.0f, vl);
    for (int k = 0; k < 12; ++k) st(F[0][k], (k < 9 && k % 4 == 0) ? one : zero, vl);
    for (int i = 0; i < NJ; ++i) {
      vf x = __riscv_vlse32_v_f32m1(q + 7 * c0 + i, qstride, vl);
      vf h = __riscv_vfmul_vf_f32m1(x, 0.5f, vl);
      vf h2 = __riscv_vfmul_vv_f32m1(h, h, vl);
      vf s = __riscv_vfmul_vv_f32m1(h, vpoly(h2, SC, 5, vl), vl);
      vf c = vpoly(h2, CC, 6, vl);
      vf s2 = __riscv_vfmul_vf_f32m1(s, 2.0f, vl);
      vf sn = __riscv_vfmul_vv_f32m1(s2, c, vl);
      vf cs = __riscv_vfrsub_vf_f32m1(__riscv_vfmul_vv_f32m1(s2, s, vl), 1.0f, vl);
      const float ca = DH_CA[i], sa = DH_SA[i];
      st(RL[0], cs, vl);
      st(RL[1], __riscv_vfneg_v_f32m1(sn, vl), vl);
      st(RL[2], zero, vl);
      st(RL[3], __riscv_vfmul_vf_f32m1(sn, ca, vl), vl);
      st(RL[4], __riscv_vfmul_vf_f32m1(cs, ca, vl), vl);
      st(RL[5], __riscv_vfmv_v_f_f32m1(-sa, vl), vl);
      st(RL[6], __riscv_vfmul_vf_f32m1(sn, sa, vl), vl);
      st(RL[7], __riscv_vfmul_vf_f32m1(cs, sa, vl), vl);
      st(RL[8], __riscv_vfmv_v_f_f32m1(ca, vl), vl);
      st(RL[9], __riscv_vfmv_v_f_f32m1(DH_A[i], vl), vl);
      st(RL[10], __riscv_vfmv_v_f_f32m1(-sa * DH_D[i], vl), vl);
      st(RL[11], __riscv_vfmv_v_f_f32m1(ca * DH_D[i], vl), vl);
      vcompose_vv(F[i], RL, F[i + 1], vl);
    }
    vcompose_vf(F[7], RH, PH, F[8], vl);
    vcompose_vf(F[8], ID, PLF, F[9], vl);
    vcompose_vf(F[8], RZ, PRF, F[10], vl);
    float* ob = out + (long)c0 * GC_NSPH * 4;
    for (int s = 0; s < GC_NSPH; ++s) {
      float (*fr)[LM] = F[SPH_LINK[s]];
      const float* l = SPH_LOCAL[s];
      for (int i = 0; i < 3; ++i) {
        vf t = __riscv_vfmul_vf_f32m1(ld(fr[3 * i + 0], vl), l[0], vl);
        t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(ld(fr[3 * i + 1], vl), l[1], vl), vl);
        t = __riscv_vfadd_vv_f32m1(t, __riscv_vfmul_vf_f32m1(ld(fr[3 * i + 2], vl), l[2], vl), vl);
        __riscv_vsse32_v_f32m1(ob + 4 * s + i, ostride, __riscv_vfadd_vv_f32m1(ld(fr[9 + i], vl), t, vl), vl);
      }
      __riscv_vsse32_v_f32m1(ob + 4 * s + 3, ostride, __riscv_vfmv_v_f_f32m1(l[3], vl), vl);
    }
    c0 += (int)vl;
  }
}
#else
void fk_spheres_rvv(const float* q, int n, float* out) { fk_spheres_scalar(q, n, out); }
#endif
