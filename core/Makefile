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

.PHONY: all module test cpptest pytest debug debug-asan clean bench plugins

all: module plugins

module: $(MODULE)

$(BUILD)/obj/%.o: %.cpp $(HDRS)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Build id = first 16 hex of sha256 over all core sources, headers, bindings and
# the Makefile (sorted), so results can name the exact simulator they used.
ID_SRCS  := $(sort $(CORE_SRC) $(HDRS) bindings/module.cpp src/adapters/ramulator_backend.cpp src/adapters/booksim_backend.cpp Makefile)
BUILD_ID := $(shell cat $(ID_SRCS) | shasum -a 256 | cut -c1-16)

$(BUILD)/obj/bindings/module.o: bindings/module.cpp $(HDRS) $(CORE_SRC) Makefile
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(PYINC) -Wno-pedantic -DGC_BUILD_ID=\"$(BUILD_ID)\" -c $< -o $@
	@echo $(BUILD_ID) > $(BUILD)/BUILD_ID

# Link to a temp file and rename into place: processes that already loaded
# the old module keep their mapping, new imports see the new one.
$(MODULE): $(CORE_OBJ) $(BUILD)/obj/bindings/module.o
	$(CXX) $(OPT) -shared -undefined dynamic_lookup $^ -o $@.tmp.$$$$ && xattr -c $@.tmp.$$$$ 2>/dev/null; mv -f $@.tmp.$$$$ $@

# L2 backend plugins, dlopen()ed at run time (the main module never links them).
# Linked with the quarantine-clearing wrapper so the sandbox does not kill the loader.
RAM       := $(ROOT)/third_party/ramulator2
NOQ       := $(ROOT)/third_party/wrap/clang++-noq
RAM_PLUGIN := $(BUILD)/libgc_ramulator.dylib

BS        := $(ROOT)/third_party/booksim2/src
BS_PLUGIN := $(BUILD)/libgc_booksim.dylib

plugins: $(RAM_PLUGIN) $(BS_PLUGIN)

# BookSim 2 in-process: its object files (minus main.o) + our adapter.
$(BS_PLUGIN): src/adapters/booksim_backend.cpp include/glasschip/memsys.hpp
	@if [ -f $(BS)/trafficmanager.o ]; then \
	  $(NOQ) -std=c++17 -O2 -fPIC -shared -Iinclude -I$(BS) -I$(BS)/allocators -I$(BS)/arbiters -I$(BS)/networks -I$(BS)/power -I$(BS)/routers \
	    $< $$(ls $(BS)/*.o $(BS)/*/*.o | grep -v '/main.o$$') -o $@.tmp && xattr -c $@.tmp 2>/dev/null; mv -f $@.tmp $@; \
	else echo "skip $@: BookSim objects not built in $(BS)"; fi

$(RAM_PLUGIN): src/adapters/ramulator_backend.cpp include/glasschip/memsys.hpp
	@if [ -f $(RAM)/libramulator.dylib ]; then \
	  $(NOQ) -std=c++20 -O2 -fPIC -shared -DFMT_CONSTEVAL= -Iinclude -I$(RAM)/src -I$(RAM)/ext/fmt/include -I$(RAM)/ext/yaml-cpp/include \
	    $< -o $@.tmp -L$(RAM) -lramulator -Wl,-rpath,$(RAM) && xattr -c $@.tmp 2>/dev/null; mv -f $@.tmp $@; \
	else echo "skip $@: $(RAM)/libramulator.dylib not built"; fi

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
