"""Lumped RC thermal network and OPP throttling with hysteresis.

Network: one junction node per power domain, each with a resistance to a shared
package node; package to enclosure; enclosure to ambient. Node equation:

    C_i dT_i/dt = P_i - sum_j G_ij (T_i - T_j) - G_i,amb (T_i - T_amb)

Integrated with backward (implicit) Euler, which is unconditionally stable, so
1 ms steps are safe even when a junction time constant is a few ms:

    (C/dt + G) T[n+1] = (C/dt) T[n] + P + G_amb * T_amb

Steady state solves G T = P + G_amb * T_amb. A single node with R to ambient gives
T = T_amb + R * P, which the tests check.

All R and C values are assumptions (power/params_vx1.yaml). Lateral coupling between
domains is ignored (a documented simplification).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from .params import domain_area_fraction


class ThermalRC:
    """Generic lumped RC network with an ambient boundary."""

    def __init__(self, names: Sequence[str], cap_j_per_k: Sequence[float],
                 edges: Sequence[tuple[str, str, float]], ambient_c: float):
        """edges: (node_a, node_b, R in K/W); node_b may be "ambient"."""
        self.names = list(names)
        self.idx = {n: i for i, n in enumerate(self.names)}
        n = len(self.names)
        self.C = np.asarray(cap_j_per_k, dtype=float)
        if np.any(self.C <= 0):
            raise ValueError("thermal capacitances must be positive")
        self.G = np.zeros((n, n))
        self.g_amb = np.zeros(n)
        for a, b, r in edges:
            g = 1.0 / float(r)
            ia = self.idx[a]
            if b == "ambient":
                self.G[ia, ia] += g
                self.g_amb[ia] += g
            else:
                ib = self.idx[b]
                self.G[ia, ia] += g
                self.G[ib, ib] += g
                self.G[ia, ib] -= g
                self.G[ib, ia] -= g
        self.ambient_c = float(ambient_c)
        self.T = np.full(n, self.ambient_c)
        self._inv_cache: dict[float, np.ndarray] = {}

    def _power_vec(self, power_w: Mapping[str, float]) -> np.ndarray:
        p = np.zeros(len(self.names))
        for k, v in power_w.items():
            if k in self.idx:
                p[self.idx[k]] += float(v)
        return p

    def step(self, power_w: Mapping[str, float], dt_s: float) -> np.ndarray:
        """Advance by dt_s with constant node powers (implicit Euler). Returns temperatures."""
        if dt_s <= 0:
            return self.T
        key = round(dt_s, 12)
        inv = self._inv_cache.get(key)
        if inv is None:
            inv = np.linalg.inv(np.diag(self.C / dt_s) + self.G)
            self._inv_cache[key] = inv
        rhs = self.C / dt_s * self.T + self._power_vec(power_w) + self.g_amb * self.ambient_c
        self.T = inv @ rhs
        return self.T

    def steady_state(self, power_w: Mapping[str, float]) -> np.ndarray:
        rhs = self._power_vec(power_w) + self.g_amb * self.ambient_c
        return np.linalg.solve(self.G, rhs)

    def set_ambient(self, ambient_c: float) -> None:
        self.ambient_c = float(ambient_c)

    def temps(self) -> dict[str, float]:
        return {n: float(self.T[i]) for i, n in enumerate(self.names)}

    def temp(self, name: str) -> float:
        return float(self.T[self.idx[name]])


def build_soc_thermal(soc: Mapping[str, Any], params: Mapping[str, Any], domains: Sequence[str],
                      ambient_c: float) -> ThermalRC:
    """Build the per-domain + package + enclosure network from the floorplan and params."""
    th = params["thermal"]
    die = float(th["die_area_mm2"])
    r_spec = float(th["r_spec_k_mm2_per_w"])
    c_spec = float(th["c_spec_j_per_k_mm2"])
    min_area = float(th.get("min_node_area_mm2", 1.0))
    names = list(domains) + ["package", "enclosure"]
    caps: list[float] = []
    edges: list[tuple[str, str, float]] = []
    for d in domains:
        area = max(domain_area_fraction(soc, d) * die, min_area)
        caps.append(c_spec * area)
        edges.append((d, "package", r_spec / area))
    slow = float(th.get("slow_node_time_scale", 1.0))
    caps.append(float(th["package_c_j_per_k"]) / slow)
    caps.append(float(th["enclosure_c_j_per_k"]) / slow)
    edges.append(("package", "enclosure", float(th["package_to_enclosure_r_k_per_w"])))
    edges.append(("enclosure", "ambient", float(th["enclosure_to_ambient_r_k_per_w"])))
    return ThermalRC(names, caps, edges, ambient_c)


@dataclass
class ThrottleController:
    """Per-domain OPP cap with hysteresis.

    Above `throttle_c` the cap steps down one OPP every `step_ps`; below `release_c` it
    steps back up one OPP every `step_ps`; in between it holds. At or above `trip_c`
    (the Tj limit) the cap drops straight to the lowest OPP.
    """

    max_index: int
    throttle_c: float
    release_c: float
    trip_c: float
    step_ps: int
    cap: int = -1
    _last_change_ps: int = field(default=-(1 << 62))

    def __post_init__(self) -> None:
        if self.cap < 0:
            self.cap = self.max_index
        if not self.release_c < self.throttle_c <= self.trip_c:
            raise ValueError("need release_c < throttle_c <= trip_c")

    @property
    def throttled(self) -> bool:
        return self.cap < self.max_index

    def update(self, t_c: float, now_ps: int) -> int:
        if t_c >= self.trip_c:
            if self.cap != 0:
                self.cap = 0
                self._last_change_ps = now_ps
            return self.cap
        due = now_ps - self._last_change_ps >= self.step_ps
        if t_c > self.throttle_c and due and self.cap > 0:
            self.cap -= 1
            self._last_change_ps = now_ps
        elif t_c < self.release_c and due and self.cap < self.max_index:
            self.cap += 1
            self._last_change_ps = now_ps
        return self.cap
