// T6: state estimation at 200 Hz.
//  1. Joint state filter: per joint constant-velocity Kalman filter [q, dq],
//     consuming the five 1 kHz encoder samples received since the last tick.
//  2. Object tracking: 8 tracks, 6-state constant-velocity Kalman filter with
//     3D position measurements from the detector, Mahalanobis gating and
//     greedy nearest-neighbour association against 8 detections.
// The step function is written for the worst-case tick (detections present).
#include "control.h"

#include "kmath.h"

namespace {

constexpr int J = 7;
constexpr double QJ = 1e-4;   // joint process noise (assumption)
constexpr double RJ = 1e-6;   // encoder noise variance (assumption)
constexpr double QO = 1e-2;   // object acceleration noise (assumption)
constexpr double RO = 4e-4;   // detection position noise variance, 2 cm sigma (assumption)
constexpr double GATE = 11.34;  // chi-square 3 dof, 99%

inline void joint_kf(double* x, double* P, double z, double dt) {
  // predict
  const double q = x[0] + dt * x[1];
  const double p00 = P[0] + 2 * dt * P[1] + dt * dt * P[2] + QJ * dt;
  const double p01 = P[1] + dt * P[2];
  const double p11 = P[2] + QJ;
  // update with position measurement
  const double s = p00 + RJ;
  const double k0 = p00 / s, k1 = p01 / s;
  const double y = z - q;
  x[0] = q + k0 * y;
  x[1] = x[1] + k1 * y;
  P[0] = (1 - k0) * p00;
  P[1] = (1 - k0) * p01;
  P[2] = p11 - k1 * p01;
}

// 6x6 constant-velocity predict: P = F P F^T + Q, F = [I dt*I; 0 I].
inline void track_predict(double* x, double* P, double dt) {
  GC_UNROLL for (int i = 0; i < 3; ++i) x[i] += dt * x[i + 3];
  double FP[36];
  GC_UNROLL for (int r = 0; r < 6; ++r) {
    GC_UNROLL for (int c = 0; c < 6; ++c) {
      FP[r * 6 + c] = P[r * 6 + c] + (r < 3 ? dt * P[(r + 3) * 6 + c] : 0.0);
    }
  }
  GC_UNROLL for (int r = 0; r < 6; ++r) {
    GC_UNROLL for (int c = 0; c < 6; ++c) {
      P[r * 6 + c] = FP[r * 6 + c] + (c < 3 ? dt * FP[r * 6 + c + 3] : 0.0);
    }
  }
  GC_UNROLL for (int i = 0; i < 3; ++i) {
    P[i * 6 + i] += QO * dt * dt * dt / 3;
    P[(i + 3) * 6 + i + 3] += QO * dt;
  }
}

// Inverse of the symmetric 3x3 innovation covariance S = P[0:3,0:3] + R.
inline void s_inverse(const double* P, double* Si) {
  const double a = P[0] + RO, b = P[1], c = P[2];
  const double d = P[7] + RO, e = P[8], f = P[14] + RO;
  const double A0 = d * f - e * e, A1 = c * e - b * f, A2 = b * e - c * d;
  const double det = a * A0 + b * A1 + c * A2;
  const double inv = 1.0 / det;
  Si[0] = A0 * inv;
  Si[1] = A1 * inv;
  Si[2] = A2 * inv;
  Si[3] = (a * f - c * c) * inv;
  Si[4] = (b * c - a * e) * inv;
  Si[5] = (a * d - b * b) * inv;  // packed symmetric [00,01,02,11,12,22]
}

inline double mahal(const double* Si, const double* y) {
  return Si[0] * y[0] * y[0] + Si[3] * y[1] * y[1] + Si[5] * y[2] * y[2] +
         2 * (Si[1] * y[0] * y[1] + Si[2] * y[0] * y[2] + Si[4] * y[1] * y[2]);
}

inline void track_update(double* x, double* P, const double* Si, const double* y) {
  // K = P H^T S^-1, H selects position: P H^T = P[:,0:3].
  double K[18];
  GC_UNROLL for (int r = 0; r < 6; ++r) {
    const double p0 = P[r * 6 + 0], p1 = P[r * 6 + 1], p2 = P[r * 6 + 2];
    K[r * 3 + 0] = p0 * Si[0] + p1 * Si[1] + p2 * Si[2];
    K[r * 3 + 1] = p0 * Si[1] + p1 * Si[3] + p2 * Si[4];
    K[r * 3 + 2] = p0 * Si[2] + p1 * Si[4] + p2 * Si[5];
  }
  GC_UNROLL for (int r = 0; r < 6; ++r) x[r] += K[r * 3] * y[0] + K[r * 3 + 1] * y[1] + K[r * 3 + 2] * y[2];
  // P = (I - K H) P  ->  P -= K * P[0:3,:]
  double HP[18];
  GC_UNROLL for (int i = 0; i < 18; ++i) HP[i] = P[i];
  GC_UNROLL for (int r = 0; r < 6; ++r) {
    GC_UNROLL for (int c = 0; c < 6; ++c) {
      P[r * 6 + c] -= K[r * 3] * HP[c] + K[r * 3 + 1] * HP[6 + c] + K[r * 3 + 2] * HP[12 + c];
    }
  }
}

}  // namespace

extern "C" void estimate_step(JointFilter* jf, const double* q_samples, ObjectTracker* ot, const double* dets,
                              int have_dets, double dt) {
  const double dts = dt / GC_JOINT_SAMPLES;
  GC_UNROLL for (int k = 0; k < GC_JOINT_SAMPLES; ++k) {
    GC_UNROLL for (int j = 0; j < J; ++j) joint_kf(jf->x[j], jf->P[j], q_samples[k * J + j], dts);
  }
  double Si[GC_TRACKS][6];
  GC_UNROLL for (int t = 0; t < GC_TRACKS; ++t) {
    track_predict(ot->x[t], ot->P[t], dt);
    s_inverse(ot->P[t], Si[t]);
  }
  if (!have_dets) return;
  int used[GC_DETS] = {0};
  GC_UNROLL for (int t = 0; t < GC_TRACKS; ++t) {
    double best = GATE;
    int best_d = -1;
    double by[3] = {0, 0, 0};
    GC_UNROLL for (int d = 0; d < GC_DETS; ++d) {
      const double y[3] = {dets[d * 3] - ot->x[t][0], dets[d * 3 + 1] - ot->x[t][1], dets[d * 3 + 2] - ot->x[t][2]};
      const double m = mahal(Si[t], y);
      const bool take = (m < best) & (used[d] == 0);
      best = take ? m : best;
      best_d = take ? d : best_d;
      by[0] = take ? y[0] : by[0];
      by[1] = take ? y[1] : by[1];
      by[2] = take ? y[2] : by[2];
    }
    if (best_d >= 0) {
      used[best_d] = 1;
      track_update(ot->x[t], ot->P[t], Si[t], by);
      ot->active[t] = 1;
    }
  }
}
