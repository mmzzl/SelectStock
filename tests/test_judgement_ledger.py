"""判定记账台账的口径测试（分箱边界、后验区间、可信门槛、时代同号、卖优先）。"""

import importlib.util
import sys

import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/build_judgement_ledger.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_judgement_ledger", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_judgement_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()

DEFAULT_RULES = {"sell": "price <= ma10 * 0.95",
                 "add": "close >= ma5 * 1.03 and rsi14 <= 65"}


def _ledger(n=400, *, up_ratio=0.5, action=3, heat_bin_id=4, start="2020-01-01"):
    """一个 (action × 热度箱) 格子的最小台账：前 up_ratio 比例的学业为正。"""
    dates = pd.date_range(start, periods=n, freq="B").strftime("%Y-%m-%d")
    k = round(n * up_ratio)
    return pd.DataFrame({
        "date": list(dates),
        "code": [f"{i % 2:06d}" for i in range(n)],
        "action": np.full(n, action, dtype=np.int8),
        "heat": np.full(n, 95.0),
        "heat_bin": np.full(n, heat_bin_id, dtype=np.int8),
        "fwd_ret_10": np.r_[np.full(k, 0.01), np.full(n - k, -0.01)],
        "fwd_ret_20": np.r_[np.full(k, 0.02), np.full(n - k, -0.02)],
        "flip_sell_20": np.r_[np.ones(n // 2), np.zeros(n - n // 2)].astype(np.int8),
    })


@pytest.mark.parametrize("heat,expected", [
    (np.nan, 0), (0.0, 1), (44.0, 1), (44.5, 2), (70.0, 2),
    (70.5, 3), (89.9, 3), (90.0, 4), (100.0, 4),
])
def test_heat_bin_boundaries(heat, expected):
    """箱边界只能有一套说法：44/70 归上界，90 起算热，NaN 单独一箱不混进冷。"""
    assert mod.heat_bin(np.array([heat]))[0] == expected


def test_beta_interval_shrinks_with_n():
    lo_mean, lo20, hi20 = mod.beta_interval(50, 100)
    hi_mean, lo2k, hi2k = mod.beta_interval(1000, 2000)
    assert lo_mean == pytest.approx(hi_mean)
    assert (hi20 - lo20) > (hi2k - lo2k)
    assert lo20 < lo_mean < hi20


def test_summarize_drops_small_cells_and_marks_credible():
    """样本量不足的格子直接不输出；区间宽于阈值的输出「不可信」而不是猜。"""
    small = mod.summarize(_ledger(n=mod.MIN_POSTERIOR_N - 1))
    assert small.empty
    wide = mod.summarize(_ledger(n=400))
    assert not bool(wide.iloc[0]["credible"])
    tight = mod.summarize(_ledger(n=40_000))
    assert bool(tight.iloc[0]["credible"])


def test_summarize_adds_pooled_row_per_action():
    """诊断算不出热度时用它兜底，所以每个状态都得有一行「全部」。"""
    out = mod.summarize(_ledger(action=3))
    assert list(out["heat_bin"]) == ["90-100", "全部"]
    pooled = out[out["heat_bin"] == "全部"].iloc[0]
    assert int(pooled["n"]) == 400
    assert pooled["up_rate_posterior"] == out.iloc[0]["up_rate_posterior"]


def test_hold_row_carries_flip_rate_and_others_do_not():
    hold = mod.summarize(_ledger(action=3))
    assert hold.iloc[0]["flip_sell_20_pct"] == pytest.approx(50.0)
    sell = mod.summarize(_ledger(action=1))
    assert pd.isna(sell.iloc[0]["flip_sell_20_pct"])


def test_summarize_day_weighted_can_reverse_the_point_mean():
    """点级均值和日等权必须分开落盘：判据扎堆的日子会让两者符号相反。

    这里造一条卖出规则——暴跌日一次判出 300 个点（每点 +0.5%），其余 104 天每天只有 1 个点
    （每点 −0.2%）。点级均值 +0.32% 看着像买入理由，日等权 −0.19% 才是「这条规则典型地值多少」。
    """
    days = pd.date_range("2019-01-01", periods=105, freq="B").strftime("%Y-%m-%d")
    sell = [(days[0], 1, 0.005)] * 300 + [(d, 1, -0.002) for d in days[1:]]
    hold = [(days[0], 3, 0.0)] * 20 + [(d, 3, 0.004) for d in days[1:]] * 20
    rows = sell + hold
    frame = pd.DataFrame({
        "date": [r[0] for r in rows],
        "code": [f"{i % 40:06d}" for i in range(len(rows))],
        "action": np.array([r[1] for r in rows], dtype=np.int8),
        "heat": np.full(len(rows), 95.0),
        "heat_bin": np.full(len(rows), 4, dtype=np.int8),
        "fwd_ret_10": np.array([r[2] for r in rows], dtype=np.float64),
        "fwd_ret_20": np.array([r[2] for r in rows], dtype=np.float64),
        "flip_sell_20": np.zeros(len(rows), dtype=np.int8),
    })
    out = mod.summarize(frame)
    cell = out[(out["action"] == "sell") & (out["heat_bin"] == "全部")].iloc[0]
    assert cell["fwd10_mean_pct"] > 0 and cell["daymean_fwd10_pct"] < 0
    assert cell["vs_hold_daymean_pp"] < 0 and cell["vs_hold_t"] < -2
    assert cell["vs_hold_days"] == 105 and bool(cell["vs_hold_era_stable"])
    # hold 自己那格不跟自己比：只给日等权均值，差分段留空
    hold_cell = out[(out["action"] == "hold") & (out["heat_bin"] == "全部")].iloc[0]
    assert np.isfinite(hold_cell["daymean_fwd10_pct"])
    assert pd.isna(hold_cell["vs_hold_daymean_pp"])


def test_summarize_drops_diff_when_days_too_few():
    """可比交易日不足 MIN_DIFF_DAYS 就不给差分和 t，宁缺不编（样本量本身是够的）。"""
    days = pd.date_range("2024-01-01", periods=mod.MIN_DIFF_DAYS - 8, freq="B")
    rows = [(d, act) for d in days for act in (1, 3) for _ in range(20)]
    frame = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d, _ in rows],
        "code": [f"{i % 20:06d}" for i in range(len(rows))],
        "action": np.array([a for _, a in rows], dtype=np.int8),
        "heat": np.full(len(rows), 95.0),
        "heat_bin": np.full(len(rows), 4, dtype=np.int8),
        "fwd_ret_10": np.linspace(-0.02, 0.02, len(rows)),
        "fwd_ret_20": np.linspace(-0.02, 0.02, len(rows)),
        "flip_sell_20": np.zeros(len(rows), dtype=np.int8),
    })
    cell = mod.summarize(frame)
    cell = cell[(cell["action"] == "sell") & (cell["heat_bin"] == "全部")].iloc[0]
    assert cell["n"] == 440 and np.isfinite(cell["daymean_fwd10_pct"])
    # 全表都没有差分时这列压根不存在（下游按「未标定」退化），有列则必须是 NaN
    diff = cell.get("vs_hold_daymean_pp")
    assert diff is None or pd.isna(diff)

    hold = mod.summarize(_ledger(action=3))
    assert hold.iloc[0]["flip_sell_20_pct"] == pytest.approx(50.0)
    sell = mod.summarize(_ledger(action=1))
    assert pd.isna(sell.iloc[0]["flip_sell_20_pct"])


def test_era_stability_uses_return_sign_not_frequency():
    """三段收益同号才算稳；前两段赚的小尾巴亏的大亏能翻掉结论。"""
    lf = mod._era_ids(np.arange(900))
    assert sorted(set(lf)) == [0, 1, 2]
    assert len(lf[lf == 0]) == 300

    mixed = pd.DataFrame({"era": np.repeat([0, 1, 2], 300),
                          "fwd_ret_10": np.r_[np.full(300, 0.05), np.full(300, 0.05),
                                              np.full(300, -0.20)]})
    assert not mod._stable(mod._era_means(mixed))

    all_pos = mixed.assign(fwd_ret_10=0.01)
    assert mod._stable(mod._era_means(all_pos))


def test_era_ids_cuts_are_global_not_per_cell():
    """同一条日期轴上不同格子的 era 编号必须对齐同一年代，否则跨箱的 gap 没法比。"""
    ord_ns = pd.to_datetime(pd.date_range("2010-01-01", periods=900, freq="B")
                            ).astype("int64").to_numpy()
    ids = mod._era_ids(ord_ns)
    assert (ids[:300] == 0).all() and (ids[300:600] == 1).all() and (ids[600:] == 2).all()


def test_era_means_drops_undersized_era():
    """单段样本太短记 nan，且两段以下不能算「同号」。"""
    short = mod.MIN_ERA_N - 1
    grp = pd.DataFrame({"era": np.r_[np.zeros(300), np.ones(short)].astype(int),
                        "fwd_ret_10": np.r_[np.full(300, 0.01), np.full(short, -0.01)]})
    means = mod._era_means(grp)
    assert np.isfinite(means[0]) and np.isnan(means[1])
    assert not mod._stable(means)


def test_judge_matrix_sell_wins_over_add_and_nan_is_unjudged():
    close = np.array([[10.0], [12.0], [50.0], [np.nan]])
    arr = {
        "close": close,
        "ma5": np.array([[9.0], [12.0], [12.0], [12.0]]),
        "ma10": np.array([[12.0], [12.0], [12.0], [12.0]]),
        "rsi14": np.array([[50.0], [50.0], [50.0], [50.0]]),
    }
    action, sell = mod.judge_matrix(arr, DEFAULT_RULES)
    assert action[0, 0] == 1 and sell[0, 0]        # 同时满足 add，仍然按 sell（与 judge_holding 一致）
    assert action[1, 0] == 3                        # 两条线都不碰 → hold
    assert action[2, 0] == 2                        # 高于卖点线、够到加仓线 → add
    assert action[3, 0] == 0                        # 缺 ma10 不判，不能编一个状态出来


def test_judge_matrix_add_without_sell():
    arr = {
        "close": np.array([[11.5]]),
        "ma5": np.array([[10.0]]),      # 11.5 >= 10.3 → add
        "ma10": np.array([[12.0]]),     # 11.5 > 0.95*12=11.4 → 不 sell
        "rsi14": np.array([[50.0]]),
    }
    action, _ = mod.judge_matrix(arr, DEFAULT_RULES)
    assert action[0, 0] == 2


def test_judge_matrix_vectorizes_any_rule_from_the_grammar():
    """规则集可以是任意多条规则的 OR/AND 复合串——判定必须和插件逐行求值同口径，不再挑形状。"""
    arr = {"close": np.array([[9.4], [11.0], [11.0]]),
           "ma10": np.array([[10.0]] * 3), "ma5": np.array([[10.0]] * 3),
           "rsi14": np.array([[80.0], [80.0], [95.0]])}
    rules = dict(DEFAULT_RULES, sell="close <= ma10 * 0.95 or rsi14 > 90")
    action, sell = mod.judge_matrix(arr, rules)
    # 第 1 根靠跌破线触发、第 3 根靠 RSI 触发（OR 的两条腿各自都要能响）
    assert sell.tolist() == [[True], [False], [True]]
    assert action.tolist() == [[1], [3], [1]]    # sell 优先；都不触发才是 hold


def test_rule_columns_stops_on_unparsable_rule():
    """表达式不在文法/白名单里时台账要炸在取列这一步，不能按另一种口径算完再落盘。"""
    with pytest.raises(SystemExit):
        mod.rule_columns({"sell": "close < mystery_col", "add": ""})
    with pytest.raises(SystemExit):
        mod.rule_columns({"sell": "(close < ma10)", "add": ""})
    assert set(mod.rule_columns(DEFAULT_RULES)) >= {"date", "close", "ma10", "rsi14", "ma5"}


def test_build_rows_maps_matrix_cells_back_to_date_and_code():
    """flat 展开是按行（日期）优先，回填错一个下标整张台账就废了。"""
    dates, codes = ["2026-01-01", "2026-01-02"], ["000001", "000002", "000003"]
    action = np.array([[3, 0, 2], [1, 3, 0]], dtype=np.int8)
    heat = np.array([[95.0, np.nan, 10.0], [50.0, 91.0, np.nan]])
    fwd = np.array([[0.01, 0.01, 0.01], [0.01, np.nan, 0.01]])
    out = mod.build_rows(dates, codes, action, heat, fwd, fwd * 2,
                         np.zeros((2, 3), dtype=bool))
    assert list(out["date"]) == ["2026-01-01", "2026-01-01", "2026-01-02"]
    assert list(out["code"]) == ["000001", "000003", "000001"]
    assert list(out["action"]) == [3, 2, 1]
    assert list(out["heat_bin"]) == [4, 1, 2]
    # 无学业（fwd NaN）的行不落盘，即使判定成立
    assert "2026-01-02/000002" not in list(zip(out["date"], out["code"]))
    assert out["date"].dtype.name == "category"


def test_load_kline_aligns_rows_by_date_not_position(tmp_path):
    """两遍扫靠 searchsorted 定位日期：文件里日期乱序、缺日、历史太短都不能把 close 填错行。"""
    def write(code, days, value):
        rows = [{"date": d, "close": value, "ma5": value, "ma10": value, "rsi14": 50.0}
                for d in days]
        pd.DataFrame(rows).to_parquet(tmp_path / f"{code}.parquet", index=False)

    axis = pd.date_range("2026-01-01", periods=70, freq="B").strftime("%Y-%m-%d").tolist()
    write("000002", list(reversed(axis)), 5.0)            # 日期乱序
    write("000001", axis[:-1], 1.0)                        # 最后一天缺（停牌）
    write("000003", axis[:59], 9.0)                        # 不足 60 行 → 丢弃
    dates, codes, arr = mod.load_kline(sorted(tmp_path.glob("*.parquet")),
                                             mod.rule_columns(DEFAULT_RULES))
    assert dates == axis                                  # 日期轴按字符串序=时间序
    assert codes == ["000001", "000002"]
    i1, i2 = codes.index("000001"), codes.index("000002")
    assert arr["close"][0, i1] == 1.0 and np.isnan(arr["close"][-1, i1])
    assert arr["close"][0, i2] == 5.0 and arr["close"][-1, i2] == 5.0


def test_labels_windows_are_forward_only():
    close = np.lib.stride_tricks.as_strided(
        np.arange(1.0, 61.0, dtype=np.float32), shape=(60, 1), strides=(4, 0))
    sell = np.zeros((60, 1), dtype=bool)
    sell[5, 0] = True
    fwd10, fwd20, flip = mod.labels(np.full((60, 1), 3, dtype=np.int8), sell, close)
    assert fwd10[0, 0] == pytest.approx(close[10] / close[0] - 1)
    assert np.isnan(fwd10[-1, 0]) and np.isnan(fwd20[-1, 0])
    assert flip[0, 0] and not flip[5, 0]      # 当天已是 sell 不算「未来会变 sell」
    assert not flip[6, 0]                      # 窗口内只有当天那一次，之后没有


def test_rolling_max_prev_excludes_today():
    close = np.array([[1.0], [5.0], [2.0], [3.0], [9.0]], dtype=np.float32)
    hi = mod._rolling_max_prev(close, 2)
    assert np.isnan(hi[0, 0]) and np.isnan(hi[1, 0])   # 不足窗口 + 不含当天
    assert hi[2, 0] == 5.0 and hi[3, 0] == 5.0
    assert hi[4, 0] == 3.0                             # 当天的 9 不算进自己的高点


def test_fired_within_prev_looks_back_not_at_today():
    fired = np.zeros((6, 1), dtype=bool)
    fired[3, 0] = True
    prev = mod._fired_within_prev(fired, 2)
    assert not prev[3, 0]                  # 当天自己那次不算「提前预警」
    assert prev[4, 0] and prev[5, 0]
    assert not prev[2, 0]                  # 窗口只看前 2 行
