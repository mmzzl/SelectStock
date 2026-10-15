"""可信度样本/标签口径测试：屏障顺序、无前视、中性、板块归类。

回归的是这一类缺陷：同一根 bar 上 low<=止损 与 high>=止盈 同时成立时若按「止盈优先」，
就会把「先被打止损」算成成功；以及特征侧误用 shift(-k) 把未来渗进样本日。
"""

import importlib.util
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_conviction_samples.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_conviction_samples", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---------------------------------------------------------------- label_barriers
def test_take_before_stop_is_success():
    """bar1 先触止盈(112>=110)，止损(94.9<=95)要到 bar3 才触 → 成功。"""
    high = [100, 112, 100, 100, 100]
    low = [100, 100, 100, 94.9, 100]
    hit_stop, hit_take, label, _, _ = mod.label_barriers(
        close=[100, 100, 100, 100, 100], high=high, low=low,
        horizon=3, stop=0.05, take=0.10)
    assert label[0] == 1 and hit_take[0] == 1 and hit_stop[0] == 0


def test_stop_before_take_is_failure():
    high = [100, 100, 100, 112, 100]
    low = [100, 94.9, 100, 100, 100]
    hit_stop, hit_take, label, _, _ = mod.label_barriers(
        close=[100, 100, 100, 100, 100], high=high, low=low,
        horizon=3, stop=0.05, take=0.10)
    assert label[0] == 0 and hit_stop[0] == 1 and hit_take[0] == 0


def test_same_bar_both_stop_takes_precedence():
    """同一根既破止损又破止盈：止损优先（与 exit_pnl / kernel_strategy 一致）。"""
    high = [100, 112, 100, 100]
    low = [100, 94.9, 100, 100]
    hit_stop, hit_take, label, _, _ = mod.label_barriers(
        close=[100, 100, 100, 100], high=high, low=low,
        horizon=3, stop=0.05, take=0.10)
    assert label[0] == 0 and hit_stop[0] == 1 and hit_take[0] == 0


def test_neither_barrier_is_neutral():
    high = [100, 103, 104, 102]
    low = [100, 98, 99, 97]
    hit_stop, hit_take, label, fwd_low, fwd_high = mod.label_barriers(
        close=[100, 100, 100, 100], high=high, low=low,
        horizon=3, stop=0.05, take=0.10)
    assert label[0] == -1 and hit_stop[0] == 0 and hit_take[0] == 0
    assert fwd_low[0] == 97 and fwd_high[0] == 104      # 前向窗口确实是 t+1..t+3


def _frame(closes, highs, lows, **cols):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "close": closes, "high": highs, "low": lows,
        "ma20": cols.get("ma20", list(closes)),
        "pos60": cols.get("pos60", [0.5] * n),
        "vol_ratio": cols.get("vol_ratio", [1.0] * n),
        "vol_z": cols.get("vol_z", [0.0] * n),
        "rsi14": cols.get("rsi14", [50.0] * n),
        "adx14": cols.get("adx14", [25.0] * n),
        "atr14": cols.get("atr14", [1.0] * n),
        "lu": cols.get("lu", [False] * n),
    })


def test_features_come_only_from_t_no_lookahead():
    """改未来几根 bar 不改变前面样本日的特征列（label 可变，特征不可变）。"""
    closes = [10, 10, 10, 10, 10, 10]
    highs = [10, 10, 10, 10, 10, 10]
    lows = [10, 10, 10, 10, 10, 10]
    ma20 = [9, 9, 9, 9, 9, 9]
    f1 = mod.build_samples_for_frame(_frame(closes, highs, lows, ma20=ma20),
                                     horizon=2, stop=0.05, take=0.10, code="600000")
    # 未来两根（index 4/5）疯狂变动：特征侧不该有任何反应
    closes2 = closes[:4] + [999, 999]
    highs2 = highs[:4] + [999, 999]
    lows2 = lows[:4] + [1, 1]
    f2 = mod.build_samples_for_frame(_frame(closes2, highs2, lows2, ma20=ma20),
                                     horizon=2, stop=0.05, take=0.10, code="600000")
    feat = ["close", "bias20", "pos60", "vol_ratio", "vol_z", "rsi14", "adx14", "atr_n", "lu"]
    a = f1.loc[f1["date"] < pd.Timestamp("2020-01-05"), feat].reset_index(drop=True)
    b = f2.loc[f2["date"] < pd.Timestamp("2020-01-05"), feat].reset_index(drop=True)
    assert a.equals(b)
    # bias20 就是 close/ma20-1，不含未来
    row = f1.iloc[0]
    assert row["bias20"] == row["close"] / 9.0 - 1.0


def test_build_samples_drops_incomplete_forward_window():
    """末尾 horizon 根没有完整前向窗口，不落样本（宁缺毋滥）。"""
    n = 6
    fr = mod.build_samples_for_frame(_frame([10] * n, [10] * n, [10] * n),
                                     horizon=2, stop=0.05, take=0.10, code="600000")
    assert len(fr) == 4                                   # 0..3 才有完整的 t+1..t+2


def test_board_and_label_dtypes():
    f = mod.build_samples_for_frame(
        _frame([10, 10, 10, 10], [10, 10, 10, 10], [10, 10, 10, 10]),
        horizon=2, stop=0.05, take=0.10, code="300001")
    assert set(f["board"].unique()) == {"创业板"}
    assert set(f["label"].unique()) <= {-1, 0, 1}
    assert f["lu"].dtype == "int8" and f["label"].dtype == "int8"
