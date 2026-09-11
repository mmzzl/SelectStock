import numpy as np
import pandas as pd
import pytest

from stock_selector.features.modeling import (
    evaluate_time_split,
    fit_gaussian,
    precision_at_k,
    time_split,
)


def test_precision_at_k():
    y = np.array([1, 1, 0, 1, 0])
    proba = np.array([0.9, 0.8, 0.7, 0.2, 0.1])
    assert precision_at_k(y, proba, 2) == 1.0
    assert precision_at_k(y, proba, 4) == 0.75


def test_time_split_no_leak():
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    idx = np.arange(60)
    train_idx, test_idx = time_split(idx, dates)
    assert dates[train_idx].max() < dates[test_idx].min()
    assert abs(len(train_idx) / 60 - 0.8) < 0.02


def _separable(n_pos=200, n_neg=800, seed=0):
    rng = np.random.default_rng(seed)
    x_pos = rng.normal(2.0, 0.5, size=(n_pos, 3))
    x_neg = rng.normal(0.0, 0.5, size=(n_neg, 3))
    X = np.vstack([x_pos, x_neg])
    y = np.array([1] * n_pos + [0] * n_neg)
    all_dates = pd.date_range("2024-01-01", periods=n_pos + n_neg, freq="B")
    dates = rng.permutation(all_dates)
    return X, y, dates


def test_fit_gaussian_class_separable():
    X, y, _ = _separable()
    idx = rng_index = np.random.default_rng(1).permutation(len(y))[:200]
    model = fit_gaussian(X[idx], y[idx], prior_pos=0.3)
    proba = model.predict_proba(X[idx])[:, 1]
    assert np.mean(proba[y[idx] == 1]) > np.mean(proba[y[idx] == 0])


def test_evaluate_time_split():
    X, y, dates = _separable()
    res = evaluate_time_split(X, y, dates, prior_pos=0.3)
    for key in ("auc", "avg_precision", "prec_at_tops", "base_rate", "n_pos_valid", "misses"):
        assert key in res
    assert 0.5 < res["auc"] <= 1.0
    assert isinstance(res["misses"], pd.DataFrame)
    assert set(res["misses"].columns) >= {"proba", "label"}
