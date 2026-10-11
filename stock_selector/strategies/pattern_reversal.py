"""低位反转策略插件。

触发条件只剩一条：`v_reversal` —— 近 FLAG_LEG_BARS 根跌过 ≥10% 且已从 6 日低点回升 ≥8%，
再叠加「形态看位置」（pos60 ≤ 0.35）、否决当日爆量，并排除涨停（买不进）。
判据写成 as-of 形式（signal_at），实盘选股与回测逐日复用同一段代码。

两种基线务必分清，结论会差一个数量级：
- **时代均值**（同年全样本 10 日收益均值）允许市场 β，数字大但会骗人；
- **同日基线**（每条信号减去它当天全样本均值，再对日序列做配对 t）才是股票级超额。
1200 只票、2016-2026、10 日持有：本策略定版组合在同日基线下是
+0.51pp(16-21, t=1.6) / +0.40pp(22-26, t=1.3)——两时代同号、但都不到显著门槛。
而**裸 `v_reversal` 是 -0.27 / -0.60pp（t=-0.9/-2.1）**：反弹状态本身没有 alpha，
是「低位 + 不爆量」这两道过滤把它翻正的。之前那句「效应几乎全在 v_reversal 上
（+2.90/+3.92pp，t=14/22）」是时代均值口径，大部分是市场 β（信号扎堆在恐慌日之后）。

**抽样轴（比任何单条判据都重要，2026-10-11）：** 上面那 +0.51/+0.40pp 只在它自己的样本里成立
（`codes()[::6][:1200]`，实际约 852 只；ml15 已逐行核对过 f10 与判据口径一致）。换成同一份数据
的其它等距抽样就翻符号：非基金列表 `[::4][:1200]` 1200 只 → **-0.61/-0.79pp（t=-2.7/-2.0）**；
风险分面板 `[::3]` 1710 只 → **+0.02/-1.04**。ETF 只占命中的 2%，不是它们撑起来的。
加权方式同样能翻符号（日均加权 vs 全池行等权 → 负的 vs +0.9~1.2pp），本策略一律按日均加权报。
所以股票级超额的诚实说法是「≈0，在 ±1pp 内随抽样摆动」，它的真实价值是「低位 + 不爆量」
这套排除法，不是一条正 alpha。验证必须同时报三条轴：时代、四子时期、**股票抽样**。

为什么不再是"看涨 K 线形态"策略：六个单根/组合看涨形态（锤子/启明星/吞没/刺透/低位十字星/
圆弧底）在低位放量门槛内，相对"同样门槛但无形态"的对照是 -0.08pp（16-21）和 -0.10pp（22-26）
——两个时代都是零。`v_reversal` 不含 K 线内容，它是纯反弹确认状态，所以本策略的真实口径是
「跌到低位 + 已经弹起来 + 不追爆量」，而不是形态识别。

逐条判据在同日基线下的身份（2026-10-11 重审，全部用 16-21/22-26 + 四子时期双签）：
- pos60 ≤ 0.35：被排除的 0.35 以上为 -1.56/-0.92pp（t=-4.3/-2.0，四子时期全负），
  断点实测就在 0.35：[0.15,0.35) 是 +0.45/+0.48，[0.35,0.55) 立刻翻成 -0.80/-0.88。放宽到
  0.45/0.55 会让保留组掉到 +0.24/+0.08 和 +0.08/-0.04，所以天花板位置就停在 0.35。
- 否决爆量：被否决组 -2.06/-1.04pp，两时代同号，是除位置外最强的一道。
- 排除涨停：收益上涨停那组反而更高（+1.78/+2.41），**留这条纯粹因为买不进**——
  f10 从当日收盘价起算，实盘拿不到那个价。别把它当收益判据来"优化"掉。

被外部样本否掉、已删掉的判据（都留在此以防再次加回来）：
- 站上 MA5：+1.31pp(16-21) / -0.68pp(22-26)，时代反号；
- 量能门槛（放量或量趋势向上）：单独看 +0.72/+0.24，但叠在 v_reversal 上两个时代都不赚
  （3.75/5.49 → 3.17/5.81），且砍掉一半覆盖；只用它不带 v_reversal 时超额仅 +0.30/+0.37；
- MA20 加速下行否决：八年外部样本 -0.12，16-18 段 -0.68，当初是基于有偏小样本加的；
- 突破形态否决：v_reversal 与箱体/颈线突破几乎不共存（同窗只有 11/8 条），是无用分支；
- pos60 下限（0.10 或 0.15）：最底部 [0,0.15) 是 +0.09/-0.01（两时代皆≈0，不是负），
  砍掉它只换来 +0.45/+0.48，却要付掉 52% 覆盖，16-21 反而更低。edge 不在"跌得最透"；
- 量比 ≥1.5 否决（在爆量 z 分之外再加一道简单量比闸门）：1.5 门槛砍组 16-21 -1.24、
  22-26 -0.41 看着同号，但 25-26 子时期 +0.44 反号；1.8 门槛四子时期全负却只砍 1.2% 样本，
  保留组 +0.50/+0.42 对基线 +0.51/+0.40 毫无改善。1.2 门槛 22-26 直接 +0.60 反号。判死；
- 大盘环境/择时闸门（指数在 MA20 上下、市场宽度分档）：时代均值口径下四个子时期全同号
  +4.5~+6.3pp，看着极其稳，但换同日基线立刻塌——闸门后各档 ≈0 或转负（低宽度档 25-26
  -1.09pp, t=-2.3），全体信号日的中位数超额是负的，只有 47-48% 的交易日为正。它测到的
  就是市场 β 本身，不是选股 alpha，所以不必为它突破 signal_at 只看单只股票的契约。

已知边界：①同日基线下的股票级超额 ~0.4pp/10 日、t≈1.5，且随抽样在 ±1pp 内摆动，够不上
独立开仓的理由，它更像一组候选的倾斜权重，不是全天候选股器；②时代口径下收益高度集中在恐慌年
（2018 +5.8、2020 +7.9、2022 +4.5pp），单边慢牛年接近零（2021 -0.4、2023 -0.0）——
这条现在可以解释了：那是 β，不是策略本身的选股能力；③买入风险分（features/risk_score.py，
默认已开启）对本策略只有 +0.15pp、8 年里 7 年为正，比它作用在 trend_rebound 上的 +0.28pp 弱——
因为 `pos60 ≤ 0.35` 已经把"被剔那批"的大部分信息吃掉了（全市场被剔组 pos60 中位 0.54，
本策略池内的被剔组只有 0.21）。别指望再叠一层模型能把它抬成买点。
"""

import pandas as pd

from ..core.base_strategy import BaseSelectStrategy
from ..features.base_columns import is_limit_up
from ..features.patterns import add_pattern_columns
from ..models.result import SelectResult

TRIGGER = "v_reversal"


class PatternReversal(BaseSelectStrategy):
    """低位反转：跌透后已回升的确认信号，低位才认，爆量与涨停不追。"""

    @property
    def description(self) -> str:
        return "低位反转：低位跌透后已回升，量能不极端时买入"

    def required_columns(self) -> list[str]:
        return [*super().required_columns(), "code", "pct", "pos60", "lu", TRIGGER, "vol_blast"]

    def required_rows(self) -> int:
        return 90                      # pos60 要 60 根，v_reversal 的跌幅腿还要左侧余量

    def signal_at(self, df) -> SelectResult | None:
        max_pos = float(self.config.get("max_pos60", 0.35))

        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < 61:
            return None
        if TRIGGER not in df.columns or "vol_blast" not in df.columns:
            df = add_pattern_columns(df)       # mongo/ak 源没有形态列时就地补算，as-of 安全
        if TRIGGER not in df.columns:
            return None
        if "pos60" not in df.columns:
            df = df.assign(pos60=_pos60(df["close"].astype(float)))

        last = df.iloc[-1]
        if not bool(last[TRIGGER]):
            return None

        # 形态看位置：同样的反弹长在 60 日区间上半段就是趋势，不是低位反转
        pos = float(last["pos60"])
        if pd.isna(pos) or pos > max_pos:
            return None
        if _is_limit_up(df, last):             # 涨停买不进这个收盘价，与收益判据无关（见模块注释）
            return None
        if bool(last["vol_blast"]):
            return None                        # 当日爆量：同日基线下 16-21 -2.06pp、22-26 -1.04pp

        close = float(last["close"])
        low6 = float(df["low"].astype(float).rolling(6).min().iloc[-1])
        rebound = close / low6 - 1 if low6 > 0 else float("nan")
        vol_ma5 = float(df["volume"].astype(float).rolling(5).mean().iloc[-1])
        vol_ratio = float(last["volume"]) / vol_ma5 if vol_ma5 > 0 else float("nan")

        # 组内任何因子（位置深浅、反弹幅度、当日涨幅）在两个时代都是反号的，
        # 没有可写进 score 的排序信息，所以命中就是同一档，细节放进 reason 给人看。
        return SelectResult(
            code="",
            name="",
            signal="pattern_reversal",
            score=0.6,
            reason=(f"低位V型反转，pos60={pos:.2f}，自6日低点反弹{rebound * 100:.1f}%，"
                    f"量比{vol_ratio:.2f}"),
        )


def _pos60(close: pd.Series) -> pd.Series:
    """与 base_columns 同一公式：数据源没带 pos60 列时现算。"""
    win = close.rolling(60)
    return (close - win.min()) / (win.max() - win.min() + 1e-9)


def _is_limit_up(df, last) -> bool:
    if "lu" in df.columns:
        return bool(last["lu"])
    if "pct" in last.index and not pd.isna(last["pct"]):
        pct = float(last["pct"])
    else:
        pct = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
    return is_limit_up(str(last.get("code", "")), pct)
