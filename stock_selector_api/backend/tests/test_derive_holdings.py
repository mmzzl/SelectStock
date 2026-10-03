from services.derive_holdings import derive_holdings_from_trades


def _trade(day, code, name, direction, qty, price):
    return {"trade_date": day, "stock_code": code, "stock_name": name,
            "direction": direction, "price": price, "quantity": qty}


def test_moving_average():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "buy", 100, 1700.0),
    ])
    assert errs == []
    h = rows[0]
    assert h["stock_code"] == "600519" and h["quantity"] == 200
    assert round(h["cost_price"], 2) == 1650.0  # (1600*100+1700*100)/200


def test_partial_sell_keeps_cost():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "sell", 40, 1800.0),
    ])
    h = rows[0]
    assert h["quantity"] == 60 and h["cost_price"] == 1600.0


def test_oversell_reports_error():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 10, 100.0),
        _trade("09-02", "600519", "茅台", "sell", 99, 110.0),
    ])
    assert rows == [] and any("600519" in e for e in errs)


def test_multiple_codes_grouped():
    rows, _ = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-01", "000858", "五粮液", "buy", 200, 142.0),
    ])
    assert len(rows) == 2


def test_sell_before_buy_makes_whole_code_unreliable():
    """区间开头就是卖出 → 更早的买入无从得知，整只剔除，不给凭空的成本基准。"""
    rows, unreliable = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "sell", 10, 100.0),
        _trade("09-02", "600519", "茅台", "buy", 50, 110.0),
    ])
    assert unreliable == ["600519"]
    assert rows == []


def test_unreliable_code_does_not_affect_others():
    rows, unreliable = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "sell", 10, 100.0),
        _trade("09-02", "000858", "五粮液", "buy", 200, 142.0),
    ])
    assert unreliable == ["600519"]
    assert [(r["stock_code"], r["quantity"]) for r in rows] == [("000858", 200)]


def test_full_sell_drops_holding():
    rows, _ = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "sell", 100, 1700.0),
    ])
    assert rows == []


def test_buy_after_zero_position_resets_cost():
    rows, _ = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "sell", 100, 1700.0),
        _trade("09-03", "600519", "茅台", "buy", 100, 1800.0),
    ])
    h = rows[0]
    assert h["quantity"] == 100 and h["cost_price"] == 1800.0


def test_interleaved_codes_grouped_by_symbol():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-01", "000858", "五粮液", "buy", 200, 142.0),
        _trade("09-02", "600519", "茅台", "buy", 100, 1700.0),
    ])
    assert errs == []
    by_code = {r["stock_code"]: r for r in rows}
    assert by_code["600519"]["quantity"] == 200
    assert round(by_code["600519"]["cost_price"], 2) == 1650.0
    assert by_code["000858"]["quantity"] == 200
    assert by_code["000858"]["cost_price"] == 142.0