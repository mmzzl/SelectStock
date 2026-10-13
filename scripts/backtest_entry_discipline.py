"""入场纪律实验：信号不能当天成交（收盘后才算出来），但次日可以**挂低一点、买不到就放弃**。

回测引擎按信号日收盘价撮合，`--audit` 量过这个假设的代价（`box_oscillate` 44%~75% 的买入
收在涨停线上，扣掉次晨开盘差后六格期望全负）。那一版只把成交价换成「次晨开盘」，
这里再往前走一步：既然只能次日买，那就头天晚上挂一张限价单——挂在**昨收 ×(1−折价)**，
当天最低都没触到就**不买**。这既是真实可执行的下单方式（App 的「买点参考价」本来就是这个意思，
只是从来没人测过按它成交会怎样），也是唯一有可能把「追涨买入」那批高成本成交自动筛掉的机制：
折价 0 就已经会跳过所有次日跳空高开的票。

每笔独立重演（不看资金/仓位竞争），出场规则与 `KernelStrategy` 完全一致（按收盘价判
止损→止盈→持有天数），所以这张表里只有入场那一处和现役回测不同。
盈亏口径也照 broker：`pnl = 卖出成交额 − 买入成交额 − 卖出费用(佣金+印花税)`（买入佣金不进取，与引擎同）。

用法:
    python3 scripts/backtest_entry_discipline.py --discounts 0,0.01,0.02
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest_exit_scan import (  # noqa: E402  (复用已测的抽样/基线/引擎调用/闸门)
    DEFAULT_ERAS,
    RiskGate,
    _buy_hold,
    _num,
    _run,
    _sample,
)
from stock_selector.backtest.provider import ParquetProvider  # noqa: E402

LOT = 100
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.0005


def _buy_fee(gross: float) -> float:
    return max(COMMISSION_MIN, gross * COMMISSION_RATE)


def _sell_fee(gross: float) -> float:
    return _buy_fee(gross) + gross * STAMP_TAX_RATE


def simulate_entry(dates, open_, low, close, signal_date: str, amount: float,
                   stop_pct: float, take_pct: float, hold_days: int,
                   discount: float) -> dict | None:
    """按「次日挂限价、买不到就放弃」重演一笔交易。

    挂单价对的是**信号日收盘价**（= 现役回测的撮合价，也就是 App「买点参考价」那张牌），
    不是次日开盘价——前一晚下单时看不到次日开盘，用开盘价算限价是前视。
    `discount=0` 因此是「不追高于昨收」，`discount=x` 是「挂在昨收下面 x」。

    返回 None 表示这笔在这个折价下买不到（当天最低都没触到挂单价，或数据不够）。成交返回
    `{fill, exit, pnl, held, reason}`，`reason` 为 `stop|take|time|limit`（limit=数据尽头仍未平）。
    """
    pos = np.where(dates == signal_date)[0]
    if not len(pos):
        return None
    ref = float(close[int(pos[0])])
    if ref <= 0:
        return None
    e = int(pos[0]) + 1                      # 只能次一交易日买
    if e >= len(dates) or close[e] <= 0:
        return None
    o, l = float(open_[e]), float(low[e])
    if o <= 0 or l <= 0:
        return None
    limit = ref * (1.0 - discount)
    if l > limit:
        return None                          # 挂单没触到：不买
    fill = min(o, limit)                     # 低开则按更低的开盘价成交
    shares = int(amount / fill // LOT) * LOT
    if shares < LOT:
        return None
    gross_buy = shares * fill

    for d in range(e + 1, len(dates)):       # T+1：买入当天不能卖
        px = float(close[d])
        if px <= 0:
            continue
        reason = ""
        if stop_pct and px <= fill * (1 - stop_pct):
            reason = "stop"
        elif take_pct and px >= fill * (1 + take_pct):
            reason = "take"
        elif hold_days and d - e >= hold_days:
            reason = "time"
        if reason:
            gross_sell = shares * px
            return {"fill": fill, "exit": px, "held": d - e, "reason": reason,
                    "pnl": gross_sell - gross_buy - _sell_fee(gross_sell)}
    return {"fill": fill, "exit": float(close[-1]), "held": len(dates) - 1 - e, "reason": "limit",
            "pnl": shares * float(close[-1]) - gross_buy - _sell_fee(shares * float(close[-1]))}


def study_cell(provider, code: str, signal_date: str, amount: float, cfg, discounts) -> dict:
    """一只票的一笔买入候选，在多个折价下的结果（同一份日线只取一次数组）。"""
    df = provider._load(code)
    if df is None or df.empty:
        return {}
    dates = df["date"].to_numpy()
    open_ = df["open"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    stop_pct, take_pct, hold_days = cfg
    out = {}
    for x in discounts:
        r = simulate_entry(dates, open_, low, close, signal_date, amount,
                           stop_pct, take_pct, int(hold_days), float(x))
        if r is not None:
            out[float(x)] = r
    return out


def summarize(results: dict[float, list[dict]], discount: float, min_trades: int) -> dict:
    got = [r for r in results[discount] if r]
    if not got:
        return {"n": 0, "wr": 0.0, "payoff": 0.0, "pf": 0.0, "exp": 0.0, "open": 0}
    pnls = [r["pnl"] for r in got]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    # 利润因子（总盈/总亏）要与赔率同排：赔率高而胜率低时两者会给出相反的结论，
    # 只报赔率就等于把「小亏很多次 + 大赚一次」当成好策略。
    return {
        "n": len(got),
        "wr": len(wins) / len(got),
        "payoff": _num(avg_win / avg_loss if avg_loss else None, 99.0),
        "pf": _num(sum(wins) / -sum(losses) if sum(losses) < 0 else None, 99.0),
        "exp": sum(pnls) / len(got),
        "open": sum(1 for r in got if r["reason"] == "limit"),
    }


def run(args) -> None:
    provider = ParquetProvider(ROOT / "data" / "stock_kline")
    eras = args.eras.split(",")
    offsets = [int(x) for x in args.offsets.split(",")]
    exits = [tuple(float(x) for x in item.split(":")) for item in args.exits.split(",")]
    discounts = [float(x) for x in args.discounts.split(",")]
    if 0.0 not in discounts:
        discounts = [0.0] + discounts          # 「挂在昨收」这一档永远要跑，它是别的档的对照

    gate = None
    if args.reject:
        thr = (float(args.reject_thr) if args.reject_thr
               else json.loads(Path(args.reject).with_name("risk_ex10.meta.json")
                              .read_text(encoding="utf-8"))["reject_threshold_ex10"])
        gate = RiskGate(args.reject, thr)
    codes = gate.codes if gate is not None else provider.codes()
    universes = {(era, offset): _sample(codes, args.universe, offset)
                 for era in eras for offset in offsets}
    baselines = {key: _buy_hold(provider, universes[key], key[0]) for key in universes}
    arms = [("无闸门", None)] + ([("有闸门", gate)] if gate is not None else [])
    args.keep_trades = True
    args.audit = False

    agg: dict[tuple, dict[float, list[dict]]] = {}
    for name in args.strategies.split(","):
        for cfg in exits:
            for arm, g in arms:
                results: dict[float, list[dict]] = {x: [] for x in discounts}
                for era in eras:
                    for offset in offsets:
                        t0 = time.time()
                        r = _run(provider, name, cfg, era, universes[(era, offset)], args, g)
                        # 引擎给出的买入成交就是「信号日收盘买入」那批候选；这里逐笔改成次日下单
                        for t in r.get("trades", []):
                            if t.get("side") != "buy":
                                continue
                            got = study_cell(provider, t["code"], t["date"],
                                             float(t.get("amount") or args.target_amount),
                                             cfg, discounts)
                            for x in discounts:
                                results[x].append(got.get(x, {}))
                summary = {x: summarize(results, x, args.min_trades) for x in discounts}
                for x in discounts:
                    agg[(name, cfg, arm, x)] = summary[x]
                    agg[(name, cfg, arm, x)]["cells"] = (len(eras) * len(offsets))
                line = " | ".join(
                    f"折{x:.1%} 成{summary[x]['n']}/{len(results[x])}"
                    f" payoff{_num(summary[x]['payoff'], 99):4.2f} 期望{summary[x]['exp']:+7.1f}"
                    for x in discounts)
                print(f"  {name:17s} {arm:6s} 止损{cfg[0]:.2f} 止盈{cfg[1]:2.2f} "
                      f"持有{cfg[2]:2.0f} 时代{len(eras)}×抽样{len(offsets)} | {line}"
                      f"  ({time.time() - t0:.0f}s)", flush=True)

    print("\n===== 汇总：每个 (策略, 出场, 闸门, 折价) 的合并样本 =====")
    for (name, cfg, arm, x), s in agg.items():
        enough = s["n"] >= args.min_trades
        # 目标 ①+④：赔率、胜率、利润因子、期望四项一起看。只看赔率会把
        # 「低胜率×高赔率」的截断结果当达标（本项目已实测出现过这种格）。
        payoff_ok = _num(s["payoff"], 0.0) >= args.payoff_target
        wr_ok = s["wr"] >= args.wr_target
        pf_ok = s["pf"] >= args.pf_target
        exp_ok = s["exp"] > 0
        ok = enough and payoff_ok and wr_ok and pf_ok and exp_ok
        print(f"  {name:17s} {arm:6s} 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} 持有{cfg[2]:2.0f} "
              f"折价{x:5.1%} 成交{s['n']:4d} 胜率{s['wr']:5.1%} "
              f"payoff{_num(s['payoff'], 99):5.2f} 利润因子{s['pf']:5.2f} "
              f"期望{s['exp']:+7.1f} 未平{s['open']:3d} "
              + ("①+④达标✓" if ok else "达标×")
              + ("" if enough else f" 样本不足(<{args.min_trades})"))
    print(f"  达标口径：成交≥{args.min_trades} 且 赔率≥{args.payoff_target:.1f} 且 "
          f"胜率≥{args.wr_target:.0%} 且 利润因子≥{args.pf_target:.1f} 且 单笔期望>0。")
    print("\n  成交数是合并样本（所有时代×抽样），所以只能横向比折价，不能和单格回测笔数比。")
    print("  「买不到就放弃」会让成交数随折价下降：剩下的那批才是真实能拿到的，"
          "所以判据是成交数与单笔期望同时看，只看期望可以把任何策略做漂亮。")
    print(f"  未平(reason=limit)那几笔按最后一天收盘价结算，实盘还拿着，读数里要当噪声。")


def main():
    ap = argparse.ArgumentParser(description="入场纪律：次日挂低多少、买不到就放弃")
    ap.add_argument("--strategies", default="box_oscillate,pattern_reversal")
    ap.add_argument("--exits", default="0.05:0.00:10,0.04:0.10:15")
    ap.add_argument("--eras", default=",".join(DEFAULT_ERAS))
    ap.add_argument("--offsets", default="0,137,274")
    ap.add_argument("--universe", type=int, default=250)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--position-cap-pct", type=float, default=0.2)
    ap.add_argument("--target-amount", type=float, default=15000.0)
    ap.add_argument("--discounts", default="0,0.005,0.01,0.02,0.03")
    ap.add_argument("--payoff-target", type=float, default=2.0)
    ap.add_argument("--wr-target", type=float, default=0.50, help="胜率目标（目标④的口径之一）")
    ap.add_argument("--pf-target", type=float, default=2.0, help="利润因子目标（目标④的口径之一）")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--reject", default="data/models/risk_oof.parquet")
    ap.add_argument("--reject-thr", default="")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
