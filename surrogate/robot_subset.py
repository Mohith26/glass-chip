"""Co-simulated subset: robot success for a sample of design points.

The main dataset is chip-only (no physics). For the "robot success" outputs I run a smaller
Latin hypercube through the full closed loop (studies/runner.py: MuJoCo robot, glasschip_core
at the same DSE fidelity mix, PowerManager, the real scenario YAML with its events) and record
grasp success rate and pick rate next to the chip metrics. Recording bitrate is not a knob
here (each scenario keeps its own recording task), so the bitrate column is fixed at
0.2 Gbit/s, the S1 default.

This subset is small, so its conformal intervals are wide; that is the honest answer for a
20 second run with a handful of grasp attempts.

Run:  .venv/bin/python -m surrogate.robot_subset run --n 200 --workers 8
      .venv/bin/python -m surrogate.robot_subset fit
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surrogate.dataset import DATA, INPUTS, SCENARIOS, decode, latin_hypercube, to_cfg  # noqa: E402

RPARTS = DATA / "robot_parts"
ROBOT_DURATION_S = 20.0
ROBOT_OUT = ["grasp_success_rate", "pick_rate"]
SCEN_FILES = {"S1": "S1", "S2": "S2", "S3": "S3", "S6": "S6"}
POLICY_ID = {"nominal": "P0", "race_to_idle": "P3", "pace_to_deadline": "P4"}


def _run_one(args: tuple[int, dict[str, float]]) -> dict[str, Any]:
    sid, num = args
    import yaml
    from studies.runner import run
    from surrogate.chiprun import soc_from_config
    num = dict(num)
    num["bitrate_gbps"] = 0.2
    cfg = to_cfg(num)
    soc = soc_from_config(cfg, yaml.safe_load((ROOT / "configs/soc/vx1.yaml").read_text()))
    fid = yaml.safe_load((Path(__file__).parent / "fidelity_dse.yaml").read_text())
    static = cfg["power_policy"] != "pace_to_deadline"
    vmin = {}
    for dom, key in (("npu", "vmin_npu"), ("cpu_a", "vmin_cpu"), ("cpu_b", "vmin_cpu"), ("simt", "vmin_simt"),
                     ("dsp", "vmin_dsp")):
        v = float(cfg[key])
        if not static or v < 0.75 - 1e-9:
            vmin[dom] = v
    lvf = float(cfg["lv_arith_factor"])
    ov: dict[str, Any] = {
        "policy": {"scheduler": cfg["scheduler"]},
        "power": {"vmin": vmin, "low_voltage_arith": {"enabled": lvf > 1.0 + 1e-9, "reduction_x": max(lvf, 1.0)},
                  "thermal_init": "settle"},
        "scenario": {"transport": cfg["transport"], "ambient_c": float(cfg["ambient_c"])},
        "qos": bool(cfg["qos"]),
        "cache_partition": {"safety": 2, "realtime": 10, "best_effort": 4} if cfg["cache_partition"] else None,
    }
    row: dict[str, Any] = {"sample_id": sid, "phase": "robot"}
    row.update({f"in.{k}": v for k, v in num.items()})
    t0 = time.time()
    try:
        r = run(SCEN_FILES[cfg["scenario"]], POLICY_ID[cfg["power_policy"]], fidelity_yaml=fid, soc_yaml=soc,
                seed=1 + sid % 1000, duration_s=ROBOT_DURATION_S, overrides=ov)
        rb = r.get("robot") or {}
        row["out.grasp_success_rate"] = rb.get("grasp_success_rate")
        off = rb.get("objects_offered") or 0
        row["out.pick_rate"] = (rb.get("placed", 0) / off) if off else None
        row["out.grasp_attempts"] = rb.get("grasp_attempts")
        row["out.oscillation_episodes"] = rb.get("oscillation_episodes")
        for t, d in (r.get("tasks") or {}).items():
            row[f"out.{t}.miss_rate"] = d.get("miss_rate")
            row[f"out.{t}.p99_ms"] = (d.get("response_ms") or {}).get("p99")
        pw = r.get("power") or {}
        row["out.energy_per_robot_hour_wh"] = pw.get("energy_per_robot_hour_wh")
        row["out.peak_temp_c"] = pw.get("peak_temp_c")
        row["robot_json"] = json.dumps(rb, default=str)[:4000]
        row["provenance"] = json.dumps(r.get("provenance", {}), default=str)[:4000]
        row["ok"] = True
        row["error"] = ""
    except Exception as e:
        row["ok"] = False
        row["error"] = f"{type(e).__name__}: {e}"[:500]
    row["wall_s"] = time.time() - t0
    return row


def run_subset(n: int, workers: int, seed: int = 99) -> None:
    import multiprocessing as mp
    rng = np.random.default_rng(seed)
    u = latin_hypercube(n, len(INPUTS), rng)
    pts = [(2_000_000 + i, decode(dict(zip(INPUTS, u[i])))) for i in range(n)]
    have: set[int] = set()
    for p in RPARTS.glob("*.parquet"):
        have.update(pd.read_parquet(p, columns=["sample_id"])["sample_id"].tolist())
    todo = [p for p in pts if p[0] not in have]
    print(f"robot subset: {len(pts)} planned, {len(todo)} to run", flush=True)
    RPARTS.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    with mp.get_context("spawn").Pool(workers, maxtasksperchild=20) as pool:
        for i, row in enumerate(pool.imap_unordered(_run_one, todo), 1):
            if (DATA / "STOP").exists():
                pool.terminate()
                break
            rows.append(row)
            if len(rows) >= 10:
                pd.DataFrame(rows).to_parquet(RPARTS / f"r-{int(time.time() * 1000)}.parquet", index=False)
                rows = []
                print(f"{i}/{len(todo)} {(time.time() - t0) / 60:.1f} min", flush=True)
    if rows:
        pd.DataFrame(rows).to_parquet(RPARTS / f"r-{int(time.time() * 1000)}.parquet", index=False)


def load_robot() -> pd.DataFrame:
    parts = sorted(RPARTS.glob("*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    return df.drop_duplicates("sample_id", keep="last").sort_values("sample_id").reset_index(drop=True)


def fit(alpha: float = 0.1) -> dict[str, Any]:
    """LightGBM (small) per robot output, split conformal on a held-out calibration set."""
    from surrogate.conformal import clopper_pearson, conformal_qhat
    from surrogate.train import fit_lgb, lgb_features
    df = load_robot()
    df = df[df["ok"]].reset_index(drop=True)
    X = df[[f"in.{k}" for k in INPUTS]].to_numpy(float)
    rng = np.random.default_rng(5)
    perm = rng.permutation(len(df))
    a, b = int(0.6 * len(df)), int(0.8 * len(df))
    tr, ca, te = perm[:a], perm[a:b], perm[b:]
    out: dict[str, Any] = {"n": len(df), "n_train": len(tr), "n_calib": len(ca), "n_test": len(te),
                           "duration_s": ROBOT_DURATION_S, "wall_s_mean": float(df["wall_s"].mean()),
                           "models": {}, "report": {}}
    for name in ROBOT_OUT:
        y = df[f"out.{name}"].to_numpy(float)
        ok = np.isfinite(y)
        tri, cai, tei = tr[ok[tr]], ca[ok[ca]], te[ok[te]]
        m = fit_lgb(lgb_features(X[tri]), y[tri], n_estimators=150,
                    params=dict(num_leaves=7, min_child_samples=8, learning_rate=0.05))
        s = np.abs(y[cai] - m.predict(lgb_features(X[cai])))
        q = {str(al): conformal_qhat(s, al) for al in (0.2, 0.1, 0.05)}
        pt = m.predict(lgb_features(X[tei]))
        lo, hi = np.clip(pt - q[str(alpha)], 0, 1), np.clip(pt + q[str(alpha)], 0, 1)
        inside = (y[tei] >= lo) & (y[tei] <= hi)
        k = int(inside.sum())
        base_mae = float(np.mean(np.abs(y[tei] - np.mean(y[tri]))))
        out["models"][name] = {"model": m, "qhat": q}
        out["report"][name] = {
            "coverage": k / len(tei) if len(tei) else None, "covered": k, "n_test": int(len(tei)),
            "ci95": list(clopper_pearson(k, len(tei))) if len(tei) else None, "nominal": 1 - alpha,
            "qhat": q, "test_mae": float(np.mean(np.abs(pt - y[tei]))), "baseline_mae_predict_mean": base_mae,
            "median_width": float(np.median(hi - lo)), "y_mean": float(np.nanmean(y)), "y_std": float(np.nanstd(y))}
    with open(DATA / "robot_bundle.pkl", "wb") as fh:
        pickle.dump(out, fh)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=200)
    r.add_argument("--workers", type=int, default=8)
    sub.add_parser("fit")
    a = ap.parse_args()
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if a.cmd == "run":
        run_subset(a.n, a.workers)
    else:
        res = fit()
        print(json.dumps(res["report"], indent=1, default=str))
