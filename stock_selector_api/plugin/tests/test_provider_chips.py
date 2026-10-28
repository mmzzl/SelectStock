"""portfolio/provider.py 的筹码价位：诊断页那三档价的真实数据来源是日线 parquet。

口径与选股插件同源（`stock_selector/features/chips.py`），这里只验三件事：
1. 只吃 `date ≤ natural_day` 的行——切在昨天的诊断不能把今天的成交算进成本分布；
2. 切日挪到某一天，窗口右端跟着挪，读到的是那一天的收盘与主峰距离；
3. 历史不满 60 根（新上市/缺数）或文件缺失时返回 None，让上层写「算不出」而不是编一个价。
"""
import pandas as pd
import pytest

from portfolio.provider import KlineProvider


def _dates(n: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=n)]


def _write(tmp_path, code: str, rows: list[tuple[str, float, float, float]], amount=5e8):
    df = pd.DataFrame([{"code": code, "name": "测试", "date": d, "low": lo, "high": hi,
                        "close": c, "amount": amount, "volume": amount / c}
                       for d, lo, hi, c in rows])
    df.to_parquet(tmp_path / f"{code}.parquet")


def test_chip_levels_uses_only_rows_up_to_the_natural_day(tmp_path):
    base, extra = _dates(60), _dates(62)[60:]
    _write(tmp_path, "600001",
           [(d, 13.5, 14.5, 14.0) for d in base]
           + [(d, 25.0, 26.0, 25.5) for d in extra])
    lv = KlineProvider(base[-1], kline_dir=tmp_path).chip_levels("600001")
    assert lv["close"] == pytest.approx(14.0)            # 10 月那两段没进窗口
    assert lv["buy_ref_price"] == pytest.approx(13.5)    # 主峰下沿 = 这 60 天的箱体下沿


def test_chip_levels_follows_the_cutoff(tmp_path):
    days = _dates(62)                     # 60 天低位箱体 + 末尾 2 天跳到 25 一带
    _write(tmp_path, "600001",
           [(d, 13.5, 14.5, 14.0) for d in days[:-2]]
           + [(d, 25.0, 26.0, 25.5) for d in days[-2:]])
    early = KlineProvider(days[-3], kline_dir=tmp_path).chip_levels("600001")
    late = KlineProvider(days[-1], kline_dir=tmp_path).chip_levels("600001")
    assert early["close"] == pytest.approx(14.0)
    assert late["close"] == pytest.approx(25.5)
    # 主峰还是那 58 天堆出来的下沿，但现价已经翻上去 → 主峰在脚下、上方没有套牢盘
    assert late["close_below_peak"] is False
    assert late["peak_gap_pct"] > 50.0
    assert early["peak_gap_pct"] < late["peak_gap_pct"]


def test_chip_levels_none_when_history_short_or_file_missing(tmp_path):
    days = _dates(20)
    _write(tmp_path, "600002", [(d, 13.5, 14.5, 14.0) for d in days])
    assert KlineProvider(days[-1], kline_dir=tmp_path).chip_levels("600002") is None
    assert KlineProvider(days[-1], kline_dir=tmp_path).chip_levels("600999") is None
