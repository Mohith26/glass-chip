// Native sanity test for the control, estimation and SSM kernels.
// Build and run: make -C workloads/capture/control test
#include <cmath>
#include <cstdio>
#include <cstring>

#include "control.h"
#include "kmath.h"

static int fails = 0;
#define CHECK(cond, msg)                         \
  do {                                           \
    if (!(cond)) {                               \
      std::printf("FAIL: %s\n", msg);            \
      ++fails;                                   \
    }                                            \
  } while (0)

int main() {
  // sincos accuracy over the Panda joint range.
  double maxerr = 0;
  for (int i = -3000; i <= 3000; ++i) {
    double x = i * 1e-3, s, c;
    gc::sincos_pi(x, &s, &c);
    maxerr = std::fmax(maxerr, std::fmax(std::fabs(s - std::sin(x)), std::fabs(c - std::cos(x))));
  }
  std::printf("sincos max abs error %.2e\n", maxerr);
  CHECK(maxerr < 1e-5, "sincos accuracy");

  // Gravity at the zero pose: joint 1 axis is vertical, so tau1 ~ 0.
  double q0[7] = {0, 0, 0, 0, 0, 0, 0}, dq0[7] = {0}, tau[7];
  rnea_gravity_coriolis(q0, dq0, tau);
  std::printf("tau(q=0): ");
  for (double t : tau) std::printf("%.3f ", t);
  std::printf("\n");
  CHECK(std::fabs(tau[0]) < 1e-9, "joint 1 carries no gravity torque");
  // Ready pose: shoulder and elbow load.
  double qr[7] = {0, -0.785, 0, -2.356, 0, 1.571, 0.785};
  rnea_gravity_coriolis(qr, dq0, tau);
  std::printf("tau(q=ready): ");
  for (double t : tau) std::printf("%.3f ", t);
  std::printf("\n");
  CHECK(std::fabs(tau[1]) > 1.0 && std::fabs(tau[1]) < 87.0, "shoulder gravity torque plausible");
  // Finite-difference check: gravity torque equals dU/dq for potential energy.
  // Indirect check: rotating joint 1 must not change gravity torques (vertical axis).
  double qr2[7];
  std::memcpy(qr2, qr, sizeof qr);
  qr2[0] = 1.0;
  double tau2[7];
  rnea_gravity_coriolis(qr2, dq0, tau2);
  double d = 0;
  for (int i = 0; i < 7; ++i) d = std::fmax(d, std::fabs(tau2[i] - tau[i]));
  CHECK(d < 1e-4, "gravity invariant to joint 1 rotation");

  // Control step holds at the trajectory point with zero error -> tau = gravity.
  ControlIn in{};
  ControlState st{};
  st.alpha = 0.2;
  for (int i = 0; i < 7; ++i) {
    in.q[i] = in.q0[i] = in.q1[i] = qr[i];
    in.kp[i] = 600;
    in.kd[i] = 50;
    st.tau_prev[i] = tau[i];
  }
  in.seg_T = 0.02;
  in.phase = 0.5;
  in.speed_scale = 1.0;
  double out[7];
  control_step(&in, &st, out);
  double e = 0;
  for (int i = 0; i < 7; ++i) e = std::fmax(e, std::fabs(out[i] - tau[i]));
  CHECK(e < 1e-6, "zero-error control equals gravity compensation");

  // Estimator: a joint moving at constant velocity converges.
  JointFilter jf{};
  ObjectTracker ot{};
  for (int j = 0; j < 7; ++j) jf.P[j][0] = jf.P[j][2] = 1.0;
  for (int t = 0; t < GC_TRACKS; ++t) {
    for (int k = 0; k < 6; ++k) ot.P[t][k * 6 + k] = 1.0;
    ot.x[t][0] = 0.1 * t;
  }
  double qs[5 * 7], dets[8 * 3];
  double tt = 0;
  for (int it = 0; it < 400; ++it) {
    for (int k = 0; k < 5; ++k) {
      tt += 0.001;
      for (int j = 0; j < 7; ++j) qs[k * 7 + j] = 0.5 * tt;
    }
    for (int dd = 0; dd < 8; ++dd) {
      dets[dd * 3] = 0.1 * dd + 0.2 * tt;
      dets[dd * 3 + 1] = 0;
      dets[dd * 3 + 2] = 0;
    }
    estimate_step(&jf, qs, &ot, dets, 1, 0.005);
  }
  std::printf("joint vel est %.4f (true 0.5), track0 vx %.4f (true 0.2)\n", jf.x[0][1], ot.x[0][3]);
  CHECK(std::fabs(jf.x[0][1] - 0.5) < 0.01, "joint KF velocity");
  CHECK(std::fabs(ot.x[0][3] - 0.2) < 0.02, "object track velocity");

  // SSM: a far human gives full speed, a near human stops the robot.
  float hum[64 * 3], sph[61 * 4];
  for (int i = 0; i < 61; ++i) {
    sph[i * 4] = 0;
    sph[i * 4 + 1] = 0;
    sph[i * 4 + 2] = 0.5f;
    sph[i * 4 + 3] = 0.05f;
  }
  for (int i = 0; i < 64; ++i) {
    hum[i * 3] = 3.0f;
    hum[i * 3 + 1] = 0;
    hum[i * 3 + 2] = 1.0f;
  }
  SsmOut so;
  ssm_step(hum, sph, 1.0f, 1.6f, &so);
  std::printf("ssm far: d=%.3f Sp=%.3f scale=%.2f\n", so.min_dist, so.protective_dist, so.speed_scale);
  CHECK(so.speed_scale == 1.0f, "far human full speed");
  for (int i = 0; i < 64; ++i) {
    hum[i * 3] = 0.1f;
    hum[i * 3 + 2] = 0.6f;
  }
  ssm_step(hum, sph, 1.0f, 1.6f, &so);
  CHECK(so.speed_scale == 0.0f, "near human stops robot");
  std::printf("%s\n", fails ? "SOME CHECKS FAILED" : "all checks passed");
  return fails ? 1 : 0;
}
