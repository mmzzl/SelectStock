"""板块热力图读接口内核：只读离线脚本预计算好的 parquet，进程内不做任何算术（R1-2）。

数据由 scripts/build_sector_heat.py 每天 18:30 整文件覆盖写：
    data/sector_heat/board.parquet   格子（行业+概念 × 3/7/30 交易日）
    data/sector_heat/member.parquet  格子点开后成分股明细
个股 K 线直接读 data/stock_kline/{code}.parquet 的尾部若干行。

缓存按 (文件, mtime) 键控：脚本重跑后无需重启本进程就能看到新数据。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
HEAT_DIR = ROOT / "data" / "sector_heat"
KLINE_DIR = ROOT / "data" / "stock_kline"

CODE_RE = re.compile(r"^\d{6}$")
KLINE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "pct", "ma5", "ma10", "ma20", "ma60"]

_cache: dict[str, tuple[float, pd.DataFrame]] = {}


class HeatDataMissing(Exception):
    """预计算文件不存在（脚本还没跑过 / 数据盘没挂上），给前端一句人话而不是 500。"""


BOARD_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,16}$")


def _table(name: str) -> pd.DataFrame:
    path = HEAT_DIR / f"{name}.parquet"
    if not path.exists():
        raise HeatDataMissing(f"缺少 {path.name}，请先执行 python scripts/build_sector_heat.py")
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    df = pd.read_parquet(path)
    _cache[str(path)] = (mtime, df)
    return df


def _value(v):
    if v is None or pd.isna(v):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, bool):
        return bool(v)
    if hasattr(v, "item"):          # numpy 标量不进 JSON
        return v.item()
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _value(v) for k, v in row.items()} for row in df.to_dict("records")]


def windows_available() -> list[int]:
    return sorted(int(w) for w in _table("board")["window"].unique())


def heat(window: int = 3, kind: str = "all") -> dict:
    """热力图格子：按区间涨幅降序（前端拿到即渲染，不再排序/加总）。"""
    board = _table("board")
    df = board[board["window"] == int(window)]
    if kind in ("industry", "concept"):
        df = df[df["kind"] == kind]
    df = df.sort_values("ret_pct", ascending=False)
    return {
        "asof": _value(board["asof"].iloc[0]) if len(board) else None,
        "window": int(window),
        "windows": windows_available(),
        "kinds": sorted(board["kind"].unique().tolist()),
        "count": int(len(df)),
        "boards": _records(df),
    }


def board_detail(board_code: str, window: int = 3) -> dict:
    """一个格子的详情：板块指标 + 成分股区间表现（涨幅降序）。"""
    if not BOARD_CODE_RE.match(board_code):
        raise ValueError("board_code 非法，应为行业码（A05/J66）或概念码（BK0425）")
    board = _table("board")
    member = _table("member")
    key = board_code.upper()
    hit = board[(board["board_code"].str.upper() == key) & (board["window"] == int(window))]
    rows = member[(member["board_code"].str.upper() == key) & (member["window"] == int(window))]
    rows = rows.sort_values("ret_pct", ascending=False)
    return {
        "board": _records(hit)[0] if len(hit) else None,
        "member_count": int(len(rows)),
        "members": _records(rows),
    }


def stock_kline(code: str, days: int = 120) -> dict:
    """个股 K 线尾部若干根，供前端 canvas 画蜡烛；单位：volume 手、amount 元。"""
    if not CODE_RE.match(str(code)):
        raise ValueError("股票代码需为 6 位数字")
    path = KLINE_DIR / f"{str(code).zfill(6)}.parquet"
    if not path.exists():
        raise HeatDataMissing(f"{code} 没有本地日线，请先执行 build_kline_features.py")
    available = set(pq.ParquetFile(path).schema_arrow.names)
    use = [c for c in KLINE_COLUMNS if c in available] + (["name"] if "name" in available else [])
    df = pd.read_parquet(path, columns=use).dropna(subset=["date", "close"])
    df = df.sort_values("date").tail(int(days))
    bars = _records(df)
    return {
        "code": str(code).zfill(6),
        "name": df["name"].iloc[-1] if "name" in df.columns and len(df) else "",
        "bars": bars,
    }
