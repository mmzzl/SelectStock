"""portfolio/posterior.py — 诊断可信度直读判定台账的后验表，不再抄写死常数。

`scripts/build_judgement_ledger.py` 每天将 2006–今全市场每个「日期 × 股票」按插件同一条
规则判成 sell/add/hold，并统计往后 10/20 个交易日的实际分布（Beta 后验 + 95% 区间 +
三段时代同号性）。这里只读那张 `posterior.parquet`，让诊断文案里的数字跟着台账一起更新。

口径要说清：这是**状态分布**不是个股预测——「处在同样形态的持仓点整体后来怎么样了」。
所以取的是 `heat_bin == 全部` 那一行（热度分箱行只能当门控，板块成员是用最新一天的成分
回推历史的，含已知前视）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FILE = (Path(__file__).resolve().parents[3]
                / "data" / "judgement_ledger" / "posterior.parquet")
ALL_BIN = "全部"

_cache: dict | None = None
_cache_key: tuple = ()


def _path() -> Path:
    return Path(os.environ.get("POSTERIOR_FILE") or DEFAULT_FILE)


def _load(path: Path) -> dict:
    import pandas as pd

    try:
        df = pd.read_parquet(path)
    except Exception as exc:      # 台账没建/正在重建不能让诊断整页报错
        logger.warning("后验表读取失败 %s：%s", path, exc)
        return {}
    out: dict = {}
    for rec in df[df["heat_bin"] == ALL_BIN].to_dict("records"):
        action = str(rec.get("action") or "")
        if not action:
            continue
        out[action] = {
            "n": int(rec.get("n") or 0),
            "up_pct": round(float(rec.get("up_rate_posterior") or 0) * 100, 2),
            "ci_low_pct": round(float(rec.get("ci_low") or 0) * 100, 2),
            "ci_high_pct": round(float(rec.get("ci_high") or 0) * 100, 2),
            "ci_width_pp": float(rec.get("ci_width_pp") or 0),
            "credible": bool(rec.get("credible")),
            "fwd10_mean_pct": float(rec.get("fwd10_mean_pct") or 0),
            "fwd10_p5_pct": float(rec.get("fwd10_p5_pct") or 0),
            "fwd20_mean_pct": float(rec.get("fwd20_mean_pct") or 0),
            "fwd20_p5_pct": _opt(rec.get("fwd20_p5_pct")),
            "flip_sell_20_pct": _opt(rec.get("flip_sell_20_pct")),
            "era_sign_stable": bool(rec.get("era_sign_stable")),
            # 日等权那组：点级均值会被判据扎堆的日子主导（sell 格点级 +0.87%、日等权 −0.14%），
            # 所以文案优先读这里。vs_hold_* 是「同热度同日 vs hold」的配对差分，水位已抵消。
            "daymean_fwd10_pct": _opt(rec.get("daymean_fwd10_pct")),
            "days": _opt(rec.get("days")),
            "vs_hold_pp": _opt(rec.get("vs_hold_daymean_pp")),
            "vs_hold_t": _opt(rec.get("vs_hold_t")),
            "vs_hold_days": _opt(rec.get("vs_hold_days")),
            "vs_hold_era_stable": _flag(rec.get("vs_hold_era_stable")),
        }
    return out


def _flag(v) -> bool:
    """布尔列缺值时落成 NaN，而 `bool(nan)` 是 True——会把「没算出差分」印成「三段同号」。"""
    return _opt(v) is not None and float(v) > 0


def _opt(v):
    """NaN/None → None：缺列（旧版台账）时让调用方退化，而不是印一个 nan。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def lookup(action: str) -> dict | None:
    """取某个判定状态的全样本后验；台账缺失或该状态样本不足时返回 None。"""
    global _cache, _cache_key
    path = _path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if _cache is None or _cache_key != key:
        _cache, _cache_key = _load(path), key
    row = (_cache or {}).get(action)
    return row if row and row["n"] > 0 else None
