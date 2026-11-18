"""DVFS mechanics: voltage-regulator efficiency, transition energy, power gating.

All coefficients are assumptions documented in power/params_vx1.yaml.

Regulator loss model (generic buck converter shape):
    x      = P_out / P_rated
    P_loss = P_rated * (a0 + a1 * x + a2 * x^2)
    eta    = P_out / (P_out + P_loss)
a0 is fixed switching/controller loss, a1 a load-proportional term, a2 conduction
(I^2 R) loss. Efficiency is poor at very light load, peaks mid-range, and droops
at heavy load.

DVFS transition energy: 0.5 * C_rail * |V1^2 - V0^2| (charging the rail and its decap).
Power gating: the domain draws retention leakage while gated; each wake costs
C_rail * V^2 (recharging the rail) and `wake_latency_us` of delay (the delay is
applied by the simulator, if it supports gating).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True)
class RegulatorModel:
    """Load-dependent voltage regulator efficiency for one domain."""

    rated_w: float
    a0: float
    a1: float
    a2: float

    def loss_w(self, p_out_w: float) -> float:
        x = max(p_out_w, 0.0) / self.rated_w
        return self.rated_w * (self.a0 + self.a1 * x + self.a2 * x * x)

    def input_w(self, p_out_w: float) -> float:
        """Power drawn from the supply to deliver p_out_w."""
        return max(p_out_w, 0.0) + self.loss_w(p_out_w)

    def efficiency(self, p_out_w: float) -> float:
        p = max(p_out_w, 0.0)
        return p / (p + self.loss_w(p)) if p > 0 else 0.0

    def max_efficiency(self) -> float:
        """Best efficiency over the load range (analytic optimum x* = sqrt(a0/a2))."""
        xs = np.concatenate([np.linspace(1e-4, 2.0, 4000), [np.sqrt(self.a0 / self.a2) if self.a2 > 0 else 1.0]])
        return float(max(self.efficiency(x * self.rated_w) for x in xs))


@dataclass(frozen=True)
class GatingParams:
    wake_latency_ps: int
    retention_leak_fraction: float
    wake_cap_f: float
    gateable: tuple[str, ...]
    idle_before_gate_ps: int = 0

    def wake_energy_j(self, v: float) -> float:
        return self.wake_cap_f * v * v


def transition_energy_j(rail_cap_f: float, v0: float, v1: float) -> float:
    """Energy to move a rail from v0 to v1 (charging only; discharge energy is lost too, so use |dV^2|)."""
    return 0.5 * rail_cap_f * abs(v1 * v1 - v0 * v0)


def gating_from_params(params: Mapping[str, Any]) -> GatingParams:
    g = params["gating"]
    return GatingParams(
        wake_latency_ps=int(round(float(g["wake_latency_us"]) * 1e6)),
        retention_leak_fraction=float(g["retention_leak_fraction"]),
        wake_cap_f=float(g["wake_energy_rail_cap_nf"]) * 1e-9,
        gateable=tuple(g.get("gateable", [])),
        idle_before_gate_ps=int(round(float(g.get("idle_before_gate_us", 0.0)) * 1e6)),
    )
