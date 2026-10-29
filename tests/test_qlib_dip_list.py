"""批次 G2 应用侧的 RED 测试：把「今天涨 → 明天低开买入」事件臂接成第三条榜 `qlib_dip`。

钉四件事（都是行为，不是实现细节）：
① 名次只在**事件人群**（当日涨幅 ≥ +0.1%，与 `dip_event` 训练口径同一条）里排；
② 模型来源可单独指定（`QLIB_DIP_MODEL_DIR`），没配就退回线上模型——一条环境变量换/回滚；
③ 行内价位块 = 低吸规则价（买线=今收、无失效线、无目标价），不是筹码块；
④ 没配事件模型时策略列表里不出现 `qlib_dip`（不能让 App 拿到空榜当成「今天没票」）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _pred(pairs: dict[str, float], day="2026-10-28") -> pd.Series:
    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp(day)], sorted(pairs)], names=["datetime", "instrument"])
    return pd.Series([pairs[i] for i in sorted(pairs)], index=idx, name="score")


# ---------------------------------------------------------------- ① 人群与名次
def test_rank_dip_only_ranks_today_upticks():
    from stock_selector_qlib import serving

    pred = _pred({"SH600001": 0.9, "SH600002": 0.5, "SH600003": 0.7})
    chg = pd.Series({"SH600001": 0.020, "SH600002": -0.030, "SH600003": 0.0011})
    rows = serving.rank_dip(pred, chg, topk=10)

    # 002 当天跌 3% ⇒ 不在「今天涨」的人群里，连榜都不上
    assert [r["qlib_symbol"] for r in rows] == ["SH600001", "SH600003"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["universe"] == 2, rows[0]


def test_rank_dip_boundary_uses_training_threshold():
    """+0.1% 是训练人群的下界（`dip_event` min_chg=0.001）；平盘和 −0.1% 都不算涨。"""
    from stock_selector_qlib import serving

    pred = _pred({"SH600001": 0.1, "SH600002": 0.2})
    chg = pd.Series({"SH600001": 0.0009, "SH600002": 0.0})
    assert serving.rank_dip(pred, chg, topk=10) == []


def test_rank_dip_ties_break_by_code_ascending():
    from stock_selector_qlib import serving

    pred = _pred({"SH600009": 0.5, "SH600001": 0.5})
    chg = pd.Series({"SH600009": 0.05, "SH600001": 0.05})
    rows = serving.rank_dip(pred, chg, topk=10)
    assert [r["qlib_symbol"] for r in rows] == ["SH600001", "SH600009"]


def test_rank_dip_score_is_percentile_inside_population():
    from stock_selector_qlib import serving

    pred = _pred({f"SH60000{i}": float(i) for i in (1, 2, 3)})
    chg = pd.Series({f"SH60000{i}": 0.02 for i in (1, 2, 3)})
    rows = serving.rank_dip(pred, chg, topk=10)
    assert [r["score"] for r in rows] == pytest.approx([1.0, 2 / 3, 1 / 3])


# ---------------------------------------------------------------- ② 模型来源
def test_dip_artifact_off_when_no_event_model_present(monkeypatch):
    """没配事件模型 = 这条榜不存在（qlib_dip 不进策略列表），不能拿线上模型冒充事件模型。"""
    from stock_selector_qlib import artifacts, serving

    monkeypatch.delenv("QLIB_DIP_MODEL_DIR", raising=False)
    monkeypatch.setattr(artifacts, "exists", lambda dest=None: False)
    art, source = serving.dip_artifact()
    assert art is None
    assert source == "off"


def test_dip_artifact_uses_env_dir_when_present(monkeypatch, tmp_path):
    from stock_selector_qlib import artifacts, serving

    fake = tmp_path / "model_batchG_evt"
    fake.mkdir()
    # 产物齐不齐由 artifacts.exists 真判（四件套），这里只桩掉重活 lgb 加载
    for name in (artifacts.MODEL_FILE, artifacts.NORM_FILE, artifacts.POOL_FILE, artifacts.META_FILE):
        (fake / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv("QLIB_DIP_MODEL_DIR", str(fake))
    loaded = artifacts.Artifacts(None, ["x"], np.zeros(1), np.ones(1), ["SH600001"],
                                 {"run_id": "batchG_evt"})
    monkeypatch.setattr(artifacts, "load", lambda dest=None: loaded)
    art, source = serving.dip_artifact()
    assert art.run_id == "batchG_evt"
    assert source == str(fake)


def test_dip_artifact_degrades_when_load_fails(monkeypatch, tmp_path):
    """目录在、产物坏 = 配置错，不能整个服务 502：这条榜自己下线，主榜照常出票。"""
    from stock_selector_qlib import serving

    fake = tmp_path / "model_broken"
    fake.mkdir()
    monkeypatch.setenv("QLIB_DIP_MODEL_DIR", str(fake))

    def boom(dest=None):
        raise RuntimeError("缺 norm.json")

    monkeypatch.setattr("stock_selector_qlib.artifacts.load", boom)
    art, source = serving.dip_artifact()
    assert art is None
    assert source.startswith("off")


# ---------------------------------------------------------------- ③ 价位块
def test_dip_row_carries_low_buy_rule_price():
    """事件榜的交易语义=低吸：买线就是今收（明开 ≤ 今收才买），不设失效线、不设目标价。"""
    from stock_selector_qlib import api

    row = {"code": "002636", "qlib_symbol": "SZ002636", "price": 41.85, "chg": 0.0603}
    levels = api._dip_entry_levels(row)
    assert levels["buy_ref_price"] == pytest.approx(41.85)
    assert levels["buy_invalid_price"] is None
    assert levels["sell_target_price"] is None
    assert "低开" in levels["level_note"] and "不设失效线" in levels["level_note"]


def test_dip_strategy_is_registered(monkeypatch):
    from stock_selector_qlib import api, serving

    art = type("A", (), {"run_id": "batchG_evt", "meta": {}})()
    monkeypatch.setattr(serving, "dip_artifact", lambda: (art, "batchG_evt"))
    assert api.STRATEGY_DIP in api._strategies()


def test_dip_strategy_absent_when_disabled(monkeypatch):
    from stock_selector_qlib import api, serving

    monkeypatch.setattr(serving, "dip_artifact", lambda: (None, "off"))
    assert api.STRATEGY_DIP not in api._strategies()
