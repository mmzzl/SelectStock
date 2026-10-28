"""「明日榜」影子盘的纯算子测试（docs §37.6 开关①）。

四条判据各自对应一个会真出错的地方：
- 取前 K 必须**同日重跑同一张榜**（tie-break 不写死，每天榜单会抖动，前瞻账就没法对账）。
- E1 用的是 T+1 开盘价，不是 T 收盘——§35.5 那条时间红线：名单 17:30 才出，T 收盘买不到。
- 缺次日行情必须标 pending 而**不是按 0 入账**：0 意思是"没跌"，缺数意思是"不知道"，
  把不知道当没跌会把胜率系统性抬高（§33 报数戒律里同一类错）。
- 台账按 (日期, 代码) 幂等：补跑不能把同一笔记两遍。
"""
import pandas as pd
import pytest

from stock_selector_qlib import nextday
from stock_selector_qlib.nextday import (
    POOL_ROW, BAND_ROW, band_day, band_plan, daily_excess, e1_pct, e2_pct, excess,
    merge_ledger_rows, net_after_cost, pool_baseline, replay_band, top_by_score, hold_pct,
)


def test_top_by_score_takes_highest_and_breaks_ties_by_code():
    s = pd.Series({"SH600000": 0.5, "SZ000001": 0.9, "SZ000002": 0.9, "SH600001": 0.1})
    assert top_by_score(s, 2) == ["SZ000001", "SZ000002"]
    assert top_by_score(s[::-1], 2) == ["SZ000001", "SZ000002"]


def test_top_by_score_short_pool_returns_all_sorted():
    s = pd.Series({"SH600000": 0.5, "SZ000001": 0.9})
    assert top_by_score(s, 30) == ["SZ000001", "SH600000"]


def test_top_by_score_rejects_non_positive_k():
    with pytest.raises(ValueError):
        top_by_score(pd.Series({"SH600000": 0.5}), 0)


def test_e1_is_next_open_to_next_close_not_signal_close():
    # 入场只用 T+1 开盘价：名单 17:30 才出，T 收盘价根本买不到（§35.5 那条红线）
    assert round(e1_pct(next_open=9.8, next_close=10.2), 4) == 4.0816


def test_e2_is_next_open_to_day_after_open():
    assert round(e2_pct(next_open=10.0, next_next_open=10.15), 4) == 1.5


def test_missing_leg_is_none_not_zero():
    assert e1_pct(next_open=9.8, next_close=None) is None
    assert e2_pct(next_open=None, next_next_open=10.0) is None
    assert e1_pct(next_open=0.0, next_close=10.2) is None


def test_net_after_cost_subtracts_one_round_trip():
    assert round(net_after_cost(0.227), 4) == 0.107
    assert round(net_after_cost(0.05), 4) == -0.07


def test_merge_ledger_is_idempotent_and_keeps_settled_legs():
    day = "2026-10-28"
    first = [{"signal_date": day, "instrument": "SH600000", "e1_pct": 1.0, "settled": True},
             {"signal_date": day, "instrument": "SZ000001", "e1_pct": None, "settled": False}]
    # 补跑：同一笔带来已结算的值，另一笔还是缺数
    again = [{"signal_date": day, "instrument": "SH600000", "e1_pct": 1.0, "settled": True},
             {"signal_date": day, "instrument": "SZ000001", "e1_pct": 2.5, "settled": True},
             {"signal_date": day, "instrument": "SZ000002", "e1_pct": None, "settled": False}]
    out = merge_ledger_rows(first, again)
    assert len(out) == 3
    by = {r["instrument"]: r for r in out}
    assert by["SZ000001"]["e1_pct"] == 2.5 and by["SZ000001"]["settled"] is True
    assert by["SH600000"]["e1_pct"] == 1.0
    assert by["SZ000002"]["settled"] is False


def test_merge_ledger_never_overwrites_a_settled_number_with_missing():
    day = "2026-10-28"
    old = [{"signal_date": day, "instrument": "SH600000", "e1_pct": 1.0, "settled": True}]
    new = [{"signal_date": day, "instrument": "SH600000", "e1_pct": None, "settled": False}]
    out = {r["instrument"]: r for r in merge_ledger_rows(old, new)}
    assert out["SH600000"]["e1_pct"] == 1.0 and out["SH600000"]["settled"] is True


def test_pool_row_is_the_equal_weight_baseline_not_the_picks():
    """§37 的判据是「超额 = 名单 − 同池等权」，所以账上必须每天单独存一条 __POOL__ 基准腿。

    没有这条腿，前瞻账只能报绝对值 ⇒ 涨市里好看、跌市里难看，读数和回测里那个 +0.099pp 根本不是一回事。
    """
    bars = {"SH600000": (10.0, 10.2, 10.1), "SZ000001": (20.0, 20.6, 20.2)}   # (T+1 开, T+1 收, T+2 开)
    base = pool_baseline(bars)
    assert round(base["e1_pct"], 4) == 2.5           # mean(+2.0%, +3.0%)
    assert round(base["e2_pct"], 4) == 1.0           # mean(+1.0%, +1.0%)
    assert base["instrument"] == POOL_ROW and base["settled"] is True


def test_pool_baseline_ignores_members_with_a_missing_leg():
    bars = {"SH600000": (10.0, 10.2, 10.1), "SZ000001": (10.0, None, 10.1)}
    base = pool_baseline(bars)
    assert round(base["e1_pct"], 4) == 2.0           # 只有第一支算得出 E1
    assert round(base["e2_pct"], 4) == 1.0           # E2 两支都在


def test_pool_baseline_is_none_when_no_leg_is_known():
    assert pool_baseline({"SH600000": (None, 10.2, None)})["e1_pct"] is None


def test_excess_subtracts_the_pool_row():
    picks = [{"e1_pct": 1.0}, {"e1_pct": 3.0}]
    base = {"e1_pct": 2.5, "e2_pct": None}
    assert round(excess(picks, base, "e1_pct"), 4) == -0.5      # mean(1,3)=2.0 − 2.5
    assert excess(picks, base, "e2_pct") is None                 # 基准缺腿 ⇒ 这天的超额算不出来
    assert excess([], {"e1_pct": 2.5}, "e1_pct") is None         # 名单空也不算 0


def test_daily_excess_pairs_each_day_with_its_own_pool_row():
    """前瞻账的日序列必须是**超额**（§37 判据），基准腿单独存，所以要按天配平。

    涨市里名单绝对收益 +2%、同池 +2.5% ⇒ 超额是负的；只看绝对值会把"跟着大盘涨"读成"选股有效"。
    """
    rows = [
        {"signal_date": "2026-10-26", "instrument": "SH600000", "rank": 1, "e1_pct": 2.0, "settled": True},
        {"signal_date": "2026-10-26", "instrument": "SZ000001", "rank": 2, "e1_pct": 4.0, "settled": True},
        {"signal_date": "2026-10-26", "instrument": POOL_ROW, "rank": 0, "e1_pct": 2.5, "settled": True},
        {"signal_date": "2026-10-27", "instrument": "SH600000", "rank": 1, "e1_pct": -1.0, "settled": True},
        {"signal_date": "2026-10-27", "instrument": POOL_ROW, "rank": 0, "e1_pct": -3.0, "settled": True},
        {"signal_date": "2026-10-28", "instrument": "SH600000", "rank": 1, "e1_pct": 5.0, "settled": True},
        # 这一天没有基准腿 ⇒ 超额算不出来，不能拿绝对值冒充
    ]
    by = daily_excess(rows)
    assert round(by.loc["2026-10-26", "e1"], 4) == 0.5      # mean(2,4)=3.0 − 2.5
    assert round(by.loc["2026-10-27", "e1"], 4) == 2.0      # −1.0 − (−3.0)
    assert by.loc["2026-10-28", "e1"] is None or pd.isna(by.loc["2026-10-28", "e1"])


def test_daily_excess_ignores_unsettled_picks():
    rows = [
        {"signal_date": "2026-10-26", "instrument": "SH600000", "rank": 1, "e1_pct": None, "settled": False},
        {"signal_date": "2026-10-26", "instrument": POOL_ROW, "rank": 0, "e1_pct": 1.0, "settled": True},
    ]
    assert pd.isna(daily_excess(rows).loc["2026-10-26", "e1"])


# ---------------------------------------------------------------------------
# band 组合规则（docs §37.9 L3：前 30 建仓 / 跌出前 60 才卖）
# 影子账要前瞻的是这条**组合规则**，不是每天重排的 E1——E1 扣费后超额是负的（−0.021pp）。
# ---------------------------------------------------------------------------


def _ranked(n):
    return [f"SH{600000 + i:07d}" for i in range(n)]     # 名次 1..n，代码递增且互不相同


def test_band_holds_everything_inside_the_band_and_fills_from_top_k():
    ranked = _ranked(80)
    held = ranked[40:55]                                  # 名次 41–55，都还在 band 内
    plan = band_plan(held, ranked, k=30, band=60)
    assert plan["exits"] == []                            # 没跌出前 60 ⇒ 一支都不卖
    assert plan["keep"] == held
    assert plan["entries"] == ranked[:15]                 # 只从**前 30** 里补，补到 30 支为止


def test_band_sells_only_names_that_fell_out_of_the_band():
    ranked = _ranked(80)
    held = [ranked[10], ranked[59], ranked[60], "SH9999999"]   # 名次 11、60、61、今天没打分
    plan = band_plan(held, ranked, k=30, band=60)
    assert plan["exits"] == [ranked[60], "SH9999999"]     # 61 名之后 + 判不了分的都卖
    assert plan["keep"] == [ranked[10], ranked[59]]


def test_band_does_not_buy_rank_31_when_top_k_is_already_held():
    """建仓只从**前 k 名**里取：前 30 名已经都在手里 ⇒ 第 31 名再高分也不买。"""
    ranked = _ranked(80)
    plan = band_plan(ranked[:30], ranked, k=30, band=60)
    assert plan["exits"] == [] and plan["entries"] == []
    assert ranked[30] not in plan["hold"]


def test_band_entries_come_from_top_k_even_when_held_names_are_low_ranked():
    ranked = _ranked(10)
    plan = band_plan([ranked[3], ranked[4]], ranked, k=3, band=6)   # 手里两支都在 band 内、但名次靠后
    assert plan["exits"] == []
    assert plan["entries"] == [ranked[0]]                            # 只补 1 个名额，且从前 3 名里取
    assert plan["hold"] == [ranked[0], ranked[3], ranked[4]]         # 按名次升序规范化


def test_band_allows_short_portfolio_when_scoring_is_thin():
    """打分只覆盖 5 支时，凑不满 30 就**空着**——不许拿第 6 名之后的东西硬填，也不许记成满仓。"""
    ranked = _ranked(5)
    plan = band_plan([], ranked, k=30, band=60)
    assert plan["entries"] == ranked and len(plan["hold"]) == 5


def test_band_plan_is_pure_and_order_preserving():
    ranked = _ranked(70)
    held = [ranked[50], ranked[3], ranked[61]]
    a = band_plan(held, ranked)
    b = band_plan(list(reversed(held)), ranked)
    assert sorted(a["keep"]) == sorted([ranked[50], ranked[3]]) and a["keep"] == b["keep"]
    assert a["hold"] == sorted(a["keep"] + a["entries"])  # 明天手里的东西说得清
    assert a["hold"] != held                              # 返回的是规范化后的新列表


# ---------------------------------------------------------------------------
# band 规则的**账**：影子盘要前瞻的是这条组合规则（§37.9 L3），得能按天结算、能摊成本
# ---------------------------------------------------------------------------


def _row(d, inst, rank, e1=None, hold=None, gap=None, buyable=True):
    return {"signal_date": d, "instrument": inst, "rank": rank, "score": 1.0,
            "e1_pct": e1, "hold_pct": hold, "gap_pct": gap,
            "settled": e1 is not None, "buyable": buyable}


def _base(d, e0, e1=0.0):
    return {"signal_date": d, "instrument": POOL_ROW, "rank": 0, "e0_pct": e0, "e1_pct": e1,
            "settled": True}


# --- 隔夜腿本身：裸价面板上跨除权那一格必须判"不可算"，不能当成暴跌 -------------------------


def test_gap_pct_is_the_overnight_leg_with_the_exrights_guard():
    assert round(nextday.gap_pct(10.0, 10.9), 4) == 9.0     # T 收 10 → T+1 开 10.9
    assert round(nextday.gap_pct(10.0, 9.9), 4) == -1.0
    assert nextday.gap_pct(10.0, 5.0) is None               # −50% 多半是除权，不是暴跌
    assert nextday.gap_pct(None, 9.9) is None and nextday.gap_pct(10.0, 0) is None


def test_band_day_new_buys_take_only_the_intraday_leg():
    """建仓日开盘簿是空的 ⇒ 当天只有日内腿，不许凭空长出一条隔夜腿。"""
    d = "2026-10-28"
    rows = [_base(d, 0.5), _row(d, "SH600000", 1, e1=1.0, gap=None),
            _row(d, "SZ000001", 2, e1=3.0, gap=None)]
    res = band_day([], rows, k=2, band=3)
    assert round(res["gross_pct"], 4) == 2.0                # mean(1.0, 3.0)，隔夜腿 = 0
    assert round(res["overnight_pct"], 4) == 0.0 and res["n_open"] == 0
    assert round(res["excess_pp"], 4) == 1.5


def test_band_day_continuations_earn_the_night_and_the_day_as_two_legs():
    """续持 = 先吃隔夜 `gap_pct`（T收→T+1开）再吃日内 `e1_pct`（T+1开→T+1收），两腿串起来乘。

    `hold_pct`（T收→T+1收）在这里是**干扰项**：再平衡发生在开盘，组合收益必须按"隔夜×日内"拆，
    否则同一条账会出现两种时间基。
    """
    d = "2026-10-28"
    rows = [_base(d, 0.0), _row(d, "SH600000", 1, e1=-0.5, hold=99.0, gap=1.0)]
    res = band_day(["SH600000"], rows, k=1, band=3)
    assert round(res["overnight_pct"], 4) == 1.0
    assert round(res["intraday_pct"], 4) == -0.5
    assert round(res["gross_pct"], 4) == round(1.01 * 0.995 * 100 - 100, 4)   # +0.495%


def test_band_day_books_the_exit_position_overnight_leg():
    """**当天开盘卖出的票，隔夜那条腿必须记账**——它在 T 收盘还在手里，到 T+1 开盘才走。

    这是 §37.13 修掉的那个洞：早先的写法让卖出的票"当天一分钱收益都没有"，
    而这个窗口的隔夜腿是负的（同池基准 −0.131%/日），丢掉它等于**凭空给策略加分**。
    """
    d = "2026-10-28"
    rows = [_base(d, 0.0), _row(d, "SH600000", 1, e1=2.0, gap=1.0)]          # 续持
    res = band_day(["SH600000", "SZ000009"], rows, k=1, band=1,
                   fetch=lambda dd, inst: {"gap_pct": -3.0, "e1_pct": None})  # SZ000009 掉出榜 ⇒ 今早卖
    assert res["exits"] == ["SZ000009"] and res["n_exit"] == 1
    assert round(res["overnight_pct"], 4) == -1.0            # mean(+1.0, −3.0)
    assert round(res["intraday_pct"], 4) == 2.0              # 收盘簿只剩 SH600000
    assert round(res["gross_pct"], 4) == round(0.99 * 1.02 * 100 - 100, 4)    # −0.98%


def test_band_day_costs_only_the_sides_it_actually_traded():
    d = "2026-10-28"
    rows = [_base(d, 0.5), _row(d, "SH600000", 1, e1=1.0, gap=0.5),
            _row(d, "SZ000001", 2, e1=3.0, gap=0.5), _row(d, "SZ000002", 3, e1=9.0, gap=9.0)]
    # 手里只有 D（今天没打分 ⇒ 卖出）；买 2 卖 1 = 3 条边，摊在 2 支持仓上
    res = band_day(["SH7777777"], rows, k=2, band=3)
    assert round(res["cost_pp"], 6) == round(3 * nextday_side_pp() / 2, 6)
    assert round(res["turnover"], 4) == 0.75               # 3 / (2 × 2)
    assert round(res["net_pp"], 4) == round(res["excess_pp"] - res["cost_pp"], 4)


def nextday_side_pp() -> float:
    from stock_selector_qlib.nextday import SIDE_COST_PP
    return SIDE_COST_PP


def test_band_day_drops_names_with_a_missing_leg_instead_of_counting_zero():
    """缺腿的票从**它所在那条腿**的均值里掉出去，另一条腿不受影响，更不许当 0。

    SH600001 有日内腿、没隔夜腿 ⇒ 隔夜均值只剩 SH600000，但日内均值照样把它算进去。
    掉出去多少必须写在账上（`n_open` vs `n_open_priced`），不然这个数没人看得见。
    """
    d = "2026-10-28"
    rows = [_base(d, 0.2), _row(d, "SH600000", 1, e1=1.0, gap=0.8),
            _row(d, "SH600001", 2, e1=2.0, gap=None)]
    res = band_day(["SH600000", "SH600001"], rows, k=2, band=3)
    assert round(res["overnight_pct"], 4) == 0.8           # 只剩有隔夜价的那支
    assert res["n_open"] == 2 and res["n_open_priced"] == 1
    assert round(res["intraday_pct"], 4) == 1.5            # 缺隔夜不影响它吃日内
    assert round(res["gross_pct"], 4) == round(1.008 * 1.015 * 100 - 100, 4)
    assert res["n_hold"] == 2


def test_band_day_without_a_baseline_records_gross_and_stays_unsettled():
    """基准腿还没落账时：毛收益照记，超额留 None、settled=False，等下次补 ⇒ 绝不拿绝对值冒充超额。"""
    d = "2026-10-28"
    res = band_day([], [_row(d, "SH600000", 1, e1=1.0, gap=None)], k=1, band=3)
    assert round(res["gross_pct"], 4) == 1.0
    assert res["excess_pp"] is None and res["settled"] is False


def test_band_day_returns_none_when_not_a_single_leg_is_computable():
    d = "2026-10-28"
    assert band_day([], [_row(d, "SH600000", 1, e1=None, gap=None)], k=1, band=3) is None


def test_replay_band_carries_holdings_and_sells_only_on_a_band_exit():
    """三天连起来：D1 建仓 → D2 名次掉到 band 边上也不动 → D3 掉出 band 才卖，且从不买第 2 名之后。"""
    d1, d2, d3 = "2026-10-27", "2026-10-28", "2026-10-29"
    rows = [
        _base(d1, 0.0), _row(d1, "SH000001", 1, e1=1.0, gap=None),
        _row(d1, "SH000002", 2, e1=9.0, gap=None, buyable=False),
        _base(d2, 0.0), _row(d2, "SH000002", 1, e1=9.0, gap=1.0, buyable=False),
        _row(d2, "SH000001", 3, e1=0.2, gap=0.6),
        _base(d3, 0.0), _row(d3, "SH000003", 1, e1=2.0, gap=3.0),
    ]
    out = replay_band(rows, k=1, band=3)
    assert [r["signal_date"] for r in out] == [d1, d2, d3]
    assert {r["instrument"] for r in out} == {BAND_ROW}
    assert round(out[0]["gross_pct"], 4) == 1.0                        # 建仓日只吃日内
    assert round(out[1]["gross_pct"], 4) == round(1.006 * 1.002 * 100 - 100, 4)   # 隔夜 0.6 + 日内 0.2
    assert out[1]["n_exit"] == 0 and out[1]["cost_pp"] == 0.0
    assert out[2]["n_exit"] == 1 and out[2]["n_new"] == 1
    assert round(out[2]["cost_pp"], 6) == round(nextday_side_pp() * 2, 6)
    # D3 卖的那支今天没上榜 ⇒ 账上没它的行情，这条隔夜腿**掉出去且看得见**，绝不当 0
    assert out[2]["n_open"] == 1 and out[2]["n_open_priced"] == 0
    assert round(out[2]["gross_pct"], 4) == 2.0



def test_replay_band_values_names_that_fell_off_the_list_via_the_fetch_hook():
    """掉出前 60 的票**不在榜面行里**，账上没有它的行情 ⇒ 靠 `fetch` 从面板现取，不然卖出那天又是一条静默的洞。"""
    d1, d2 = "2026-10-27", "2026-10-28"
    rows = [_base(d1, 0.0), _row(d1, "SH000001", 1, e1=1.0), _base(d2, 0.0),
            _row(d2, "SH000002", 1, e1=2.0)]

    seen = []

    def fetch(day, inst):
        seen.append((day, inst))
        return {"gap_pct": -4.0, "e1_pct": None}          # 昨天收在手里的那支今早跳空低开 4%

    out = replay_band(rows, k=1, band=1, fetch=fetch)
    assert seen == [(d2, "SH000001")]
    assert round(out[1]["overnight_pct"], 4) == -4.0
    assert round(out[1]["gross_pct"], 4) == round(0.96 * 1.02 * 100 - 100, 4)   # −1.92%



def test_pool_baseline_also_carries_the_close_to_close_leg_for_the_band_book():
    bars = {"SH600000": (10.0, 10.2, 10.1, 10.0), "SZ000001": (20.0, 20.6, 20.2, 20.0)}
    base = pool_baseline(bars)
    assert round(base["e0_pct"], 4) == 2.5                 # mean(+2.0%, +3.0%) 按 T 收盘→T+1 收盘
    assert round(base["e1_pct"], 4) == 2.5


def test_pool_baseline_without_the_signal_close_has_no_e0_leg():
    assert pool_baseline({"SH600000": (10.0, 10.2, 10.1)})["e0_pct"] is None


def test_daily_excess_ignores_the_bookkeeping_rows_and_names_that_are_not_buyable():
    """榜面扩到前 60（band 判定要用）以后，E1 序列只能取**可买的前 30**：
    把 31–60 名算进去会把 E1 读数悄悄改掉，而那是个没人预注册过的组合。"""
    d = "2026-10-28"
    rows = [_row(d, "SH600000", 1, e1=2.0, hold=1.0), _row(d, "SZ000001", 2, e1=100.0, hold=9.0,
                                                            buyable=False),
            _base(d, 0.0, e1=0.0),
            {"signal_date": d, "instrument": BAND_ROW, "rank": 0, "e1_pct": 7.0, "settled": True}]
    by = daily_excess(rows)
    assert by.loc[d, "笔"] == 1
    assert round(by.loc[d, "e1"], 4) == 2.0


# ---------------------------------------------------------------------------
# 跨夜的腿：qlib bin 面板是**未复权裸价**，除权日会跳出一个假的暴跌
# ---------------------------------------------------------------------------


def test_hold_pct_is_signal_close_to_next_close():
    assert round(hold_pct(10.0, 10.4), 4) == 4.0


def test_hold_pct_returns_none_across_an_exright_jump():
    """主板一天最多 ±10%，收盘→收盘跳出 10.2% 以外只可能是除权 ⇒ 这根不算，不是记成一个巨大的跌幅。"""
    assert hold_pct(10.0, 5.0) is None                     # 送转/分红砍半
    assert hold_pct(10.0, 11.2) is None                    # +12% 也不是真涨跌
    assert hold_pct(10.0, 10.9) == pytest.approx(9.0)      # 涨停附近还在板幅内，要算


def test_e2_also_refuses_an_exright_jump():
    assert e2_pct(next_open=10.0, next_next_open=5.0) is None
    assert round(e2_pct(next_open=10.0, next_next_open=10.5), 4) == 5.0


def test_pool_baseline_drops_an_exright_member_from_the_close_to_close_leg():
    bars = {"SH600000": (10.0, 10.2, 10.1, 10.0),        # 正常 +2%
            "SZ000001": (10.0, 5.0, 5.1, 10.0)}          # 除权：收盘腰斩
    base = pool_baseline(bars)
    assert round(base["e0_pct"], 4) == 2.0               # 只有第一支进基准
    assert round(base["e2_pct"], 4) == 1.0               # 开盘→开盘 10.1/10.0 正常
