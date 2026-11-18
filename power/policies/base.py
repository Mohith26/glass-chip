"""Policy interface shared by P0-P6.

A policy turns the current state (pending jobs, OPP tables, thermal caps,
execution statistics) into one OPP index per DVFS-capable domain. The manager
applies the thermal cap and the set_vmin floor/pin afterwards, and calls
sim.set_domain_freq for any domain whose OPP changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..predictor import ExecTimePredictor
from ..vf import DomainVF


@dataclass
class PolicyContext:
    now_ps: int
    step_ps: int
    vf: Mapping[str, DomainVF]
    current: Mapping[str, int]
    pending: list[dict[str, Any]]
    predictor: ExecTimePredictor
    parallel_units: Mapping[str, int] = field(default_factory=dict)
    utilization: Mapping[str, float] = field(default_factory=dict)   # busy fraction, last interval

    def dvfs_domains(self) -> list[str]:
        return [d for d, v in self.vf.items() if not v.fixed]


class PowerPolicy:
    """Base class. Subclasses override decide()."""

    name = "base"
    gating = False          # True: power-gate idle gateable domains (race to idle)
    static = True           # True: the OPP does not depend on load (set_vmin pins voltage)

    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = dict(params or {})

    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "gating": self.gating, "params": self.params}
