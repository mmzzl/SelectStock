"""批次 E3 跑数：基准 + 6 实验格 + 3 同档位纯躺平对照格，留出段一次性裁决。

预注册真源在 `stock_selector_api/README.md` §6「批次 E3」。本脚本只做那件事：
判据、出场、撮合全部不动，唯一变量是「今天能动用多少现金」，
而最终裁决不看「跟旧基准比好不好」，只看 **Δ = 实验格 − 同档位躺平格**。

五件事按预注册写死，别在这里改：
- E3-1 窗：默认 `--window holdout`（2022-01-01→2026-09-30，`data/portfolio_holdout/` 的矩阵 +
  `data/actions_holdout/market_state.parquet`）。`--window history` 只给接线冒烟用（E/E2 那套产物），
  它的读数**不用于任何判定**，也**不记台账**——留出段只读一次这条不能从旁边绕过去。
- E3-2 格子 = 6 实验格（`reserve {10,20,30}% × tranches {2,3}`，与 E/E2 完全同一口径）
  + 对照格 C10/C20/C30（`tranches=0`：同样预留，但下限永不放松）。对照格不参与 ①–⑥，只用来算 Δ。
  原预注册只写了 C10/C20，2026-10-17 在正式跑数之前补上 C30（Δ 三档单调性必须有 30% 档的同档对照），
  裁决阈值、Δ 定义、窗、票池一字不动，台账由 8 次改记 9 次。
- E3-3 裁决：20% 那一对里至少一格同时 `Δ年化 ≥ +1.0pp` 且 `Δ回撤改善 ≥ +1.0pp`。
- E3-4 单调性：以 `reserve` 为轴，**用 Δ 序列**（不是原始指标——各档位的躺平基准本身不同，
  原始值会被现金占用率干扰）判「20% 不低于两侧」，×2 与 ×3 两条轴各自成立；
  另加机制列同号 ≥ 5/6。Δ 达标但单调性不过 ⇒ 判偶然命中，不采信。
- E3-5 六道门槛：①–⑥ 原文照抄 E/E2，仍然全过才算账户层合格，但在 E3 里**只是必要条件**。
- E3-6 台账：9 格 = 9 次，129,904 → 129,913；只有 holdout 窗跑满 9 格才记。

十趟都读同一份信号矩阵，基准、实验格、对照格之间不存在「取数不同源」这条退路。
矩阵的窗边界缺陷（窗前历史行被 `searchsorted` 钉到日历第一天）已在 `scripts/portfolio_signal_matrix.py`
的 `calendar_positions()` 修掉，并由 `tests/test_portfolio_signal_matrix.py` 锁住——E3 用的是修好之后重建的矩阵。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.portfolio_signal_matrix import (                     # noqa: E402
    DATE_FROM, DATE_TO, MATRIX_FILE, OUT_DIR, STRATEGY_NAME, CachedKernelStrategy,
    MatrixProvider, load_hit_names,
)
from stock_selector.backtest.cash_policy import (                 # noqa: E402
    MARKET_STATE as DEFAULT_MARKET_STATE, CashPolicy, crash_dates,
)
from stock_selector.backtest.engine import BacktestEngine                # noqa: E402
from stock_selector.core.selector import create_strategy                 # noqa: E402

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


@dataclass(frozen=True)
class Window:
    """一套「矩阵 + 触发日历 + 判据窗」的三件套，必须整组换，不许混搭。"""

    name: str
    date_from: str
    date_to: str
    matrix_dir: Path
    market_state: Path


# E3-1：留出段是正式跑数；历史窗（E/E2 的产物）只用于验接线，读数不作数
HOLDOUT = Window("holdout", "2022-01-01", "2026-09-30",
                 ROOT / "data" / "portfolio_holdout",
                 ROOT / "data" / "actions_holdout" / "market_state.parquet")
HISTORY = Window("history", DATE_FROM, DATE_TO, OUT_DIR, DEFAULT_MARKET_STATE)
WINDOWS = {w.name: w for w in (HOLDOUT, HISTORY)}

# E3-2 格子（跑之前定死，不许按读数增删）
RESERVES = (0.10, 0.20, 0.30)
TRANCHES = (2, 3)
CONTROL_RESERVES = RESERVES            # 每档都要有同档位躺平对照，否则 Δ 单调性判不了（2026-10-17 补 C30）
EXPERIMENTAL = [(r, t) for r in RESERVES for t in TRANCHES]
CELLS = EXPERIMENTAL + [(r, 0) for r in CONTROL_RESERVES]
N_CELLS = len(CELLS)

# 批次标签与台账累计：E=129,892→129,898（仪器失效）、E2=129,898→129,904、
# E3=129,904→129,913（9 格 = 6 实验格 + 3 躺平对照格）
BATCH = "E3"
LEDGER_EXPECT_BEFORE = 129_904

# E5 六道门槛（单位：百分点）
DD_IMPROVE_PP = 2.0
ANNUAL_TOLERANCE_PP = 2.0
TRADE_RATIO_MIN = 0.80
YEARLY_WORSEN_PP = 3.0
# E6 机制前提：实际放出子弹并且真的买进去的交易日数
MECHANISM_MIN_DAYS = 10
# E2-2 前置校验：超限拒单占买单数的上限（整手/手续费边界残差，不是停摆）
CAP_BLOCKED_RATIO_MAX = 0.01
# E3-3 的硬指标阈值（跑数之前写死，读数之后不得回调）
DELTA_PP = 1.0
# E3-4 单调性要求的档位轴与机制同号比例
MONOTONIC_PEAK = 20
MECHANISM_SIGN_MIN = 5

logger = logging.getLogger("cash_batch")


def level(reserve_pct: float) -> int:
    """档位用整数表示：0.1+0.2 之类的浮点误差不能让 20% 配到 C10 上去。"""
    return int(round(100.0 * float(reserve_pct)))


def control_name(reserve_pct: float) -> str:
    return f"C{level(reserve_pct)}"


def cell_name(reserve_pct: float, tranches: int) -> str:
    if reserve_pct <= 0:
        return "基准"
    if tranches <= 0:
        return control_name(reserve_pct)
    return f"{level(reserve_pct)}%x{tranches}"


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
             hit_names: dict, window: Window) -> dict:
    """跑一格，返回判定要用的全部读数（含逐日净值，机制读数要从这里取次日价）。"""
    # tranches=0 就是「下限永不放松」：CashPolicy 的日程循环 range(0) 不进，floor 恒等于 base，
    # active_days 为空 ⇒ 机制列自动不适用。对照格不需要任何新通道代码。
    policy = None if reserve_pct <= 0 else CashPolicy.build(reserve_pct, tranches,
                                                            path=window.market_state)
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
        strategy_name=f"{STRATEGY_NAME}_{cell_name(reserve_pct, tranches)}",
        params={"reserve_pct": reserve_pct, "tranches": tranches,
                "window": [window.date_from, window.date_to], "capital": CAPITAL,
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
        "cell": cell_name(reserve_pct, tranches),
        "reserve_pct": reserve_pct,
        "tranches": tranches,
        "kind": ("baseline" if reserve_pct <= 0 else "control" if tranches <= 0 else "experimental"),
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
    留出段只有 39 个触发日，所以这里只判符号与前提，t 值不许当显著性证据（E6 原文）。
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
    """E5 六道门槛逐条判 + E6 机制单列（机制不是第 7 道门槛，别混进判定）。

    E3-2：对照格（`tranches=0`）**不参与 ①–⑥**，只用来算 Δ；基准也不算。
    """
    if cell["kind"] == "baseline":
        return {}
    if cell["kind"] == "control":
        return {"gates": {}, "gates_passed": "—", "verdict": "对照",
                "control_note": "E3-2：纯躺平对照格，不参与 ①–⑥ 判定，只作 Δ 的分母"}
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


def delta(experimental: dict, control: dict) -> dict:
    """Δ = 实验格 − **同档位**纯躺平对照格（E3-3）。跨档位配对在这里是硬错误。

    两笔都算「相对同一条躺平基准的超额」：年化取差，回撤取「躺平格回撤 − 实验格回撤」
    （正数＝实验格回撤更浅）。跟旧基准的差留给 ①–⑥，那只是必要条件。
    """
    if experimental["kind"] != "experimental":
        raise ValueError(f"{experimental['cell']} 不是实验格，Δ 只能由实验格减对照格")
    if control["kind"] != "control":
        raise ValueError(f"{control['cell']} 不是纯躺平对照格（tranches 必须为 0）")
    if level(experimental["reserve_pct"]) != level(control["reserve_pct"]):
        raise ValueError(f"E3 硬规则：{experimental['cell']} 只能和 "
                         f"{control_name(experimental['reserve_pct'])} 配对，"
                         f"这里配到了 {control['cell']}（跨档位）")
    return {
        "cell": experimental["cell"],
        "control": control["cell"],
        "reserve_level": level(experimental["reserve_pct"]),
        "tranches": experimental["tranches"],
        "annual_pp": experimental["annual_return_pp"],
        "control_annual_pp": control["annual_return_pp"],
        "delta_annual_pp": round(experimental["annual_return_pp"] - control["annual_return_pp"], 4),
        "max_drawdown_pp": experimental["max_drawdown_pp"],
        "control_max_drawdown_pp": control["max_drawdown_pp"],
        "delta_dd_improve_pp": round(abs(control["max_drawdown_pp"])
                                      - abs(experimental["max_drawdown_pp"]), 4),
    }


def pair_deltas(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """按档位找同档对照格。找不到对照的档位（预注册没给 C30）列进 unpaired，不许拿别的档位凑。"""
    controls = {level(r["reserve_pct"]): r for r in rows if r["kind"] == "control"}
    paired: list[dict] = []
    unpaired: list[str] = []
    for row in rows:
        if row["kind"] != "experimental":
            continue
        control = controls.get(level(row["reserve_pct"]))
        if control is None:
            unpaired.append(row["cell"])
            continue
        paired.append(delta(row, control))
    return paired, unpaired


def e33_decision(deltas: list[dict]) -> dict:
    """E3-3：20% 那一对里至少一格同时 Δ年化 ≥ 1.0pp 且 Δ回撤改善 ≥ 1.0pp（都对 C20 比）。"""
    pair_20 = [d for d in deltas if d["reserve_level"] == MONOTONIC_PEAK]
    if not pair_20:
        return {"verdict": "无法判定",
                "reason": f"20% 档没有同档位对照格（{control_name(0.20)} 不在本次读数里）⇒ 按不采信处理",
                "cells": []}
    for d in pair_20:
        d["e33_cell_pass"] = (d["delta_annual_pp"] >= DELTA_PP
                              and d["delta_dd_improve_pp"] >= DELTA_PP)
    hit = [d["cell"] for d in pair_20 if d["e33_cell_pass"]]
    return {
        "verdict": "过" if hit else "不过",
        "threshold_pp": DELTA_PP,
        "passed_cells": hit,
        "reason": (f"{'、'.join(hit)} 相对 {pair_20[0]['control']} 同时做到 "
                   f"Δ年化≥+{DELTA_PP}pp 且 Δ回撤改善≥+{DELTA_PP}pp"
                   if hit else
                   f"20% 那两格相对 {pair_20[0]['control']} 没有一格同时过 Δ年化≥+{DELTA_PP}pp "
                   f"与 Δ回撤改善≥+{DELTA_PP}pp ⇒ 账户层的改善读不出「暴跌择时」的额外贡献"),
    }


def e34_decision(deltas: list[dict], unpaired: list[str], verdicts: list[dict]) -> dict:
    """E3-4：Δ 序列的单调性（20% 为峰，两条 tranches 轴各自成立）+ 机制列同号 ≥ 5/6。

    这里**只用 Δ**：三个档位的躺平基准各不相同，实验格的原始年化/原始回撤会被「少投了多少钱」
    带着走，直接拿原始值排大小等于把 E3-3 想排除的那个混淆又请回来。
    """
    axes: dict[str, dict] = {}
    all_ok = True
    for t in TRANCHES:
        series = {d["reserve_level"]: d for d in deltas if d["tranches"] == t}
        if any(lvl not in series for lvl in (10, MONOTONIC_PEAK, 30)):
            axes[f"tranches={t}"] = {"status": "无法判定",
                                     "reason": f"Δ 序列缺档位（无同档对照：{', '.join(unpaired) or '—'}）"}
            all_ok = False
            continue
        checks = {}
        for metric, label in (("delta_annual_pp", "Δ年化"), ("delta_dd_improve_pp", "Δ回撤改善")):
            peak, low, high = series[MONOTONIC_PEAK][metric], series[10][metric], series[30][metric]
            checks[label] = {"10%": low, "20%": peak, "30%": high,
                             "ok": peak >= low and peak >= high}
            all_ok &= bool(checks[label]["ok"])
        axes[f"tranches={t}"] = {"status": "过" if all(c["ok"] for c in checks.values()) else "不过",
                                 "checks": checks}
    signs = [v["mechanism"]["next_day_rel_pp"] for v in verdicts
             if v.get("mechanism") and not np.isnan(v["mechanism"]["next_day_rel_pp"])]
    pos = sum(1 for s in signs if s > 0)
    neg = sum(1 for s in signs if s < 0)
    mechanism_ok = max(pos, neg) >= MECHANISM_SIGN_MIN
    all_ok &= mechanism_ok
    return {
        "verdict": "过" if all_ok else "不过",
        "axes": axes,
        "mechanism": {"n_cells": len(signs), "positive": pos, "negative": neg,
                      "required": f"同号≥{MECHANISM_SIGN_MIN}/{len(EXPERIMENTAL)}",
                      "ok": mechanism_ok},
        "unpaired_cells": unpaired,
        "note": "违反单调性 ⇒ 即使 E3-3 的 Δ 过线也判偶然命中，不采信、不铺开、不进卖出链设计（E3-4 原文）",
    }


def final_decision(e33: dict, e34: dict, n_gates_pass: int) -> dict:
    """三条并判：E3-3（Δ 达标）+ E3-4（单调性）是充分性，E3-5 六门槛只是必要条件。"""
    if e33["verdict"] != "过":
        verdict, reason = "不采信", f"E3-3 不过：{e33['reason']}"
    elif e34["verdict"] != "过":
        verdict, reason = "不采信", f"E3-3 过线但 E3-4 单调性不过（{e34['verdict']}）⇒ 判偶然命中"
    else:
        verdict, reason = "采信", "E3-3 与 E3-4 同时成立"
    return {"verdict": verdict, "reason": reason,
            "e33": e33["verdict"], "e34": e34["verdict"],
            "e35_gates_passed_cells": n_gates_pass,
            "e8_action": ("按 E8 关掉组合层现金调度这条线，不回头加格子" if verdict == "不采信"
                          else "才允许另开一条「这条机制怎么落到操作面」的新预注册（E3 本身不做设计）")}


def ledger_record(n_cells: int, rows: list[dict], verdicts: list[dict], before: int,
                  window: Window) -> dict:
    """台账记录：`n_pass` 必须是「六道门槛判定为过」的格数（E3-7 的那笔债，单测锁住）。"""
    return {"batch": BATCH, "chain": "portfolio_cash", "combos": n_cells,
            "trials_total": before + n_cells, "dry_run": False,
            "n_pass": sum(1 for v in verdicts if v.get("verdict") == "过"),
            "window": [window.date_from, window.date_to], "strategy": STRATEGY_NAME,
            "cells": [r["cell"] for r in rows],
            "ledger_note": f"预注册 {LEDGER_EXPECT_BEFORE:,} → {LEDGER_EXPECT_BEFORE + n_cells:,}"}


def ledger_before() -> int:
    """台账累计试次：与逐笔扫描共用一条线（换脚本就清零 = 同一批行情能重试）。"""
    total, last = 0, None
    if TRIAL_LEDGER.is_file():
        for line in TRIAL_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            total += int(rec.get("combos") or 0)
            if rec.get("trials_total") is not None:
                last = int(rec["trials_total"])
    return max(last or 0, total)


def record_trial(rows: list[dict], verdicts: list[dict], window: Window) -> None:
    before = ledger_before()
    if before != LEDGER_EXPECT_BEFORE:
        logger.warning("台账累计试次是 %s，预注册写的是 %s——格子数没变，但请确认中间没插跑别的大规模扫描",
                       f"{before:,}", f"{LEDGER_EXPECT_BEFORE:,}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
           **ledger_record(len(rows), rows, verdicts, before, window)}
    with TRIAL_LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logger.info("已记试次：%d 格 → 累计 %s", len(rows), f"{rec['trials_total']:,}")


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


def print_raw_layer(rows: list[dict], verdicts: list[dict]) -> None:
    """第一层：所有格子的**原始**账户读数（含对照格），Δ 一个都不出现在这一层里。"""
    cols = ["cell", "kind", "total_return_pp", "annual_return_pp", "max_drawdown_pp", "calmar_pp",
            "sharpe", "trade_count", "avg_deployed_ratio", "avg_position_ratio",
            "cash_floor_blocked", "cap_blocked"]
    print("\n== 第一层：原始账户读数（基准/实验格/对照格同屏；逐笔口径只展示） ==")
    print("   （cap_blocked = 被单票上限拒掉的买单数；跑完必须是个位数级别的边界残差，不能成规模）")
    print(pd.DataFrame([{c: r[c] for c in cols} for r in rows]).to_string(index=False))

    print("\n== E3-5 六道门槛（实验格逐条判；对照格按 E3-2 不参与判定） ==")
    gate_names = next((list(v["gates"]) for v in verdicts if v.get("gates")), [])
    lines = []
    for row, v in zip(rows, verdicts):
        if row["kind"] == "baseline":
            lines.append({"cell": row["cell"], "gates_passed": "基准", "verdict": "—",
                          **{name: "—" for name in gate_names}})
        elif not v.get("gates"):
            lines.append({"cell": row["cell"], "gates_passed": "—",
                          "verdict": v.get("verdict", "对照"),
                          **{name: "不参与" for name in gate_names}})
        else:
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
    print("   E3-5：六道门槛全过只是「账户层合格」的必要条件，充分性看第二层的 E3-3 + E3-4")

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

    print("\n== 逐年年内回撤 pp ==")
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


def print_delta_layer(deltas: list[dict], unpaired: list[str], e33: dict, e34: dict,
                      final: dict, rows: list[dict]) -> None:
    """第二层：只有 Δ 与三条裁决。原始指标不回这张表里混。"""
    print("\n== 第二层：Δ = 实验格 − 同档位纯躺平对照格（E3-3 的唯一硬指标） ==")
    if not deltas:
        print("（没有任何一格配到同档位对照格 ⇒ 无法裁决）")
    else:
        cols = ["cell", "control", "tranches", "annual_pp", "control_annual_pp", "delta_annual_pp",
                "max_drawdown_pp", "control_max_drawdown_pp", "delta_dd_improve_pp"]
        table = pd.DataFrame([{c: d[c] for c in cols} for d in deltas])
        table["E3-3单格"] = [d.get("e33_cell_pass", False) for d in deltas] if e33["verdict"] != "无法判定" else "—"
        print(table.to_string(index=False))
        print("   （Δ年化>0 = 比同档位躺平多赚；Δ回撤改善>0 = 比同档位躺平回撤更浅）")
    if unpaired:
        print(f"   无同档位对照、只有 ①–⑥ 的格子：{', '.join(unpaired)}"
              f"（每档都必须有同档位躺平对照；跨档位配对是硬错误，这里不凑）")

    print("\n== E3-4 单调性（用 Δ 序列判，不用原始指标） ==")
    for axis, detail in e34["axes"].items():
        print(f"   {axis}：{detail['status']}"
              + (f" —— {detail.get('reason', '')}" if detail["status"] != "过" else ""))
        for label, check in (detail.get("checks") or {}).items():
            print(f"      {label}: 10%={check['10%']:+.3f} 20%={check['20%']:+.3f} "
                  f"30%={check['30%']:+.3f} ⇒ {'20% 为峰' if check['ok'] else '20% 不是峰'}")
    m = e34["mechanism"]
    print(f"   机制列同号：{m['n_cells']} 格里 正{m['positive']}/负{m['negative']}，"
          f"要求{m['required']} ⇒ {'成立' if m['ok'] else '不成立'}")

    print("\n== 最终裁决（E3-3 充分性 + E3-4 单调性；E3-5 六门槛只是必要条件） ==")
    print(f"   E3-3：{e33['verdict']} —— {e33['reason']}")
    print(f"   E3-4：{e34['verdict']}")
    print(f"   E3-5：六道门槛全过的实验格 {final['e35_gates_passed_cells']}/6")
    print(f"   ⇒ 裁决：**{final['verdict']}**（{final['reason']}）")
    print(f"   ⇒ 后续：{final['e8_action']}")
    print(f"   说明：{e34['note']}")

    if rows:
        n_exp = sum(1 for r in rows if r["kind"] == "experimental")
        print(f"\n   读法提醒：本次共 {len(rows)} 趟（基准 + 实验格 {n_exp} 个 + 对照格 "
              f"{sum(1 for r in rows if r['kind'] == 'control')} 个）；"
              f"2022+ 留出段只读这一次，读完当场结案（E3-6）")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="批次 E3：组合层现金调度 10 趟跑数（基准 + 9 格）")
    parser.add_argument("--window", choices=sorted(WINDOWS), default=HOLDOUT.name,
                        help=f"{HOLDOUT.name}=留出段正式跑数（默认）；"
                             f"{HISTORY.name}=只验接线，读数不作数、不记台账")
    parser.add_argument("--dry-run", action="store_true", help="只列格子与门槛，不跑数、不记台账")
    parser.add_argument("--cells", default="", help="只跑指定格（如 10%x2,C20），基准始终跑；调通用")
    parser.add_argument("--no-ledger", action="store_true", help="跑满 9 格也不记台账（冒烟专用）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    window = WINDOWS[args.window]
    out = Path(args.out or LOG_DIR / f"batch_{BATCH.lower()}_{'holdout' if window is HOLDOUT else 'smoke'}_readout.json")

    triggers, _ = crash_dates(window.market_state)
    in_window = [d for d in triggers if window.date_from <= d <= window.date_to]
    logger.info("触发口径：mkt_med_pct ≤ -3.0 且 mkt_n ≥ 1500 → %s 窗内 %d 个触发日",
                window.name, len(in_window))
    if window is HOLDOUT and len(in_window) < MECHANISM_MIN_DAYS:
        raise SystemExit(f"E3-1 前置不过：留出段窗内只有 {len(in_window)} 个触发日"
                         f"（< {MECHANISM_MIN_DAYS}），机制读不出来，不许开跑")

    cells = CELLS
    if args.cells:
        wanted = {c.strip() for c in args.cells.split(",") if c.strip()}
        cells = [(r, t) for (r, t) in CELLS if cell_name(r, t) in wanted]
        logger.warning("只跑 %s —— 这不是预注册的 9 格，读数不得用于 E3 判定", sorted(wanted))

    if args.dry_run:
        print(json.dumps({"window": [window.date_from, window.date_to], "window_name": window.name,
                          "baseline": "基准(=现引擎, reserve 0)",
                          "cells": [cell_name(r, t) for r, t in cells],
                          "gates": [f"回撤改善≥{DD_IMPROVE_PP}pp", "Calmar≥基准", "Sharpe≥基准",
                                    f"年化≥基准−{ANNUAL_TOLERANCE_PP}pp",
                                    f"成交笔数≥基准{TRADE_RATIO_MIN:.0%}",
                                    f"逐年回撤不恶化>{YEARLY_WORSEN_PP}pp"],
                          "e33": {"pair": "20% 档 vs C20", "delta_pp": DELTA_PP},
                          "e34": {"axis": "Δ 序列（非原始指标）", "peak": "20%",
                                  "mechanism_sign_min": MECHANISM_SIGN_MIN},
                          "mechanism_min_days": MECHANISM_MIN_DAYS,
                          "ledger": f"{LEDGER_EXPECT_BEFORE:,} → {LEDGER_EXPECT_BEFORE + N_CELLS:,}"},
                         ensure_ascii=False, indent=2))
        return 0

    provider = MatrixProvider(window.matrix_dir / MATRIX_FILE)
    hit_names = load_hit_names(window.matrix_dir)
    logger.info("矩阵就绪：%d 天 × %d 只（%s）", len(provider.dates()), len(provider.codes()),
                window.matrix_dir)

    rows: list[dict] = []
    logger.info("跑 基准")
    baseline = run_cell(0.0, 0, provider, hit_names, window)
    rows.append(baseline)
    check_baseline_trades(baseline, sorted({d[:4] for d in provider.dates()}))
    for reserve, tranches in cells:
        logger.info("跑 %s（tranches=%d）", cell_name(reserve, tranches), tranches)
        rows.append(run_cell(reserve, tranches, provider, hit_names, window))
        # 边跑边落，留出段只读一次：中途机器出事不许从第一格重来
        _dump(rows, window, out, None, None)

    verdicts = [judge(r, baseline, mechanism(r, baseline, provider)) for r in rows]
    deltas, unpaired = pair_deltas(rows)
    e33 = e33_decision(deltas)
    e34 = e34_decision(deltas, unpaired, verdicts)
    n_gates_pass = sum(1 for v in verdicts if v.get("verdict") == "过")
    final = final_decision(e33, e34, n_gates_pass)

    logger.info("E3-3 %s / E3-4 %s / 六门槛全过 %d 格 ⇒ 裁决 %s",
                e33["verdict"], e34["verdict"], n_gates_pass, final["verdict"])
    print_raw_layer(rows, verdicts)
    print_delta_layer(deltas, unpaired, e33, e34, final, rows)
    _dump(rows, window, out, verdicts, (deltas, unpaired, e33, e34, final))

    experimental_rows = [r for r in rows if r["kind"] == "experimental"]
    experimental_verdicts = [v for r, v in zip(rows, verdicts) if r["kind"] == "experimental"]
    control_rows = [r for r in rows if r["kind"] == "control"]
    if window is not HOLDOUT:
        logger.warning("%s 窗只用于验接线：本次不记台账，读数不得用于 E3 判定", window.name)
    elif len(experimental_rows) == len(EXPERIMENTAL) and len(control_rows) == len(CONTROL_RESERVES):
        if args.no_ledger:
            logger.warning("--no-ledger：跑满了 9 格但没记台账")
        else:
            record_trial(experimental_rows + control_rows, experimental_verdicts, window)
    else:
        logger.warning("格子数不是预注册的 9（实验 %d + 对照 %d），本次不记台账",
                       len(experimental_rows), len(control_rows))
    return 0


def _dump(rows: list[dict], window: Window, out: Path, verdicts: list[dict] | None,
          decision: tuple | None) -> None:
    """读数落盘。跑数途中也调一次（verdicts/decision 还没算出来时只存原始层）。"""
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    baseline = clean[0]
    payload = {"batch": BATCH, "window": [window.date_from, window.date_to],
               "window_name": window.name,
               "matrix_dir": str(window.matrix_dir),
               "market_state": str(window.market_state),
               "strategy": STRATEGY_NAME,
               "cells": clean,
               "gates": {"dd_improve_pp": DD_IMPROVE_PP, "annual_tolerance_pp": ANNUAL_TOLERANCE_PP,
                         "trade_ratio_min": TRADE_RATIO_MIN, "yearly_worsen_pp": YEARLY_WORSEN_PP,
                         "mechanism_min_days": MECHANISM_MIN_DAYS},
               "delta_threshold_pp": DELTA_PP,
               "pairs": {r["cell"]: (control_name(r["reserve_pct"])
                                     if r["kind"] == "experimental" else None) for r in clean}}
    if verdicts is not None:
        payload["judgements"] = {r["cell"]: v for r, v in zip(rows, verdicts) if r["kind"] != "baseline"}
        payload["baseline"] = baseline
    if decision is not None:
        deltas, unpaired, e33, e34, final = decision
        payload["layer2_delta"] = deltas
        payload["layer2_unpaired"] = unpaired
        payload["e33"] = e33
        payload["e34"] = e34
        payload["final"] = final
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              default=lambda o: None if isinstance(o, np.floating)
                              and np.isnan(o) else str(o)), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
