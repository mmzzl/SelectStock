"""test_analyzer.py"""
import json

from portfolio.analyzer import analyze_holdings


class FakeProvider:
    """确定性行情/指标：每只持仓一行最新指标。"""

    def __init__(self, rows: dict):
        self.rows = rows

    def latest_row(self, code: str) -> dict | None:
        return self.rows.get(code)


HOLDINGS = [
    {"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0},
    {"code": "600002", "name": "乙", "quantity": 50, "cost_price": 20.0},
    {"code": "600003", "name": "丙", "quantity": 10, "cost_price": 30.0},
]

ROWS = {
    "600001": {"close": 9.0, "price": 9.0, "ma10": 10.0, "ma5": 10.0, "ma20": 10.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # sell（9<=9.5）
    "600002": {"close": 25.0, "price": 25.0, "ma10": 24.0, "ma5": 20.0, "ma20": 20.0,
               "rsi14": 50.0, "vol_ma5": 100.0, "volume": 100.0},   # add
    "600003": {"close": 30.0, "price": 30.0, "ma10": 29.0, "ma5": 30.0, "ma20": 29.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # hold
}


def test_analyze_judges_and_computes_revenue():
    out = analyze_holdings(HOLDINGS, FakeProvider(ROWS))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600001"]["action"] == "sell"
    assert by_code["600002"]["action"] == "add"
    assert by_code["600003"]["action"] == "hold"
    # 收益快照由插件算（R1-4）
    assert by_code["600001"]["latest_price"] == 9.0
    assert by_code["600001"]["holding_pnl"] == round((9.0 - 10.0) * 100, 2)
    assert by_code["600002"]["holding_pnl"] == round((25.0 - 20.0) * 50, 2)
    # 卖出价格线：与判定同一条 sell 表达式反算（ma10*0.95）
    assert by_code["600001"]["sell_trigger_price"] == 9.5    # 已触发，线在上方=涨回解除
    assert by_code["600002"]["sell_trigger_price"] == 22.8   # 未触发，线在下方=跌破触发
    assert by_code["600003"]["sell_trigger_price"] == 27.55
    # summary
    s = out["summary"]
    assert s["action_counts"] == {"sell": 1, "add": 1, "hold": 1}
    assert s["total_market_value"] == round(9.0 * 100 + 25.0 * 50 + 30.0 * 10, 2)
    assert s["holding_count"] == 3


def test_analyze_missing_row_sorted_to_sell():
    rows = dict(ROWS)
    del rows["600003"]                       # 行情缺失
    out = analyze_holdings(HOLDINGS, FakeProvider(rows))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600003"]["action"] == "hold"
    assert by_code["600003"]["action_reason"] == "行情缺失，维持原判不做交易建议"
    assert by_code["600003"]["sell_trigger_price"] is None   # 没行情就没法反算，不能编一个价


def test_analyze_swaps_only_the_price_when_live_quote_given():
    """18:00 之前手动触发诊断：指标行还是上一交易日的，判定照旧，但落库的盈亏得按现价算。"""
    out = analyze_holdings(HOLDINGS[:1], FakeProvider(ROWS),
                           quotes={"600001": {"price": 9.6}})
    r = out["results"][0]
    assert r["latest_price"] == 9.6
    assert r["holding_pnl"] == -40.0
    assert r["action"] == "sell"                 # 判定仍来自指标行（close 9.0）
    assert r["sell_trigger_price"] == 9.5


def test_quote_holdings_reports_price_with_time_and_source(monkeypatch):
    from portfolio import pricing
    from portfolio.analyzer import quote_holdings

    monkeypatch.setattr(pricing, "resolve", lambda codes, day: {
        "600001": {"price": 9.5, "price_time": "20261012150000", "price_source": "live"},
        "600002": {"price": 25.0, "price_time": "2026-10-10", "price_source": "kline"},
    })
    out = quote_holdings(HOLDINGS, "2026-10-12")
    by = {r["code"]: r for r in out["results"]}
    assert by["600001"]["latest_price"] == 9.5
    assert by["600001"]["holding_pnl"] == round((9.5 - 10.0) * 100, 2)
    assert by["600001"]["market_value"] == 950.0
    assert by["600001"]["price_source"] == "live"
    assert by["600002"]["price_time"] == "2026-10-10"    # 旧价带着旧日期回来
    assert by["600003"]["latest_price"] is None          # 取不到价就留空，不编一个数
    assert "holding_pnl" not in by["600003"]


def test_quote_holdings_survives_one_code_without_data(monkeypatch):
    """一只票（退市/没建过档案）取不到价不能让整页没有价。"""
    from portfolio import pricing
    from portfolio.analyzer import quote_holdings
    from portfolio.pricing import PricingError

    def resolve(codes, day):
        if len(codes) > 1:
            raise PricingError("整批取价失败")
        if codes[0] == "600003":
            raise PricingError(f"无行情: {codes[0]}")
        return {codes[0]: {"price": 10.0, "price_time": "2026-10-10", "price_source": "kline"}}

    monkeypatch.setattr(pricing, "resolve", resolve)
    out = quote_holdings(HOLDINGS, "2026-10-12")
    by = {r["code"]: r for r in out["results"]}
    assert by["600001"]["latest_price"] == 10.0
    assert by["600003"]["latest_price"] is None


def _hold_row(close=10.5, ma10=11.0, ma5=10.5):
    return {"close": close, "price": close, "ma10": ma10, "ma5": ma5, "ma20": 10.0,
            "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0}


def test_disaster_line_flips_a_hold_into_sell():
    holdings = [{"code": "600009", "name": "丁", "quantity": 100, "cost_price": 12.0}]
    out = analyze_holdings(holdings, FakeProvider({"600009": _hold_row()}))
    r = out["results"][0]
    assert r["action"] == "sell" and "灾难线" in r["action_reason"]


def test_hold_row_carries_quantified_conviction():
    holdings = [{"code": "600009", "name": "丁", "quantity": 100, "cost_price": 10.8}]
    out = analyze_holdings(holdings, FakeProvider({"600009": _hold_row()}))
    r = out["results"][0]
    assert r["action"] == "hold"
    # 线在 ma10*0.95=10.45，现价 10.5 → 卖点在下方 0.5%
    assert "均线卖点在下方 0.5%" in r["suggestion"]
    assert "再亏" in r["suggestion"] and "元" in r["suggestion"]
    assert "自行转卖" in r["suggestion"]
    assert "后 10 日上涨" in r["suggestion"]      # 数字直读 posterior.parquet
    assert "回本需 +2.9%" in r["suggestion"]         # 现价 10.5 低于成本 10.8


def test_closed_form_rules_drop_the_ledger_flip_line(tmp_path, monkeypatch):
    """形态卖出规则关掉后，「hold 点 20 日内 46% 自行转卖」这条必须消失。

    它衡量的是「hold 后来自己变成 sell」，而现在 sell 只剩成本灾难线（台账没有成本价，
    根本复现不出这一格）；parquet 里那个数还是占位规则时代测的，印出来就是假数。
    """
    from portfolio.analyzer import _conviction_line

    monkeypatch.setenv("POSTERIOR_FILE", str(tmp_path / "no_posterior.parquet"))
    text = _conviction_line({"latest_price": 10.5, "action": "hold",
                             "sell_trigger_price": None}, 10.8, 100,
                            form_sell_closed=True)
    assert "自行转卖" not in text
    assert "尾部 p5" in text          # 尾部风险描述的是任意持仓点，不是某条规则的产物，留着
    open_text = _conviction_line({"latest_price": 10.5, "action": "hold",
                                  "sell_trigger_price": None}, 10.8, 100)
    assert "自行转卖" in open_text    # 规则开着时照旧


def test_closed_ruleset_via_active_file(tmp_path, monkeypatch):
    """整条链：active.json 给空列表 → hold、无价格线、不印转卖率。"""
    rules_file = tmp_path / "closed.json"
    rules_file.write_text(json.dumps({"name": "只留灾难线", "sell_rules": [],
                                      "add_rules": [], "disaster_stop_pct": 8.0}),
                          encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(rules_file))
    holdings = [{"code": "600009", "name": "丁", "quantity": 100, "cost_price": 10.8}]
    out = analyze_holdings(holdings, FakeProvider({"600009": _hold_row()}))
    r = out["results"][0]
    assert r["action"] == "hold" and r["sell_trigger_price"] is None
    assert "自行转卖" not in r["suggestion"]


def test_realtime_price_below_trigger_says_so():
    """判定拿日线指标行（所以仍是 hold），价格线拿实时价（已破线）。这时不能写「在下方」反话。

    成本写 10.6 而不是 10.8：实时价 10.2 对 10.8 是浮亏 −5.6%，2026-10-28 灾难线收到 −5% 之后
    那条线会先响、判成 sell，这条用例要测的「均线卖点已被跌破」那句话就轮不到了。
    """
    holdings = [{"code": "600009", "name": "丁", "quantity": 100, "cost_price": 10.6}]
    out = analyze_holdings(holdings, FakeProvider({"600009": _hold_row()}),
                           quotes={"600009": {"price": 10.2}})
    r = out["results"][0]
    assert r["action"] == "hold"
    assert "已跌破均线卖点 2.5%" in r["suggestion"]


def test_sell_row_does_not_quote_hold_baseline():
    """「同类 hold 有 45% 转卖」只描述 hold 那一格，判成 sell 还印它是自己跟自己抬杠。"""
    holdings = [{"code": "600009", "name": "丁", "quantity": 100, "cost_price": 12.0}]
    out = analyze_holdings(holdings, FakeProvider({"600009": _hold_row()}))
    r = out["results"][0]
    assert r["action"] == "sell"
    assert "转为卖出" not in r["suggestion"] and "p5" not in r["suggestion"]
    assert "回本需 +14.3%" in r["suggestion"]


def test_missing_row_still_holds_without_inventing_numbers():
    holdings = [{"code": "600008", "name": "戊", "quantity": 100, "cost_price": 12.0}]
    out = analyze_holdings(holdings, FakeProvider({}))
    r = out["results"][0]
    assert r["action"] == "hold" and r["suggestion"] == ""


class ChipProvider(FakeProvider):
    """带筹码主峰价位的行情提供器（真实实现是 portfolio.provider.KlineProvider）。"""

    def __init__(self, rows: dict, levels: dict):
        super().__init__(rows)
        self.levels = levels

    def chip_levels(self, code: str) -> dict | None:
        return self.levels.get(code)


CHIP = {"600001": {"buy_ref_price": 8.6, "sell_target_price": 9.9, "buy_invalid_price": 7.2,
                   "chip_window": 60, "atr14": 0.7, "peak_weight_pct": 33.0,
                   "close": 9.0, "close_below_peak": False, "peak_gap_pct": 4.65}}
# 现价已经跌出主峰下方（2026-10-28 当日榜 30 只全是这种）
CHIP_BELOW = {"600001": {**CHIP["600001"], "close": 5.0, "close_below_peak": True,
                         "peak_gap_pct": -41.86}}


def test_诊断行带买入价与第一止盈价():
    """持仓诊断也要给出那三档价（用户：「选股和诊断股票的时候，显示买入价格，卖出价格」）。"""
    out = analyze_holdings(HOLDINGS, ChipProvider(ROWS, CHIP))
    r = {x["code"]: x for x in out["results"]}["600001"]
    assert r["buy_ref_price"] == 8.6
    assert r["sell_target_price"] == 9.9      # 卖出价：近 60 日筹码主峰上沿
    assert r["buy_invalid_price"] == 7.2
    assert "筹码主峰" in r["level_note"] and "8.60" in r["level_note"]


def test_诊断行的价位口径也带上主峰在现价上方那句():
    """和名单页同一句措辞（`chips.levels_note`）：只给三个数字会被读成今天挂得进去。"""
    r = {x["code"]: x for x in analyze_holdings(HOLDINGS, ChipProvider(ROWS, CHIP_BELOW))["results"]}["600001"]
    assert "现价在主峰下方 41.9%" in r["level_note"]
    assert len(r["level_note"]) <= 160


def test_算不出主峰时价位留空且说明原因():
    out = analyze_holdings(HOLDINGS, ChipProvider(ROWS, {}))
    r = {x["code"]: x for x in out["results"]}["600001"]
    assert r["buy_ref_price"] is None and r["sell_target_price"] is None
    assert "60" in r["level_note"]           # 说清是「历史不够」，不是「没有这回事」


def test_行情提供器不支持价位时诊断照常出():
    """FakeProvider / 老库缺列时不能整单失败：价位是展示块，缺了就留空。"""
    r = {x["code"]: x for x in analyze_holdings(HOLDINGS, FakeProvider(ROWS))["results"]}["600001"]
    assert r["buy_ref_price"] is None and r["sell_target_price"] is None
    assert r["level_note"]
