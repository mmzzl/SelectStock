"""可信度特征装配测试：训练侧与线上侧必须拼出同一个向量。

回归点（这类错不报错，只会悄悄给出错的概率）：
- 列序 = FEATURES，board 走 BOARD_CATEGORIES 下标；
- bias20 线上没有列，用 close/ma20-1 现算，与样本表里的 bias20 列同式；
- atr_n 是 atr14/close 的比值（绝对 ATR 跨价位不可比，屏障按比例定义）；
- 核心三列（bias20/pos60/vol_ratio）任一不可判 → ok=False，调用方退化；
- 其余列缺失 → NaN 交给 LightGBM 的缺失分支，不硬填 0。
"""
import math

from stock_selector.conviction import bucket, features as fx


def test_feature_order_and_board_index():
    row = {"code": "300750", "close": 10.0, "ma20": 10.0, "pos60": 0.5, "vol_ratio": 1.2,
           "vol_z": 0.8, "rsi14": 55.0, "adx14": 20.0, "atr14": 0.5, "lu": 1}
    vec, ok = fx.row_to_vector(row)
    assert ok and len(vec) == len(fx.FEATURES)
    assert vec[:8] == [0.0, 0.5, 1.2, 0.8, 55.0, 20.0, 0.05, 1.0]
    assert vec[fx.BOARD_COL] == bucket.BOARD_CATEGORIES.index("创业板")
    assert fx.FEATURES == [*fx.NUMERIC_FEATURES, "board"]


def test_bias20_falls_back_to_close_over_ma20():
    """样本表里 bias20 是现成列，线上只有 close/ma20——两边走同一个式子。"""
    row = {"code": "600000", "close": 10.0, "pos60": 0.3, "vol_ratio": 0.9}
    assert fx.bias20({**row, "ma20": 10.1}) == row["close"] / 10.1 - 1.0
    assert fx.bias20({**row, "bias20": 0.12}) == 0.12       # 有现成列就用它
    assert fx.bias20(row) is None                            # 没 ma20 就不判，不猜 0


def test_missing_optional_features_go_nan_not_zero():
    row = {"code": "600000", "close": 10.0, "ma20": 10.0, "pos60": 0.3, "vol_ratio": 0.9}
    vec, ok = fx.row_to_vector(row)
    assert ok                                       # 核心三列齐就还能打分
    for name in ("vol_z", "rsi14", "adx14", "atr_n", "lu"):
        assert math.isnan(vec[fx.NUMERIC_FEATURES.index(name)])


def test_core_feature_missing_disqualifies_row():
    for key, row in (("pos60", {"code": "600000", "close": 10.0, "ma20": 10.0,
                                "vol_ratio": 0.9}),
                     ("ma20", {"code": "600000", "close": 10.0, "pos60": 0.3,
                               "vol_ratio": 0.9})):
        vec, ok = fx.row_to_vector(row)
        assert ok is False, key


def test_nan_and_junk_values_are_not_finite():
    row = {"code": "600000", "close": float("nan"), "ma20": 10.0, "pos60": "abc",
           "vol_ratio": float("inf")}
    vec, ok = fx.row_to_vector(row)
    assert ok is False


def test_unknown_board_row_still_scores_on_main_board():
    """classify_board 未知前缀归主板（与建样本同口径），不会出现下标取不到。"""
    assert fx.board_code({"code": "999999"}) == bucket.BOARD_CATEGORIES.index(bucket.BOARD_MAIN)


def test_board_index_rejects_unknown_name():
    assert bucket.board_index("科创板") == 2
    assert bucket.board_index("新三板") is None
