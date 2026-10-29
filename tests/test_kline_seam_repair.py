"""除权前一根被复权快照改写（docs §48.5）的定点修复判据。

形状：库里除权日的**前一根**K 线被乘了一次 step（前复权快照只重写队尾那根），
于是凭空多出一根「跌 30%」的假摔。两个条件同时成立才算，少一个就是天天误报：
① 那根存储值隐含的日跌幅**超出该板块涨跌停幅度**（真实市场做不出来）；
② 那个跌幅**正好等于那次除权的比例**（step−1 附近，容差 3pp）——否则就是别的病（停牌、退市整理、数据源自己出错）。
最后一道闸门是上游裸价：库里 ≈ 上游 × step 且库里 ≠ 上游，才算确证。
"""
import importlib.util

SCRIPT = "scripts/kline_seam_repair.py"


def _load():
    spec = importlib.util.spec_from_file_location("kline_seam_repair", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_board_limit_follows_the_first_two_digits():
    """主板 10%、创业板/科创板 20%、北交所 30%；ST 的 5% 判不了就不加码（宁少报）。"""
    mod = _load()
    assert mod.board_limit("600519") == 0.10
    assert mod.board_limit("000001") == 0.10
    assert mod.board_limit("300803") == 0.20
    assert mod.board_limit("688200") == 0.20
    assert mod.board_limit("830799") == 0.30
    assert mod.board_limit("430047") == 0.30
    assert mod.board_limit("512390") is None       # 基金/指数：没有涨跌停这道物理约束


def test_candidate_needs_impossible_drop_that_the_step_can_explain():
    """判据：那根隐含跌幅**超出该板块涨跌停**（真实做不出来），**又落在这次除权可能造出的区间里**。

    一根被乘过 step 的 K 线，隐含跌幅 = (1+真实涨跌)×step − 1，真实涨跌在 ±涨跌停之间 ⇒
    可能区间就是 [(1−L)×step−1, (1+L)×step−1]。区间外的深跌是别的病（停牌、退市整理、源出错），不在这里修。
    """
    mod = _load()
    # 300803 2026-09-21：真值 82.45（跌 17.55%，20% 板以内）× step 0.6897 = 56.87 ⇒ 隐含跌 43.13%
    got = mod.candidate({"close": 56.87}, {"close": 100.0}, 0.6897, "300803")
    assert got["implied"] == -0.4313
    assert got["band"] == (-0.44824, -0.17236)
    # 创业板 20% 板：跌 17.55% 是正常行情 ⇒ 不是洞，别报
    assert mod.candidate({"close": 82.45}, {"close": 100.0}, 0.6897, "300803") is None
    # 跌 60% 超出 step 能解释的区间 ⇒ 不是这根柱的事，交给上游体检
    assert mod.candidate({"close": 40.0}, {"close": 100.0}, 0.6897, "300803") is None
    # 与 step 落在同一区间，但跌幅还在涨跌停以内 ⇒ 真实行情就能长这样，别动
    assert mod.candidate({"close": 93.0}, {"close": 100.0}, 0.85, "600519") is None


def test_candidate_is_not_compared_against_a_missing_previous_bar():
    mod = _load()
    assert mod.candidate({"close": 56.87}, None, 0.6897, "300803") is None
    assert mod.candidate(None, {"close": 100.0}, 0.6897, "300803") is None


def test_upstream_confirms_only_when_stored_equals_raw_times_step():
    mod = _load()
    ok, ratio = mod.confirm(56.87, 82.45, 0.6897)
    assert ok and abs(ratio - 0.6897) < 1e-3
    # 库里本来就是裸价 ⇒ ratio≈1，没什么可修
    assert mod.confirm(82.45, 82.45, 0.6897)[0] is False
    # 上游跟 step 也对不上（差 12%）⇒ 不能断定是复权快照改的，留给人工
    assert mod.confirm(50.0, 82.45, 0.6897)[0] is False


def test_step_tolerance_holds_to_the_exchange_formula_not_tencents():
    """实测那根被改写的柱子，比值跟交易所公式推的 step 会差 0.7%~1.8%（300533 0.6873 vs 0.6750）。

    根因不是我算错台阶，是**改写用的那个快照**由源自己按四舍五入的除权参考价算，两边本来就不必分毫不差。
    判据的权威是「库里 ≠ 上游裸价」，step 只是解释得通的旁证 ⇒ 3% 以内算同一件事，12% 就不算了。
    """
    mod = _load()
    assert mod.confirm(18.186, 26.46, 0.675009)[0] is True      # 差 1.79%
    assert mod.confirm(94.65, 94.65, 0.998415)[0] is False      # 本来就是裸价（真跌停那天）


def test_patch_carries_the_four_price_fields_and_nothing_else():
    """只改价格四根柱；量、额不动（复权不改量），标签也不在这里改（那是 relabel 的事）。"""
    mod = _load()
    bar = {"open": 82.0, "high": 83.0, "low": 81.0, "close": 82.45,
           "volume": 1234, "amount": 9999.0, "date": "2026-09-21 15:00"}
    got = mod.patch(bar)
    assert set(got) == {"open", "high", "low", "close"}
    assert got == {"open": 82.0, "high": 83.0, "low": 81.0, "close": 82.45}
    assert "amount" not in got and "date" not in got


def test_upstream_bars_are_keyed_by_plain_date():
    mod = _load()
    rows = [{"day": "2026-09-21", "open": "82.0", "high": "83.0", "low": "81.0",
             "close": "82.45", "volume": "100"},
            {"day": "2026-10-08 15:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
    got = mod.by_date(rows)
    assert got["2026-09-21"]["close"] == 82.45          # 字符串要转成数
    assert got["2026-10-08"]["close"] == 1.5            # 带时刻的按日切
    assert mod.by_date(None) == {}
