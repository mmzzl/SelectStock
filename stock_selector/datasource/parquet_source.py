"""本地指标宽表数据源：读取 data/stock_kline/{code}.parquet。

该文件由 scripts/build_kline_features.py 从 MongoDB stock_kline 预计算生成，
含 30+ 个 talib 指标与派生列，选股内核离线读取、零网络依赖。

注意：全市场约 5000+ 只、单只 1000+ 行，因此只按需读取、不缓存行情帧，
避免常驻内存膨胀；跨请求复用请由调用方以子进程/任务方式管理。
"""

from pathlib import Path

import pandas as pd

from .base_source import BaseDataSource

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "stock_kline"


class ParquetSource(BaseDataSource):
    """按股票分文件的本地数据源。

    config:
      dir: 数据目录，默认 <repo>/data/stock_kline
      columns: 读取的列清单（剪列可显著降低耗时）；缺省读全部列
      rows: 仅保留最近 N 个交易日（0=不限制）
    """

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.dir = self._resolve_dir(self.config.get("dir"))
        self.columns = list(self.config["columns"]) if self.config.get("columns") else None
        self.rows = int(self.config.get("rows") or 0)
        self._names: dict[str, str] = {}
        self._max_date: str | None = None

    @staticmethod
    def _resolve_dir(raw) -> Path:
        """相对路径按仓库根解析，避免服务从不同工作目录启动时读不到数据。"""
        path = Path(raw) if raw else DEFAULT_DIR
        return path if path.is_absolute() else ROOT / path

    def available(self) -> bool:
        """目录下是否存在可读的指标宽表。"""
        return self.dir.is_dir() and any(self.dir.glob("*.parquet"))

    def prepare(self, columns: list[str] | None = None, rows: int = 0) -> None:
        """把策略声明的列/行需求并入读取范围，只放宽不收紧。

        配置的 columns 为空表示读全部列、rows=0 表示不限行数，两者都已经是最大
        范围，不能被策略声明"缩小"。
        """
        if columns and self.columns is not None:
            merged = list(self.columns)
            merged.extend(c for c in columns if c not in merged)
            self.columns = merged
        if rows and self.rows:
            self.rows = max(self.rows, int(rows))

    def get_stock_list(self) -> list[dict]:
        """股票列表来自文件名；名称在取行情时顺带登记（见 name_of）。"""
        if not self.dir.is_dir():
            return []
        return [{"code": path.stem, "name": ""} for path in sorted(self.dir.glob("*.parquet"))]

    def get_kline(self, code: str):
        path = self.dir / f"{code}.parquet"
        if not path.is_file():
            return None
        columns = self.columns
        if columns and "name" not in columns:
            columns = [*columns, "name"]      # 剪列时保留名称，否则结果无法标注股票名
        try:
            df = pd.read_parquet(path, columns=columns)
        except Exception:
            df = pd.read_parquet(path)        # 剪列清单与文件实际列不符时退回全列
        if df is None or df.empty or "date" not in df.columns:
            return None

        df["date"] = pd.to_datetime(df["date"].astype(str).str.slice(0, 10))
        df = df.sort_values("date").reset_index(drop=True)
        if self.rows:
            df = df.iloc[-self.rows :].reset_index(drop=True)
        if "name" in df.columns:
            self._names[code] = str(df["name"].iloc[-1])
        self._note_date(df["date"].iloc[-1].strftime("%Y-%m-%d"))
        return df

    def name_of(self, code: str) -> str:
        return self._names.get(code, "")

    def max_date(self) -> str | None:
        """已读取数据里最晚的交易日，用于暴露数据新鲜度。"""
        return self._max_date

    def _note_date(self, date: str) -> None:
        if self._max_date is None or date > self._max_date:
            self._max_date = date
