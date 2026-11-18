"""Leakage power per domain: P_leak = V * I0 * exp(cT * (T - T0)).

The exponential temperature dependence is a common first-order fit for
subthreshold leakage over a limited temperature range (the form is prescribed
by the Glass Chip spec, Section 8.1; it ignores DIBL, so leakage falls only
linearly with V here, which is conservative for low-voltage savings).
I0 per domain is an assumption: it is set so that leakage at (v_nominal, T0) is
a fixed fraction of the domain's estimated peak dynamic power.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


class LeakageModel:
    """Temperature-dependent leakage per power domain."""

    def __init__(self, i0_a: Mapping[str, float], c_t_per_c: float, t0_c: float):
        self.i0_a = dict(i0_a)
        self.c_t = float(c_t_per_c)
        self.t0_c = float(t0_c)

    @classmethod
    def from_params(cls, params: Mapping[str, Any], peak_dyn_nominal_w: Mapping[str, float],
                    v_nominal_by_domain: Mapping[str, float]) -> "LeakageModel":
        lk = params["leakage"]
        frac = float(lk["fraction_of_peak_dynamic"])
        i0 = {d: frac * p / v_nominal_by_domain[d] for d, p in peak_dyn_nominal_w.items()}
        return cls(i0, float(lk["c_t_per_c"]), float(lk["t0_c"]))

    T_CLAMP_C = 175.0   # the fit is meaningless far above Tj limits; clamp to keep runaway finite

    def power_w(self, domain: str, v: float, t_c: float) -> float:
        """Leakage power of `domain` at supply v (volts) and temperature t_c (Celsius)."""
        t = min(t_c, self.T_CLAMP_C)
        return v * self.i0_a[domain] * math.exp(self.c_t * (t - self.t0_c))
