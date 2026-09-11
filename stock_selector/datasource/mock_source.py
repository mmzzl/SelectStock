"""无 akshare 时的模拟数据源，用于框架骨架演示与测试。

生产环境无需此文件；接入真实数据源后，该模拟源仅作为离线/演示回退。
"""

import numpy as np
import pandas as pd

from .base_source import BaseDataSource


class MockSource(BaseDataSource):
    """生成确定性模拟 K 线数据，保证骨架可独立运行。"""

    def get_stock_list(self) -> list[dict]:
        return [
            {"code": "600000", "name": "浦发银行"},
            {"code": "000001", "name": "平安银行"},
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000858", "name": "五粮液"},
            {"code": "601318", "name": "中国平安"},
        ]

    def get_kline(self, code: str):
        seed = int(code) % 1000
        rng = np.random.default_rng(seed)

        n = self.config.get("fetch_rows", 120)
        dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")

        # 构造带趋势的模拟价格序列
        base = 20 + (seed % 80)
        trend = np.linspace(0, base * 0.15, n)
        noise = rng.normal(0, 1.2 / 100, n).cumsum() * base
        close = base + trend + noise
        close = np.maximum(close, 1.0)

        open_ = np.concatenate([[close[0]], close[:-1].copy()])
        high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.02, n))
        low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.02, n))
        volume = rng.integers(1_000_000, 20_000_000, n)

        return pd.DataFrame(
            {
                "date": dates,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
