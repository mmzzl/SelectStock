"""shadow 台账：灰度期间把「闸门剔 / 不剔」与「收盘假设 / 次开盘」四格并排记账。

**为什么要有这张台账**：闸门 `adx14≥90` 的准入依据全部来自回测，而回测有两个已知偏差——
撮合按**信号日收盘**成交（实盘最早次日开盘能成交），横截面分母是回测股票池
（已改成全市场，见 `risk/cross_section.py`）。准入时决定保留胜率/赔率/利润因子
作为**熔断指标**而不是前置条件，正是因为它们只能靠实盘样本回答。所以台账要同时记：

- **两臂**：闸门放行（pass）与剔除（reject）——同一批信号、同一天、同一个分母，
  差的就是那一道闸门。这是 shadow 期唯一有意义的对照。
- **两种成交假设**：从信号日收盘算的 `ret_close`，与从**次一交易日开盘**算的 `ret_open`。
  两者之差就是这个策略在实盘里真实要多付（或少付）的那一段，回测页上永远看不到它。

出场按**收盘价**重演机械规则（止损 5%、无止盈、持有 10 日），与回测引擎同口径
（`KernelStrategy.evaluate_holdings` 判的也是收盘价），所以两臂的单笔期望可以直接
与 README《结论卡：adx14 高分位闸门》那九格对照。次开盘那一臂只把**入场价**换成
次日开盘，出场判定不变——这样两臂之差纯粹是入场口径的差，不掺出场规则的差。

熔断指标（`--trip-*` 可调，默认按回测实测的最差格留出余量）：
    笔数 ≥ 60、胜率 ≥ 35%、赔率 ≥ 1.5、利润因子 ≥ 1.0、
    次开盘相对收盘的平均劣化 ≤ 0.5%（成交假设没有崩）、
    放行臂单笔期望 > 0 且 d期望（放行−剔除）> 0（闸门确实有用）
任一条不满足 → `verdict=trip`，影子期结束、闸门不许切 live。

用法:
    # 影子期每天收盘后跑一次（append，不覆盖历史）
    python3 scripts/build_shadow_ledger.py --days 40
    # 换个观察窗看结论（--report 只读盘、不写文件）
    python3 scripts/build_shadow_ledger.py --days 20 --report
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

from stock_selector.backtest.provider import ParquetProvider            # noqa: E402
from stock_selector.core.selector import create_strategy                # noqa: E402
from stock_selector.risk import cross_section as xs                     # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
OUT_DIR = ROOT / "data" / "shadow_ledger"
DETAIL = OUT_DIR / "shadow.parquet"
SUMMARY = OUT_DIR / "shadow_summary.json"

HOLD = 10          # 持有交易日（现役出场参数）
STOP = 0.05        # 止损（现役出场参数）
TAKE = 0.0         # 无止盈（现役出场参数）
FACTOR = "adx14"
MIN_PCT = 90.0     # 与回测用的阈值一致：当日全市场前 10%
TRIP = {"min_trades": 60, "min_win_rate": 0.35, "min_payoff": 1.5,
        "min_profit_factor": 1.0, "max_open_drag": 0.005}

logger = logging.getLogger("shadow_ledger")


def _exit_from(entry: np.ndarray, closes: np.ndarray, stop: float, take: float) -> np.ndarray:
    """给定入场价与后续收盘序列，逐条重演机械出场，返回收益率。

    顺序与引擎一致：每个持有日先看止损、再看止盈，都不触发就持有到期按当日收盘出。
    触发的那天**按当天收盘价成交**——引擎就是这么记的；改成次日开盘会把这张表
    和回测页的数字混成两个口径，而 shadow 要比的正是"入场口径"这一个变量。
    """
    n, h = closes.shape
    pnl = np.full(n, np.nan)
    active = np.ones(n, dtype=bool)
    for d in range(h):
        x = (closes[:, d] / entry) - 1.0
        alive = active & np.isfinite(x)
        stop_hit = alive & (x <= -stop)
        take_hit = alive & (take > 0) & (x >= take)
        pnl[stop_hit] = x[stop_hit]
        pnl[take_hit] = x[take_hit]
        active &= ~(stop_hit | take_hit)
    finite = np.isfinite(closes)
    any_finite = finite.any(axis=1)
    last_idx = h - 1 - np.argmax(finite[:, ::-1], axis=1)
    tail = closes[np.arange(n), np.where(any_finite, last_idx, 0)] / entry - 1.0
    pnl = np.where(active, np.where(any_finite, tail, np.nan), pnl)
    return pnl


def _load(provider: ParquetProvider, codes: list[str], strategy, lookback: int,
          factor: str, end: str) -> pd.DataFrame:
    """取一段面板：判据要用的列 + 闸门那一列 + open/close（两种成交假设都要用）。"""
    cols = list(dict.fromkeys([*(strategy.required_columns() or []),
                               "date", "code", "open", "close", "volume", factor]))
    parts = []
    for code in codes:
        path = Path(KLINE_DIR) / f"{code}.parquet"
        if not path.is_file():
            continue
        # 走 read_parquet(columns=…) 而不是 provider._load：后者会把 82 列整表常驻缓存
        # （全市场 6631 只 ≈ 3 GB），而这里只需要十列，且分位与命中都只要最后一小段。
        try:
            df = pd.read_parquet(path, columns=cols)
        except Exception as exc:
            logger.warning("跳过 %s：%s", code, exc)
            continue
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df["code"] = code
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    panel = pd.concat(parts, ignore_index=True)
    panel = panel[panel["date"].astype(str) <= end].reset_index(drop=True)
    keep = [c for c in cols if c in panel.columns]
    return panel[keep]


def collect_rows(panel: pd.DataFrame, strategy, lookback: int, min_pct: float,
                 factor: str, codes: list[str]) -> pd.DataFrame:
    """逐日逐股找命中，并给每条命中挂上：当日全市场分位、闸门判定、两种成交假设的收益。

    分位按**当日全市场**算（`xs.daily_percentile`），与实盘 `RiskContext.cross_pct`
    同一个函数、同一份列、同一套并列处理——shadow 与实盘若用两套分位口径，
    这张台账就不能拿来核验实盘。
    """
    records: list[dict] = []
    # 先取每日分母：闸门看的是"排在那批票里的第几成"，只排命中是不够的
    pct_by_day: dict[str, dict[str, float]] = {}
    for date, grp in panel.groupby(panel["date"].astype(str)):
        if factor not in grp.columns:
            continue
        values = {}
        for code, sub in grp.groupby("code", sort=False):
            value = sub[factor].iloc[-1]
            try:
                values[str(code)] = float(value)
            except (TypeError, ValueError):
                continue
        pct_by_day[date] = xs.daily_percentile(values, factor)
    sizes = [len(v) for v in pct_by_day.values()]
    logger.info("分母：%d 个交易日，每日 %d~%d 只（不足 %d 只的那天整层不判）",
                len(sizes), min(sizes) if sizes else 0, max(sizes) if sizes else 0,
                xs.MIN_CROSS_SECTION)

    for code, raw in panel.groupby("code", sort=False):
        df = raw.sort_values("date").reset_index(drop=True)
        dates = df["date"].astype(str).to_numpy()
        close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
        open_ = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
        hit_dates = []
        for p in range(lookback, len(df) - HOLD - 1):
            if strategy.signal_at(df.iloc[p - lookback + 1: p + 1]) is None:
                continue
            hit_dates.append(p)
        if not hit_dates:
            continue
        for p in hit_dates:
            day = str(dates[p])
            pct = (pct_by_day.get(day) or {}).get(str(code))
            if pct is None or not np.isfinite(pct) or len(pct_by_day.get(day, {})) < xs.MIN_CROSS_SECTION:
                gate = -1                      # -1=不判（分位取不到），与 0=剔除、1=放行区分开
            else:
                gate = 1 if pct >= min_pct else 0
            entry_close = close[p]
            entry_open = open_[p + 1]
            if not np.isfinite(entry_close) or entry_close <= 0:
                continue
            fwd = close[p + 1: p + 1 + HOLD]
            rec = {
                "date": day, "code": str(code), "strategy": getattr(strategy, "signal", ""),
                "gate": gate, "pct": pct, "entry_close": entry_close,
                "fwd10_close_pct": (close[p + HOLD] / entry_close - 1.0) * 100
                if np.isfinite(close[p + HOLD]) else np.nan,
            }
            for label, entry in (("close", entry_close), ("open", entry_open)):
                if not np.isfinite(entry) or entry <= 0:
                    rec[f"ret_{label}_pct"] = np.nan
                    continue
                rec[f"ret_{label}_pct"] = float(_exit_from(
                    np.array([entry]), fwd.reshape(1, -1), STOP, TAKE)[0]) * 100.0
            records.append(rec)
    return pd.DataFrame(records)


def summarize(detail: pd.DataFrame, trip: dict) -> dict:
    """两臂 × 两种成交假设的指标，以及熔断判定。

    指标口径与回测一致：胜率/赔率/利润因子/单笔期望都是**点级**（逐笔），因为影子期
    没有「同一天几十条命中」那种扎堆问题（实盘每天最多买 5 只）；但仍给日等权那一列，
    免得某天命中扎堆把点级抬起来——两列同号才算稳。
    """
    out: dict = {"trip_thresholds": trip, "arms": {}, "checks": []}

    def arm_stats(sub: pd.DataFrame) -> dict:
        if sub.empty:
            return {"n": 0}
        row = {"n": int(len(sub)), "days": int(sub["date"].nunique())}
        for label in ("close", "open"):
            v = sub[f"ret_{label}_pct"].dropna().to_numpy(dtype=float)
            if v.size == 0:
                row[label] = {"n": 0}
                continue
            wins, losses = v[v > 0], v[v < 0]
            both = bool(wins.size and losses.size)     # 只有一边有样本时赔率/利润因子**估不出来**，
            #   写成 0 或 nan 都会被读成"很差"，所以留 None，由调用方印成「-」
            row[label] = {
                "n": int(v.size),
                "win_rate": round(float((v > 0).mean()), 4),
                "payoff": round(float(wins.mean() / abs(losses.mean())), 4) if both else None,
                "profit_factor": round(float(wins.sum() / abs(losses.sum())), 4) if both else None,
                "expectancy_pp": round(float(v.mean()), 4),
                "median_pp": round(float(np.median(v)), 4),
                "p5_pp": round(float(np.percentile(v, 5)), 4),
            }
        daily = sub.dropna(subset=["ret_close_pct"]).groupby("date")["ret_close_pct"].mean()
        row["daymean_exp_pp"] = round(float(daily.mean()), 4) if len(daily) else None
        return row

    passed = detail[detail["gate"] == 1]
    rejected = detail[detail["gate"] == 0]
    out["arms"] = {"pass": arm_stats(passed), "reject": arm_stats(rejected),
                   "all": arm_stats(detail[detail["gate"] >= 0])}
    pc = out["arms"]["pass"].get("close", {})
    rc = out["arms"]["reject"].get("close", {})
    po = out["arms"]["pass"].get("open", {})
    drag = None
    if pc.get("n") and po.get("n"):
        drag = round(pc["expectancy_pp"] - po["expectancy_pp"], 4)
    # 放行臂没有样本时，"d期望 > 0"会因为 0 − (负数) 而假通过：那一格没有结论，不是好结论
    enough_pass = bool(pc.get("n")) and bool(rc.get("n"))
    out["checks"] = [
        ("笔数够", pc.get("n", 0), pc.get("n", 0) >= trip["min_trades"],
         f"放行臂 ≥ {trip['min_trades']} 笔"),
        ("胜率", pc.get("win_rate"), (pc.get("win_rate") or 0) >= trip["min_win_rate"],
         f"≥ {trip['min_win_rate']:.0%}（回测放行臂最低 42%）"),
        ("赔率", pc.get("payoff"), (pc.get("payoff") or 0) >= trip["min_payoff"],
         f"≥ {trip['min_payoff']}（回测放行臂最低 1.22）"),
        ("利润因子", pc.get("profit_factor"), (pc.get("profit_factor") or 0) >= trip["min_profit_factor"],
         f"≥ {trip['min_profit_factor']}"),
        ("单笔期望>0", pc.get("expectancy_pp"), (pc.get("expectancy_pp") or -1) > 0,
         "放行臂期望必须为正（① 的实盘口径）"),
        ("d期望>0", round((pc.get("expectancy_pp") or 0) - (rc.get("expectancy_pp") or 0), 4),
         enough_pass and (pc.get("expectancy_pp") or 0) > (rc.get("expectancy_pp") or 0),
         "放行臂 − 剔除臂 > 0：闸门确实有用（任一臂无样本则不判）"),
        ("成交假设未崩", drag, (drag is not None and drag <= trip["max_open_drag"] * 100),
         f"收盘口径 − 次开盘口径 ≤ {trip['max_open_drag'] * 100:.2f}pp"),
    ]
    failed = [c for c in out["checks"] if not c[2]]
    out["verdict"] = "trip" if failed else "ok"
    out["failed_checks"] = [c[0] for c in failed]
    return out


def render(summary: dict) -> str:
    arms = summary["arms"]
    lines = ["", "===== shadow 台账：闸门两臂 × 两种成交假设 ====="]
    lines.append(f"  熔断判定：**{summary['verdict']}**"
                 + (f"（未过：{', '.join(summary['failed_checks'])}）"
                    if summary["failed_checks"] else "（全部指标在阈值内）"))
    lines.append(f"  {'臂':6s}{'口径':6s}{'笔数':>6s}{'胜率':>8s}{'赔率':>8s}{'利润因子':>10s}"
                 f"{'单笔期望pp':>13s}{'中位':>8s}{'p5':>8s}")

    def cell(value, width: int, spec: str) -> str:
        """None 一律印「-」而不是 0/nan：估不出来与"等于 0"是两件事，混起来会误判。"""
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return "-".rjust(width)
        return format(value, spec).rjust(width)

    for arm in ("pass", "reject", "all"):
        a = arms[arm]
        for label, key in (("收盘", "close"), ("次开盘", "open")):
            s = a.get(key) or {}
            lines.append(f"  {arm:6s}{label:6s}{s.get('n', 0):6d}"
                         + cell(s.get('win_rate'), 9, '.1%')
                         + cell(s.get('payoff'), 8, '.2f')
                         + cell(s.get('profit_factor'), 10, '.2f')
                         + cell(s.get('expectancy_pp'), 13, '+.3f')
                         + cell(s.get('median_pp'), 8, '+.2f')
                         + cell(s.get('p5_pp'), 8, '+.2f'))
    lines.append("")
    for name, value, ok, rule in summary["checks"]:
        shown = f"{value:.4g}" if isinstance(value, (int, float)) and value is not None else "-"
        lines.append(f"  {'✓' if ok else '✗'} {name:12s} 实测 {shown:>10s}  要求 {rule}")
    lines += ["",
              "  次开盘那一列是把**入场价**换成次一交易日开盘、出场判定不变算出来的，",
              "  所以两臂之差纯粹是入场口径的差；它比回测页诚实——回测按信号日收盘成交，",
              "  实盘最早次日开盘能买到，差的那一段只有这里看得到。"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="shadow 台账：闸门两臂 × 收盘/次开盘两种成交假设")
    ap.add_argument("--days", type=int, default=40, help="取最近 N 个交易日（默认 40）")
    ap.add_argument("--strategy", default="pattern_reversal")
    ap.add_argument("--factor", default=FACTOR)
    ap.add_argument("--min-pct", type=float, default=MIN_PCT)
    ap.add_argument("--hold", type=int, default=HOLD)
    ap.add_argument("--stop", type=float, default=STOP)
    ap.add_argument("--take", type=float, default=TAKE)
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 只票（调试用）")
    ap.add_argument("--report", action="store_true", help="只读盘出报告，不写文件")
    ap.add_argument("-v", "--verbose", action="store_true")
    for key in TRIP:
        ap.add_argument(f"--trip-{key.replace('_', '-')}", type=float, default=TRIP[key])
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    trip = {k: getattr(args, f"trip_{k}") for k in TRIP}

    provider = ParquetProvider(KLINE_DIR)
    strategy = create_strategy(args.strategy)
    lookback = max(int(strategy.required_rows() or 0), 60)
    codes = provider.codes()
    if args.limit:
        codes = codes[: args.limit]

    end = provider.dates()[-1] if provider.dates() else ""
    all_dates = [d for d in provider.dates() if d <= end]
    start = all_dates[-args.days - lookback] if len(all_dates) > args.days + lookback else all_dates[0]
    t0 = time.time()
    panel = _load(provider, codes, strategy, lookback, args.factor, end)
    panel = panel[panel["date"].astype(str) >= start].reset_index(drop=True)
    if panel.empty:
        raise SystemExit(f"没有 {start} 之后的日线，先跑 kline_spider")
    logger.info("面板 %d 行 × %d 只（%s ~ %s）", len(panel), panel["code"].nunique(),
                start, end)
    detail = collect_rows(panel, strategy, lookback, args.min_pct, args.factor, codes)
    if detail.empty:
        raise SystemExit("观察窗内没有任何命中，换个 --days 或 --strategy")
    detail = detail[detail["gate"] >= 0].reset_index(drop=True)
    logger.info("命中 %d 条（放行 %d / 剔除 %d / 不判 %d），用时 %.0fs",
                len(detail), int((detail["gate"] == 1).sum()),
                int((detail["gate"] == 0).sum()),
                len(detail) - int((detail["gate"] >= 0).sum()), time.time() - t0)

    summary = summarize(detail, trip)
    print(render(summary))
    if not args.report:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        # append：影子期是每天累积的，覆盖写等于把前几天的证据抹掉
        if DETAIL.is_file():
            old = pd.read_parquet(DETAIL)
            merged = pd.concat([old, detail], ignore_index=True)
            merged = merged.drop_duplicates(subset=["date", "code", "strategy"], keep="last")
        else:
            merged = detail
        merged.to_parquet(DETAIL, index=False)
        SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已写 %s（%d 行）与 %s", DETAIL, len(merged), SUMMARY)


if __name__ == "__main__":
    main()
