"""资金榜服务：薄代理选股插件(8002) 的预计算榜单。

红线同 sector_service：3/7/30 交易日的滚动求和、占比、区间涨幅全部在
`scripts/build_fund_rank.py` 里算完，后端一处算术都不做；单位是元，换算「亿」在前端。

插件回 code!=0（产物没生成 / 档位参数非法）一律翻成 BizError：这一档显示不出来，
也要让人知道为什么，不能回一份空榜当作「今天没有资金动向」。
"""
from __future__ import annotations

import logging

import httpx

from config import settings
from core.exceptions import BizError

logger = logging.getLogger("fund_service")

_PATH = "/api/fund/rank"

# 插件未启（mode=mock）时给空壳：形状与真数据一致，但绝不编造行情
_EMPTY = {
    "asof": None, "window": None, "windows": [], "side": "in", "days": 0,
    "count": 0, "total": 0, "rows": [], "days_used": [], "thin_days": [], "notice": None,
}


async def rank(window: int = 3, side: str = "in", limit: int = 50) -> dict:
    if settings.selection_plugin_mode != "http":
        return dict(_EMPTY)
    url = settings.selection_plugin_base_url.rstrip("/") + _PATH
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"window": window, "side": side, "limit": limit})
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("资金榜插件不可用 %s: %s", url, exc)
        raise BizError("资金数据服务暂不可用，请稍后重试")
    if body.get("code") != 0:
        raise BizError(body.get("message") or "资金数据不可用")
    return body.get("data") or dict(_EMPTY)
