// Sphere vs ESDF collision cost, cuRobo style: trilinear lookup of the signed
// distance at each sphere center, minus the radius, through a hinge.
//   cost = max(0, (r + eta) - trilinear(grid, c))
// Three versions: scalar C, RVV intrinsics (vectorized over spheres with indexed
// gathers for the 8 corners), and one xcol.sdf study instruction per sphere.
// sdf_cost_ref is the exact semantics of xcol.sdf (same operation order as the
// IDL in riscv/udb/ and the gem5 implementation on branch glasschip-xcol).
#include <math.h>

#include "gc_kern.h"

#ifdef __riscv_vector
#include <riscv_vector.h>
#endif

float sdf_cost_ref(const float* sph, const GridDesc* g) {
  const float org[3] = {g->ox, g->oy, g->oz};
  const uint32_t n[3] = {g->nx, g->ny, g->nz};
  uint32_t i0[3];
  float fr[3];
  for (int a = 0; a < 3; ++a) {
    float t = sph[a] - org[a];
    float gp = t * g->inv_h;
    float hi = (float)(n[a] - 1);
    gp = fmaxf(gp, 0.0f);
    gp = fminf(gp, hi);
    uint32_t ii = (uint32_t)gp;
    if (ii > n[a] - 2) ii = n[a] - 2;
    i0[a] = ii;
    fr[a] = gp - (float)ii;
  }
  const uint32_t sy = g->nx, sz = g->nx * g->ny;
  const float* p = g->base + (i0[2] * sz + i0[1] * sy + i0[0]);
  const float d0 = p[0], d1 = p[1], d2 = p[sy], d3 = p[sy + 1];
  const float d4 = p[sz], d5 = p[sz + 1], d6 = p[sz + sy], d7 = p[sz + sy + 1];
  float c00 = d0 + fr[0] * (d1 - d0);
  float c10 = d2 + fr[0] * (d3 - d2);
  float c01 = d4 + fr[0] * (d5 - d4);
  float c11 = d6 + fr[0] * (d7 - d6);
  float c0 = c00 + fr[1] * (c10 - c00);
  float c1 = c01 + fr[1] * (c11 - c01);
  float dist = c0 + fr[2] * (c1 - c0);
  float pen = (sph[3] + g->eta) - dist;
  return fmaxf(pen, 0.0f);
}

void esdf_cost_scalar(const float* spheres, int m, const GridDesc* g, float* cost) {
  for (int i = 0; i < m; ++i) cost[i] = sdf_cost_ref(spheres + 4 * i, g);
}

#if defined(__riscv) && !defined(GC_NO_XCOL)
// xcol.sdf fd, rs1, rs2: R-type, opcode custom-0 (0x0B), funct3 0, funct7 0.
static inline float xcol_sdf(const float* sph, const GridDesc* g) {
  float r;
  __asm__ volatile(".insn r 0x0B, 0, 0, %0, %1, %2" : "=f"(r) : "r"(sph), "r"(g));
  return r;
}
// Unrolled 4x so four independent xcol.sdf instructions are in flight before their results are
// stored; without this an in-order core waits the full latency for every sphere.
void esdf_cost_xcol(const float* spheres, int m, const GridDesc* g, float* cost) {
  int i = 0;
  for (; i + 4 <= m; i += 4) {
    const float* s = spheres + 4 * i;
    float r0 = xcol_sdf(s, g), r1 = xcol_sdf(s + 4, g), r2 = xcol_sdf(s + 8, g), r3 = xcol_sdf(s + 12, g);
    cost[i] = r0; cost[i + 1] = r1; cost[i + 2] = r2; cost[i + 3] = r3;
  }
  for (; i < m; ++i) cost[i] = xcol_sdf(spheres + 4 * i, g);
}
#else
void esdf_cost_xcol(const float* spheres, int m, const GridDesc* g, float* cost) {
  esdf_cost_scalar(spheres, m, g, cost);
}
#endif

#ifdef __riscv_vector
typedef vfloat32m2_t vf;
typedef vuint32m2_t vu;

static inline vf lerp(vf a, vf b, vf f, size_t vl) {
  return __riscv_vfadd_vv_f32m2(a, __riscv_vfmul_vv_f32m2(f, __riscv_vfsub_vv_f32m2(b, a, vl), vl), vl);
}

// Grid index and fraction along one axis, same steps as sdf_cost_ref.
static inline vf axis(vf c, float o, float inv_h, uint32_t n, vu* idx, size_t vl) {
  vf gp = __riscv_vfmul_vf_f32m2(__riscv_vfsub_vf_f32m2(c, o, vl), inv_h, vl);
  gp = __riscv_vfmax_vf_f32m2(gp, 0.0f, vl);
  gp = __riscv_vfmin_vf_f32m2(gp, (float)(n - 1), vl);
  vu ii = __riscv_vfcvt_rtz_xu_f_v_u32m2(gp, vl);
  ii = __riscv_vminu_vx_u32m2(ii, n - 2, vl);
  *idx = ii;
  return __riscv_vfsub_vv_f32m2(gp, __riscv_vfcvt_f_xu_v_f32m2(ii, vl), vl);
}

void esdf_cost_rvv(const float* spheres, int m, const GridDesc* g, float* cost) {
  const uint32_t sy = 4 * g->nx, sz = 4 * g->nx * g->ny;  // byte strides
  for (int i = 0; i < m;) {
    size_t vl = __riscv_vsetvl_e32m2((size_t)(m - i));
    const float* s = spheres + 4 * i;
    vf cx = __riscv_vlse32_v_f32m2(s + 0, 16, vl);
    vf cy = __riscv_vlse32_v_f32m2(s + 1, 16, vl);
    vf cz = __riscv_vlse32_v_f32m2(s + 2, 16, vl);
    vf r = __riscv_vlse32_v_f32m2(s + 3, 16, vl);
    vu ix, iy, iz;
    vf fx = axis(cx, g->ox, g->inv_h, g->nx, &ix, vl);
    vf fy = axis(cy, g->oy, g->inv_h, g->ny, &iy, vl);
    vf fz = axis(cz, g->oz, g->inv_h, g->nz, &iz, vl);
    // byte offset = 4 * ((iz * ny + iy) * nx + ix)
    vu off = __riscv_vmadd_vx_u32m2(iz, g->ny, iy, vl);
    off = __riscv_vmadd_vx_u32m2(off, g->nx, ix, vl);
    off = __riscv_vsll_vx_u32m2(off, 2, vl);
    const float* b = g->base;
    vf d0 = __riscv_vluxei32_v_f32m2(b, off, vl);
    vf d1 = __riscv_vluxei32_v_f32m2(b + 1, off, vl);
    vu oy = __riscv_vadd_vx_u32m2(off, sy, vl);
    vf d2 = __riscv_vluxei32_v_f32m2(b, oy, vl);
    vf d3 = __riscv_vluxei32_v_f32m2(b + 1, oy, vl);
    vu oz = __riscv_vadd_vx_u32m2(off, sz, vl);
    vf d4 = __riscv_vluxei32_v_f32m2(b, oz, vl);
    vf d5 = __riscv_vluxei32_v_f32m2(b + 1, oz, vl);
    vu oyz = __riscv_vadd_vx_u32m2(oz, sy, vl);
    vf d6 = __riscv_vluxei32_v_f32m2(b, oyz, vl);
    vf d7 = __riscv_vluxei32_v_f32m2(b + 1, oyz, vl);
    vf c00 = lerp(d0, d1, fx, vl), c10 = lerp(d2, d3, fx, vl);
    vf c01 = lerp(d4, d5, fx, vl), c11 = lerp(d6, d7, fx, vl);
    vf c0 = lerp(c00, c10, fy, vl), c1 = lerp(c01, c11, fy, vl);
    vf dist = lerp(c0, c1, fz, vl);
    vf pen = __riscv_vfsub_vv_f32m2(__riscv_vfadd_vf_f32m2(r, g->eta, vl), dist, vl);
    __riscv_vse32_v_f32m2(cost + i, __riscv_vfmax_vf_f32m2(pen, 0.0f, vl), vl);
    i += (int)vl;
  }
}
#else
void esdf_cost_rvv(const float* spheres, int m, const GridDesc* g, float* cost) {
  esdf_cost_scalar(spheres, m, g, cost);
}
#endif
