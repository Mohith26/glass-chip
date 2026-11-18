"""gem5 SE-mode runner for the Glass Chip RISC-V kernels, with xcol.sdf latency knobs.

Same system as calib/gem5/se_run.py (one core, 64 KiB L1I/L1D, 1 MiB L2, 2 GHz,
VLEN 256). Extra options for the xcol build (third_party/gem5-xcol, branch
glasschip-xcol, binary build/RISCV_XCOL/gem5.opt):
    --xcol-lat N        result latency of xcol.sdf in cycles (adds a dedicated unit)
    --xcol-issue N      MinorCPU: cycles between two xcol.sdf issues on that unit
    --xcol-pipelined 0|1  O3CPU: 1 = a new xcol.sdf can start every cycle,
                        0 = the unit is busy for the full latency
Without --xcol-lat the stock functional-unit pools are used.

Original docstring follows.

gem5 SE-mode runner for RV64 kernels (Glass Chip L3 CPU reference).

Builds one RISC-V core with private L1I/L1D and an L2 sized like a VX-1 app CPU
(configs/soc/vx1.yaml: 64 KiB L1I/L1D, 1 MiB L2 per cluster, 2.0 GHz) in front of a
simple DDR model, then runs a static RV64 binary in syscall-emulation mode.

Run with gem5 itself:
    third_party/gem5/build/RISCV/gem5.opt --outdir=OUT calib/gem5/se_run.py \
        --cpu timing|minor|o3 --binary path/to/rv64_static [--args "..."] [--vlen 256]

Cache latencies and the DRAM model are gem5 defaults or simple assumptions, labeled in
the outputs; this is a reference for relative CPI, not a model of a specific product.
"""

import argparse

import m5
from m5.objects import (
    AddrRange,
    Cache,
    DDR4_2400_8x8,
    MemCtrl,
    Process,
    Root,
    RiscvMinorCPU,
    RiscvO3CPU,
    RiscvTimingSimpleCPU,
    SEWorkload,
    SrcClockDomain,
    SystemXBar,
    L2XBar,
    System,
    VoltageDomain,
)

ap = argparse.ArgumentParser()
ap.add_argument("--cpu", default="timing", choices=["timing", "minor", "o3"])
ap.add_argument("--binary", required=True)
ap.add_argument("--args", default="")
ap.add_argument("--freq", default="2GHz")
ap.add_argument("--vlen", type=int, default=256)
ap.add_argument("--l1kb", type=int, default=64)
ap.add_argument("--l2kb", type=int, default=1024)
ap.add_argument("--xcol-lat", type=int, default=0)
ap.add_argument("--xcol-issue", type=int, default=1)
ap.add_argument("--xcol-pipelined", type=int, default=1)
a = ap.parse_args()


class L1(Cache):
    assoc = 4
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 16
    tgts_per_mshr = 20


class L2(Cache):
    assoc = 16
    tag_latency = 12
    data_latency = 12
    response_latency = 12
    mshrs = 32
    tgts_per_mshr = 12


system = System()
system.clk_domain = SrcClockDomain(clock=a.freq, voltage_domain=VoltageDomain())
system.mem_mode = "timing"
system.mem_ranges = [AddrRange("2GiB")]

cpu_cls = {"timing": RiscvTimingSimpleCPU, "minor": RiscvMinorCPU, "o3": RiscvO3CPU}[a.cpu]
system.cpu = cpu_cls()

if a.xcol_lat > 0:
    if a.cpu == "o3":
        from m5.objects import (
            FP_ALU, FP_MultDiv, FUDesc, FUPool, IntALU, IntMultDiv, Matrix_Unit, OpDesc,
            PredALU, RdWrPort, ReadPort, SIMD_Unit, System_Unit, WritePort,
        )
        # gem5 26 O3: the pool that issue uses is the IQ unit's (cpu.instQueues[0].fuPool).
        system.cpu.instQueues[0].fuPool = FUPool(FUList=[
            IntALU(), IntMultDiv(), FP_ALU(), FP_MultDiv(), ReadPort(), SIMD_Unit(),
            Matrix_Unit(), System_Unit(), PredALU(), WritePort(), RdWrPort(),
            FUDesc(opList=[OpDesc(opClass="XcolSdf", opLat=a.xcol_lat,
                                  pipelined=bool(a.xcol_pipelined))], count=1),
        ])
    elif a.cpu == "minor":
        from m5.objects import (
            MinorDefaultFloatSimdFU, MinorDefaultIntDivFU, MinorDefaultIntFU,
            MinorDefaultIntMulFU, MinorDefaultMemFU, MinorDefaultMiscFU, MinorDefaultPredFU,
            MinorFU, MinorFUPool,
        )
        from m5.objects.BaseMinorCPU import minorMakeOpClassSet
        system.cpu.executeFuncUnits = MinorFUPool(funcUnits=[
            MinorDefaultIntFU(), MinorDefaultIntFU(), MinorDefaultIntMulFU(),
            MinorDefaultIntDivFU(), MinorDefaultFloatSimdFU(), MinorDefaultPredFU(),
            MinorDefaultMemFU(), MinorDefaultMiscFU(),
            MinorFU(opClasses=minorMakeOpClassSet(["XcolSdf"]), opLat=a.xcol_lat,
                    issueLat=a.xcol_issue),
        ])

system.cpu.icache = L1(size=f"{a.l1kb}KiB")
system.cpu.dcache = L1(size=f"{a.l1kb}KiB")
system.cpu.icache_port = system.cpu.icache.cpu_side
system.cpu.dcache_port = system.cpu.dcache.cpu_side

system.l2bus = L2XBar()
system.cpu.icache.mem_side = system.l2bus.cpu_side_ports
system.cpu.dcache.mem_side = system.l2bus.cpu_side_ports
system.l2cache = L2(size=f"{a.l2kb}KiB")
system.l2cache.cpu_side = system.l2bus.mem_side_ports

system.membus = SystemXBar()
system.l2cache.mem_side = system.membus.cpu_side_ports
system.cpu.createInterruptController()
system.system_port = system.membus.cpu_side_ports

system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = DDR4_2400_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

system.workload = SEWorkload.init_compatible(a.binary)
process = Process()
process.cmd = [a.binary] + (a.args.split() if a.args else [])
system.cpu.workload = process
system.cpu.createThreads()
system.cpu.isa[0].vlen = a.vlen  # RVV VLEN in bits (vx1.yaml vlen_bits)

root = Root(full_system=False, system=system)
m5.instantiate()
ev = m5.simulate()
print(f"GEM5_EXIT tick={m5.curTick()} cause={ev.getCause()}")
