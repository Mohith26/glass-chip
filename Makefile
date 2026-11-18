# Top-level build: delegates to the simulation kernel in core/.
.PHONY: all core test core-test core-debug bench clean

all: core

core:
	$(MAKE) -C core module

test: core-test

core-test:
	$(MAKE) -C core test

core-debug:
	$(MAKE) -C core debug

bench:
	$(MAKE) -C core bench

clean:
	$(MAKE) -C core clean
