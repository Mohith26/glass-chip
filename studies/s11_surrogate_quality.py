"""s11: surrogate quality (spec Section 12).

Question: how accurate is the learned surrogate, how wide are its conformal intervals, does the
empirical coverage on held-out data match the nominal level, and how much faster is it than
the full model?

Reads the trained bundle (data/surrogate/bundle.pkl, from surrogate/train.py), the dataset
(data/surrogate/parts), the robot co-sim subset (data/surrogate/robot_bundle.pkl) and the
exported ONNX model. Also runs two small checks that it caches in data/surrogate/:
  * fidelity check: 40 test-split designs re-run at the all-L1 `study` mix, compared with the
    interactive-mix truth the surrogate was trained on;
  * co-sim shift: the chip-only surrogate's coverage on chip metrics measured inside the
    closed-loop co-sim runs (a different release process), to show what distribution shift does.
Writes results/s11.json and results/figures/s11_*.{png,svg}.
"""
from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surrogate.conformal import (ALPHAS, DEADLINES_MS, clopper_pearson, load_bundle, task_of)  # noqa: E402
from surrogate.dataset import DATA, DURATION_S, WARMUP_S, INPUTS, load_dataset, provenance, to_cfg  # noqa: E402
from surrogate.train import BUNDLE_PATH, prepare  # noqa: E402

FIG = ROOT / "results" / "figures"
ONNX_PATH = ROOT / "viz" / "public" / "surrogate" / "surrogate.onnx"
FID_CACHE = DATA / "fidelity_check.parquet"
HEADLINE = ["energy_per_robot_hour_wh", "avg_power_w", "peak_temp_c", "worst_safety_miss_rate", "T4_vla.p99_ms",
            "T4_vla.miss_rate", "T2_detect.p99_ms", "T8_safety.p99_ms", "chain_camera_to_plan.p99_ms"]


def _fid_one(args: tuple[int, dict[str, float]]) -> dict[str, Any]:
    import yaml
    from surrogate.chiprun import run_chip
    sid, num = args
    fid = yaml.safe_load((ROOT / "configs" / "fidelity" / "study.yaml").read_text())
    fid["npu"] = "L1"     # SCALE-Sim tables only exist for the 64x64 array
    r = run_chip(to_cfg(num), seed=1 + sid % 1000, duration_s=DURATION_S, warmup_s=WARMUP_S, fidelity=fid)
    return {"sample_id": sid, **{f"study.{k}": v for k, v in r.items()}}


def fidelity_check(df: pd.DataFrame, test_idx: np.ndarray, n: int = 40, workers: int = 4) -> pd.DataFrame:
    if FID_CACHE.exists():
        return pd.read_parquet(FID_CACHE)
    import multiprocessing as mp
    rng = np.random.default_rng(3)
    pick = rng.choice(test_idx, size=min(n, len(test_idx)), replace=False)
    pts = [(int(df.loc[i, "sample_id"]), {k: float(df.loc[i, f"in.{k}"]) for k in INPUTS}) for i in pick]
    with mp.get_context("spawn").Pool(workers) as pool:
        rows = pool.map(_fid_one, pts)
    out = pd.DataFrame(rows)
    out.to_parquet(FID_CACHE, index=False)
    return out


RUNLEN_CACHE = DATA / "runlength_check.json"


def _runlen_one(args: tuple[str, float]) -> dict[str, Any]:
    from surrogate.chiprun import VX1_DEFAULT, run_chip
    pol, d = args
    c = dict(VX1_DEFAULT, power_policy=pol, scheduler="edf", vmin_npu=0.45, vmin_cpu=0.5, vmin_simt=0.5, vmin_dsp=0.5)
    if pol != "pace_to_deadline":
        c.update(vmin_npu=0.75, vmin_cpu=0.75, vmin_simt=0.75, vmin_dsp=0.75)   # static policies at nominal
    r = run_chip(c, seed=1, duration_s=d, warmup_s=1.0)
    return {"policy": pol, "duration_s": d, "energy_per_robot_hour_wh": r["energy_per_robot_hour_wh"],
            "worst_safety_miss_rate": r["worst_safety_miss_rate"], "T4_vla.p99_ms": r["T4_vla.p99_ms"]}


def runlength_check(workers: int = 4) -> dict[str, Any]:
    """Does a 2 s run (1 s burn-in) give the same energy as a 6 s run? VX-1 default, S1, EDF.

    P4 gets its Vmin floors at the table minimums so it can actually scale; P0 and P3 run at
    nominal. This checks that the burn-in hides P4's warm-up.
    """
    if RUNLEN_CACHE.exists():
        return json.loads(RUNLEN_CACHE.read_text())
    import multiprocessing as mp
    jobs = [(p, d) for p in ("nominal", "race_to_idle", "pace_to_deadline") for d in (DURATION_S, 6.0)]
    with mp.get_context("spawn").Pool(workers) as pool:
        rows = pool.map(_runlen_one, jobs)
    out: dict[str, Any] = {"rows": rows, "rel_diff_energy_2s_vs_6s": {}}
    for p in ("nominal", "race_to_idle", "pace_to_deadline"):
        a = [r for r in rows if r["policy"] == p]
        e2 = next(r for r in a if r["duration_s"] == DURATION_S)["energy_per_robot_hour_wh"]
        e6 = next(r for r in a if r["duration_s"] == 6.0)["energy_per_robot_hour_wh"]
        out["rel_diff_energy_2s_vs_6s"][p] = (e2 - e6) / e6
    RUNLEN_CACHE.write_text(json.dumps(out))
    return out


def node_ortweb_latency() -> dict[str, Any] | None:
    """Time the exported model with onnxruntime-web (wasm backend) under Node."""
    script = ROOT / "surrogate" / "bench_ortweb.mjs"
    if not script.exists() or not ONNX_PATH.exists():
        return None
    try:
        r = subprocess.run(["node", str(script), str(ONNX_PATH), str(len(INPUTS))], capture_output=True, text=True,
                           timeout=120, cwd=str(ROOT / "viz"))
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:  # pragma: no cover
        return {"error": str(e)}


def main() -> None:
    t_start = time.time()
    bundle = load_bundle()
    df = prepare(load_dataset()).reset_index(drop=True)
    raw = load_dataset()
    X = df[[f"in.{k}" for k in INPUTS]].to_numpy(float)
    split = {k: np.asarray(v) for k, v in bundle.split.items()}
    rep = bundle.report

    # ------------------------------------------------------------------ dataset
    fin = raw["finished_utc"].to_numpy(float)
    gen = {}
    for ph, g in raw.groupby(raw["phase"].str.replace(r"\d+$", "", regex=True)):
        f = g["finished_utc"].to_numpy(float)
        gen[ph] = {"n": int(len(g)), "cpu_hours": float(g["out.cpu_s"].sum() / 3600),
                   "first_to_last_finish_min": float((f.max() - f.min()) / 60)}
    dataset = {
        "n_total": int(len(raw)), "n_ok": int(raw["ok"].sum()), "n_failed": int((~raw["ok"]).sum()),
        "n_lhs": int((raw["phase"] == "lhs").sum()), "n_active_learning": int((raw["phase"] != "lhs").sum()),
        "split_sizes": {k: int(len(v)) for k, v in split.items()},
        "sim_seconds_per_sample": DURATION_S, "warmup_s": WARMUP_S, "thermal_init": "settle", "fidelity": "surrogate/fidelity_dse.yaml (interactive mix)",
        "sample_wall_s_median": float(raw["out.wall_s"].median()), "sample_cpu_s_median": float(raw["out.cpu_s"].median()),
        "sample_wall_s_p95": float(raw["out.wall_s"].quantile(0.95)),
        "total_cpu_hours": float(raw["out.cpu_s"].sum() / 3600), "by_phase": gen,
        "wall_clock_note": "first_to_last_finish_min is the elapsed time of each generation phase on a shared machine "
                           "(10 worker processes, other jobs running)",
        "n_inputs": len(INPUTS), "inputs": INPUTS,
    }

    # ------------------------------------------------------------------ per-output quality
    outputs = {}
    cov90 = []
    for name, r in rep.items():
        a = r.get("alpha_0.1")
        if not a:
            continue
        o = {"chosen_model": r["chosen"], "interval_method": r["method"], "n_calib": r["n_calib"], "n_test": r["n_test"],
             "val_mae": r["val_mae"], "test_mae": r.get("test_mae"), "test_median_abs_err": r.get("test_median_abs_err"),
             "test_median_rel_err": r.get("test_median_rel_err"), "test_mape": r.get("test_mape"),
             "test_r2_model_space": r.get("test_r2_model_space"), "coverage": {}}
        for al in ALPHAS:
            o["coverage"][str(1 - al)] = r[f"alpha_{al}"]
        if name.endswith("_ms") and task_of(name) in DEADLINES_MS:
            o["median_width_over_deadline_90"] = a["median_width"] / DEADLINES_MS[task_of(name)]
        if np.nanstd(df[f"out.{name}"].to_numpy(float)) == 0:
            o["degenerate"] = "constant in the data; excluded from coverage statistics"
        outputs[name] = o
        if "degenerate" not in o:
            cov90.append((name, a["coverage"], a["ci95"][0], a["ci95"][1], a["n"]))
    n_out = len(cov90)
    inside = sum(1 for _, c, lo, hi, _ in cov90 if lo <= 0.9 <= hi)
    below = [n for n, c, lo, hi, _ in cov90 if hi < 0.9]
    above = [n for n, c, lo, hi, _ in cov90 if lo > 0.9]
    covs = np.array([c for _, c, _, _, _ in cov90])
    lat_w = [o["median_width_over_deadline_90"] for o in outputs.values() if "median_width_over_deadline_90" in o]
    from scipy.stats import binom
    n_cal_typ = int(np.median([rep[n]["n_calib"] for n, *_ in cov90]))
    n_test_typ = int(np.median([n for *_, n in cov90]))
    summary = {
        "n_outputs": n_out, "n_degenerate_outputs": sum(1 for o in outputs.values() if "degenerate" in o), "n_chosen_lgb": sum(o["chosen_model"] == "lgb" for o in outputs.values()),
        "n_chosen_mlp": sum(o["chosen_model"] == "mlp" for o in outputs.values()),
        "n_cqr": sum(o["interval_method"] == "cqr" for o in outputs.values()),
        "nominal": 0.9, "coverage_min": float(covs.min()), "coverage_median": float(np.median(covs)),
        "coverage_max": float(covs.max()), "coverage_mean": float(covs.mean()),
        "n_nominal_inside_ci95": inside, "outputs_ci_below_nominal": below, "outputs_ci_above_nominal": above,
        "expected_outside_by_chance_if_independent": 0.05 * n_out,
        "note_band": ("single_split_band_95_theory adds the calibration-draw variance (coverage given the calibration "
                      "set is Beta distributed, variance about a(1-a)/(n_cal+2)) to the binomial test variance."),
        "note": ("Each output's 95% Clopper-Pearson CI covers only the test-set binomial error. It ignores the "
                 "calibration draw (q-hat is itself random), and every output shares the same calibration and test "
                 "points, so these per-output checks are neither complete nor independent. The theory band and the "
                 "re-split check below account for both sources. Above-nominal coverage on outputs with many ties "
                 "(miss rates that are exactly 0) is expected: split conformal guarantees >= nominal."),
        "latency_median_width_over_deadline_90": {"median": float(np.median(lat_w)), "max": float(np.max(lat_w))} if lat_w else None,
        "frac_uncertain_90_median": float(np.median([rep[n]["alpha_0.1"]["frac_uncertain"] for n in outputs])),
        "single_split_sampling_sd_theory": float(np.sqrt(0.9 * 0.1 / (n_cal_typ + 2) + 0.9 * 0.1 / n_test_typ)),
        "single_split_band_95_theory": [float(0.9 - 1.96 * np.sqrt(0.9 * 0.1 / (n_cal_typ + 2) + 0.9 * 0.1 / n_test_typ)),
                                        float(0.9 + 1.96 * np.sqrt(0.9 * 0.1 / (n_cal_typ + 2) + 0.9 * 0.1 / n_test_typ))],
        "n_inside_theory_band": int(sum(1 for _, c, _, _, _ in cov90 if abs(c - 0.9) <= 1.96 * np.sqrt(0.9 * 0.1 / (n_cal_typ + 2) + 0.9 * 0.1 / n_test_typ))),
    }
    headline = {n: {"chosen": outputs[n]["chosen_model"], "test_mae": outputs[n]["test_mae"],
                    "test_median_rel_err": outputs[n]["test_median_rel_err"],
                    "coverage_90": outputs[n]["coverage"]["0.9"]["coverage"],
                    "coverage_90_ci95": outputs[n]["coverage"]["0.9"]["ci95"],
                    "median_width_90": outputs[n]["coverage"]["0.9"]["median_width"],
                    "frac_uncertain_90": outputs[n]["coverage"]["0.9"]["frac_uncertain"]}
                for n in HEADLINE if n in outputs}

    # ------------------------------------------------------------------ re-split check
    resplit = resplit_coverage(bundle, df, X, split)
    summary["resplit"] = resplit["summary"]
    for n, v in resplit["per_output"].items():
        if n in outputs:
            outputs[n]["resplit_coverage_90"] = v

    # ------------------------------------------------------------------ speed
    names = list(bundle.models)
    xi = X[split["test"][:200]]
    t0 = time.perf_counter()
    for i in range(50):
        bundle.predict(xi[i:i + 1], names)
    py_ms = (time.perf_counter() - t0) / 50 * 1000
    t0 = time.perf_counter()
    bundle.predict(xi, names)
    py_batch_ms = (time.perf_counter() - t0) / len(xi) * 1000
    speed: dict[str, Any] = {"python_bundle_all_outputs_ms_per_design": py_ms,
                             "python_bundle_batch_ms_per_design": py_batch_ms, "n_outputs_python": len(names)}
    if ONNX_PATH.exists():
        import onnxruntime as ort
        sess = ort.InferenceSession(str(ONNX_PATH))
        x32 = xi.astype(np.float32)
        sess.run(None, {"x": x32[:1]})
        t0 = time.perf_counter()
        for i in range(len(x32)):
            sess.run(None, {"x": x32[i:i + 1]})
        speed["onnx_web_model_ms_per_design_python_ort"] = (time.perf_counter() - t0) / len(x32) * 1000
        speed["onnx_bytes"] = ONNX_PATH.stat().st_size
    nw = node_ortweb_latency()
    if nw:
        speed["onnxruntime_web_wasm_node"] = nw
    sim_wall = dataset["sample_wall_s_median"]
    sim_cpu = dataset["sample_cpu_s_median"]
    speed["full_sim_wall_s_per_design_median"] = sim_wall
    speed["full_sim_cpu_s_per_design_median"] = sim_cpu
    speed["speedup_python_bundle_vs_sim_cpu"] = sim_cpu / (py_ms / 1000)
    if "onnx_web_model_ms_per_design_python_ort" in speed:
        speed["speedup_onnx_vs_sim_cpu"] = sim_cpu / (speed["onnx_web_model_ms_per_design_python_ort"] / 1000)
    if nw and "ms_per_run" in nw:
        speed["speedup_ortweb_vs_sim_cpu"] = sim_cpu / (nw["ms_per_run"] / 1000)
    speed["note"] = ("Full-sim time is one design point at 2.0 simulated seconds (1.0 s burn-in plus a 1.0 s measured window) on the interactive mix, measured as "
                     "process CPU time inside dataset generation on a shared machine. Surrogate time covers every output "
                     "plus its conformal interval.")

    # ------------------------------------------------------------------ robot subset
    robot = None
    rb = DATA / "robot_bundle.pkl"
    if rb.exists():
        with open(rb, "rb") as fh:
            r = pickle.load(fh)
        robot = {k: v for k, v in r.items() if k != "models"}

    # ------------------------------------------------------------------ fidelity check
    fid = fidelity_check(df, split["test"])
    fid_rep = {}
    m = fid.merge(df, on="sample_id")
    Xf = m[[f"in.{k}" for k in INPUTS]].to_numpy(float)
    for name in ["T4_vla.p99_ms", "T2_detect.p99_ms", "T8_safety.p99_ms", "energy_per_robot_hour_wh", "avg_power_w",
                 "chain_camera_to_plan.p99_ms"]:
        ys = m[f"study.{name}"].to_numpy(float)
        yi = m[f"out.{name}"].to_numpy(float)
        ok = np.isfinite(ys) & np.isfinite(yi)
        pr = bundle.predict(Xf[ok], [name], 0.1)[name]
        inside_s = (ys[ok] >= pr["lo"]) & (ys[ok] <= pr["hi"])
        fid_rep[name] = {"n": int(ok.sum()),
                         "median_rel_diff_study_vs_interactive": float(np.median(np.abs(ys[ok] - yi[ok]) / np.maximum(np.abs(yi[ok]), 1e-9))),
                         "surrogate_coverage_of_study_truth_90": float(inside_s.mean()),
                         "ci95": list(clopper_pearson(int(inside_s.sum()), int(ok.sum())))}
    fidelity = {"n_designs": int(len(fid)), "reference": "configs/fidelity/study.yaml with npu L1",
                "per_output": fid_rep,
                "note": "Coverage against a different fidelity mix is not guaranteed; this measures the shift."}

    # ------------------------------------------------------------------ co-sim shift
    shift = None
    rparts = sorted((DATA / "robot_parts").glob("*.parquet"))
    if rparts:
        rd = pd.concat([pd.read_parquet(p) for p in rparts]).drop_duplicates("sample_id")
        rd = rd[rd["ok"]]
        Xr = rd[[f"in.{k}" for k in INPUTS]].to_numpy(float)
        shift = {}
        for name, col in [("T4_vla.p99_ms", "out.T4_vla.p99_ms"), ("T2_detect.p99_ms", "out.T2_detect.p99_ms"),
                          ("energy_per_robot_hour_wh", "out.energy_per_robot_hour_wh"),
                          ("T8_safety.p99_ms", "out.T8_safety.p99_ms")]:
            if col not in rd:
                continue
            y = rd[col].to_numpy(float)
            ok = np.isfinite(y)
            pr = bundle.predict(Xr[ok], [name], 0.1)[name]
            ins = (y[ok] >= pr["lo"]) & (y[ok] <= pr["hi"])
            shift[name] = {"n": int(ok.sum()), "coverage_90": float(ins.mean()),
                           "ci95": list(clopper_pearson(int(ins.sum()), int(ok.sum()))),
                           "median_rel_err": float(np.median(np.abs(pr["point"] - y[ok]) / np.maximum(np.abs(y[ok]), 1e-9)))}
        shift["note"] = ("Co-sim runs differ from the chip-only training runs (20 s runs, scenario events mid-run, "
                         "robot-driven T5 releases, live core build). Coverage is not guaranteed here.")

    runlen = runlength_check()

    # ------------------------------------------------------------------ figures
    figs = make_figures(bundle, df, X, split, cov90)

    prov = provenance()
    prov.update({"script": "studies/s11_surrogate_quality.py", "bundle": str(BUNDLE_PATH.relative_to(ROOT)),
                 "date": str(date.today()), "runtime_s": time.time() - t_start})
    out = {"provenance": prov, "dataset": dataset, "summary": summary, "headline": headline, "speed": speed,
           "model_sizes": {"onnx_bytes": speed.get("onnx_bytes"), "bundle_pickle_bytes": BUNDLE_PATH.stat().st_size},
           "robot_subset": robot, "run_length_check": runlen, "fidelity_check": fidelity, "cosim_shift": shift, "outputs": outputs,
           "figures": figs,
           "guarantee": ("Split conformal and CQR give marginal coverage >= 1 - alpha under exchangeability of "
                         "calibration and test design points. They do not give coverage conditional on a design, "
                         "and they say nothing outside the sampled design space or under a changed simulator.")}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "s11.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({"summary": summary, "speed": speed}, indent=1, default=float)[:4000])


def resplit_coverage(bundle: Any, df: pd.DataFrame, X: np.ndarray, split: dict[str, np.ndarray],
                     n_rep: int = 200, alpha: float = 0.1, seed: int = 11) -> dict[str, Any]:
    """Coverage over many random calibration/test re-splits of the held-out pool.

    The models never saw the calibration or test points, so re-splitting that pool and
    recomputing q-hat is a valid way to look at the distribution of test coverage. Split
    conformal promises that its average over splits is >= 1 - alpha (and <= 1 - alpha + 1/(n+1)
    without ties). The spread across re-splits shows the combined calibration and test sampling
    error that a single split's binomial interval leaves out.
    """
    from surrogate.conformal import conformal_qhat
    from surrogate.train import fwd, lgb_features
    rng = np.random.default_rng(seed)
    pool = np.concatenate([split["calib"], split["test"]])
    F = lgb_features(X[pool])
    per: dict[str, Any] = {}
    for name, m in bundle.models.items():
        y = df[f"out.{name}"].to_numpy(float)[pool]
        ok = np.isfinite(y)
        if ok.sum() < 100 or np.nanstd(y[ok]) == 0:
            per[name] = {"degenerate": True, "note": "constant in the data (for example a miss rate that is always 0)"}
            continue
        z = fwd(name, y[ok])
        zp = m.predict_z(X[pool][ok])
        if m.conformal["method"] == "cqr":
            lo_q, hi_q = m.q_lo.predict(F[ok]), m.q_hi.predict(F[ok])
            s = np.maximum(lo_q - z, z - hi_q)
        else:
            s = np.abs(z - zp)
        n = len(s)
        covs = []
        for _ in range(n_rep):
            perm = rng.permutation(n)
            c, t = perm[: n // 2], perm[n // 2:]
            q = conformal_qhat(s[c], alpha)
            covs.append(float(np.mean(s[t] <= q)))
        covs_a = np.array(covs)
        per[name] = {"mean": float(covs_a.mean()), "sd": float(covs_a.std()), "p2_5": float(np.quantile(covs_a, 0.025)),
                     "p97_5": float(np.quantile(covs_a, 0.975)), "n_pool": int(n), "n_rep": n_rep}
    good = [v for v in per.values() if not v.get("degenerate")]
    means = np.array([v["mean"] for v in good])
    summary = {"n_outputs": len(good), "n_degenerate": sum(1 for v in per.values() if v.get("degenerate")),
               "mean_of_means": float(means.mean()), "min_mean": float(means.min()), "max_mean": float(means.max()),
               "median_sd_across_resplits": float(np.median([v["sd"] for v in good])),
               "n_mean_below_nominal_by_more_than_0_005": int(np.sum(means < 1 - alpha - 0.005)),
               "note": "score s <= q-hat is the same event as y inside the interval (before float rounding)."}
    return {"summary": summary, "per_output": per}


def make_figures(bundle: Any, df: pd.DataFrame, X: np.ndarray, split: dict[str, np.ndarray],
                 cov90: list[tuple[str, float, float, float, int]]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIG.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9})
    files = []
    # Coverage per output with CIs.
    cov = sorted(cov90, key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(7.5, 0.13 * len(cov) + 1.2))
    y = np.arange(len(cov))
    ax.errorbar([c for _, c, _, _, _ in cov], y, xerr=[[c - lo for _, c, lo, _, _ in cov], [hi - c for _, c, _, hi, _ in cov]],
                fmt="o", ms=3, color="#2563eb", ecolor="#93c5fd", lw=1)
    ax.axvline(0.9, color="#dc2626", lw=1, label="nominal 90%")
    ax.set_yticks(y, [n for n, _, _, _, _ in cov], fontsize=6)
    ax.set_xlabel("empirical coverage on the held-out test split (95% Clopper-Pearson CI)")
    ax.set_title("s11: conformal coverage per output at 90% nominal")
    ax.legend(loc="lower right")
    fig.tight_layout()
    for ext in ("png", "svg"):
        p = FIG / f"s11_coverage.{ext}"
        fig.savefig(p, dpi=160)
        files.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    # Parity plots with intervals for four headline outputs.
    te = split["test"]
    sel = ["energy_per_robot_hour_wh", "T4_vla.p99_ms", "chain_camera_to_plan.p99_ms", "worst_safety_miss_rate"]
    fig, axs = plt.subplots(1, 4, figsize=(13, 3.4))
    for ax, n in zip(axs, sel):
        yv = df[f"out.{n}"].to_numpy(float)[te]
        ok = np.isfinite(yv)
        pr = bundle.predict(X[te][ok], [n], 0.1)[n]
        inside = (yv[ok] >= pr["lo"]) & (yv[ok] <= pr["hi"])
        ax.errorbar(yv[ok], pr["point"], yerr=[pr["point"] - pr["lo"], pr["hi"] - pr["point"]], fmt="none",
                    ecolor="#cbd5e1", lw=0.5, zorder=1)
        ax.scatter(yv[ok][inside], pr["point"][inside], s=4, color="#2563eb", zorder=2, label="inside interval")
        ax.scatter(yv[ok][~inside], pr["point"][~inside], s=6, color="#dc2626", zorder=3, label="outside")
        lim = [min(yv[ok].min(), pr["point"].min()), max(yv[ok].max(), pr["point"].max())]
        ax.plot(lim, lim, color="#111827", lw=0.8)
        if n.endswith("_ms"):
            from matplotlib.ticker import NullFormatter
            ax.set_xscale("log"); ax.set_yscale("log")
            for axis in (ax.xaxis, ax.yaxis):
                axis.set_minor_formatter(NullFormatter())
        ax.set_title(f"{n}\ncoverage {inside.mean():.3f} (n={ok.sum()})", fontsize=8)
        ax.set_xlabel("full model"); ax.set_ylabel("surrogate")
    axs[0].legend(fontsize=7)
    fig.tight_layout()
    for ext in ("png", "svg"):
        p = FIG / f"s11_parity.{ext}"
        fig.savefig(p, dpi=160)
        files.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    return files


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    main()
