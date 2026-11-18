"""P4 pace-to-deadline and P6 surrogate-guided.

P4, per DVFS domain d, every manager step:
  1. For each pending job with remaining work on d, predict remaining cycles as
        R = est_remaining_cycles[d] * inflation(task, d)
     where inflation is a high quantile (default q=0.95; 0.999 for safety tasks) of
     the observed actual/estimate cycle ratio (prior 1.25 until enough samples).
  2. (Margins: a fixed one plus a fraction of the relative deadline, raised per task by slack
     feedback when the task's recent 5th-percentile slack drops below its base margin.)
     Subtract the task's learned frequency-independent time M (memory stalls, middleware;
     high quantile, scaled by the job's remaining fraction) from its remaining window
     (deadline - now - margin), then split what is left across the job's domains in
     proportion to their remaining time at nominal frequency (a standard
     deadline-partitioning heuristic) to get an effective deadline on d.
     Warm-up: until a task has `min_samples` completions, the domains its pending jobs use
     stay at or above nominal.
  3. Add anticipated jobs (EDF processor-demand view): for each task with a known period,
     every future release before the latest pending deadline on that domain is added as a
     job with the task's high-quantile cycle demand and its own effective deadline.
  4. Required frequency (EDF demand bound on a single server):
        f_req = max_k  sum_{i: dl_i <= dl_k} R_i / (dl_k - now)
     times (1 + margin_frac). With `use_parallel_units`, the sum is divided by the
     domain's unit count, but no single job may need more than its own R_k / window.
  5. Safety floor: a domain that hosts a safety-critical task never drops below
        demand_q(safety) / (share * rel_deadline - margin)
     so a safety job released at any time can still finish by its deadline.
  6. Pick the lowest OPP with f >= f_req (max OPP if none is fast enough).
  Infrastructure domains (default: noc_cache) stay at nominal: their clock sets every task's
  memory time, which the per-task model only learns at the current NoC clock (lowering it
  with a utilization governor made T4 miss every deadline on the real core). Set
  `govern_infrastructure: true` to govern them anyway. Compute domains that no task maps work
  to (for example an idle CPU cluster) use a utilization governor: f_req = util * f_now /
  target_util (target 0.7, assumption).

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
        # Margin as a fraction of each job's relative deadline (reserved slack the pacing never uses).
        self.dl_margin_frac = float(p.get("deadline_margin_frac", 0.08))
        self.safety_dl_margin_frac = float(p.get("safety_deadline_margin_frac", 0.10))
        # Slack feedback: if the 5th percentile of a task's recent slack (fraction of its relative
        # deadline) falls below its base margin, the deficit is added to that task's margin. Corrects
        # model error (e.g. tile sharing late in a job) without retuning. Capped at max_margin_frac.
        self.slack_feedback = bool(p.get("slack_feedback", True))
        self.max_margin_frac = float(p.get("max_margin_frac", 0.30))
        self.anticipate = bool(p.get("anticipate", True))
        self.max_phantoms = int(p.get("max_phantoms_per_task", 400))
        # Optional: also power-gate idle gateable domains (pace + gate). Off for P4 by default.
        self.gating = bool(p.get("gate_idle", False))
        self.use_parallel_units = bool(p.get("use_parallel_units", False))
        self.target_util = float(p.get("unmapped_target_util", 0.7))
        self.govern_unmapped = bool(p.get("govern_unmapped", True))
        # Shared memory-system domains: job cycles there do not capture how much every task's
        # latency depends on their clock, so they never use the per-job demand bound.
        self.infrastructure = set(p.get("infrastructure_domains", ["noc_cache"]))
        self.govern_infrastructure = bool(p.get("govern_infrastructure", False))
        self.min_samples = int(p.get("min_samples", 5))
        self.warm_domains: set[str] = set()
        self.last_requirements: dict[str, float] = {}

    # ----------------------------------------------------------------- helpers
    def _margin_frac(self, pred, task: str, safety: bool) -> float:
        base = self.safety_dl_margin_frac if safety else self.dl_margin_frac
        if not self.slack_feedback:
            return base
        lo = pred.slack_low(task)
        if lo is None:
            return base
        return min(base + max(0.0, base - lo), self.max_margin_frac)

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
        self.warm_domains = set()

        for job in ctx.pending:
            rem = {d: float(c) for d, c in (job.get("est_remaining_cycles") or {}).items()
                   if c and c > 0 and d in ctx.vf}
            if not rem:
                continue
            task = str(job["task_id"])
            safety = pred.is_safety(task)
            if pred.n_completed(task) < self.min_samples:
                self.warm_domains.update(d for d in rem if d in dvfs)
            left = 1.0 - min(max(float(job.get("frac_done", 0.0) or 0.0), 0.0), 1.0)
            if pred.has_time_model(task):
                # critical-path compute cycles still to run on each domain (rescales with 1/f)
                demand = {d: pred.cpc_cycles(task, d, safety) * pred.remaining_fraction(job, d) for d in rem}
            else:
                demand = {d: c * pred.inflation(task, d, safety) for d, c in rem.items()}
            demand = {d: c for d, c in demand.items() if c > 0}
            if not demand:
                continue
            m_ps = pred.fixed_time_ps(task, safety) * left
            rel = int(job["deadline_ps"]) - int(job["release_ps"])
            frac_m = self._margin_frac(pred, task, safety) * rel
            window = int(job["deadline_ps"]) - now - self._margin(safety, trans) - m_ps - frac_m
            shares = self._shares(ctx, demand)
            for d, r in demand.items():
                if d not in dvfs:
                    continue
                items[d].append((now + int(shares[d] * window), r))

        for task in pred.tasks():
            safety = pred.is_safety(task)
            doms = [d for d in pred.domains_of(task) if d in ctx.vf]
            if not doms:
                continue
            if pred.has_time_model(task):
                demand = {d: pred.cpc_cycles(task, d, safety) for d in doms}
            else:
                demand = {d: pred.demand_cycles(task, d, safety) for d in doms}
            demand = {d: c for d, c in demand.items() if c}
            if not demand:
                continue
            shares = self._shares(ctx, demand)
            rel_dl = pred.rel_deadline_ps(task)
            margin = self._margin(safety, trans) + pred.fixed_time_ps(task, safety)
            if rel_dl:
                margin += self._margin_frac(pred, task, safety) * rel_dl
            if safety and rel_dl:
                for d, c in demand.items():
                    if d in dvfs:
                        w = shares[d] * (rel_dl - margin) * 1e-12
                        floors[d] = max(floors[d], c / w if w > 0 else math.inf)
            period = pred.period_ps(task)
            last = pred.last_release_ps(task)
            if (self.anticipate or safety) and period and rel_dl and last is not None:
                # EDF processor-demand view: every future release whose deadline can fall before a
                # pending deadline on the same domain adds demand (not just the next step's releases).
                for d, c in demand.items():
                    if d not in dvfs:
                        continue
                    horizon = max([dl for dl, _ in items.get(d, [])] + [now + ctx.step_ps])
                    r = last + period
                    n = 0
                    while r < horizon and n < self.max_phantoms:
                        if r >= now:
                            dl = r + int(shares[d] * (rel_dl - margin))
                            items[d].append((dl, c))
                            w = (dl - r) * 1e-12
                            floors[d] = max(floors[d], c / w if w > 0 else math.inf)
                        r += period
                        n += 1
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
            if d in self.infrastructure:
                out[d] = self._governor(ctx, d) if self.govern_infrastructure else self._nominal(ctx, d)
                continue
            if d not in mapped and not items.get(d):
                out[d] = self._governor(ctx, d)
                continue
            f_req = self.required_freq(ctx, items.get(d, []), floors.get(d, 0.0), d)
            self.last_requirements[d] = f_req
            out[d] = ctx.vf[d].lowest_index_for_freq(f_req) if math.isfinite(f_req) else ctx.vf[d].max_index
            if d in self.warm_domains:
                out[d] = max(out[d], ctx.vf[d].nominal_index)
        return out

    def _nominal(self, ctx: PolicyContext, d: str) -> int:
        dvf = ctx.vf[d]
        return max(dvf.nominal_index, dvf.floor_index)

    def _governor(self, ctx: PolicyContext, d: str) -> int:
        dvf = ctx.vf[d]
        if not self.govern_unmapped or d not in ctx.utilization:
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
        self.surrogate_kind = "callable" if surrogate is not None else "none (P4 behavior)"

    def _placeholder(self, ctx: PolicyContext) -> SurrogateFn:
        """Conservative stand-in for the conformal surrogate (not a learned model).

        Upper bound on finish = now + k * (remaining M + sum_d remaining critical-path cycles / f_d),
        with the candidate frequency on the domain being decided and the current OPP elsewhere,
        and k = placeholder_factor (default 1.5) to cover queueing behind other jobs.
        """
        pred = ctx.predictor
        k = float(self.params.get("placeholder_factor", 1.5))

        def ub(job: Mapping[str, Any], domain: str, f_hz: float, now_ps: int) -> float:
            task = str(job["task_id"])
            safety = pred.is_safety(task)
            left = 1.0 - min(max(float(job.get("frac_done", 0.0) or 0.0), 0.0), 1.0)
            rem = {d: c for d, c in (job.get("est_remaining_cycles") or {}).items() if c and c > 0 and d in ctx.vf}
            t = pred.fixed_time_ps(task, safety) * left
            for d, c in rem.items():
                cyc = pred.cpc_cycles(task, d, safety) * pred.remaining_fraction(job, d) \
                    if pred.has_time_model(task) else c * pred.inflation(task, d, safety)
                f = f_hz if d == domain else ctx.vf[d].opps[ctx.current.get(d, ctx.vf[d].nominal_index)].freq_hz
                t += cyc / f * 1e12
            return now_ps + k * t

        return ub

    def _build_surrogate(self) -> None:
        """Build the surrogate from params["surrogate_factory"] = {"callable": "pkg.mod:Name", "kwargs": {...}}.

        Lets a config (or the shared runner's policy overrides) select the conformal surrogate without
        code changes. The surrogate itself is built by the surrogate worker (surrogate/p6.py).
        """
        spec = self.params.get("surrogate_factory")
        if self.surrogate is not None or not spec:
            return
        import importlib
        mod, name = str(spec["callable"]).split(":")
        self.surrogate = getattr(importlib.import_module(mod), name)(**dict(spec.get("kwargs") or {}))

    def surrogate_stats(self) -> dict[str, Any] | None:
        st = getattr(self.surrogate, "stats", None)
        return st() if callable(st) else None

    def decide(self, ctx: PolicyContext) -> dict[str, int]:
        self._build_surrogate()
        if self.surrogate is None and self.params.get("placeholder_surrogate"):
            self.surrogate_kind = "placeholder"
            return self._decide_with(ctx, self._placeholder(ctx))
        if self.surrogate is None:
            self.surrogate_kind = "none (P4 behavior)"
            return super().decide(ctx)
        self.surrogate_kind = "callable"
        return self._decide_with(ctx, self.surrogate)

    def _decide_with(self, ctx: PolicyContext, surrogate: SurrogateFn) -> dict[str, int]:
        _, floors = self._items_and_floors(ctx)
        trans = max((v.transition_ps for v in ctx.vf.values() if not v.fixed), default=0)
        out: dict[str, int] = {}
        mapped = {d for t in ctx.predictor.tasks() for d in ctx.predictor.domains_of(t)}
        for d in ctx.dvfs_domains():
            dvf = ctx.vf[d]
            jobs = [j for j in ctx.pending if (j.get("est_remaining_cycles") or {}).get(d, 0) > 0]
            if d in self.infrastructure:
                out[d] = self._governor(ctx, d) if self.govern_infrastructure else self._nominal(ctx, d)
                continue
            if d not in mapped and not jobs:
                out[d] = self._governor(ctx, d)
                continue
            start = dvf.lowest_index_for_freq(floors.get(d, 0.0) * (1 + self.margin_frac))
            choice = dvf.max_index
            for o in dvf.opps[start:]:
                ok = True
                for j in jobs:
                    safety = ctx.predictor.is_safety(str(j["task_id"]))
                    rel = int(j["deadline_ps"]) - int(j["release_ps"])
                    frac_m = self._margin_frac(ctx.predictor, str(j["task_id"]), safety) * rel
                    limit = int(j["deadline_ps"]) - self._margin(safety, trans) - frac_m
                    if surrogate(j, d, o.freq_hz, ctx.now_ps) > limit:
                        ok = False
                        break
                if ok:
                    choice = o.index
                    break
            if d in self.warm_domains:
                choice = max(choice, dvf.nominal_index)
            out[d] = choice
        return out
