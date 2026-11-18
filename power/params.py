"""Loading helpers for the SoC config and the power parameter file.

All numbers in the power parameter file are assumptions (see power/README.md).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOC_PATH = ROOT / "configs" / "soc" / "vx1.yaml"
DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent / "params_vx1.yaml"
POLICY_DIR = ROOT / "configs" / "policies"

DOMAINS: tuple[str, ...] = ("cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io")


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Read a YAML file into a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_soc(path: str | Path | None = None) -> dict[str, Any]:
    """Load the SoC config (defaults to configs/soc/vx1.yaml)."""
    return load_yaml(path or DEFAULT_SOC_PATH)


def load_params(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load power parameters and apply a shallow-recursive override dict."""
    params = load_yaml(path or DEFAULT_PARAMS_PATH)
    if overrides:
        params = deep_merge(params, overrides)
    return params


def load_policy(name_or_path: str | Path) -> dict[str, Any]:
    """Load a policy config by id prefix (e.g. "P4") or by path."""
    p = Path(name_or_path)
    if p.exists():
        cfg = load_yaml(p)
    else:
        matches = sorted(POLICY_DIR.glob(f"{name_or_path}_*.yaml")) + sorted(POLICY_DIR.glob(f"{name_or_path}.yaml"))
        if not matches:
            raise FileNotFoundError(f"no policy config for {name_or_path!r} in {POLICY_DIR}")
        cfg = load_yaml(matches[0])
        cfg.setdefault("_path", str(matches[0]))
    return cfg


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of base with over merged in recursively."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def domain_blocks(soc: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """All blocks (plus the NoC section) that sit in a power domain."""
    out = [b for b in soc.get("blocks", {}).values() if b.get("power_domain") == domain]
    noc = soc.get("noc")
    if noc and noc.get("power_domain") == domain:
        out.append(noc)
    return out


def domain_area_fraction(soc: dict[str, Any], domain: str) -> float:
    """Die area fraction of a domain from the floorplan rectangles in the SoC config."""
    total = 0.0
    for b in soc.get("blocks", {}).values():
        if b.get("power_domain") == domain and "floorplan" in b:
            fp = b["floorplan"]
            total += float(fp["w"]) * float(fp["h"])
    return total
