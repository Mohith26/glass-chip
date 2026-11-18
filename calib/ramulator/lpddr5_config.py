"""Build Ramulator 2.1 LPDDR5 configs that match the VX-1 DRAM block.

Ramulator 2.1 ships LPDDR5 timing presets only up to LPDDR5-6400. VX-1 asks for
LPDDR5X-8533. I produce two configs:

* ``lpddr5_6400``: the stock JEDEC-derived LPDDR5_6400 preset (nearest shipped).
* ``lpddr5x_8533_derived``: the LPDDR5 state machine with tCK = 938 ps
  (8533 MT/s with a 4:1 WCK:CK ratio, 937.5 ps rounded up to an integer ps) and
  core timings converted from the nanosecond values in configs/soc/vx1.yaml.
  Timings that vx1.yaml does not give are scaled from the 6400 preset at constant
  nanoseconds. This is an extrapolation, not a JEDEC LPDDR5X preset.

Organization: VX-1 has a 128-bit bus. LPDDR5 channels are x16, so I model eight
x16 channels (one Ramulator controller each) interleaved at 64 B granularity.
That is the same 128 bits as vx1.yaml's 2 controllers x 2 channels x 32 bits.
Ramulator's LPDDR5 bank-group mode is 4 bank groups x 4 banks (JEDEC); vx1.yaml
says 4 x 8, which LPDDR5 does not define, so I keep the JEDEC 16 banks and note it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
RAM_PY = ROOT / "third_party" / "ramulator2" / "python"
if str(RAM_PY) not in sys.path:
    sys.path.insert(0, str(RAM_PY))

import ramulator  # noqa: E402
from ramulator.export import dict_to_yaml  # noqa: E402

# Stock LPDDR5_6400 cycle counts (tCK = 1250 ps), copied from Ramulator's preset table
# so the derivation below is explicit.
_P6400 = {
    "nCL": 17, "nRCD": 15, "nRP": 15, "nRPab": 17, "nRAS": 34, "nRC": 49, "nWR": 28,
    "nRTP": 6, "nCWL": 9, "nWTRS": 5, "nWTRL": 10,
}
_TCK6400 = 1250


def _cycles(ns: float, tck_ps: int) -> int:
    return int(math.ceil(ns * 1000.0 / tck_ps - 1e-9))


def derived_8533_overrides(vx1_dram: dict[str, Any], tck_ps: int = 938) -> dict[str, int]:
    """Timing overrides for an LPDDR5X-8533-like device.

    vx1.yaml timing_ns keys used: tRCD, tRP, CL, tRAS, tWTR. Others keep the
    6400 preset's nanosecond value.
    """
    t = vx1_dram.get("timing_ns", {})
    o: dict[str, int] = {}
    for k, v in _P6400.items():
        o[k] = _cycles(v * _TCK6400 / 1000.0, tck_ps)  # constant-ns scaling
    if "tRCD" in t:
        o["nRCD"] = _cycles(t["tRCD"], tck_ps)
    if "tRP" in t:
        o["nRP"] = _cycles(t["tRP"], tck_ps)
        o["nRPab"] = max(o["nRPab"], o["nRP"] + 2)
    if "CL" in t:
        o["nCL"] = _cycles(t["CL"], tck_ps)
    if "tRAS" in t:
        o["nRAS"] = _cycles(t["tRAS"], tck_ps)
    o["nRC"] = max(o["nRC"], o["nRAS"] + o["nRP"])
    if "tWTR" in t:
        o["nWTRL"] = _cycles(t["tWTR"], tck_ps)
    o["rate"] = int(vx1_dram.get("data_rate_mts", 8533))
    o["tCK_ps"] = tck_ps
    return o


def load_vx1(path: Path | None = None) -> dict[str, Any]:
    p = path or ROOT / "configs" / "soc" / "vx1.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def build(variant: str, channels: int = 8, frontend: str = "External",
          read_buffer: int = 32, write_buffer: int = 32, refresh: bool = True) -> dict[str, Any]:
    """Return a Ramulator top-level config dict (frontend + memory_system)."""
    soc = load_vx1()
    dram_cfg = soc["blocks"]["dram"]
    if variant == "lpddr5_6400":
        dram = ramulator.dram.LPDDR5(org_preset="LPDDR5_16Gb_x16", timing_preset="LPDDR5_6400")
    elif variant == "lpddr5x_8533_derived":
        dram = ramulator.dram.LPDDR5(org_preset="LPDDR5_16Gb_x16", timing_preset="LPDDR5_6400",
                                     **derived_8533_overrides(dram_cfg))
    else:
        raise ValueError(variant)
    ctrl = ramulator.controller.LPDDR5(
        dram=dram,
        scheduler=ramulator.scheduler.FRFCFS(),
        refresh_manager=(ramulator.refresh_manager.AllBank() if refresh
                         else ramulator.refresh_manager.NoRefresh()),
        row_policy=ramulator.row_policy.Open(),
        addr_mapper=ramulator.addr_mapper.RoBaRaCoCh(),
        read_buffer_size=read_buffer,
        write_buffer_size=write_buffer,
    )
    mem = ramulator.memory_system.GenericDRAM(
        clock_ratio=1,
        controllers=[ctrl] * channels,
        channel_mapper=ramulator.channel_mapper.CacheLineInterleave(),
    )
    fe = ramulator.frontend.External(clock_ratio=1)
    return {"frontend": fe.to_config(), "memory_system": mem.to_config()}


def peak_gbps(variant: str, channels: int = 8) -> float:
    rate = 6400 if variant == "lpddr5_6400" else load_vx1()["blocks"]["dram"]["data_rate_mts"]
    return rate * 1e6 * 2 * channels / 1e9  # x16 = 2 bytes per transfer per channel


def write_yaml(variant: str, out: Path, **kw: Any) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dict_to_yaml(build(variant, **kw)))
    return out


if __name__ == "__main__":
    for v in ("lpddr5_6400", "lpddr5x_8533_derived"):
        p = write_yaml(v, ROOT / "calib" / "ramulator" / "configs" / f"{v}.yaml")
        print(p, peak_gbps(v))
    print(derived_8533_overrides(load_vx1()["blocks"]["dram"]))
