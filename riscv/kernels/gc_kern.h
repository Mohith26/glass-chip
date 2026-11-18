// Glass Chip RISC-V kernels: shared types and declarations.
//
// The kernels are small, cuRobo-style building blocks of the motion planner (T5):
// batched forward kinematics to collision spheres, sphere vs ESDF collision cost
// (trilinear distance lookup + hinge), and sphere-sphere self-collision cost.
// Every kernel has a scalar version and an RVV-intrinsics version; the ESDF cost
// also has a version that uses the study instruction xcol.sdf (custom-0 opcode,
// see riscv/udb/ and riscv/README.md).
//
// Floating-point policy: everything is float32 and built with -ffp-contract=off
// so the scalar code, the RVV code and the xcol.sdf semantics perform the same
// IEEE operations in the same order (no fused multiply-add). That makes the
// scalar and xcol results bit-identical and the RVV results identical except
// where a vector reduction reorders a sum.
#pragma once
#include <stdint.h>

#include "franka_spheres.h"

#ifdef __cplusplus
extern "C" {
#endif

// ESDF grid descriptor, exactly the 40-byte memory layout xcol.sdf reads via rs2.
// Distances are float32, x fastest: index = (iz * ny + iy) * nx + ix,
// sample (ix, iy, iz) sits at origin + h * (ix, iy, iz).
typedef struct {
  float ox, oy, oz;     // grid origin (m)
  float inv_h;          // 1 / voxel size (1/m)
  uint32_t nx, ny, nz;  // samples per axis, each >= 2
  float eta;            // activation distance (m): cost = max(0, r + eta - d)
  const float* base;    // distance samples
} GridDesc;

// Problem sizes (one T5 TrajOpt line-search evaluation for a seed batch).
#define GC_SEEDS 4
#define GC_STEPS 32
#define GC_NCFG (GC_SEEDS * GC_STEPS)        // 128 joint configurations
#define GC_NQ (GC_NCFG * GC_NSPH)            // 7808 sphere queries
#define GC_GNX 80
#define GC_GNY 80
#define GC_GNZ 50                            // 80 x 80 x 50 at 20 mm, as in T3_esdf

// Forward kinematics: q is [n][7] (AoS), spheres_out is [n][GC_NSPH][4] world xyz + r.
void fk_spheres_scalar(const float* q, int n, float* spheres_out);
void fk_spheres_rvv(const float* q, int n, float* spheres_out);

// One query: the exact reference semantics of xcol.sdf.
float sdf_cost_ref(const float* sphere4, const GridDesc* g);
void esdf_cost_scalar(const float* spheres, int m, const GridDesc* g, float* cost);
void esdf_cost_rvv(const float* spheres, int m, const GridDesc* g, float* cost);
void esdf_cost_xcol(const float* spheres, int m, const GridDesc* g, float* cost);

// Self collision: per configuration, sum over the 694 checked pairs of
// max(0, ra + rb + buffers - |ca - cb|). spheres is [n][GC_NSPH][4].
void selfcol_scalar(const float* spheres, int n, float* cost);
void selfcol_rvv(const float* spheres, int n, float* cost);

// Deterministic inputs shared by the host reference and the RV64 runs.
void make_trajectories(float* q /* [GC_NCFG][7] */);
void make_grid(float* dist /* [GC_GNZ][GC_GNY][GC_GNX] */, GridDesc* g);

#ifdef __cplusplus
}
#endif
