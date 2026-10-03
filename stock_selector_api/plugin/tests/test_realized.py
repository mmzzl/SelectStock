from collections.abc import Callable

import pytest

from portfolio.realized import compute_pnl


@pytest.fixture
def prices() -> Callable[[str], float]:
    def _fn(code: str) -> float:
        return {"600519": 1800.0, "000001": 12.0}.get(code, 1.0)
    return _fn


def _t(day, code, name, direction, qty, price, fee=0.0, stamp=0.0):
    return {"trade_date": day, "stock_code": code, "stock_name": name,
            "direction": direction, "price": price, "quantity": qty,
            "commission": fee, "stamp_tax": stamp, "transfer_fee": 0.0,
            "settled_amount": 0.0}


def test_realized_on_partial_sell(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-02", "600519", "茅台", "sell", 40, 1800.0, fee=5.0, stamp=72.0),
    ], [], prices)
    row = out["per_code"][0]
    assert row["position"] == 60                                  # 100-40
    assert row["avg_cost"] == 1600.0                              # 卖出不动成本
    assert round(row["realized_pnl"], 2) == 7923.0                # (1800-1600)*40 - 5 - 72
    assert round(row["unrealized_pnl"], 2) == (1800.0 - 1600.0) * 60


def test_moving_average_reused_by_backend_derive(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-02", "600519", "茅台", "buy", 100, 1700.0),
    ], [], prices)
    row = out["per_code"][0]
    assert round(row["avg_cost"], 4) == 1650.0
    assert row["position"] == 200


def test_unrealized_uses_holdings_fallback(prices):
    out = compute_pnl([], [{"code": "000001", "name": "平安", "quantity": 1000, "cost_price": 10.0}], prices)
    row = out["per_code"][0]
    assert row["realized_pnl"] == 0.0
    assert round(row["unrealized_pnl"], 2) == (12.0 - 10.0) * 1000


def test_summaries(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-01", "000001", "平安", "buy", 1000, 10.0),
    ], [], prices)
    assert out["unrealized_summary"]["total_value"] == 1800.0 * 100 + 12.0 * 1000


def test_closed_codes_are_not_priced():
    """已平仓的代码不参与取价：真实交割单三十几个代码，任一外部行情失败都会把整页利润分析拖进 503。"""
    asked = []

    def _fn(code):
        asked.append(code)
        return 12.0

    out = compute_pnl([
        _t("09-01", "000001", "平安", "buy", 100, 10.0),
        _t("09-02", "000001", "平安", "sell", 100, 12.0),      # 已清仓
        _t("09-03", "000002", "万科", "buy", 100, 9.0),       # 仍在持
    ], [], _fn)
    assert asked == ["000002"]
    closed = next(r for r in out["per_code"] if r["code"] == "000001")
    assert closed["position"] == 0
    assert closed["realized_pnl"] == 200.0
    assert closed["unrealized_pnl"] == 0.0
    # 「持仓 N 只」只数还在场的，不能把 76 个交易过的代码全算进去
    assert out["unrealized_summary"]["holding_count"] == 1
