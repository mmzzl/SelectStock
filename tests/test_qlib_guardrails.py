"""名单护栏（薄流动 / 拉升后阴跌）的口径测试。

这两条要同时被两处用：回测的四臂对照（scripts/qlib_guardrail_ab.py）和线上打分
（stock_selector_qlib/serving.py）。所以这里守的是**定义本身**——宽表进、布尔宽表出，
不碰 qlib 面板、不碰模型产物，CI 能跑。

守的四件事：
1. 薄流动按「当日票池的相对分位」判，不是按绝对金额；
2. 妖痕只在「跌得还不深」时才关门（深跌桶是模型的盈利来源，不能一起赶走）；
3. 判不了的票（历史不够长）不误伤；
4. **只用当日及以前的数据**——把未来几天剪掉，之前每天的判定必须一字不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import guardrails  # noqa: E402


def dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2026-01-01", periods=n)


def amount_frame(values: dict[str, float], days: int = 30) -> pd.DataFrame:
    """每只票 20 日均额≈常数：values 给的就是它每天的成交额（元）。"""
    idx = dates(days)
    return pd.DataFrame({code: np.full(days, amt) for code, amt in values.items()}, index=idx)


# ---------------------------------------------------------------------------
# 护栏 1：薄流动
# ---------------------------------------------------------------------------
def test_thin_liquidity_blocks_the_bottom_quartile_of_that_days_pool():
    amounts = amount_frame({f"S{i}": (i + 1) * 1e8 for i in range(8)})  # 1 亿 ~ 8 亿
    block = guardrails.thin_liquidity(amounts)
    last = block.iloc[-1]
    # P25 在这 8 个值上是 2.75 亿 ⇒ 只有 1 亿、2 亿那两只被判薄
    assert sorted(last[last].index) == ["S0", "S1"]
    assert last.dtype == bool


def test_thin_liquidity_is_relative_not_absolute():
    """整池都很有钱时不该有人被关门：分位是「相对本票池」，不是绝对金额线。"""
    amounts = amount_frame({f"S{i}": (i + 1) * 100e8 for i in range(8)})
    block = guardrails.thin_liquidity(amounts)
    assert block.iloc[-1].sum() == 2  # 依旧剔掉最低的 1/4，但门槛随票池一起抬高


def test_thin_liquidity_needs_half_a_window_of_valid_days():
    """有效天不足一半 ⇒ 判不了流动性，不能把它算进「薄」里（那天东财断供会把整池关门）。"""
    amounts = amount_frame({"G": 5e8, "T": 1e8}, days=30)
    amounts.loc[:, "T"] = np.nan
    amounts.loc[dates(30)[-4:], "T"] = 1e8  # 只有 4 个有效天
    block = guardrails.thin_liquidity(amounts)
    last = block.iloc[-1]
    assert not last["T"]   # 判不了 ≠ 判薄
    assert not last["G"]   # 只剩一只可判，分位无从谈起


# ---------------------------------------------------------------------------
# 护栏 2：拉升后阴跌（妖痕）但还没跌深
# ---------------------------------------------------------------------------
def _yao_shape(end_price: float, days: int = 71) -> np.ndarray:
    """平静 10 → 20 日内涨到 15（+50%）→ 回落到 11.5 → 最后 20 日走平。

    距 71 日高点 −23%，最近 20 日几乎没跌：正是「暴涨过、现在阴跌但跌得不深」那类。
    """
    c = np.full(days, 10.0)
    c[30:46] = np.linspace(10.0, 15.0, 16)      # d45 = 15，比 d25 涨 50%
    c[46:51] = np.linspace(15.0, 11.5, 5)       # 回落到 11.5
    c[51:] = np.linspace(11.5, end_price, days - 51)
    return c


def test_spike_fade_blocks_only_when_the_decline_is_still_shallow():
    closes = pd.DataFrame({"YAO": _yao_shape(11.8), "DEEP": _yao_shape(8.0)}, index=dates(71))
    block = guardrails.spike_fade(closes)
    assert block.iloc[-1]["YAO"] == True  # noqa: E712  同样有妖痕，但 20 日只跌了一点
    assert block.iloc[-1]["DEEP"] == False  # noqa: E712  已经跌 30%，那是模型赚分的深跌桶


def test_spike_fade_needs_the_surge():
    """一路阴跌、从没暴涨过的票不是妖痕票。

    这条刻意只用「距高点 −21%、近 20 日 −7%」这两个和妖痕票**完全一样**的读数，
    唯一差别是没有那次 +40% 拉升——关门判据必须是拉升，不能是「跌了」。
    """
    quiet = np.linspace(15.0, 11.8, 71)
    closes = pd.DataFrame({"QUIET": quiet}, index=dates(71))
    last = closes.iloc[-1, 0]
    assert last / quiet.max() - 1 <= -0.20          # 回撤够深（和妖痕票同一档）
    assert last / quiet[-21] - 1 > -0.15            # 近 20 日跌得不深（同一档）
    assert guardrails.spike_fade(closes).iloc[-1]["QUIET"] == False  # noqa: E712


def test_spike_fade_ignores_a_spike_older_than_the_lookback():
    """暴涨发生在 71 个交易日以前 ⇒ 按口径不算妖痕（这条线是预注册里写死的窗口）。

    前 21 根从 10 涨到 15（任一根对它前 20 根都 +50%），之后 71 根只是从 14.9 阴跌到 11.8：
    距那段的高点已经 −20.8%，但窗口里找不到 +40% 的拉升。
    """
    span = guardrails.SPIKE_SPAN
    surge = np.concatenate([np.linspace(10.0, 15.0, 21), np.full(9, 15.0)])
    fade = np.linspace(14.9, 11.8, span)
    shape = np.concatenate([surge, fade])
    closes = pd.DataFrame({"OLD": shape}, index=dates(len(shape)))
    # 自查：这段确实是「高点 −20.8%、近 20 日 −7%」，只差没有拉升（证明卡的是窗口，不是别的）
    assert 11.8 / 14.9 - 1 <= -0.20
    assert abs(closes.iloc[-1, 0] - 11.8) < 1e-9
    assert guardrails.spike_fade(closes).iloc[-1]["OLD"] == False  # noqa: E712


def test_spike_fade_with_short_history_does_not_block():
    closes = pd.DataFrame({"NEW": _yao_shape(11.8)[:40]}, index=dates(40))
    assert guardrails.spike_fade(closes).iloc[-1]["NEW"] == False  # noqa: E712


# ---------------------------------------------------------------------------
# 合并与安全边界
# ---------------------------------------------------------------------------
def test_blocked_is_the_union_of_the_two_rules():
    amounts = amount_frame({"THIN": 1e8, "FINE": 8e8}, days=71)
    closes = pd.DataFrame({"THIN": np.full(71, 12.0), "FINE": _yao_shape(11.8)},
                          index=dates(71))
    block = guardrails.blocked(closes, amounts)
    assert block.iloc[-1]["THIN"] == True  # noqa: E712  只中护栏 1
    assert block.iloc[-1]["FINE"] == True  # noqa: E712  只中护栏 2


def test_blocked_keeps_the_same_shape_as_input():
    amounts = amount_frame({"A": 5e8, "B": 1e8}, days=71)
    closes = pd.DataFrame({"A": np.full(71, 10.0), "B": np.full(71, 10.0)}, index=dates(71))
    block = guardrails.blocked(closes, amounts)
    assert list(block.index) == list(closes.index)
    assert list(block.columns) == ["A", "B"]
    assert block.dtypes.eq(bool).all()


def test_flags_do_not_change_when_future_days_are_cut():
    """前视自查：最后一天之后的行情被剪掉，之前每一天的判定必须逐格相同。"""
    idx = dates(112)
    shape = np.concatenate([_yao_shape(11.8), _yao_shape(11.8)[30:]])
    closes = pd.DataFrame({f"C{j}": shape * (1.0 + 0.05 * j) for j in range(5)}, index=idx)
    amounts = pd.DataFrame({f"C{j}": np.linspace(0.5e8, 9e8, len(idx)) * (j + 1)
                            for j in range(5)}, index=idx)
    full = guardrails.blocked(closes, amounts)
    assert full.iloc[:80].to_numpy().sum() > 0  # 重叠段里真有票被判过，否则这个自求是空的
    cut = guardrails.blocked(closes.iloc[:80], amounts.iloc[:80])
    pd.testing.assert_frame_equal(full.iloc[:80], cut)


# ---------------------------------------------------------------------------
# 接到 serving 的名单上：剔谁、怎么数、判不了怎么办、怎么关掉
# ---------------------------------------------------------------------------
from stock_selector_qlib import serving  # noqa: E402


def scores(**values) -> pd.Series:
    """当日全池分数（index=代码），按分数降序摆好，跟 day_ranking 递给护栏的那张表同形。"""
    return pd.Series(values, index=list(values)).sort_values(ascending=False)


def test_apply_guardrails_drops_blocked_names_and_keeps_the_rest():
    tail = scores(A=0.9, B=0.8, C=0.7)
    thin = pd.Series({"A": False, "B": True, "C": False})
    yao = pd.Series({"A": False, "B": False, "C": False})
    kept, stats = serving.apply_guardrails(tail, thin, yao)
    assert list(kept.index) == ["A", "C"]
    assert stats["guard_dropped"] == 1


def test_apply_guardrails_counts_each_rule_and_says_the_reason():
    tail = scores(A=0.9, B=0.8, C=0.7, D=0.6)
    thin = pd.Series({"A": False, "B": True, "C": False, "D": True})
    yao = pd.Series({"A": False, "B": False, "C": True, "D": True})
    kept, stats = serving.apply_guardrails(tail, thin, yao)
    assert list(kept.index) == ["A"]
    assert stats["guard_thin"] == 2          # 薄流动两条（其中 D 同时中两条，不重复计成两个人）
    assert stats["guard_yao"] == 2
    assert stats["guard_dropped"] == 3
    assert stats["guard_notes"] == {"B": {"rank_raw": 2, "reason": "薄流动"},
                                    "C": {"rank_raw": 3, "reason": "拉升后阴跌"},
                                    "D": {"rank_raw": 4, "reason": "薄流动、拉升后阴跌"}}


def test_apply_guardrails_keeps_everything_when_it_cannot_judge():
    """行情缺失/判不了（NaN、票不在护栏表里）一律不剔：断供那天不能把名单清空。"""
    tail = scores(A=0.9, B=0.8)
    thin = pd.Series({"A": np.nan, "B": False})
    yao = pd.Series(dtype="float64")  # 整张表空的：一天行情都没回来
    kept, stats = serving.apply_guardrails(tail, thin, yao)
    assert list(kept.index) == ["A", "B"]
    assert stats["guard_dropped"] == 0


def test_apply_guardrails_is_a_no_op_when_the_switch_is_off(monkeypatch):
    monkeypatch.setenv("QLIB_GUARDRAILS", "off")
    tail = scores(A=0.9, B=0.8)
    thin = pd.Series({"A": True, "B": True})
    kept, stats = serving.apply_guardrails(tail, thin, pd.Series(dtype="bool"))
    assert list(kept.index) == ["A", "B"]
    assert stats["guard_off"] is True


def test_guardrails_enabled_defaults_to_on(monkeypatch):
    monkeypatch.delenv("QLIB_GUARDRAILS", raising=False)
    assert serving.guardrails_enabled()
    monkeypatch.setenv("QLIB_GUARDRAILS", "off")
    assert not serving.guardrails_enabled()


def test_ranks_by_code_says_why_a_guarded_name_has_no_rank(monkeypatch):
    """被护栏挡下的票在持仓页不能只说「没上榜」：它今天被打过分、只是没进名单，得说清是谁拦的。"""
    rows = [{"code": "600000", "qlib_symbol": "SH600000", "rank": 1, "universe": 1,
             "score_raw": 0.5, "score": 1.0, "date": "2026-10-23"}]
    monkeypatch.setattr(serving, "day_ranking", lambda wait=True: list(rows))
    monkeypatch.setattr(serving, "guard_notes",
                        lambda: {"300313": {"rank_raw": 1, "reason": "薄流动"}})
    monkeypatch.setattr(serving, "score_pool", lambda art: ["SH600000", "SZ300313"])
    out = serving.ranks_by_code(["600000", "300313", "000001"])
    by = {i["code"]: i for i in out["items"]}
    assert by["600000"]["rank"] == 1
    assert by["300313"]["rank"] is None
    assert "薄流动" in by["300313"]["note"] and "第 1 名" in by["300313"]["note"]
    assert "候选池" in by["000001"]["note"]
