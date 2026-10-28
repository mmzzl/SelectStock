"""「今天尾盘买」名单：深跌 + 缩量（批次 V/V3 里 10 年 4 段唯一过门槛的那族）。

口径（docs §36）：
- 深跌 = 收盘相对 20 个交易日前的跌幅；缩量 = 当日成交额 / 近 20 日均额。各在人口内取百分位，各占 0.5。
- 人口 = 主板可买（剔 300/301/688/689）∩ 20 日均额 ≥1 亿 ∩ 当日未封板（|涨跌| < 9.9%）∩ 剔 ST/退。
- 读数（2015-01-05→2026-10-27，2,691 个交易日，T 收盘买 → T+1 收盘卖）：
  次日毛超额 +0.086pp/日（按日配对 t 5.2），**次日上涨概率 51.1%**（同期合规人口 47.9%）。
  扣双边 0.12% 成本后：持有 1 天 −0.030%/日（**每天来回是亏的**），持有 3 天 +0.048%/日，持有 5 天 +0.118%/日。
- 这族里**不能**加「当日收阳 / 尾盘强 / 放量突破」：V3 实测把尾盘强单独拿出来是 10 年最强负因子（−0.224pp，t −14.3）。

数据面板只到 T 日收盘 ⇒ 盘中（14:50）要出价得用实时行情覆盖最后一根，`--live` 走腾讯快照，默认不打网络。
名字（判 ST）默认读 `data/csv/all_stock.csv` 那份快照，`--names live` 才问现名——过期表判 ST 等于没判（§17 的教训），
所以打印里如实标出用的是哪一份。

用法：
    python3 scripts/qlib_dip_shrink_list.py                  # 面板最后一天，前 30 名
    python3 scripts/qlib_dip_shrink_list.py --top 10 --date 2026-10-20
    python3 scripts/qlib_dip_shrink_list.py --live           # 14:50 用实时行情覆盖最后一根（打网络）
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

BOARD_PREFIX = ("SH60", "SZ00")      # 他 10-26 点名的交易权限：300/301/688/689 一律不出（§30）
WINDOW = 20
MIN_AMOUNT = 1e8
LIMIT_PCT = 9.9


@dataclass
class Bar:
    """一支票最近若干根的收盘与成交额（下标 -1 是信号日 T）。"""
    code: str
    name: str
    close: list[float]
    amount: list[float]


def _percentile(values: list[float]) -> list[float]:
    """人口内百分位，和回测的 argsort(argsort(v))/(n-1) 完全同款：并列按输入序给相邻名次，不做平均。"""
    n = len(values)
    if n < 2:
        return [0.0] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for rank, i in enumerate(order):
        out[i] = rank / (n - 1)
    return out


def screen(bars: list[Bar]) -> tuple[list[dict], dict]:
    """返回 (入选票的指标, 各道筛的剔除计数)。计数必须可见——静默少一批票就是缺陷。"""
    counts = {"收到": len(bars), "非主板": 0, "名字判ST或退": 0, "历史不足": 0,
              "数据缺失": 0, "薄流动": 0, "当日封板": 0, "入选": 0}
    kept: list[tuple[Bar, float, float, float]] = []
    for b in bars:
        if not b.code.startswith(BOARD_PREFIX):
            counts["非主板"] += 1
            continue
        if "ST" in (b.name or "").upper() or "退" in (b.name or ""):
            counts["名字判ST或退"] += 1
            continue
        if len(b.close) < WINDOW + 1 or len(b.amount) < WINDOW + 1:
            counts["历史不足"] += 1
            continue
        c_now, c_20 = b.close[-1], b.close[-(WINDOW + 1)]
        amt_today = b.amount[-1]
        if not all(x and x > 0 for x in (c_now, c_20, amt_today)) \
           or any(x is None or x <= 0 for x in b.close[-(WINDOW + 1):]) \
           or any(x is None or x <= 0 for x in b.amount[-WINDOW:]):
            counts["数据缺失"] += 1
            continue
        dip = c_now / c_20 - 1.0
        pct = c_now / b.close[-2] - 1.0
        amt20 = sum(b.amount[-WINDOW:]) / WINDOW
        if amt20 < MIN_AMOUNT:
            counts["薄流动"] += 1
            continue
        if abs(pct) * 100 >= LIMIT_PCT:
            counts["当日封板"] += 1
            continue
        kept.append((b, dip, amt_today / amt20, pct))

    counts["入选"] = len(kept)
    if not kept:
        return [], counts
    zd = _percentile([-d for _, d, _, _ in kept])
    zs = _percentile([-r for _, _, r, _ in kept])
    rows = [{"code": b.code, "name": b.name, "close": round(b.close[-1], 3),
             "pct": round(p * 100, 2), "dip20_pct": round(d * 100, 2),
             "amount_ratio": round(r, 3), "amount_yi": round(sum(b.amount[-WINDOW:]) / WINDOW / 1e8, 2),
             "score": round(0.5 * z1 + 0.5 * z2, 4)}
            for (b, d, r, p), z1, z2 in zip(kept, zd, zs)]
    rows.sort(key=lambda x: (-x["score"], x["code"]))
    return rows, counts


def screen_and_rank(bars: list[Bar], top: int = 30) -> list[dict]:
    rows, _ = screen(bars)
    return rows[:top]


def read_bin_series(path: Path, n_cal: int):
    """`<field>.day.bin`：首格是起始日历下标，其余逐日。返回按日历对齐的定长数组（缺格 NaN）。"""
    import numpy as np

    if not path.exists():
        return None
    a = np.fromfile(path, dtype="<f")
    if a.size < 2:
        return None
    st = int(a[0])
    out = np.full(n_cal, np.nan)
    span = min(n_cal - st, a.size - 1)
    out[st:st + span] = a[1:1 + span]
    return out


def load_bars(date: str | None, names_mode: str = "file") -> tuple[list[Bar], str, dict]:
    """从 bin 面板取 `date`（默认面板最后一天）那天的收盘/成交额序列。

    候选取自 `data/qlib/universe.txt` 里的主板那批（和 §36 回测**同一份人口**，1,769 支）——
    直接扫 features/ 会把 3,700 支全市场主板都拉进来，百分位是在另一张表上排的，读数就不等价了。
    另一处小差异如实写出：这里要求最近 21 格**连续有行情**（停牌断档的票剔掉），
    回测里允许窗口内断档，所以实际人口会比回测略严一点。
    """
    import numpy as np

    from qlib_daily_universe import NAME_FILE, panel_calendar, read_names

    cal = panel_calendar()
    if date is None:
        idx = len(cal) - 1
    else:
        if date not in cal:
            raise SystemExit(f"面板日历里没有 {date}（最后一天 {cal[-1]}）")
        idx = cal.index(date)
    need = WINDOW + 1
    names = read_names(NAME_FILE) if names_mode == "file" else None
    recent_counts = np.zeros(21)

    uni = REPO / "data" / "qlib" / "universe.txt"
    syms = [s.strip() for s in uni.read_text(encoding="utf-8").splitlines() if s.strip()] \
        if uni.exists() else []
    if not syms:
        raise SystemExit(f"{uni} 读不到 ⇒ 无法保证与 §36 回测同人口，先不出价")
    bars: list[Bar] = []
    for sym in syms:
        if not sym.startswith(BOARD_PREFIX):
            continue
        base = Path("/data/work/qlib_data/cn/features") / sym.lower()
        cl = read_bin_series(base / "close.day.bin", len(cal))
        if cl is None or idx < need - 1:
            continue
        recent_counts += _ok_np(cl[idx - 20:idx + 1])
        if not _ok(cl[idx - need + 1:idx + 1]):
            continue
        am = read_bin_series(base / "amount.day.bin", len(cal))
        amt = list(am[idx - need + 1:idx + 1]) if am is not None else [float("nan")] * need
        name = (names.get(sym, "") if names_mode == "file" else "")
        bars.append(Bar(code=sym, name=name, close=list(cl[idx - need + 1:idx + 1]), amount=amt))

    if names_mode == "live":
        from stock_selector.quote import tencent

        live = tencent.fetch([b.code[2:] for b in bars], timeout=8.0)
        for b in bars:
            q = live.get(b.code[2:]) or {}
            b.name = q.get("name") or b.name
    stale_note = f"名字表 {NAME_FILE}（快照，非现名）" if names_mode == "file" else "腾讯现名"
    return bars, cal[idx], {"day_counts": [int(x) for x in recent_counts], "names_from": stale_note}


def _ok(vals) -> bool:
    return all(v == v and v > 0 for v in vals)


def _ok_np(vals) -> list[int]:
    import numpy as np

    return (np.isfinite(vals) & (vals > 0)).astype(int)


def hole_warning(meta: dict, asof: str) -> str:
    """面板缺数那类事故（§31）在这里表现为「今天有行情的票突然变少」⇒ 必须喊出来，不能安静出半张名单。"""
    counts = meta["day_counts"]
    if len(counts) < 6:
        return ""
    prior_days = sorted(counts[:-1])
    today, prior = counts[-1], prior_days[len(prior_days) // 2 - 1]
    if prior and today < 0.85 * prior:
        return (f"⚠ {asof} 有行情的票只有 {today} 支，不足近 20 日中位 {prior} 的 85%"
                f"⇒ 行情链可能缺数，这份名单先别用")
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="信号日 T，默认面板最后一天")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--names", choices=("file", "live"), default="file")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    bars, asof, meta = load_bars(args.date, args.names)
    rows, counts = screen(bars)
    warn = hole_warning(meta, asof)
    print(f"信号日 T = {asof}｜人口筛：{counts}｜名字来源：{meta['names_from']}")
    if warn:
        print(warn)
    print(f"口径：T 收盘买入 → T+1 收盘卖出。10 年实测 次日上涨概率 51.1%（人口 47.9%），"
          f"毛超额 +0.086pp/日；扣 0.12% 双边成本后持有 3~5 天才为正（+0.048%/+0.118% 每日）。")
    print(f"{'名次':>4} {'代码':<9}{'名称':<10}{'收盘':>8}{'今日':>8}{'20日跌幅':>10}{'额比':>7}{'得分':>7}")
    for i, r in enumerate(rows[:args.top], 1):
        print(f"{i:>4} {r['code']:<9}{(r['name'] or ''):<10}{r['close']:>8.2f}{r['pct']:>7.2f}%"
              f"{r['dip20_pct']:>9.2f}%{r['amount_ratio']:>7.2f}{r['score']:>7.3f}")
    if args.json:
        args.json.write_text(json.dumps({"asof": asof, "counts": counts, "rows": rows},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
