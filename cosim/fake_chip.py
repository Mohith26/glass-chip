"""Stand-in chip models that implement the co-sim ChipModel protocol.

ZeroLatencyChip: every job completes at its release time (an infinitely fast chip). This is
the zero-latency robot baseline of spec Section 11.4.

FixedLatencyChip: each task has a fixed latency plus optional jitter. Used for the
latency-tolerance study (s14) and for tests of the stale-data semantics. Per task overrun
behavior:

    pipeline   jobs overlap freely (latency only, no throughput limit)
    skip       one job at a time; a release while busy is skipped (returns -1)
    queue      one job at a time; releases wait in FIFO order

Times are int64 picoseconds, like the glasschip_core API.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any

import numpy as np

PS_PER_MS = 1_000_000_000
PS_PER_US = 1_000_000

# Default relative deadlines (ms) from spec Section 6.1. Defaults, not measured requirements.
DEFAULT_DEADLINE_MS: dict[str, float] = {
    "T1_ingest_wrist": 15, "T1_ingest_overhead": 15, "T2_detect": 33, "T2_detect_overhead": 33, "T3_esdf": 66, "T4_vla": 100,
    "T5_plan": 50, "T6_estimate": 5, "T7_control": 1, "T8_safety": 10, "T10_record": 1000,
    "T11_speech": 200, "T12_arm2_control": 1, "T13_map_save": 1000, "T14_ota": 5000, "T15_diag": 1000,
}

# How the injected end-to-end latency of s14 is split over the camera-to-grasp chain.
E2E_SPLIT: dict[str, float] = {"T1_ingest_wrist": 0.10, "T1_ingest_overhead": 0.10, "T2_detect": 0.35,
                               "T2_detect_overhead": 0.35, "T4_vla": 0.35, "T5_plan": 0.20}


def _deadline_ps(task: str, deadlines_ms: dict[str, float] | None) -> int:
    ms = (deadlines_ms or {}).get(task, DEFAULT_DEADLINE_MS.get(task, 100.0))
    return int(round(ms * PS_PER_MS))


class ZeroLatencyChip:
    """Infinitely fast chip: finish_ps == release_ps for every job."""

    name = "zero_latency"

    def __init__(self, deadlines_ms: dict[str, float] | None = None) -> None:
        self.deadlines_ms = deadlines_ms
        self._done: list[dict[str, Any]] = []
        self._next = 0
        self.now_ps = 0

    def release_job(self, task_id: str, t_ps: int, tag: int) -> int:
        jid = self._next
        self._next += 1
        dl = _deadline_ps(task_id, self.deadlines_ms)
        self._done.append({"job_id": jid, "task_id": task_id, "tag": int(tag), "release_ps": int(t_ps),
                           "start_ps": int(t_ps), "finish_ps": int(t_ps), "deadline_ps": int(t_ps) + dl,
                           "missed": False, "response_ps": 0})
        return jid

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        self.now_ps = max(self.now_ps, int(t_ps))
        out = [c for c in self._done if c["finish_ps"] <= t_ps]
        self._done = [c for c in self._done if c["finish_ps"] > t_ps]
        return out

    def pending_jobs(self) -> list[dict[str, Any]]:
        return []


@dataclass
class TaskTiming:
    latency_ms: float = 0.0
    jitter_ms: float = 0.0          # uniform in [0, jitter] added to latency
    overrun: str = "pipeline"       # pipeline | skip | queue


class FixedLatencyChip:
    """Per-task fixed latency plus seeded jitter, with simple overrun handling."""

    name = "fixed_latency"

    def __init__(self, timings: dict[str, TaskTiming] | None = None, default: TaskTiming | None = None,
                 seed: int = 0, deadlines_ms: dict[str, float] | None = None) -> None:
        self.timings = dict(timings or {})
        self.default = default or TaskTiming()
        self.rng = np.random.default_rng(seed)
        self.deadlines_ms = deadlines_ms
        self._heap: list[tuple[int, int, dict[str, Any]]] = []
        self._busy_until: dict[str, int] = {}
        self._next = 0
        self.now_ps = 0
        self.skipped: dict[str, int] = {}

    @classmethod
    def with_injected_e2e(cls, e2e_ms: float, seed: int = 0, base: dict[str, TaskTiming] | None = None,
                          split: dict[str, float] | None = None, jitter_frac: float = 0.0) -> "FixedLatencyChip":
        """Spread e2e_ms over the camera-to-grasp chain (T1, T2, T4, T5) using `split`.

        Each camera's T1 and detection task gets the share (only one camera sits on any given
        chain, so the shares along a chain add up to e2e_ms). Other tasks
        keep small fixed latencies inside their deadlines (T6 0.5 ms, T7 0.2 ms, T8 1 ms).
        """
        split = split or E2E_SPLIT
        t: dict[str, TaskTiming] = {
            "T6_estimate": TaskTiming(0.5, 0.0, "pipeline"),
            "T7_control": TaskTiming(0.2, 0.0, "skip"),
            "T8_safety": TaskTiming(1.0, 0.0, "pipeline"),
            "T3_esdf": TaskTiming(10.0, 0.0, "pipeline"),
        }
        t.update(base or {})
        for task, frac in split.items():
            lat = e2e_ms * frac
            t[task] = TaskTiming(lat, lat * jitter_frac, "pipeline")
        return cls(t, TaskTiming(1.0, 0.0, "pipeline"), seed=seed)

    def timing(self, task_id: str) -> TaskTiming:
        return self.timings.get(task_id, self.default)

    def release_job(self, task_id: str, t_ps: int, tag: int) -> int:
        tm = self.timing(task_id)
        t_ps = int(t_ps)
        lat = tm.latency_ms + (self.rng.random() * tm.jitter_ms if tm.jitter_ms > 0 else 0.0)
        dur = int(round(lat * PS_PER_MS))
        start = t_ps
        if tm.overrun != "pipeline":
            busy = self._busy_until.get(task_id, -1)
            if busy > t_ps:
                if tm.overrun == "skip":
                    self.skipped[task_id] = self.skipped.get(task_id, 0) + 1
                    return -1
                start = busy
        finish = start + dur
        if tm.overrun != "pipeline":
            self._busy_until[task_id] = finish
        jid = self._next
        self._next += 1
        dl = t_ps + _deadline_ps(task_id, self.deadlines_ms)
        c = {"job_id": jid, "task_id": task_id, "tag": int(tag), "release_ps": t_ps, "start_ps": start,
             "finish_ps": finish, "deadline_ps": dl, "missed": finish > dl, "response_ps": finish - t_ps}
        heapq.heappush(self._heap, (finish, jid, c))
        return jid

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        self.now_ps = max(self.now_ps, int(t_ps))
        out = []
        while self._heap and self._heap[0][0] <= t_ps:
            out.append(heapq.heappop(self._heap)[2])
        return out

    def pending_jobs(self) -> list[dict[str, Any]]:
        return [{"job_id": c["job_id"], "task_id": c["task_id"], "release_ps": c["release_ps"],
                 "deadline_ps": c["deadline_ps"], "started": c["start_ps"] <= self.now_ps}
                for _, _, c in sorted(self._heap)]
