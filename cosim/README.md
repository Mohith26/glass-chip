# glasschip-cosim

This is the robot half of Glass Chip. A simulated Franka Panda picks objects off a moving conveyor, and every piece of its software stack (camera ingest, detection, tracking, policy, planning, control, safety) runs as jobs on a chip model. A job's output reaches the robot only when the chip says the job finished, so chip latency changes what the arm does.

One thing to say plainly up front: the arm's behavior comes from a scripted expert in hybrid mode. I could not run an open VLA closed loop at a useful rate on this machine, so the VLA's compute is modeled from its real operator graph (task T4 on the chip), while a scripted expert decides where the arm goes. The policy sits behind a small interface (`cosim/stack/policy.py`), so a real VLA can replace it later.

## How it works

Physics is MuJoCo at 1 ms steps. At every step the bridge (`cosim/bridge.py`):

1. releases the jobs that are due (camera frames at 30 Hz per camera, policy at 8 Hz, state estimation at 200 Hz, control at 1 kHz, safety at 100 Hz), snapshotting each job's inputs at release time;
2. advances the chip model to the same virtual time (int64 picoseconds);
3. applies completed jobs in finish order, and releases chained jobs when their upstream finishes (camera ingest to detection, policy to planner);
4. sends the last finished control command to the motors and steps physics.

Stale data behaves as the spec asks. A late controller leaves the previous torque on the motors (zero-order hold). A late detector means the tracker and policy keep using the previous detection. A late safety monitor stops the arm late. I track two end-to-end chains with tags that travel with the data: camera frame to first actuator command from the resulting plan, and camera frame to first actuator command that uses the resulting safety decision.

| Piece | File | What it does |
|---|---|---|
| Scene | `envs/build_scene.py` | Generates `panda_torque.xml` (Menagerie Panda with joint torque motors, TCP site, wrist camera) and `conveyor_scene.xml` (belt, 14 pooled objects, bin, overhead camera, human proxy) |
| Belt | `conveyor.py` | Friction-capped drive force toward belt velocity, spawning, object bookkeeping, human walker |
| T1, T2 | `stack/perception.py` | Ground-truth snapshot inside each camera frustum at capture time, then noisy poses |
| T6 | `stack/estimator.py` | Constant-velocity Kalman track per object, stamped with the capture time of its last detection |
| T4 | `stack/policy.py` | Scripted expert: picks the next reachable object, predicts the intercept from the track and its age, keeps refining while a refinement can still land in time |
| T5 | `stack/planner.py` | Damped least squares IK on MuJoCo Jacobians, a time-indexed joint trajectory that follows the object down, closes, lifts, carries to the bin; quintic blending on replan |
| T7 | `stack/controller.py` | 1 kHz joint impedance law with inertia shaping, output is joint torque for the torque motors; trajectory clock scaled by the safety speed factor |
| T8 | `stack/safety.py` | Speed and separation monitoring: full speed above 1.0 m, linear slowdown, protective stop below 0.45 m |
| Chips | `fake_chip.py`, `core_chip.py` | Zero-latency chip, fixed latency plus jitter chip, and an adapter for `glasschip_core` |

Assumptions worth knowing: the belt speed has a 5 % ripple and each object slips with a 15 % speed variation (both Ornstein-Uhlenbeck processes). Without some disturbance, a policy that compensates for latency would barely notice latency at all; the disturbance is what makes old observations go wrong, and `results/s14.json` includes a sensitivity sweep over it. Camera noise is 3 mm (overhead) and 1.5 mm (wrist). Data association uses object ids. All of these are labeled in code.

## Running it

```
.venv/bin/python -m cosim.run --scenario configs/scenarios/S1_nominal.yaml --chip zero
.venv/bin/python -m cosim.run --scenario configs/scenarios/S2_second_camera.yaml --chip core --record data/cosim/runs/S2
.venv/bin/python -m cosim.run --speed 0.4 --duration 60 --chip fixed --e2e-ms 300
.venv/bin/python -m cosim.export_scene            # scene geometry for the web viewer (see SCENE_FORMAT.md)
.venv/bin/python studies/s14_latency_tolerance.py  # latency tolerance study
.venv/bin/python -m pytest cosim/tests -q
```

Scenario YAMLs use the shared scenario format (configs/scenarios): `T2_detect` runs on wrist frames and `T2_detect_overhead` on overhead frames, release periods come from `workloads/graphs`. `cosim/scenarios_dev/` has a few extra scenarios I used while building this.

The run is deterministic for a given seed (physics, belt disturbances, sensor noise and chip jitter all draw from seeded generators); a test checks that two runs produce identical body poses and events.

Recordings: `CoSim.save_recording(prefix)` writes `prefix.npz` (60 Hz body poses for every body, object states, chip job records) and `prefix.json` (body names, robot events, chains, metrics). `cosim/replay.py` writes the same run in the viewer's replay format through `tools/replay_writer.py`.

## Results

The latency tolerance numbers (baseline success per speed, success at each injected latency, and where success falls under 90 % of baseline) are in `results/s14.json`, with the figure in `results/figures/s14_latency_tolerance.svg`.

## Limitations

- Behavior comes from a scripted expert, not a learned policy.
- There are no rendered images. Perception is a ground-truth snapshot with noise and frustum checks, and there is no occlusion model.
- Object association uses ids, not geometry.
- The disturbance model sets the shape of the latency tolerance curve. Its parameters are assumptions, not measurements of a real conveyor.
- The belt is a force field on objects, not a moving surface, so frictional effects at the contact patch (tipping under acceleration) are not modeled.
- The safety monitor uses a capsule for the person and spheres for the arm; it is a separation check, not a certified safety function.
