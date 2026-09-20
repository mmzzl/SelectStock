"""test_server_pnl.py — /api/portfolio/pnl 端点契约。

行情在测试环境隔离：monkeypatch portfolio.pricing.latest_price 为确定性函数，
避免读真实 parquet / 触发 AKShare 网络（路由内 call-time import 会取到补丁后的属性）。
"""
import portfolio.pricing as pricing
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


def test_pnl_endpoint_ok(monkeypatch):
    monkeypatch.setattr(pricing, "latest_price",
                        lambda code, day: {"600519": 1800.0}.get(code, 1.0))
    r = client.post("/api/portfolio/pnl", json={
        "user_id": "u1", "natural_day": "2026-09-20",
        "trades": [{"trade_date": "2026-09-01", "stock_code": "600519", "stock_name": "茅台",
                    "direction": "buy", "price": 1600.0, "quantity": 100}],
        "holdings": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert "per_code" in body and "realized_summary" in body
    row = body["per_code"][0]
    assert row["code"] == "600519" and row["latest_price"] == 1800.0
    assert row["position"] == 100 and row["avg_cost"] == 1600.0


def test_pnl_holdings_fallback(monkeypatch):
    monkeypatch.setattr(pricing, "latest_price",
                        lambda code, day: {"000001": 12.0}.get(code, 1.0))
    r = client.post("/api/portfolio/pnl", json={
        "user_id": "u1", "natural_day": "2026-09-20", "trades": [],
        "holdings": [{"code": "000001", "name": "平安", "quantity": 1000, "cost_price": 10.0}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["unrealized_summary"]["holding_count"] == 1
    assert body["per_code"][0]["unrealized_pnl"] == 2000.0


def test_pnl_invalid_body_rejected():
    r = client.post("/api/portfolio/pnl", json={"user_id": "u1"})
    assert r.status_code == 422
