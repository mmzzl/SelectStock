"""risk/snapshot.py — 风控读的行情口径：盘后落盘快照优先，缺失时退实时快照。

为什么不直接读日线：本地日线只有涨停标记 `lu`、**没有跌停**，成交额自 2026-05-21 起断供
（全是 0），涨跌停阈值还得分主板/创业/科创/北交所/ST/基金。`data/market_snapshot` 里的
涨停价/跌停价是交易所下发的原值（抓取脚本 scripts/fetch_market_snapshot.py）。

缺文件（当天没跑、回测历史日期、新标的）时的降级顺序写死在这里，规则层不再各判各的：
1. 当日落盘快照；2. 实时快照（腾讯，只给当天，回测用 `allow_live=False` 关掉）；
3. 都没有 → 返回空字典，规则**跳过判定**而不是猜一个价。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from stock_selector.quote import tencent

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "market_snapshot"

# 落盘快照里风控要用的列；NaN 一律归一成 None，让规则走「不可得就跳过」而不是拿 NaN 比较
_FIELDS = ("price", "pct", "volume", "amount", "turnover_pct", "vol_ratio", "float_mv",
           "total_mv", "limit_up", "limit_down", "at_limit_up", "at_limit_down", "asset", "name")
_FLAGS = ("at_limit_up", "at_limit_down")
_TEXT = ("asset", "name")

_cache: dict[str, pd.DataFrame] = {}
CACHE_MAX = 4               # 一天全市场约 6600 行 / 2MB，只热着最近几个交易日


def snapshot_dir() -> Path:
    return Path(os.environ.get("RISK_SNAPSHOT_DIR", DEFAULT_DIR))


def clear_cache() -> None:
    """调度/测试换目录时用（缓存键里已带目录，正常调用不必清）。"""
    _cache.clear()


def _load_day(day: str) -> pd.DataFrame:
    """读一整天的落盘快照，以六位代码为索引；缺文件返回空表。

    缺文件是「整天都没跑抓取脚本」，一天只报一次 INFO 就够了——全市场 6600 只逐票报会把
    日志刷成噪音，真正的逐票缺失由规则留下的指标（limit_data/avg_amount 为空）去说明。
    """
    key = f"{snapshot_dir()}|{day}"
    if key not in _cache:
        path = snapshot_dir() / f"{day}.parquet"
        if path.is_file():
            frame = pd.read_parquet(path)
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            frame = frame.set_index("code")
        else:
            logger.info("没有 %s：%s 的涨跌停/成交额口径全部退到日线标记或直接跳过"
                        "（涨跌停只能判涨停，跌停无从判断）", path, day or "该日")
            frame = pd.DataFrame()
        while len(_cache) >= CACHE_MAX:
            _cache.pop(min(_cache))        # 键尾是 YYYY-MM-DD，字典序即时间序，丢最旧的
        _cache[key] = frame
    return _cache[key]


def _cell(row: pd.Series, key: str):
    if key not in row.index:
        return None
    value = row[key]
    if pd.isna(value):
        return None
    if key in _FLAGS:
        return bool(value)
    if key in _TEXT:
        return str(value)
    return float(value)


def from_file(code: str, day: str) -> dict | None:
    frame = _load_day(day)
    if frame.empty or code not in frame.index:
        return None
    row = frame.loc[code]
    out = {key: _cell(row, key) for key in _FIELDS}
    out["source"] = "file"
    out["date"] = day
    return out


def limit_hit(price: float | None, limit: float | None, up: bool) -> bool:
    """价格贴住涨跌停：报价 0.01 一跳，留半个 tick 容差，和落盘脚本同一口径。"""
    if price is None or limit is None or limit <= 0:
        return False
    return price >= limit - 0.005 if up else price <= limit + 0.005


def normalize(code: str, raw: dict) -> dict:
    """把腾讯原始快照补成**风控口径**：算出贴板标记、标出基金、标明来源。

    盘后落盘文件里 `at_limit_up/at_limit_down` 是抓取得到的列，而实时接口只给涨跌停价，
    「贴没贴板」必须在进风控之前用同一个 `limit_hit` 算出来——否则盘中链路传给风控的
    字典少了这两个键，涨停买不进这条规则会**静默失效**（缺键就当没贴板放行）。
    """
    out = dict(raw)
    price = out.get("price")
    out.setdefault("asset", "fund" if str(code)[0] in ("1", "5") else "stock")
    out.setdefault("source", "live")
    out.setdefault("date", "")
    out["at_limit_up"] = limit_hit(price, out.get("limit_up"), True)
    out["at_limit_down"] = limit_hit(price, out.get("limit_down"), False)
    return out


def from_live(code: str) -> dict | None:
    """实时快照：只反映当下。回测/复盘不要用它——拿不到历史当天的涨跌停价。"""
    q = tencent.fetch([code]).get(str(code))
    return normalize(code, q) if q else None


def get(code: str, day: str | None = None, allow_live: bool = True) -> dict:
    """取一只票的行情口径；缺的字段是 None，规则据此跳过判定。全缺返回空字典。"""
    day = (day or "")[:10]
    if day:
        found = from_file(code, day)
        if found:
            return found
    if allow_live:
        found = from_live(code)
        if found:
            return found
    logger.debug("快照缺失 %s@%s，涨跌停/流动性口径不可得", code, day or "-")
    return {}
