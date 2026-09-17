"""portfolio/pricing.py — 行情取价：K线 parquet 优先（含指标列），AKShare 实时兜底。"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from stock_selector.datasource.ak_source import AKShareSource


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
    df = AKShareSource().get_kline(code)
    if df is None or df.empty or "close" not in df.columns:
        raise PricingError(f"AKShare 无行情: {code}")
    return float(df.iloc[-1]["close"])


def latest_price(code: str, natural_day: str) -> float:
    """K线 parquet 优先；文件缺失 → AKShare 实时兜底。"""
    try:
        df = _load_kline_df(code)
        return latest_price_from_df(df, natural_day)
    except PricingError:
        return _ak_latest(code)
