"""Train the Glass Chip surrogate (spec Section 10.3).

Per output I train two candidates and keep the one with the lower validation error:
  * LightGBM gradient-boosted trees on the raw design knobs plus one derived feature
    (peak NPU MACs per cycle = tiles x array^2);
  * a small MLP (2 hidden layers of 64, ReLU) on knobs scaled to [0, 1] with one-hot
    categorical knobs.
Latency outputs are modeled as ln(ms + 0.01) and miss rates as ln(rate + 0.001), so errors are
relative; power, energy and temperature are modeled in their own units. For latency tails
(p95, p99 and chain quantiles) I also train LightGBM quantile models at 5 percent and 95 percent
for conformalized quantile regression (conformal.py). The area proxy is closed form
(dataset.area_proxy) and is not learned.

Splits (fixed seed): the Latin-hypercube samples are permuted and split 60/10/15/15 into
train / validation / calibration / test. Active-learning samples join the training split only.
After model selection on validation, the chosen model is refit on train + validation.

Run:  .venv/bin/python -m surrogate.train
"""
from __future__ import annotations

import json
import math
import pickle
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*eval_set.*")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surrogate.dataset import (CHAINS, DATA, INPUTS, NOC_BITS, POWER_POLICIES, SCALAR_OUT, SCENARIOS,  # noqa: E402
                               SCHEDULERS, SPACE, TASKS_OUT, DURATION_S, WARMUP_S, load_dataset)

BUNDLE_PATH = DATA / "bundle.pkl"
LAT_EPS = 0.01        # ms
RATE_EPS = 0.001
SPLIT_SEED = 1234
CATEGORICAL = {"scheduler": len(SCHEDULERS), "power_policy": len(POWER_POLICIES), "scenario": len(SCENARIOS),
               "noc_bits": len(NOC_BITS)}
# Which scenarios run each optional task (others run everywhere).
TASK_SCENARIOS = {"T2_detect_overhead": {"S2", "S6"}, "T12_arm2_control": {"S6"}}
LGB_PARAMS = dict(learning_rate=0.08, num_leaves=31, min_child_samples=10, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.9, reg_lambda=1.0, n_estimators=800, verbose=-1, n_jobs=4)


def output_kind(name: str) -> str:
    if name.endswith("_ms"):
        return "latency"
    if name.endswith("miss_rate"):
        return "rate"
    return "linear"


def is_tail(name: str) -> bool:
    return name.endswith(".p95_ms") or name.endswith(".p99_ms") or (name.startswith("chain_") and name.endswith("_ms"))


def fwd(name: str, y: np.ndarray) -> np.ndarray:
    k = output_kind(name)
    if k == "latency":
        return np.log(np.maximum(y, 0) + LAT_EPS)
    if k == "rate":
        return np.log(np.clip(y, 0, 1) + RATE_EPS)
    return y


def inv(name: str, z: np.ndarray) -> np.ndarray:
    k = output_kind(name)
    if k == "latency":
        return np.maximum(np.exp(z) - LAT_EPS, 0.0)
    if k == "rate":
        return np.clip(np.exp(z) - RATE_EPS, 0.0, 1.0)
    return z


def modeled_outputs() -> list[str]:
    out = []
    for t in TASKS_OUT + CHAINS:
        out += [f"{t}.p50_ms", f"{t}.p95_ms", f"{t}.p99_ms"]
    out += [f"{t}.miss_rate" for t in TASKS_OUT]
    out += SCALAR_OUT
    return out


# --------------------------------------------------------------------------- features
def lgb_features(X: np.ndarray) -> np.ndarray:
    """Raw knobs (INPUTS order) plus peak NPU MACs per cycle (tiles x array^2)."""
    i_t, i_a = INPUTS.index("npu_tiles"), INPUTS.index("npu_array")
    return np.column_stack([X, X[:, i_t] * X[:, i_a] ** 2])


def mlp_features(X: np.ndarray) -> np.ndarray:
    """Knobs scaled to [0, 1] (log scale for bitrate), one-hot for categorical knobs."""
    cols = []
    for j, k in enumerate(INPUTS):
        kind, lo, hi, _, _ = SPACE[k]
        x = X[:, j]
        if k in CATEGORICAL:
            for c in range(CATEGORICAL[k]):
                cols.append((np.round(x) == c).astype(float))
        elif kind == "logfloat":
            cols.append((np.log(x) - math.log(lo)) / (math.log(hi) - math.log(lo)))
        else:
            cols.append((x - lo) / (hi - lo))
    i_t, i_a = INPUTS.index("npu_tiles"), INPUTS.index("npu_array")
    cols.append(X[:, i_t] * X[:, i_a] ** 2 / (8 * 128 ** 2))
    return np.column_stack(cols)


# --------------------------------------------------------------------------- data prep
def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Clean targets: drop failed runs, censor missing values where the task did run.

    A latency of NaN for a task that is part of the scenario means no job finished in the
    measured window (for example T5 when T4 never completes). I censor it at the window
    length and set its miss rate to 1. Outputs for tasks that are not part of a scenario stay NaN
    and those rows are excluded when training that output.
    """
    df = df[df["ok"]].copy()
    window_ms = (DURATION_S - WARMUP_S) * 1000.0
    scen = df["in.scenario"].round().astype(int).map(lambda i: SCENARIOS[i])
    df["scenario_name"] = scen
    for t in TASKS_OUT + CHAINS:
        present = np.ones(len(df), bool) if t not in TASK_SCENARIOS else scen.isin(TASK_SCENARIOS[t]).to_numpy()
        for q in ("p50_ms", "p95_ms", "p99_ms"):
            c = f"out.{t}.{q}"
            if c not in df:
                df[c] = np.nan
            v = df[c].to_numpy(float)
            v = np.where(present & ~np.isfinite(v), window_ms, v)
            v = np.where(present, v, np.nan)
            df[c] = v
        if t in TASKS_OUT:
            c = f"out.{t}.miss_rate"
            if c not in df:
                df[c] = np.nan
            v = df[c].to_numpy(float)
            v = np.where(present & ~np.isfinite(v), 1.0, v)
            df[c] = np.where(present, v, np.nan)
    # worst rates recomputed after censoring
    safety = [f"out.{t}.miss_rate" for t in ("T7_control", "T8_safety", "T12_arm2_control")]
    df["out.worst_safety_miss_rate"] = df[safety].max(axis=1, skipna=True)
    return df


def split(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """Index arrays for train / val / calib / test (LHS only for val/calib/test)."""
    rng = np.random.default_rng(SPLIT_SEED)
    lhs = df.index[df["phase"] == "lhs"].to_numpy()
    lhs = lhs[rng.permutation(len(lhs))]
    n = len(lhs)
    a, b, c = int(0.6 * n), int(0.7 * n), int(0.85 * n)
    al = df.index[df["phase"] != "lhs"].to_numpy()
    return {"train": np.concatenate([lhs[:a], al]), "val": lhs[a:b], "calib": lhs[b:c], "test": lhs[c:]}


# --------------------------------------------------------------------------- models
@dataclass
class MLPModel:
    """Small MLP stored as plain numpy weights (exportable to ONNX by hand)."""
    weights: list[np.ndarray]
    biases: list[np.ndarray]
    y_mean: float
    y_std: float

    def predict(self, F: np.ndarray) -> np.ndarray:
        h = F
        for i, (W, b) in enumerate(zip(self.weights, self.biases)):
            h = h @ W + b
            if i < len(self.weights) - 1:
                h = np.maximum(h, 0)
        return h[:, 0] * self.y_std + self.y_mean


def fit_mlp(F: np.ndarray, z: np.ndarray, seed: int = 0) -> MLPModel:
    from sklearn.neural_network import MLPRegressor
    mu, sd = float(z.mean()), float(z.std() + 1e-9)
    m = MLPRegressor(hidden_layer_sizes=(64, 64), activation="relu", alpha=1e-4, learning_rate_init=2e-3,
                     max_iter=600, early_stopping=True, validation_fraction=0.1, n_iter_no_change=30,
                     random_state=seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.fit(F, (z - mu) / sd)
    return MLPModel([w.astype(np.float32) for w in m.coefs_], [b.astype(np.float32) for b in m.intercepts_], mu, sd)


def fit_lgb(F: np.ndarray, z: np.ndarray, Fv: np.ndarray | None = None, zv: np.ndarray | None = None,
            objective: str = "regression", alpha: float | None = None, n_estimators: int | None = None,
            params: dict[str, Any] | None = None) -> Any:
    import lightgbm as lgb
    p = dict(LGB_PARAMS)
    if params:
        p.update(params)
    if n_estimators is not None:
        p["n_estimators"] = n_estimators
    if objective == "quantile":
        p.update(objective="quantile", alpha=alpha)
    m = lgb.LGBMRegressor(**p)
    if Fv is not None:
        m.fit(F, z, eval_set=[(Fv, zv)], callbacks=[lgb.early_stopping(50, verbose=False)])
    else:
        m.fit(F, z)
    return m


@dataclass
class OutputModel:
    name: str
    kind: str                       # latency | rate | linear
    chosen: str                     # lgb | mlp
    point: Any                      # LGBMRegressor or MLPModel (refit on train+val)
    val_mae_z: dict[str, float]     # validation MAE in model space for each candidate
    val_mae: dict[str, float]       # validation MAE in output units
    q_lo: Any = None                # LightGBM 5 percent quantile model (tails only)
    q_hi: Any = None
    n_train: int = 0
    conformal: dict[str, Any] = field(default_factory=dict)   # filled by conformal.py

    def predict_z(self, X: np.ndarray) -> np.ndarray:
        if self.chosen == "mlp":
            return self.point.predict(mlp_features(X))
        return self.point.predict(lgb_features(X))


def train_output(name: str, df: pd.DataFrame, idx: dict[str, np.ndarray]) -> OutputModel | None:
    col = f"out.{name}"
    X = df[[f"in.{k}" for k in INPUTS]].to_numpy(float)
    y = df[col].to_numpy(float)
    ok = np.isfinite(y)
    tr = idx["train"][ok[idx["train"]]]
    va = idx["val"][ok[idx["val"]]]
    if len(tr) < 50 or len(va) < 10:
        return None
    z = fwd(name, y)
    Ftr, Fva = lgb_features(X[tr]), lgb_features(X[va])
    Mtr, Mva = mlp_features(X[tr]), mlp_features(X[va])
    lg = fit_lgb(Ftr, z[tr], Fva, z[va])
    best_iter = int(lg.best_iteration_ or lg.n_estimators)
    pred_l = lg.predict(Fva)
    ml = fit_mlp(Mtr, z[tr])
    pred_m = ml.predict(Mva)
    mae_z = {"lgb": float(np.mean(np.abs(pred_l - z[va]))), "mlp": float(np.mean(np.abs(pred_m - z[va])))}
    mae = {"lgb": float(np.mean(np.abs(inv(name, pred_l) - y[va]))), "mlp": float(np.mean(np.abs(inv(name, pred_m) - y[va])))}
    chosen = "lgb" if mae_z["lgb"] <= mae_z["mlp"] else "mlp"
    trva = np.concatenate([tr, va])
    if chosen == "lgb":
        point = fit_lgb(lgb_features(X[trva]), z[trva], n_estimators=max(best_iter, 50))
    else:
        point = fit_mlp(mlp_features(X[trva]), z[trva])
    om = OutputModel(name, output_kind(name), chosen, point, mae_z, mae, n_train=len(trva))
    if is_tail(name):
        F = lgb_features(X[trva])
        # Quantile models: smaller trees and at most 300 rounds (they only shape the CQR band,
        # and they dominate the size of the browser model).
        nq = min(max(best_iter, 100), 300)
        qp = dict(num_leaves=15)
        om.q_lo = fit_lgb(F, z[trva], objective="quantile", alpha=0.05, n_estimators=nq, params=qp)
        om.q_hi = fit_lgb(F, z[trva], objective="quantile", alpha=0.95, n_estimators=nq, params=qp)
    return om


_SHARED: dict[str, Any] = {}


def _init_worker(df: pd.DataFrame, idx: dict[str, np.ndarray]) -> None:
    _SHARED["df"], _SHARED["idx"] = df, idx
    LGB_PARAMS["n_jobs"] = 1


def _train_worker(name: str) -> tuple[str, OutputModel | None]:
    return name, train_output(name, _SHARED["df"], _SHARED["idx"])


def train_all(df: pd.DataFrame | None = None, outputs: list[str] | None = None, workers: int = 4) -> dict[str, Any]:
    df = prepare(load_dataset() if df is None else df)
    df = df.reset_index(drop=True)
    idx = split(df)
    models: dict[str, OutputModel] = {}
    t0 = time.time()
    names = outputs or modeled_outputs()
    import multiprocessing as mp
    with mp.get_context("spawn").Pool(workers, initializer=_init_worker, initargs=(df, idx)) as pool:
        for name, om in pool.imap_unordered(_train_worker, names):
            if om is not None:
                models[name] = om
                print(f"{name:42s} {om.chosen}  val MAE lgb {om.val_mae['lgb']:.4g}  mlp {om.val_mae['mlp']:.4g}", flush=True)
    models = {n: models[n] for n in names if n in models}
    return {"models": models, "split": {k: v.tolist() for k, v in idx.items()}, "train_s": time.time() - t0,
            "n_rows": len(df), "sample_ids": df["sample_id"].tolist()}


def main() -> None:
    from surrogate.conformal import calibrate_bundle
    res = train_all()
    bundle = calibrate_bundle(res)
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BUNDLE_PATH, "wb") as fh:
        pickle.dump(bundle, fh)
    print(f"saved {BUNDLE_PATH} ({BUNDLE_PATH.stat().st_size / 1e6:.1f} MB), trained in {res['train_s']:.0f} s")


if __name__ == "__main__":
    # Run through the importable module so pickled classes resolve as surrogate.train.*.
    import surrogate.train as _train
    _train.main()
