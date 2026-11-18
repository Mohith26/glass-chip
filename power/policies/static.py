"""Static policies: P0-P2 (nominal V/f; they differ only in scheduler) and P3 race to idle."""
from __future__ import annotations

from .base import PolicyContext, PowerPolicy


class NominalPolicy(PowerPolicy):
    """P0 / P1 / P2: every DVFS domain at its nominal OPP (or the set_vmin pin)."""

    name = "nominal"

    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in ctx.dvfs_domains():
            v = ctx.vf[d]
            out[d] = v.pinned_index if v.pinned_index is not None else v.nominal_index
        return out


class RaceToIdlePolicy(PowerPolicy):
    """P3: every DVFS domain at its highest OPP; gateable domains are power-gated when idle.

    The gating itself (wake latency on the next job) is applied by the simulator through
    sim.set_power_gating if it exists; the manager does the energy accounting either way.
    """

    name = "race_to_idle"
    gating = True

    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in ctx.dvfs_domains():
            v = ctx.vf[d]
            out[d] = v.pinned_index if v.pinned_index is not None else v.max_index
        return out


class OfflineOptimalOnline(NominalPolicy):
    """P5 has no online form. Running the manager with P5 behaves like P0 and records the
    job trace that power/offline_opt.py turns into the minimum-energy lower bound."""

    name = "offline_optimal"
