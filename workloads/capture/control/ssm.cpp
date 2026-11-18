// T8 (RT part): speed and separation monitoring.
// Minimum distance between 64 clustered human points and the robot's 61
// collision spheres (cuRobo's Franka sphere set size), then the ISO/TS 15066
// protective separation distance
//   S_p = v_h (T_r + T_s) + v_r T_r + B + C + Z_d + Z_r
// and a linear speed scale between S_p and S_p + margin.
// The outer loop over human points is kept as a loop; the inner loop over
// spheres is unrolled so the static count is (outer body) x 64 + epilogue.


#include "control.h"

namespace {
constexpr float T_R = 0.010f;   // robot reaction time: one T8 period (assumption)
constexpr float T_S = 0.120f;   // stopping time at full speed (assumption)
constexpr float B = 0.05f;      // braking distance at full speed (assumption)
constexpr float C = 0.10f;      // intrusion distance (assumption)
constexpr float Z = 0.04f;      // sensor + robot position uncertainty Z_d + Z_r (assumption)
constexpr float MARGIN = 0.30f; // linear slowdown band (assumption)
}  // namespace

extern "C" void ssm_step(const float* human_pts, const float* spheres, float robot_speed, float human_speed,
                         SsmOut* out) {
  float best_sq = 1e30f;
  float best_r = 0.0f;
#pragma clang loop unroll(disable) vectorize(disable)
  for (int h = 0; h < GC_HUMAN_POINTS; ++h) {
    const float hx = human_pts[h * 3], hy = human_pts[h * 3 + 1], hz = human_pts[h * 3 + 2];
#pragma clang loop unroll(full)
    for (int s = 0; s < GC_ROBOT_SPHERES; ++s) {
      const float dx = hx - spheres[s * 4], dy = hy - spheres[s * 4 + 1], dz = hz - spheres[s * 4 + 2];
      const float d2 = dx * dx + dy * dy + dz * dz;
      const bool take = d2 < best_sq;
      best_sq = take ? d2 : best_sq;
      best_r = take ? spheres[s * 4 + 3] : best_r;
    }
  }
  const float d = __builtin_sqrtf(best_sq) - best_r;
  const float sp = human_speed * (T_R + T_S) + robot_speed * T_R + B + C + Z;
  float scale = (d - sp) / MARGIN;
  scale = scale < 0.0f ? 0.0f : (scale > 1.0f ? 1.0f : scale);
  out->min_dist = d;
  out->protective_dist = sp;
  out->speed_scale = scale;
}
