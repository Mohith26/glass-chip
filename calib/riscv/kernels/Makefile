# RV64 static binaries for gem5 SE, built with zig cc (LLVM 22 + ld.lld + musl).
# Apple clang 21 has no RISC-V backend, so it cannot be used here.
ROOT := $(abspath ../../..)
ZIG := $(ROOT)/.venv/bin/python -m ziglang
export ZIG_GLOBAL_CACHE_DIR := $(ROOT)/third_party/zig-cache
export ZIG_LOCAL_CACHE_DIR := $(ROOT)/third_party/zig-cache
CTRL := $(ROOT)/workloads/capture/control
OUT := ../build
TGT := -target riscv64-linux-musl -static -O2
RVV := -mcpu=baseline_rv64+v
CFLAGS := -fno-math-errno

all: $(OUT)/axpy $(OUT)/gemm $(OUT)/control_bench $(OUT)/hello_musl

$(OUT)/axpy: axpy.c bench.h
	$(ZIG) cc $(TGT) $(RVV) $(CFLAGS) $< -o $@
$(OUT)/gemm: gemm.c bench.h
	$(ZIG) cc $(TGT) $(RVV) $(CFLAGS) $< -o $@
# Control kernels: rv64gc for control/ssm and rv64gcv for estimate, as in count_rv64.py;
# one binary, so I build everything with +v and note it.
$(OUT)/control_bench: control_bench.cpp bench.h $(CTRL)/control.cpp $(CTRL)/estimate.cpp $(CTRL)/ssm.cpp
	$(ZIG) c++ $(TGT) $(RVV) $(CFLAGS) -std=c++20 -fno-exceptions -fno-rtti -I. -I$(CTRL) \
	  control_bench.cpp $(CTRL)/control.cpp $(CTRL)/estimate.cpp $(CTRL)/ssm.cpp -o $@
$(OUT)/hello_musl: hello.c
	$(ZIG) cc $(TGT) $< -o $@
