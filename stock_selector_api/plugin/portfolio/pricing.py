"""portfolio/pricing.py — 行情取价：**「今天」实时优先**（腾讯批量），日线 parquet 兜底。

原来的口径是「parquet 优先、实时只在文件缺失时才问」，代价是持仓价整天不动：日线 parquet
由 `build_kline_features.py` 每天 18:00 重建，所以收盘前读到的永远是**上一交易日**的收盘，
盘中手动触发诊断也会把旧收盘落进当天 run。现在只要判据的日期就是今天，先问行情。

历史日期不动：补跑 `natural_day=上周三` 必须用上周三那根收盘，否则同一份数据两次跑出两个
盈亏，历史 run 也没法比。

单位与字段解析统一走 `stock_selector.quote.tencent`，这里不再重复一遍下标。
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from time import monotonic

import pandas as pd

from stock_selector.datasource.ak_source import AKShareSource
from stock_selector.quote import tencent as tencent_quote

logger = logging.getLogger(__name__)

LIVE_CACHE_TTL = 30.0        # 秒：一次请求里同批代码只问一次行情（持仓列表每只都发一次是浪费）

_live_cache: dict[str, tuple[float, str, float]] = {}    # code → (价格, 行情时刻, 本地抓取时刻)


class PricingError(Exception):
    """行情不可得（触发后端重试/置 failed）。"""


_DEFAULT_KLINE_DIR = str(Path(__file__).resolve().parents[3] / "data" / "stock_kline")


def _kline_dir() -> Path:
    return Path(os.environ.get("PLUGIN_KLINE_DIR", _DEFAULT_KLINE_DIR))


def _market_prefix(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "30", "12")):
        return "sz"
    return "bj"


def _code_file(code: str) -> Path | None:
    for suffix in ("", f".{_market_prefix(code)}"):
        p = _kline_dir() / f"{code}{suffix}.parquet"
        if p.is_file():
            return p
    return None


def _load_kline_df(code: str) -> pd.DataFrame:
    p = _code_file(code)
    if p is None:
        raise PricingError(f"无K线数据: {code}")
    df = pd.read_parquet(p)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    return df


def _latest_bar(df: pd.DataFrame, natural_day: str) -> pd.Series:
    if df is None or df.empty or "close" not in df.columns:
        raise PricingError("行情数据为空或缺少 close 列")
    sub = df[df["date"] <= natural_day] if "date" in df.columns else df
    if sub.empty:
        raise PricingError(f"natural_day({natural_day}) 前无行情")
    return sub.sort_values("date").iloc[-1]


def latest_price_from_df(df: pd.DataFrame, natural_day: str) -> float:
    return float(_latest_bar(df, natural_day)["close"])


def _bar_date(df: pd.DataFrame, natural_day: str) -> str:
    """那一根日线的日期——`price_source=kline` 时必须让调用方看得见价是哪天的。"""
    row = _latest_bar(df, natural_day)
    return str(row["date"])[:10] if "date" in row.index else ""


def _ak_latest(code: str) -> float:
    try:
        df = _ak_kline(code)
        if df is not None and not df.empty and "close" in df.columns:
            return float(df.iloc[-1]["close"])
    except Exception as exc:   # AKShare 网络层抛的是 requests 异常，不接住就变成 500 而不是可兜底的缺价
        logger.warning("AKShare K线(%s) 失败，转实时价兜底: %s", code, exc)
    latest = _tencent_latest(code)
    if latest is not None:
        return latest
    raise PricingError(f"AKShare 无行情: {code}")


def live_quotes(codes: list[str]) -> dict[str, tuple[float, str]]:
    """批量实时价（一次腾讯请求）+ `LIVE_CACHE_TTL` 秒进程内缓存，返回 {代码: (价格, 行情时刻)}。

    缓存不是可有可无的优化：持仓列表、利润分析、持仓诊断三条路径会在同一次页面加载里问同一
    批代码，逐个问就是把三个请求当成三百个用。字段解析统一走 `stock_selector.quote.tencent`。
    """
    wanted = [str(c) for c in codes]
    now = monotonic()
    pending = [c for c in wanted
               if _live_cache.get(c) is None or now - _live_cache[c][2] > LIVE_CACHE_TTL]
    if pending:
        try:
            fetched = tencent_quote.fetch(pending)
        except Exception as exc:      # 行情问不到 = 今天没有实时价，退回日线，不能把整次诊断打挂
            logger.warning("腾讯实时价批量（%d 只）失败: %s", len(pending), exc)
            fetched = {}
        for code in pending:
            item = fetched.get(code) or {}
            price = item.get("price")
            if price and price > 0:
                _live_cache[code] = (float(price), str(item.get("quote_time") or ""), monotonic())
    fresh = monotonic()
    return {c: (_live_cache[c][0], _live_cache[c][1]) for c in wanted
            if c in _live_cache and fresh - _live_cache[c][2] <= LIVE_CACHE_TTL}


def _tencent_latest(code: str) -> float | None:
    """单只实时价：走批量入口，保证同一次请求里不会为每只票各发一个 HTTP。"""
    hit = live_quotes([str(code)]).get(str(code))
    return hit[0] if hit else None


# 场内基金（ETF/LOF）不在 stock_zh_a_hist 里，那条接口对 15/5 字头代码返回空表
_FUND_PREFIXES = ("15", "50", "51", "52", "56", "58")


def _ak_kline(code: str):
    if code.startswith(_FUND_PREFIXES):
        try:
            import akshare as ak

            # 必须带 start_date：不限窗口会让东财一次吐全部历史，实测直接被掐连接
            raw = ak.fund_etf_hist_em(symbol=code, period="daily",
                                      start_date=_window_start(), adjust="")
            if raw is not None and not raw.empty:
                df = raw.rename(columns={"日期": "date", "收盘": "close"})
                return df[["date", "close"]].reset_index(drop=True)
        except Exception as exc:        # 吞掉异常也要留痕，否则上层只看到「无行情」，分不清是限流还是代码接错
            logger.warning("fund_etf_hist_em(%s) 失败，退回股票口径: %s", code, exc)
    return AKShareSource().get_kline(code)


def _window_start() -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=240)).strftime("%Y%m%d")


def wants_live(natural_day: str) -> bool:
    """今天及以后才允许用实时价。补跑历史日期必须用那一天的收盘，否则同一份数据两次
    诊断出两个盈亏，历史 run 之间也没法比。"""
    return str(natural_day)[:10] >= date.today().isoformat()


def resolve(codes: list[str], natural_day: str) -> dict[str, dict]:
    """每只票的价 + 时刻 + 来源：今天实时优先，问不到行情退回日线那一根；历史只认那根收盘。

    返回值里 `price_time` 对 `kline` 是**那根日线的日期**——持仓价一旦可能过期，就必须让
    看的人知道它是几点/哪天的数，否则「今天跌了 9% 而页面纹丝不动」这种事无从发现。
    """
    live = live_quotes([str(c) for c in codes]) if wants_live(natural_day) else {}
    out: dict[str, dict] = {}
    for raw in codes:
        code = str(raw)
        if code in live:
            price, stamp = live[code]
            out[code] = {"price": round(price, 4), "price_time": stamp, "price_source": "live"}
            continue
        try:
            df = _load_kline_df(code)
        except PricingError:                     # 本地没文件（新标的/没建过）才走 AKShare
            out[code] = {"price": round(_ak_latest(code), 4),
                         "price_time": "", "price_source": "akshare"}
            continue
        out[code] = {"price": round(latest_price_from_df(df, natural_day), 4),
                     "price_time": _bar_date(df, natural_day), "price_source": "kline"}
    return out


def prewarm(codes: list[str], natural_day: str) -> None:
    """一次把整批代码的实时价问进缓存：逐只取价的旧调用点用它把 N 个请求压成 1 个。"""
    if wants_live(natural_day) and codes:
        live_quotes([str(c) for c in codes])


def latest_price(code: str, natural_day: str) -> float:
    """单只取价，口径同 `resolve`。批量场景请直接用 `resolve`/`prewarm`。"""
    return resolve([code], natural_day)[str(code)]["price"]
