# glasschip-core build (plain Makefile, no CMake needed).
#   make            build the Python module build/glasschip_core*.so
#   make test       build + run C++ unit tests and pytest
#   make debug      ASan/UBSan build of the C++ unit tests
#   make clean

ROOT     := $(abspath ..)
PY       := $(ROOT)/.venv/bin/python
CXX      := clang++
BUILD    := build
CXXSTD   := -std=c++20
WARN     := -Wall -Wextra -Wpedantic -Wno-gnu-zero-variadic-macro-arguments
OPT      ?= -O3 -flto=thin
CXXFLAGS := $(CXXSTD) $(OPT) $(WARN) -fPIC -Iinclude -fvisibility=hidden

PYINC    := $(shell $(PY) -m pybind11 --includes)
EXT      := $(shell $(PY) -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
MODULE   := $(BUILD)/glasschip_core$(EXT)

CORE_SRC := src/kernel/json.cpp src/kernel/config.cpp src/components/memsys.cpp \
            src/components/engines.cpp src/runtime/runtime.cpp src/sim.cpp
CORE_OBJ := $(patsubst %.cpp,$(BUILD)/obj/%.o,$(CORE_SRC))
HDRS     := $(wildcard include/glasschip/*.hpp)

TEST_SRC := tests/cpp/test_main.cpp
TEST_BIN := $(BUILD)/test_core

.PHONY: all module test cpptest pytest debug debug-asan clean bench

all: module

module: $(MODULE)

$(BUILD)/obj/%.o: %.cpp $(HDRS)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/obj/bindings/module.o: bindings/module.cpp $(HDRS)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(PYINC) -Wno-pedantic -c $< -o $@

$(MODULE): $(CORE_OBJ) $(BUILD)/obj/bindings/module.o
	$(CXX) $(OPT) -shared -undefined dynamic_lookup $^ -o $@

$(BUILD)/vx1.json: $(ROOT)/configs/soc/vx1.yaml
	@mkdir -p $(BUILD)
	$(PY) -c "import yaml,json,sys;json.dump(yaml.safe_load(open('$<')),open('$@','w'))"

$(TEST_BIN): $(CORE_OBJ) $(TEST_SRC) $(HDRS)
	$(CXX) $(CXXFLAGS) $(TEST_SRC) $(CORE_OBJ) -o $@

cpptest: $(TEST_BIN) $(BUILD)/vx1.json
	./$(TEST_BIN) $(BUILD)/vx1.json

pytest: $(MODULE)
	$(PY) -m pytest -q tests

test: cpptest pytest

# UBSan in trap mode plus libc++ debug hardening (bounds-checked containers).
# Needs no sanitizer runtime, so it also works inside sandboxes where the
# ASan/UBSan runtimes cannot start.
debug: $(BUILD)/vx1.json
	@mkdir -p $(BUILD)/debug
	$(CXX) $(CXXSTD) -O1 -g $(WARN) -Iinclude -fsanitize=undefined -fsanitize-trap=undefined \
	  -D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_DEBUG -fno-omit-frame-pointer \
	  $(CORE_SRC) $(TEST_SRC) -o $(BUILD)/debug/test_core_hardened
	./$(BUILD)/debug/test_core_hardened $(BUILD)/vx1.json

# Full AddressSanitizer + UBSan (runtime required; use on a normal machine).
debug-asan: $(BUILD)/vx1.json
	@mkdir -p $(BUILD)/debug
	$(CXX) $(CXXSTD) -O1 -g $(WARN) -Iinclude -fsanitize=address,undefined -fno-omit-frame-pointer \
	  $(CORE_SRC) $(TEST_SRC) -o $(BUILD)/debug/test_core_asan
	./$(BUILD)/debug/test_core_asan $(BUILD)/vx1.json

bench: $(MODULE)
	$(PY) bench/bench_speed.py

clean:
	rm -rf $(BUILD)
