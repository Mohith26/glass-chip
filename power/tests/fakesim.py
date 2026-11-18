"""FakeSim: a small event-driven stand-in for glasschip_core used by the power tests and demo.

It implements the subset of the core API the power manager uses (CONTRACTS.md):
release_job, run_until, pending_jobs, set_domain_freq, domain_freq, counters,
set_scheduler, now_ps; plus the API I requested from the core (notes/requests-power.md):
set_power_gating(domain, enabled, wake_latency_ps), counters with gated_ps/wakeups, and
completions with cycles_by_domain.

Model (synthetic, not VX-1 timing): every power domain is one preemptive server. A job is a
chain of stages; each stage runs on one domain for a number of cycles drawn at release
(mean * uniform(1 - jitter, 1 + jitter), seeded). Time per stage = cycles / f. A DVFS change
stalls the domain for the transition latency. A gated domain wakes after wake latency when
work arrives. Activity counters are cycles x the domain's peak activity per cycle x a
per-task utilization.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

PS_PER_S = 1_000_000_000_000


@dataclass
class FakeTask:
    task_id: str
    period_ps: int
    deadline_ps: int
    stages: list[tuple[str, float]]          # (domain, mean cycles)
    jitter: float = 0.1
    priority: int = 0                        # lower = higher priority (fixed_priority)
    criticality: str = "high"
    utilization: float = 0.6                 # activity per busy cycle, fraction of peak
    offset_ps: int = 0

    def info(self) -> dict[str, Any]:
        return {"period_us": self.period_ps / 1e6, "deadline_us": self.deadline_ps / 1e6,
                "criticality": self.criticality}


@dataclass
class _Job:
    job_id: int
    task: FakeTask
    tag: int
    release_ps: int
    deadline_ps: int
    cycles: list[float]
    stage: int = 0
    done_in_stage: float = 0.0
    start_ps: int | None = None
    ready_ps: int = 0
    cycles_by_domain: dict[str, float] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.task.stages[self.stage][0]

    @property
    def remaining_in_stage(self) -> float:
        return self.cycles[self.stage] - self.done_in_stage


class FakeSim:
    """Deterministic synthetic chip model with the core's power-relevant API."""

    def __init__(self, tasks: list[FakeTask], nominal_freq_hz: Mapping[str, float],
                 peak_activity: Mapping[str, Mapping[str, float]], transition_ps: int = 20_000_000, seed: int = 0):
        self.tasks = {t.task_id: t for t in tasks}
        self.freq = dict(nominal_freq_hz)
        self.peak_activity = {d: dict(v) for d, v in peak_activity.items()}
        self.transition_ps = int(transition_ps)
        self.rng = np.random.default_rng(seed)
        self.now_ps = 0
        self._next_id = 1
        self._releases: list[tuple[int, int, _Job]] = []
        self._ready: dict[str, list[_Job]] = {d: [] for d in self.freq}
        self._stall_until: dict[str, int] = {d: 0 for d in self.freq}
        self._target_freq: dict[str, float] = dict(self.freq)
        self._gating: dict[str, int | None] = {d: None for d in self.freq}
        self._gated: dict[str, bool] = {d: False for d in self.freq}
        self._sched: dict[str, str] = {d: "fifo" for d in self.freq}
        self._jobs: dict[int, _Job] = {}
        self._completed: list[dict[str, Any]] = []
        self._ctr: dict[str, dict[str, float]] = {}
        self._reset_counters()
        self.freq_changes = 0

    # ------------------------------------------------------------------ API
    def set_scheduler(self, engine: str, policy: str) -> None:
        targets = {"cpu": ["cpu_a", "cpu_b"], "dma": ["noc_cache"]}.get(engine, [engine])
        for d in targets:
            if d in self._sched:
                self._sched[d] = policy

    def set_domain_freq(self, domain: str, freq_hz: float) -> None:
        if domain not in self.freq or freq_hz == self._target_freq[domain]:
            return
        self._target_freq[domain] = float(freq_hz)
        self._stall_until[domain] = max(self._stall_until[domain], self.now_ps) + self.transition_ps
        self.freq_changes += 1

    def domain_freq(self, domain: str) -> float:
        return self.freq[domain]

    def set_power_gating(self, domain: str, enabled: bool, wake_latency_ps: int, idle_before_gate_ps: int = 0) -> None:
        # FakeSim gates at once (no idle timer); the argument is accepted for API parity.
        self._gating[domain] = int(wake_latency_ps) if enabled else None
        self._gated[domain] = bool(enabled) and not self._ready[domain]

    def release_job(self, task_id: str, t_ps: int, tag: int = 0) -> int:
        t = self.tasks[task_id]
        cyc = [m * float(self.rng.uniform(1 - t.jitter, 1 + t.jitter)) for _, m in t.stages]
        jid = self._next_id
        self._next_id += 1
        job = _Job(jid, t, tag, int(t_ps), int(t_ps) + t.deadline_ps, cyc, ready_ps=int(t_ps))
        heapq.heappush(self._releases, (int(t_ps), jid, job))
        return jid

    def pending_jobs(self) -> list[dict[str, Any]]:
        out = []
        for j in sorted(self._jobs.values(), key=lambda j: j.job_id):
            est: dict[str, float] = {}
            for i, (d, mean) in enumerate(j.task.stages):
                if i < j.stage:
                    continue
                frac_left = (j.remaining_in_stage / j.cycles[i]) if i == j.stage else 1.0
                est[d] = est.get(d, 0.0) + mean * frac_left
            total = sum(j.cycles)
            done = sum(j.cycles[:j.stage]) + j.done_in_stage
            out.append({"job_id": j.job_id, "task_id": j.task.task_id, "release_ps": j.release_ps,
                        "deadline_ps": j.deadline_ps, "started": j.start_ps is not None,
                        "frac_done": done / total if total else 1.0, "est_remaining_cycles": est})
        return out

    def counters(self, reset: bool = True) -> dict[str, dict[str, float]]:
        out = {d: dict(v) for d, v in self._ctr.items()}
        if reset:
            self._reset_counters()
        return out

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        t_ps = int(t_ps)
        while True:
            self._admit_releases()
            self._finish_stages()
            if self.now_ps >= t_ps:
                break
            nxt = t_ps
            if self._releases:
                nxt = min(nxt, self._releases[0][0])
            for d in self.freq:
                if self._stall_until[d] > self.now_ps:
                    nxt = min(nxt, self._stall_until[d])
                    continue
                job = self._pick(d)
                if job is not None:
                    dt = int(math.ceil(job.remaining_in_stage / self.freq[d] * PS_PER_S))
                    nxt = min(nxt, self.now_ps + max(dt, 1))
            self._advance(max(nxt - self.now_ps, 1))
        done, self._completed = self._completed, []
        return done

    # -------------------------------------------------------------- internals
    def _reset_counters(self) -> None:
        self._ctr = {d: {"busy_ps": 0.0, "idle_ps": 0.0, "gated_ps": 0.0, "wakeups": 0} for d in self.freq}

    def _admit_releases(self) -> None:
        while self._releases and self._releases[0][0] <= self.now_ps:
            _, _, job = heapq.heappop(self._releases)
            self._jobs[job.job_id] = job
            self._enqueue(job)

    def _enqueue(self, job: _Job) -> None:
        d = job.domain
        job.ready_ps = self.now_ps
        self._ready[d].append(job)
        if self._gated[d]:
            self._gated[d] = False
            self._ctr[d]["wakeups"] += 1
            self._stall_until[d] = max(self._stall_until[d], self.now_ps + int(self._gating[d] or 0))

    def _pick(self, d: str) -> _Job | None:
        q = self._ready[d]
        if not q:
            return None
        pol = self._sched[d]
        if pol in ("edf", "edf_mc"):
            return min(q, key=lambda j: (j.deadline_ps, j.job_id))
        if pol == "fixed_priority":
            return min(q, key=lambda j: (j.task.priority, j.job_id))
        return min(q, key=lambda j: (j.ready_ps, j.job_id))

    def _advance(self, dt: int) -> None:
        t0 = self.now_ps
        for d in self.freq:
            c = self._ctr[d]
            stall_end = self._stall_until[d]
            stall = min(max(stall_end - t0, 0), dt)
            run = dt - stall
            job = self._pick(d) if run > 0 else None
            if stall > 0:
                c["idle_ps"] += stall
                if stall_end <= t0 + dt:
                    self.freq[d] = self._target_freq[d]
            if job is None:
                c["idle_ps"] += run
                if self._gated[d]:
                    c["gated_ps"] += run
                elif self._gating[d] is not None and run > 0:
                    self._gated[d] = True
                    c["gated_ps"] += run
                continue
            if job.start_ps is None:
                job.start_ps = t0 + stall
            cycles = run / PS_PER_S * self.freq[d]
            cycles = min(cycles, job.remaining_in_stage)
            job.done_in_stage += cycles
            job.cycles_by_domain[d] = job.cycles_by_domain.get(d, 0.0) + cycles
            c["busy_ps"] += run
            for k, n in self.peak_activity.get(d, {}).items():
                c[k] = c.get(k, 0.0) + cycles * n * job.task.utilization
        for d in self.freq:
            if self._stall_until[d] <= t0 + dt and self.freq[d] != self._target_freq[d]:
                self.freq[d] = self._target_freq[d]
        self.now_ps = t0 + dt
        self._finish_stages()

    def _finish_stages(self) -> None:
        for d in self.freq:
            q = self._ready[d]
            for job in list(q):
                if job.remaining_in_stage <= 1e-6 * max(job.cycles[job.stage], 1.0):
                    q.remove(job)
                    job.stage += 1
                    job.done_in_stage = 0.0
                    if job.stage >= len(job.task.stages):
                        self._complete(job)
                    else:
                        self._enqueue(job)
            if not self._ready[d] and self._gating[d] is not None and self._stall_until[d] <= self.now_ps:
                self._gated[d] = True

    def _complete(self, job: _Job) -> None:
        del self._jobs[job.job_id]
        fin = self.now_ps
        self._completed.append({
            "job_id": job.job_id, "task_id": job.task.task_id, "tag": job.tag, "release_ps": job.release_ps,
            "start_ps": job.start_ps if job.start_ps is not None else fin, "finish_ps": fin,
            "deadline_ps": job.deadline_ps, "missed": fin > job.deadline_ps, "response_ps": fin - job.release_ps,
            "cycles_by_domain": dict(job.cycles_by_domain),
        })


def run_closed_loop(sim: FakeSim, manager: Any, duration_ps: int, step_ps: int = PS_PER_S // 1000) -> dict[str, Any]:
    """Bridge-like loop: release jobs due in [t, t+step), manager.step(t), run_until(t+step)."""
    manager.apply_scheduler(sim)
    next_rel = {tid: t.offset_ps for tid, t in sim.tasks.items()}
    tag = 0
    t = 0
    while t < duration_ps:
        for tid, task in sim.tasks.items():
            while next_rel[tid] < t + step_ps and next_rel[tid] < duration_ps:
                sim.release_job(tid, next_rel[tid], tag)
                tag += 1
                next_rel[tid] += task.period_ps
        sim._admit_releases()
        manager.step(sim, t)
        done = sim.run_until(t + step_ps)
        manager.observe_completions(done)
        t += step_ps
    manager.step(sim, t)
    return manager.summary()


MS = PS_PER_S // 1000


def synthetic_tasks(scale: float = 1.0) -> list[FakeTask]:
    """A SYNTHETIC periodic job set loosely shaped like the robot stack (not captured workloads).

    `scale` multiplies every task's cycles (heavier load at scale > 1).
    """
    s = scale
    return [
        FakeTask("ctrl", 1 * MS, 1 * MS, [("rt", 0.30e6 * s)], jitter=0.10, priority=0, criticality="safety",
                 utilization=0.5),
        FakeTask("safety_mon", 10 * MS, 10 * MS, [("dsp", 1.5e6 * s), ("rt", 0.06e6 * s)], jitter=0.10, priority=1,
                 criticality="safety", utilization=0.6),
        FakeTask("estimate", 5 * MS, 5 * MS, [("cpu_b", 1.0e6 * s)], jitter=0.15, priority=2, utilization=0.5),
        FakeTask("detect", 33 * MS, 33 * MS, [("npu", 9.6e6 * s), ("cpu_a", 4.0e6 * s)], jitter=0.15, priority=3,
                 utilization=0.7),
        FakeTask("map", 66 * MS, 66 * MS, [("simt", 13.0e6 * s)], jitter=0.15, priority=4, utilization=0.6),
        FakeTask("vla", 125 * MS, 100 * MS, [("npu", 30.0e6 * s), ("cpu_a", 10.0e6 * s)], jitter=0.15, priority=5,
                 utilization=0.7),
    ]


def make_fakesim(manager: Any, tasks: list[FakeTask], seed: int = 0) -> FakeSim:
    """FakeSim whose domain frequencies, transition latency and activity match a PowerManager."""
    freqs = {d: v.nominal.freq_hz for d, v in manager.vf.items()}
    trans = next(iter(manager.vf.values())).transition_ps
    return FakeSim(tasks, freqs, manager.energy.peak_activity, transition_ps=trans, seed=seed)


def task_info(tasks: list[FakeTask]) -> dict[str, dict[str, Any]]:
    return {t.task_id: t.info() for t in tasks}
