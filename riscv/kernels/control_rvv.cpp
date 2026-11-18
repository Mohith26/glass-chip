// T7 joint impedance control step with the per-joint parts in RVV intrinsics.
// Same math as workloads/capture/control/control.cpp::control_step: the 7-joint
// Hermite trajectory sample, velocity filter, impedance law and torque clamps run
// as one vector of 7 doubles (e64, LMUL 2 at VLEN 256 holds 8). The recursive
// Newton-Euler term is a serial chain over joints and stays scalar
// (rnea_gravity_coriolis from control.cpp).
// Note: VX-1's RT core has no vector unit; this variant answers "what would T7
// cost on an RVV app core", it is not the RT-core configuration.
#include "control.h"

#ifdef __riscv_vector
#include <riscv_vector.h>

namespace {
constexpr double TAU_MAX[7] = {87, 87, 87, 87, 12, 12, 12};
constexpr double DTAU_MAX = 1.0;
}  // namespace

extern "C" void control_step_rvv(const ControlIn* in, ControlState* st, double* tau_out) {
  const double s = in->phase;
  const double s2 = s * s, s3 = s2 * s;
  const double h00 = 2 * s3 - 3 * s2 + 1, h10 = s3 - 2 * s2 + s, h01 = -2 * s3 + 3 * s2, h11 = s3 - s2;
  const double dh00 = 6 * s2 - 6 * s, dh10 = 3 * s2 - 4 * s + 1, dh01 = -6 * s2 + 6 * s, dh11 = 3 * s2 - 2 * s;
  const size_t vl = __riscv_vsetvl_e64m2(7);
  vfloat64m2_t q0 = __riscv_vle64_v_f64m2(in->q0, vl), q1 = __riscv_vle64_v_f64m2(in->q1, vl);
  vfloat64m2_t v0 = __riscv_vle64_v_f64m2(in->v0, vl), v1 = __riscv_vle64_v_f64m2(in->v1, vl);
  // qd = h00 q0 + h10 T v0 + h01 q1 + h11 T v1   (same association as the scalar code)
  vfloat64m2_t qd = __riscv_vfmul_vf_f64m2(q0, h00, vl);
  qd = __riscv_vfadd_vv_f64m2(qd, __riscv_vfmul_vv_f64m2(__riscv_vfmv_v_f_f64m2(h10 * in->seg_T, vl), v0, vl), vl);
  qd = __riscv_vfadd_vv_f64m2(qd, __riscv_vfmul_vf_f64m2(q1, h01, vl), vl);
  qd = __riscv_vfadd_vv_f64m2(qd, __riscv_vfmul_vv_f64m2(__riscv_vfmv_v_f_f64m2(h11 * in->seg_T, vl), v1, vl), vl);
  vfloat64m2_t dqd = __riscv_vfadd_vv_f64m2(__riscv_vfmul_vf_f64m2(q0, dh00, vl), __riscv_vfmul_vf_f64m2(q1, dh01, vl), vl);
  dqd = __riscv_vfdiv_vf_f64m2(dqd, in->seg_T, vl);
  dqd = __riscv_vfadd_vv_f64m2(dqd, __riscv_vfmul_vf_f64m2(v0, dh10, vl), vl);
  dqd = __riscv_vfadd_vv_f64m2(dqd, __riscv_vfmul_vf_f64m2(v1, dh11, vl), vl);
  dqd = __riscv_vfmul_vf_f64m2(dqd, in->speed_scale, vl);
  vfloat64m2_t dq = __riscv_vle64_v_f64m2(in->dq, vl);
  vfloat64m2_t dqf = __riscv_vfadd_vv_f64m2(__riscv_vfmul_vf_f64m2(dq, st->alpha, vl),
                                            __riscv_vfmul_vf_f64m2(__riscv_vle64_v_f64m2(st->dq_filt, vl), 1.0 - st->alpha, vl), vl);
  __riscv_vse64_v_f64m2(st->dq_filt, dqf, vl);
  double dqf_a[8], tau_dyn[8];
  __riscv_vse64_v_f64m2(dqf_a, dqf, vl);
  rnea_gravity_coriolis(in->q, dqf_a, tau_dyn);
  vfloat64m2_t q = __riscv_vle64_v_f64m2(in->q, vl);
  vfloat64m2_t t = __riscv_vfmul_vv_f64m2(__riscv_vle64_v_f64m2(in->kp, vl), __riscv_vfsub_vv_f64m2(qd, q, vl), vl);
  t = __riscv_vfadd_vv_f64m2(t, __riscv_vfmul_vv_f64m2(__riscv_vle64_v_f64m2(in->kd, vl), __riscv_vfsub_vv_f64m2(dqd, dqf, vl), vl), vl);
  t = __riscv_vfadd_vv_f64m2(t, __riscv_vle64_v_f64m2(tau_dyn, vl), vl);
  vfloat64m2_t tp = __riscv_vle64_v_f64m2(st->tau_prev, vl);
  t = __riscv_vfmax_vv_f64m2(t, __riscv_vfsub_vf_f64m2(tp, DTAU_MAX, vl), vl);
  t = __riscv_vfmin_vv_f64m2(t, __riscv_vfadd_vf_f64m2(tp, DTAU_MAX, vl), vl);
  vfloat64m2_t tm = __riscv_vle64_v_f64m2(TAU_MAX, vl);
  t = __riscv_vfmax_vv_f64m2(t, __riscv_vfneg_v_f64m2(tm, vl), vl);
  t = __riscv_vfmin_vv_f64m2(t, tm, vl);
  __riscv_vse64_v_f64m2(st->tau_prev, t, vl);
  __riscv_vse64_v_f64m2(tau_out, t, vl);
}
#else
extern "C" void control_step_rvv(const ControlIn* in, ControlState* st, double* tau_out) { control_step(in, st, tau_out); }
#endif
