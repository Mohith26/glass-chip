# Builds the Ramulator 2.1 trace-replay driver against third_party/ramulator2.
ROOT := $(abspath ../..)
RAM := $(ROOT)/third_party/ramulator2
CXX = $(ROOT)/third_party/wrap/clang++-noq
CXXFLAGS = -std=c++20 -O2 -DFMT_CONSTEVAL= -I$(RAM)/src -I$(RAM)/ext/fmt/include -I$(RAM)/ext/yaml-cpp/include
LDFLAGS = -L$(RAM) -lramulator -Wl,-rpath,$(RAM)

trace_driver: trace_driver.cpp $(RAM)/libramulator.dylib
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

clean:
	rm -f trace_driver
