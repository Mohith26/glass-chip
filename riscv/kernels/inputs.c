// Deterministic inputs for the kernels: joint trajectories for a 4-seed batch and
// an analytic ESDF of a small conveyor scene. Only IEEE + - * / sqrt min max are
// used, so the host and the RV64 build produce bit-identical inputs.
#include <math.h>

#include "gc_kern.h"

void make_trajectories(float* q) {
  static const float start[7] = {0.0f, -0.785f, 0.0f, -2.356f, 0.0f, 1.571f, 0.785f};
  for (int s = 0; s < GC_SEEDS; ++s) {
    const float fs = (float)s;
    const float goal[7] = {0.3f * fs - 0.45f, 0.45f, 0.1f * fs, -1.55f, 0.05f * fs, 2.0f, 0.785f};
    for (int t = 0; t < GC_STEPS; ++t) {
      const float a = (float)t * (1.0f / (GC_STEPS - 1));
      float* o = q + 7 * (s * GC_STEPS + t);
      for (int j = 0; j < 7; ++j) o[j] = start[j] + a * (goal[j] - start[j]);
    }
  }
}

static float box_sdf(float px, float py, float pz, float cx, float cy, float cz, float hx, float hy, float hz) {
  float qx = fabsf(px - cx) - hx, qy = fabsf(py - cy) - hy, qz = fabsf(pz - cz) - hz;
  float ox = fmaxf(qx, 0.0f), oy = fmaxf(qy, 0.0f), oz = fmaxf(qz, 0.0f);
  float outside = sqrtf(ox * ox + oy * oy + oz * oz);
  float inside = fminf(fmaxf(qx, fmaxf(qy, qz)), 0.0f);
  return outside + inside;
}

void make_grid(float* dist, GridDesc* g) {
  g->ox = -0.6f; g->oy = -0.8f; g->oz = -0.1f;
  g->inv_h = 50.0f;  // 20 mm voxels
  g->nx = GC_GNX; g->ny = GC_GNY; g->nz = GC_GNZ;
  g->eta = 0.05f;
  g->base = dist;
  if (!dist) return;
  for (int iz = 0; iz < GC_GNZ; ++iz)
    for (int iy = 0; iy < GC_GNY; ++iy)
      for (int ix = 0; ix < GC_GNX; ++ix) {
        float x = g->ox + 0.02f * (float)ix, y = g->oy + 0.02f * (float)iy, z = g->oz + 0.02f * (float)iz;
        float d = z;                                                       // table top at z = 0
        d = fminf(d, box_sdf(x, y, z, 0.55f, 0.0f, 0.1f, 0.15f, 0.6f, 0.1f));  // conveyor
        d = fminf(d, box_sdf(x, y, z, 0.3f, -0.35f, 0.3f, 0.05f, 0.05f, 0.3f));  // post
        float sx = x - 0.45f, sy = y - 0.25f, sz = z - 0.45f;
        d = fminf(d, sqrtf(sx * sx + sy * sy + sz * sz) - 0.08f);             // hanging object
        dist[(iz * GC_GNY + iy) * GC_GNX + ix] = d;
      }
}
