"""批次 V 冠军族（深跌+缩量）出名单的判据测试。

读数口径必须和回测一致，否则「回测里过门槛的那条」和「线上出的名单」是两回事：
深跌 = 20 日跌幅（裸价比值），缩量 = 当日成交额 / 20 日均额，两者各自在人口内取百分位，各占 0.5。
"""
import pytest

from scripts.qlib_dip_shrink_list import Bar, hole_warning, screen_and_rank


def base_bar(code, name, dip=0.5, amt_today=5e7, amt_hist=2e8, bars=21):
    """dip=1.0 ⇒ 收盘相对 20 个交易日前跌 40%。用 20 根线性铺开，最后一天只跌 2%——
    主板当天跌 40% 是不存在的行情，用它会被「当日封板」筛掉，测试就在测空气。"""
    end = 10.0 * (1 - 0.4 * dip)
    closes = [10.0 + (end - 10.0) * i / (bars - 1) for i in range(bars)]
    return Bar(code=code, name=name, close=closes, amount=[amt_hist] * (bars - 1) + [amt_today])


def test_top_of_list_is_both_deepest_and_most_shrunk():
    bars = [
        base_bar("SH600001", "深跌缩量", dip=1.0, amt_today=2e7),
        base_bar("SH600002", "深跌放量", dip=0.95, amt_today=8e8),
        base_bar("SH600003", "浅跌缩量", dip=0.2, amt_today=5e7),
    ]
    out = screen_and_rank(bars, top=3)
    assert out[0]["code"] == "SH600001"
    assert out[0]["score"] == pytest.approx(1.0)


def test_score_is_the_mean_of_two_percentiles_not_raw_magnitude():
    """两个条件同向排列时，得分必须是 1.0 / 0.5 / 0.0——极端跌幅不能把表拉爆（回测同款百分位口径）。"""
    bars = [
        base_bar("SH600011", "浅跌放量", dip=0.2, amt_today=2e9),
        base_bar("SH600012", "中间", dip=0.5, amt_today=4e8),
        base_bar("SH600013", "深跌缩量", dip=1.0, amt_today=5e7),
    ]
    out = screen_and_rank(bars, top=3)
    assert [r["code"] for r in out] == ["SH600013", "SH600012", "SH600011"]
    assert [round(r["score"], 3) for r in out] == [1.0, 0.5, 0.0]


def test_growth_enterprise_and_star_market_are_not_scored():
    """他 10-26 点名的交易权限口径：300/301/688/689 不进名单（§30）。"""
    bars = [base_bar("SZ300001", "创业板"), base_bar("SH688001", "科创板"), base_bar("SH600031", "主板")]
    assert [r["code"] for r in screen_and_rank(bars, top=10)] == ["SH600031"]


def test_st_and_delisting_names_are_dropped_by_current_name():
    bars = [base_bar("SH600041", "ST 四环"), base_bar("SH600042", "XX退"), base_bar("SH600043", "正常")]
    assert [r["code"] for r in screen_and_rank(bars, top=10)] == ["SH600043"]


def test_thin_liquidity_is_excluded():
    """20 日均额 < 1 亿 = 判不了也买不进，直接剔（docs §17.9）。"""
    bars = [base_bar("SH600051", "大流动", amt_hist=2e8, amt_today=5e7),
            base_bar("SH600052", "薄流动", amt_hist=2e6, amt_today=5e5)]
    assert [r["code"] for r in screen_and_rank(bars, top=10)] == ["SH600051"]


def test_limit_move_day_is_excluded():
    """当日涨跌 ≥9.9% ⇒ 封板/贴地板，尾盘挂单实际成交不了（和回测同一条筛子）。"""
    bars = [Bar(code="SH600061", name="涨停", close=[10.0] * 20 + [11.0], amount=[2e8] * 21),
            base_bar("SH600062", "正常", dip=0.5, amt_today=5e7)]
    assert [r["code"] for r in screen_and_rank(bars, top=10)] == ["SH600062"]


def test_history_shorter_than_the_window_is_excluded_rather_than_padded():
    bars = [Bar(code="SH600071", name="新票", close=[10.0] * 8, amount=[2e8] * 8),
            base_bar("SH600072", "老票")]
    assert [r["code"] for r in screen_and_rank(bars, top=10)] == ["SH600072"]


def test_reported_fields_carry_the_two_reasons():
    bars = [base_bar("SH600081", "a", dip=1.0, amt_today=5e7, amt_hist=2e8),
            base_bar("SH600082", "b", dip=0.5, amt_today=1e8)]
    r = screen_and_rank(bars, top=2)[0]
    assert r["dip20_pct"] == pytest.approx(-40.0)
    assert r["amount_ratio"] == pytest.approx(0.26, abs=0.02)


def test_hole_warning_fires_when_the_panel_loses_a_chunk_of_the_population():
    """§31 那类「沪市队尾漂过合并时刻」的事故，在这里只会表现为人口变少 ⇒ 必须出声，不能安静出半张名单。"""
    counts = [1700] * 20 + [1200]
    warn = hole_warning({"day_counts": counts}, "2026-10-27")
    assert "1200" in warn and "1700" in warn and "别用" in warn


def test_hole_warning_stays_quiet_on_a_normal_day():
    assert hole_warning({"day_counts": [1700, 1698, 1701, 1699, 1700, 1697]}, "2026-10-27") == ""
