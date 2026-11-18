// Tiny benchmark harness for gem5 SE runs: reads the user-level cycle and instret
// CSRs around the timed loop and prints them. iters comes from argv[1].
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static inline uint64_t rd_cycle(void) { uint64_t x; __asm__ volatile("rdcycle %0" : "=r"(x)); return x; }
static inline uint64_t rd_instret(void) { uint64_t x; __asm__ volatile("rdinstret %0" : "=r"(x)); return x; }

#define BENCH_REPORT(name, iters, c0, c1, i0, i1, chk)                                     \
  printf("BENCH %s iters=%d cycles=%llu instret=%llu check=%.6g\n", name, (int)(iters),   \
         (unsigned long long)((c1) - (c0)), (unsigned long long)((i1) - (i0)), (double)(chk))
