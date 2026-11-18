// T7: Franka Panda 7-DoF joint impedance controller with a recursive
// Newton-Euler (RNEA) gravity + Coriolis term, one call per 1 ms tick.
//
// tau = K (q_d - q) + D (dq_d - dq_f) + rnea(q, dq_f, qdd = 0)
// followed by a torque-rate limiter and absolute torque limits.
//
// Kinematics: Panda modified DH parameters (Craig convention) from Franka's
// public documentation. Inertial parameters: approximate values from the
// Gaz et al. 2019 identification as used in franka_ros. They change the torque
// values, not the instruction count, which is what this file is used for.
#include "control.h"

#include "kmath.h"

namespace {

using gc::V3;
using gc::M3;

constexpr int N = 7;
// Modified DH: a_{i-1}, d_i, alpha_{i-1} (cos/sin precomputed).
constexpr double A[N] = {0.0, 0.0, 0.0, 0.0825, -0.0825, 0.0, 0.088};
constexpr double D[N] = {0.333, 0.0, 0.316, 0.0, 0.384, 0.0, 0.0};
constexpr double CA[N] = {1, 0, 0, 0, 0, 0, 0};    // cos(alpha_{i-1})
constexpr double SA[N] = {0, -1, 1, 1, -1, 1, 1};  // sin(alpha_{i-1})

constexpr double MASS[N] = {4.970684, 0.646926, 3.228604, 3.587895, 1.225946, 1.666555, 0.735522};
constexpr double COM[N][3] = {{3.875e-03, 2.081e-03, -4.762e-02},  {-3.141e-03, -2.872e-02, 3.495e-03},
                              {2.7518e-02, 3.9252e-02, -6.6502e-02}, {-5.317e-02, 1.04419e-01, 2.7454e-02},
                              {-1.1953e-02, 4.1065e-02, -3.8437e-02}, {6.0149e-02, -1.4117e-02, -1.0517e-02},
                              {1.0517e-02, -4.252e-03, 6.1597e-02}};
// Inertia about the COM: ixx, ixy, ixz, iyy, iyz, izz.
constexpr double INERTIA[N][6] = {{0.70337, -0.000139, 0.006772, 0.70661, 0.019169, 0.009117},
                                  {0.007962, -0.003925, 0.010254, 0.02811, 0.000704, 0.025995},
                                  {0.037242, -0.004761, -0.011396, 0.036155, -0.012805, 0.01083},
                                  {0.025853, 0.007796, -0.001332, 0.019552, 0.008641, 0.028323},
                                  {0.035549, -0.002117, -0.004037, 0.029474, 0.000229, 0.008627},
                                  {0.001964, 0.000109, -0.001158, 0.004354, 0.000341, 0.005433},
                                  {0.012516, -0.000428, -0.001196, 0.010027, -0.000741, 0.004815}};
constexpr double GRAVITY = 9.81;
constexpr double TAU_MAX[N] = {87, 87, 87, 87, 12, 12, 12};
constexpr double DTAU_MAX = 1.0;  // Nm per 1 ms tick (1000 Nm/s, Franka's documented rate limit)

inline V3 inertia_mul(const double* I, V3 w) {
  return {I[0] * w.x + I[1] * w.y + I[2] * w.z, I[1] * w.x + I[3] * w.y + I[4] * w.z,
          I[2] * w.x + I[4] * w.y + I[5] * w.z};
}

}  // namespace

extern "C" void rnea_gravity_coriolis(const double* q, const double* dq, double* tau) {
  M3 R[N];   // R[i] = rotation from frame i to frame i-1
  V3 P[N];   // origin of frame i in frame i-1
  GC_UNROLL for (int i = 0; i < N; ++i) {
    double s, c;
    gc::sincos_pi(q[i], &s, &c);
    // Rot_x(alpha) * Rot_z(theta)
    R[i] = M3{{c, -s, 0.0, s * CA[i], c * CA[i], -SA[i], s * SA[i], c * SA[i], CA[i]}};
    P[i] = V3{A[i], -SA[i] * D[i], CA[i] * D[i]};
  }
  V3 w{0, 0, 0}, wd{0, 0, 0}, vd{0, 0, GRAVITY};
  V3 F[N], Nm[N];
  const V3 z{0, 0, 1};
  GC_UNROLL for (int i = 0; i < N; ++i) {
    const V3 w_prev = w;
    const V3 wr = gc::mul_t(R[i], w_prev);
    w = gc::add(wr, gc::scale(z, dq[i]));
    wd = gc::add(gc::mul_t(R[i], wd), gc::cross(wr, gc::scale(z, dq[i])));
    vd = gc::mul_t(R[i], gc::add(gc::add(gc::cross(wd, P[i]), gc::cross(w_prev, gc::cross(w_prev, P[i]))), vd));
    const V3 c{COM[i][0], COM[i][1], COM[i][2]};
    const V3 vcd = gc::add(gc::add(gc::cross(wd, c), gc::cross(w, gc::cross(w, c))), vd);
    F[i] = gc::scale(vcd, MASS[i]);
    Nm[i] = gc::add(inertia_mul(INERTIA[i], wd), gc::cross(w, inertia_mul(INERTIA[i], w)));
  }
  V3 f{0, 0, 0}, n{0, 0, 0};
  GC_UNROLL for (int i = N - 1; i >= 0; --i) {
    const V3 c{COM[i][0], COM[i][1], COM[i][2]};
    V3 fn = F[i];
    V3 nn = gc::add(Nm[i], gc::cross(c, F[i]));
    if (i + 1 < N) {
      const V3 rf = gc::mul(R[i + 1], f);
      fn = gc::add(fn, rf);
      nn = gc::add(nn, gc::add(gc::mul(R[i + 1], n), gc::cross(P[i + 1], rf)));
    }
    f = fn;
    n = nn;
    tau[i] = n.z;
  }
}

extern "C" void control_step(const ControlIn* in, ControlState* st, double* tau_out) {
  // Cubic Hermite sample of the planned trajectory segment at phase s in [0,1].
  const double s = in->phase;
  const double s2 = s * s, s3 = s2 * s;
  const double h00 = 2 * s3 - 3 * s2 + 1, h10 = s3 - 2 * s2 + s, h01 = -2 * s3 + 3 * s2, h11 = s3 - s2;
  const double dh00 = 6 * s2 - 6 * s, dh10 = 3 * s2 - 4 * s + 1, dh01 = -6 * s2 + 6 * s, dh11 = 3 * s2 - 2 * s;
  double qd[N], dqd[N], dqf[N], tau_dyn[N];
  GC_UNROLL for (int i = 0; i < N; ++i) {
    qd[i] = h00 * in->q0[i] + h10 * in->seg_T * in->v0[i] + h01 * in->q1[i] + h11 * in->seg_T * in->v1[i];
    dqd[i] = (dh00 * in->q0[i] + dh01 * in->q1[i]) / in->seg_T + dh10 * in->v0[i] + dh11 * in->v1[i];
    dqd[i] *= in->speed_scale;  // safety speed scaling from T8
    // First-order low-pass on measured velocity.
    dqf[i] = st->alpha * in->dq[i] + (1.0 - st->alpha) * st->dq_filt[i];
    st->dq_filt[i] = dqf[i];
  }
  rnea_gravity_coriolis(in->q, dqf, tau_dyn);
  GC_UNROLL for (int i = 0; i < N; ++i) {
    double t = in->kp[i] * (qd[i] - in->q[i]) + in->kd[i] * (dqd[i] - dqf[i]) + tau_dyn[i];
    t = gc::clamp(t, st->tau_prev[i] - DTAU_MAX, st->tau_prev[i] + DTAU_MAX);
    t = gc::clamp(t, -TAU_MAX[i], TAU_MAX[i]);
    st->tau_prev[i] = t;
    tau_out[i] = t;
  }
}
