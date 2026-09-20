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


async def test_commit_oversell_rejects_before_write():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    out = await trades_service.commit(uid, [await _mk_trade_row("600519", direction="sell", qty=10)])
    assert out["imported"] == 0 and out["errors"]
    assert await trades_repo.list_by_user(uid) == []


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
