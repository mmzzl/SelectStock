"""test_holdings_service.py"""
import pytest

from core.exceptions import BizError
from repositories import holdings_repo
from services import holdings_service


@pytest.fixture
async def _clean():
    for user in ("u1", "u2"):
        await holdings_service.delete_all(user)
    yield


async def test_create_ok(_clean):
    h = await holdings_service.create_holding(
        "u1", {"code": "600519", "name": "茅台", "quantity": 100, "cost_price": 1688.0})
    assert h["enabled"] is True
    assert h["user_id"] == "u1"


async def test_create_duplicate_raises(_clean):
    data = {"code": "600519", "name": "茅台", "quantity": 100, "cost_price": 1688.0}
    await holdings_service.create_holding("u1", data)
    with pytest.raises(BizError):
        await holdings_service.create_holding("u1", data)


async def test_create_200_cap(_clean):
    for i in range(200):
        await holdings_service.create_holding("u2", {
            "code": f"{i:06d}", "name": "t", "quantity": 1, "cost_price": 1.0})
    with pytest.raises(BizError):
        await holdings_service.create_holding("u2", {
            "code": "999999", "name": "t", "quantity": 1, "cost_price": 1.0})


async def test_update_and_delete(_clean):
    h = await holdings_service.create_holding(
        "u1", {"code": "600519", "quantity": 100, "cost_price": 1688.0})
    u = await holdings_service.update_holding("u1", h["id"], {"quantity": 200})
    assert u["quantity"] == 200
    await holdings_service.delete_holding("u1", h["id"])
    assert await holdings_service.find_by_user_code("u1", "600519") is None


async def test_parse_import_valid(_clean):
    text = "代码,名称,数量,成本价\n600519,贵州茅台,100,1688.5\n000858,五粮液,50,142.3"
    res = await holdings_service.parse_import(text)
    assert res.total == 2
    assert res.valid == 2
    assert res.errors == []


async def test_parse_import_row_errors_do_not_abort(_clean):
    text = "代码,名称,数量,成本价\n600519,贵州茅台,x,1688.5\n000858,五粮液,-1,142.3\n000001,平安,10,abc"
    res = await holdings_service.parse_import(text)
    assert res.valid == 0
    assert len(res.errors) == 3


async def test_parse_import_missing_header(_clean):
    with pytest.raises(BizError):
        await holdings_service.parse_import("600519,贵州茅台,100,1688.5")


async def test_apply_import(_clean):
    text = "代码,名称,数量,成本价\n600519,贵州茅台,100,1688.5\n000858,五粮液,50,142.3"
    parsed = await holdings_service.parse_import(text)
    res = await holdings_service.apply_import("u1", parsed)
    assert res.updated == 2
    assert (await holdings_service.find_by_user_code("u1", "600519"))["quantity"] == 100


async def test_apply_import_second_time_updates(_clean):
    text = "代码,名称,数量,成本价\n600519,贵州茅台,200,1688.5"
    parsed = await holdings_service.parse_import(text)
    await holdings_service.apply_import("u2", parsed)
    res = await holdings_service.apply_import("u2", parsed)
    assert res.updated == 1
    rows = await holdings_repo.list_by_user("u2")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 200