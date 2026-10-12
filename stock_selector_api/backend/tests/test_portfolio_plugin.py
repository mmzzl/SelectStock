"""插件客户端（mock 模式）：判定+收益快照结构、确定性。M2 前仅占位。"""

from services import portfolio_plugin

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    {"code": "300750", "name": "宁德时代", "quantity": 200, "cost_price": 180.0},
]


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    """替身 httpx.AsyncClient：payload 交给 json.dumps 走一遍，序列化炸了用例就炸。"""

    def __init__(self, captured, body):
        self._captured = captured
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):   # noqa: A002 - 与 httpx 同名参数
        import json as json_mod
        self._captured["url"] = url
        self._captured["payload"] = json
        json_mod.dumps(json)
        return _FakeResponse(self._body)


def _use_http_plugin(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(portfolio_plugin.settings, "plugin_mode", "http")
    monkeypatch.setattr(portfolio_plugin.settings, "plugin_base_url", "http://p:8001")
    monkeypatch.setattr(portfolio_plugin.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(captured, body))
    return captured


async def test_mock_plugin_returns_results_and_summary():
    out = await portfolio_plugin.call_plugin_analyze({"user_id": "u1", "holdings": HOLDINGS})
    assert len(out["results"]) == 3
    for r, h in zip(out["results"], HOLDINGS):
        assert r["code"] == h["code"]
        assert r["action"] in portfolio_plugin.ACTIONS
        assert r["latest_price"] > 0
        assert "holding_pnl" in r and "pnl_pct" in r
    s = out["summary"]
    assert s["holding_count"] == 3
    assert sum(s["action_counts"].values()) == 3


async def test_mock_plugin_replay_deterministic():
    a = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    b = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    assert a == b

async def test_db_holdings_with_datetime_are_canonicalized(monkeypatch):
    """DB 行带 datetime 的 created_at/updated_at：进插件前必须削成契约四字段，否则 httpx json 序列化直接炸。"""
    from datetime import datetime

    captured = _use_http_plugin(monkeypatch, {"results": [], "summary": {}})
    row = {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0,
           "enabled": True, "created_at": datetime(2026, 10, 2, 4, 5, 11),
           "updated_at": datetime(2026, 10, 2, 4, 5, 11)}
    out = await portfolio_plugin.call_plugin_analyze(
        {"user_id": "u1", "natural_day": "2026-10-02", "holdings": [row]})
    assert out["results"] == []
    assert captured["payload"]["holdings"] == [{"code": "600519", "name": "贵州茅台", "quantity": 100,
                                                "cost_price": 1688.0}]


async def test_db_trades_with_datetime_are_canonicalized(monkeypatch):
    """利润分析把交割流水也送给插件：同样得先削成契约字段，否则一导入真实数据就 500。"""
    from datetime import datetime

    captured = _use_http_plugin(monkeypatch, {"per_code": [], "realized_summary": {}, "unrealized_summary": {}})
    trade = {"trade_date": "2026-09-29", "stock_code": "002927", "stock_name": "泰永长征",
             "direction": "sell", "price": 19.7, "quantity": 400, "commission": 5.0,
             "stamp_tax": 3.94, "transfer_fee": 0.0, "settled_amount": 7871.06,
             "user_id": "u1", "source": "import", "batch_id": "b1", "id": "t1",
             "created_at": datetime(2026, 10, 3), "updated_at": datetime(2026, 10, 3)}
    holding = {"code": "002927", "name": "泰永长征", "quantity": 400, "cost_price": 18.0,
               "derived_from_trades": 1, "created_at": datetime(2026, 10, 3)}
    await portfolio_plugin.call_plugin_pnl(
        {"user_id": "u1", "natural_day": "2026-10-03", "trades": [trade], "holdings": [holding]})
    assert captured["url"] == "http://p:8001/api/portfolio/pnl"
    assert captured["payload"]["trades"] == [{
        "trade_date": "2026-09-29", "stock_code": "002927", "stock_name": "泰永长征",
        "direction": "sell", "price": 19.7, "quantity": 400, "commission": 5.0,
        "stamp_tax": 3.94, "transfer_fee": 0.0, "settled_amount": 7871.06}]
    assert captured["payload"]["holdings"] == [{
        "code": "002927", "name": "泰永长征", "quantity": 400, "cost_price": 18.0}]


async def test_quotes_mock_mode_gives_no_prices(monkeypatch):
    """mock 插件没有行情：返回空 results，持仓列表才不会拿假价盖掉诊断快照。"""
    monkeypatch.setattr(portfolio_plugin.settings, "plugin_mode", "mock")
    out = await portfolio_plugin.call_plugin_quotes({"natural_day": "2026-10-12",
                                                     "holdings": HOLDINGS})
    assert out == {"results": []}


async def test_quotes_http_posts_canonical_holdings(monkeypatch):
    captured = _use_http_plugin(monkeypatch, {"results": [
        {"code": "600519", "latest_price": 1400.0, "price_time": "20261012150000",
         "price_source": "live"}]})
    out = await portfolio_plugin.call_plugin_quotes({
        "user_id": "u1", "natural_day": "2026-10-12", "holdings": HOLDINGS})
    assert captured["url"] == "http://p:8001/api/portfolio/quotes"
    assert captured["payload"]["holdings"] == [
        {"code": h["code"], "name": h["name"], "quantity": h["quantity"],
         "cost_price": h["cost_price"]} for h in HOLDINGS]
    assert out["results"][0]["latest_price"] == 1400.0
