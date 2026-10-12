"""板块热力图服务：薄代理选股插件(8002) 的预计算结果。
红线：后端不做任何行情算术（R1-2）——窗口切法、等权涨幅都在 scripts/build_sector_heat.py 里算完。"""
from __future__ import annotations

import logging

import httpx

from config import settings
from core.exceptions import BizError

logger = logging.getLogger("sector_service")

_PATHS = {
    "heat": "/api/sector/heat",
    "board": "/api/sector/board",
    "kline": "/api/sector/kline",
}

# 插件未启（mode=mock）时给空壳：形状与真数据一致，但绝不编造行情，前端据此出空态提示
_EMPTY = {
    "heat": {"asof": None, "window": None, "windows": [], "kinds": [], "count": 0, "boards": []},
    "board": {"board": None, "member_count": 0, "members": []},
    "kline": {"code": "", "name": "", "bars": []},
}


async def _call(endpoint: str, params: dict) -> dict:
    if settings.selection_plugin_mode != "http":
        return _EMPTY[endpoint]
    url = settings.selection_plugin_base_url.rstrip("/") + _PATHS[endpoint]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("板块插件不可用 %s: %s", url, exc)
        raise BizError("板块服务暂不可用，请稍后重试")
    if body.get("code") != 0:
        raise BizError(body.get("message") or "板块数据不可用")
    return body.get("data") or _EMPTY[endpoint]


async def heat(window: int, kind: str) -> dict:
    return await _call("heat", {"window": window, "kind": kind})


async def board_detail(board_code: str, window: int) -> dict:
    return await _call("board", {"board_code": board_code, "window": window})


async def stock_kline(code: str, days: int) -> dict:
    return await _call("kline", {"code": code, "days": days})
