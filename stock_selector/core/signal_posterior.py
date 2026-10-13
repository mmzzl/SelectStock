"""core/signal_posterior.py — 把「这条判据历史上值多少钱」挂到每条选股结果上。

数据来自 `scripts/build_signal_posterior.py` 落盘的 `data/signal_posterior/signal_posterior.parquet`：
每行是一个 (策略, 时代, 抽样) 格子的 T+10 超额统计（超额 = 该点 10 日收益 − 当日样本内全体均值）。

这里把它压成一条策略级摘要。**符号稳定性优先于均值**：上一轮 pattern_reversal 就是
同日基线为正、换一批股票抽样就翻负，所以 `sign_stable=False` 时消费方必须显示出来，
不能只报那个正的平均数。`credible` 因此是「样本够 **且** 三轴同号」，不是只看置信区间；
`verdict` 是给页面直接显示的一句话。
"""
from __future__ import annotations

from pathlib import Path

FILE = (Path(__file__).resolve().parents[2]
        / "data" / "signal_posterior" / "signal_posterior.parquet")

_cache: dict | None = None
_cache_key: tuple = ()


def _load(path: Path) -> dict:
    import pandas as pd

    try:
        df = pd.read_parquet(path)
    except Exception:
        return {}
    return _load_from_df(df)


def _load_from_df(df) -> dict:
    out: dict[str, dict] = {}
    for name, g in df.groupby("strategy"):
        cells = g.to_dict("records")
        # 主口径 = 日等权（当天命中的均值先算，再按天平均），与既定汇报口径一致；
        # 行等权会被命中扎堆的恐慌日主导，同一判据能两种加权下反号，所以只并列展示。
        daymeans = [float(c.get("excess10_daymean_pp", c["excess10_mean_pp"])) for c in cells]
        dayw = [max(int(c.get("days") or 1), 1) for c in cells]
        pointmeans = [float(c["excess10_mean_pp"]) for c in cells]
        weights = [max(int(c["n"]), 1) for c in cells]
        total_n = sum(weights)
        total_d = sum(dayw)
        out[str(name)] = {
            "n": total_n,
            "cells": len(cells),
            "eras": sorted({str(c["era"]) for c in cells}),
            "beat_pct": round(100 * sum(float(c["beat_rate_posterior"]) * w
                                         for c, w in zip(cells, weights)) / total_n, 2),
            "excess_pp": round(sum(m * w for m, w in zip(daymeans, dayw)) / total_d, 3),
            "excess_point_pp": round(sum(e * w for e, w in zip(pointmeans, weights)) / total_n, 3),
            "excess_min_pp": round(min(daymeans), 3),
            "excess_max_pp": round(max(daymeans), 3),
            "fwd10_mean_pct": round(sum(float(c["fwd10_mean_pct"]) * w
                                        for c, w in zip(cells, weights)) / total_n, 3),
            # 同号 = 所有 (时代 × 抽样) 格子的日等权超额都在 0 的同一侧
            "sign_stable": bool(min(daymeans) > 0 or max(daymeans) < 0),
            # 两个轴分开命名，再合成一个 credible 给消费方：CI 窄只说明「样本够」，
            # 同号才说明「结论稳」。把 trend_rebound 那种 −0.32pp、格子间翻号的读数
            # 按「样本充足」报成可信，就是把抽样噪声说成结论。
            "sample_adequate": bool(all(c["credible"] for c in cells)),
            "credible": bool(all(c["credible"] for c in cells)
                             and (min(daymeans) > 0 or max(daymeans) < 0)),
            "window": "T+10 超额·日等权（相对当日全样本均值）",
        }
        out[str(name)]["verdict"] = _verdict(out[str(name)])
    return out


def _verdict(s: dict) -> str:
    """一句话结论。页面应该只显示这个，而不是单独显示那个平均数。"""
    if not s["sample_adequate"]:
        # 区间宽是「按格子切完之后，单格里上涨概率估不准」，不是总样本少——
        # 一页上同时印「样本不足」和「样本 1.9 万点」会自相矛盾，必须点名是单格。
        return "分格样本不足（单格上涨概率区间>2pp），不定论"
    if not s["sign_stable"]:
        return "换时代/换抽样会翻号，未标定"
    return "正超额·三轴同号" if s["excess_pp"] > 0 else "负超额·三轴同号（反指）"


def for_strategy(name: str) -> dict | None:
    """该策略的历史后验；表还没建时返回 None（调用方按「未标定」显示，不能编一个数）。"""
    global _cache, _cache_key
    try:
        stat = FILE.stat()
        key = (str(FILE), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if _cache is None or _cache_key != key:
        _cache, _cache_key = _load(FILE), key
    return (_cache or {}).get(name)
