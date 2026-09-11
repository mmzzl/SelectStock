"""使用 akshare 行情接口的默认数据源实现。"""

import pandas as pd

from .base_source import BaseDataSource


class AKShareSource(BaseDataSource):
    """基于 akshare 的行情数据源，编号标准化 DataFrame。"""

    major_type = True

    def get_stock_list(self) -> list[dict]:
        """获取 A 股全部股票列表（code/name）。"""
        import akshare as ak

        df = ak.stock_info_a_code_name()
        records = []
        for _, row in df.iterrows():
            records.append({"code": str(row["code"]).zfill(6), "name": str(row["name"])})
        return records

    def get_kline(self, code: str):
        """拉取单只股票日 K 线并标准化字段。"""
        import akshare as ak

        start = self.config.get("start_date", "20240101")
        adjust = self.config.get("adjust", "qfq")
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                adjust=adjust,
            )
        except Exception:
            return None
        if raw is None or raw.empty:
            return None

        df = raw.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep].reset_index(drop=True)
