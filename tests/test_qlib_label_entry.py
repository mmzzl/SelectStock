"""批次 U：标签的「入场点」维度。

历史所有批次（M/P/Q/R/S/T）用的都是 `Ref($close,-H)/Ref($close,-1)-1`：
特征到 T 收盘，但**入场定在 T+1 收盘**——T 收盘→T+1 收盘这一段在标签里被丢掉。
用户 2026-10-28 的诉求正好是这段：「今天买入，明天上涨」。所以这里加 entry 维度，
默认值必须原样保留历史口径（改了它，前面所有批次读数全部作废）。
"""
import pytest

from scripts.qlib_run import LABEL_ENTRIES, label_formula


def test_default_entry_is_the_historical_next_close_convention():
    assert label_formula(5) == "Ref($close, -5) / Ref($close, -1) - 1"
    assert label_formula(10) == "Ref($close, -10) / Ref($close, -1) - 1"


def test_close_entry_buys_at_the_signal_day_close():
    """entry="close" = 信号日收盘就买，H=1 就是「今天买、明天卖」这一段收益。"""
    assert label_formula(1, entry="close") == "Ref($close, -1) / $close - 1"
    assert label_formula(3, entry="close") == "Ref($close, -3) / $close - 1"


def test_close_entry_still_requires_at_least_one_day_hold():
    with pytest.raises(ValueError):
        label_formula(0, entry="close")


def test_unknown_entry_is_rejected_rather_than_silently_defaulted():
    with pytest.raises(ValueError):
        label_formula(5, entry="gap")


def test_open_entry_with_one_day_horizon_is_the_executable_next_day_leg():
    """entry="open"、H=1 ⇒ `Ref($close,-1)/Ref($open,-1)-1` ＝ **T+1 开盘买、T+1 收盘看**，
    正是 §35.5 那条红线之后唯一做得到的那条腿（名单 17:30 才出，T 收盘买不到）。

    这条不是把 close 口径换个名字：W1 的标签是 T 收→T+1 收，里面那段隔夜（T 收→T+1 开）
    他压根拿不到（§37.12 量出来那段在宽人口上平均 −0.131%/日），拿它当训练目标等于让模型去学一段交易不了的收益。
    """
    assert label_formula(1, entry="open") == "Ref($close, -1) / Ref($open, -1) - 1"
    assert label_formula(2, entry="open") == "Ref($close, -2) / Ref($open, -1) - 1"


def test_next_entry_with_one_day_horizon_is_rejected():
    """批次 U 的 batchU_n1 臂踩到的坑：entry="next" 时分子分母都是 Ref($close,-1)，
    H=1 会让标签恒等于 0（IC 全 NaN），训练照常跑完、什么都不报错。必须直接拒掉。"""
    with pytest.raises(ValueError):
        label_formula(1, entry="next")


def test_dir_entry_asks_whether_tomorrow_is_up_not_how_much():
    """§37.15 批次 Y：用户要的是「今天买、第二天**上涨**」＝命中率，不是平均幅度。

    W3 的标签 `Ref($close,-1)/Ref($open,-1)-1` 量的是涨多少，所以它给出 +0.217pp/日的钱、
    却只多买到 +1.4pp 的天数（§37.14.1 X3）。这条 entry="dir" 用 `Sign(...)` 把同一天的
    同一条腿（T+1 开盘→T+1 收盘，即真正做得到的 E1）压成 −1/0/+1；
    loss="mse" 下预测值 ≈ P(涨)−P(跌)，**按分数排就是按明天红的概率排**。
    除标签外与 W3 一字不动，所以读数差异只能来自"幅度 vs 方向"这一个维度。
    """
    assert "dir" in LABEL_ENTRIES
    assert label_formula(1, entry="dir") == "Sign(Ref($close, -1) / Ref($open, -1) - 1)"
    assert label_formula(2, entry="dir") == "Sign(Ref($close, -2) / Ref($open, -1) - 1)"


def test_dir_entry_rejects_zero_horizon_like_the_others():
    with pytest.raises(ValueError):
        label_formula(0, entry="dir")
