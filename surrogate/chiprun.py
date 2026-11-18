"""Chip-only driver for design-space sampling (no robot physics).

The surrogate dataset needs thousands of runs, so most samples skip MuJoCo and drive
glasschip_core directly with the same release rules the co-sim bridge uses:

* periodic tasks release on the 1 ms grid at offset + k * period (bridge semantics, no jitter
  beyond what the 1 ms grid introduces);
* T2_detect is released when a wrist T1 job completes, T2_detect_overhead when an overhead
  T1 job completes (chained, as in cosim/bridge.py);
* T5_plan is released when a T4_vla job completes and the policy would emit a new goal. Without
  a robot there is no goal signature, so I use the scenario's sporadic rate: every
  round(f_T4 / sporadic_rate_hz)-th T4 completion triggers a plan (2 Hz by default);
* the PowerManager steps every 1 ms exactly as in the bridge.

Scenario variants are steady-state versions of S1/S2/S3/S6 (events fire at t = 0) plus two
continuous knobs that stand in for S5 (ambient temperature) and S7 (recording bitrate).

Robot success is not produced here; it comes from the co-simulated subset (see dataset.py).
"""
from __future__ import annotations

import copy
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CORE_BUILD = ROOT / "core" / "build"
# The dataset pins a snapshot of the core module and SoC config (data/surrogate/snapshot/) so a
# core rebuild or a vx1.yaml edit by another stream mid-run cannot mix two models in one dataset.
SNAPSHOT = ROOT / "data" / "surrogate" / "snapshot"
_core_dir = SNAPSHOT if (SNAPSHOT.exists() and os.environ.get("GLASSCHIP_SURROGATE_LIVE_CORE") != "1") else CORE_BUILD
if str(_core_dir) not in sys.path:
    sys.path.insert(0, str(_core_dir))
# Same for the power library (power/ is edited by another stream while the dataset runs).
if _core_dir == SNAPSHOT and (SNAPSHOT / "pkg" / "power").exists():
    sys.path.insert(0, str(SNAPSHOT / "pkg"))

PS_PER_MS = 1_000_000_000
PS_PER_US = 1_000_000
GRAPH_DIR = ROOT / "workloads" / "graphs"
SOC_PATH = (SNAPSHOT / "vx1.yaml") if (SNAPSHOT / "vx1.yaml").exists() else ROOT / "configs" / "soc" / "vx1.yaml"
POWER_PARAMS_PATH = (SNAPSHOT / "params_vx1.yaml") if (SNAPSHOT / "params_vx1.yaml").exists() else ROOT / "power" / "params_vx1.yaml"
DSE_FIDELITY_PATH = Path(__file__).resolve().parent / "fidelity_dse.yaml"

BASE_TASKS = ["T1_ingest_wrist", "T1_ingest_overhead", "T2_detect", "T3_esdf", "T4_vla", "T5_plan",
              "T6_estimate", "T7_control", "T8_safety", "T10_record_4k"]
S6_EXTRA = ["T2_detect_overhead", "T11_speech", "T12_arm2_control", "T13_map_save", "T14_ota", "T15_diag"]
SCENARIOS = ["S1", "S2", "S3", "S6"]
SAFETY_TASKS = ["T7_control", "T8_safety", "T12_arm2_control"]
CHAINED_FROM = {"T1_ingest_wrist": "T2_detect", "T1_ingest_overhead": "T2_detect_overhead"}

_GRAPHS: dict[str, dict[str, Any]] = {}


def load_graph(task: str) -> dict[str, Any]:
    if task not in _GRAPHS:
        _GRAPHS[task] = json.loads((GRAPH_DIR / f"{task}.json").read_text())
    return copy.deepcopy(_GRAPHS[task])


def scale_bitrate(graph: dict[str, Any], bitrate_bps: float) -> dict[str, Any]:
    """Scale a recording graph's byte and instruction counts to a new bitrate (bytes_scale_param)."""
    p = graph.get("params", {})
    base = float(p.get("bitrate_bps", 0) or 0)
    if base <= 0:
        return graph
    s = bitrate_bps / base
    for op in graph["ops"]:
        if op.get("bytes_scale_param") != "bitrate_bps":
            continue
        for k in ("bytes_in", "bytes_out", "bytes_w"):
            if op.get(k):
                op[k] = int(round(op[k] * s))
        if isinstance(op.get("instr"), dict):
            op["instr"] = {k: int(round(v * s)) for k, v in op["instr"].items()}
    p["bitrate_bps"] = bitrate_bps
    return graph


def scenario_tasks(scenario: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Task list and per-task overrides {period_us, deadline_us} for a steady-state scenario."""
    tasks = list(BASE_TASKS)
    ov: dict[str, dict[str, Any]] = {}
    if scenario in ("S2", "S6"):
        tasks.append("T2_detect_overhead")
    if scenario == "S6":
        tasks += [t for t in S6_EXTRA if t not in tasks]
    if scenario == "S3":
        # configs/scenarios/S3_human_enters.yaml: safety escalation to 5 ms period and deadline,
        # replan burst of 4 plans in 200 ms (here: one burst per second while the human is present).
        ov["T8_safety"] = {"period_us": 5000, "deadline_us": 5000}
    return tasks, ov


def deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def soc_from_config(cfg: Mapping[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the design knobs to the VX-1 SoC dict (see dataset.SPACE for ranges)."""
    soc = copy.deepcopy(base if base is not None else yaml.safe_load(SOC_PATH.read_text()))
    b = soc["blocks"]
    tiles = int(cfg["npu_tiles"])
    arr = int(cfg["npu_array"])
    b["npu"]["tiles"] = tiles
    b["npu"]["array"] = [arr, arr]
    b["npu"]["int8_macs_per_cycle_per_tile"] = arr * arr
    b["npu"]["sram_per_tile_kb"] = float(cfg["npu_sram_mb"]) * 1024.0
    # Extra tiles share the four NPU mesh nodes round robin (core: nodes[i % n]).
    b["sys_cache"]["size_kb"] = int(round(float(cfg["cache_mb"]))) * 1024
    soc["noc"]["link_bits"] = int(cfg["noc_bits"])
    # DRAM bandwidth knob -> data rate at the fixed 128-bit bus (timing in ns unchanged).
    gbps = float(cfg["dram_gbps"])
    b["dram"]["data_rate_mts"] = gbps * 1e9 * 8 / b["dram"]["bus_bits_total"] / 1e6
    cores = int(cfg["cpu_cores"])
    b["cpu_a"]["cores"] = int(math.ceil(cores / 2))
    b["cpu_b"]["cores"] = int(cores // 2)
    return soc


def policy_from_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    from power.params import load_policy
    mode = str(cfg["power_policy"])
    pid = {"nominal": "P0", "race_to_idle": "P3", "pace_to_deadline": "P4", "surrogate_guided": "P6"}[mode]
    pol = load_policy(pid)
    pol["scheduler"] = str(cfg["scheduler"])
    return pol


def _memoize_predictor(pred: Any) -> None:
    """Cache ExecTimePredictor.inflation / demand_cycles between observations.

    The P4 policy calls these (each a numpy quantile over up to 200 samples) once per pending
    job per domain per 1 ms step. Their inputs only change when observe_pending or
    observe_completion is called, so I clear the cache there. Results are identical; this only
    removes repeated work (it was most of the Python time for P4 runs).
    """
    cache: dict[tuple[Any, ...], Any] = {}
    infl, dem = pred.inflation, pred.demand_cycles
    obs_p, obs_c = pred.observe_pending, pred.observe_completion

    def inflation(task: str, domain: str, safety: bool | None = None) -> float:
        k = ("i", task, domain, safety)
        if k not in cache:
            cache[k] = infl(task, domain, safety)
        return cache[k]

    def demand_cycles(task: str, domain: str, safety: bool | None = None) -> float | None:
        k = ("d", task, domain, safety)
        if k not in cache:
            cache[k] = dem(task, domain, safety)
        return cache[k]

    def observe_pending(*a: Any, **kw: Any) -> None:
        cache.clear()
        obs_p(*a, **kw)

    def observe_completion(*a: Any, **kw: Any) -> None:
        cache.clear()
        obs_c(*a, **kw)

    pred.inflation, pred.demand_cycles = inflation, demand_cycles
    pred.observe_pending, pred.observe_completion = observe_pending, observe_completion


def _quantiles(x: list[float]) -> tuple[float, float, float]:
    if not x:
        return (float("nan"),) * 3
    a = np.asarray(x, float)
    return tuple(float(v) for v in np.quantile(a, [0.5, 0.95, 0.99]))  # type: ignore[return-value]


def run_chip(cfg: Mapping[str, Any], seed: int = 1, duration_s: float = 2.0, warmup_s: float = 0.25,
             fidelity: Mapping[str, Any] | None = None, surrogate: Any = None,
             soc_base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one design point chip-only; returns a flat metrics dict.

    cfg keys: npu_tiles, npu_array, npu_sram_mb, cache_mb, cache_partition (0/1), qos (0/1),
    noc_bits, dram_gbps, cpu_cores, scheduler, power_policy, vmin_npu, vmin_cpu, vmin_simt,
    vmin_dsp, lv_arith_factor, transport, scenario, bitrate_gbps, ambient_c.
    """
    import glasschip_core as gc
    from power.manager import PowerManager
    from power.predictor import task_info_from_graphs

    t_wall = time.perf_counter()
    t_cpu = time.process_time()
    soc = soc_from_config(cfg, soc_base)
    fid = dict(fidelity) if fidelity is not None else yaml.safe_load(DSE_FIDELITY_PATH.read_text())
    scenario = str(cfg["scenario"])
    tasks, ov = scenario_tasks(scenario)
    graphs: dict[str, dict[str, Any]] = {}
    for t in tasks:
        g = load_graph(t)
        if t == "T10_record_4k":
            g = scale_bitrate(g, float(cfg["bitrate_gbps"]) * 1e9)
        if t in ov:
            if "period_us" in ov[t]:
                g["release"]["period_us"] = int(ov[t]["period_us"])
            if "deadline_us" in ov[t]:
                g["deadline_us"] = int(ov[t]["deadline_us"])
        graphs[t] = g

    sim = gc.Sim(soc, fid, int(seed))
    sim.set_middleware(json.loads((GRAPH_DIR / "middleware.json").read_text()), str(cfg["transport"]),
                       "single_threaded")
    for t in tasks:
        sim.add_task(graphs[t])
    if int(cfg.get("qos", 0)):
        sim.set_qos(True)
    if int(cfg.get("cache_partition", 0)):
        ways = int(soc["blocks"]["sys_cache"].get("ways", 16))
        sim.set_cache_partition({"safety": 2, "realtime": ways - 6, "best_effort": 4})

    pol = policy_from_config(cfg)
    lvf = float(cfg.get("lv_arith_factor", 1.0))
    lv = {"enabled": lvf > 1.0 + 1e-9, "reduction_x": max(lvf, 1.0)}
    from power.params import load_params
    pm = PowerManager(pol, soc, params=load_params(POWER_PARAMS_PATH), ambient_c=float(cfg["ambient_c"]), task_info=task_info_from_graphs(graphs.values()),
                      surrogate=surrogate, thermal_init="steady", low_voltage_arith=lv, record_series=False)
    for dom, key in (("npu", "vmin_npu"), ("cpu_a", "vmin_cpu"), ("cpu_b", "vmin_cpu"), ("simt", "vmin_simt"),
                     ("dsp", "vmin_dsp")):
        if key in cfg and cfg[key] is not None:
            v = float(cfg[key])
            # Static policies: a value below nominal is a static undervolt (S8 semantics).
            # P4/P6: the value is the DVFS floor.
            if not pm.policy.static or v < 0.75 - 1e-9:
                pm.set_vmin(dom, v)
    _memoize_predictor(pm.predictor)
    pm.apply_scheduler(sim)

    # Release bookkeeping (bridge semantics on a 1 ms grid).
    periodic = [t for t in tasks if t not in ("T2_detect", "T2_detect_overhead", "T5_plan")]
    next_rel = {}
    for t in periodic:
        rel = graphs[t]["release"]
        next_rel[t] = int(rel.get("offset_us", 0) or 0) * PS_PER_US
    period_ps = {t: int(graphs[t]["release"]["period_us"]) * PS_PER_US for t in periodic}
    t4_per_plan = max(1, int(round((1e6 / graphs["T4_vla"]["release"]["period_us"]) / 2.0)))
    burst_ms = {0, 50, 100, 150} if scenario == "S3" else set()

    n_steps = int(round(duration_s * 1000))
    warm_ps = int(round(warmup_s * 1000)) * PS_PER_MS
    rel_ps: dict[int, tuple[str, int]] = {}
    tag = 0
    t4_done = 0
    last_det_origin: int | None = None          # T1 release of the newest finished T2 (any camera)
    t4_origin: dict[int, int | None] = {}        # T4 job -> origin of the detection it consumed
    t5_origin: dict[int, int | None] = {}
    last_t8_origin: int | None = None
    last_ovh_t1_rel: int | None = None
    t8_origin: dict[int, int | None] = {}
    pending_t7_chain: list[int] = []             # origins waiting for the next T7 completion
    resp: dict[str, list[float]] = {t: [] for t in tasks}
    miss: dict[str, int] = {t: 0 for t in tasks}
    cnt: dict[str, int] = {t: 0 for t in tasks}
    skipped: dict[str, int] = {t: 0 for t in tasks}
    chain_grasp: list[float] = []
    chain_safety: list[float] = []
    det_lat: list[float] = []

    def release(task: str, t_ps: int) -> int:
        nonlocal tag
        tag += 1
        jid = int(sim.release_job(task, int(t_ps), tag))
        if jid < 0:
            if t_ps >= warm_ps:
                skipped[task] += 1
            return -1
        rel_ps[jid] = (task, t_ps)
        if task == "T4_vla":
            t4_origin[jid] = last_det_origin
        if task == "T8_safety":
            t8_origin[jid] = last_ovh_t1_rel
        return jid

    def complete(c: Mapping[str, Any], t_ps: int) -> None:
        nonlocal t4_done, last_det_origin, last_ovh_t1_rel
        task = str(c["task_id"])
        jid = int(c["job_id"])
        r = int(c["release_ps"])
        rel_ps.pop(jid, None)
        if r >= warm_ps:
            cnt[task] += 1
            resp[task].append(int(c["response_ps"]) / PS_PER_MS)
            if c.get("missed"):
                miss[task] += 1
        if task in CHAINED_FROM and CHAINED_FROM[task] in graphs:
            if task == "T1_ingest_overhead":
                last_ovh_t1_rel = r
            det = CHAINED_FROM[task]
            j2 = release(det, t_ps)
            if j2 >= 0:
                t5_origin[j2] = r      # remember camera origin on the detection job
        elif task == "T1_ingest_overhead":
            last_ovh_t1_rel = r
        if task in ("T2_detect", "T2_detect_overhead"):
            o = t5_origin.pop(jid, None)
            if o is not None:
                last_det_origin = o
                if o >= warm_ps:
                    det_lat.append((int(c["finish_ps"]) - o) / PS_PER_MS)
        elif task == "T4_vla":
            t4_done += 1
            o = t4_origin.pop(jid, None)
            if t4_done % t4_per_plan == 0:
                j5 = release("T5_plan", t_ps)
                if j5 >= 0:
                    t5_origin[j5] = o
        elif task == "T5_plan":
            o = t5_origin.pop(jid, None)
            if o is not None:
                pending_t7_chain.append(o)
        elif task == "T8_safety":
            o = t8_origin.pop(jid, None)
            if o is not None:
                pending_t7_chain.append(-o - 1)     # negative marks the safety chain
        elif task == "T7_control":
            f = int(c["finish_ps"])
            for o in pending_t7_chain:
                if o >= 0:
                    if o >= warm_ps:
                        chain_grasp.append((f - o) / PS_PER_MS)
                else:
                    oo = -o - 1
                    if oo >= warm_ps:
                        chain_safety.append((f - oo) / PS_PER_MS)
            pending_t7_chain.clear()

    def drain(t_ps: int) -> None:
        for _ in range(32):
            comps = sim.run_until(t_ps)
            if not comps:
                break
            comps = sorted(comps, key=lambda c: (c["finish_ps"], c["job_id"]))
            pm.observe_completions(comps)
            for c in comps:
                complete(c, t_ps)

    for k in range(n_steps):
        t_ps = k * PS_PER_MS
        drain(t_ps)
        for t in periodic:
            nr = next_rel[t]
            if t_ps >= nr:
                release(t, t_ps)
                while nr <= t_ps:
                    nr += period_ps[t]
                next_rel[t] = nr
        if burst_ms and (k % 1000) in burst_ms and k > 0:
            release("T5_plan", t_ps)
        drain(t_ps)
        pm.step(sim, t_ps)
    end_ps = n_steps * PS_PER_MS
    drain(end_ps)
    pm.step(sim, end_ps)
    # Jobs still pending at the end that are past their deadline count as misses.
    for j in sim.pending_jobs():
        task = str(j["task_id"])
        if int(j["release_ps"]) >= warm_ps and int(j["deadline_ps"]) <= end_ps and task in cnt:
            cnt[task] += 1
            miss[task] += 1
            resp[task].append((end_ps - int(j["release_ps"])) / PS_PER_MS)

    summ = pm.summary()
    out: dict[str, Any] = {}
    for t in tasks:
        p50, p95, p99 = _quantiles(resp[t])
        out[f"{t}.p50_ms"] = p50
        out[f"{t}.p95_ms"] = p95
        out[f"{t}.p99_ms"] = p99
        n = cnt[t] + skipped[t]
        # Skipped releases (on_overrun: skip) are deadline failures for the skipped frame.
        out[f"{t}.miss_rate"] = (miss[t] + skipped[t]) / n if n else float("nan")
        out[f"{t}.jobs"] = n
    for name, arr in (("chain_camera_to_plan", chain_grasp), ("chain_human_to_control", chain_safety),
                      ("chain_camera_to_detection", det_lat)):
        p50, p95, p99 = _quantiles(arr)
        out[f"{name}.p50_ms"] = p50
        out[f"{name}.p95_ms"] = p95
        out[f"{name}.p99_ms"] = p99
        out[f"{name}.n"] = len(arr)
    safety_rates = [out[f"{t}.miss_rate"] for t in SAFETY_TASKS if f"{t}.miss_rate" in out]
    out["worst_safety_miss_rate"] = float(np.nanmax(safety_rates)) if safety_rates else float("nan")
    hi = [t for t in tasks if graphs[t].get("criticality") in ("high", "safety")]
    out["worst_rt_miss_rate"] = float(np.nanmax([out[f"{t}.miss_rate"] for t in hi]))
    out["avg_power_w"] = float(summ["avg_power_w"])
    out["peak_power_w"] = float(summ["peak_power_w"])
    out["energy_per_robot_hour_wh"] = float(summ["energy_per_robot_hour_wh"])
    out["peak_temp_c"] = float(summ["peak_temp_c"])
    out["time_throttled_s"] = float(summ["time_throttled_s"])
    out["hypothetical"] = bool(summ["hypothetical"])
    out["wall_s"] = time.perf_counter() - t_wall
    out["cpu_s"] = time.process_time() - t_cpu
    out["sim_s"] = duration_s
    return out


VX1_DEFAULT: dict[str, Any] = {
    "npu_tiles": 4, "npu_array": 64, "npu_sram_mb": 2.0, "cache_mb": 8, "cache_partition": 0, "qos": 0,
    "noc_bits": 256, "dram_gbps": 136.5, "cpu_cores": 8, "scheduler": "fifo", "power_policy": "nominal",
    "vmin_npu": 0.75, "vmin_cpu": 0.75, "vmin_simt": 0.75, "vmin_dsp": 0.75, "lv_arith_factor": 1.0,
    "transport": "copy", "scenario": "S1", "bitrate_gbps": 0.2, "ambient_c": 25.0,
}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=2.0)
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = dict(VX1_DEFAULT)
    for kv in a.set:
        k, v = kv.split("=")
        cfg[k] = type(VX1_DEFAULT[k])(v) if not isinstance(VX1_DEFAULT[k], bool) else bool(int(v))
    r = run_chip(cfg, duration_s=a.dur)
    print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}, indent=0))
