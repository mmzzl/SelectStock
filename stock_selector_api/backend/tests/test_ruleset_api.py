"""规则集接口：菜单/权限登记 + CRUD + 应用 的 HTTP 链路。"""
import uuid

import pytest

from services import ruleset_service as svc


class _Sent:
    def __init__(self, task_id):
        self.id = task_id


@pytest.fixture()
def fake_celery(monkeypatch):
    sent = []
    monkeypatch.setattr(svc.celery_app, "send_task",
                        lambda name, args=None, **kw: sent.append(args) or
                        _Sent("replay-" + uuid.uuid4().hex[:6]))
    monkeypatch.setattr(svc.celery_app.control, "revoke", lambda *a, **kw: None)
    return sent


@pytest.fixture(autouse=True)
async def _clean_rule_sets():
    from repositories import rule_sets_repo

    await rule_sets_repo._delete_from("rule_sets", {})
    svc.remove_active_file()
    yield
    await rule_sets_repo._delete_from("rule_sets", {})
    svc.remove_active_file()


def _body(name=None):
    return {"name": name or ("页面规则" + uuid.uuid4().hex[:6]), "remark": "接口用例",
            "disaster_stop_pct": 7.5,
            "sell_conditions": [{"label": "RSI 超买",
                                 "conditions": [{"field": "rsi14", "op": ">", "value": 90}],
                                 },
                                {"label": "跌破 10 日线",
                                 "conditions": [{"field": "close", "op": "<=", "ref": "ma10",
                                                 "coef": 0.95}]}],
            "add_conditions": [{"label": "站上 20 日线",
                                "conditions": [{"field": "close", "op": ">", "ref": "ma20"}]}]}


async def test_seed_registers_permissions_and_menu(client, admin_headers):
    codes = [p["code"] for m in (await client.get("/api/v1/sys/permissions/tree",
                                                  headers=admin_headers)).json()["data"]
             for f in m["features"] for p in f["permissions"]]
    assert {"rule_set:list", "rule_set:create", "rule_set:update",
            "rule_set:delete", "rule_set:apply"} <= set(codes)

    menus = (await client.get("/api/v1/sys/menus/tree", headers=admin_headers)).json()["data"]
    paths = [m["path"] for node in menus for m in _flatten(node)]
    assert "#/rulesets" in paths

    all_roles = (await client.get("/api/v1/sys/roles", headers=admin_headers)).json()["data"]
    readonly = next(x for x in all_roles if x["code"] == "readonly")
    detail = (await client.get(f"/api/v1/sys/roles/{readonly['id']}",
                              headers=admin_headers)).json()["data"]
    # 只读档能看规则集与可信度，但不能改判据、更不能切换线上生效的那套
    assert "rule_set:list" in detail["permission_codes"]
    assert "rule_set:apply" not in detail["permission_codes"]


def _flatten(menu):
    yield menu
    for child in menu.get("children") or []:
        yield from _flatten(child)


async def test_crud_and_apply_over_http(client, admin_headers, fake_celery):
    r = await client.get("/api/v1/rule-sets/options", headers=admin_headers)
    assert r.status_code == 200, r.text
    opt = r.json()["data"]
    assert {"rsi14", "close", "ma20"} <= {f["field"] for f in opt["fields"]}
    assert set(opt["ops"]) == {"<", "<=", ">", ">="}

    r = await client.get("/api/v1/rule-sets/active", headers=admin_headers)
    assert r.json()["data"] is None

    r = await client.post("/api/v1/rule-sets", json=_body(), headers=admin_headers)
    assert r.status_code == 200, r.text
    row = r.json()["data"]
    assert row["sell_expr"] == "rsi14 > 90 or close <= ma10 * 0.95"

    r = await client.get("/api/v1/rule-sets", headers=admin_headers)
    assert row["id"] in [i["id"] for i in r.json()["data"]["items"]]
    # 列表不回传判据串：页面列表只要名字与可信度，串在详情里看
    assert "sell_expr" not in r.json()["data"]["items"][0]

    r = await client.put(f"/api/v1/rule-sets/{row['id']}", json=_body(name="改名后"),
                         headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == "改名后"

    r = await client.post(f"/api/v1/rule-sets/{row['id']}/apply", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["replay"]["status"] == "queued"
    assert fake_celery[0][0]["rule_set_id"] == row["id"]

    r = await client.get("/api/v1/rule-sets/active", headers=admin_headers)
    assert r.json()["data"]["rule_set_id"] == row["id"]

    r = await client.delete(f"/api/v1/rule-sets/{row['id']}", headers=admin_headers)
    assert "已退回默认规则" in r.json()["message"]
    assert not svc.active_path().exists()


async def test_create_rejects_bad_condition(client, admin_headers, fake_celery):
    body = _body()
    body["sell_conditions"] = [{"label": "错列", "conditions": [{"field": "not_a_column",
                                                                 "op": ">", "value": 1}]}]
    r = await client.post("/api/v1/rule-sets", json=body, headers=admin_headers)
    assert r.status_code == 422, r.text
    assert "第 1 条规则" in r.json()["message"]


async def test_missing_rule_set_is_404(client, admin_headers):
    r = await client.get("/api/v1/rule-sets/nope", headers=admin_headers)
    assert r.status_code == 404, r.text


async def test_routes_require_login(client, fake_celery):
    r = await client.post("/api/v1/rule-sets", json=_body())
    assert r.status_code == 401
    assert fake_celery == []
