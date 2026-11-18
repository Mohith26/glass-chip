"""Conformal intervals for the Glass Chip surrogate (spec Section 10.3).

Split conformal prediction
--------------------------
For a point model f trained without the calibration set, the score on calibration point i is
s_i = |z_i - f(x_i)| in model space (log space for latencies and miss rates). With n
calibration points and miscoverage alpha, q_hat is the ceil((n + 1)(1 - alpha))-th smallest
score, and the interval for a new x is [f(x) - q_hat, f(x) + q_hat], mapped back to output
units. If calibration and test points are exchangeable, P(y in interval) >= 1 - alpha
(marginal over x and over the calibration draw), and <= 1 - alpha + 1/(n + 1) without ties.

Conformalized quantile regression (Romano, Patterson, Candes, NeurIPS 2019)
---------------------------------------------------------------------------
For latency tails I use LightGBM quantile models q_lo (5 percent) and q_hi (95 percent). The
score is s_i = max(q_lo(x_i) - z_i, z_i - q_hi(x_i)) and the interval is
[q_lo(x) - q_hat, q_hi(x) + q_hat]. Same guarantee, but the width adapts to x.

What this does not guarantee: coverage conditional on a particular x or region, or coverage
for designs outside the sampled space or for a changed simulator (distribution shift).

Fallback rule (UI and P6): an interval is "uncertain, run full model" when its width exceeds
FALLBACK_REL x deadline for a latency output with a deadline, or FALLBACK_RATE in absolute terms
for a miss rate.
"""
from __future__ import annotations

import math
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALPHAS = (0.2, 0.1, 0.05, 0.01)
DEFAULT_ALPHA = 0.1
FALLBACK_REL = 0.5        # interval width / deadline above which the surrogate defers
FALLBACK_RATE = 0.05      # absolute miss-rate interval width above which the surrogate defers

# Deadlines (ms) per task from workloads/graphs, and chain budgets (assumptions).
DEADLINES_MS = {"T1_ingest_wrist": 15.0, "T1_ingest_overhead": 15.0, "T2_detect": 33.0, "T2_detect_overhead": 33.0,
                "T3_esdf": 66.0, "T4_vla": 100.0, "T5_plan": 50.0, "T6_estimate": 5.0, "T7_control": 1.0,
                "T8_safety": 10.0, "T10_record_4k": 33.333, "T12_arm2_control": 1.0,
                # chain budgets: assumptions. camera_to_plan uses the s14 tolerance at 0.3 m/s (250 ms).
                "chain_camera_to_plan": 250.0, "chain_human_to_control": 50.0, "chain_camera_to_detection": 33.0}


def conformal_qhat(scores: np.ndarray, alpha: float) -> float:
    """Split-conformal quantile: the ceil((n+1)(1-alpha))-th smallest score (inf if n too small)."""
    s = np.sort(np.asarray(scores, float))
    n = len(s)
    k = int(math.ceil((n + 1) * (1 - alpha)))
    if k > n:
        return float("inf")
    return float(s[k - 1])


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact binomial confidence interval for k successes out of n."""
    from scipy.stats import beta
    a = 1 - level
    lo = 0.0 if k == 0 else float(beta.ppf(a / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - a / 2, k + 1, n - k))
    return lo, hi


def task_of(name: str) -> str:
    return name.rsplit(".", 1)[0]


@dataclass
class Bundle:
    """Trained point/quantile models plus conformal q-hat per output and alpha."""
    models: dict[str, Any]
    split: dict[str, list[int]]
    sample_ids: list[int]
    report: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- prediction
    def predict(self, X: np.ndarray, names: Iterable[str] | None = None, alpha: float = DEFAULT_ALPHA
                ) -> dict[str, dict[str, np.ndarray]]:
        """{output: {point, lo, hi, uncertain}} in output units for rows of X (INPUTS order)."""
        from surrogate.train import inv, lgb_features
        X = np.atleast_2d(np.asarray(X, float))
        F = None
        out: dict[str, dict[str, np.ndarray]] = {}
        for name in names or self.models:
            m = self.models[name]
            z = m.predict_z(X)
            c = m.conformal
            q = c["qhat"][str(alpha)]
            if c["method"] == "cqr":
                if F is None:
                    F = lgb_features(X)
                lo_z = m.q_lo.predict(F) - q
                hi_z = m.q_hi.predict(F) + q
                lo_z = np.minimum(lo_z, z)
                hi_z = np.maximum(hi_z, z)
            else:
                lo_z, hi_z = z - q, z + q
            p, lo, hi = inv(name, z), inv(name, lo_z), inv(name, hi_z)
            out[name] = {"point": p, "lo": lo, "hi": hi, "uncertain": self.uncertain(name, lo, hi)}
        return out

    @staticmethod
    def uncertain(name: str, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        if name.endswith("miss_rate"):
            return (hi - lo) > FALLBACK_RATE
        if name.endswith("_ms"):
            d = DEADLINES_MS.get(task_of(name))
            if d:
                return (hi - lo) > FALLBACK_REL * d
        return np.zeros(len(lo), bool)

    def boundary_score(self, X: np.ndarray, alpha: float = DEFAULT_ALPHA) -> np.ndarray:
        """Active-learning score: largest normalized interval width among intervals that
        straddle a deadline (latency) or the 1 percent miss line (rates)."""
        score = np.zeros(len(X))
        names = [n for n in self.models if (n.endswith(".p99_ms") and task_of(n) in DEADLINES_MS) or n.endswith("miss_rate")]
        pr = self.predict(X, names, alpha)
        for n in names:
            lo, hi = pr[n]["lo"], pr[n]["hi"]
            if n.endswith("miss_rate"):
                s = np.where((lo <= 0.01) & (hi >= 0.01), (hi - lo) / 0.05, 0.0)
            else:
                d = DEADLINES_MS[task_of(n)]
                s = np.where((lo <= d) & (hi >= d), (hi - lo) / d, 0.0)
            score = np.maximum(score, s)
        return score


def calibrate_bundle(res: dict[str, Any]) -> Bundle:
    """Compute q-hat per output on the calibration split and coverage on the test split."""
    from surrogate.dataset import INPUTS, load_dataset
    from surrogate.train import fwd, inv, lgb_features, prepare, is_tail
    df = prepare(load_dataset()).reset_index(drop=True)
    assert df["sample_id"].tolist() == res["sample_ids"], "dataset changed between training and calibration"
    X = df[[f"in.{k}" for k in INPUTS]].to_numpy(float)
    cal = np.asarray(res["split"]["calib"])
    te = np.asarray(res["split"]["test"])
    report: dict[str, Any] = {}
    for name, m in res["models"].items():
        y = df[f"out.{name}"].to_numpy(float)
        z = fwd(name, y)
        c_idx = cal[np.isfinite(y[cal])]
        t_idx = te[np.isfinite(y[te])]
        zc = m.predict_z(X[c_idx])
        method = "cqr" if (is_tail(name) and m.q_lo is not None) else "split"
        if method == "cqr":
            Fc = lgb_features(X[c_idx])
            s = np.maximum(m.q_lo.predict(Fc) - z[c_idx], z[c_idx] - m.q_hi.predict(Fc))
        else:
            s = np.abs(z[c_idx] - zc)
        m.conformal = {"method": method, "n_calib": int(len(c_idx)),
                       "qhat": {str(a): conformal_qhat(s, a) for a in ALPHAS}}
        b = Bundle({name: m}, res["split"], res["sample_ids"])
        rep: dict[str, Any] = {"method": method, "chosen": m.chosen, "n_calib": len(c_idx), "n_test": len(t_idx),
                               "qhat": m.conformal["qhat"], "val_mae": m.val_mae, "val_mae_model_space": m.val_mae_z}
        if len(t_idx):
            for a in ALPHAS:
                pr = b.predict(X[t_idx], [name], a)[name]
                inside = (y[t_idx] >= pr["lo"] - 1e-12) & (y[t_idx] <= pr["hi"] + 1e-12)
                k = int(inside.sum())
                lo_ci, hi_ci = clopper_pearson(k, len(t_idx))
                width = pr["hi"] - pr["lo"]
                rep[f"alpha_{a}"] = {"coverage": k / len(t_idx), "covered": k, "n": len(t_idx),
                                     "ci95": [lo_ci, hi_ci], "nominal": 1 - a,
                                     "nominal_inside_ci": bool(lo_ci <= 1 - a <= hi_ci),
                                     "median_width": float(np.median(width)), "mean_width": float(np.mean(width)),
                                     "frac_uncertain": float(np.mean(pr["uncertain"]))}
            pr = b.predict(X[t_idx], [name], DEFAULT_ALPHA)[name]
            err = pr["point"] - y[t_idx]
            denom = np.maximum(np.abs(y[t_idx]), 1e-9)
            rep["test_mae"] = float(np.mean(np.abs(err)))
            rep["test_median_abs_err"] = float(np.median(np.abs(err)))
            if name.endswith("_ms"):
                rep["test_median_rel_err"] = float(np.median(np.abs(err) / denom))
                rep["test_mape"] = float(np.mean(np.abs(err) / denom))
            elif not name.endswith("miss_rate"):
                rep["test_mape"] = float(np.mean(np.abs(err) / denom))
            ss_res = float(np.sum((z[t_idx] - b.models[name].predict_z(X[t_idx])) ** 2))
            ss_tot = float(np.sum((z[t_idx] - z[t_idx].mean()) ** 2))
            rep["test_r2_model_space"] = 1 - ss_res / ss_tot if ss_tot > 0 else None
        report[name] = rep
    return Bundle(res["models"], res["split"], res["sample_ids"], report)


def load_bundle(path: str | Path | None = None) -> Bundle:
    from surrogate.train import BUNDLE_PATH
    with open(path or BUNDLE_PATH, "rb") as fh:
        return pickle.load(fh)
