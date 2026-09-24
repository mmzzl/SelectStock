"""MongoDB 日线数据源：直连 stock_kline 集合取最新行情。

与 scripts/build_kline_features.py 读同一份数据（frequency=9 日线、adjust=qfq），
索引 code_1_frequency_1_date_-1 支撑下单只查询约 10ms，全市场 5000+ 只约 1 分钟。

仅提供基础 OHLCV 列（不含 talib 指标列）；指标/规则/模型类策略请用 parquet 源。
"""

import re
from pathlib import Path

import pandas as pd

from .base_source import BaseDataSource

CONFIG_YAML = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
PROJECTION = {
    "_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1
}
COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


class MongoSource(BaseDataSource):
    """从 MongoDB 读取日线行情的数据源。

    config:
      host / port / database: 缺省读 stock_selector/config/config.yaml 的 mongodb 节
      frequency: 行情周期，默认 9（日线）
      adjust: 复权方式，默认 qfq
      rows: 每只股票取最近 N 个交易日（均线/箱体类策略需要预热），默认 120
      codes_collection: 股票列表与名称来源集合，默认 bk_stocks
    """

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        mongo = _load_mongo_config()
        self.host = self.config.get("host") or mongo.get("host", "127.0.0.1")
        self.port = int(self.config.get("port") or mongo.get("port", 27017))
        self.database = self.config.get("database") or mongo.get("database", "eastmoney_news")
        self.frequency = int(self.config.get("frequency", 9))
        self.adjust = self.config.get("adjust", "qfq")
        self.rows = int(self.config.get("rows", 120))
        self.codes_collection = self.config.get("codes_collection", "bk_stocks")
        self._client = None
        self._max_date: str | None = None

    def _db(self):
        if self._client is None:
            from pymongo import MongoClient

            self._client = MongoClient(
                host=self.host, port=self.port, serverSelectionTimeoutMS=5000
            )
        return self._client[self.database]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get_stock_list(self) -> list[dict]:
        """股票列表与名称均取自 bk_stocks。"""
        seen: dict[str, str] = {}
        cursor = self._db()[self.codes_collection].find(
            {}, {"_id": 0, "stock_code": 1, "stock_name": 1}
        )
        for doc in cursor:
            code = normalize_code(doc.get("stock_code"))
            name = doc.get("stock_name")
            if code and name:
                seen.setdefault(code, str(name))
        return [{"code": code, "name": name} for code, name in sorted(seen.items())]

    def get_kline(self, code: str):
        query = {"code": code, "frequency": self.frequency, "adjust": self.adjust}
        docs = list(
            self._db()["stock_kline"].find(query, PROJECTION).sort("date", -1).limit(self.rows)
        )
        if not docs:
            return None

        df = pd.DataFrame(docs)
        # date 形如 "2026-09-23 15:00"，截取自然日后按日期升序返回
        df["date"] = pd.to_datetime(df["date"].astype(str).str.slice(0, 10))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        df = df.sort_values("date").reset_index(drop=True)
        df = df[[c for c in COLUMNS if c in df.columns]]
        self._note_date(df["date"].iloc[-1].strftime("%Y-%m-%d"))
        return df

    def max_date(self) -> str | None:
        """已读取数据里最晚的交易日。"""
        return self._max_date

    def _note_date(self, date: str) -> None:
        if self._max_date is None or date > self._max_date:
            self._max_date = date


def normalize_code(raw) -> str:
    """把 600519 / 600519.SH / SH600519 等写法统一为 6 位数字代码。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-6:] if len(digits) >= 6 else ""


def _load_mongo_config() -> dict:
    if not CONFIG_YAML.is_file():
        return {}
    import yaml

    data = yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8")) or {}
    return data.get("mongodb") or {}
