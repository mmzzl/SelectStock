"""test_server.py"""
import httpx
import pytest

from server import app


@pytest.mark.asyncio
async def test_analyze_endpoint_contract():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/analyze", json={
            "user_id": "u_1",
            "natural_day": "2026-09-17",
            "holdings": [
                {"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0},
            ],
        })
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body and "summary" in body
    assert body["results"][0]["code"] == "600001"


@pytest.mark.asyncio
async def test_analyze_invalid_body_rejected():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/analyze", json={"user_id": "u_1"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_quotes_endpoint_contract(monkeypatch):
    """持仓页每次打开都调这个端点：只给价和价的时代，不建 run、不做判定。"""
    import portfolio.pricing as pricing

    monkeypatch.setattr(pricing, "resolve", lambda codes, day: {
        c: {"price": 12.3, "price_time": "20261012150000", "price_source": "live"} for c in codes})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/quotes", json={
            "natural_day": "2026-10-12",
            "holdings": [{"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0}],
        })
    assert resp.status_code == 200
    row = resp.json()["results"][0]
    assert row["latest_price"] == 12.3 and row["price_source"] == "live"
    assert row["price_time"] == "20261012150000"
    assert row["holding_pnl"] == 230.0


@pytest.mark.asyncio
async def test_quotes_endpoint_rejects_empty_holdings():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/quotes", json={"natural_day": "2026-10-12",
                                                                "holdings": []})
    assert resp.status_code == 422
