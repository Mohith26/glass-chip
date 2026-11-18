# Native build + test of the control kernels (host clang), and RV64 assembly.
CXX = clang++
CXXFLAGS = -std=c++20 -O2 -Wall -Wextra -Werror
SRCS = control.cpp estimate.cpp ssm.cpp
OUT = ../../../build/workloads_control

test: $(OUT)/control_test
	$(OUT)/control_test

$(OUT)/control_test: $(SRCS) control_test.cpp control.h kmath.h
	mkdir -p $(OUT)
	$(CXX) $(CXXFLAGS) -o $@ $(SRCS) control_test.cpp

.PHONY: test
