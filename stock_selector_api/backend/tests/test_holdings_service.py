"""test_holdings_service.py"""
import pytest

from core.exceptions import BizError
from repositories import holdings_repo
from services import holdings_service


@pytest.fixture(autouse=True)
async def _clean():
    for user in ("u1", "u2"):
        await holdings_service.delete_all(user)
    yield


@pytest.mark.parametrize("qty,cost", [(100, 10.0), (0, 10.0), (100, 0.0)])
async def test_create_ok(qty, cost):
    h = await holdings_service.create_holding("u1", {"code": "600519", "name": "茅台",
                                                     "quantity": qty, "cost_price": cost})
    assert h["enabled"] is True
    assert h["user_id"] == "u1"


async def test_create_duplicate_code_rejected():
    await holdings_service.create_holding("u1", {"code": "600519", "quantity": 100, "cost_price": 1})
    with pytest.raises(BizError):
        await holdings_service.create_holding("u1", {"code": "600519", "quantity": 200, "cost_price": 2})


async def test_create_200_cap():
    await holdings_service.delete_all("u2")  # 起点 0
    for i in range(200):
        await holdings_service.create_holding("u2", {"code": f"{i:06d}", "quantity": 1, "cost_price": 1})
    with pytest.raises(BizError):
        await holdings_service.create_holding("u2", {"code": "999999", "quantity": 1, "cost_price": 1})


async def test_update_delete_and_owner_guard(_clean):
    h = await holdings_service.create_holding("u1", {"code": "600519", "quantity": 100, "cost_price": 1688.0})
    u = await holdings_service.update_holding("u1", h["id"], {"quantity": 200})
    assert u["quantity"] == 200
    with pytest.raises(BizError):
        await holdings_service.update_holding("u2", h["id"], {"quantity": 1})
    with pytest.raises(BizError):
        await holdings_service.delete_holding("u2", h["id"])
    await holdings_service.delete_holding("u1", h["id"])
    assert await holdings_service.find_by_user_code("u1", "600519") is None


def test_parse_import_valid():
    text = "code,name,quantity,cost_price\n600519,贵州茅台,100,1688.5\n000858,五粮液,50,142\n"
    result = holdings_service.parse_import(text, strategy="upsert")
    assert result["total"] == 2
    assert result["valid"] == 2
    assert result["errors"] == []


def test_parse_import_row_errors_do_not_abort():
    text = "code,name,quantity,cost_price\nabc,坏码,100,1\n600519,茅台,x,1688.5\n600000,平安,10,-5\n"
    result = holdings_service.parse_import(text, strategy="skip")
    assert result["valid"] == 0
    assert len(result["errors"]) == 3


def test_parse_import_missing_header():
    with pytest.raises(BizError):
        holdings_service.parse_import("a,b,c\n1,2,3\n", strategy="skip")


def test_parse_import_bad_strategy():
    with pytest.raises(BizError):
        holdings_service.parse_import("code,name,quantity,cost_price\n1,2,3,4\n", strategy="merge")


async def test_apply_import_upsert(_clean):
    text = "code,name,quantity,cost_price\n600519,贵州茅台,100,1688.5\n000858,五粮液,50,142\n"
    res = await holdings_service.apply_import("u1", text, "upsert")
    assert res["updated"] == 2 and res["valid"] == 2
    assert (await holdings_service.find_by_user_code("u1", "600519"))["quantity"] == 100


async def test_apply_import_second_time_updates(_clean):
    text = "code,name,quantity,cost_price\n600519,贵州茅台,200,1688.5\n"
    await holdings_service.apply_import("u2", text, "upsert")
    res = await holdings_service.apply_import("u2", text, "upsert")
    assert res["updated"] == 1
    rows = await holdings_repo.list_by_user("u2")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 200


async def test_apply_import_skip_skips(_clean):
    text = "code,name,quantity,cost_price\n600519,贵州茅台,200,1688.5\n"
    await holdings_service.apply_import("u1", text, "upsert")
    res = await holdings_service.apply_import("u1", text, "skip")
    assert res["skipped"] == 1 and res["updated"] == 0
    assert (await holdings_service.find_by_user_code("u1", "600519"))["quantity"] == 200