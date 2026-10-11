"""买入风险分：因果性、缺失数据姿态、阈值判定与内核接线。"""

import importlib.util
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.features import risk_score  # noqa: E402
from stock_selector.features.risk_features import (  # noqa: E402
    FEATURE_NAMES, MIN_BARS, RAW_COLUMNS, build_features,
)


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_kline_features", ROOT / "scripts" / "build_kline_features.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _kline(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """用生产链路 compute_indicators 造一张真实的指标宽表（含全部特征所需列）。"""
    rng = np.random.default_rng(seed)
    close = 10 * np.cumprod(1 + rng.normal(0, 0.02, n))
    raw = pd.DataFrame({
        "code": "603000",
        "name": "Test",
        "date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "open": close * 0.995,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": rng.uniform(8e5, 2e6, n).round(0),
        "amount": (close * 1e6).round(0),
    })
    return _load_builder().compute_indicators(raw)


def _write_model(dirpath: Path, constant: float, features=None, threshold: float = -0.0037):
    """造一个只会输出常数的假模型 + meta，用来验阈值判定与清单校验。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    model = DummyRegressor(strategy="constant", constant=constant)
    model.fit(np.zeros((3, 2)), [0.0, 0.0, 0.0])     # joblib 载入后要能直接 predict
    joblib.dump(model, dirpath / "risk_ex10.joblib")
    meta = {
        "features": list(features or FEATURE_NAMES),
        "reject_threshold_ex10": threshold,
        "n_stocks": 1, "n_rows": 1, "trained_at": "test",
    }
    (dirpath / "risk_ex10.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return dirpath


def test_feature_list_is_complete_and_unique():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert len(RAW_COLUMNS) == len(set(RAW_COLUMNS))


def test_build_features_causal():
    """任意截断点的最后一行必须与全历史算法一致——出现未来函数就必然在这里暴露。"""
    d = _kline()
    full = build_features(d)
    assert list(full.columns) == FEATURE_NAMES
    assert len(full) == len(d)
    for cut in (MIN_BARS, len(d) - 37, len(d) - 1):
        part = build_features(d.iloc[:cut].reset_index(drop=True))
        a = full.iloc[cut - 1]
        b = part.iloc[-1]
        bad = [c for c in FEATURE_NAMES
               if not (pd.isna(a[c]) and pd.isna(b[c])) and not np.isclose(a[c], b[c])]
        assert not bad, f"截断到 {cut} 时这些列变了: {bad}"


def test_build_features_values_sane():
    f = build_features(_kline()).iloc[-1]
    assert 0.0 <= f["pos60"] <= 1.0
    assert f["box20"] >= 0 and f["dd_hi20"] <= 0
    assert f["lu20"] >= 0 and f["lu5"] >= 0
    # 随机游走很少真涨停：从未涨停时 age 就该是 NaN（模型把缺失当分支学），不是 0
    assert pd.isna(f["age"]) or f["age"] >= 0


def test_never_limit_up_stock_is_still_scored(tmp_path):
    """age/lu20 缺失不等于"判不了"。否则绝大多数从没涨停的票会静默绕过风控。"""
    d = _write_model(tmp_path / "m", constant=-0.01)
    k = _kline()
    assert pd.isna(build_features(k).iloc[-1]["age"])
    value = risk_score.score(k, model_dir=d)
    assert value == pytest.approx(-0.01)


def test_score_returns_none_when_row_mostly_missing(tmp_path):
    d = _write_model(tmp_path / "m", constant=-0.01)
    k = _kline()
    for col in ("close", "ma5", "ma10", "ma20", "ma60", "atr14", "macd_hist",
                "boll_upper", "boll_lower", "obv", "rsi14", "kdj_j", "wr14",
                "adx14", "roc10", "body_ratio", "upper_ratio", "lower_ratio",
                "sup20", "res20"):
        k[col] = k[col].astype(float).mask(k["date"] >= k["date"].iloc[-30])
    assert risk_score.score(k, model_dir=d) is None


def test_score_returns_none_when_history_too_short():
    d = _kline(n=MIN_BARS - 5)
    assert risk_score.score(d) is None
    assert risk_score.decide(d) == (False, None)   # 判不了就放行，不猜


def test_missing_model_raises(tmp_path):
    with pytest.raises(RuntimeError, match="train_risk_model"):
        risk_score.score(_kline(), model_dir=tmp_path / "nowhere")


def test_feature_mismatch_raises(tmp_path):
    """列序错位会给出看着合理实则无意义的分数，必须拒绝启动。"""
    d = _write_model(tmp_path / "m", constant=-0.01, features=FEATURE_NAMES[:-1])
    with pytest.raises(RuntimeError, match="特征清单"):
        risk_score.score(_kline(), model_dir=d)


def test_decide_applies_absolute_threshold(tmp_path):
    d = _write_model(tmp_path / "m", constant=-0.0120, threshold=-0.0037)
    rejected, value = risk_score.decide(_kline(), model_dir=d)
    assert rejected is True
    assert value == pytest.approx(-0.0120)

    d2 = _write_model(tmp_path / "ok", constant=-0.0010, threshold=-0.0037)
    assert risk_score.decide(_kline(), model_dir=d2) == (False, -0.0010)


def test_cache_invalidates_on_retrain(tmp_path):
    import os

    d = _write_model(tmp_path / "m", constant=-0.01)
    k = _kline()
    assert risk_score.score(k, model_dir=d) == pytest.approx(-0.01)

    _write_model(tmp_path / "m", constant=0.02)          # 重新训练，不必重启服务
    path = d / "risk_ex10.joblib"
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert risk_score.score(k, model_dir=d) == pytest.approx(0.02)


def test_relative_model_dir_resolves_to_repo_root():
    """settings.yaml 里写 "data/models"：相对路径必须按仓库根解析。

    服务与 pytest 的工作目录都可能不同，按包目录解析会找不到模型而让 risk_filter 报错。
    """
    assert risk_score._resolve_dir("data/models") == ROOT / "data" / "models"
    assert risk_score.DEFAULT_MODEL_DIR == ROOT / "data" / "models"


def test_shipped_model_loads_and_scores(tmp_path):
    """仓库里的模型必须能被默认路径加载，且配置里的开关真能生效。"""
    from stock_selector.core import selector

    if not (ROOT / "data" / "models" / "risk_ex10.joblib").is_file():
        pytest.skip("模型未生成（data/ 不入库），先跑 scripts/train_risk_model.py")
    _, meta, thr = risk_score.load()                    # 缺省 model_dir
    assert list(meta["features"]) == FEATURE_NAMES
    assert thr < 0                                     # 阈值是"预测超额为负"的水位
    assert selector._risk_gate({"enabled": True}) is risk_score


def test_selector_fails_loudly_when_enabled_without_model(monkeypatch):
    from stock_selector.core import selector

    with pytest.raises(RuntimeError, match="train_risk_model"):
        selector._risk_gate({"enabled": True, "model_dir": "/tmp/no-such-model-dir"})
    assert selector._risk_gate(None) is None
    assert selector._risk_gate({"enabled": False}) is None
