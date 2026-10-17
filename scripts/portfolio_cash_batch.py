"""批次 E 跑数：基准（reserve 0）+ 6 格现金调度，账户层六道门槛同屏判定。

预注册真源在 `stock_selector_api/README.md` §6「组合层批次 E」。本脚本只做那件事：
判据、出场、撮合全部不动，唯一变量是「今天能动用多少现金」。

三条实现口径按预注册写死，别在这里改：
- E1 基准 = `cash_policy=None` 的现引擎行为，参数逐条取 `backtest_main.py` 默认；
- E4 格子 = `reserve {10,20,30}% × tranches {2,3}`，跑前定死，台账记 6 次；
- E5 判定用账户层六道门槛，逐笔盈亏只展示不判过（那是单票 α 的尺子）。

七趟都读同一份信号矩阵（`scripts/portfolio_signal_matrix.py` 预计算并已做 200 点等价校验），
所以基准与实验格之间不存在「取数不同源」这条退路。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.portfolio_signal_matrix import (                     # noqa: E402
    DATE_FROM, DATE_TO, MATRIX_FILE, OUT_DIR, STRATEGY_NAME, CachedKernelStrategy,
    MatrixProvider, load_hit_names,
)
from stock_selector.backtest.cash_policy import CashPolicy, crash_dates   # noqa: E402
from stock_selector.backtest.engine import BacktestEngine                # noqa: E402
from stock_selector.core.selector import create_strategy                 # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
LOG_DIR = ROOT / "logs" / "portfolio"
TRIAL_LEDGER = ROOT / "logs" / "profit_scan" / "trials.jsonl"

# E1 对照基准：scripts/backtest_main.py 的现有默认，一格参数都不为实验让路
CAPITAL = 100_000.0
MAX_POSITIONS = 5
POSITION_CAP_PCT = 0.2
TARGET_AMOUNT = 20_000.0
MAX_HOLD_DAYS = 10
STOP_LOSS_PCT = 0.05
TAKE_PROFIT_PCT = 0.0

# E4 格子（跑之前定死，不许按读数增删）
RESERVES = (0.10, 0.20, 0.30)
TRANCHES = (2, 3)
CELLS = [(r, t) for r in RESERVES for t in TRANCHES]

# 批次标签与台账累计：E（仪器有缺陷，已判失效）= 129,892 → 129,898；E2（修复后重测）续到 129,904
BATCH = "E2"
LEDGER_EXPECT_BEFORE = 129_898

# E5 六道门槛（单位：百分点）
DD_IMPROVE_PP = 2.0
ANNUAL_TOLERANCE_PP = 2.0
TRADE_RATIO_MIN = 0.80
YEARLY_WORSEN_PP = 3.0
# E6 机制前提：实际放出子弹并且真的买进去的交易日数
MECHANISM_MIN_DAYS = 10
# E2-2 前置校验：超限拒单占买单数的上限（整手/手续费边界残差，不是停摆）
CAP_BLOCKED_RATIO_MAX = 0.01

logger = logging.getLogger("cash_batch")


def _calmar(annual: float, max_dd: float) -> float:
    """年化 ÷ |最大回撤|；回撤为 0 时不给数（用 inf 会把「没回撤」和「没数据」混成一个值）。"""
    return float("nan") if abs(max_dd) < 1e-12 else annual / abs(max_dd)


def _yearly_drawdown(equity: list[dict]) -> dict[str, float]:
    """年内最大回撤：每年 1 月把峰重置为该年第一天的净值，跨年的水下不算进新年。

    E5 ⑥ 要的是「这一年自己跌了多少」，用全期峰值会让 2018 借 2017 的高点，读不出逐年恶化。
    """
    out: dict[str, float] = {}
    by_year: dict[str, list[float]] = {}
    for point in equity:
        by_year.setdefault(point["date"][:4], []).append(float(point["total_value"]))
    for year, values in sorted(by_year.items()):
        peak, worst = values[0], 0.0
        for v in values:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1.0)
        out[year] = worst
    return out


def _ratios(equity: list[dict]) -> tuple[float, float]:
    """(平均投入率, 平均持仓占比)。前者是预注册口径 `1 − mean(cash/total_value)`。

    两个都报：投入率把 T+1 在途的 settling 算成「已投出去」，持仓占比才算真仓位有多重——
    读者要靠这两列看出「回撤改善里有多少只是少投了钱」。
    """
    total = np.array([p["total_value"] for p in equity], dtype=float)
    cash = np.array([p["cash"] for p in equity], dtype=float)
    held = np.array([p["positions_value"] for p in equity], dtype=float)
    ok = total > 0
    return (1.0 - float(np.mean(cash[ok] / total[ok])), float(np.mean(held[ok] / total[ok])))


def run_cell(reserve_pct: float, tranches: int, provider: MatrixProvider,
             hit_names: dict) -> dict:
    """跑一格，返回判定要用的全部读数（含逐日净值，机制读数要从这里取次日价）。"""
    policy = None if reserve_pct <= 0 else CashPolicy.build(reserve_pct, tranches)
    engine = BacktestEngine(
        CachedKernelStrategy(
            strategy=create_strategy(STRATEGY_NAME),
            provider=provider,
            hit_names=hit_names,
            target_amount=TARGET_AMOUNT,
            max_hold_days=MAX_HOLD_DAYS,
            stop_loss_pct=STOP_LOSS_PCT,
            take_profit_pct=TAKE_PROFIT_PCT,
        ),
        provider,
        capital=CAPITAL,
        max_positions=MAX_POSITIONS,
        position_cap_pct=POSITION_CAP_PCT,
        strategy_name=f"{STRATEGY_NAME}_reserve{int(reserve_pct * 100)}_t{tranches}",
        params={"reserve_pct": reserve_pct, "tranches": tranches,
                "window": [DATE_FROM, DATE_TO], "capital": CAPITAL,
                "target_amount": TARGET_AMOUNT,
                "exit": [MAX_HOLD_DAYS, STOP_LOSS_PCT, TAKE_PROFIT_PCT]},
        cash_policy=policy,
    )
    started = time.time()
    result = engine.run()
    m = result.metrics
    exec_stats = result.executability
    equity = result.equity
    ratios = _ratios(equity)
    row = {
        "cell": "基准" if reserve_pct <= 0 else f"{int(reserve_pct * 100)}%x{tranches}",
        "reserve_pct": reserve_pct,
        "tranches": tranches,
        "total_return_pp": round(100.0 * m["total_return"], 4),
        "annual_return_pp": None if m["annual_return"] is None
                            else round(100.0 * m["annual_return"], 4),
        "max_drawdown_pp": round(100.0 * m["max_drawdown"], 4),
        "sharpe": round(m["sharpe"], 4),
        "calmar_pp": float("nan"),
        "trade_count": m["trade_count"],
        "payoff_ratio": m["payoff_ratio"],
        "expectancy_yuan": m["expectancy"],
        "win_rate_pp": round(100.0 * m["win_rate"], 4),
        "avg_deployed_ratio": round(ratios[0], 4),
        "avg_position_ratio": round(ratios[1], 4),
        "cash_floor_blocked": exec_stats.get("cash_floor_blocked", 0),
        "cap_blocked": exec_stats.get("cap_blocked", 0),
        "buy_blocked": exec_stats.get("buy_blocked", 0),
        "sell_blocked": exec_stats.get("sell_blocked", 0),
        "seconds": round(time.time() - started, 1),
        "_equity": equity,
        "_trades": result.trades,
        "_policy": policy,
    }
    row["calmar_pp"] = _calmar((m["annual_return"] or 0.0) * 100.0, m["max_drawdown"] * 100.0)
    row["yearly_dd_pp"] = {y: round(100.0 * v, 4) for y, v in _yearly_drawdown(equity).items()}
    return row


def mechanism(cell: dict, baseline: dict, provider: MatrixProvider) -> dict:
    """E6 机制读数：本格在放弹日程里买进的票，它们的**次日**收益，绝对/相对两套口径。

    相对口径减的是「基准组合在同一天的全组合收益」，也就是剥掉当天大盘 β；
    样本只有 30 个触发日，所以这里只判符号与前提，t 值不许当显著性证据（E6 原文）。
    """
    policy = cell["_policy"]
    if policy is None:
        return {}
    dates = provider.dates()
    index = {d: i for i, d in enumerate(dates)}
    base_equity = {p["date"]: float(p["total_value"]) for p in baseline["_equity"]}

    release_days = set(policy.active_days)
    abs_pp: list[float] = []
    rel_pp: list[float] = []
    traded_days: set[str] = set()
    for trade in cell["_trades"]:
        if trade["side"] not in ("buy", "add"):
            continue
        day = trade["date"]
        if day not in release_days:
            continue
        nxt = index.get(day)
        if nxt is None or nxt + 1 >= len(dates):
            continue
        next_day = dates[nxt + 1]
        px_now, px_next = provider.price(day, trade["code"]), provider.price(next_day, trade["code"])
        base_now, base_next = base_equity.get(day), base_equity.get(next_day)
        if px_now is None or px_next is None or not base_now or not base_next:
            continue
        stock_pp = (px_next / px_now - 1.0) * 100.0
        abs_pp.append(stock_pp)
        rel_pp.append(stock_pp - (base_next / base_now - 1.0) * 100.0)
        traded_days.add(day)

    n_days = len(traded_days)
    return {
        "release_days": len(release_days),
        "active_days": n_days,
        "triggered": n_days >= MECHANISM_MIN_DAYS,
        "buy_orders_in_window": len(abs_pp),
        "next_day_abs_pp": round(float(np.mean(abs_pp)), 4) if abs_pp else float("nan"),
        "next_day_rel_pp": round(float(np.mean(rel_pp)), 4) if rel_pp else float("nan"),
        "next_day_median_abs_pp": round(float(np.median(abs_pp)), 4) if abs_pp else float("nan"),
        "next_day_positive_pct": round(100.0 * float(np.mean([x > 0 for x in abs_pp])), 4) if abs_pp else float("nan"),
    }


def judge(cell: dict, baseline: dict, mech: dict) -> dict:
    """E5 六道门槛逐条判 + E6 机制单列（机制不是第 7 道门槛，别混进判定）。"""
    if cell["reserve_pct"] <= 0:
        return {}
    dd_improve = abs(baseline["max_drawdown_pp"]) - abs(cell["max_drawdown_pp"])
    yearly_worst_gap = max(
        abs(cell["yearly_dd_pp"][y]) - abs(baseline["yearly_dd_pp"][y])
        for y in baseline["yearly_dd_pp"] if y in cell["yearly_dd_pp"])
    gates = {
        "①回撤改善≥2.0pp": (dd_improve >= DD_IMPROVE_PP, round(dd_improve, 4)),
        "②Calmar≥基准": (cell["calmar_pp"] >= baseline["calmar_pp"],
                          round(cell["calmar_pp"] - baseline["calmar_pp"], 4)),
        "③Sharpe≥基准": (cell["sharpe"] >= baseline["sharpe"],
                          round(cell["sharpe"] - baseline["sharpe"], 4)),
        "④年化≥基准−2.0pp": (cell["annual_return_pp"] >= baseline["annual_return_pp"] - ANNUAL_TOLERANCE_PP,
                              round(cell["annual_return_pp"] - baseline["annual_return_pp"], 4)),
        "⑤成交笔数≥基准80%": (cell["trade_count"] >= TRADE_RATIO_MIN * baseline["trade_count"],
                                round(cell["trade_count"] / baseline["trade_count"], 4)),
        "⑥逐年回撤不恶化>3pp": (yearly_worst_gap <= YEARLY_WORSEN_PP, round(yearly_worst_gap, 4)),
    }
    n_pass = sum(1 for ok, _ in gates.values() if ok)
    both_negative = cell["annual_return_pp"] < 0 and baseline["annual_return_pp"] < 0
    return {
        "gates": gates,
        "gates_passed": f"{n_pass}/6",
        "verdict": "过" if n_pass == 6 else "不过",
        # E2-4 的读法注释：年化同为负时，Calmar 只是「负得少一点」，没有区分力
        "calmar_caveat": (f"基准 {baseline['annual_return_pp']}% 与本格 {cell['annual_return_pp']}% "
                          f"年化都为负 ⇒ ②Calmar 只作辅助参考，须与年化、绝对回撤一起人工读"
                          if both_negative else ""),
        "mechanism": mech,
        "e8_note": ("E5 全过但机制未触发 ⇒ 按 E8 ① 判「少投资省钱」，不得上线"
                    if n_pass == 6 and not mech.get("triggered") else ""),
    }


def record_trial(n_cells: int, cells: list[dict], verdicts: list[dict]) -> None:
    """台账：试次累计必须与逐笔扫描共用一条线（换脚本就清零 = 同一批行情能重试）。"""
    total = 0
    last = None
    if TRIAL_LEDGER.is_file():
        for line in TRIAL_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            total += int(rec.get("combos") or 0)
            if rec.get("trials_total") is not None:
                last = int(rec["trials_total"])
    before = max(last or 0, total)
    if before != LEDGER_EXPECT_BEFORE:
        logger.warning("台账累计试次是 %s，预注册写的是 %s——格子数没变，但请确认中间没插跑别的大规模扫描",
                       f"{before:,}", f"{LEDGER_EXPECT_BEFORE:,}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
           "batch": BATCH, "chain": "portfolio_cash", "combos": n_cells,
           "trials_total": before + n_cells, "dry_run": False,
           "n_pass": sum(1 for v in verdicts if v.get("verdict") == "过"),
           "window": [DATE_FROM, DATE_TO], "strategy": STRATEGY_NAME,
           "cells": [r["cell"] for r in cells],
           "ledger_note": f"预注册 {LEDGER_EXPECT_BEFORE:,} → {LEDGER_EXPECT_BEFORE + n_cells:,}"}
    with TRIAL_LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logger.info("已记试次：%d 格 → 累计 %s", n_cells, f"{before + n_cells:,}")


def _yearly_buy_counts(cell: dict) -> dict[str, int]:
    return {y: sum(1 for t in cell["_trades"] if t["date"][:4] == y and t["side"] in ("buy", "add"))
            for y in sorted({t["date"][:4] for t in cell["_trades"]})}


def check_baseline_trades(baseline: dict, years: list[str]) -> None:
    """E2-2 前置校验：基准每一年都必须在交易，且超限拒单不成规模。

    批次 E 就是栽在这里——基准 2021 全年 0 笔、年内回撤 0.0000，
    于是 ⑥ 对任何还在买的格子都必然判「恶化」，整批读数失效。
    允许的残差：单笔金额是按开盘前那次 `total_present_value` 算的，同日成交几笔后
    手续费会把上限压低几块钱，正好卡在整手边界上的一单会被拒掉（实测 5 年 1 笔）。
    这跟「整年停摆」不是一回事，所以门槛按占买单数的比例给，不按 0 给。
    """
    counts = {y: sum(1 for t in baseline["_trades"]
                     if t["date"][:4] == y and t["side"] in ("buy", "add")) for y in years}
    frozen = sorted(y for y, n in counts.items() if n == 0)
    n_buys = sum(counts.values())
    blocked_ratio = baseline["cap_blocked"] / n_buys if n_buys else float("inf")
    if frozen or blocked_ratio > CAP_BLOCKED_RATIO_MAX:
        raise SystemExit(
            f"前置校验不过：基准逐年买入 {counts}，冻结年 {frozen}，"
            f"cap_blocked={baseline['cap_blocked']}（占买单 {blocked_ratio:.2%}，"
            f"上限 {CAP_BLOCKED_RATIO_MAX:.1%}）。说明单笔金额仍会被单票上限成规模拒掉，先修仪器再跑格子。")
    logger.info("E2-2 前置校验通过：基准逐年买入 %s，cap_blocked=%d（占买单 %.2f%%，是同日多笔成交的"
                "手续费/整手边界残差，非停摆）",
                counts, baseline["cap_blocked"], 100.0 * blocked_ratio)


def print_table(rows: list[dict], verdicts: list[dict]) -> None:
    cols = ["cell", "total_return_pp", "annual_return_pp", "max_drawdown_pp", "calmar_pp",
            "sharpe", "trade_count", "avg_deployed_ratio", "avg_position_ratio",
            "cash_floor_blocked", "cap_blocked"]
    print("\n== E5 账户层读数（基准与六格同屏；判定只看六道门槛，逐笔口径只展示） ==")
    print("   （cap_blocked = 被单票上限拒掉的买单数；批次 E 的病就是被这一列藏住的，"
          "跑完必须是个位数级别的边界残差，不能成规模）")
    table = pd.DataFrame([{c: r[c] for c in cols} for r in rows])
    print(table.to_string(index=False))

    print("\n== E5 六道门槛（每格逐条：过/不过（与基准的差）） ==")
    gate_names = list(verdicts[1]["gates"]) if len(verdicts) > 1 else []
    lines = []
    for row, v in zip(rows, verdicts):
        if not v:
            lines.append({"cell": row["cell"], "gates_passed": "基准", "verdict": "—"})
            continue
        entry = {"cell": row["cell"], "gates_passed": v["gates_passed"], "verdict": v["verdict"]}
        for name in gate_names:
            ok, gap = v["gates"][name]
            entry[name] = ("过" if ok else "不过") + f"({gap:+.3f})"
        lines.append(entry)
    print(pd.DataFrame(lines).to_string(index=False))
    n_caveat = sum(1 for v in verdicts if v.get("calmar_caveat"))
    if n_caveat:
        print(f"   ②Calmar 读法：{n_caveat} 格与基准的年化同为负数 ⇒ ② 只作辅助参考，"
              f"必须与年化、绝对回撤一起人工看（E2-4 的口径注释）")

    print("\n== E6 机制读数（暴跌日放子弹 → 那些票的次日收益；≥10 天才允许读） ==")
    mech_rows = []
    for row, v in zip(rows, verdicts):
        m = v.get("mechanism") or {}
        if not m:
            continue
        mech_rows.append({"cell": row["cell"],
                          "下限放松日": m["release_days"],
                          "放弹且买入的交易日": m["active_days"],
                          "机制前提": "满足" if m["triggered"] else f"不足{MECHANISM_MIN_DAYS}天",
                          "窗内买单": m["buy_orders_in_window"],
                          "次日绝对pp": m["next_day_abs_pp"],
                          "次日超额pp": m["next_day_rel_pp"],
                          "次日为正占比pp": m["next_day_positive_pct"],
                          "止损线": v.get("e8_note", "")})
    print(pd.DataFrame(mech_rows).to_string(index=False) if mech_rows else "（无）")

    print("\n== 逐年年内回撤 pp（2021 无触发日 = 只体现预留拖累的对照年） ==")
    years = sorted({y for r in rows for y in r["yearly_dd_pp"]})
    print(pd.DataFrame([{"cell": r["cell"], **{y: r["yearly_dd_pp"].get(y) for y in years}}
                        for r in rows]).to_string(index=False))

    # E2-4 ⑥ 的读法注释：回撤对比必须并排「那一年还在不在交易」，
    # 否则基准一旦停盘（批次 E 的 2021），任何还在买的格子都会被读成「恶化」——那是伪失败。
    print("\n== 逐年成交笔数（买/卖）：区分「真回撤恶化」与「基准停盘造成的伪失败」 ==")
    act = []
    for r in rows:
        row_counts: dict[str, str] = {}
        for y in years:
            tr = [t for t in r["_trades"] if t["date"][:4] == y]
            row_counts[y] = f"{sum(1 for t in tr if t['side'] in ('buy', 'add'))}" \
                            f"/{sum(1 for t in tr if t['side'] == 'sell')}"
        act.append({"cell": r["cell"], **row_counts})
    print(pd.DataFrame(act).to_string(index=False))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="批次 E：组合层现金调度 7 趟跑数")
    parser.add_argument("--dry-run", action="store_true", help="只列格子与门槛，不跑数、不记台账")
    parser.add_argument("--cells", default="", help="只跑指定格（如 10%x2,30%x3），基准始终跑；调通用")
    parser.add_argument("--out", default=str(LOG_DIR / f"batch_{BATCH.lower()}_readout.json"))
    args = parser.parse_args()

    triggers, _ = crash_dates()
    logger.info("触发口径：mkt_med_pct ≤ -3.0 且 mkt_n ≥ 1500 → 窗内 %d 个触发日",
                len([d for d in triggers if DATE_FROM <= d <= DATE_TO]))

    cells = CELLS
    if args.cells:
        wanted = {c.strip() for c in args.cells.split(",") if c.strip()}
        cells = [(r, t) for (r, t) in CELLS if f"{int(r * 100)}%x{t}" in wanted]
        logger.warning("只跑 %s —— 这不是预注册的 6 格，读数不得用于 E5 判定", wanted)

    if args.dry_run:
        print(json.dumps({"baseline": "基准(=现引擎, reserve 0)", "cells": [f"{int(r * 100)}%x{t}"
                                                        for r, t in cells],
                          "gates": [f"回撤改善≥{DD_IMPROVE_PP}pp", "Calmar≥基准", "Sharpe≥基准",
                                    f"年化≥基准−{ANNUAL_TOLERANCE_PP}pp",
                                    f"成交笔数≥基准{TRADE_RATIO_MIN:.0%}",
                                    f"逐年回撤不恶化>{YEARLY_WORSEN_PP}pp"],
                          "mechanism_min_days": MECHANISM_MIN_DAYS},
                         ensure_ascii=False, indent=2))
        return 0

    provider = MatrixProvider(OUT_DIR / MATRIX_FILE)
    hit_names = load_hit_names(OUT_DIR)
    logger.info("矩阵就绪：%d 天 × %d 只", len(provider.dates()), len(provider.codes()))

    rows: list[dict] = []
    logger.info("跑 基准")
    baseline = run_cell(0.0, 0, provider, hit_names)
    rows.append(baseline)
    check_baseline_trades(baseline, sorted({d[:4] for d in provider.dates()}))
    for reserve, tranches in list(cells):
        logger.info("跑 %s × %d", f"{int(reserve * 100)}%", tranches)
        rows.append(run_cell(reserve, tranches, provider, hit_names))

    baseline = rows[0]
    verdicts = [judge(r, baseline, mechanism(r, baseline, provider)) for r in rows]

    n_pass = sum(1 for v in verdicts[1:] if v.get("verdict") == "过")
    logger.info("6 格里过 %d 格；按 E8：0 过就在 −3%% 触发口径下关掉这条线，不换阈值铺格子", n_pass)
    print_table(rows, verdicts)

    if len(cells) == 6:
        record_trial(len(cells), rows[1:], verdicts[1:])
    else:
        logger.warning("格子数不是 6，本次不记台账（预注册台账只认那一趟）")

    payload = {"window": [DATE_FROM, DATE_TO], "strategy": STRATEGY_NAME,
               "pool_codes": len(provider.codes()), "calendar_days": len(provider.dates()),
               "baseline": {k: v for k, v in baseline.items() if not k.startswith("_")},
               "cells": [{**{k: v for k, v in r.items() if not k.startswith("_")},
                          "judgement": verdicts[i]}
                         for i, r in enumerate(rows) if i],
               "gates": {"dd_improve_pp": DD_IMPROVE_PP, "annual_tolerance_pp": ANNUAL_TOLERANCE_PP,
                         "trade_ratio_min": TRADE_RATIO_MIN, "yearly_worsen_pp": YEARLY_WORSEN_PP,
                         "mechanism_min_days": MECHANISM_MIN_DAYS}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                         default=lambda o: None if isinstance(o, np.floating)
                                         and np.isnan(o) else str(o)), encoding="utf-8")
    logger.info("读数已落盘：%s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
