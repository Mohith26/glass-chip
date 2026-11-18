"""BookSim 2 configuration for the VX-1 4x4 mesh, derived from configs/soc/vx1.yaml.

Mapping from vx1.yaml `noc:` to BookSim:
* topology mesh, k = dims[0] = 4, n = 2. BookSim node id = x + 4*y, so the DRAM
  controller nodes [0,3] and [2,3] are nodes 12 and 14.
* routing xy -> routing_function = dim_order (x first, then y).
* virtual_channels -> num_vcs = 4. vc_buf_size = 8 flits (assumption; vx1.yaml does not
  give buffer depth).
* router_latency_cycles = 2 and link_latency_cycles = 1 -> routing_delay = 0 with
  speculative VC/switch allocation (vc_alloc_delay = sw_alloc_delay = 1, st_final_delay
  = 1). I checked this with the trace mode: each extra hop adds exactly 3 cycles
  (2 router + 1 link); see calib/booksim/hop_check.csv.
* flit size = link_bits / 8 = 32 B. A 64 B cache-line transfer is 2 flits; the vx1.yaml
  max_packet_bytes of 256 B is 8 flits. No separate head flit: BookSim carries the
  header in the first flit.
* cycle = 1 / noc.freq_hz (1.5 GHz -> 0.667 ns).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
BOOKSIM = ROOT / "third_party" / "booksim2" / "src" / "booksim"


def load_noc() -> dict[str, Any]:
    with open(ROOT / "configs" / "soc" / "vx1.yaml") as f:
        noc = yaml.safe_load(f)["noc"]
    # PyYAML (YAML 1.1) reads "1.5e9" as a string; coerce numeric fields.
    noc["freq_hz"] = float(noc["freq_hz"])
    return noc


def node_id(x: int, y: int, k: int = 4) -> int:
    return x + k * y


def dram_nodes() -> list[int]:
    with open(ROOT / "configs" / "soc" / "vx1.yaml") as f:
        soc = yaml.safe_load(f)
    return [node_id(x, y) for x, y in soc["blocks"]["dram"]["nodes"]]


def base_config(noc: dict[str, Any] | None = None, vc_buf: int = 8) -> dict[str, Any]:
    noc = noc or load_noc()
    assert noc["topology"] == "mesh" and noc["routing"] == "xy"
    assert noc["router_latency_cycles"] == 2 and noc["link_latency_cycles"] == 1, \
        "delay mapping below was calibrated for 2-cycle routers and 1-cycle links"
    return {
        "topology": "mesh",
        "k": noc["dims"][0],
        "n": 2,
        "routing_function": "dim_order",
        "num_vcs": noc["virtual_channels"],
        "vc_buf_size": vc_buf,
        "wait_for_tail_credit": 0,
        "vc_allocator": "separable_input_first",
        "sw_allocator": "separable_input_first",
        "alloc_iters": 1,
        "credit_delay": 1,
        "routing_delay": 0,
        "vc_alloc_delay": 1,
        "sw_alloc_delay": 1,
        "speculative": 1,
        "st_final_delay": 1,
        "input_speedup": 1,
        "output_speedup": 1,
        "internal_speedup": 1.0,
    }


def flit_bytes(noc: dict[str, Any] | None = None) -> int:
    noc = noc or load_noc()
    return noc["link_bits"] // 8


def write_cfg(path: Path, cfg: dict[str, Any]) -> Path:
    lines = []
    for k, v in cfg.items():
        lines.append(f"{k} = {v};")
    path.write_text("\n".join(lines) + "\n")
    return path
