import uuid

from repositories import holdings_repo, trades_repo
from services import trades_service


async def _mk_trade_row(code="600519", days=("2026-09-01",), direction="buy", qty=100, price=10.0):
    return {"trade_date": days[0], "stock_code": code, "stock_name": "N",
            "direction": direction, "price": price, "quantity": qty,
            "amount": price * qty, "commission": 0.0, "stamp_tax": 0.0,
            "transfer_fee": 0.0, "settled_amount": price * qty}


async def test_commit_derives_holdings():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    rows = [
        await _mk_trade_row("600519", ("2026-09-01",), "buy", 100, 1600.0),
        await _mk_trade_row("600519", ("2026-09-02",), "buy", 100, 1700.0),
    ]
    out = await trades_service.commit(uid, rows)
    assert out["imported"] == 2 and out["holdings_changed"] == ["600519"] and out["errors"] == []
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert h["quantity"] == 200 and round(h["cost_price"], 2) == 1650.0
    assert h["derived_from_trades"] is True


async def test_commit_oversell_still_imports_but_keeps_holding_untouched():
    """券商对账单只覆盖查询区间：卖出早于买入不能整单拒绝，交易照常入库，该代码持仓不动并给 warning。"""
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "N",
                                "quantity": 300, "cost_price": 99.0, "derived_from_trades": True})
    out = await trades_service.commit(uid, [await _mk_trade_row("600519", direction="sell", qty=10)])
    assert out["imported"] == 1 and out["errors"] == []
    assert out["warnings"] and "600519" in out["warnings"][0]
    assert len(await trades_repo.list_by_user(uid)) == 1
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert (h["quantity"], h["cost_price"]) == (300, 99.0)   # 没被推导成 290 覆盖


async def test_commit_skips_rows_already_in_db():
    """重叠区间的第二份交割单：完全相同的成交不得二次入库（会把持仓翻倍）。"""
    uid = f"u-{uuid.uuid4().hex[:8]}"
    old = [await _mk_trade_row("600519", ("2026-09-01",), "buy", 100, 1600.0),
           await _mk_trade_row("600519", ("2026-09-02",), "buy", 100, 1700.0)]
    assert (await trades_service.commit(uid, old))["imported"] == 2

    overlap = old + [await _mk_trade_row("600519", ("2026-09-03",), "buy", 100, 1800.0)]
    out = await trades_service.commit(uid, overlap)
    assert out["imported"] == 1 and out["duplicates"] == 2
    assert any("重复" in w for w in out["warnings"])
    assert len(await trades_repo.list_by_user(uid)) == 3
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert h["quantity"] == 300 and round(h["cost_price"], 2) == 1700.0


async def test_commit_whole_replay_is_noop():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    rows = [await _mk_trade_row("000001", direction="buy", qty=200, price=10.0)]
    assert (await trades_service.commit(uid, rows))["imported"] == 1
    out = await trades_service.commit(uid, rows)
    assert out["imported"] == 0 and out["duplicates"] == 1
    assert len(await trades_repo.list_by_user(uid)) == 1
    assert (await holdings_repo.find_by_user_code(uid, "000001"))["quantity"] == 200


async def test_commit_keeps_identical_fill_inside_one_statement():
    """同一天同价同量的两笔真单同时出现在一份交割单里：多重集扣减，第二笔不能被当成重复丢掉。"""
    uid = f"u-{uuid.uuid4().hex[:8]}"
    r = await _mk_trade_row("000002", direction="buy", qty=100, price=9.0)
    out = await trades_service.commit(uid, [r, dict(r)])
    assert out["imported"] == 2 and out["duplicates"] == 0
    assert (await holdings_repo.find_by_user_code(uid, "000002"))["quantity"] == 200


async def test_manual_create_and_delete():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    r = await _mk_trade_row()
    created = await trades_service.create_one(uid, r)
    rows = await trades_service.list_trades(uid, {})
    assert len(rows["items"]) == 1
    await trades_service.delete_one(uid, created["id"])
    assert await trades_service.list_trades(uid, {}) == {"items": [], "total": 0}
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert h is None
