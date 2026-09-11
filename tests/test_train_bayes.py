import importlib.util
import joblib
import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/train_bayes.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("train_bayes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk(code, n=200, limup_days=(), start=10.0):
    rng = np.random.default_rng(0)
    close = np.full(n, start)
    for i in range(1, n):
        if i in limup_days:
            close[i] = close[i - 1] * 1.10
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.0005, 0.01))
    vol = np.full(n, 100.0)
    for d in limup_days:
        vol[d] = 250.0
    return pd.DataFrame({
        "code": code, "name": "T",
        "date": pd.date_range("2025-01-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol, "amount": vol * close,
    })


def test_module_loads():
    mod = _load_script()
    assert callable(mod.main)
    assert str(mod.KLINE_DIR).endswith("stock_kline")


def test_diag_code(tmp_path):
    mod = _load_script()
    code = "600000"
    (tmp_path / f"{code}.parquet").parent.mkdir(exist_ok=True)
    _mk(code).to_parquet(tmp_path / f"{code}.parquet", index=False, compression="zstd")
    mod.main(["--code", code, "--kline-dir", str(tmp_path)])  # 不抛错即通过


def test_train_writes_artifacts(tmp_path):
    mod = _load_script()
    for code, days in (("600000", range(190, 194)), ("600001", ())):
        _mk(code, limup_days=days).to_parquet(
            tmp_path / f"{code}.parquet", index=False, compression="zstd"
        )
    model_file = tmp_path / "m.joblib"
    report_file = tmp_path / "r.md"
    mod.main([
        "--train", "--kline-dir", str(tmp_path),
        "--models-dir", str(tmp_path), "--reports-dir", str(tmp_path),
        "--model-file", str(model_file), "--report-file", str(report_file),
        "--prior-pos", "0.15",
    ])
    assert model_file.exists() and report_file.exists()
    with open(model_file, "rb") as fh:
        payload = joblib.load(fh)
    assert "model" in payload and "features" in payload and "prior_pos" in payload
    assert "precision@50" in report_file.read_text(encoding="utf-8")
