# RISC-V planner and control kernels for Glass Chip (scalar, RVV, and the xcol.sdf study instruction).
# RV64 static binaries via zig cc (LLVM + ld.lld + musl); Apple clang has no RISC-V backend.
# Host reference binaries via Apple clang (same sources, no RVV, xcol replaced by its C reference).
ROOT := $(abspath ../..)
ZIG := $(ROOT)/.venv/bin/python -m ziglang
export ZIG_GLOBAL_CACHE_DIR := $(ROOT)/third_party/zig-cache
export ZIG_LOCAL_CACHE_DIR := $(ROOT)/third_party/zig-cache
CTRL := $(ROOT)/workloads/capture/control
OUT := ../build
# -fno-vectorize / -fno-slp-vectorize keep the scalar baselines scalar (intrinsics are unaffected).
# -ffp-contract=off: no FMA contraction, so scalar, RVV, xcol and host do the same IEEE ops.
COMMON := -O2 -ffp-contract=off -fno-math-errno -fno-vectorize -fno-slp-vectorize
RV := -target riscv64-linux-musl -static -mcpu=baseline_rv64+v $(COMMON)
HOSTCC := $(ROOT)/third_party/wrap/clang-noq
HOSTCXX := $(ROOT)/third_party/wrap/clang++-noq
KSRC := kbench.c fk.c esdf.c selfcol.c inputs.c
KHDR := gc_kern.h franka_spheres.h
CSRC := ctrl_bench.cpp control_rvv.cpp $(CTRL)/control.cpp

all: $(OUT)/kbench_rv64 $(OUT)/kbench_host $(OUT)/ctrl_rv64 $(OUT)/ctrl_host $(OUT)/kernels_rv64.dis

$(OUT)/kbench_rv64: $(KSRC) $(KHDR)
	@mkdir -p $(OUT)
	$(ZIG) cc $(RV) $(KSRC) -lm -o $@
$(OUT)/kbench_host: $(KSRC) $(KHDR)
	@mkdir -p $(OUT)
	$(HOSTCC) $(COMMON) $(KSRC) -lm -o $@
$(OUT)/ctrl_rv64: $(CSRC) $(CTRL)/control.h
	$(ZIG) c++ $(RV) -std=c++20 -fno-exceptions -fno-rtti -I$(CTRL) $(CSRC) -o $@
$(OUT)/ctrl_host: $(CSRC) $(CTRL)/control.h
	$(HOSTCXX) $(COMMON) -std=c++20 -fno-exceptions -fno-rtti -I$(CTRL) $(CSRC) -o $@
# Disassembly of the hot loops (to confirm the scalar builds have no vector code and xcol.sdf is emitted).
$(OUT)/kernels_rv64.dis: $(OUT)/kbench_rv64 $(OUT)/ctrl_rv64
	$(ZIG) objdump -d --no-show-raw-insn $(OUT)/kbench_rv64 > $@ 2>/dev/null || \
	  /opt/homebrew/opt/llvm/bin/llvm-objdump -d $(OUT)/kbench_rv64 > $@

clean:
	rm -f $(OUT)/kbench_rv64 $(OUT)/kbench_host $(OUT)/ctrl_rv64 $(OUT)/ctrl_host $(OUT)/kernels_rv64.dis

.PHONY: all clean
