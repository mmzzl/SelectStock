"""可信度表口径测试：era-consistent 分箱边缘、中性分母、credible 门槛、共用落箱。

回归点：
- 分箱边缘只许来自训练区间（edges-end 及以前），否则未来分布从箱边界渗进历史特征（分布泄漏）；
- 中性默认不进分母，但 n_neutral 必须照写；
- credible 同时要 CI 够窄 且 n>=300。
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from stock_selector.conviction import bucket

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_conviction_table.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_conviction_table", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ------------------------------------------------------------ 共用落箱
def test_assign_bin_boundaries():
    assert bucket.assign_bin(-5, (0.0, 1.0)) == 0
    assert bucket.assign_bin(0.0, (0.0, 1.0)) == 0        # 下边缘含
    assert bucket.assign_bin(0.5, (0.0, 1.0)) == 1
    assert bucket.assign_bin(1.0, (0.0, 1.0)) == 1        # 上边缘含
    assert bucket.assign_bin(2.0, (0.0, 1.0)) == 2
    assert bucket.assign_bin(float("nan"), (0.0, 1.0)) == -1
    out = bucket.bin_values(np.array([-1.0, 0.5, 2.0, np.nan]), (0.0, 1.0))
    assert out.tolist() == [0, 1, 2, -1] and out.dtype == np.int16


def test_classify_board():
    assert bucket.classify_board("600000") == "主板"
    assert bucket.classify_board("000001") == "主板"
    assert bucket.classify_board("300750") == "创业板"
    assert bucket.classify_board("688981") == "科创板"
    assert bucket.classify_board("833171") == "北交所"
    assert bucket.classify_board("430047") == "北交所"
    assert bucket.classify_board("920000") == "北交所"


def test_funds_are_not_pooled_into_main_board():
    """1,445 只场内基金以前被默认支归进主板，占后验分母 5.92%、某格 17.12%。

    同样 +10%/-5%/10 日屏障，基金止盈率 22.6% vs 股票 30.9%，混池把股票格子拉偏 2pp
    ——比那张表自己印出来的 95%CI 宽度还大。基金必须单成一类。
    """
    for code in ("159915", "510300", "512880", "520530", "530680", "560000", "588000"):
        assert bucket.classify_board(code) == bucket.BOARD_FUND, code
    assert bucket.BOARD_FUND in bucket.BOARD_CATEGORIES
    # 北交所/创业板的 8/4/300 前缀不能被基金规则吃掉
    assert bucket.classify_board("830799") == bucket.BOARD_BJ


# ------------------------------------------------------------ era-consistent edges
def _samples_with_dates(monkeypatch):
    monkeypatch.setattr(mod, "MIN_TRAIN_N", 5)
    rng = np.random.default_rng(0)
    n_train, n_test = 100, 100
    dates = (["2019-06-01"] * n_train) + (["2021-06-01"] * n_test)
    # 测试区间分布明显不同（更宽），用全样本分位会得到不同边缘
    bias = np.r_[rng.uniform(-0.02, 0.02, n_train), rng.uniform(-1, 1, n_test)]
    pos = np.r_[rng.uniform(0, 1, n_train), rng.uniform(0, 1, n_test)]
    vol = np.r_[rng.uniform(0.8, 1.2, n_train), rng.uniform(0.5, 5.0, n_test)]
    return pd.DataFrame({"date": pd.to_datetime(dates), "board": "主板", "label": 1,
                         "bias20": bias, "pos60": pos, "vol_ratio": vol})


def test_edges_come_from_train_range_only(monkeypatch):
    s = _samples_with_dates(monkeypatch)
    edges = mod.compute_edges(s, "2020-12-31")
    train = s[s["date"] <= pd.Timestamp("2020-12-31")]
    expected = train["bias20"].quantile([1 / 3, 2 / 3]).to_numpy()
    assert edges["bias20"] == (expected[0], expected[1])
    # 若误用全样本分位，bias 边缘会被测试区间的极值拉远——证明这个断言抓得住泄漏
    full = s["bias20"].quantile([1 / 3, 2 / 3]).to_numpy()
    assert abs(full[0] - expected[0]) > 1e-3


# ------------------------------------------------------------ 中性分母 / credible
def _binned(board, labels, bias_bin=0, pos_bin=0, vol_bin=0):
    return pd.DataFrame({
        "board": [board] * len(labels),
        "bias_bin": bias_bin, "pos_bin": pos_bin, "vol_bin": vol_bin,
        "label": np.asarray(labels, dtype=np.int8),
    })


_EDGES = {"bias20": (0.0, 1.0), "pos60": (0.0, 1.0), "vol_ratio": (0.0, 1.0)}


def test_neutral_excluded_from_rate_but_still_counted():
    labels = [1] * 10 + [0] * 10 + [-1] * 30
    binned = _binned("主板", labels)

    excl = mod.summarize(binned, {"bias20": (0, 1), "pos60": (0, 1), "vol_ratio": (0, 1)},
                         10, 0.05, 0.10, include_neutral=False)
    r = excl.iloc[0]
    assert r["n_success"] == 10 and r["n_failure"] == 10 and r["n_neutral"] == 30
    assert abs(r["hit_rate"] - 0.5) < 1e-9                 # Beta(11,11) 均值

    incl = mod.summarize(binned, {"bias20": (0, 1), "pos60": (0, 1), "vol_ratio": (0, 1)},
                         10, 0.05, 0.10, include_neutral=True)
    r2 = incl.iloc[0]
    assert r2["n_neutral"] == 30
    assert r2["hit_rate"] < r["hit_rate"]                  # 中性并入分母后止盈率被稀释
    assert r2["hit_rate"] == round(11 / 52, 6)             # Beta(11,41) 均值，与落盘精度一致


def test_credible_needs_narrow_ci(monkeypatch):
    big = _binned("主板", [1] * 5000 + [0] * 5000)                    # n=10000 → CI ≈1.96pp
    small = _binned("主板", [1] * 100 + [0] * 100, bias_bin=1)        # 另一格：n=200，且 n<300
    table = mod.summarize(pd.concat([big, small], ignore_index=True),
                          {"bias20": (0, 1), "pos60": (0, 1), "vol_ratio": (0, 1)},
                          horizon=10, stop=0.05, take=0.10, include_neutral=False)
    by_n = table.set_index("n")
    assert by_n.loc[10000, "credible"] and by_n.loc[10000, "ci_width_pp"] <= 2.0
    assert not by_n.loc[200, "credible"]
    assert list(table.columns) == mod.TABLE_COLUMNS
