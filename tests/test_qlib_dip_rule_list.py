"""批次 H 应用侧的 RED 测试：`qlib_dip` 换成「低吸持有」臂（`batchH_r1`）后，榜必须跟着模型走。

用户 2026-10-29 拍板「换成现在这个吧」——把这条榜从 §44 事件臂（明开买、**当天收盘无条件卖**）
换到 §45 低吸持有臂（明开买、**哪天收盘高过买入价哪天卖，跌 5% 兜底，上限 10 个交易日**）。
换臂带来两处必须跟着动的地方，都是行为不是口味：

① **人群闸门**：`serving.DIP_MIN_CHG` 写死 +0.1%，那是 batchG_evt 的训练人群下界；
   batchH_r1 只在「信号日涨 ≥+5%」的事件上训练过。榜继续按 +0.1% 排＝把模型没学过的样本当好答案。
   ⇒ 闸门从模型 meta 的 `rule_spec`/`event_spec` 取，取不到才退回常量。
② **口径文案**：`/api/strategy/detail` 那条榜的说明现在写「收盘无条件出」，
   那是被他判掉的旧语义；App 上写的必须是新臂的卖法。
"""
from __future__ import annotations

import pandas as pd
import pytest


def _pred(pairs: dict[str, float], day="2026-10-28") -> pd.Series:
    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp(day)], sorted(pairs)], names=["datetime", "instrument"])
    return pd.Series([pairs[i] for i in sorted(pairs)], index=idx, name="score")


# ---------------------------------------------------------------- ① 人群闸门跟着模型
def test_dip_min_chg_comes_from_the_rule_arm_meta():
    """`batchH_r1` 的 meta 里 `rule_spec.min_chg=0.05` ⇒ 闸门是 +5%，不是常量那 +0.1%。"""
    from stock_selector_qlib import serving

    art = type("A", (), {"meta": {"rule_spec": {"min_chg": 0.05}}})()
    assert serving.dip_min_chg(art) == pytest.approx(0.05)


def test_dip_min_chg_still_reads_the_event_arm_meta():
    """上一臂 `batchG_evt` 存的是 `event_spec`，同一只眼睛要都认（换线得能指回去）。"""
    from stock_selector_qlib import serving

    art = type("A", (), {"meta": {"event_spec": {"min_chg": 0.001}}})()
    assert serving.dip_min_chg(art) == pytest.approx(0.001)


def test_dip_min_chg_falls_back_to_the_constant_when_meta_has_none():
    """meta 缺字段 = 老产物/手工产物，不能崩，退回 `DIP_MIN_CHG` 并让调用方照样出榜。"""
    from stock_selector_qlib import serving

    assert serving.dip_min_chg(type("A", (), {"meta": {}})()) == pytest.approx(serving.DIP_MIN_CHG)
    assert serving.dip_min_chg(None) == pytest.approx(serving.DIP_MIN_CHG)


def test_rank_dip_takes_the_threshold_from_the_caller():
    """涨 2% 的票在 +5% 闸门下不能进榜（它是 batchH_r1 没学过的人群）。"""
    from stock_selector_qlib import serving

    pred = _pred({"SH600001": 0.9, "SH600002": 0.5})
    chg = pd.Series({"SH600001": 0.060, "SH600002": 0.020})
    rows = serving.rank_dip(pred, chg, topk=10, min_chg=0.05)
    assert [r["qlib_symbol"] for r in rows] == ["SH600001"]
    assert rows[0]["universe"] == 1, rows[0]


def test_rank_dip_default_threshold_is_unchanged():
    """不传 min_chg 还是旧那条 +0.1%（其他调用方与既有读数不受影响）。"""
    from stock_selector_qlib import serving

    pred = _pred({"SH600001": 0.9, "SH600002": 0.5})
    chg = pd.Series({"SH600001": 0.060, "SH600002": 0.0011})
    rows = serving.rank_dip(pred, chg, topk=10)
    assert [r["qlib_symbol"] for r in rows] == ["SH600001", "SH600002"]


# ---------------------------------------------------------------- ② 文案跟着模型
def _art(meta: dict):
    return type("A", (), {"run_id": meta.get("run_id", "x"), "meta": meta})()


def test_dip_detail_of_rule_arm_describes_hold_until_up():
    """新臂：人口 +5%、买开盘价、涨了就卖、跌 5% 兜底、上限 10 个交易日。"""
    from stock_selector_qlib import api

    art = _art({"run_id": "batchH_r1",
                "label": "rule(T+1开盘买→收盘涨卖/跌5%止损/上限10天)",
                "rule_spec": {"min_chg": 0.05, "max_gap": 0.0,
                              "stop_pct": -0.05, "max_hold": 10}})
    d = api.dip_detail(art)
    assert "≥+5" in d["params"]["population"], d["params"]["population"]
    assert "收盘" in d["params"]["exit_rule"] and "止损" in d["params"]["exit_rule"]
    assert "10" in d["params"]["exit_rule"]
    assert "开盘价" in d["params"]["entry_rule"]


def test_dip_detail_of_rule_arm_does_not_say_close_conditional_exit():
    """「收盘无条件出」是上一臂（§44 E1）的卖法，用户明确判掉：新臂文案里不能还有这句。"""
    from stock_selector_qlib import api

    art = _art({"run_id": "batchH_r1", "rule_spec": {"min_chg": 0.05, "max_gap": 0.0,
                                                     "stop_pct": -0.05, "max_hold": 10}})
    blob = api.dip_detail(art)
    assert "收盘无条件出" not in blob["description"], blob["description"]
    assert "收盘无条件出" not in blob["params"]["exit_rule"]


def test_dip_detail_of_event_arm_keeps_same_day_exit():
    """指回 batchG_evt 时文案要跟着回去（当天收盘卖），别让新臂的话冒充旧臂。"""
    from stock_selector_qlib import api

    art = _art({"run_id": "batchG_evt",
                "event_spec": {"min_chg": 0.001, "max_gap": 0.0}})
    d = api.dip_detail(art)
    assert "收盘无条件出" in d["params"]["exit_rule"], d["params"]["exit_rule"]
    assert "≥+0.1" in d["params"]["population"]


def test_dip_detail_is_what_the_endpoint_serves(monkeypatch):
    """`/api/strategy/detail` 不能继续发模块级写死的那份——那是换臂后最容易被忽略的口径分叉。"""
    from stock_selector_qlib import api, artifacts, serving

    main = _art({"run_id": "batchW_c3", "label": "Ref($close,-1)/Ref($open,-1)-1"})
    art = _art({"run_id": "batchH_r1", "rule_spec": {"min_chg": 0.05, "max_gap": 0.0,
                                                     "stop_pct": -0.05, "max_hold": 10}})
    monkeypatch.setattr(artifacts, "load", lambda dest=None: main)
    monkeypatch.setattr(serving, "score_pool", lambda a: ["SH600001"])
    monkeypatch.setattr(serving, "dip_artifact", lambda: (art, "model_batchH_r1"))

    data = api.strategy_detail()["data"]
    served = next(d for d in data if d["name"] == api.STRATEGY_DIP)
    assert served["params"]["model"] == "batchH_r1"
    assert "收盘无条件出" not in served["description"], served["description"]


# ---------------------------------------------------------------- ③ 行内价位块的说明
def _row():
    return {"code": "002636", "qlib_symbol": "SZ002636", "price": 41.85, "chg": 0.0603}


def test_dip_row_note_of_rule_arm_is_the_hold_exit():
    """App 每行的说明要写这条臂自己的卖法（涨卖/跌 5% 止损/上限 10 日），不能继续是当天收盘卖。"""
    from stock_selector_qlib import api

    art = _art({"run_id": "batchH_r1", "rule_spec": {"min_chg": 0.05, "max_gap": 0.0,
                                                     "stop_pct": -0.05, "max_hold": 10}})
    note = api._dip_entry_levels(_row(), art)["level_note"]
    assert "止损" in note and "10" in note, note
    assert "收盘无条件出" not in note, note
    assert "不设失效线" in note and "低开" in note


def test_dip_row_note_of_event_arm_keeps_same_day_exit():
    from stock_selector_qlib import api

    art = _art({"run_id": "batchG_evt", "event_spec": {"min_chg": 0.001, "max_gap": 0.0}})
    note = api._dip_entry_levels(_row(), art)["level_note"]
    assert "收盘无条件出" in note, note


def test_dip_row_note_still_survives_missing_art():
    """行内说明是展示块：拿不到臂（老调用方/单测）也要出价，退回默认那条口径而不是崩。"""
    from stock_selector_qlib import api

    levels = api._dip_entry_levels(_row(), None)
    assert levels["buy_ref_price"] == pytest.approx(41.85)


# ---------------------------------------------------------------- ④ 信号日不能丢
def test_rank_dip_keeps_signal_date_when_scores_are_a_flat_series():
    """`dip_scores` 在护栏前做过 `xs()`，传给 `rank_dip` 的是**单层**索引 ⇒ 日期判不出来，
    行就带着空 asof 落库（10-29 12:27 那期 `qlib_dip` 30 行 asof 全空，主榜/板榜都是 2026-10-28）。
    调用方既然知道是哪一天，就必须能把日期带进来。"""
    from stock_selector_qlib import serving

    scores = pd.Series({"SH600001": 0.9, "SH600002": 0.7})
    chg = pd.Series({"SH600001": 0.06, "SH600002": 0.08})
    rows = serving.rank_dip(scores, chg, topk=10, min_chg=0.05, date="2026-10-28")
    assert [r["date"] for r in rows] == ["2026-10-28", "2026-10-28"], rows


def test_rank_dip_still_derives_date_from_a_multi_index():
    """给的是两层索引时照旧自己取那天（主榜那条路径的用法不变）。"""
    from stock_selector_qlib import serving

    rows = serving.rank_dip(_pred({"SH600001": 0.9, "SH600002": 0.7}),
                            pd.Series({"SH600001": 0.06, "SH600002": 0.08}),
                            topk=10, min_chg=0.05)
    assert [r["date"] for r in rows] == ["2026-10-28", "2026-10-28"], rows
