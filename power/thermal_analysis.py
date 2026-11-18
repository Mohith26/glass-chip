"""Thermal analysis helpers that reuse the PowerManager's models (used by study s08).

* `steady_state_with_leakage`: fixed-point solve of junction temperatures when leakage
  depends on temperature (leakage -> power -> temperature -> leakage). Detects thermal
  runaway (no fixed point below a cap).
* `open_loop_onset`: time for the hottest junction to reach the throttle threshold from a
  given initial state, driving the RC network with a constant non-leakage power per domain
  and recomputing leakage and regulator loss every step. Valid up to the onset (before onset
  no throttling changes the power), which is all it reports.

Both are pure model calculations on top of power measured in a co-sim run; they are not
co-simulation results themselves.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .thermal import build_soc_thermal


def _domain_power(pm: Any, p_nonleak: Mapping[str, float], volts: Mapping[str, float], temps: Mapping[str, float],
                  gated_frac: Mapping[str, float] | None = None) -> tuple[dict[str, float], float]:
    out: dict[str, float] = {}
    loss = 0.0
    gf = gated_frac or {}
    for d in pm.domains:
        v_rest = pm.energy.voltages_for(d, volts[d])[1]
        leak = pm.leakage.power_w(d, v_rest, temps[d])
        g = float(gf.get(d, 0.0))
        leak = leak * (1 - g) + leak * pm.gating.retention_leak_fraction * g
        p = float(p_nonleak.get(d, 0.0)) + leak
        out[d] = p
        loss += pm.regulators[d].loss_w(p)
    return out, loss


def steady_state_with_leakage(pm: Any, p_nonleak: Mapping[str, float], volts: Mapping[str, float], ambient_c: float,
                              gated_frac: Mapping[str, float] | None = None, params: Mapping[str, Any] | None = None,
                              t_cap_c: float = 200.0, iters: int = 200) -> dict[str, Any]:
    """Steady-state temperatures with temperature-dependent leakage (fixed-point iteration)."""
    net = build_soc_thermal(pm.soc, params or pm.params, pm.domains, ambient_c)
    temps = {n: ambient_c for n in net.names}
    runaway = False
    for _ in range(iters):
        pw, loss = _domain_power(pm, p_nonleak, volts, temps, gated_frac)
        pw = dict(pw)
        pw[pm.reg_loss_node] = pw.get(pm.reg_loss_node, 0.0) + loss
        ss = net.steady_state(pw)
        new = {n: float(ss[i]) for i, n in enumerate(net.names)}
        if max(new.values()) > t_cap_c:
            runaway = True
            temps = new
            break
        if max(abs(new[n] - temps[n]) for n in net.names) < 1e-6:
            temps = new
            break
        temps = new
    pw, loss = _domain_power(pm, p_nonleak, volts, temps, gated_frac)
    return {"temps_c": temps, "t_max_c": max(temps[d] for d in pm.domains), "runaway": runaway,
            "power_w": sum(pw.values()) + loss, "leakage_w": sum(pw.values()) - sum(p_nonleak.values())}


def open_loop_onset(pm: Any, p_nonleak: Mapping[str, float], volts: Mapping[str, float], ambient_c: float,
                    threshold_c: float, init_temps: Mapping[str, float] | None = None,
                    params: Mapping[str, Any] | None = None, dt_s: float = 0.5, t_max_s: float = 4 * 3600.0,
                    gated_frac: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Seconds until the hottest junction first exceeds threshold_c (None if it never does by t_max_s)."""
    net = build_soc_thermal(pm.soc, params or pm.params, pm.domains, ambient_c)
    if init_temps is not None:
        net.T = np.array([float(init_temps.get(n, ambient_c)) for n in net.names])
    t = 0.0
    while t < t_max_s:
        temps = net.temps()
        if max(temps[d] for d in pm.domains) >= threshold_c:
            return {"onset_s": t, "t_max_c": max(temps[d] for d in pm.domains)}
        pw, loss = _domain_power(pm, p_nonleak, volts, temps, gated_frac)
        pw[pm.reg_loss_node] = pw.get(pm.reg_loss_node, 0.0) + loss
        net.step(pw, dt_s)
        t += dt_s
    temps = net.temps()
    return {"onset_s": None, "t_max_c": max(temps[d] for d in pm.domains)}
