"""test_holdings_repo.py"""
import pytest

from repositories import holdings_repo


@pytest.fixture
async def _seed():
    for user in ("u1", "u2"):
        for r in await holdings_repo.list_by_user(user):
            await holdings_repo.delete(r["id"])
    await holdings_repo.create({"user_id": "u1", "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1688.0, "enabled": True})
    await holdings_repo.create({"user_id": "u1", "code": "000858", "name": "五粮液",
                                "quantity": 50, "cost_price": 142.0, "enabled": False})
    await holdings_repo.create({"user_id": "u2", "code": "000001", "name": "平安",
                                "quantity": 10, "cost_price": 10.0, "enabled": True})
    yield
    for user in ("u1", "u2"):
        for r in await holdings_repo.list_by_user(user):
            await holdings_repo.delete(r["id"])


async def test_list_by_user_only_owner(_seed):
    rows = await holdings_repo.list_by_user("u1")
    assert {r["code"] for r in rows} == {"600519", "000858"}


async def test_list_enabled_only(_seed):
    rows = await holdings_repo.list_enabled("u1")
    assert [r["code"] for r in rows] == ["600519"]


async def test_find_by_user_code(_seed):
    h = await holdings_repo.find_by_user_code("u1", "600519")
    assert h is not None and h["quantity"] == 100
    assert await holdings_repo.find_by_user_code("u2", "600519") is None


async def test_count_by_user(_seed):
    assert await holdings_repo.count_by_user("u1") == 2
    assert await holdings_repo.count_by_user("u2") == 1