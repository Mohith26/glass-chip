// Times the Glass Chip control-stack kernels (workloads/capture/control) on RV64:
// T7 control_step (1 kHz), T6 estimate_step (200 Hz), T8 ssm_step (100 Hz, RT part).
#include <cmath>
#include <cstring>
extern "C" {
#include "bench.h"
}
#include "control.h"

int main(int argc, char **argv) {
  int iters = argc > 1 ? atoi(argv[1]) : 10;
  const char *which = argc > 2 ? argv[2] : "control";
  double chk = 0;
  uint64_t c0, c1, i0, i1;
  if (!std::strcmp(which, "control")) {
    ControlIn in{};
    ControlState st{};
    st.alpha = 0.2;
    in.seg_T = 0.5; in.speed_scale = 1.0;
    for (int j = 0; j < 7; ++j) { in.q[j] = 0.1 * j; in.q0[j] = 0.0; in.q1[j] = 0.2 * j;
      in.kp[j] = 100; in.kd[j] = 10; }
    double tau[7];
    c0 = rd_cycle(); i0 = rd_instret();
    for (int it = 0; it < iters; ++it) {
      in.phase = (it % 100) * 0.01;
      control_step(&in, &st, tau);
      chk += tau[1];
    }
    c1 = rd_cycle(); i1 = rd_instret();
    BENCH_REPORT("T7_control_step", iters, c0, c1, i0, i1, chk);
  } else if (!std::strcmp(which, "estimate")) {
    static JointFilter jf; static ObjectTracker ot;
    double qs[GC_JOINT_SAMPLES * 7], dets[GC_DETS * 3];
    for (int i = 0; i < GC_JOINT_SAMPLES * 7; ++i) qs[i] = 0.01 * i;
    for (int i = 0; i < GC_DETS * 3; ++i) dets[i] = 0.1 * i;
    for (int t = 0; t < GC_TRACKS; ++t) ot.active[t] = 1;
    c0 = rd_cycle(); i0 = rd_instret();
    for (int it = 0; it < iters; ++it) {
      estimate_step(&jf, qs, &ot, dets, it % 6 == 0, 0.005);
      chk += jf.x[0][0];
    }
    c1 = rd_cycle(); i1 = rd_instret();
    BENCH_REPORT("T6_estimate_step", iters, c0, c1, i0, i1, chk);
  } else {
    static float hp[GC_HUMAN_POINTS * 3], sp[GC_ROBOT_SPHERES * 4];
    for (int i = 0; i < GC_HUMAN_POINTS * 3; ++i) hp[i] = 1.0f + 0.01f * i;
    for (int i = 0; i < GC_ROBOT_SPHERES * 4; ++i) sp[i] = 0.02f * (i % 17);
    SsmOut o{};
    c0 = rd_cycle(); i0 = rd_instret();
    for (int it = 0; it < iters; ++it) {
      ssm_step(hp, sp, 0.5f, 1.6f, &o);
      chk += o.min_dist;
    }
    c1 = rd_cycle(); i1 = rd_instret();
    BENCH_REPORT("T8_ssm_step", iters, c0, c1, i0, i1, chk);
  }
  return 0;
}
