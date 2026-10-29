"""主榜换到低吸臂（2026-10-29 用户拍板「换成低吸臂（主榜也换）」）的 RED 测试。

线上模型一旦换成臂（`meta.rule_spec` / `meta.event_spec`），主榜的四件事必须跟着换：
① 出票人群 = **这个臂自己训练时的那条闸门**（`min_chg`），而且筛在打分**之前**
   ——臂没学过的票根本不该进模型，排到跌票身上就是 §44 那个退化形状；
   没有 spec 的老臂（batchW_c3）行为逐位不变，别把全市场榜顺手改了。
② 人群为空 = 明说「今天没有符合人群的票」，不能静默出空榜、也不能把 `pd.concat([])`
   的 ValueError 当成打分崩。
③ 主榜行的 reason 与价位块跟着臂：人口要说人口，价位块是低吸规则价（明开≤今收挂开盘价买、
   涨了就卖），不是筹码三档。
④ `/api/strategy/detail` 第一条（主榜）在臂上线时不再宣称「预测未来 N 日横截面收益」。
"""
from __future__ import annotations

import pandas as pd
import pytest

from stock_selector_qlib import api, artifacts, serving

DAY = "2026-10-29"
PREV = "2026-10-28"


def _art(meta: dict, run_id: str = "batchX") -> object:
    return type("Art", (), {"run_id": run_id, "meta": meta, "pool": []})()


RULE_META = {"rule_spec": {"min_chg": 0.05, "max_gap": 0.0, "stop_pct": -0.05,
                           "max_hold": 10, "cost": 0.002},
             "label": "rule(T+1开盘买→收盘涨卖/跌5%止损/上限10天)", "train_end": "2023-12-29"}
OLD_META = {"label": "Ref($close, -5) / Ref($open, -1) - 1", "train_end": "2023-12-29"}


# ---------------------------------------------------------------- ① 闸门读 meta
def test_arm_spec_is_empty_for_a_full_market_arm():
    assert serving.arm_spec(_art(OLD_META)) == {}
    assert serving.population_gate(_art(OLD_META)) is None


def test_population_gate_reads_the_arms_own_min_chg():
    assert serving.population_gate(_art(RULE_META)) == pytest.approx(0.05)
    ev = {"event_spec": {"min_chg": 0.001}}
    assert serving.population_gate(_art(ev)) == pytest.approx(0.001)
    # 事件榜那条兜底路径不受影响：没有 spec 仍然用 DIP_MIN_CHG
    assert serving.dip_min_chg(_art(OLD_META)) == pytest.approx(serving.DIP_MIN_CHG)


# ---------------------------------------------------------------- ①② 打分前筛人群
def _wire(monkeypatch, art, pool, chg, scores):
    """把 day_ranking 的行情依赖全桩掉，并记下它**实际拿去打分的名单**。

    护栏、告警、缓存都要桩：这条链会走钉钉，测试绝不能真发。
    """
    seen: dict = {}
    monkeypatch.setattr(artifacts, "load", lambda *a, **k: art)
    monkeypatch.setattr(serving, "init_qlib", lambda *a, **k: None)
    monkeypatch.setattr(serving, "calendar", lambda: [PREV, DAY])
    monkeypatch.setattr(serving, "score_pool", lambda a: list(pool))
    monkeypatch.setattr(serving, "pct_of_day",
                        lambda syms, date: {s: chg.get(s) for s in syms})
    monkeypatch.setattr(serving, "_cached_rows", lambda key: None)
    monkeypatch.setattr(serving, "alert_ranking_coverage", lambda problem: False)
    serving._cache.clear()

    def fake_score_frame(a, beg=None, end=None, pool=None, chunk=None):
        names = list(pool if pool is not None else serving.score_pool(a))
        seen["pool"] = names
        idx = pd.MultiIndex.from_product([[pd.Timestamp(DAY)], names],
                                        names=["datetime", "instrument"])
        pred = pd.Series([scores[s] for s in names], index=idx, name="score")
        return pred, {"pool": len(names), "chunk": 0, "thin_rows": 0, "no_row": 0,
                      "scored": len(names), "max_nan": 0}

    def fake_flags(date, pool):
        seen["guard_pool"] = list(pool)
        return pd.Series(dtype="float64"), pd.Series(dtype="float64")

    monkeypatch.setattr(serving, "score_frame", fake_score_frame)
    monkeypatch.setattr(serving, "guardrail_flags", fake_flags)
    monkeypatch.setattr(serving, "apply_guardrails",
                        lambda tail, thin, yao: (tail, {"guard_dropped": 0, "guard_thin": 0,
                                                         "guard_yao": 0, "guard_notes": {}}))
    return seen


POOL = ["SH600001", "SZ002636", "SH600003", "SZ000960"]
CHG = {"SH600001": 0.080, "SZ002636": 0.0603, "SH600003": 0.0011, "SZ000960": -0.030}
SCORES = {"SH600001": 1.0, "SZ002636": 0.9, "SH600003": 0.8, "SZ000960": 0.7}


def test_day_ranking_scores_only_the_arm_population(monkeypatch):
    seen = _wire(monkeypatch, _art(RULE_META, "batchH_r1"), POOL, CHG, SCORES)
    rows = serving.day_ranking(refresh=True)

    # +0.11% 与 −3% 两只连模型的输入都不该拿到
    assert seen["pool"] == ["SH600001", "SZ002636"]
    assert seen["guard_pool"] == ["SH600001", "SZ002636"]
    assert [r["qlib_symbol"] for r in rows] == ["SH600001", "SZ002636"]
    assert rows[0]["universe"] == 2
    # 价位块和 reason 都要用当日涨幅：闸门筛过之后行里必须带着它自己那格的涨幅
    assert rows[0]["chg"] == pytest.approx(0.080)
    assert rows[1]["chg"] == pytest.approx(0.0603)


def test_day_ranking_keeps_whole_pool_for_an_arm_without_spec(monkeypatch):
    """老臂（batchW_c3 那类全市场横截面）一条都不许变：没 spec = 没有闸门。"""
    seen = _wire(monkeypatch, _art(OLD_META, "batchW_c3"), POOL, CHG, SCORES)
    rows = serving.day_ranking(refresh=True)
    assert seen["pool"] == POOL
    assert len(rows) == 4
    assert rows[0]["universe"] == 4


def test_day_ranking_says_so_when_the_arm_population_is_empty(monkeypatch, caplog):
    _wire(monkeypatch, _art(RULE_META, "batchH_r1"), POOL,
          {k: v - 0.10 for k, v in CHG.items()}, SCORES)
    rows = serving.day_ranking(refresh=True)
    assert rows == []
    msg = " ".join(r.message for r in caplog.records)
    assert "5.0" in msg and "今天没有" in msg


# ---------------------------------------------------------------- ③ 主榜行的文案与价位块
def _main_row(**over) -> dict:
    row = {"code": "002636", "qlib_symbol": "SZ002636", "date": DAY, "score_raw": 0.9,
           "score": 1.0, "rank": 1, "raw_rank": 1, "universe": 109, "chg": 0.0603,
           "price": 41.85}
    row.update(over)
    return row


def _wire_model_rows(monkeypatch):
    monkeypatch.setattr(api, "_names_for", lambda rows: {})
    monkeypatch.setattr(api, "_chip_levels", lambda rows: ({}, []))
    monkeypatch.setattr(serving, "pct_of_day", lambda syms, date: {s: 0.0603 for s in syms})


def test_main_arm_row_reason_names_the_population(monkeypatch):
    _wire_model_rows(monkeypatch)
    out = api._model_rows(_art(RULE_META, "batchH_r1"), [_main_row()])

    assert out[0]["strategy"] == api.STRATEGY          # 还是主榜那条，不另立名字
    assert "今日上涨票" in out[0]["reason"] and "109" in out[0]["reason"]
    assert "护栏后票池" not in out[0]["reason"]          # 「护栏后票池」是全市场那套说法
    assert "低吸" in out[0]["signal"]


def test_main_arm_row_uses_the_low_buy_rule_price_not_the_chip_block(monkeypatch):
    _wire_model_rows(monkeypatch)
    out = api._model_rows(_art(RULE_META, "batchH_r1"), [_main_row()])
    ex = out[0]["extra"]
    assert ex["buy_ref_price"] == pytest.approx(41.85)
    assert ex["sell_target_price"] is None
    assert ex["buy_invalid_price"] is None
    assert "低开" in ex["level_note"] and "收盘" in ex["level_note"]


def test_main_row_without_arm_spec_keeps_the_old_wording(monkeypatch):
    """老臂的主榜一个字都不许变（reason 说护栏后票池、价位块仍是筹码三档）。"""
    _wire_model_rows(monkeypatch)
    monkeypatch.setattr(api, "_level_fields", lambda lv, miss: {"buy_ref_price": 9.9,
                                                                "sell_target_price": 10.5})
    out = api._model_rows(_art(OLD_META, "batchW_c3"), [_main_row()])
    assert "今日上涨票" not in out[0]["reason"]
    assert "护栏后票池 109" in out[0]["reason"]
    assert out[0]["extra"]["sell_target_price"] == pytest.approx(10.5)


def test_board_row_says_inside_the_gated_list_when_the_model_is_an_arm(monkeypatch):
    """闸门一进主榜，「全榜第 N」这句就成假话了：全市场那一榜已经不存在，
    板行引用的必须是「上涨票里第 N」——同一份打分表，人口换了说法得跟着换。"""
    _wire_model_rows(monkeypatch)
    row = _main_row(rank=3, raw_rank=3, universe=41, board_pct=10.0)
    out = api._model_rows(_art(RULE_META, "batchH_r1"), [row], board_slice=True)
    assert "全榜第" not in out[0]["reason"]
    assert "上涨票榜第 3" in out[0]["reason"]


# ---------------------------------------------------------------- ④ 策略详情
def _detail(monkeypatch, meta: dict, run_id: str) -> dict:
    art = _art(meta, run_id)
    monkeypatch.setattr(artifacts, "load", lambda *a, **k: art)
    monkeypatch.setattr(serving, "score_pool", lambda a: list(POOL))
    monkeypatch.setattr(serving, "dip_artifact", lambda: (None, "off"))
    return api.strategy_detail()["data"][0]


def test_main_detail_follows_the_arm(monkeypatch):
    d = _detail(monkeypatch, RULE_META, "batchH_r1")
    assert "≥+5.0%" in d["description"]
    assert "横截面" not in d["description"]
    assert d["params"]["population"].startswith("当日收盘涨幅 ≥+5.0%")
    assert d["params"]["model"] == "batchH_r1"


def test_main_detail_keeps_cross_section_wording_for_the_old_arm(monkeypatch):
    d = _detail(monkeypatch, OLD_META, "batchW_c3")
    assert "横截面收益" in d["description"]
    assert d["params"]["model"] == "batchW_c3"


# ---------------------------------------------------------------- ⑤ 两条榜同源时并入
def test_dip_list_merges_into_main_when_both_point_at_the_same_model(monkeypatch, tmp_path):
    """主榜换成低吸臂之后，`QLIB_DIP_MODEL_DIR` 与线上模型是同一份产物。

    这时再单独立一条 `qlib_dip`，同一天就会落库两套一模一样的 1–30 名次——
    看的人会当成两个模型给了两个结论（§30.6 那个「自己造出重复数据」的坑）。
    """
    fake = tmp_path / "model_batchH_r1"
    fake.mkdir()
    for name in (artifacts.MODEL_FILE, artifacts.NORM_FILE, artifacts.POOL_FILE, artifacts.META_FILE):
        (fake / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv("QLIB_DIP_MODEL_DIR", str(fake))
    monkeypatch.setattr(artifacts, "MODEL_DIR", fake)
    monkeypatch.setattr(artifacts, "load", lambda dest=None: _art(RULE_META, "batchH_r1"))

    art, source = serving.dip_artifact()
    assert art is None
    assert "并入" in source


# ---------------------------------------------------------------- ⑥ 持仓页查名次的原因
def _wire_rank(monkeypatch, art, rows, pool, pcts):
    monkeypatch.setattr(artifacts, "load", lambda *a, **k: art)
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False, wait=True: rows)
    monkeypatch.setattr(serving, "guard_notes", lambda: {})
    monkeypatch.setattr(serving, "score_pool", lambda a: list(pool))
    monkeypatch.setattr(serving, "pct_of_day", lambda syms, date: {s: pcts.get(s) for s in syms})
    monkeypatch.setattr(serving, "calendar", lambda: [PREV, DAY])


def test_rank_endpoint_says_the_population_when_the_arm_gates(monkeypatch):
    """闸门一开，「在候选池里却没名次」的原因就变了：不是缺行情，是今天涨幅没过人口线。

    持仓页看到的都是同一个「没上榜」，但三件事的应对完全不同——缺数要去查行情落库，
    过线是规则（这只票本来就不该由这条臂来说话）。
    """
    hit = {"code": "600001", "qlib_symbol": "SH600001", "date": DAY, "score_raw": 1.0,
           "score": 1.0, "rank": 1, "raw_rank": 1, "universe": 2, "chg": 0.08}
    _wire_rank(monkeypatch, _art(RULE_META, "batchH_r1"), [hit],
               ["SH600001", "SH600002"], {"SH600001": 0.08, "SH600002": 0.012})
    out = serving.ranks_by_code(["600002"], wait=False)
    note = out["items"][0]["note"]
    assert "5.0" in note and "+1.2%" in note
    assert "停牌" not in note and "日线断档" not in note


def test_rank_endpoint_still_says_missing_quotes_when_it_passed_the_gate(monkeypatch):
    """过了人口线却不在榜上 = 当日因子/行情判不出，这句话不能被人口说明顶掉。"""
    _wire_rank(monkeypatch, _art(RULE_META, "batchH_r1"), [],
               ["SH600002"], {"SH600002": 0.080})
    out = serving.ranks_by_code(["600002"], wait=False)
    assert "行情或因子" in out["items"][0]["note"]


def test_rank_endpoint_keeps_old_note_without_a_gate(monkeypatch):
    """老臂（无闸门）那条「停牌或日线断档」的说法一个字都不变。"""
    _wire_rank(monkeypatch, _art(OLD_META, "batchW_c3"), [], ["SH600002"], {"SH600002": 0.012})
    out = serving.ranks_by_code(["600002"], wait=False)
    assert "停牌或日线断档" in out["items"][0]["note"]
