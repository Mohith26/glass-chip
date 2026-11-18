"""P4 pace-to-deadline and P6 surrogate-guided.

P4, per DVFS domain d, every manager step:
  1. For each pending job with remaining work on d, predict remaining cycles as
        R = est_remaining_cycles[d] * inflation(task, d)
     where inflation is a high quantile (default q=0.95; 0.999 for safety tasks) of
     the observed actual/estimate cycle ratio (prior 1.25 until enough samples).
  2. Give the job an effective deadline on d by splitting its remaining window
     (deadline - now - margin) across its domains in proportion to their remaining
     time at nominal frequency (a standard deadline-partitioning heuristic).
  3. Add anticipated jobs: for each task with a known period whose next release falls
     before the next decision, add one job with its high-quantile demand.
  4. Required frequency (EDF demand bound on a single server):
        f_req = max_k  sum_{i: dl_i <= dl_k} R_i / (dl_k - now)
     times (1 + margin_frac). With `use_parallel_units`, the sum is divided by the
     domain's unit count, but no single job may need more than its own R_k / window.
  5. Safety floor: a domain that hosts a safety-critical task never drops below
        demand_q(safety) / (share * rel_deadline - margin)
     so a safety job released at any time can still finish by its deadline.
  6. Pick the lowest OPP with f >= f_req (max OPP if none is fast enough).
  Domains that no task maps work to (for example noc_cache, which carries traffic but
  has no job cycles) use a utilization governor instead: f_req = util * f_now / target_util
  (target 0.7, assumption). Without a utilization reading they stay at nominal.

P6 is the same, except that when a surrogate callable is supplied it replaces step 4:
the lowest OPP is chosen such that surrogate(job, domain, freq_hz, now_ps), an upper
bound on the job's finish time in ps, is <= deadline - margin for every pending job on
that domain. The safety floor from step 5 still applies. Without a surrogate, P6 is P4.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Mapping

from .base import PolicyContext, PowerPolicy

SurrogateFn = Callable[[Mapping[str, Any], str, float, int], float]


class PaceToDeadlinePolicy(PowerPolicy):
    """P4: lowest OPP that is predicted to meet every pending deadline with margin."""

    name = "pace_to_deadline"
    static = False

    def __init__(self, params: Mapping[str, Any] | None = None):
        super().__init__(params)
        p = self.params
        self.margin_frac = float(p.get("margin_frac", 0.10))
        self.margin_ps = int(round(float(p.get("margin_us", 50.0)) * 1e6))
        self.safety_margin_ps = int(round(float(p.get("safety_margin_us", 100.0)) * 1e6))
        self.anticipate = bool(p.get("anticipate", True))
        self.use_parallel_units = bool(p.get("use_parallel_units", False))
        self.target_util = float(p.get("unmapped_target_util", 0.7))
        self.last_requirements: dict[str, float] = {}

    # ----------------------------------------------------------------- helpers
    def _margin(self, safety: bool, transition_ps: int) -> int:
        return (self.safety_margin_ps if safety else self.margin_ps) + transition_ps

    def _shares(self, ctx: PolicyContext, rem: Mapping[str, float]) -> dict[str, float]:
        t_nom = {d: c / ctx.vf[d].nominal.freq_hz for d, c in rem.items()}
        tot = sum(t_nom.values())
        return {d: (t / tot if tot > 0 else 1.0) for d, t in t_nom.items()}

    def _items_and_floors(self, ctx: PolicyContext) -> tuple[dict[str, list[tuple[int, float]]], dict[str, float]]:
        pred = ctx.predictor
        now = ctx.now_ps
        items: dict[str, list[tuple[int, float]]] = defaultdict(list)
        floors: dict[str, float] = defaultdict(float)
        dvfs = set(ctx.dvfs_domains())
        trans = max((ctx.vf[d].transition_ps for d in dvfs), default=0)

        for job in ctx.pending:
            rem = {d: float(c) for d, c in (job.get("est_remaining_cycles") or {}).items()
                   if c and c > 0 and d in ctx.vf}
            if not rem:
                continue
            task = str(job["task_id"])
            safety = pred.is_safety(task)
            window = int(job["deadline_ps"]) - now - self._margin(safety, trans)
            shares = self._shares(ctx, rem)
            for d, c in rem.items():
                if d not in dvfs:
                    continue
                r = c * pred.inflation(task, d, safety)
                items[d].append((now + int(shares[d] * window), r))

        for task in pred.tasks():
            safety = pred.is_safety(task)
            doms = [d for d in pred.domains_of(task) if d in ctx.vf]
            if not doms:
                continue
            demand = {d: pred.demand_cycles(task, d, safety) for d in doms}
            demand = {d: c for d, c in demand.items() if c}
            if not demand:
                continue
            shares = self._shares(ctx, demand)
            rel_dl = pred.rel_deadline_ps(task)
            margin = self._margin(safety, trans)
            if safety and rel_dl:
                for d, c in demand.items():
                    if d in dvfs:
                        w = shares[d] * (rel_dl - margin) * 1e-12
                        floors[d] = max(floors[d], c / w if w > 0 else math.inf)
            period = pred.period_ps(task)
            last = pred.last_release_ps(task)
            if (self.anticipate or safety) and period and rel_dl and last is not None:
                r = last + period
                while r < now + ctx.step_ps:
                    if r >= now:
                        for d, c in demand.items():
                            if d not in dvfs:
                                continue
                            dl = r + int(shares[d] * (rel_dl - margin))
                            items[d].append((dl, c))
                            w = (dl - r) * 1e-12
                            floors[d] = max(floors[d], c / w if w > 0 else math.inf)
                    r += period
        return items, floors

    def required_freq(self, ctx: PolicyContext, items: list[tuple[int, float]], floor: float, d: str) -> float:
        f_req = floor
        units = max(int(ctx.parallel_units.get(d, 1)), 1) if self.use_parallel_units else 1
        cum = 0.0
        for dl, c in sorted(items):
            cum += c
            w = (dl - ctx.now_ps) * 1e-12
            if w <= 0:
                return math.inf
            f_req = max(f_req, cum / (units * w), c / w)
        return f_req * (1.0 + self.margin_frac)

    # ------------------------------------------------------------------ decide
    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        items, floors = self._items_and_floors(ctx)
        out: dict[str, int] = {}
        self.last_requirements = {}
        mapped = {d for t in ctx.predictor.tasks() for d in ctx.predictor.domains_of(t)}
        for d in ctx.dvfs_domains():
            if d not in mapped and not items.get(d):
                out[d] = self._governor(ctx, d)
                continue
            f_req = self.required_freq(ctx, items.get(d, []), floors.get(d, 0.0), d)
            self.last_requirements[d] = f_req
            out[d] = ctx.vf[d].lowest_index_for_freq(f_req) if math.isfinite(f_req) else ctx.vf[d].max_index
        return out

    def _governor(self, ctx: PolicyContext, d: str) -> int:
        dvf = ctx.vf[d]
        if d not in ctx.utilization:
            return max(dvf.nominal_index, dvf.floor_index)
        f_now = dvf.opps[ctx.current.get(d, dvf.nominal_index)].freq_hz
        f_req = ctx.utilization[d] * f_now / self.target_util
        self.last_requirements[d] = f_req
        return dvf.lowest_index_for_freq(f_req)


class SurrogateGuidedPolicy(PaceToDeadlinePolicy):
    """P6: P4 with a surrogate upper bound on finish time. Falls back to P4 without one."""

    name = "surrogate_guided"

    def __init__(self, params: Mapping[str, Any] | None = None, surrogate: SurrogateFn | None = None):
        super().__init__(params)
        self.surrogate = surrogate

    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        if self.surrogate is None:
            return super().decide(ctx)
        _, floors = self._items_and_floors(ctx)
        trans = max((v.transition_ps for v in ctx.vf.values() if not v.fixed), default=0)
        out: dict[str, int] = {}
        mapped = {d for t in ctx.predictor.tasks() for d in ctx.predictor.domains_of(t)}
        for d in ctx.dvfs_domains():
            dvf = ctx.vf[d]
            jobs = [j for j in ctx.pending if (j.get("est_remaining_cycles") or {}).get(d, 0) > 0]
            if d not in mapped and not jobs:
                out[d] = self._governor(ctx, d)
                continue
            start = dvf.lowest_index_for_freq(floors.get(d, 0.0) * (1 + self.margin_frac))
            choice = dvf.max_index
            for o in dvf.opps[start:]:
                ok = True
                for j in jobs:
                    safety = ctx.predictor.is_safety(str(j["task_id"]))
                    limit = int(j["deadline_ps"]) - self._margin(safety, trans)
                    if self.surrogate(j, d, o.freq_hz, ctx.now_ps) > limit:
                        ok = False
                        break
                if ok:
                    choice = o.index
                    break
            out[d] = choice
        return out
