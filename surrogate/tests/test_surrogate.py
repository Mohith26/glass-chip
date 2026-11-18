"""Unit tests for the surrogate stream (no simulator runs, fast)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "studies"))

from surrogate.conformal import clopper_pearson, conformal_qhat  # noqa: E402
from surrogate.dataset import INPUTS, SPACE, area_proxy, decode, latin_hypercube, to_cfg  # noqa: E402
from surrogate.p6 import _alpha_power_inverse  # noqa: E402
from surrogate.train import fwd, inv, lgb_features, mlp_features  # noqa: E402


def test_qhat_is_the_finite_sample_quantile():
    s = np.arange(1, 100, dtype=float)          # n = 99
    # ceil((99 + 1) * 0.9) = 90 -> the 90th smallest score
    assert conformal_qhat(s, 0.1) == 90.0
    assert math.isinf(conformal_qhat(np.arange(5.0), 0.1))   # n too small for 90%


def test_split_conformal_covers_on_synthetic_data():
    rng = np.random.default_rng(0)
    hits = []
    for _ in range(200):
        cal = rng.standard_t(3, 300)
        q = conformal_qhat(np.abs(cal), 0.1)
        test = rng.standard_t(3, 300)
        hits.append(np.mean(np.abs(test) <= q))
    m = float(np.mean(hits))
    assert 0.895 <= m <= 0.91          # >= 0.9 and <= 0.9 + 1/(n+1) in expectation


def test_clopper_pearson_contains_point_estimate():
    lo, hi = clopper_pearson(90, 100)
    assert lo < 0.9 < hi
    assert clopper_pearson(0, 10)[0] == 0.0 and clopper_pearson(10, 10)[1] == 1.0


def test_decode_respects_ranges():
    u = latin_hypercube(500, len(INPUTS), np.random.default_rng(1))
    for row in u:
        num = decode(dict(zip(INPUTS, row)))
        for k, (kind, lo, hi, _, _) in SPACE.items():
            assert lo - 1e-9 <= num[k] <= hi + 1e-9, k
        cfg = to_cfg(num)
        assert cfg["noc_bits"] in (128, 256, 512)
        assert cfg["npu_array"] % 8 == 0


def test_latin_hypercube_stratifies_each_column():
    n = 50
    u = latin_hypercube(n, 4, np.random.default_rng(2))
    for j in range(4):
        assert sorted(np.floor(u[:, j] * n).astype(int).tolist()) == list(range(n))


def test_transforms_round_trip():
    y = np.array([0.0, 0.002, 1.0, 120.0])
    assert np.allclose(inv("T4_vla.p99_ms", fwd("T4_vla.p99_ms", y)), y)
    r = np.array([0.0, 0.01, 0.5, 1.0])
    assert np.allclose(inv("T4_vla.miss_rate", fwd("T4_vla.miss_rate", r)), r)


def test_features_shapes():
    X = np.array([[decode(dict(zip(INPUTS, np.full(len(INPUTS), 0.5))))[k] for k in INPUTS]])
    assert lgb_features(X).shape == (1, len(INPUTS) + 1)
    F = mlp_features(X)
    assert np.all(F >= -1e-9) and np.all(F <= 1 + 1e-9)


def test_area_proxy_grows_with_npu():
    base = decode(dict(zip(INPUTS, np.full(len(INPUTS), 0.5))))
    big = dict(base, npu_tiles=8, npu_array=128)
    assert area_proxy(big) > area_proxy(base)


def test_alpha_power_inverse_round_trip():
    f_nom = 1.2e9
    assert _alpha_power_inverse(f_nom, f_nom) == pytest.approx(0.75, abs=1e-6)
    k = f_nom * 0.75 / (0.75 - 0.30) ** 1.3
    f = k * (0.55 - 0.30) ** 1.3 / 0.55
    assert _alpha_power_inverse(f, f_nom) == pytest.approx(0.55, abs=1e-6)


def test_pareto_mask_matches_brute_force():
    from s12_dse import pareto_mask
    F = np.random.default_rng(4).random((400, 3))
    m = pareto_mask(F)
    bf = np.array([not np.any(np.all(F <= f, 1) & np.any(F < f, 1)) for f in F])
    assert (m == bf).all()


def test_scale_bitrate_scales_bytes():
    from surrogate.chiprun import load_graph, scale_bitrate
    g = load_graph("T10_record_4k")
    b0 = sum(op.get("bytes_in", 0) for op in g["ops"] if op.get("bytes_scale_param"))
    g2 = scale_bitrate(load_graph("T10_record_4k"), g["params"]["bitrate_bps"] * 2)
    b1 = sum(op.get("bytes_in", 0) for op in g2["ops"] if op.get("bytes_scale_param"))
    assert b1 == pytest.approx(2 * b0, rel=1e-6)
