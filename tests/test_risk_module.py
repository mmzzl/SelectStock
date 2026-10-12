"""风控层测试：行情口径读取 → 上下文换算 → 五条规则 → 引擎出决策。

规则里凡是「数据不可得」的分支都单独钉住：风控的失败姿态是**跳过判定并留痕**，
不是拿 0 或 NaN 去比较（那会把复牌股、新股、缺快照的历史日期整批误杀）。
"""

import pandas as pd
import pytest

from stock_selector.risk import context as ctx_mod
from stock_selector.risk import rules as R
from stock_selector.risk import sizing as S
from stock_selector.risk import snapshot as snap
from stock_selector.risk.context import RiskContext, build
from stock_selector.risk.engine import RiskConfig, RiskEngine


def _cfg(**kw) -> RiskConfig:
    base = dict(capital=200000, max_position_pct=0.2, min_avg_amount=3e7,
                max_participation=0.02, max_slippage_bp=100, max_drift_bp=200,
                min_spread_bp=2, probe_order_amount=5e4)
    base.update(kw)
    return RiskConfig(**base)


def _kline(days=30, close=10.0, volume=100000.0, amount=0.0, lu=False, pct=1.0):
    dates = pd.date_range("2026-09-01", periods=days, freq="B")
    return pd.DataFrame({
        "date": dates, "close": [close] * days, "volume": [volume] * days,
        "amount": [amount] * days, "lu": [lu] * days, "pct": [pct] * days,
    })


def _ctx(**kw) -> RiskContext:
    base = dict(code="600000", name="浦发银行", action="buy", signal_price=10.0,
                day="2026-10-12", kline=_kline(), quote={})
    base.update(kw)
    return RiskContext(**base)


# ── snapshot：落盘优先、实时兜底、都没有就空 ────────────────────────────────────

def _write_snapshot(tmp_path, day="2026-10-12"):
    pd.DataFrame([
        {"date": day, "code": "600000", "name": "浦发银行", "asset": "stock", "price": 9.68,
         "pct": 1.47, "volume": 407851.0, "amount": 3.94e8, "turnover_pct": 0.12,
         "vol_ratio": 0.7, "float_mv": 3224.01, "total_mv": 3224.01, "limit_up": 10.49,
         "limit_down": 8.59, "at_limit_up": False, "at_limit_down": False},
        {"date": day, "code": "510300", "name": "沪深300ETF", "asset": "fund", "price": 4.372,
         "pct": -0.3, "volume": 1.0, "amount": 1.0, "turnover_pct": 1.4, "vol_ratio": 0.8,
         "float_mv": 1066.0, "total_mv": 1066.0, "limit_up": 4.824, "limit_down": 3.947,
         "at_limit_up": False, "at_limit_down": False},
    ]).to_parquet(tmp_path / f"{day}.parquet", index=False)


@pytest.fixture
def snap_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RISK_SNAPSHOT_DIR", str(tmp_path))
    snap.clear_cache()
    yield tmp_path
    snap.clear_cache()


def test_snapshot_reads_landed_file(snap_dir):
    _write_snapshot(snap_dir)
    got = snap.get("600000", "2026-10-12", allow_live=False)
    assert got["limit_up"] == 10.49 and got["source"] == "file"
    assert got["asset"] == "stock"


def test_snapshot_missing_day_gives_empty_without_live(snap_dir):
    _write_snapshot(snap_dir)
    # allow_live=False 是回测口径：绝不拿今天的实时行情去判历史那天能不能成交
    assert snap.get("600000", "2026-01-05", allow_live=False) == {}


def test_snapshot_falls_back_to_live(snap_dir, monkeypatch):
    monkeypatch.setattr(snap.tencent, "fetch", lambda codes, **kw: {
        "600000": {"code": "600000", "name": "浦发银行", "price": 9.7, "pct": 1.6,
                   "volume": 1.0, "amount": 1.0, "turnover_pct": 0.1, "vol_ratio": 0.7,
                   "float_mv": 3224.0, "total_mv": 3224.0, "limit_up": 10.49,
                   "limit_down": 8.59, "bids": [(9.69, 10)], "asks": [(9.71, 10)],
                   "quote_time": "20261012130400", "avg_price": 9.66}})
    got = snap.get("600000", "2026-10-12")     # 当天没落盘 → 实时
    assert got["source"] == "live" and got["price"] == 9.7
    assert got["at_limit_up"] is False


def test_limit_hit_uses_half_tick():
    assert snap.limit_hit(10.485, 10.49, True)
    assert not snap.limit_hit(8.60, 8.59, False)
    assert not snap.limit_hit(9.7, None, True)   # 退市/停牌无涨跌停价 → 不算贴板


# ── context：单位与缺数据 ───────────────────────────────────────────────────────

def test_avg_amount_falls_back_to_volume_when_amount_broken():
    """amount 自 2026-05-21 起断供为 0，流动性必须退回 volume(手)×close×100。"""
    c = _ctx(kline=_kline(days=10, close=10.0, volume=100000.0, amount=0.0))
    assert c.avg_amount == pytest.approx(100000 * 10.0 * 100)


def test_avg_amount_drops_suspended_rows():
    df = _kline(days=10, volume=100000.0)
    df.loc[df.index[:5], ["volume", "close"]] = 0.0     # 停牌行整行 0
    assert _ctx(kline=df).avg_amount == pytest.approx(100000 * 10.0 * 100)


def test_avg_amount_none_when_too_short():
    assert _ctx(kline=_kline(days=3)).avg_amount is None


def test_float_mv_converts_from_yi_unit():
    assert _ctx(quote={"float_mv": 3224.01}).float_mv_yuan == pytest.approx(3224.01e8)
    assert _ctx(quote={}).float_mv_yuan is None


def test_daily_vol_falls_back_to_conservative_value():
    """没有日线或波动算不出来时用保守值：波动为 0 会让冲击成本恒为 0，那是假象。"""
    assert _ctx(kline=None).daily_vol_pct == 2.0
    assert _ctx(kline=_kline(pct=1.0)).daily_vol_pct == 2.0


def test_price_prefers_signal_then_quote():
    assert _ctx(signal_price=10.0, quote={"price": 9.68}).price == 10.0
    assert _ctx(signal_price=None, quote={"price": 9.68}).price == 9.68
    assert _ctx(signal_price=None, quote={}).price is None


def test_build_uses_snapshot_by_default(snap_dir, monkeypatch):
    _write_snapshot(snap_dir)
    c = build("600000", "buy", day="2026-10-12", allow_live=False)
    assert c.quote["limit_down"] == 8.59


# ── sizing：额度、整手、滑点 ────────────────────────────────────────────────────

def test_cap_value_subtracts_held_position():
    assert S.cap_value(200000, 0.2, 20000) == pytest.approx(20000)
    # 已超限时按 0，不给负的额度（负的会算出负股数）
    assert S.cap_value(200000, 0.2, 50000) == 0.0


def test_plan_position_rounds_down_to_lots():
    plan = S.plan_position(10.0, 200000, 0.2, 0.0, None)
    assert plan.shares == 4000 and plan.amount == 40000     # 额度 40000 元 / 现价 10 = 整手 4000 股
    assert plan.shares % 100 == 0


def test_plan_position_rounds_partial_lot_down():
    assert S.plan_position(10.5, 200000, 0.2, 0.0, None).shares == 3800   # 40000/10.5=3809→3800


def test_plan_position_caps_request_and_marks_over():
    plan = S.plan_position(10.0, 200000, 0.2, 0.0, 10000)
    assert plan.shares == 4000 and plan.over_cap and "超出单票额度" in plan.reason


def test_plan_position_sell_side_not_capped():
    """卖出能卖多少只看手里有多少，买入额度不该把减仓信号拦掉。"""
    plan = S.plan_position(10.0, 200000, 0.2, 50000, None, enforce_cap=False)
    assert plan.shares == 5000 and not plan.over_cap


def test_no_price_no_shares():
    plan = S.plan_position(None, 200000, 0.2, 0.0, None)
    assert plan.shares is None and "无法折算" in plan.reason


def test_half_spread_prefers_order_book():
    bp = S.half_spread_bp({"bids": [(9.68, 10)], "asks": [(9.72, 10)]}, 9.70)
    assert bp == pytest.approx(0.02 / 9.70 * 1e4)


def test_half_spread_without_book_is_half_tick():
    bp = S.half_spread_bp({}, 10.0)
    assert bp == pytest.approx(0.005 / 10.0 * 1e4)      # 半个 tick = 5bp


def test_impact_is_square_root_of_participation():
    i1 = S.impact_bp(1e4, 1e8, 2.0)
    i4 = S.impact_bp(4e4, 1e8, 2.0)
    assert i4 / i1 == pytest.approx(2.0)                # 单额 4 倍 → 冲击 2 倍
    assert S.impact_bp(1e4, None, 2.0) == 0.0           # 没有日均成交额就不臆造冲击


def test_drift_bp_counts_both_directions():
    assert S.drift_bp(10.0, 10.2) == pytest.approx(200)
    assert S.drift_bp(10.0, 9.8) == pytest.approx(200)
    assert S.drift_bp(None, 9.8) == 0.0


def test_estimate_fill_price_direction():
    buy = S.estimate(10.0, 4e4, quote={}, avg_amount=1e8, daily_vol_pct=2.0,
                     signal_price=10.0, action="buy", min_spread_bp=2.0)
    sell = S.estimate(10.0, 4e4, quote={}, avg_amount=1e8, daily_vol_pct=2.0,
                      signal_price=10.0, action="sell", min_spread_bp=2.0)
    assert buy.fill_price > 10.0 and sell.fill_price < 10.0
    assert buy.total_bp == pytest.approx(buy.spread_bp + buy.impact_bp)


# ── rules：五条各自的通过/拦截/不可得 ──────────────────────────────────────────

def test_tradability_blocks_buy_at_limit_up():
    v = R.tradability(_ctx(quote={"price": 10.49, "at_limit_up": True, "limit_up": 10.49,
                                  "asset": "stock"}), _cfg(), {})
    assert v.blocked and "买不进" in v.reason


def test_tradability_blocks_sell_at_limit_down():
    v = R.tradability(_ctx(action="sell", signal_price=None,
                           quote={"price": 8.59, "at_limit_down": True, "limit_down": 8.59,
                                  "asset": "stock"}), _cfg(), {})
    assert v.blocked and "卖不出" in v.reason


def test_tradability_allows_normal_day():
    v = R.tradability(_ctx(quote={"price": 9.68, "at_limit_up": False, "at_limit_down": False,
                                  "limit_up": 10.49, "limit_down": 8.59, "source": "file",
                                  "asset": "stock"}), _cfg(), {})
    assert v.status == "allow" and v.metrics["limit_up"] == 10.49


def test_tradability_skips_funds():
    """基金不设成交判定，与日线 `lu` 对基金恒为 False 的口径一致。"""
    v = R.tradability(_ctx(quote={"price": 4.37, "asset": "fund", "at_limit_up": True}),
                      _cfg(), {})
    assert v.status == "allow" and v.metrics["skipped"] == "fund"


def test_tradability_falls_back_to_daily_limit_up_column():
    """没有快照（历史日期/回测）时退回日线 `lu`，涨停买不进这条不能失效。"""
    v = R.tradability(_ctx(quote={}, kline=_kline(lu=True)), _cfg(), {})
    assert v.blocked and v.metrics["limit_data"] == "kline_lu"


def test_tradability_sell_side_without_limit_down_data_allows():
    """日线只有涨停没有跌停：卖出侧无口径时放行并留痕，不猜跌停价。"""
    v = R.tradability(_ctx(action="sell", signal_price=None, quote={}, kline=_kline()), _cfg(), {})
    assert v.status == "allow" and v.metrics["limit_down_unknown"] is True


def test_liquidity_blocks_thin_name():
    v = R.liquidity(_ctx(kline=_kline(days=20, volume=1000.0)), _cfg(), {})
    assert v.blocked and "日均成交额" in v.reason


def test_liquidity_allows_liquid_name():
    v = R.liquidity(_ctx(kline=_kline(days=20, volume=1000000.0)), _cfg(), {})
    assert v.status == "allow" and v.metrics["avg_amount"] == pytest.approx(1e6 * 10 * 100)


def test_liquidity_allows_when_amount_unknown():
    v = R.liquidity(_ctx(kline=None), _cfg(), {})
    assert v.status == "allow" and v.metrics["avg_amount"] is None


def test_position_adjusts_oversized_request():
    state = {}
    v = R.position(_ctx(shares=10000, quote={"price": 10.0}, signal_price=10.0), _cfg(), state)
    assert v.status == "adjust" and state["plan"].shares == 4000


def test_position_blocks_when_cap_used_up():
    v = R.position(_ctx(position_value=40000, quote={"price": 10.0}, signal_price=10.0), _cfg(), {})
    assert v.blocked and "额度" in v.reason


def test_position_blocks_without_capital_config():
    """没配总资金时不给建议股数：宁可不给数量，也不编一个默认账户出来。"""
    v = R.position(_ctx(quote={"price": 10.0}), _cfg(capital=0), {})
    assert v.blocked and "risk.capital" in v.reason


def test_slippage_blocks_when_order_eats_the_day():
    state = {"plan": S.PositionPlan(100000, 1e6, 1e6, 0.0, False, "")}
    v = R.slippage(_ctx(quote={"price": 10.0}, kline=_kline(days=20, volume=1000.0)), _cfg(), state)
    assert v.blocked and "占日均成交额" in v.reason


def test_slippage_blocks_stale_signal_price():
    v = R.slippage(_ctx(signal_price=10.0, quote={"price": 10.5}), _cfg(), {})
    assert v.blocked and "偏离" in v.reason


def test_slippage_blocks_when_total_exceeds_cap():
    state = {"plan": S.PositionPlan(4000, 40000, 40000, 0.0, False, "")}
    # 半价差下限 5bp 就超了上限 3bp：滑点上限是配置项，收紧到比最小价差还小时必须拦
    v = R.slippage(_ctx(signal_price=10.0, quote={"price": 10.0},
                        kline=_kline(days=20, volume=100000.0, pct=5.0)), _cfg(
                            max_slippage_bp=3), state)
    assert v.blocked and "预估滑点" in v.reason


def test_slippage_allows_liquid_normal_order():
    state = {"plan": S.PositionPlan(4000, 40000, 40000, 0.0, False, "")}
    v = R.slippage(_ctx(signal_price=10.0, quote={"price": 10.0},
                        kline=_kline(days=20, volume=1000000.0, pct=1.0)), _cfg(), state)
    assert v.status == "allow" and v.metrics["total_slippage_bp"] < 100


def test_model_skips_sell_side():
    """风险分的标签是「买入持有 10 日超额」，卖出侧不在它的论证范围内。"""
    v = R.model(_ctx(action="sell"), _cfg(ml_enabled=True), {})
    assert v.status == "allow" and v.metrics["skipped"] == "sell_side"


def test_model_blocks_below_threshold(monkeypatch):
    monkeypatch.setattr("stock_selector.features.risk_score.decide",
                        lambda df, model_dir=None: (True, -0.02))
    v = R.model(_ctx(), _cfg(ml_enabled=True), {})
    assert v.blocked and "-2.00pp" in v.reason and v.metrics["risk_ex10_pp"] == -2.0


def test_model_allows_when_score_unavailable(monkeypatch):
    monkeypatch.setattr("stock_selector.features.risk_score.decide",
                        lambda df, model_dir=None: (False, None))
    v = R.model(_ctx(), _cfg(ml_enabled=True), {})
    assert v.status == "allow" and v.metrics["risk_score"] is None


def test_model_blocks_when_model_file_missing(monkeypatch):
    """模型缺失是配置故障，必须显式拦下并说明，不能静默放行让闸门形同虚设。"""
    def boom(df, model_dir=None):
        raise RuntimeError("风险分模型不存在")

    monkeypatch.setattr("stock_selector.features.risk_score.decide", boom)
    v = R.model(_ctx(), _cfg(ml_enabled=True), {})
    assert v.blocked and "风险分模型不存在" in v.reason


# ── engine：顺序、短路、决策内容 ────────────────────────────────────────────────

def test_engine_blocks_on_first_failing_rule():
    engine = RiskEngine(_cfg(ml_enabled=False))
    called = []

    def spy(rule):
        def wrapped(c, cfg, state):
            called.append(rule.__name__)
            return rule(c, cfg, state)
        wrapped.__name__ = rule.__name__      # 引擎用规则名打前缀，替身必须保住它
        return wrapped

    engine.rules = tuple(spy(r) for r in R.RULES)
    d = engine.review(_ctx(quote={"price": 10.49, "at_limit_up": True, "limit_up": 10.49,
                                  "asset": "stock"}, signal_price=10.49))
    assert d.blocked and called == ["tradability"]           # 短路：后面的规则不再跑
    assert "tradability" in d.reasons[0]


def test_engine_allows_and_fills_decision_fields():
    engine = RiskEngine(_cfg(ml_enabled=False))
    d = engine.review(_ctx(quote={"price": 10.0, "asset": "stock", "at_limit_up": False,
                                  "at_limit_down": False, "limit_up": 10.49,
                                  "limit_down": 8.59, "source": "file"},
                           signal_price=10.0,
                           kline=_kline(days=20, volume=1000000.0, pct=1.0)))
    assert d.verdict == "allow"
    assert d.shares == 4000 and d.order_amount == 40000
    assert d.est_fill_price and d.est_fill_price > 10.0
    assert d.slippage_bp is not None and d.slippage_bp < 100
    assert [v.name for v in d.checks] == ["tradability", "liquidity", "position", "slippage",
                                          "model"]


def test_engine_disabled_passes_through():
    engine = RiskEngine(_cfg(enabled=False))
    d = engine.review(_ctx(quote={"price": 10.49, "at_limit_up": True, "asset": "stock"}))
    assert d.verdict == "allow" and d.metrics["risk_disabled"] and d.checks == []


def test_decision_extra_is_flat_for_storage():
    engine = RiskEngine(_cfg(ml_enabled=False))
    d = engine.review(_ctx(quote={"price": 10.0, "asset": "stock", "source": "file",
                                  "limit_up": 10.49, "limit_down": 8.59},
                           signal_price=10.0, kline=_kline(days=20, volume=1000000.0, pct=1.0)))
    extra = d.extra()
    assert extra["risk_verdict"] == "allow" and extra["risk_shares"] == 4000
    assert extra["risk_action"] == "buy" and extra["limit_up"] == 10.49
    assert all(not isinstance(v, dict) for v in extra.values())


def test_engine_from_settings_reads_risk_subtree():
    settings = {"risk": {"capital": 500000, "max_position_pct": 0.1, "enabled": True}}
    engine = RiskEngine.from_settings(settings)
    assert engine.cfg.capital == 500000 and engine.cfg.max_position_pct == 0.1


def test_engine_from_settings_without_risk_section_uses_defaults():
    engine = RiskEngine.from_settings({"selector": {}})
    assert engine.cfg.capital == 200000 and engine.cfg.enabled is True


def test_review_signal_builds_context(snap_dir, monkeypatch):
    _write_snapshot(snap_dir)
    monkeypatch.setattr("stock_selector.features.risk_score.decide",
                        lambda df, model_dir=None: (False, None))
    engine = RiskEngine(_cfg())
    d = engine.review_signal("600000", "buy", day="2026-10-12", allow_live=False,
                             kline=_kline(days=20, volume=1000000.0, pct=1.0))
    assert d.verdict in ("allow", "adjust")
    assert d.metrics["limit_up"] == 10.49      # 行情口径确实进了决策
