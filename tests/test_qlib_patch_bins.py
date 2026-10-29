"""面板 .bin 定点改格（接缝修复的传播侧，任务 #89）。

`.bin` 的形状是「首格 = 这只票第一天在面板日历里的下标，之后连续一格一天」，
所以改一格只能按 `下标 − 首格` 定位；算错一位就把之后每一天都挪走（比空洞严重得多）。
8003 打分、回测、训练读的都是 .bin，不是 Mongo ⇒ Mongo 修完不传播等于没修。
"""
import importlib.util
import math

import numpy as np

SCRIPT = "scripts/qlib_patch_bins.py"


def _load():
    spec = importlib.util.spec_from_file_location("qlib_patch_bins", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAL = ["2026-05-20", "2026-05-21", "2026-05-22", "2026-05-25", "2026-05-26"]


def _arr():
    # 首格 1 = 这只票从日历第 1 天开始，之后 4 格
    return np.array([1.0, 10.0, 20.0, float("nan"), 40.0], dtype="<f")


def test_patch_writes_only_the_cell_for_that_date():
    mod = _load()
    arr = _arr()
    got = mod.patch(arr, {d: i for i, d in enumerate(CAL)}, {"2026-05-25": 33.0})
    assert got["changed"] == 1 and got["out_of_range"] == []
    # 首格说「从日历第 1 天起」⇒ 05-25（下标 3）落在 arr[3]，那格原本正是 NaN
    assert arr[3] == 33.0
    assert arr[1:3].tolist() == [10.0, 20.0] and arr[4] == 40.0   # 别的格子一个字没动
    assert arr[0] == 1.0                                          # 首格（起始下标）绝不改


def test_patch_refuses_days_outside_the_bin_range():
    """日历里有这天、但这只票的 bin 没覆盖到 ⇒ 不能往后垫格子，报出来让人去走 dump_all。"""
    mod = _load()
    arr = _arr()
    got = mod.patch(arr, {d: i for i, d in enumerate(CAL)}, {"2026-05-20": 99.0})
    assert got["changed"] == 0 and got["out_of_range"] == ["2026-05-20"]
    assert arr[0:3].tolist() == [1.0, 10.0, 20.0] and math.isnan(arr[3]) and arr[4] == 40.0
    arr2 = _arr()
    later = {d: i for i, d in enumerate(CAL + ["2026-05-27"])}
    got2 = mod.patch(arr2, later, {"2026-05-27": 55.0})
    assert got2["changed"] == 0 and got2["out_of_range"] == ["2026-05-27"]
    assert arr2[4] == 40.0


def test_patch_is_idempotent():
    mod = _load()
    arr = _arr()
    idx = {d: i for i, d in enumerate(CAL)}
    mod.patch(arr, idx, {"2026-05-22": 21.0})
    got = mod.patch(arr, idx, {"2026-05-22": 21.0})
    assert got["changed"] == 0


def test_price_fields_are_the_four_we_carry_and_vwap_follows_the_pair():
    """库里改的是价格四列；面板的 vwap 是 amount/volume 推的（不改），
    所以只改 close 那一列会让 `$vwap/$close` 变——必须跟着按面板自己的口径重算。"""
    mod = _load()
    assert mod.PRICE_FIELDS == ("open", "high", "low", "close")
    assert mod.vwap_of({"amount": 2000.0, "volume": 100.0}) == 20.0
    assert mod.vwap_of({"amount": None, "volume": 100.0}) is None   # 没额就不硬编 close
