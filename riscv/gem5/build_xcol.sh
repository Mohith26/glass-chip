#!/bin/bash
# Builds the Glass Chip xcol variant of gem5 (branch glasschip-xcol, worktree third_party/gem5-xcol)
# into build/RISCV_XCOL so the calibration build at third_party/gem5/build/RISCV stays untouched.
set -e
ROOT=/Users/mohithgajjela/glass-chip
UVPY=/Users/mohithgajjela/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none
cd $ROOT/third_party/gem5-xcol
export PATH=$ROOT/.venv/bin:$UVPY/bin:$PATH LIBRARY_PATH=$UVPY/lib
export CC=$ROOT/third_party/wrap/clang-noq CXX=$ROOT/third_party/wrap/clang++-noq
export LINKFLAGS_EXTRA="-Wl,-rpath,$UVPY/lib" GEM5PY_LINKFLAGS_EXTRA="-Wl,-rpath,$UVPY/lib"
if [ ! -f build/RISCV_XCOL/gem5.build/kconfig ] && [ ! -f build/RISCV_XCOL/config ]; then
  python -u $ROOT/.venv/bin/scons defconfig build/RISCV_XCOL build_opts/RISCV --ignore-style
fi
python -u $ROOT/.venv/bin/scons build/RISCV_XCOL/gem5.opt -j12 --ignore-style
xattr -c build/RISCV_XCOL/gem5.opt || true
