"""Online execution-demand statistics used by the deadline-aware policies (P4, P6).

What it tracks, per task and per power domain:
  * the core's cycle estimate for each job when the job is first seen unstarted
    (from sim.pending_jobs()["est_remaining_cycles"]),
  * the actual cycles each job used on each domain, when completions report it
    (requested from the core as `cycles_by_domain`; the FakeSim provides it),
  * the ratio actual / estimate. A high quantile of this ratio is the inflation
    factor P4 applies to `est_remaining_cycles`. Before enough samples exist, a
    prior inflation (assumption, default 1.25) is used.
It also infers each task's period (median release spacing), relative deadline and
which domains it uses, so the policy can reserve capacity for jobs released before
its next decision.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable, Mapping

import numpy as np

SAFETY_LABELS = {"safety", "safety_critical", "safety-critical"}


class ExecTimePredictor:
    """Per-task demand and estimate-error statistics."""

    def __init__(self, quantile: float = 0.95, safety_quantile: float = 0.999,
                 prior_inflation: float = 1.25, min_samples: int = 5, window: int = 200,
                 task_info: Mapping[str, Mapping[str, Any]] | None = None):
        self.q = float(quantile)
        self.q_safety = float(safety_quantile)
        self.prior = float(prior_inflation)
        self.min_samples = int(min_samples)
        self.window = int(window)
        self.task_info: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (task_info or {}).items()}
        self._first: dict[int, dict[str, Any]] = {}
        self._ratio: dict[tuple[str, str], deque[float]] = defaultdict(lambda: deque(maxlen=self.window))
        self._actual: dict[tuple[str, str], deque[float]] = defaultdict(lambda: deque(maxlen=self.window))
        self._estimate: dict[tuple[str, str], deque[float]] = defaultdict(lambda: deque(maxlen=self.window))
        self._releases: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=64))
        self._rel_deadline: dict[str, int] = {}
        self._domains: dict[str, set[str]] = defaultdict(set)
        self._seen_release: dict[str, set[int]] = defaultdict(set)

    # ---------------------------------------------------------------- observe
    def _note_release(self, task: str, release_ps: int, deadline_ps: int | None) -> None:
        if release_ps not in self._seen_release[task]:
            self._seen_release[task].add(release_ps)
            self._releases[task].append(int(release_ps))
            if len(self._seen_release[task]) > 256:
                self._seen_release[task] = set(self._releases[task])
        if deadline_ps is not None:
            self._rel_deadline[task] = int(deadline_ps) - int(release_ps)

    def observe_pending(self, jobs: Iterable[Mapping[str, Any]]) -> None:
        for j in jobs:
            jid = int(j["job_id"])
            task = str(j["task_id"])
            self._note_release(task, int(j["release_ps"]), j.get("deadline_ps"))
            est = {d: float(c) for d, c in (j.get("est_remaining_cycles") or {}).items() if c and c > 0}
            self._domains[task].update(est)
            if jid not in self._first:
                unstarted = (not j.get("started", False)) and float(j.get("frac_done", 0.0)) == 0.0
                self._first[jid] = {"task": task, "est": est if unstarted else None}
                if unstarted:
                    for d, c in est.items():
                        self._estimate[(task, d)].append(c)

    def observe_completion(self, c: Mapping[str, Any]) -> None:
        task = str(c["task_id"])
        self._note_release(task, int(c["release_ps"]), c.get("deadline_ps"))
        first = self._first.pop(int(c["job_id"]), None)
        actual = c.get("cycles_by_domain")
        if not actual:
            return
        for d, a in actual.items():
            a = float(a)
            if a <= 0:
                continue
            self._domains[task].add(d)
            self._actual[(task, d)].append(a)
            if first and first.get("est") and first["est"].get(d, 0) > 0:
                self._ratio[(task, d)].append(a / first["est"][d])

    # ------------------------------------------------------------------ query
    def is_safety(self, task: str) -> bool:
        crit = str(self.task_info.get(task, {}).get("criticality", "")).lower()
        return crit in SAFETY_LABELS

    def tasks(self) -> list[str]:
        return sorted(set(self._domains) | set(self.task_info))

    def domains_of(self, task: str) -> set[str]:
        return set(self._domains.get(task, set())) | set(self.task_info.get(task, {}).get("domains", []))

    def period_ps(self, task: str) -> int | None:
        info = self.task_info.get(task, {})
        if info.get("period_us"):
            return int(round(float(info["period_us"]) * 1e6))
        rel = sorted(self._releases.get(task, []))
        if len(rel) < 3:
            return None
        diffs = np.diff(rel)
        diffs = diffs[diffs > 0]
        return int(np.median(diffs)) if len(diffs) else None

    def rel_deadline_ps(self, task: str) -> int | None:
        info = self.task_info.get(task, {})
        if info.get("deadline_us"):
            return int(round(float(info["deadline_us"]) * 1e6))
        return self._rel_deadline.get(task)

    def last_release_ps(self, task: str) -> int | None:
        rel = self._releases.get(task)
        return max(rel) if rel else None

    def inflation(self, task: str, domain: str, safety: bool | None = None) -> float:
        """High quantile of actual/estimate cycles; prior until min_samples exist."""
        safety = self.is_safety(task) if safety is None else safety
        s = self._ratio.get((task, domain))
        if not s or len(s) < self.min_samples:
            return self.prior
        q = self.q_safety if safety else self.q
        val = float(np.quantile(np.fromiter(s, float), q))
        return max(val, 1.0) if safety else val

    def demand_cycles(self, task: str, domain: str, safety: bool | None = None) -> float | None:
        """High-quantile total cycles a job of `task` needs on `domain` (None if unknown)."""
        safety = self.is_safety(task) if safety is None else safety
        q = self.q_safety if safety else self.q
        a = self._actual.get((task, domain))
        if a and len(a) >= self.min_samples:
            return float(np.quantile(np.fromiter(a, float), q))
        e = self._estimate.get((task, domain))
        if e:
            return float(np.max(np.fromiter(e, float))) * self.inflation(task, domain, safety)
        cyc = self.task_info.get(task, {}).get("cycles", {}).get(domain)
        return float(cyc) * self.prior if cyc else None


def task_info_from_graphs(graphs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pull period, deadline and criticality out of task-graph dicts (workloads IR)."""
    out: dict[str, dict[str, Any]] = {}
    for g in graphs:
        rel = g.get("release", {}) or {}
        out[g["task_id"]] = {
            "period_us": rel.get("period_us") if rel.get("kind", "periodic") == "periodic" else None,
            "deadline_us": g.get("deadline_us"),
            "criticality": g.get("criticality", ""),
        }
    return out
