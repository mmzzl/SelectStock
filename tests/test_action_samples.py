"""持仓动作决策样本（批次 C 的上游）的口径测试。

这批样本的价值全在「标签到底是不是那个动作的差额」上，所以这里不测代码能不能跑，
只钉四件容易写错的事：
1. **标签对齐**：`r_H` 必须是「决策日收盘之后再拿 H 天」的那段收益，取错一根 bar 就变成
   「预测过去」，读数会好看得离谱；
2. **T+1**：当天买的没有 h=0，最早只能持仓 1 天；
3. **可执行性**：入场日贴着涨停线不算成立、决策日一字板/停牌没有「卖掉」这个选项（这行不产出）、
   到期日卖不掉最多顺移 `REPAIR_MAX_DAYS` 行，顺移不到判 NaN 而不是 0；
4. **断档与留出段**：episode 里相邻行隔太多天要整段丢，`--to` 之后的一根 bar 都不许碰。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bs():
    sys.path.insert(0, str(ROOT))
    return _load("build_action_samples", "scripts/build_action_samples.py")


def mk(n=60, start="2015-06-01", freq="D", code="000001", **over):
    """造一只票的日线：close=100+i，涨幅 1%，高低价都不重合（既买得进也卖得掉）。"""
    date = pd.date_range(start, periods=n, freq=freq)
    i = np.arange(n, dtype=float)
    d = {
        "date": date.to_numpy(),
        "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i,
        "pct": np.full(n, 1.0), "last_close": 99 + i,
        "amount": np.full(n, 5e7), "ma20": 100 + i, "pos60": i.copy(),
        "vol_ratio": np.full(n, 1.0), "vol_z": np.zeros(n), "rsi14": np.full(n, 50.0),
        "adx14": np.full(n, 20.0), "atr14": np.full(n, 2.0),
        "lu": np.zeros(n, dtype=bool),
    }
    d.update(over)
    return pd.DataFrame(d)


def build(bs, d, code="000001", **kw):
    opts = dict(horizons=(5, 10), stride=1, min_tenure=1, min_amount=0.0,
                max_gap_days=15, date_from="2015-01-01", cut=len(d))
    opts.update(kw)
    return bs.build_for_stock(d, code, **opts)


def pos_of(bs, d, frame):
    """决策日在原始序列里的行号，供测试按位置重算标签。"""
    pos = {pd.Timestamp(x): k for k, x in enumerate(d["date"])}
    return np.array([pos[pd.Timestamp(x)] for x in frame["date"]])


def test_label_is_forward_return_from_the_decision_bar(bs):
    """r_H = cl[j+H]/cl[j] − 1，一根都不能错。"""
    d = mk()
    frame, cnt = build(bs, d)
    j = pos_of(bs, d, frame)
    cl = d["close"].to_numpy(dtype=float)
    for h in (5, 10):
        want = cl[j + h] / cl[j] - 1.0
        got = frame[f"r{h}"].to_numpy(dtype=float)
        assert np.allclose(got, want, rtol=0, atol=1e-6), f"r{h} 取错了 bar"
    assert cnt["rows"] == len(frame) > 0


def test_no_h0_row_and_hold_days_starts_at_one(bs):
    """T+1：当天买的当天不能卖，决策行最早从持仓第 1 天开始。"""
    frame, _ = build(bs, mk())
    assert frame["hold_days"].min() == 1
    assert 0 not in set(frame["hold_days"].tolist())
    assert sorted(set(frame["hold_days"].tolist())) == list(range(1, 11))


def test_decision_day_row_is_next_bar_after_entry(bs):
    """h=1 那行的日期必须紧跟入场日：入场/决策错位会让 pnl 特征与标签不是同一段行情。"""
    d = mk()
    frame, _ = build(bs, d)
    dates = [pd.Timestamp(x) for x in d["date"]]
    pos = {x: k for k, x in enumerate(dates)}
    first = frame[frame["hold_days"] == 1]
    for _, r in first.head(20).iterrows():
        assert pos[pd.Timestamp(r["date"])] == pos[pd.Timestamp(r["entry_date"])] + 1


def test_features_come_from_the_decision_bar_only(bs):
    """特征取决策日那一行（≤t），不能取入场日：pos60 每行都不同，取错一眼就看出。"""
    d = mk()
    frame, _ = build(bs, d)
    j = pos_of(bs, d, frame)
    assert np.allclose(frame["pos60"].to_numpy(dtype=float), j, atol=1e-3)
    # 浮动盈亏是「相对入场价」，不是相对决策价
    cl = d["close"].to_numpy(dtype=float)
    pos = {pd.Timestamp(x): i for i, x in enumerate(d["date"])}
    for k in range(min(30, len(frame))):
        row = frame.iloc[k]
        jj = int(j[k])
        pe = pos[pd.Timestamp(row["entry_date"])]
        assert float(row["pnl_pp"]) == pytest.approx((cl[jj] / cl[pe] - 1.0) * 100.0, abs=1e-3)


def test_unsellable_decision_day_row_is_dropped(bs):
    """决策日一字板 ⇒ 当天根本没有「卖掉」这个选项 ⇒ 这行不产出（不是产出一条假的）。"""
    d = mk()
    hi = d["high"].to_numpy(dtype=float).copy()
    lo = d["low"].to_numpy(dtype=float).copy()
    hi[12] = lo[12] = 100.0 + 12            # 一个 one-way bar
    d["high"], d["low"] = hi, lo
    frame, cnt = build(bs, d)
    j = pos_of(bs, d, frame)
    assert 12 not in set(j.tolist())
    assert cnt["row_no_sell"] >= 1


def test_limit_up_entry_day_is_not_an_episode(bs):
    """入场日贴着涨停线 ⇒ 买不进 ⇒ 这个 episode 不成立。"""
    d = mk()
    pct = d["pct"].to_numpy(dtype=float).copy()
    pct[10] = 10.0                          # 主板涨停线 9.8，10% 就是封住了
    d["pct"] = pct
    frame, _ = build(bs, d)
    dates = {pd.Timestamp(x) for x in frame["entry_date"]}
    assert pd.Timestamp(d["date"][10]) not in dates


def test_label_repairs_to_next_sellable_day(bs):
    """到期日一字板卖不掉 ⇒ 顺移到最后能成交的那天，最多 REPAIR_MAX_DAYS 行。"""
    d = mk()
    hi = d["high"].to_numpy(dtype=float).copy()
    lo = d["low"].to_numpy(dtype=float).copy()
    cl = d["close"].to_numpy(dtype=float).copy()
    k = 15                                   # 让 h=5 的到期日（j+5=15）成交不了
    hi[k] = lo[k] = cl[k]
    d["high"], d["low"] = hi, lo
    frame, _ = build(bs, d)
    sel = frame[frame["hold_days"] == 5]
    j = np.array([pd.Timestamp(x) for x in d["date"]])
    pos = {x: i for i, x in enumerate(j)}
    for _, row in sel.iterrows():
        jj = pos[pd.Timestamp(row["date"])]
        if jj + 5 == k:                      # 正好撞在那根一字板上
            assert float(row["r5"]) == pytest.approx(cl[k + 1] / cl[jj] - 1.0, abs=1e-6)


def test_gap_inside_episode_drops_the_whole_episode(bs):
    """断档（#144）：episode 的窗口只要跨过一个 `max_gap_days` 的缺口就整段丢，
    绝不把「两个月后」当成「五天后」。缺口之后的持仓照样成立。"""
    d = mk()
    date = pd.to_datetime(d["date"]).to_numpy().copy()
    date[16:] = date[15] + np.timedelta64(60, "D")     # 第 16 行起跳到两个月后
    d["date"] = date
    frame, cnt = build(bs, d, max_gap_days=15)
    assert len(frame) > 0 and cnt["drop_gap"] >= 1
    pos = {pd.Timestamp(x): i for i, x in enumerate(date)}
    for _, row in frame.iterrows():
        cross = (pos[pd.Timestamp(row["entry_date"])] >= 16) != (pos[pd.Timestamp(row["date"])] >= 16)
        assert not cross, "有决策行的窗口跨过了断档"


def test_tail_and_cut_keep_the_holdout_untouched(bs):
    """尾部不足 need 行的入场日丢弃；cut 之后的一根 bar 都不读（2022+ 留出段保密）。"""
    d = mk(n=80)
    cl = d["close"].to_numpy(dtype=float).copy()
    cl[40:] = 1e6                            # 留出段投毒：谁读到它，标签就会跳出 1e6 的量级
    d["close"] = cl
    frame, cnt = build(bs, d, cut=40)
    assert cnt["drop_tail"] >= 1
    assert len(frame) > 0
    assert pd.Timestamp(frame["date"].max()) < pd.Timestamp(d["date"][40])
    pos = {pd.Timestamp(x): i for i, x in enumerate(d["date"])}
    for _, row in frame.sample(20, random_state=7).iterrows():
        for h in (5, 10):
            assert abs(float(row[f"r{h}"])) < 1.0, "标签用到了 cut 之后的价格"


def test_min_tenure_matches_the_pool(bs):
    """上市不满 min_tenure 的行不进池：票池也是一个自由度，必须与 v1/B 同一道门槛。"""
    d = mk(n=60)
    frame, cnt = build(bs, d, min_tenure=55)
    assert cnt["episodes"] == 0
    assert frame.empty


def test_output_columns_match_the_schema(bs):
    """列契约：少一列或多一列都会让训练脚本在读表时炸，这里先钉住。"""
    frame, _ = build(bs, mk())
    assert list(frame.columns) == bs.SCHEMA_NAMES
    assert frame["r5"].notna().all() and frame["r10"].notna().all()
