"""资金榜读接口内核：只读 `scripts/build_fund_rank.py` 的产物，进程内不滚动、不换算单位。

产出目录 `data/fund_rank/`：
    rank.parquet   window × code 的累计榜（净额单位=元，占比=小数）
    meta.json      as_of、参与日清单、每档票数、薄日（缺数证据）

两条不能弯的规矩：
- **档位不在产物里 ≠ 没有资金动向**。凑不满窗口的票会被滚动求和直接剔掉，所以「30 日档空」
  几乎一定是某天采集薄了，notice 要把薄日点名出来，前端才不至于显示成「今日无流入」。
- **单位换算留给前端**（元 → 亿）。服务端一约整，两个窗口档位就会对不上账。

缓存按 (文件, mtime) 键控：脚本重跑后不必重启本进程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RANK_DIR = ROOT / "data" / "fund_rank"

WINDOWS = (3, 7, 30)
SIDES = ("in", "out")
MAX_LIMIT = 200

_cache: dict[str, tuple[float, object]] = {}


class FundDataMissing(Exception):
    """预计算产物不存在（脚本还没跑过 / 数据盘没挂上），给前端一句人话而不是 500。"""


def _path(name: str) -> Path:
    return RANK_DIR / name


def _load(name: str, reader):
    path = _path(name)
    if not path.exists():
        raise FundDataMissing(f"缺少 {path.name}，请先执行 python scripts/build_fund_rank.py")
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    value = reader(path)
    _cache[str(path)] = (mtime, value)
    return value


def _table() -> pd.DataFrame:
    return _load("rank.parquet", lambda p: pd.read_parquet(p))


def _meta() -> dict:
    return _load("meta.json", lambda p: json.loads(p.read_text(encoding="utf-8")))


def _value(v):
    if v is None or pd.isna(v):
        return None
    if isinstance(v, bool):
        return bool(v)
    if hasattr(v, "item"):          # numpy 标量不进 JSON
        return v.item()
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _value(v) for k, v in row.items()} for row in df.to_dict("records")]


def _notice(window: int, rows: list[dict], meta: dict) -> str | None:
    """空榜必须说清是「凑不满窗口」而不是「没有资金动向」。"""
    if rows:
        return None
    thin = meta.get("thin_days") or []
    if thin:
        detail = "、".join(f"{x.get('date')} 只采到 {x.get('rows')} 行" for x in thin[:3])
        return f"{window} 日档没有票：窗口内有缺数日（{detail}），凑不满 {window} 天的票不进榜"
    return (f"{window} 日档没有票：产物里这一档为空"
            f"（已有档位 {meta.get('windows') or '无'}，as_of={meta.get('as_of')}）")


def rank(window: int = 3, side: str = "in", limit: int = 50) -> dict:
    """一个窗口 + 一个方向的榜单。`side=in` 净流入降序，`out` 升序（流出最多在前）。"""
    window = int(window)
    if window not in WINDOWS:
        raise ValueError(f"window 只支持 {'/'.join(str(w) for w in WINDOWS)}")
    if side not in SIDES:
        raise ValueError("side 只支持 in / out")
    limit = max(1, min(int(limit), MAX_LIMIT))

    table = _table()
    meta = _meta()
    df = table[table["window"] == window]
    if not df.empty:
        df = df.sort_values("net_inflow", ascending=(side == "out"))
    rows = _records(df.head(limit))
    return {
        "asof": meta.get("as_of"),
        "window": window,
        "windows": [int(w) for w in (meta.get("windows") or [])],
        "side": side,
        "days": int(df["days"].iloc[0]) if len(df) else window,
        "count": len(rows),
        "total": int(len(df)),
        "rows": rows,
        "days_used": meta.get("days_used") or [],
        "thin_days": meta.get("thin_days") or [],
        "notice": _notice(window, rows, meta),
    }
