"""策略后验摘要的口径测试：可信度必须由「样本够 + 三轴同号」两个轴一起决定。

回归的是真实缺陷：格子级 `credible` 只表示 Beta 区间宽度 <2pp，
所以 trend_rebound（日等权超额 −0.32pp、换抽样翻号）曾被汇总层原样报成 `credible=True`。
"""

import pandas as pd
import pytest

from stock_selector.core import signal_posterior as sp


def _cell(name, era, offset, daymean, *, n=500, days=200, credible=True):
    return {
        "strategy": name, "era": era, "offset": offset, "n": n, "days": days,
        "beat_rate_posterior": 0.5,
        "credible": credible,
        "excess10_mean_pp": daymean,          # 行等权（抽样里与日等权同向即可）
        "excess10_daymean_pp": daymean,
        "fwd10_mean_pct": 0.2,
    }


def _load(cells):
    return sp._load_from_df(pd.DataFrame(cells))


def test_sign_flip_across_resample_is_not_credible():
    """格子全是正号才算稳定；翻一个格子就不能报「可信」。"""
    s = _load([_cell("box", "2019", 0, +0.5), _cell("box", "2019", 137, +0.4),
               _cell("box", "2022", 0, +0.6)])["box"]
    assert s["sign_stable"] and s["credible"]
    assert s["verdict"] == "正超额·三轴同号"

    flipped = _load([_cell("pat", "2019", 0, +0.5), _cell("pat", "2019", 137, -0.2),
                     _cell("pat", "2022", 0, +0.3)])["pat"]
    assert not flipped["sign_stable"]
    # 关键断言：均值仍为正、CI 仍窄，也不许是 credible
    assert flipped["excess_pp"] > 0 and flipped["sample_adequate"]
    assert not flipped["credible"]
    assert flipped["verdict"] == "换时代/换抽样会翻号，未标定"


def test_negative_but_stable_is_called_a_contradictory_signal():
    s = _load([_cell("trend", "2019", 0, -0.3), _cell("trend", "2022", 0, -0.4)])["trend"]
    assert s["sign_stable"] and s["credible"]
    assert s["verdict"] == "负超额·三轴同号（反指）"


def test_wide_ci_beats_sign_stability_as_a_blocker():
    """样本不足优先：翻号是「不知道方向」，样本不足是「连这个数都不能信」。"""
    s = _load([_cell("new", "2025", 0, +0.9, credible=False),
               _cell("new", "2025", 137, +0.8, credible=False)])["new"]
    assert s["sign_stable"] and not s["sample_adequate"]
    assert not s["credible"]
    assert s["verdict"] == "分格样本不足（单格上涨概率区间>2pp），不定论"


def test_summary_weights_days_and_points_separately():
    """日等权按天加权、行等权按点数加权：两个口径必须分开设，否则同一判据会反号。"""
    s = _load([_cell("box", "2019", 0, +1.0, n=100, days=10),
               _cell("box", "2022", 0, -3.0, n=900, days=300)])["box"]
    assert s["excess_pp"] == pytest.approx((10 * 1.0 + 300 * -3.0) / 310, abs=5e-4)
    assert s["excess_point_pp"] == pytest.approx((100 * 1.0 + 900 * -3.0) / 1000, abs=5e-4)
    assert s["excess_min_pp"] == -3.0 and s["excess_max_pp"] == 1.0
    assert s["n"] == 1000 and s["cells"] == 2 and s["eras"] == ["2019", "2022"]


def test_daymean_falls_back_to_point_mean_when_older_rows_lack_it():
    """老 parquet 没有 excess10_daymean_pp 列时不能 KeyError。"""
    rows = _load([_cell("box", "2019", 0, +0.7)])["box"]
    assert rows["excess_pp"] == 0.7


def test_for_strategy_returns_none_without_the_table(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "FILE", tmp_path / "missing.parquet")
    monkeypatch.setattr(sp, "_cache", None)
    monkeypatch.setattr(sp, "_cache_key", ())
    assert sp.for_strategy("box_oscillate") is None


def test_for_strategy_reloads_after_the_file_is_rebuilt(monkeypatch, tmp_path):
    path = tmp_path / "posterior.parquet"
    _ = path.write_bytes(b"")            # 先占位，mtime/size 变一次算一次重建
    monkeypatch.setattr(sp, "FILE", path)
    monkeypatch.setattr(sp, "_cache", None)
    monkeypatch.setattr(sp, "_cache_key", ())

    def write(daymean):
        pd.DataFrame([_cell("box", "2019", 0, daymean)]).to_parquet(path)

    import os
    import time as _t

    write(1.0)
    assert sp.for_strategy("box")["excess_pp"] == 1.0
    _t.sleep(0.01)
    os.utime(path, (0, 0))               # 时间戳归零 → 与缓存 key 不同，必须重读
    write(2.0)
    assert sp.for_strategy("box")["excess_pp"] == 2.0
    assert sp.for_strategy("unknown") is None
