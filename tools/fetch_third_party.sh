#!/usr/bin/env bash
# Fetch the third-party assets the co-sim needs (gitignored, not vendored).
# Required: MuJoCo Menagerie Franka Panda meshes (sparse clone, about 80 MB).
# Optional L2/L3 simulators (BookSim 2, Ramulator 2, SCALE-Sim, gem5) are described in calib/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p third_party
if [ ! -d third_party/mujoco_menagerie/franka_emika_panda ]; then
  git clone -q --depth 1 --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie.git third_party/mujoco_menagerie
  git -C third_party/mujoco_menagerie sparse-checkout set franka_emika_panda universal_robots_ur5e
fi
echo "third_party ready: $(ls third_party)"
