"""portfolio/pricing.py — 行情取价：K线 parquet 优先（含指标列），AKShare 实时兜底。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from stock_selector.datasource.ak_source import AKShareSource

logger = logging.getLogger(__name__)


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


def latest_price_from_df(df: pd.DataFrame, natural_day: str) -> float:
    if df is None or df.empty or "close" not in df.columns:
        raise PricingError("行情数据为空或缺少 close 列")
    sub = df[df["date"] <= natural_day] if "date" in df.columns else df
    if sub.empty:
        raise PricingError(f"natural_day({natural_day}) 前无行情")
    return float(sub.sort_values("date").iloc[-1]["close"])


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


def _tencent_symbol(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "1", "2", "3")):
        return f"sz{code}"
    return f"bj{code}"


def _tencent_latest(code: str) -> float | None:
    """实时价兜底：东财掐连接时（ETF 接口尤其频繁）腾讯行情仍给得到同一天收盘价。"""
    try:
        import requests

        resp = requests.get(f"https://qt.gtimg.cn/q={_tencent_symbol(code)}", timeout=5)
        price = float(resp.content.decode("gbk", "ignore").split("~")[3])
        return price if price > 0 else None
    except Exception as exc:
        logger.warning("腾讯实时价(%s) 失败: %s", code, exc)
        return None


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


def latest_price(code: str, natural_day: str) -> float:
    """K线 parquet 优先；文件缺失 → AKShare 实时兜底。"""
    try:
        df = _load_kline_df(code)
        return latest_price_from_df(df, natural_day)
    except PricingError:
        return _ak_latest(code)
