"""Adapter that plugs the real SoC simulator (glasschip_core) into the co-sim.

glasschip_core.Sim already satisfies the ChipModel protocol (release_job, run_until,
pending_jobs). This module builds one from config files and task graphs, and adds three
things the core does not do itself:

* tasks that have no graph go to a FixedLatencyChip fallback so a scenario never stalls;
* bitrate changes (scenario S7 `set_bitrate`): the core cannot rescale a task after it is
  added, so for every bitrate a scenario will use I add a scaled copy of the graph (ops
  marked `bytes_scale_param: bitrate_bps` scale linearly) under an alias id, release the
  copy that matches the current bitrate, and report completions under the base task id;
* activity counters are also accumulated into run totals (DRAM bytes, busy time, ...),
  while still being handed to the caller (the power manager resets them every step).

The import is guarded: if the core module is not built, `core_available()` is False.
"""

from __future__ import annotations

import copy
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from cosim.fake_chip import FixedLatencyChip, TaskTiming

ROOT = Path(__file__).resolve().parents[1]
CORE_BUILD = ROOT / "core" / "build"

try:  # guarded import: the core is built by a separate stream
    if str(CORE_BUILD) not in sys.path:
        sys.path.insert(0, str(CORE_BUILD))
    import glasschip_core as _gc  # type: ignore
except Exception:  # pragma: no cover - depends on the build
    _gc = None


def core_available() -> bool:
    return _gc is not None


def _load_yaml(p: Path | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(p, Mapping):
        return copy.deepcopy(dict(p))
    return yaml.safe_load(Path(p).read_text())


def deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def scale_graph_bitrate(graph: Mapping[str, Any], bitrate_bps: float, new_id: str) -> dict[str, Any]:
    """Copy of a task graph with bitrate-scaled ops resized to bitrate_bps."""
    g = copy.deepcopy(dict(graph))
    base = float((g.get("params") or {}).get("bitrate_bps", 0) or 0)
    if base <= 0:
        raise ValueError(f"{graph.get('task_id')}: no params.bitrate_bps to scale from")
    f = float(bitrate_bps) / base
    for op in g.get("ops", []):
        if op.get("bytes_scale_param") == "bitrate_bps":
            for k in ("bytes_in", "bytes_out", "bytes_w"):
                if op.get(k):
                    op[k] = max(1, int(round(op[k] * f)))
            ins = op.get("instr")
            if isinstance(ins, dict):
                op["instr"] = {k: int(round(v * f)) for k, v in ins.items()}
    g["task_id"] = new_id
    g.setdefault("params", {})["bitrate_bps"] = float(bitrate_bps)
    return g


def bitrate_alias(task: str, bps: float) -> str:
    return f"{task}@{bps / 1e9:.4g}G"


COUNTER_KEYS = ("busy_ps", "idle_ps", "unit_busy_ps", "int8_macs", "bf16_macs", "fp8_macs", "sram_bytes", "flit_hops",
                "cache_bytes", "dram_bytes", "dram_activates", "cpu_instr", "vec_ops", "simt_flops", "dsp_ops",
                "dma_bytes")


class CoreChip:
    """glasschip_core.Sim plus a fixed-latency fallback, bitrate aliases and counter totals."""

    name = "glasschip_core"

    def __init__(self, sim: Any, known_tasks: set[str], fallback: FixedLatencyChip | None = None,
                 info: dict[str, Any] | None = None, bitrate_alias_map: Mapping[str, Mapping[float, str]] | None = None,
                 base_bitrate: Mapping[str, float] | None = None) -> None:
        self.sim = sim
        self.known = known_tasks
        self.fallback = fallback or FixedLatencyChip(default=TaskTiming(1.0, 0.0, "pipeline"))
        self.info = info or {}
        self._fb_base = 1 << 40  # fallback job ids live in a separate high range
        self._aliases: dict[str, dict[float, str]] = {k: dict(v) for k, v in (bitrate_alias_map or {}).items()}
        self._alias_to_base = {a: t for t, m in self._aliases.items() for a in m.values() if a != t}
        self._current: dict[str, str] = {}
        self.bitrate = dict(base_bitrate or {})
        self.totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    # -- ChipModel -----------------------------------------------------------------
    def release_job(self, task_id: str, t_ps: int, tag: int) -> int:
        tid = self._current.get(task_id, task_id)
        if tid in self.known:
            return int(self.sim.release_job(tid, int(t_ps), int(tag)))
        jid = self.fallback.release_job(task_id, t_ps, tag)
        return -1 if jid < 0 else self._fb_base + jid

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        out = list(self.sim.run_until(int(t_ps)))
        if self._alias_to_base:
            for c in out:
                base = self._alias_to_base.get(c["task_id"])
                if base is not None:
                    c["task_id"] = base
        for c in self.fallback.run_until(t_ps):
            c = dict(c)
            c["job_id"] = self._fb_base + int(c["job_id"])
            out.append(c)
        return out

    def pending_jobs(self) -> list[dict[str, Any]]:
        pj = list(self.sim.pending_jobs())
        if self._alias_to_base:
            for j in pj:
                base = self._alias_to_base.get(j.get("task_id"))
                if base is not None:
                    j["task_id"] = base
        return pj + self.fallback.pending_jobs()

    # -- extras --------------------------------------------------------------------
    def counters(self, reset: bool = True) -> dict[str, Any]:
        c = self.sim.counters(reset)
        if reset:
            for dom, vals in c.items():
                if isinstance(vals, Mapping):
                    tot = self.totals[dom]
                    for k in COUNTER_KEYS:
                        v = vals.get(k)
                        if isinstance(v, (int, float)):
                            tot[k] += float(v)
                    if "units" in vals:
                        tot["units"] = float(vals["units"])
        return c

    def set_bitrate(self, task: str, bps: float) -> bool:
        """Switch `task` to its pre-added copy for `bps`. Returns False if no copy exists."""
        m = self._aliases.get(task)
        if not m:
            return False
        key = min(m, key=lambda b: abs(b - float(bps)))
        if abs(key - float(bps)) > 1e-6 * max(1.0, float(bps)):
            return False
        self._current[task] = m[key]
        self.bitrate[task] = key
        return True

    def __getattr__(self, item: str) -> Any:
        # set_domain_freq, trace_*, set_qos and the rest go straight to the core.
        if item == "sim":
            raise AttributeError(item)
        return getattr(self.sim, item)


def make_core_chip(tasks: Iterable[str], soc: str | Path | Mapping[str, Any] = ROOT / "configs/soc/vx1.yaml",
                   fidelity: str | Path | Mapping[str, Any] = ROOT / "configs/fidelity/interactive.yaml",
                   graph_dir: str | Path = ROOT / "workloads/graphs", seed: int = 0,
                   transport: str = "copy", executor: str = "single_threaded",
                   scheduler: dict[str, str] | None = None, soc_overrides: Mapping[str, Any] | None = None,
                   fidelity_params: Mapping[str, Any] | None = None,
                   bitrates: Mapping[str, Iterable[float]] | None = None,
                   graph_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> CoreChip:
    """Build a CoreChip with every task in `tasks` that has a graph file in graph_dir.

    soc / fidelity may be paths or already-parsed dicts. soc_overrides is deep-merged into the
    SoC dict (design-space studies). bitrates maps a task id to every bitrate a scenario will
    switch it to (scaled graph copies are added up front). graph_overrides replaces the graph
    loaded for a task id with the given dict (e.g. a re-costed T5_plan).
    """
    if _gc is None:
        raise RuntimeError(f"glasschip_core is not importable from {CORE_BUILD}; build the core first")
    soc_d = _load_yaml(soc)
    if soc_overrides:
        soc_d = deep_merge(soc_d, soc_overrides)
    fid_d = _load_yaml(fidelity)
    if fidelity_params:
        fid_d = deep_merge(fid_d, {"params": dict(fidelity_params)})
    sim = _gc.Sim(soc_d, fid_d, int(seed))
    gdir = Path(graph_dir)
    mw = gdir / "middleware.json"
    if mw.exists():
        sim.set_middleware(json.loads(mw.read_text()), transport, executor)
    known: set[str] = set()
    aliases: dict[str, dict[float, str]] = {}
    base_rate: dict[str, float] = {}
    for t in sorted(set(tasks)):
        if graph_overrides and t in graph_overrides:
            g = copy.deepcopy(dict(graph_overrides[t]))
            g["task_id"] = t
        else:
            f = gdir / f"{t}.json"
            if not f.exists():
                continue
            g = json.loads(f.read_text())
        sim.add_task(g)
        known.add(t)
        rates = sorted({float(b) for b in (bitrates or {}).get(t, [])})
        if rates:
            base = float((g.get("params") or {}).get("bitrate_bps", 0) or 0)
            base_rate[t] = base
            aliases[t] = {base: t}
            for b in rates:
                if abs(b - base) <= 1e-6 * base:
                    continue
                aid = bitrate_alias(t, b)
                sim.add_task(scale_graph_bitrate(g, b, aid))
                known.add(aid)
                aliases[t][b] = aid
    for eng, pol in (scheduler or {}).items():
        sim.set_scheduler(eng, pol)
    return CoreChip(sim, known, info={"soc": "dict" if isinstance(soc, Mapping) else str(soc),
                                      "fidelity": "dict" if isinstance(fidelity, Mapping) else str(fidelity),
                                      "graph_dir": str(gdir), "tasks_on_core": sorted(known)},
                    bitrate_alias_map=aliases, base_bitrate=base_rate)
