"""Shared pytest fixtures for glasschip-core Python API tests."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Callable

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "core" / "build"))
sys.path.insert(0, str(ROOT / "tools"))

import glasschip_core as gc  # noqa: E402

PS_MS = 1_000_000_000


@pytest.fixture(scope="session")
def soc() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "configs" / "soc" / "vx1.yaml").read_text())


def load_fidelity(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / "configs" / "fidelity" / f"{name}.yaml").read_text())


def fixture_graph(name: str) -> dict[str, Any]:
    return json.loads((FIX / f"{name}.json").read_text())


ALL_FIXTURES = ["T90_cam", "T91_det", "T92_policy", "T93_esdf", "T94_ctl", "T95_est", "T96_log"]


@pytest.fixture
def make_sim(soc: dict[str, Any]) -> Callable[..., Any]:
    """Factory: make_sim(mix='interactive', seed=1, tasks=ALL_FIXTURES, middleware=('copy','single_threaded'))."""

    def _make(mix: str = "interactive", seed: int = 1, tasks: list[str] | None = None, middleware: tuple[str, str] | None = ("copy", "single_threaded"),
              fidelity: dict[str, Any] | None = None) -> Any:
        sim = gc.Sim(soc, fidelity or load_fidelity(mix), seed)
        for t in tasks or ALL_FIXTURES:
            sim.add_task(fixture_graph(t))
        if middleware:
            sim.set_middleware(fixture_graph("middleware"), middleware[0], middleware[1])
        return sim

    return _make


def drive(sim: Any, t0_ms: int, t1_ms: int, tasks: list[str] | None = None) -> list[dict[str, Any]]:
    """Release fixture tasks at their periods in 1 ms steps; return completions."""
    periods = {"T90_cam": 33, "T91_det": 33, "T92_policy": 125, "T93_esdf": 66, "T94_ctl": 1, "T95_est": 5, "T96_log": 33}
    tasks = tasks or list(periods)
    out: list[dict[str, Any]] = []
    for ms in range(t0_ms, t1_ms):
        for t in tasks:
            if ms % periods[t] == 0:
                sim.release_job(t, ms * PS_MS, ms)
        out += sim.run_until((ms + 1) * PS_MS)
    return out
