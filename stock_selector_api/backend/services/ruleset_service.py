"""services/ruleset_service.py — 规则集 CRUD、「应用」写盘、全市场回放投递。

后端在这条链上只做一件事：把页面的结构化条件拼成表达式串并校验（文法真源
`stock_selector/backtest/rule_expr.py`，与线上判定、台账、回放同一份），然后把
「这套规则现在生效」写进 data/rules/active.json。统计本身交给 worker 跑
scripts/ruleset_posterior.py——一次全市场回放要扫 6,600 个 parquet，不能在 API 进程里做。

两条口径要说清（跟用户定方案 B 时确认过的边界一致）：
- 「应用」是排他的：同一时刻只有一行 is_active，active.json 也只有一份；
- 删掉正在生效的那行 = 撤销应用，文件删掉，插件与台账自动退回默认规则
  （默认那两条没标定过，见 portfolio/rules.py 的说明，所以这里不假装它还是一条可用判据）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from celery.result import AsyncResult

from celery_app import celery_app
from config import settings
from core.exceptions import BizError, NotFoundError
from core.rule_grammar import rule_expr
from repositories import rule_sets_repo

logger = logging.getLogger("ruleset_service")

MAX_RULES_PER_SIDE = 8
MAX_CONDITIONS_PER_RULE = 6
TERMINAL = ("done", "failed")

# 排队/运行中的回放长时间拿不到任何 Celery 状态（worker 掉线）就判失败，
# 否则这条规则集永远显示「回放中」，页面既不能重跑也看不出为什么没数
STALE_SECONDS = 3 * 3600


# ------------------------------------------------------------------ 条件 → 表达式

def _cond_error(where: str, idx: int, exc: Exception) -> BizError:
    return BizError(f"{where}第 {idx + 1} 条规则不合法：{exc}", code=422, status_code=422)


def _compile_side(raw, where: str) -> tuple[list[dict], str, set[str]]:
    """结构化条件列表 → ([{"label","expr"}...], OR 串, 用到的字段集合)。"""
    re_mod = rule_expr()
    items = list(raw or [])
    if len(items) > MAX_RULES_PER_SIDE:
        raise BizError(f"{where}最多 {MAX_RULES_PER_SIDE} 条规则", code=422, status_code=422)
    out: list[dict] = []
    fields: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise BizError(f"{where}第 {i + 1} 项不是对象", code=422, status_code=422)
        conds = list(item.get("conditions") or [])
        if not conds:
            continue                       # 空规则丢掉：一条都不填等于没有这条规则
        if len(conds) > MAX_CONDITIONS_PER_RULE:
            raise BizError(f"{where}第 {i + 1} 条规则最多 {MAX_CONDITIONS_PER_RULE} 个条件",
                           code=422, status_code=422)
        try:
            expr = re_mod.build_expression(conds)
            fields |= set(re_mod.validate(expr))
        except (re_mod.RuleSyntaxError, ValueError, TypeError) as exc:
            # ValueError/TypeError 也归到这里：条件里的 coef/value 是页面传来的 JSON 标量，
            # "abc" 这种值会在 float() 上炸，直接冒出去就是一个 500 而不是「这条规则写错了」。
            raise _cond_error(where, i, exc) from None
        label = str(item.get("label") or "").strip() or f"{where}规则{i + 1}"
        out.append({"label": label[:40], "conditions": conds, "expr": expr})
    exprs = [i["expr"] for i in out]
    return out, re_mod.join_rules(exprs), fields


def _compile(sell_raw, add_raw) -> dict:
    sells, sell_expr, f1 = _compile_side(sell_raw, "卖出")
    adds, add_expr, f2 = _compile_side(add_raw, "加仓")
    if not sells:
        raise BizError("至少要有一条卖出规则：「全部卖出规则都不触发」这个状态无从定义",
                       code=422, status_code=422)
    return {
        "sell_conditions": json.dumps([{"label": i["label"], "conditions": i["conditions"]}
                                       for i in sells], ensure_ascii=False),
        "add_conditions": json.dumps([{"label": i["label"], "conditions": i["conditions"]}
                                      for i in adds], ensure_ascii=False),
        "sell_expr": sell_expr,
        "add_expr": add_expr,
        "used_fields": ",".join(sorted(f1 | f2))[:500],
    }


def _rule_items(json_text: str) -> list[dict]:
    """存储的结构化条件 → active.json 要的 [{"label","expr"}]（每次现拼，不缓存串）。"""
    re_mod = rule_expr()
    out = []
    for i, item in enumerate(json.loads(json_text or "[]")):
        conds = item.get("conditions") or []
        expr = re_mod.build_expression(conds)
        if expr:
            out.append({"label": item.get("label") or f"规则{i + 1}", "expr": expr})
    return out


# ------------------------------------------------------------------ 生效文件

def active_path() -> Path:
    return Path(settings.active_rules_file)


def posterior_path() -> Path:
    return Path(settings.ruleset_posterior_file)


def write_active_file(row: dict) -> Path:
    """原子写：插件按 (mtime,size) 热读，半截文件会让它一次读不到 JSON 就退回默认规则。"""
    payload = {
        "rule_set_id": row["id"],
        "name": row["name"],
        "source": "admin",
        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sell": row["sell_expr"],
        "add": row["add_expr"],
        "sell_rules": _rule_items(row["sell_conditions"]),
        "add_rules": _rule_items(row["add_conditions"]),
        "disaster_stop_pct": float(row["disaster_stop_pct"]),
    }
    path = active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def remove_active_file() -> None:
    active_path().unlink(missing_ok=True)


# ------------------------------------------------------------------ CRUD

def _loads(text: str):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return None


def _decorate(row: dict, full: bool) -> dict:
    out = dict(row)
    out["sell_conditions"] = _loads(row.get("sell_conditions")) or []
    out["add_conditions"] = _loads(row.get("add_conditions")) or []
    summary = _loads(row.get("posterior"))
    out.pop("posterior", None)
    if summary:
        joint = summary.get("joint_hold") or {}
        out["hold_confidence_pct"] = summary.get("hold_confidence_pct")
        out["hold_confidence_note"] = summary.get("hold_confidence_note") or ""
        out["hold_confidence_base_pct"] = joint.get("base_pct")
        out["hold_confidence_lift"] = joint.get("lift")
        out["hold_confidence_t"] = joint.get("vs_base_t")
    else:
        out["hold_confidence_pct"] = None
    if full:
        # 详情页要展示逐条回放统计（每条卖出规则各自有没有区分度），列表只留合计那几个数
        out["posterior"] = summary
    else:
        for key in ("sell_expr", "add_expr", "celery_task_id"):
            out.pop(key, None)
    return out


async def list_rule_sets(page: int = 1, size: int = 20) -> dict:
    rows, total = await rule_sets_repo.list_all(page, size)
    items = [_decorate(await _sync_replay(row), full=False) for row in rows]
    return {"items": items, "total": total}


async def get_rule_set(rule_set_id: str) -> dict:
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    return _decorate(await _sync_replay(row), full=True)


async def create_rule_set(body: dict, user_id: str) -> dict:
    compiled = _compile(body.get("sell_conditions"), body.get("add_conditions"))
    data = {
        "name": str(body.get("name") or "").strip(),
        "remark": str(body.get("remark") or "")[:200],
        "disaster_stop_pct": float(body.get("disaster_stop_pct") or 8.0),
        **compiled,
    }
    if not data["name"]:
        raise BizError("规则集名称不能为空", code=422, status_code=422)
    row = await rule_sets_repo.create(data)
    logger.info("规则集已新建：%s（%s）by %s", row["name"], row["id"], user_id)
    return _decorate(row, full=True)


async def update_rule_set(rule_set_id: str, body: dict) -> dict:
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    compiled = _compile(body.get("sell_conditions"), body.get("add_conditions"))
    patch = {
        "name": str(body.get("name") or row["name"]).strip()[:64],
        "remark": str(body.get("remark") if body.get("remark") is not None
                      else row["remark"])[:200],
        "disaster_stop_pct": float(body.get("disaster_stop_pct") or row["disaster_stop_pct"]),
        **compiled,
    }
    # 规则改了就等于换了一套判据：旧回放数字描述的是上一条规则，留着就是在骗人
    patch.update({"replay_status": "", "replay_progress": 0, "replay_stage": "",
                  "replay_error": "", "posterior": ""})
    saved = await rule_sets_repo.update(rule_set_id, patch)
    if row.get("is_active"):
        write_active_file({**row, **patch})       # 生效中的规则被改，文件必须跟着改
    return _decorate(saved, full=True)


async def delete_rule_set(rule_set_id: str) -> dict:
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    if row.get("replay_status") in ("queued", "running"):
        # 删掉就是不要这套判据了，跑到一半的数字没人会再看：撤掉，别占着唯一那路队列
        await _revoke(row.get("celery_task_id"))
    await rule_sets_repo.delete(rule_set_id)
    reverted = False
    if row.get("is_active"):
        # 撤销生效：删掉文件，插件与台账退回默认规则（没有新的应用就用默认的）
        remove_active_file()
        posterior_path().unlink(missing_ok=True)
        reverted = True
    return {"deleted": rule_set_id, "reverted_to_default": reverted}


# ------------------------------------------------------------------ 应用 + 回放

async def _revoke(task_id: str) -> None:
    """撤销 Celery 回放（排队中就丢弃，已在跑就终止）。terminate 需要 prefork 池；
    撤不动也不算错——大不了这次回放白跑，产物按 rule_set_id 归在旧行上，不会串到新的生效规则。
    """
    if not task_id:
        return
    try:
        await asyncio.to_thread(celery_app.control.revoke, task_id, terminate=True)
    except Exception as exc:  # noqa: BLE001  broker 不可达时删除/应用仍应成功
        logger.warning("撤销回放 %s 失败：%s", task_id, exc)


async def _cancel_replaying(except_id: str, reason: str) -> list[str]:
    """换规则时作废别的在跑回放：那一轮测的是已经被换掉的判据，继续跑只是占内存。

    这台机器上还有线上服务，全市场回放只允许一路（两路 12M 行扫描会把内存吃穿），
    所以「作废旧的」同时也就是「给新的腾位置」。
    """
    cancelled: list[str] = []
    for job in await rule_sets_repo.find_replaying():
        if job["id"] == except_id:
            continue
        await _revoke(job.get("celery_task_id"))
        await rule_sets_repo.update(job["id"], {
            "replay_status": "failed", "replay_progress": 0, "replay_stage": "",
            "replay_error": reason})
        cancelled.append(job["id"])
    return cancelled

async def apply_rule_set(rule_set_id: str, user_id: str) -> dict:
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    if not row.get("sell_expr"):
        raise BizError("这条规则集没有卖出规则，无法应用", code=422, status_code=422)
    # 先作废在跑的回放：既腾出唯一那路队列，也避免旧规则的数覆盖到新规则头上
    cancelled = await _cancel_replaying(except_id=rule_set_id, reason="应用了另一套规则，这轮回放作废")
    await rule_sets_repo.deactivate_all(except_id=rule_set_id)
    await rule_sets_repo.update(rule_set_id, {"is_active": True})
    write_active_file({**row, "is_active": True})
    logger.info("规则集 %s（%s）已应用，生效判定与可信度都改用这套", row["name"], rule_set_id)
    try:
        replay = await _dispatch_replay(rule_set_id, user_id)
    except BizError as exc:
        # 这里不能把错误抛回页面：active.json 已经写盘，线上判定此刻真的换了。
        # 报 503 让人以为「什么都没发生」比报「已应用、回放没跑」危险得多。
        logger.warning("规则集 %s 已生效但回放投递失败：%s", rule_set_id, exc)
        replay = {"status": "failed", "celery_task_id": "", "error": exc.message}
    return {"applied": rule_set_id, "name": row["name"], "replay": replay,
            "cancelled_replays": cancelled}


async def replay_rule_set(rule_set_id: str, user_id: str) -> dict:
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    if not row.get("is_active"):
        raise BizError("只有正在生效的规则集才跑回放：可信度描述的是「当前这套规则」",
                       code=409, status_code=409)
    if row.get("replay_status") in ("queued", "running"):
        raise BizError(f"这轮回放还在跑（{row.get('replay_stage') or row['replay_status']}），"
                       f"请等它结束", code=409, status_code=409)
    return await _dispatch_replay(rule_set_id, user_id)


async def _dispatch_replay(rule_set_id: str, user_id: str) -> dict:
    """投递全市场回放（调用方已保证同一路列没有别的老回放占位）。"""
    row = await rule_sets_repo.get(rule_set_id)
    params = {"rule_set_id": rule_set_id, "name": row["name"],
              "rules_file": settings.active_rules_file,
              "out_file": settings.ruleset_posterior_file,
              "block": settings.ruleset_replay_block}
    await rule_sets_repo.update(rule_set_id, {
        "replay_status": "queued", "replay_progress": 0, "replay_stage": "排队中",
        "replay_error": "", "celery_task_id": ""})
    try:
        result = await asyncio.to_thread(
            celery_app.send_task, settings.ruleset_task_name, args=[params])
    except Exception as exc:  # noqa: BLE001  broker 不可达
        message = f"回放投递失败: {exc}"[:512]
        await rule_sets_repo.update(rule_set_id, {"replay_status": "failed",
                                                  "replay_error": message})
        raise BizError(f"{message}（请确认 Celery worker 与 Redis 已启动）",
                       code=503, status_code=503) from exc
    await rule_sets_repo.update(rule_set_id, {"celery_task_id": result.id})
    logger.info("规则集 %s 的回放已投递（task %s, by %s）", rule_set_id, result.id, user_id)
    return {"celery_task_id": result.id, "status": "queued"}


def _read_celery(task_id: str) -> tuple[str, object]:
    ar = AsyncResult(task_id, app=celery_app)
    return ar.state, (ar.info if ar.state == "PROGRESS" else ar.result)


def _age_seconds(row: dict) -> float:
    value = row.get("updated_at") or row.get("created_at")
    if not isinstance(value, datetime):
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()


def _brief(summary: dict) -> dict:
    """只把列表页要渲染的那几个数留在库里，整份分布留在 posterior 文件里。"""
    joint = summary.get("joint_hold") or {}
    return {
        "computed_at": summary.get("computed_at"),
        "name": summary.get("name"),
        "rule_set_id": summary.get("rule_set_id"),
        "universe": summary.get("universe"),
        "hold_confidence_pct": summary.get("hold_confidence_pct"),
        "hold_confidence_note": summary.get("hold_confidence_note"),
        "joint_hold": {k: joint.get(k) for k in ("n", "days", "flat_pct", "base_pct",
                                                  "lift", "vs_base_t", "tail8_pct",
                                                  "tail8_lift", "enough")},
        # 逐条也用同一把尺子（lift + 配对 t），页面才能统一判「无区分度」
        "sell_rules": [{k: r.get(k) for k in ("label", "expr", "n", "days", "flat_pct", "base_pct",
                                              "lift", "vs_base_t", "fly_pct", "tail8_pct",
                                              "tail8_lift", "median_ret_pct", "exc20_pp", "enough")}
                       for r in summary.get("sell_rules") or []],
        "add_rules": [{k: r.get(k) for k in ("label", "expr", "n", "days", "flat_pct", "base_pct",
                                             "lift", "vs_base_t", "fly_pct", "tail8_pct",
                                             "median_ret_pct", "exc20_pp", "enough")}
                      for r in summary.get("add_rules") or []],
    }


async def _sync_replay(row: dict) -> dict:
    """把 Celery 状态归并回规则集行：只在读接口触发，没人看时不写库。"""
    if row.get("replay_status") in TERMINAL or not row.get("replay_status"):
        return row
    if not row.get("celery_task_id"):
        return row
    state, payload = await asyncio.to_thread(_read_celery, row["celery_task_id"])
    patch: dict = {}
    if state == "PROGRESS":
        meta = payload if isinstance(payload, dict) else {}
        patch = {"replay_status": "running",
                 "replay_progress": int(meta.get("progress") or 0),
                 "replay_stage": str(meta.get("stage") or "")[:128]}
    elif state in ("STARTED", "RETRY"):
        patch = {"replay_status": "running", "replay_stage": "worker 已接收"}
    elif state == "SUCCESS":
        summary = payload if isinstance(payload, dict) else {}
        patch = {"replay_status": "done", "replay_progress": 100, "replay_stage": "完成",
                 "posterior": json.dumps(_brief(summary), ensure_ascii=False),
                 "replayed_at": datetime.now(timezone.utc), "replay_error": ""}
    elif state == "FAILURE":
        patch = {"replay_status": "failed",
                 "replay_error": (payload if isinstance(payload, str)
                                  else str(payload))[:512]}
    elif _age_seconds(row) > STALE_SECONDS:
        patch = {"replay_status": "failed",
                 "replay_error": f"{STALE_SECONDS // 3600} 小时内没有拿到任何进度，判定 worker 无响应"}
    if not patch:
        return row
    saved = await rule_sets_repo.update(row["id"], patch)
    return {**row, **patch, **(saved or {})}


# ------------------------------------------------------------------ 表单初始化

def options() -> dict:
    re_mod = rule_expr()
    path = active_path()
    return {
        "fields": re_mod.fields_catalog(),
        "ops": list(re_mod.OPS),
        "disaster": {"min": 1.0, "max": 30.0, "default": 8.0},
        "max_rules": MAX_RULES_PER_SIDE,
        "max_conditions": MAX_CONDITIONS_PER_RULE,
        "active_file": {"exists": path.is_file(), "path": str(path)},
        "replay_contract": {"horizon_days": 20, "flat_threshold_pct": 3.0,
                            "fly_threshold_pct": 3.0, "tail_threshold_pct": 8.0,
                            "min_samples": 200, "min_days": 30},
    }


async def active_snapshot() -> dict | None:
    """当前生效的规则集（回测页面预填规则集快照用）；没有生效行就返回 None。"""
    return _snapshot(await rule_sets_repo.find_active())


async def snapshot_by_id(rule_set_id: str) -> dict:
    """按 id 取快照：回测投递时由服务端注入，前端只提交 rule_set_id，
    否则判据串可以被伪造，作业参数里记的「这次跑的哪套规则」就不可信了。"""
    row = await rule_sets_repo.get(rule_set_id)
    if not row:
        raise NotFoundError("规则集不存在")
    return _snapshot(row)


def _snapshot(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"rule_set_id": row["id"], "name": row["name"],
            "sell": row["sell_expr"], "add": row["add_expr"],
            "disaster_stop_pct": float(row["disaster_stop_pct"])}
