"""规则集：条件编译、应用写盘、回放状态归并，以及回测按 id 注入判据串。

用假的 send_task/_read_celery，测试不碰 Redis；生效文件落在 conftest 的临时目录，
不会碰到仓库 data/rules/active.json（那份是 8002 插件正在读的线上判据）。
"""
import json
import uuid

import pytest

from core.exceptions import BizError, NotFoundError
from repositories import backtest_jobs_repo, rule_sets_repo
from services import backtest_service
from services import ruleset_service as svc


class _Sent:
    def __init__(self, task_id):
        self.id = task_id


class _Calls(list):
    """投递记录（list 语义：sent[0] / len(sent)），顺带挂 revoked 供撤销断言。"""

    revoked: list


def _rsi_high():
    return {"label": "RSI 超买", "conditions": [{"field": "rsi14", "op": ">", "value": 90}]}


def _below_ma10():
    return {"label": "跌破 10 日线 5%",
            "conditions": [{"field": "close", "op": "<=", "ref": "ma10", "coef": 0.95}]}


def _add_above_ma20():
    return {"label": "站上 20 日线再加", "conditions": [{"field": "close", "op": ">", "ref": "ma20"}]}


def _body(**kw):
    data = {"name": "测试规则集" + uuid.uuid4().hex[:6], "remark": "用例",
            "disaster_stop_pct": 8.0,
            "sell_conditions": [_rsi_high(), _below_ma10()],
            "add_conditions": [_add_above_ma20()]}
    data.update(kw)
    return data


@pytest.fixture()
def sent(monkeypatch):
    calls, revoked = _Calls(), []

    def fake_send_task(name, args=None, **kwargs):
        calls.append({"name": name, "args": args})
        return _Sent("replay-" + uuid.uuid4().hex[:6])

    def fake_revoke(task_id, terminate=False, **kwargs):
        revoked.append((task_id, terminate))

    monkeypatch.setattr(svc.celery_app, "send_task", fake_send_task)
    # control.revoke 会往 broker 发广播：测试绝不能真发，否则线上 worker 收到无主撤销
    monkeypatch.setattr(svc.celery_app.control, "revoke", fake_revoke)
    calls.revoked = revoked
    return calls


@pytest.fixture(autouse=True)
async def _clean_rule_sets():
    await rule_sets_repo._delete_from("rule_sets", {})
    svc.remove_active_file()
    yield
    await rule_sets_repo._delete_from("rule_sets", {})
    svc.remove_active_file()


# ------------------------------------------------------------------ 条件 → 表达式

async def test_compile_joins_rules_and_records_fields():
    row = await svc.create_rule_set(_body(), "u1")
    assert row["sell_expr"] == "rsi14 > 90 or close <= ma10 * 0.95"
    assert row["add_expr"] == "close > ma20"
    assert set(row["used_fields"].split(",")) == {"rsi14", "close", "ma10", "ma20"}
    assert [i["label"] for i in row["sell_conditions"]] == ["RSI 超买", "跌破 10 日线 5%"]


async def test_create_requires_at_least_one_sell_rule():
    with pytest.raises(BizError) as exc:
        await svc.create_rule_set(_body(sell_conditions=[]), "u1")
    assert "全部卖出规则都不触发" in exc.value.message or "至少要有一条卖出规则" in exc.value.message
    assert exc.value.status_code == 422


@pytest.mark.parametrize("cond", [
    {"field": "nope", "op": ">", "value": 1},          # 白名单外的列
    {"field": "rsi14", "op": "==", "value": 90},       # 等号：行内求值与向量化会分叉
    {"field": "rsi14", "op": ">"},                     # 既无 ref 也无 value
    {"field": "rsi14", "op": ">", "value": "abc"},     # JSON 标量不是数：不能变成 500
])
async def test_bad_condition_is_422_not_crash(cond):
    with pytest.raises(BizError) as exc:
        await svc.create_rule_set(_body(sell_conditions=[{"label": "坏", "conditions": [cond]}]), "u1")
    assert exc.value.status_code == 422
    assert "第 1 条规则" in exc.value.message


async def test_empty_rule_dropped_and_name_required():
    row = await svc.create_rule_set(
        _body(sell_conditions=[_rsi_high(), {"label": "空", "conditions": []}]), "u1")
    assert row["sell_expr"] == "rsi14 > 90"
    with pytest.raises(BizError):
        await svc.create_rule_set(_body(name="   "), "u1")


# ------------------------------------------------------------------ 应用 / 删除

async def test_apply_writes_active_file_and_dispatches_replay(sent):
    row = await svc.create_rule_set(_body(), "u1")
    out = await svc.apply_rule_set(row["id"], "u1")
    assert out["replay"]["status"] == "queued"
    assert sent[0]["name"] == svc.settings.ruleset_task_name
    params = sent[0]["args"][0]
    assert params["rule_set_id"] == row["id"]
    assert params["rules_file"] == svc.settings.active_rules_file
    assert params["block"] == svc.settings.ruleset_replay_block

    payload = json.loads(svc.active_path().read_text(encoding="utf-8"))
    assert payload["rule_set_id"] == row["id"] and payload["source"] == "admin"
    assert payload["sell"] == "rsi14 > 90 or close <= ma10 * 0.95"
    # 插件按规则逐条判：label 要跟着走，否则告警文案只剩表达式
    assert [r["label"] for r in payload["sell_rules"]] == ["RSI 超买", "跌破 10 日线 5%"]
    assert payload["disaster_stop_pct"] == 8.0      # _body() 显式填了 8.0，照 body 落库
    assert (await rule_sets_repo.get(row["id"]))["replay_status"] == "queued"


async def test_apply_is_exclusive_and_cancels_previous_replay(sent):
    first = await svc.create_rule_set(_body(name="甲"), "u1")
    second = await svc.create_rule_set(_body(name="乙"), "u1")
    out1 = await svc.apply_rule_set(first["id"], "u1")
    out2 = await svc.apply_rule_set(second["id"], "u1")
    assert out2["cancelled_replays"] == [first["id"]]
    assert sent.revoked == [(out1["replay"]["celery_task_id"], True)]
    # 旧那一行要说清「数没了是因为被换掉」，而不是留个永远转圈的排队状态
    old = await rule_sets_repo.get(first["id"])
    assert old["replay_status"] == "failed" and "作废" in old["replay_error"]
    assert not old["is_active"]
    assert (await rule_sets_repo.find_active())["id"] == second["id"]
    assert json.loads(svc.active_path().read_text(encoding="utf-8"))["name"] == "乙"
    assert len(sent) == 2      # 腾出的位置立刻给了新规则集


async def test_apply_keeps_effect_when_replay_dispatch_fails(monkeypatch):
    row = await svc.create_rule_set(_body(), "u1")

    def boom(name, args=None, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(svc.celery_app, "send_task", boom)
    out = await svc.apply_rule_set(row["id"], "u1")
    # 判定此刻真的换了：不能报 503 让人以为什么都没发生
    assert out["replay"]["status"] == "failed" and "redis down" in out["replay"]["error"]
    assert (await rule_sets_repo.get(row["id"]))["is_active"]
    assert svc.active_path().is_file()


async def test_replay_only_for_active_rule_set():
    row = await svc.create_rule_set(_body(), "u1")
    with pytest.raises(BizError) as exc:
        await svc.replay_rule_set(row["id"], "u1")
    assert exc.value.status_code == 409
    assert "只有正在生效" in exc.value.message


async def test_replay_rejects_second_click_while_queued(sent):
    row = await svc.create_rule_set(_body(), "u1")
    await svc.apply_rule_set(row["id"], "u1")
    with pytest.raises(BizError) as exc:
        await svc.replay_rule_set(row["id"], "u1")
    assert exc.value.status_code == 409 and "还在跑" in exc.value.message
    assert len(sent) == 1


async def test_update_active_row_follows_into_file(sent):
    row = await svc.create_rule_set(_body(), "u1")
    await svc.apply_rule_set(row["id"], "u1")
    saved = await svc.update_rule_set(row["id"], _body(sell_conditions=[_rsi_high()], name="改名"))
    assert saved["sell_expr"] == "rsi14 > 90"
    assert json.loads(svc.active_path().read_text(encoding="utf-8"))["sell"] == "rsi14 > 90"
    # 规则换了就是另一套判据，旧回放的数字不能再挂在这行上
    assert saved["replay_status"] == "" and saved["hold_confidence_pct"] is None


async def test_delete_active_row_reverts_to_default(sent):
    row = await svc.create_rule_set(_body(), "u1")
    out = await svc.apply_rule_set(row["id"], "u1")
    # 刚应用完就删：回放还在排队，撤销它而不是等它跑完（那套判据已经作废了）
    result = await svc.delete_rule_set(row["id"])
    assert result["reverted_to_default"] is True
    assert not svc.active_path().exists()
    assert sent.revoked == [(out["replay"]["celery_task_id"], True)]
    with pytest.raises(NotFoundError):
        await svc.get_rule_set(row["id"])


async def test_delete_inactive_row_keeps_effective_file(sent):
    keep = await svc.create_rule_set(_body(name="留下"), "u1")
    junk = await svc.create_rule_set(_body(name="删掉"), "u1")
    await svc.apply_rule_set(keep["id"], "u1")
    out = await svc.delete_rule_set(junk["id"])
    assert out["reverted_to_default"] is False
    assert svc.active_path().is_file()
    assert json.loads(svc.active_path().read_text(encoding="utf-8"))["name"] == "留下"


async def test_rerun_replay_after_previous_finished(sent, monkeypatch):
    row = await svc.create_rule_set(_body(), "u1")
    await svc.apply_rule_set(row["id"], "u1")
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("SUCCESS", _posterior_payload()))
    assert (await svc.get_rule_set(row["id"]))["replay_status"] == "done"
    out = await svc.replay_rule_set(row["id"], "u1")
    assert out["status"] == "queued" and len(sent) == 2


# ------------------------------------------------------------------ 回放状态归并

def _posterior_payload():
    return {
        "computed_at": "2026-10-16T10:00:00+00:00", "name": "测试", "rule_set_id": "rs-1",
        "universe": 111, "hold_confidence_pct": 33.6, "hold_confidence_note": "样本充足",
        "joint_hold": {"n": 120000, "days": 300, "flat_pct": 33.6, "base_pct": 34.83,
                       "lift": 0.965, "vs_base_t": -8.54, "tail8_pct": 6.1,
                       "tail8_lift": 0.9, "enough": True},
        "sell_rules": [{"label": "RSI 超买", "expr": "rsi14 > 90", "n": 512, "days": 210,
                        "flat_pct": 14.4, "base_pct": 34.83, "lift": 0.413, "vs_base_t": -12.1,
                        "fly_pct": 85.6, "tail8_pct": 3.1, "tail8_lift": 0.42,
                        "median_ret_pct": 2.4, "exc20_pp": -6.1, "enough": True}],
        "add_rules": [],
    }


async def test_sync_replay_progress_then_success_stores_brief(sent, monkeypatch):
    row = await svc.create_rule_set(_body(), "u1")
    await svc.apply_rule_set(row["id"], "u1")

    state = {"state": "PROGRESS", "payload": {"progress": 37, "stage": "回放 3/8 块"}}
    monkeypatch.setattr(svc, "_read_celery", lambda tid: (state["state"], state["payload"]))

    detail = await svc.get_rule_set(row["id"])
    assert detail["replay_status"] == "running" and detail["replay_progress"] == 37
    assert detail["replay_stage"] == "回放 3/8 块"

    state["state"], state["payload"] = "SUCCESS", _posterior_payload()
    detail = await svc.get_rule_set(row["id"])
    assert detail["replay_status"] == "done"
    assert detail["hold_confidence_pct"] == 33.6
    assert detail["hold_confidence_base_pct"] == 34.83
    assert detail["hold_confidence_lift"] == 0.965
    assert detail["hold_confidence_t"] == -8.54
    stored = json.loads((await rule_sets_repo.get(row["id"]))["posterior"])
    assert stored["sell_rules"][0]["fly_pct"] == 85.6
    assert "equity" not in json.dumps(stored)
    # 详情页要能列出逐条统计，列表不带（一页 20 行没必要拖 JSON）
    assert detail["posterior"]["sell_rules"][0]["label"] == "RSI 超买"
    assert detail["posterior"]["sell_rules"][0]["lift"] == 0.413   # 逐条也用 lift+t 判区分度
    assert "posterior" not in (await svc.list_rule_sets(1, 20))["items"][0]

    state["state"], state["payload"] = "PROGRESS", {"progress": 1}
    # 终态后不再查 Celery
    assert (await svc.get_rule_set(row["id"]))["replay_status"] == "done"


async def test_sync_replay_failure_and_stale(sent, monkeypatch):
    row = await svc.create_rule_set(_body(), "u1")
    await svc.apply_rule_set(row["id"], "u1")
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("FAILURE", "RuntimeError: parquet 缺失"))
    detail = await svc.get_rule_set(row["id"])
    assert detail["replay_status"] == "failed" and "parquet 缺失" in detail["replay_error"]

    other = await svc.create_rule_set(_body(name="丙"), "u1")
    await svc.apply_rule_set(other["id"], "u1")
    monkeypatch.setattr(svc, "_read_celery", lambda tid: ("PENDING", None))
    assert (await svc.get_rule_set(other["id"]))["replay_status"] == "queued"
    monkeypatch.setattr(svc, "_age_seconds", lambda row: svc.STALE_SECONDS + 1)
    detail = await svc.get_rule_set(other["id"])
    assert detail["replay_status"] == "failed" and "无响应" in detail["replay_error"]


# ------------------------------------------------------------------ 表单与快照

async def test_options_exposes_field_catalog():
    opt = svc.options()
    names = {f["field"] for f in opt["fields"]}
    assert {"rsi14", "close", "ma20", "atr14"} <= names
    assert set(opt["ops"]) == {"<", "<=", ">", ">="}
    assert opt["replay_contract"]["min_samples"] == 200
    assert opt["active_file"]["path"] == svc.settings.active_rules_file


async def test_disaster_default_is_the_minus_five_ruler(sent):
    """没填灾难线就落 −5%：2026-10-28 用户点名全项目统一这把尺子，后端四处默认值原来是 8.0。

    表单默认值（options）和新建落库（create→active.json）要一起走：只改一边，
    管理页新建一条不填灾难线的规则集、点「应用」就会把线上的 5% 顶回 8%。
    """
    body = _body()
    body.pop("disaster_stop_pct")
    row = await svc.create_rule_set(body, "u1")
    assert row["disaster_stop_pct"] == 5.0
    await svc.apply_rule_set(row["id"], "u1")
    payload = json.loads(svc.active_path().read_text(encoding="utf-8"))
    assert payload["disaster_stop_pct"] == 5.0
    opt = svc.options()
    assert opt["disaster"]["default"] == 5.0
    assert opt["replay_contract"]["tail_threshold_pct"] == 5.0


async def test_snapshot_by_id_and_active(sent):
    row = await svc.create_rule_set(_body(), "u1")
    assert await svc.active_snapshot() is None
    await svc.apply_rule_set(row["id"], "u1")
    snap = await svc.active_snapshot()
    assert snap["rule_set_id"] == row["id"] and snap["sell"] == row["sell_expr"]
    assert (await svc.snapshot_by_id(row["id"]))["disaster_stop_pct"] == 8.0
    with pytest.raises(NotFoundError):
        await svc.snapshot_by_id("nope")


# ------------------------------------------------------------------ 回测联动

def _bt_params():
    # target_amount/rule_set_id 都是自研内核的键，qlib 引擎不读，必须点名 engine=self
    return {"engine": "self", "start": "2026-01-01", "end": "2026-02-01",
            "capital": 50000, "target_amount": 5000}


@pytest.fixture()
def bt_sent(monkeypatch):
    calls = []

    def fake_send_task(name, args=None, **kwargs):
        calls.append({"name": name, "args": args})
        return _Sent("bt-" + uuid.uuid4().hex[:6])

    monkeypatch.setattr(backtest_service.celery_app, "send_task", fake_send_task)
    return calls


async def test_backtest_injects_snapshot_server_side(bt_sent):
    row = await svc.create_rule_set(_body(), "u1")
    uid = "bt-" + uuid.uuid4().hex[:8]
    out = await backtest_service.start_run(uid, {**_bt_params(), "rule_set_id": row["id"]})
    job = await backtest_jobs_repo.get(out["job_id"])
    params = json.loads(job["params"])
    assert params["sell_rule"] == "rsi14 > 90 or close <= ma10 * 0.95"
    assert params["add_rule"] == "close > ma20"
    assert bt_sent[0]["args"][0]["rule_set_id"] == row["id"]


async def test_backtest_rejects_forged_rule_string(bt_sent):
    uid = "bt-" + uuid.uuid4().hex[:8]
    with pytest.raises(BizError) as exc:
        await backtest_service.start_run(uid, {**_bt_params(), "sell_rule": "close > 1"})
    assert exc.value.status_code == 400
    assert bt_sent == []


async def test_backtest_rejects_rule_set_with_strategy(bt_sent):
    row = await svc.create_rule_set(_body(), "u1")
    uid = "bt-" + uuid.uuid4().hex[:8]
    with pytest.raises(BizError):
        await backtest_service.start_run(
            uid, {**_bt_params(), "rule_set_id": row["id"], "strategy": "pattern_reversal"})
    assert bt_sent == []


async def test_backtest_without_rule_set_keeps_mongo_rules(bt_sent):
    uid = "bt-" + uuid.uuid4().hex[:8]
    out = await backtest_service.start_run(uid, _bt_params())
    params = json.loads((await backtest_jobs_repo.get(out["job_id"]))["params"])
    assert "rule_set_id" not in params and "sell_rule" not in params
