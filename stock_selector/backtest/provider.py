"""Parquet 数据提供器：按需加载每只股票的指标 parquet，提供每日行情。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


class ParquetProvider:
    """从 data/stock_kline/{code}.parquet 提供回测所需逐日数据。

    按股票文件惰性加载并缓存（首次访问读入内存），
    price()/rows() 按日查询，停牌（无当日行）返回 None/缺省。
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._cache: dict[str, pd.DataFrame] = {}
        self._dates: list[str] | None = None

    def _load(self, code: str) -> pd.DataFrame | None:
        if code in self._cache:
            return self._cache[code]
        path = self.data_dir / f"{code}.parquet"
        if not path.exists():
            self._cache[code] = pd.DataFrame()
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("date").reset_index(drop=True)
        self._cache[code] = df
        return df

    def _files(self) -> list[Path]:
        return sorted(self.data_dir.glob("*.parquet"))

    def codes(self) -> list[str]:
        """数据面上全部股票代码（升序）。"""
        return [path.stem for path in self._files()]

    def window(self, code: str, date: str, rows: int = 0):
        """as-of 切片：只返回 date 当日及之前的行，最多最近 rows 行。

        这是"回测不得看未来"的唯一取数入口——任何按日求值的策略都应通过它取数据。
        基准日停牌（当日无行）时返回的最后一行会早于 date，由调用方决定是否弃用。
        """
        df = self._load(code)
        if df is None or df.empty:
            return None
        sub = df.loc[df["date"] <= date]
        if sub.empty:
            return None
        if rows:
            sub = sub.iloc[-rows:]
        return sub.reset_index(drop=True)

    def dates(self) -> list[str]:
        if self._dates is not None:
            return self._dates
        seen: set[str] = set()
        for path in self._files():
            try:
                col = pd.read_parquet(path, columns=["date"])
                dates = pd.to_datetime(col["date"]).dt.strftime("%Y-%m-%d")
                seen.update(dates.tolist())
            except Exception:
                continue
        self._dates = sorted(seen)
        return self._dates

    def price(self, date: str, code: str) -> float | None:
        row = self._row(date, code)
        return float(row["close"]) if row is not None else None

    def row(self, date: str, code: str) -> dict | None:
        """当日整行行情（open/high/low/close/pct…），停牌或没有这只票返回 None。

        撮合要判断涨跌停与一字板，只看收盘价不够：`high == low` 才能认出「没有对手盘」。
        """
        return self._row(date, code)

    def prices(self, date: str, codes: list[str] | None = None) -> dict[str, float]:
        rows = self.rows(date, codes)
        return {c: float(r["close"]) for c, r in rows.items()}

    def rows(self, date: str, codes: list[str] | None = None) -> dict[str, dict]:
        """返回 date 当日各股票（code -> 指标行 dict）。"""
        result: dict[str, dict] = {}
        if codes is None:
            codes = [p.stem for p in self._files()]
        for code in codes:
            row = self._row(date, code)
            if row is not None:
                result[code] = row
        return result

    def _row(self, date: str, code: str) -> dict | None:
        df = self._load(code)
        if df is None or df.empty:
            return None
        m = df["date"] == date
        if not m.any():
            return None
        rec = df.loc[m].iloc[0]
        return rec.to_dict()
