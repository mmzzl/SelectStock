"""消息面服务：薄代理选股插件(8002) 的当日新闻判定，并标出「和我的持仓有关」。

红线同 sector_service：方向、强度、理由全部由 LLM 那层产出，后端一处都不自己判。
这里只做两件事——取数、拿用户自己的持仓做匹配。

为什么持仓相关性要在这儿算：`watch_target.code` 是 LLM 从新闻里抄的，实测 45 条里只有 4 条
给了六位股票代码（其余是板块 BK 码或空），单靠代码去匹会几乎全漏；而 `name` 是它写的公司名，
和持仓表里的名字基本能对上，所以代码或名称任一命中都算相关。
"""
from __future__ import annotations

import logging
import re

import httpx

from config import settings
from core.exceptions import BizError
from repositories import holdings_repo
from repositories import users as users_repo
from services import inbox_notify
from services.notify import channels as channel_repo
from services.notify import senders

logger = logging.getLogger("news_service")

_PATH = "/api/news/daily"

_EMPTY = {
    "day": None, "stale_days": None,
    "counts": {"bullish": 0, "bearish": 0, "risk": 0, "watch": 0},
    "items": {"bullish": [], "bearish": [], "risk": [], "watch": []},
    "health": {"analyzed": 0, "failed": 0, "fail_rate": None},
}

_BLANK = re.compile(r"\s+")


def _norm_name(s: str) -> str:
    """名称归一：持仓表里的名字带空格和全角字母（'张 裕Ａ'），LLM 给的是紧凑写法。"""
    return _BLANK.sub("", str(s or "")).upper()


async def _fetch(day: str | None) -> dict:
    if settings.selection_plugin_mode != "http":
        return dict(_EMPTY)
    url = settings.selection_plugin_base_url.rstrip("/") + _PATH
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params={"day": day or ""})
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("消息面插件不可用 %s: %s", url, exc)
        raise BizError("消息面服务暂不可用，请稍后重试")
    if body.get("code") != 0:
        raise BizError(body.get("message") or "消息面数据不可用")
    return body.get("data") or dict(_EMPTY)


def _mark(items: list[dict], codes: set[str], names: set[str]) -> list[dict]:
    out = []
    for it in items:
        hit = (it.get("code") in codes) or (_norm_name(it.get("name")) in names and
                                            _norm_name(it.get("name")))
        out.append({**it, "holding": bool(hit)})
    return out


def _decorate(data: dict, codes: set[str], names: set[str], holding_count: int) -> dict:
    items = {k: _mark(v, codes, names) for k, v in (data.get("items") or {}).items()}
    mine = [it for v in items.values() for it in v if it["holding"]]
    mine.sort(key=lambda it: it.get("score") or 0)
    return {**data, "items": items, "mine": mine, "holding_count": holding_count}


def _sets(rows: list[dict]) -> tuple[set[str], set[str], int]:
    codes = {str(h.get("code") or "").zfill(6) for h in rows if h.get("code")}
    names = {_norm_name(h.get("name")) for h in rows if h.get("name")} - {""}
    return codes, names, len(codes)


async def daily_for_user(user_id: str, day: str | None = None) -> dict:
    """当日消息面 + 该用户持仓的相关性标记；`mine` 是按强度排好的持仓清单。"""
    return await _decorate_user(await _fetch(day), user_id)


async def _decorate_user(data: dict, user_id: str) -> dict:
    codes, names, n = _sets(await holdings_repo.list_enabled(user_id))
    return _decorate(data, codes, names, n)


async def _active_users() -> list[dict]:
    rows, _ = await users_repo.list(filters={"is_active": True}, page=1, size=500)
    return rows or []


def _line(it: dict) -> str:
    label = {"bearish": "利空", "risk": "风险", "bullish": "利好", "watch": "关注"}[it["direction"]]
    who = it.get("name") or "—"
    code = f"({it['code']})" if it.get("code") else ""
    score = f"{it['score']:+d}" if it["direction"] != "watch" else (it.get("priority") or "中")
    reason = (it.get("reason") or "").strip().replace("\n", " ")
    # 风险行在读取层已被整成「标题即全文」（news/read.py），这里不能再拖一个空 ｜
    tail = f"｜{reason[:60]}" if reason else ""
    return f"· [{label}] {who}{code} {score}{tail}"


def build_digest(data: dict, *, top: int = 5) -> tuple[str, str]:
    """把一天的消息面压成一条推送（标题, 正文）。持仓相关优先，其次全市场利空与风险。

    数据没跑出来也要出声：失败率高、或者整批为空时，正文直接写清楚，
    而不是发一条「今日无消息面」——那正是过去两周静默空转看起来像正常的样子。
    """
    day = data.get("day")
    counts = data.get("counts") or {}
    health = data.get("health") or {}
    fail_rate = health.get("fail_rate")

    if not day:
        return ("消息面 · 无数据", "近 5 天没有成功产出的消息面批次：LLM 分析可能全失败或调度没跑，"
                                "请查 logs/news_analyze_*.log")

    head = [f"消息面 · {day}"]
    if data.get("stale_days"):
        head.append(f"（{data['stale_days']} 天前的批次）")
    head.append(f"｜利好 {counts.get('bullish', 0)} · 利空 {counts.get('bearish', 0)} · "
                f"风险 {counts.get('risk', 0)} · 关注 {counts.get('watch', 0)}")
    if fail_rate is not None and fail_rate > 0.2:
        head.append(f"｜分析失败 {health.get('failed', 0)}/{health.get('analyzed', 0)} 条")
    lines = ["".join(head)]

    items = data.get("items") or {}
    mine = data.get("mine") or []
    if mine:
        lines.append(f"—— 与我的 {data.get('holding_count', 0)} 只持仓有关 ——")
        lines.extend(_line(it) for it in mine[:top])
    neg = [dict(it, direction="bearish") for it in (items.get("bearish") or [])[:top]]
    neg += [dict(it, direction="risk") for it in (items.get("risk") or [])[:top]]
    neg = [it for it in neg if not it.get("holding")]
    neg.sort(key=lambda it: it.get("score") or 0)
    if neg:
        lines.append("—— 全市场 ——")
        lines.extend(_line(it) for it in neg[:top])
    if len(lines) == 1:
        lines.append("（没有拆出利空/风险条目）")
    return (f"消息面 · {day}", "\n".join(lines)[:1800])


async def push_digest() -> dict:
    """每日消息面推送：机器人渠道（钉钉等）一条 + 每个用户一条站内信/WS。

    走 `channels` 而不是 `notify_service.emit`：后者要有人在「订阅策略」里把这个事件勾上才发，
    而当前策略表是空的——静默不发等于没做，这正是这次要修掉的毛病。

    整批只向插件取一次，持仓匹两次：机器人那一份按「全部启用持仓」的并集标相关（私有部署，
    群里看的是同一批账户，少一次打扰），站内信按各人自己的持仓标。
    """
    data = await _fetch(None)
    active = await _active_users()
    rows_by_user = {u["id"]: await holdings_repo.list_enabled(u["id"]) for u in active}

    union = _sets([h for rows in rows_by_user.values() for h in rows])
    title, text = build_digest(_decorate(data, *union))

    robots = []
    for ctype in ("dingtalk", "wecom", "feishu"):
        for listing in channel_repo.list_all(ctype):
            if not listing.get("enabled", True):
                continue
            # list_all 是对外视图（webhook 已脱敏），发送必须换成解密视图
            ch = channel_repo.get_by_key(listing.get("key") or "", ctype)
            if not ch or not ch.get("enabled", True):
                continue
            try:
                await senders.send(ctype, ch, title, text)
                robots.append(f"{ctype}:{ch.get('key')}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("消息面推送失败 渠道=%s(%s): %s", ctype, ch.get("key"), exc)
    if not robots:
        logger.error("消息面机器人推送没有可用渠道，本轮只发了站内信（通知配置在页面「通知渠道」）")

    sent_users = []
    for u in active:
        try:
            mine = _decorate(data, *_sets(rows_by_user[u["id"]]))
            t, body = build_digest(mine)
            await inbox_notify.notify_user(u["id"], "news_digest", t, body, related_type="news")
            sent_users.append(u.get("username") or u["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("消息面站内信失败 用户=%s: %s", u.get("username"), exc)
    logger.info("消息面推送完成（%s）：机器人 %s，站内信 %d 人", data.get("day"),
                robots or ["无可用渠道"], len(sent_users))
    return {"day": data.get("day"), "robots": robots, "users": sent_users}
