"""持仓诊断服务：幂等重放 / 用户隔离 / 判定收敛透传（零后端算术）。"""

from repositories import portfolio_results, portfolio_runs
from services import portfolio_service

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
]


async def _clean():
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await portfolio_runs._delete_from("portfolio_diag_runs", {})


async def test_analyze_create_and_return_results():
    await _clean()
    out = await portfolio_service.analyze("svc-u1", "2026-09-16", HOLDINGS)
    assert out["run_id"]
    assert out["status"] == "completed"
    assert len(out["results"]) == 2
    for r in out["results"]:
        assert r["action"] in {"sell", "hold", "add"}
        assert "latest_price" in r and "holding_pnl" in r
    await _clean()


async def test_analyze_idempotent_replay():
    await _clean()
    a = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    assert a["run_id"] == b["run_id"]
    runs = await portfolio_runs._find_all({"user_id": "svc-u2"})
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    await _clean()


async def test_analyze_user_isolation_key():
    await _clean()
    a = await portfolio_service.analyze("svc-u3", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u3b", "2026-09-16", HOLDINGS)
    assert a["run_id"] != b["run_id"]
    await _clean()


async def test_list_runs_user_isolation():
    await _clean()
    await portfolio_service.analyze("svc-u4", "2026-09-16", HOLDINGS)
    items, total = await portfolio_service.list_runs("svc-u4")
    assert total >= 1 and all(x["user_id"] == "svc-u4" for x in items)
    items, total = await portfolio_service.list_runs("svc-other")
    assert total == 0
    await _clean()


async def test_sell_trigger_price_roundtrip(monkeypatch):
    """插件反算的卖出价格线必须一路走到接口出参：漏字段会静默变成"没有卖点价"。"""
    await _clean()

    async def fake_plugin(payload):
        return {"results": [{
            "code": "600519", "name": "贵州茅台", "action": "hold", "action_reason": "x",
            "latest_price": 1688.0, "quantity": 100, "holding_pnl": 0.0, "pnl_pct": 0.0,
            "suggestion": "", "sell_trigger_price": 1603.6,
        }], "summary": {"action_counts": {"hold": 1}, "total_market_value": 168800.0,
                        "total_pnl": 0.0, "holding_count": 1}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out = await portfolio_service.analyze("svc-u5", "2026-09-16", HOLDINGS[:1])
    assert out["results"][0]["sell_trigger_price"] == 1603.6
    row = (await portfolio_results.list_by_run(out["run_id"]))[0]
    assert row["sell_trigger_price"] == 1603.6

    # 幂等重放走库里读回来的那条路径，同样要带得上
    again = await portfolio_service.analyze("svc-u5", "2026-09-16", HOLDINGS[:1])
    assert again["results"][0]["sell_trigger_price"] == 1603.6
    await _clean()


async def test_conviction_and_game_fields_roundtrip(monkeypatch):
    """插件新透传的信念/止盈/博弈字段必须落库再出参：漏一个就会静默丢字段。"""
    await _clean()

    extra = {
        "conviction_pct": 72.5, "conviction_level": "高", "conviction_text": "趋势确认",
        "take_profit_price": 1800.0, "game_v": 0.1, "game_s": 0.2, "game_text": "放量上攻",
    }

    async def fake_plugin(payload):
        return {"results": [{
            "code": "600519", "name": "贵州茅台", "action": "hold", "action_reason": "x",
            "latest_price": 1688.0, "quantity": 100, "holding_pnl": 0.0, "pnl_pct": 0.0,
            "suggestion": "", "sell_trigger_price": 1603.6, **extra,
        }], "summary": {"action_counts": {"hold": 1}, "total_market_value": 168800.0,
                        "total_pnl": 0.0, "holding_count": 1}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out = await portfolio_service.analyze("svc-u6", "2026-09-16", HOLDINGS[:1])
    for key, val in extra.items():
        assert out["results"][0][key] == val, key

    row = (await portfolio_results.list_by_run(out["run_id"]))[0]
    for key, val in extra.items():
        assert row[key] == val, key

    # 幂等重放走库里读回来的那条路径，同样要带得上
    again = await portfolio_service.analyze("svc-u6", "2026-09-16", HOLDINGS[:1])
    for key, val in extra.items():
        assert again["results"][0][key] == val, key
    await _clean()


async def test_chip_price_levels_roundtrip(monkeypatch):
    """筹码三档价位 + 口径句一路走到出参；算不出时必须是 None/说明，不能落 0。"""
    await _clean()

    extra = {
        "buy_ref_price": 13.5, "sell_target_price": 14.5, "buy_invalid_price": 12.1,
        "level_note": "近 60 日筹码主峰 13.50~14.50；买入挂下沿、止盈挂上沿",
    }

    async def fake_plugin(payload):
        return {"results": [{
            "code": "600519", "name": "贵州茅台", "action": "hold", "action_reason": "x",
            "latest_price": 1688.0, "quantity": 100, "holding_pnl": 0.0, "pnl_pct": 0.0,
            "suggestion": "", "sell_trigger_price": 1603.6,
            **extra,
        }, {
            "code": "000858", "name": "五粮液", "action": "hold", "action_reason": "x",
            "latest_price": 142.0, "quantity": 50, "holding_pnl": 0.0, "pnl_pct": 0.0,
            "suggestion": "", "sell_trigger_price": None,
            "buy_ref_price": None, "sell_target_price": None, "buy_invalid_price": None,
            "level_note": "近 60 交易日历史不足（新上市或行情缺数），算不出筹码主峰",
        }], "summary": {"action_counts": {"hold": 2}, "total_market_value": 2.0,
                        "total_pnl": 0.0, "holding_count": 2}}

    monkeypatch.setattr("services.portfolio_plugin.call_plugin_analyze", fake_plugin)
    out = await portfolio_service.analyze("svc-u7", "2026-09-16", HOLDINGS)
    got = {r["code"]: r for r in out["results"]}
    first, second = got["600519"], got["000858"]
    for key, val in extra.items():
        assert first[key] == val, key
    assert second["buy_ref_price"] is None and second["sell_target_price"] is None
    assert second["level_note"].startswith("近 60 交易日历史不足")

    rows = await portfolio_results.list_by_run(out["run_id"])
    by_code = {r["code"]: r for r in rows}
    for key, val in extra.items():
        assert by_code["600519"][key] == val, key
    assert by_code["000858"]["buy_invalid_price"] is None

    again = await portfolio_service.analyze("svc-u7", "2026-09-16", HOLDINGS)
    replayed = {r["code"]: r for r in again["results"]}
    assert replayed["600519"]["sell_target_price"] == 14.5
    assert replayed["000858"]["buy_ref_price"] is None
    await _clean()