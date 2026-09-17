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
