// Small fixed-size math helpers for the control and estimation kernels.
// Everything is header-only, inline and loop-unrolled so the RV64 static
// instruction count of each step function is a straight-line count.
#pragma once

#define GC_UNROLL _Pragma("clang loop unroll(full)")

namespace gc {

struct V3 {
  double x, y, z;
};

inline V3 add(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline V3 sub(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline V3 scale(V3 a, double s) { return {a.x * s, a.y * s, a.z * s}; }
inline double dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 cross(V3 a, V3 b) {
  return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}

// Row-major 3x3.
struct M3 {
  double m[9];
};

inline V3 mul(const M3& r, V3 v) {
  return {r.m[0] * v.x + r.m[1] * v.y + r.m[2] * v.z, r.m[3] * v.x + r.m[4] * v.y + r.m[5] * v.z,
          r.m[6] * v.x + r.m[7] * v.y + r.m[8] * v.z};
}
inline V3 mul_t(const M3& r, V3 v) {  // r^T v
  return {r.m[0] * v.x + r.m[3] * v.y + r.m[6] * v.z, r.m[1] * v.x + r.m[4] * v.y + r.m[7] * v.z,
          r.m[2] * v.x + r.m[5] * v.y + r.m[8] * v.z};
}

// Branch-free sine and cosine for |x| <= pi using the half-angle trick:
// s = sin(x/2), c = cos(x/2) from degree-9/10 Taylor series on [-pi/2, pi/2],
// then sin x = 2 s c, cos x = 1 - 2 s^2. Max abs error about 4e-6.
inline void sincos_pi(double x, double* s_out, double* c_out) {
  const double h = 0.5 * x;
  const double h2 = h * h;
  const double s = h * (1.0 + h2 * (-1.0 / 6 + h2 * (1.0 / 120 + h2 * (-1.0 / 5040 + h2 * (1.0 / 362880)))));
  const double c = 1.0 + h2 * (-0.5 + h2 * (1.0 / 24 + h2 * (-1.0 / 720 + h2 * (1.0 / 40320 + h2 * (-1.0 / 3628800)))));
  *s_out = 2.0 * s * c;
  *c_out = 1.0 - 2.0 * s * s;
}

inline double clamp(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }

}  // namespace gc
