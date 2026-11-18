// Benchmark and verification driver for the planner kernels.
//   kbench <fk|esdf|selfcol> <scalar|rvv|xcol> <iters> [dump]
// On RV64 the timed region is bracketed by rdcycle/rdinstret (user CSRs, read in
// gem5 SE mode). With "dump" every output is printed as raw float bits so the
// results can be compared bit for bit against the host reference build.
// The host build (no RVV, no xcol) runs the reference for every variant name and
// also checks the polynomial FK against a double-precision libm FK.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>

#include "gc_kern.h"

#ifdef __riscv
static inline uint64_t rd_cycle(void) { uint64_t x; __asm__ volatile("rdcycle %0" : "=r"(x)); return x; }
static inline uint64_t rd_instret(void) { uint64_t x; __asm__ volatile("rdinstret %0" : "=r"(x)); return x; }
static inline void fence(void) { __asm__ volatile("fence rw, rw" ::: "memory"); }
#else
static inline uint64_t rd_cycle(void) { return 0; }
static inline uint64_t rd_instret(void) { return 0; }
static inline void fence(void) {}
#endif

static float Q[GC_NCFG * 7];
static float SPH[GC_NQ * 4];
static float GRID[GC_GNX * GC_GNY * GC_GNZ];
static float OUT[GC_NQ * 4];

#ifndef __riscv
// Double-precision libm FK of the sphere centers, for the host accuracy check only.
static void fk_ref_double(const float* q, double* out) {
  static const double A[7] = {0, 0, 0, 0.0825, -0.0825, 0, 0.088}, D[7] = {0.333, 0, 0.316, 0, 0.384, 0, 0};
  static const double CA[7] = {1, 0, 0, 0, 0, 0, 0}, SA[7] = {0, -1, 1, 1, -1, 1, 1};
  double R[11][9], P[11][3];
  for (int k = 0; k < 9; ++k) R[0][k] = (k % 4 == 0);
  P[0][0] = P[0][1] = P[0][2] = 0;
  for (int i = 0; i < 7; ++i) {
    double s = sin(q[i]), c = cos(q[i]);
    double rl[9] = {c, -s, 0, s * CA[i], c * CA[i], -SA[i], s * SA[i], c * SA[i], CA[i]};
    double pl[3] = {A[i], -SA[i] * D[i], CA[i] * D[i]};
    for (int a = 0; a < 3; ++a) {
      for (int b = 0; b < 3; ++b) R[i + 1][3 * a + b] = R[i][3 * a] * rl[b] + R[i][3 * a + 1] * rl[3 + b] + R[i][3 * a + 2] * rl[6 + b];
      P[i + 1][a] = P[i][a] + R[i][3 * a] * pl[0] + R[i][3 * a + 1] * pl[1] + R[i][3 * a + 2] * pl[2];
    }
  }
  const double h = sqrt(0.5);
  double rh[9] = {h, h, 0, -h, h, 0, 0, 0, 1}, ph[3] = {0, 0, 0.107};
  double id[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1}, rz[9] = {-1, 0, 0, 0, -1, 0, 0, 0, 1};
  double pl[3] = {0, 0.04, 0.0584}, pr[3] = {0, -0.04, 0.0584};
  const double* rs[3] = {rh, id, rz};
  const double* ps[3] = {ph, pl, pr};
  const int par[3] = {7, 8, 8};
  for (int k = 0; k < 3; ++k) {
    int f = 8 + k, p = par[k];
    for (int a = 0; a < 3; ++a) {
      for (int b = 0; b < 3; ++b) R[f][3 * a + b] = R[p][3 * a] * rs[k][b] + R[p][3 * a + 1] * rs[k][3 + b] + R[p][3 * a + 2] * rs[k][6 + b];
      P[f][a] = P[p][a] + R[p][3 * a] * ps[k][0] + R[p][3 * a + 1] * ps[k][1] + R[p][3 * a + 2] * ps[k][2];
    }
  }
  for (int s = 0; s < GC_NSPH; ++s) {
    const int L = SPH_LINK[s];
    for (int a = 0; a < 3; ++a)
      out[4 * s + a] = P[L][a] + R[L][3 * a] * SPH_LOCAL[s][0] + R[L][3 * a + 1] * SPH_LOCAL[s][1] + R[L][3 * a + 2] * SPH_LOCAL[s][2];
  }
}
#endif

int main(int argc, char** argv) {
  if (argc < 4) {
    fprintf(stderr, "usage: kbench <fk|esdf|selfcol> <scalar|rvv|xcol> <iters> [dump] [grid=FILE] [savegrid=FILE]\n");
    return 2;
  }
  const char* k = argv[1];
  const char* v = argv[2];
  const int iters = atoi(argv[3]);
  int dump = 0;
  const char* gridfile = NULL;
  const char* savegrid = NULL;
  for (int a = 4; a < argc; ++a) {
    if (!strcmp(argv[a], "dump")) dump = 1;
    else if (!strncmp(argv[a], "grid=", 5)) gridfile = argv[a] + 5;
    else if (!strncmp(argv[a], "savegrid=", 9)) savegrid = argv[a] + 9;
  }
  GridDesc g;
  make_trajectories(Q);
  if (gridfile) {  // skip the 320k-voxel scene build inside the simulator: load the host-built grid
    // open/read rather than fread: musl's fread uses readv, which gem5's RISC-V SE syscall table lacks
    int fd = open(gridfile, O_RDONLY);
    size_t want = sizeof(float) * GC_GNX * GC_GNY * GC_GNZ, got = 0;
    while (fd >= 0 && got < want) {
      ssize_t r = read(fd, (char*)GRID + got, want - got);
      if (r <= 0) break;
      got += (size_t)r;
    }
    if (fd < 0 || got != want) {
      fprintf(stderr, "cannot read %s\n", gridfile);
      return 3;
    }
    close(fd);
    float* keep = GRID;
    make_grid(NULL, &g);  // descriptor only
    g.base = keep;
  } else {
    make_grid(GRID, &g);
  }
  if (savegrid) {
    FILE* f = fopen(savegrid, "wb");
    fwrite(GRID, sizeof(float), GC_GNX * GC_GNY * GC_GNZ, f);
    fclose(f);
  }
  fk_spheres_scalar(Q, GC_NCFG, SPH);  // the collision kernels always see the same spheres
  int nout = 0;
  const int is_rvv = !strcmp(v, "rvv"), is_xcol = !strcmp(v, "xcol");
  double chk = 0;
  fence();
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  for (int it = 0; it < iters; ++it) {
    if (!strcmp(k, "fk")) {
      if (is_rvv) fk_spheres_rvv(Q, GC_NCFG, OUT); else fk_spheres_scalar(Q, GC_NCFG, OUT);
      nout = GC_NQ * 4;
    } else if (!strcmp(k, "esdf")) {
      if (is_rvv) esdf_cost_rvv(SPH, GC_NQ, &g, OUT);
      else if (is_xcol) esdf_cost_xcol(SPH, GC_NQ, &g, OUT);
      else esdf_cost_scalar(SPH, GC_NQ, &g, OUT);
      nout = GC_NQ;
    } else {
      if (is_rvv) selfcol_rvv(SPH, GC_NCFG, OUT); else selfcol_scalar(SPH, GC_NCFG, OUT);
      nout = GC_NCFG;
    }
    chk += OUT[(it * 977) % nout];
  }
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  double sum = 0;
  int nz = 0;
  for (int i = 0; i < nout; ++i) { sum += OUT[i]; nz += OUT[i] != 0.0f; }
  printf("BENCH kernel=%s variant=%s iters=%d cycles=%llu instret=%llu check=%.9g sum=%.9g nonzero=%d n=%d\n", k, v,
         iters, (unsigned long long)(c1 - c0), (unsigned long long)(i1 - i0), chk, sum, nz, nout);
#ifndef __riscv
  if (!strcmp(k, "fk")) {
    static double ref[GC_NSPH * 4];
    double maxerr = 0;
    for (int c = 0; c < GC_NCFG; ++c) {
      fk_ref_double(Q + 7 * c, ref);
      for (int s = 0; s < GC_NSPH; ++s)
        for (int a = 0; a < 3; ++a) {
          double e = fabs(ref[4 * s + a] - OUT[(c * GC_NSPH + s) * 4 + a]);
          if (e > maxerr) maxerr = e;
        }
    }
    printf("FKCHECK max_abs_err_vs_libm_double_m=%.3g\n", maxerr);
  }
#endif
  if (dump) {  // one hex word per line ("D <i> <bits>"), formatted by hand: printf is ~1k instructions per call
    static char buf[GC_NQ * 4 * 20];
    char* w = buf;
    static const char hx[] = "0123456789abcdef";
    for (int i = 0; i < nout; ++i) {
      uint32_t b;
      memcpy(&b, &OUT[i], 4);
      char tmp[12];
      int n = 0, v = i;
      do { tmp[n++] = (char)('0' + v % 10); v /= 10; } while (v);
      *w++ = 'D'; *w++ = ' ';
      while (n) *w++ = tmp[--n];
      *w++ = ' ';
      for (int k = 7; k >= 0; --k) *w++ = hx[(b >> (4 * k)) & 15];
      *w++ = '\n';
    }
    fflush(stdout);
    for (char* r = buf; r < w;) {
      ssize_t m = write(1, r, (size_t)(w - r));
      if (m <= 0) break;
      r += m;
    }
  }
  return 0;
}
