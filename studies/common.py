"""Shared helpers for the studies: parallel runs, confidence intervals, provenance, figure style."""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
import time
import traceback
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

RESULTS = ROOT / "results"
FIGS = RESULTS / "figures"
TASK_ORDER = ["T1_ingest_wrist", "T1_ingest_overhead", "T2_detect", "T2_detect_overhead", "T3_esdf", "T4_vla",
              "T5_plan", "T6_estimate", "T7_control", "T8_safety", "T10_record", "T10_record_4k", "T11_speech",
              "T12_arm2_control", "T13_map_save", "T14_ota", "T15_diag"]
SAFETY = {"T7_control", "T8_safety", "T12_arm2_control"}


def default_workers() -> int:
    """Leave cores for the other workers sharing this machine."""
    return int(os.environ.get("GC_WORKERS", max(1, min(8, (os.cpu_count() or 4) - 6))))


def _call(job: Mapping[str, Any]) -> dict[str, Any]:
    from studies.runner import run
    kw = dict(job.get("kwargs", {}))
    t0 = time.perf_counter()
    try:
        r = run(**kw)
        r["_job"] = {k: v for k, v in job.items() if k != "kwargs"}
        r["_ok"] = True
        return r
    except Exception as e:  # keep the sweep going; report the failure
        return {"_job": {k: v for k, v in job.items() if k != "kwargs"}, "_ok": False, "error": repr(e),
                "traceback": traceback.format_exc()[-2000:], "wall_s": time.perf_counter() - t0}


def run_many(jobs: list[dict[str, Any]], workers: int | None = None, slim: Callable[[dict], dict] | None = None,
             log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Run jobs ({"kwargs": runner.run kwargs, ...labels}) in a process pool, in order."""
    workers = workers or default_workers()
    out: list[dict[str, Any] | None] = [None] * len(jobs)
    t0 = time.perf_counter()
    with Pool(workers) as pool:
        for i, r in pool.imap_unordered(_indexed, list(enumerate(jobs))):
            if slim is not None and r.get("_ok"):
                r = slim(r)
            out[i] = r
            done = sum(x is not None for x in out)
            status = "ok" if r.get("_ok") else f"FAILED {r.get('error')}"
            log(f"[{done}/{len(jobs)}] {r['_job']} {status} ({time.perf_counter() - t0:.0f} s)")
    return out  # type: ignore[return-value]


def _indexed(ij):
    i, j = ij
    return i, _call(j)


def slim_result(r: dict[str, Any], keep_missed: bool = False) -> dict[str, Any]:
    """Drop bulky fields before storing a run in a results JSON."""
    r = dict(r)
    if not keep_missed:
        r.pop("missed_jobs", None)
    r.pop("jobs", None)
    act = dict(r.get("activity") or {})
    act.pop("by_domain", None)
    r["activity"] = act
    return r


def mean_ci(x: Iterable[float], conf: float = 0.95) -> dict[str, float | int | None]:
    """Mean and a t-based confidence interval over seeds."""
    a = np.asarray([v for v in x if v is not None and np.isfinite(v)], dtype=float)
    n = len(a)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    m = float(a.mean())
    if n == 1:
        return {"n": 1, "mean": m, "lo": m, "hi": m}
    from scipy import stats
    h = float(stats.t.ppf(0.5 + conf / 2, n - 1) * a.std(ddof=1) / math.sqrt(n))
    return {"n": n, "mean": m, "lo": m - h, "hi": m + h}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def provenance(script: str, configs: list[str], seeds: list[int], extra: Mapping[str, Any] | None = None,
               runs: list[dict] | None = None) -> dict[str, Any]:
    from studies.runner import core_build_id, git_sha
    p = {"script": script, "git_sha": git_sha(), "date": _dt.date.today().isoformat(),
         "config_files": configs, "seeds": seeds, "core_build": core_build_id(),
         "fidelity": "configs/fidelity/interactive.yaml", "soc": "configs/soc/vx1.yaml",
         "behavior_source": "scripted expert in hybrid mode; VLA compute modeled from its operator graph"}
    if runs:
        walls = [r.get("wall_s") for r in runs if r.get("wall_s")]
        sims = [r.get("duration_s") for r in runs if r.get("duration_s")]
        p["compute"] = {"runs": len(runs), "failed": sum(1 for r in runs if not r.get("_ok", True)),
                        "wall_s_total": float(sum(walls)) if walls else None,
                        "sim_s_total": float(sum(sims)) if sims else None}
    if extra:
        p.update(extra)
    return p


def write_json(name: str, data: Mapping[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{name}.json"
    p.write_text(json.dumps(data, indent=1, default=_default))
    return p


def _default(o: Any) -> Any:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25,
                         "axes.titleweight": "bold", "axes.titlesize": 11, "legend.frameon": False})
    return plt


def savefig(fig, name: str, note: str | None = None) -> list[str]:
    FIGS.mkdir(parents=True, exist_ok=True)
    if note:
        fig.text(0.005, -0.02, note, fontsize=8, color="#333333", ha="left", va="top")
    out = []
    for ext in ("svg", "png"):
        p = FIGS / f"{name}.{ext}"
        fig.savefig(p, dpi=160, bbox_inches="tight", pad_inches=0.15)
        out.append(str(p.relative_to(ROOT)))
    return out


TASK_COLORS = {"T1": "#6C8EBF", "T2": "#2E86AB", "T3": "#8E7CC3", "T4": "#D1495B", "T5": "#F0A202",
               "T6": "#3BB273", "T7": "#222222", "T8": "#E07A5F", "T10": "#9AA5B1", "T11": "#B5838D",
               "T12": "#555555", "T13": "#A3B18A", "T14": "#C9ADA7", "T15": "#7F8C8D"}


def task_color(t: str) -> str:
    return TASK_COLORS.get(t.split("_")[0], "#888888")
