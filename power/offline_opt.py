"""P5 offline optimal: minimum-energy OPP assignment for a recorded job trace (a lower bound).

Given a trace of jobs (release, deadline, cycles per domain) I solve, per DVFS domain,
a mixed-integer program with HiGHS (through scipy.optimize.milp):

  slots s of length dt (default 1 ms), OPPs o with frequency f_o and voltage V_o
  y[s,o] in {0,1}  one OPP per slot
  x[j,s,o] >= 0    cycles of job j executed in slot s at OPP o
  sum_o y[s,o] = 1                                  for each slot
  sum_j x[j,s,o] <= f_o * dt * y[s,o]               capacity
  sum_{s in window(j), o} x[j,s,o] = C_j            every job finishes by its deadline
  minimize  (1/eta_max) * sum [ x * (e_o + (P_busy(o) - P_idle(o)) / f_o) + y * dt * P_idle(o) ]
  (implemented in an aggregated form with per-slot work variables; see solve_domain)

  e_o      = e_nom * (V_o / V_nom)^2      switching energy per busy cycle (from the run's counters)
  P_busy(o) = P_clk(V_o, f_o) + P_leak(V_o, T_ref)
  P_idle(o) = retention * P_leak(V_o, T_ref)   if idle gating is allowed for the domain
            = P_busy(o)                        otherwise

Simplifications, each of which can only lower the optimum, so the result stays a
lower bound on any deadline-meeting run evaluated with the same energy model:
  * domains are solved independently: a job's per-domain work may use its whole
    [release, deadline] window on every domain (stage order across domains is relaxed);
  * a window includes every slot it overlaps (partial slots count as whole slots);
  * within a slot, work on one domain is perfectly packed (no scheduler or contention);
  * DVFS transition latency and energy, wake latency and wake energy are ignored;
  * leakage is evaluated at T_ref (default: ambient, the coolest the die can be);
  * regulator loss uses the regulator's best efficiency at every load;
  * jobs whose deadline falls after the horizon are dropped;
  * domains with activity but no job cycles (NoC, DRAM traffic) are charged their
    recorded switching energy scaled to their lowest allowed OPP voltage.
I always report HiGHS's dual bound (<= the optimum), so the number stays a valid lower
bound even when the solver stops at its gap tolerance or time limit.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_array

PS_PER_S = 1_000_000_000_000


@dataclass
class DomainBound:
    domain: str
    energy_j: float
    status: str
    n_jobs: int
    unmet_cycles: float = 0.0
    mip_gap: float | None = None
    opp_by_slot: list[int] = field(default_factory=list)
    solve_s: float = 0.0


def _window(rel: int, dl: int, slot_ps: int, n_slots: int) -> range:
    s0 = min(max(int(rel // slot_ps), 0), n_slots - 1)
    s1 = int(math.ceil(dl / slot_ps))
    return range(s0, min(max(s1, s0 + 1), n_slots))


def solve_domain(domain: str, jobs: list[tuple[int, int, float]], pm: Any, slot_ps: int, horizon_ps: int,
                 t_ref_c: float, gate_idle: bool, time_limit_s: float = 20.0, integral: bool = True,
                 mip_rel_gap: float = 1e-3) -> DomainBound:
    """Solve the per-domain MILP. `pm` is a PowerManager after a run (supplies models and activity profile).

    Aggregated form (same optimum as the per-job-per-OPP form, fewer variables):
      y[s,o] binary, z[s,o] >= 0 work at OPP o in slot s, x[j,s] >= 0 work of job j in slot s.
      sum_o y[s,o] = 1 ; z[s,o] <= cap_o y[s,o] ; sum_j x[j,s] = sum_o z[s,o] ; sum_s x[j,s] = C_j.
    The reported energy is HiGHS's dual bound (<= the true optimum) so it is a valid lower bound
    even when the solver stops at the gap tolerance or the time limit. integral=False solves the LP
    relaxation (time-sharing OPPs inside a slot), a looser but still valid bound.
    """
    dvf = pm.vf[domain]
    opps = dvf.opps[dvf.floor_index:]
    n_slots = int(math.ceil(horizon_ps / slot_ps))
    dt_s = slot_ps / PS_PER_S
    vn = dvf.nominal.volts
    e_nom = pm.cycle_energy_profile().get(domain, 0.0)
    if e_nom <= 0:
        e_nom = pm.energy.energy_per_busy_cycle_j(domain, vn)
    gate = gate_idle and domain in pm.gating.gateable
    eta = pm.regulators[domain].max_efficiency()
    f = np.array([o.freq_hz for o in opps])
    e_o = np.array([e_nom * (o.volts / vn) ** 2 for o in opps])
    p_leak = np.array([pm.leakage.power_w(domain, pm.energy.voltages_for(domain, o.volts)[1], t_ref_c) for o in opps])
    p_busy = np.array([pm.energy.clock_power_w(domain, o.volts, o.freq_hz) for o in opps]) + p_leak
    p_idle = p_leak * pm.gating.retention_leak_fraction if gate else p_busy.copy()
    min_idle_slot_j = float(np.min(p_idle)) * dt_s / eta

    jobs = [(r, d, c) for r, d, c in jobs if c > 0 and d <= horizon_ps]
    if not jobs:
        return DomainBound(domain, n_slots * min_idle_slot_j, "no_jobs", 0)

    windows = [_window(r, d, slot_ps, n_slots) for r, d, _ in jobs]
    used = sorted({s for w in windows for s in w})
    slot_pos = {s: i for i, s in enumerate(used)}
    n_o, n_s, nj = len(opps), len(used), len(jobs)
    xs_list = [(j, slot_pos[s]) for j, w in enumerate(windows) for s in w]
    nx = len(xs_list)
    ny = nz = n_s * n_o
    oy, oz, ox, ou = 0, ny, ny + nz, ny + nz + nx
    nvar = ou + nj
    a_o = (e_o + (p_busy - p_idle) / f) / eta
    cost = np.zeros(nvar)
    cost[oy:oy + ny] = np.tile(p_idle * dt_s / eta, n_s)
    cost[oz:oz + nz] = np.tile(a_o, n_s)
    big = float(np.max(a_o)) * 1e4
    cost[ou:] = big
    cap = f * dt_s

    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    lo: list[float] = []
    hi: list[float] = []

    def entry(row: int, col: int, val: float) -> None:
        rows.append(row)
        cols.append(col)
        vals.append(val)

    def bound_row(low: float, high: float) -> int:
        lo.append(low)
        hi.append(high)
        return len(lo) - 1

    for sp in range(n_s):                       # one OPP per slot
        r = bound_row(1.0, 1.0)
        for o in range(n_o):
            entry(r, oy + sp * n_o + o, 1.0)
    for sp in range(n_s):                       # capacity: z <= cap * y
        for o in range(n_o):
            k = sp * n_o + o
            r = bound_row(-np.inf, 0.0)
            entry(r, oz + k, 1.0)
            entry(r, oy + k, -cap[o])
    bal0 = len(lo)                              # slot balance: sum_j x - sum_o z = 0
    for sp in range(n_s):
        r = bound_row(0.0, 0.0)
        for o in range(n_o):
            entry(r, oz + sp * n_o + o, -1.0)
    dem0 = len(lo)                              # demand: sum_s x + u = C
    for j, (_, _, c) in enumerate(jobs):
        r = bound_row(c, c)
        entry(r, ou + j, 1.0)
    for k, (j, sp) in enumerate(xs_list):
        entry(bal0 + sp, ox + k, 1.0)
        entry(dem0 + j, ox + k, 1.0)
    r = len(lo)
    A = coo_array((vals, (rows, cols)), shape=(r, nvar)).tocsr()
    integrality = np.zeros(nvar)
    if integral:
        integrality[oy:oy + ny] = 1
    ub = np.full(nvar, np.inf)
    ub[oy:oy + ny] = 1.0
    t0 = time.time()
    res = milp(cost, constraints=LinearConstraint(A, np.array(lo), np.array(hi)), integrality=integrality,
               bounds=Bounds(np.zeros(nvar), ub),
               options={"time_limit": time_limit_s, "disp": False, "mip_rel_gap": mip_rel_gap})
    solve_s = time.time() - t0
    if res.x is None:
        return DomainBound(domain, float("nan"), f"solver_failed: {res.message}", nj, solve_s=solve_s)
    unmet = float(np.sum(res.x[ou:]))
    bound = getattr(res, "mip_dual_bound", None) if integral else None
    obj = float(bound) if bound is not None and math.isfinite(bound) else float(res.fun)
    obj -= big * unmet
    status = ("optimal_within_gap" if res.status == 0 else "time_limit_dual_bound") if integral else "lp_relaxation"
    if unmet > 1e-6:
        status += "_with_unmet_cycles"
    y = res.x[oy:oy + ny].reshape(n_s, n_o)
    chosen = {s: int(np.argmax(y[slot_pos[s]])) + dvf.floor_index for s in used}
    min_idle_idx = int(np.argmin(p_idle)) + dvf.floor_index
    opp_by_slot = [chosen.get(s, min_idle_idx) for s in range(n_slots)]
    energy = obj + (n_slots - n_s) * min_idle_slot_j
    return DomainBound(domain, energy, status, nj, unmet_cycles=unmet, mip_gap=getattr(res, "mip_gap", None),
                       opp_by_slot=opp_by_slot, solve_s=solve_s)


def solve_trace(trace: Iterable[Mapping[str, Any]], pm: Any, horizon_ps: int | None = None,
                slot_ms: float = 1.0, t_ref_c: float | None = None, gate_idle: bool = True,
                time_limit_s: float = 20.0, integral: bool = True, mip_rel_gap: float = 1e-3) -> dict[str, Any]:
    """Lower-bound energy for a job trace under the manager's energy model.

    trace: dicts with release_ps, deadline_ps, cycles_by_domain (PowerManager.job_trace).
    pm: the PowerManager whose models (and run activity profile) define the energy model.
    """
    trace = list(trace)
    horizon_ps = int(horizon_ps if horizon_ps is not None else pm.elapsed_ps)
    slot_ps = int(round(slot_ms * 1e9))
    t_ref = pm.thermal.ambient_c if t_ref_c is None else float(t_ref_c)
    per_domain: dict[str, DomainBound] = {}
    for d in pm.domains:
        dvf = pm.vf[d]
        jobs = [(int(j["release_ps"]), int(j["deadline_ps"]), float(j["cycles_by_domain"].get(d, 0.0)))
                for j in trace if j.get("cycles_by_domain")]
        has_cycles = any(c > 0 for _, _, c in jobs)
        if dvf.fixed or not has_cycles:
            low = dvf.opps[dvf.floor_index] if not dvf.fixed else dvf.opps[0]
            vn = dvf.nominal.volts
            dyn = pm.dyn_energy_nominal_j.get(d, 0.0) * ((low.volts / vn) ** 2 if not dvf.fixed else 1.0)
            gate = gate_idle and d in pm.gating.gateable
            v_rest = pm.energy.voltages_for(d, low.volts)[1]
            leak = pm.leakage.power_w(d, v_rest, t_ref)
            p_idle = leak * pm.gating.retention_leak_fraction if gate else leak + pm.energy.clock_power_w(d, low.volts, low.freq_hz)
            eta = pm.regulators[d].max_efficiency()
            e = (dyn + p_idle * horizon_ps / PS_PER_S) / eta
            per_domain[d] = DomainBound(d, e, "analytic_no_jobs", 0)
            continue
        per_domain[d] = solve_domain(d, jobs, pm, slot_ps, horizon_ps, t_ref, gate_idle, time_limit_s, integral,
                                     mip_rel_gap)
    total = float(sum(b.energy_j for b in per_domain.values()))
    dur_s = horizon_ps / PS_PER_S
    return {
        "energy_j": total,
        "avg_power_w": total / dur_s if dur_s > 0 else 0.0,
        "energy_per_robot_hour_wh": total / dur_s if dur_s > 0 else 0.0,
        "duration_s": dur_s,
        "slot_ms": slot_ms,
        "t_ref_c": t_ref,
        "gate_idle": gate_idle,
        "solver": "HiGHS MILP via scipy.optimize.milp",
        "is_lower_bound": True,
        "per_domain": {d: {"energy_j": b.energy_j, "status": b.status, "n_jobs": b.n_jobs,
                           "unmet_cycles": b.unmet_cycles, "mip_gap": b.mip_gap, "solve_s": b.solve_s}
                       for d, b in per_domain.items()},
        "opp_by_slot": {d: b.opp_by_slot for d, b in per_domain.items() if b.opp_by_slot},
    }
