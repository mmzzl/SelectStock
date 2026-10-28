"""批次 T 读数：阶梯加仓（40/30/20/10 + k×ATR 间距）与「止损挂最近一笔成交价」（docs §33）。

用法：
    python3 -u scripts/qlib_ladder_ab.py                    # 主读数（Q 档 673 天）+ 现役核对（72 天）
    python3 -u scripts/qlib_ladder_ab.py --archive pred_batchP_h10

口径全部照 §33.2 写死的表，这里只复述读数时必须一起给的三条：
- **点级**：每只票独立拿 10 万（`--target-cost`）走阶梯，不控账户现金、不并仓 ⇒
  它回答「这条规则的每笔交易赚不赚」，不回答「组合净值会不会更好」。
- 触线按**线价**成交（跳空低开时真实成交更差），加仓跳空高开按**开盘**成交 ⇒ 止损臂偏乐观。
- 阶梯没打满四档时，剩下的钱在本读数里按 **0 收益** 算（不假设利息），所以除了「已投入部分的
  收益率」还给「按目标成本折算的收益」——后者才是和 A 臂同分母的那个数。

规则本体在 `stock_selector/backtest/ladder.py`（纯函数，单测 `tests/test_backtest_ladder.py`）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector.backtest import ladder  # noqa: E402
from stock_selector.features.chips import ATR_WINDOW, atr14  # noqa: E402
from stock_selector_qlib import artifacts, serving  # noqa: E402

FIELDS = ("$open", "$high", "$low", "$close", "$amount")
TOP = 30                       # App 一屏的名单长度，与 §27 同
ARMS = ("B2", "B15", "C")
K_OF = {"B2": ladder.K_MAIN, "B15": ladder.K_ALT, "C": ladder.K_MAIN}
STOP_OF = {"B2": ladder.STOP_PCT, "B15": ladder.STOP_PCT, "C": 0.0}


def price_frames(pool: list[str], beg: str, end: str, hold: int) -> dict[str, pd.DataFrame]:
    """[beg−预热, end+hold+2] 的 OHLC/成交额宽表。ATR 要往回看 15 根，所以左边先挪。"""
    import qlib.data

    days = serving.calendar()
    start = days[max(0, days.index(beg) - (ATR_WINDOW + 2))]
    tail = days[min(len(days) - 1, days.index(end) + hold + 2)]
    frame = qlib.data.D.features(list(pool), list(FIELDS), start_time=start, end_time=tail)
    out = {}
    for field in FIELDS:
        w = frame[field].unstack("instrument")
        w.index = pd.to_datetime(w.index)
        out[field] = w.sort_index()
    return out


def window(out: dict[str, pd.DataFrame], dates: pd.DatetimeIndex, i: int,
           code: str, hold: int) -> pd.DataFrame | None:
    """切出「入场日之前 14 根 + 入场日 + 之后 hold 根」这一帧；未来段缺数就不给读数。

    断档必须挡在门口：`simulate_ladder` 右端不够 hold 根时返回 None，但**中途**停牌（NaN）会被
    当成「既没触止损也没触加仓」混进「拿满 10 日」，那等于把缺数读成平安。
    """
    beg = i - ATR_WINDOW
    if beg < 0 or i + hold >= len(dates):
        return None
    rows = dates[beg:i + hold + 1]
    bars = pd.DataFrame({name[1:]: out[name].loc[rows, code].to_numpy(dtype=float)
                         for name in FIELDS})
    forward = bars.iloc[ATR_WINDOW:]
    if not np.isfinite(forward.to_numpy(dtype=float)).all():
        return None
    return bars


def entry_plan(bars: pd.DataFrame, k: float) -> tuple[float, float | None, float | None]:
    """入场日该知道的那三件事：ATR14、第一档触发价、第一条止损线——全都只吃入场日及之前。"""
    entry = float(bars["close"].iloc[ATR_WINDOW])
    vol = atr14(bars.iloc[:ATR_WINDOW + 1])
    return entry, ladder.rung_price(entry, vol, k), ladder.raise_stop(None, entry)


def lookahead_check(bars_list: list[pd.DataFrame], k: float) -> None:
    """§33.4 的自查：把入场日**之后**的行情改成极端值，上面那三件事必须一字不变。

    同时跑一个反向对照（故意多喂一根未来 bar 进 ATR）——如果它也不变，这个自查就是空的，
    绿灯说明不了任何事。
    """
    same = 0
    worst = 0.0
    for bars in bars_list:
        entry, rung, stop = entry_plan(bars, k)
        perturbed = bars.copy()
        fwd = perturbed.iloc[ATR_WINDOW + 1:]
        for col in ("open", "high", "close"):
            perturbed.loc[fwd.index, col] = fwd[col] * 10.0
        perturbed.loc[fwd.index, "low"] = fwd["low"] / 10.0
        e2, r2, s2 = entry_plan(perturbed, k)
        same += int(e2 == entry and r2 == rung and s2 == stop)
        peek = atr14(perturbed.iloc[:ATR_WINDOW + 2])   # 反向对照：故意多算一根未来
        base = atr14(bars.iloc[:ATR_WINDOW + 1])
        if np.isfinite(peek) and np.isfinite(base) and base > 0:
            worst = max(worst, abs(peek - base) / base)
    print(f"\n=== 防前瞻自查（§33.4） ===")
    print(f"    抽 {len(bars_list)} 个窗口，把入场日之后改成 ±10 倍极端行情 ⇒ "
          f"入场日 ATR/第一档触发价/第一条止损线**一字不变**的窗口数 = {same}/{len(bars_list)}"
          f"（必须全中）")
    print(f"    反向对照：同一段极端行情只要多喂进 ATR 一根，最大相对变化 {worst:.1%}"
          f"（要明显 >0，否则上面那条全中是空测）")


def on_target(df: pd.DataFrame, arm: str) -> pd.Series:
    """按目标成本折算的收益：已投入收益率 × 投入比例（没打满的那几档按 0 收益算）。"""
    return df[f"{arm}_ret"] * df[f"{arm}_inv"]


def paired_daily(df: pd.DataFrame, arm: str) -> pd.Series:
    """日等权配对差（同一天同一批票，臂减 A），再按天平均——t 的自变量是「日」不是「笔」。"""
    return (df[arm] - df["A"]).groupby(df["date"]).mean().sort_index().dropna()


def t_stat(daily: pd.Series, hold: int) -> tuple[float, float]:
    std = float(daily.std())
    t = float(daily.mean()) / std * np.sqrt(len(daily)) if std else float("nan")
    return t, t / np.sqrt(hold)


def run_archive(art, name: str, hold: int, top: int, target_cost: float) -> None:
    path = REPO / "data" / "qlib" / f"{name}.parquet"
    if not path.is_file():
        print(f"[跳过] 没有分数档案 {path.name}")
        return
    pred = pd.read_parquet(path)
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    days = sorted(pred["datetime"].unique())
    beg, end = pd.Timestamp(days[0]), pd.Timestamp(days[-1])
    pool = sorted(pred["instrument"].unique())
    print(f"\n{'=' * 78}\n分数档案 {name}：{len(pool)} 只 / {len(days)} 个信号日"
          f"（{beg.date()}→{end.date()}）  每笔预算 {target_cost:,.0f} 元\n{'=' * 78}")

    t0 = time.time()
    out = price_frames(pool, str(beg.date()), str(end.date()), hold)
    dates = out["$close"].index
    pos = {d: i for i, d in enumerate(dates)}
    scores = pred.pivot(index="datetime", columns="instrument", values="score")

    rows, probes, skipped = [], [], 0
    for d in scores.index:
        i = pos.get(d)
        if i is None or i + hold >= len(dates):
            continue
        top_codes = scores.loc[d].dropna().sort_values(ascending=False).index[:top]
        pool_base = out["$close"].iloc[i + hold] / out["$close"].iloc[i] - 1.0
        pool_base = float(pool_base[pool_base == pool_base].mean())
        for code in top_codes:
            bars = window(out, dates, i, code, hold)
            if bars is None:
                skipped += 1
                continue
            entry = float(bars["close"].iloc[ATR_WINDOW])
            if not np.isfinite(entry) or entry <= 0:
                skipped += 1
                continue
            if len(probes) < 8:
                probes.append(bars)
            # A 臂：一笔买满、不设止损、拿满 hold 日（=§27 的口径，只是这里把费扣掉）
            gross = float(bars["close"].iloc[ATR_WINDOW + hold]) / entry - 1.0
            ret_a = ((target_cost * (1 + gross)) * (1 - ladder.SELL_FEE)
                     / (target_cost * (1 + ladder.BUY_FEE)) - 1.0)
            fwd = bars.iloc[ATR_WINDOW + 1:ATR_WINDOW + hold + 1]
            # 裸价面板的污染计数：除权跳空在这里长得和真跌一模一样（§33.4 订正）
            seg = bars["close"].iloc[ATR_WINDOW:ATR_WINDOW + hold + 1]
            steps_ret = seg / seg.shift(1) - 1.0
            rec = {"date": d, "code": code, "A": ret_a, "base": pool_base,
                   "entry": entry, "gross": gross,
                   "atr": atr14(bars.iloc[:ATR_WINDOW + 1]),
                   "deep": float(fwd["low"].min()) / entry - 1.0,
                   "peak": float(fwd["high"].max()) / entry - 1.0,
                   "drop5": int((steps_ret < -0.05).sum()),
                   "impossible": int((steps_ret.abs() > 0.205).sum())}
            for arm in ARMS:
                r = ladder.simulate_ladder(bars, entry_at=ATR_WINDOW,
                                           target_cost=target_cost, k=K_OF[arm],
                                           stop_pct=STOP_OF[arm], hold=hold)
                if r is None:
                    rec[f"{arm}_ret"] = float("nan")
                    continue
                rec[f"{arm}_ret"] = r["ret_pct"]
                rec[f"{arm}_inv"] = r["invested"] / target_cost
                rec[f"{arm}_steps"] = r["steps"]
                rec[f"{arm}_stop"] = 1.0 if r["exit_reason"] == "stop" else 0.0
                rec[f"{arm}_exit"] = r["exit_at"] - ATR_WINDOW
            rows.append(rec)
    df = pd.DataFrame(rows)
    for arm in ARMS:
        df[arm] = on_target(df, arm)
    print(f"用时 {time.time() - t0:.0f} 秒  可评估 {len(df):,} 个「日·票」（缺数/越界剔掉 {skipped:,}）"
          f"  基准=当日全池 {len(pool)} 只等权 {hold} 日收益")

    print(f"\n=== 底色：名单 {top} 只在 {hold} 日内怎么走 ===")
    print(f"    入场日 ATR14：中位 {df['atr'].median():.3f}"
          f"（占价 {df['atr'].median() / df['entry'].median():.2%}）⇒ "
          f"第一档加仓要涨 {ladder.K_MAIN * df['atr'].median() / df['entry'].median():.2%}，"
          f"止损线在下方 {ladder.STOP_PCT:.0%}")
    print(f"    10 日最深下探：中位 {df['deep'].median():+.1%}，"
          f"10 分位 {df['deep'].quantile(0.10):+.1%}（先碰止损还是先碰加仓，看这两行）")
    print(f"    10 日最高上冲：中位 {df['peak'].median():+.1%}，"
          f"25 分位 {df['peak'].quantile(0.25):+.1%}")
    print(f"    不设止损拿满 {hold} 日：平均 {df['A'].mean():+.2%}（扣费后，超额 "
          f"{(df['A'] - df['base']).mean():+.2%}）")
    bars_total = len(df) * hold
    print(f"    裸价污染（§33.4 订正）：持有期 {bars_total:,} 根里单日跌 >5% 共 {int(df['drop5'].sum()):,} 根"
          f"（{df['drop5'].sum() / bars_total:.2%}），单日 |涨跌| >20.5% 共 {int(df['impossible'].sum()):,} 根"
          f"——除权跳空在这里就是根真下跌，止损触线率别当干净数引用")

    print(f"\n=== 四臂读数（扣费后；超额 = 减当日全池等权 {hold} 日收益） ===")
    print(f"{'臂':>4} {'已投入收益率':>12} {'投入比例':>9} {'按目标成本收益':>13} "
          f"{'平均笔数':>8} {'加满四档':>8} {'止损先响':>8} {'平均超额':>9}")
    for arm in ("A",) + ARMS:
        if arm == "A":
            inv, steps = pd.Series(1.0, index=df.index), pd.Series(1.0, index=df.index)
            full = stopped = float("nan")
            ret, on_tgt = df["A"], df["A"]
        else:
            ret = df[f"{arm}_ret"]
            inv, steps = df[f"{arm}_inv"], df[f"{arm}_steps"]
            on_tgt = df[arm]
            full = float((steps == len(ladder.WEIGHTS)).mean())
            stopped = float(df[f"{arm}_stop"].mean())
        ex = on_tgt - df["base"]
        print(f"{arm:>4} {ret.mean():>+11.2%} {float(inv.mean()):>9.1%} "
              f"{on_tgt.mean():>+12.2%} {float(steps.mean()):>8.2f} "
              f"{full:>7.1%} {stopped:>7.1%} {ex.mean():>+10.2%}")

    print(f"\n=== 配对日差（臂 − A，日等权；t 已 ÷√{hold}） ===")
    print(f"{'对照':>9} {'日均差':>9} {'t':>7} {'t÷√10':>7} {'前半':>8} {'后半':>8} {'同号':>6} {'天数':>6}")
    for arm in ARMS:
        daily = paired_daily(df, arm)
        half = len(daily) // 2
        t, t10 = t_stat(daily, hold)
        a, b = daily.iloc[:half].mean(), daily.iloc[half:].mean()
        same = "同" if np.sign(a) == np.sign(b) else "反"
        print(f"{arm + '−A':>9} {daily.mean():>+8.2%} {t:>7.2f} {t10:>7.2f} "
              f"{a:>+7.2%} {b:>+7.2%} {same:>6} {len(daily):>6}")
    daily_b = paired_daily(df, "B2")
    daily_c = paired_daily(df, "C")
    daily_bc = (daily_b - daily_c).dropna()
    t, t10 = t_stat(daily_bc, hold)
    print(f"{'B2−C':>9} {daily_bc.mean():>+8.2%} {t:>7.2f} {t10:>7.2f}  "
          f"（这一行=止损上移那半条旋钮单独值多少钱）")

    if probes:
        lookahead_check(probes, ladder.K_MAIN)

    print(f"\n读法：B2−A 是「阶梯 + 抬止损」的合力，C−A 只是加仓节奏，B2−C 只是止损上移。"
          f"§33.3 的主判据看 B2−A 的 t÷√10 与前后半段是否同号。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=None,
                    help="分数档案名（不带 .parquet）；缺省跑 pred_batchQ_lab10 主读数 + 现役 batchP_h10 核对")
    ap.add_argument("--target-cost", type=float, default=100000.0)
    ap.add_argument("--top", type=int, default=TOP)
    ap.add_argument("--hold", type=int, default=10)
    args = ap.parse_args()

    art = artifacts.load()
    serving.init_qlib()
    print(f"模型档案：{art.run_id}（train_end {art.meta.get('train_end')}）")
    archives = [args.archive] if args.archive else ["pred_batchQ_lab10", "pred_batchP_h10"]
    for name in archives:
        run_archive(art, name, args.hold, args.top, args.target_cost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
