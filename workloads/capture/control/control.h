// Shared C ABI for the control, estimation and safety kernels.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// ---- T7 joint impedance control (7 joints) ----
typedef struct {
  double q[7], dq[7];          // measured joint position and velocity
  double q0[7], v0[7];         // trajectory segment start
  double q1[7], v1[7];         // trajectory segment end
  double seg_T;                // segment duration (s)
  double phase;                // position within segment, 0..1
  double kp[7], kd[7];         // impedance gains
  double speed_scale;          // from the safety monitor, 0..1
} ControlIn;

typedef struct {
  double dq_filt[7];
  double tau_prev[7];
  double alpha;                // velocity filter coefficient
} ControlState;

void rnea_gravity_coriolis(const double* q, const double* dq, double* tau);
void control_step(const ControlIn* in, ControlState* st, double* tau_out);

// ---- T6 state estimation ----
#define GC_JOINT_SAMPLES 5     // 1 kHz joint samples consumed per 200 Hz estimator tick
#define GC_TRACKS 8
#define GC_DETS 8

typedef struct {
  double x[7][2];              // per joint [q, dq]
  double P[7][3];              // per joint symmetric 2x2 covariance [p00, p01, p11]
} JointFilter;

typedef struct {
  double x[GC_TRACKS][6];      // [px, py, pz, vx, vy, vz]
  double P[GC_TRACKS][36];     // 6x6 covariance, row-major
  int active[GC_TRACKS];
} ObjectTracker;

void estimate_step(JointFilter* jf, const double* q_samples /* [5][7] */, ObjectTracker* ot,
                   const double* dets /* [8][3] */, int have_dets, double dt);

// ---- T8 speed and separation monitoring (RT part) ----
#define GC_HUMAN_POINTS 64
#define GC_ROBOT_SPHERES 61

typedef struct {
  float min_dist;
  float protective_dist;
  float speed_scale;
} SsmOut;

void ssm_step(const float* human_pts /* [64][3] */, const float* spheres /* [61][4] */,
              float robot_speed, float human_speed, SsmOut* out);

#ifdef __cplusplus
}
#endif
