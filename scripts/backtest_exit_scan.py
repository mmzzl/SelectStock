"""退出参数扫描：把「赢比到 2.0」拆成两个口径，并按三轴门槛判定能不能算数。

入场与实盘选股同源（KernelStrategy），出场走三条机械规则（止损/止盈/持有天数），
本脚本只动出场参数，用来回答一个具体问题：赢比能不能被参数凑到 2.0，凑到之后
单笔期望是否还是正的，换个时代 / 换批股票是否同号。

两个口径都测，因为页面历史上的「盈亏比」指的是 profit_factor（总盈/总亏），而
单笔赔率 payoff（均盈/均亏）才是输一把赢一把的比值：
    profit_factor = payoff × 胜率/(1-胜率)
胜率 33% 时 payoff 2.0 只对应 profit_factor 1.0；反过来 profit_factor 达 2.0 也可以
是「小亏很多笔 + 大赚一笔」的截断结果。所以两个数分开看、都过才算达标。

用法:
    python3 scripts/backtest_exit_scan.py
    python3 scripts/backtest_exit_scan.py --universe 250 --eras 2025-09-01:2026-09-01
    # 再加一臂：买入前过样本外风险分（先跑 train_risk_model.py --oof-out 出那份打分）
    python3 scripts/backtest_exit_scan.py --reject data/models/risk_oof.parquet \
        --exits 0.05:0.15:20,0.06:0.18:25

给 --reject 时股票池自动换成「有样本外分的那批」（约 1/3 抽样），两臂共用同一池、同一出场参数，
差的只有买入前那道剔除，所以笔数会掉——掉笔数本身不是成本，换手才是。
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.backtest.engine import BacktestEngine
from stock_selector.backtest.kernel_strategy import KernelStrategy
from stock_selector.backtest.provider import ParquetProvider
from stock_selector.core.selector import create_strategy
from stock_selector.features.base_columns import board_limit_threshold

# (止损, 止盈, 最大持有交易日)：百分比都对成本价，与插件判据无关
DEFAULT_EXITS = [
    (0.05, 0.00, 10),   # 现状缺省：只止损 + 10 日清仓，无止盈
    (0.05, 0.10, 20),
    (0.05, 0.15, 20),
    (0.06, 0.18, 25),
    (0.08, 0.16, 20),
    (0.08, 0.24, 30),
    (0.10, 0.30, 40),
]
DEFAULT_ERAS = ["2019-09-01:2020-09-01", "2022-09-01:2023-09-01", "2025-09-01:2026-09-01"]


def _sample(codes: list[str], n: int, offset: int) -> list[str]:
    """等距抽样再整体平移 offset 格：换一批股票就是换个样本。

    前 n 个 code 全是最老编号的深市主板，板块直接偏掉，所以不能只取头 n 个。
    """
    if n >= len(codes):
        return codes
    step = len(codes) // n
    return [codes[(i * step + offset) % len(codes)] for i in range(n)]


def _buy_hold(provider: ParquetProvider, codes: list[str], era: str) -> float:
    """同一批股票在这个时代的等权买入持有收益（首日买入、末日卖出，不计费用）。

    加这一列是因为绝对收益会被时代吃掉：2022 与 2025 两个样本年买入持有都是负的，
    「期望为负」里有多少是选股没用、有多少只是大盘，只有减掉这条基线才分得开。
    """
    start, end = era.split(":")
    rets = []
    for code in codes:
        w = provider.window(code, end)
        if w is None:
            continue
        w = w.loc[w["date"] >= start]
        if len(w) < 2:
            continue
        first, last = float(w.iloc[0]["close"]), float(w.iloc[-1]["close"])
        if first > 0:
            rets.append(last / first - 1)
    return sum(rets) / len(rets) if rets else 0.0


class RiskGate:
    """买入侧剔除闸门：命中之后、下单之前过一遍样本外风险分。

    分数只能来自 `scripts/train_risk_model.py --oof-out`（每折只用折前数据训练、折间留 40 天空窗）。
    拿现役模型给历史打分等于用 2026 年的知识判 2019 年，闸门会明显过好，那样测出来的结论没用。
    当天没有分（涨停日被排除在评估集外、新股行数不足）按放行处理——与实盘「信息不够就不判」一致。
    """

    def __init__(self, path: str | Path, threshold: float) -> None:
        df = pd.read_parquet(path, columns=["code", "date", "score"])
        df = df[df["score"].notna()].sort_values("date")
        self.threshold = float(threshold)
        self.codes = sorted(df["code"].unique())
        self._by: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for code, grp in df.groupby("code", sort=False):
            self._by[code] = (grp["date"].to_numpy("datetime64[D]").astype("int64"),
                              grp["score"].to_numpy(dtype="float32"))

    def __call__(self, date: str, code: str) -> bool:
        got = self._by.get(code)
        if got is None:
            return True
        days, scores = got
        key = np.datetime64(date, "D").astype("int64")
        i = int(np.searchsorted(days, key))
        if i >= len(days) or days[i] != key:
            return True
        return bool(scores[i] > self.threshold)


def _run(provider: ParquetProvider, name: str, cfg, era: str, universe: list[str], args,
         gate=None) -> dict:
    stop_pct, take_pct, hold_days = cfg
    start, end = era.split(":")
    strategy = KernelStrategy(
        strategy=create_strategy(name),
        provider=provider,
        target_amount=args.target_amount,
        universe=universe,
        max_hold_days=int(hold_days),
        stop_loss_pct=stop_pct,
        take_profit_pct=take_pct,
        reject_gate=gate,
    )
    engine = BacktestEngine(
        strategy=strategy,
        data=provider,
        capital=args.capital,
        max_positions=args.max_positions,
        position_cap_pct=args.position_cap_pct,
        start_date=start,
        end_date=end,
        params={"strategy": name, "stop_loss_pct": stop_pct, "take_profit_pct": take_pct,
                "max_hold_days": hold_days, "era": era, "universe": len(universe),
                "universe_size": args.universe, "target_amount": args.target_amount,
                "gate": "risk_oof" if gate is not None else ""},
    )
    res = engine.run()
    m = res.metrics
    eq = [p for p in res.equity if p["total_value"] > 0]
    # 两臂的总收益不能直接比：闸门剔掉 100 笔＝更多天钱没放进去，
    # 收益差里有多少是「挑得准」、有多少只是「仓位低」，用平均持仓占比分开。
    expo = sum(p["positions_value"] / p["total_value"] for p in eq) / len(eq) if eq else 0.0
    r = {
        "n": m["trade_count"],
        "wr": m["win_rate"],
        "payoff": m["payoff_ratio"],
        "pf": m["profit_factor"],
        "exp": m["expectancy"],
        "ret": m["total_return"],
        "dd": m["max_drawdown"],
        "rej": strategy.rejected,
        "expo": expo,
    }
    if args.audit:
        r.update(_fill_audit(provider, res.trades))
    if getattr(args, "keep_trades", False):
        # 给入场纪律实验用：它要逐笔把成交改成「次日下单」重演，所以要把明细带出去。
        r["trades"] = res.trades
    return r


def _num(v, default: float = 0.0) -> float:
    """payoff / profit_factor 在没有亏损或没有盈利时序列化为 None（inf 不可存）。"""
    return default if v is None else float(v)


def _next_row(provider, date: str, code: str) -> dict | None:
    """成交日之后的第一个有日线交易日（停牌自动跳过），用于量「次一交易日才能真下单」的价差。"""
    df = provider._load(code)
    if df is None or df.empty:
        return None
    pos = np.where(df["date"].to_numpy() == date)[0]
    if not len(pos) or pos[0] + 1 >= len(df):
        return None
    return df.iloc[pos[0] + 1].to_dict()


def _fill_audit(provider, trades: list[dict]) -> dict:
    """成交可执行性审计（只读，不改回测行为）。

    引擎按当日收盘价撮合，没有任何涨跌停约束：跌停封死那天照样记一笔「卖出成交」，
    涨停那天照样记「买入成交」。而 payoff 上 2.0 靠的正是止损砍左尾（原始命中点的
    T+10 日等权超额是 0 到负），所以「被砍掉的那批深亏里有多少其实出不了货」
    直接决定这个赢比能不能信。

    还有一笔口径差：信号是收盘后算出来的，真实成交最早在次一交易日，所以买入用
    「次晨开盘价 ÷ 回测撮合价 − 1」量回测白拿的进场优势；涨停日那笔通常最大，
    因为这类策略本来就是靠冲高那天取胜的。
    """
    out = {"sell_n": 0, "ld_sell": 0, "one_sell": 0, "ld_loss": 0.0,
           "loss_sum": 0.0, "lu_buy": 0, "one_buy": 0, "buy_n": 0, "no_row": 0,
           "gap_sum": 0.0, "gap_n": 0, "gap_lu_sum": 0.0, "gap_lu_n": 0,
           "gap_cost_sum": 0.0}
    for t in trades:
        side = t.get("side")
        if side not in ("buy", "sell", "add"):
            continue
        row = provider.rows(t["date"], [t["code"]]).get(t["code"])
        if not row:
            # 取不到当天日线（停牌/日期不在样本里）就不计入分母：
            # 默认判成「能成交」会把占比压低，默认判成「不能」会把止损显得无用。
            out["no_row"] += 1
            continue
        pnl = float(t.get("pnl") or 0.0)
        hi, lo = float(row.get("high") or 0), float(row.get("low") or 0)
        sealed = bool(hi and lo and hi == lo)      # 一字板：当天根本没对手盘
        pct = float(row.get("pct") or 0.0)
        thr = board_limit_threshold(t["code"])
        if side == "sell":
            out["sell_n"] += 1
            if pnl < 0:
                out["loss_sum"] += -pnl
            if pct <= -thr:
                out["ld_sell"] += 1
                if pnl < 0:
                    out["ld_loss"] += -pnl
            if sealed:
                out["one_sell"] += 1
        else:
            out["buy_n"] += 1
            # 分开数：收在涨停线（可能炸板、还能买进，只是追高）与一字封死（真的买不到）
            at_limit = pct >= thr
            if at_limit:
                out["lu_buy"] += 1
            if sealed:
                out["one_buy"] += 1
            nxt = _next_row(provider, t["date"], t["code"])
            fill = float(t.get("price") or 0)
            if nxt is not None and fill > 0 and float(nxt.get("open") or 0) > 0:
                gap = float(nxt["open"]) / fill - 1.0
                out["gap_sum"] += gap
                out["gap_n"] += 1
                # 换算成钱：次晨开盘买入要多付多少元/笔，直接从单笔期望里扣
                out["gap_cost_sum"] += gap * float(t.get("amount") or 0)
                if at_limit:
                    out["gap_lu_sum"] += gap
                    out["gap_lu_n"] += 1
    return out


def scan(args) -> list[tuple]:
    provider = ParquetProvider(ROOT / "data" / "stock_kline")
    eras = args.eras.split(",")
    offsets = [int(x) for x in args.offsets.split(",")]
    exits = [tuple(float(x) for x in item.split(":")) for item in args.exits.split(",")]

    gate = None
    if args.reject:
        thr = (float(args.reject_thr) if args.reject_thr
               else json.loads(Path(args.reject).with_name("risk_ex10.meta.json")
                              .read_text(encoding="utf-8"))["reject_threshold_ex10"])
        gate = RiskGate(args.reject, thr)
        print(f"闸门：样本外风险分 ≤ {thr:+.6f} 剔除；打分覆盖 {len(gate.codes)} 只票"
              f"（现役模型按文件序 stride 抽样训练，覆盖数 != 全市场 "
              f"{len(provider.codes())} 只；两臂都只能在这批里抽，"
              "所以这张表读「同池同参只差一道剔除」，不能和全市场那张表比绝对值）", flush=True)
    # 两臂必须用同一批股票，否则「闸门有没有用」和「换了个样本」分不清
    codes = gate.codes if gate is not None else provider.codes()
    universes = {(era, offset): _sample(codes, args.universe, offset)
                 for era in eras for offset in offsets}
    baselines = {key: _buy_hold(provider, universes[key], key[0]) for key in universes}
    arms = [("无闸门", None)] + ([("有闸门", gate)] if gate is not None else [])

    rows: list[tuple] = []
    for name in args.strategies.split(","):
        for cfg in exits:
            for arm, g in arms:
                cells: list[tuple] = []
                for era in eras:
                    for offset in offsets:
                        t0 = time.time()
                        r = _run(provider, name, cfg, era, universes[(era, offset)], args, g)
                        r["bh"] = baselines[(era, offset)]
                        cells.append((era[:4], offset, r))
                        aud = ""
                        if args.audit and r.get("sell_n"):
                            share = (r["ld_loss"] / r["loss_sum"]) if r["loss_sum"] else 0.0
                            aud = (f" 卖跌停{r['ld_sell']}/{r['sell_n']}"
                                   f"(一字{r['one_sell']}，占亏损{share:.0%})"
                                   f" 买涨停{r['lu_buy']}/{r['buy_n']}(一字{r['one_buy']})"
                                   + (f" 无日线{r['no_row']}" if r["no_row"] else "")
                                   + (f" 次晨差{r['gap_sum'] / r['gap_n']:+.2%}"
                                      f"(涨停单{r['gap_lu_sum'] / max(r['gap_lu_n'], 1):+.2%})"
                                      f" 扣后期望{r['exp'] - r['gap_cost_sum'] / max(r['buy_n'], 1):+7.1f}"
                                      if r["gap_n"] else ""))
                        print(f"  {name:17s} {arm:6s} 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} "
                              f"持有{cfg[2]:2.0f} 时代{era[:4]} 样本+{offset} n={r['n']:3d} "
                              f"剔{r['rej']:5d} "
                              f"payoff={_num(r['payoff'], 99):5.2f} pf={_num(r['pf'], 99):5.2f} "
                              f"期望={r['exp']:+7.1f} 仓={r['expo']:.0%} 收益={r['ret']:+7.2%} "
                              f"持有基准={r['bh']:+6.2%} 超额={r['ret'] - r['bh']:+7.2%} "
                              f"回撤={r['dd']:+7.2%}{aud} ({time.time() - t0:.0f}s)", flush=True)
                rows.append((name, cfg, arm, cells))
    return rows


def render(rows, args) -> str:
    out = ["", "===== 汇总：每个 (策略, 退出参数, 闸门) 的全部样本 ====="]
    for name, cfg, arm, cells in rows:
        payoffs = [_num(c[2]["payoff"], 99.0) for c in cells]
        pfs = [_num(c[2]["pf"], 99.0) for c in cells]
        wrs = [c[2]["wr"] for c in cells]
        exps = [c[2]["exp"] for c in cells]
        excess = [c[2]["ret"] - c[2]["bh"] for c in cells]
        ns = [c[2]["n"] for c in cells]
        expos = [c[2]["expo"] for c in cells]
        enough = min(ns) >= args.min_trades
        payoff_pass = enough and min(payoffs) >= args.payoff_target and min(exps) > 0
        pf_pass = enough and min(pfs) >= args.pf_target and min(exps) > 0
        wr_pass = enough and min(wrs) >= args.wr_target and min(exps) > 0
        # 目标 ①+④ 的字面口径：赔率、胜率、利润因子、期望四项在**每一格**同时达标。
        # 单项过线都不算数——赔率可以靠截断亏损凑出来而期望仍为负，
        # 胜率可以靠"多买多亏"压到 50% 以下换高赔率，利润因子在胜率很低时照样能过 2。
        all_pass = enough and payoff_pass and wr_pass and pf_pass
        out.append(
            f"{name:17s} {arm:6s} 止损{cfg[0]:.2f} 止盈{cfg[1]:.2f} 持有{cfg[2]:2.0f} | "
            f"payoff {min(payoffs):.2f}~{max(payoffs):.2f} | pf {min(pfs):.2f}~{max(pfs):.2f} | "
            f"胜率 {min(wrs):.1%}~{max(wrs):.1%} | "
            f"期望 {min(exps):+7.1f}~{max(exps):+7.1f} | "
            f"超额 {min(excess):+6.2%}~{max(excess):+6.2%} 均值{sum(excess)/len(excess):+6.2%} | "
            f"笔数 {min(ns)}~{max(ns)} 剔{sum(c[2]['rej'] for c in cells):5d} "
            f"仓 {min(expos):.0%}~{max(expos):.0%}"
            + (f" | 卖跌停{sum(c[2]['ld_sell'] for c in cells)}/"
               f"{sum(c[2]['sell_n'] for c in cells)}"
               f" 占亏{sum(c[2]['ld_loss'] for c in cells) / max(sum(c[2]['loss_sum'] for c in cells), 1e-9):.0%}"
               f" 一字卖{sum(c[2]['one_sell'] for c in cells)}"
               f" 买涨停{sum(c[2]['lu_buy'] for c in cells)}/"
               f"{sum(c[2]['buy_n'] for c in cells)}"
               f" 一字买{sum(c[2]['one_buy'] for c in cells)}"
               if "ld_sell" in cells[0][2] else "")
            + " | "
            + ("payoff✓ " if payoff_pass else "payoff× ")
            + ("pf✓ " if pf_pass else "pf× ")
            + ("胜率✓" if wr_pass else "胜率×")
            + ("  ①+④全格达标✓" if all_pass else "")
            + ("" if enough else f"  样本不足(<{args.min_trades} 笔)"))
    out.append("")
    out.append("  达标口径：全部时代 × 全部抽样 上 payoff>=%.1f、胜率>=%.0f%%、pf>=%.1f 且单笔期望>0。"
               % (args.payoff_target, args.wr_target * 100, args.pf_target))
    out.append("  只过一个不算过：pf 可由低胜率×高赔率凑出，payoff 可由截断亏损凑出而期望仍为负。")
    out.append("  超额=收益−同样本等权买入持有。绝对期望为负可能只是时代在跌，"
               "超额仍为 0 才说明选股本身没用。")
    out.append("  闸门臂的笔数会下降（少买），比的是同一批股票、同一组出场参数，只差在买入前那一道剔除。")
    out.append("  跨臂比收益/超额要同时看「仓」：剔掉买入机会＝更多天空仓，收益差里混着仓位差；"
               "判「挑得准不准」只看 payoff 与单笔期望。")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="退出参数扫描：赢比两个口径 + 三轴门槛")
    ap.add_argument("--strategies", default="box_oscillate,pattern_reversal")
    ap.add_argument("--exits", default=",".join(f"{s}:{t}:{h}" for s, t, h in DEFAULT_EXITS))
    ap.add_argument("--eras", default=",".join(DEFAULT_ERAS))
    ap.add_argument("--offsets", default="0,137", help="抽样平移格数，逗号分隔=多个股票样本")
    ap.add_argument("--universe", type=int, default=300)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--position-cap-pct", type=float, default=0.2)
    ap.add_argument("--target-amount", type=float, default=15000.0)
    ap.add_argument("--payoff-target", type=float, default=2.0)
    ap.add_argument("--pf-target", type=float, default=2.0)
    ap.add_argument("--wr-target", type=float, default=0.50,
                    help="胜率目标（目标④的口径之一，与赔率/利润因子/期望同时达标才算过）")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--audit", action="store_true",
                    help="附成交可执行性审计：卖出那天是不是跌停/一字、买入那天是不是涨停。只读，不改回测行为")
    ap.add_argument("--reject", default="",                    help="样本外风险分 parquet（train_risk_model.py --oof-out），给了就多跑一臂「有闸门」")
    ap.add_argument("--reject-thr", default="",
                    help="剔除阈值（分），留空用 risk_ex10.meta.json 里的 reject_threshold_ex10")
    args = ap.parse_args()
    print(render(scan(args), args))


if __name__ == "__main__":
    main()
