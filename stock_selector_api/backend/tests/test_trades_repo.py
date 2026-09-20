from repositories import trades_repo


async def test_create_and_get():
    created = await trades_repo.create({
        "user_id": "u1", "trade_date": "2026-09-01", "stock_code": "600519",
        "stock_name": "贵州茅台", "direction": "buy", "price": 1680.5,
        "quantity": 100, "amount": 168050.0, "commission": 5.0,
        "stamp_tax": 0.0, "transfer_fee": 0.0, "settled_amount": 168055.0,
        "source": "manual", "batch_id": "",
    })
    got = await trades_repo.get(created["id"])
    assert got["stock_code"] == "600519" and got["direction"] == "buy"


async def test_list_by_user_filters():
    base = {"user_id": "u2", "trade_date": "2026-09-01", "stock_code": "600519",
            "stock_name": "N", "price": 10.0, "quantity": 100, "amount": 1000.0,
            "settled_amount": 1005.0, "source": "manual"}
    await trades_repo.create({**base, "direction": "buy"})
    await trades_repo.create({**base, "direction": "sell", "trade_date": "2026-09-05"})
    buys = await trades_repo.list_by_user("u2", direction="buy")
    assert len(buys) == 1 and buys[0]["direction"] == "buy"
    ranged = await trades_repo.list_by_user("u2", date_from="2026-09-02", date_to="2026-09-06")
    assert len(ranged) == 1 and ranged[0]["direction"] == "sell"
    by_code = await trades_repo.list_by_user("u2", code="600519")
    assert len(by_code) == 2