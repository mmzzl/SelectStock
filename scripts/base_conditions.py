"""base_conditions.py — 把 78 个预处理字段铺开成「写得出来、跨票可比」的基础条件清单。

为什么要这一步（2026-10-16）：之前那 28 条候选是我手工猜的，结果 `vol_diverge`（缩量背离）被我标成
卖出判据、其实它过的是买入侧——**人猜分类会把信号埋掉**。所以先不猜哪条有用，把能写的条件全列出来、
数清楚有多少，再决定怎么扫。

四条口径上的硬规矩：
1. **只写文法写得出来的**。`stock_selector/backtest/rule_expr.py` 是「当日字段之间比大小」，
   没有取昨日值的算子 ⇒ **「上穿/下穿」写不出来**，只能写成「快线在慢线上方」这种状态；
   也不支持负数字面量，`gap_pct < -3` 必须写成 `gap_pct + 3 < 0`（本脚本按这个规则生成缺口类条件）。
2. **阈值只按留出段之前算**（默认止于 2021-12-31）。连分位数都不许用 2022 年之后的数据，
   否则「预注册」就是句空话。
3. **绝对量级的字段一律不当阈值**：`volume`/`amount`/`obv`/`vol_ma5`/`vol_ma20`/`last_close`。
   5 块的票和 300 块的票成交量差几千倍，「成交量 > 6 万手」跨票没有含义，
   它们的信息已经被量比（vol_ratio）、量能Z（vol_z）这些比值覆盖。
4. **和 MACD 三件、ATR 必须先除以收盘价**再取分位：它们的绝对尺度和股价一起涨，
   同一个数对 5 块的票是异常、对 300 块的票是日常。

产物写 Mongo `profit_scan_base_conditions`（后台管理端可查），本地再留一份 JSON 便于眼验。

用法:
    python3 scripts/base_conditions.py --dry-run            # 只打印，不写库
    python3 scripts/base_conditions.py                      # 写 Mongo + 本地 JSON
    python3 scripts/base_conditions.py --step 8 --buckets 6 # 抽样更密 / 换档位数
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.backtest import rule_expr  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
LOG_DIR = ROOT / "logs" / "profit_scan"

HOLDOUT_START = "2021-12-31"   # 留出段起点：阈值/分位数一律不许看这天之后的数据
COLLECTION = "profit_scan_base_conditions"

# —— 字段角色（78 个字段的完整划分，脚本会自核对总数，漂移了就知道是 parquet 加了列）——
PRICE_COLS = ["close", "open", "high", "low"]
REF_LINES = ["ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
             "boll_upper", "boll_mid", "boll_lower",
             "neckline_sup", "neckline_res", "box_upper", "box_lower", "sup20", "res20"]
NORMALIZE = ["macd_dif", "macd_dea", "macd_hist", "atr14"]     # 阈值按 field/close 取分位
SKIP_ABS = ["volume", "amount", "obv", "vol_ma5", "vol_ma20", "last_close"]
# 线对线：文法写不出「上穿」，这是「快线在慢线上方」的状态对
LINE_PAIRS = [("ma5", "ma10"), ("ma5", "ma20"), ("ma5", "ma60"), ("ma10", "ma20"),
              ("ma10", "ma60"), ("ma20", "ma60"), ("ema12", "ema26"), ("ma20", "ema26"),
              ("kdj_k", "kdj_d"), ("macd_dif", "macd_dea")]
# 价格偏离参考线的档：贴线 1% 和跌破 20% 是两种完全不同的事，所以给 6 档而不是 10 档
# （数值列的 10 档是等频分位，价格的偏离没有「分位」可切，档越密越像在凑阈值）
DEVIATIONS = [0.01, 0.03, 0.05, 0.08, 0.12, 0.20]

KIND_NAMES = {
    "switch": "开关型（形态/量能事件响了没响）",
    "numeric": "有界数值区间档",
    "ratio": "归一后区间档（除以收盘价）",
    "price_vs_line": "价格 vs 参考线",
    "line_state": "线对线状态（文法写不出上穿）",
}


def ladder(n: int, base: list[float]) -> list[float]:
    """要更多档就在原阶梯后面续 4% 一跳；默认 6 档够 10 档口径对照。"""
    if n <= len(base):
        return base[:n]
    return base + [_round(base[-1] + 0.04 * (i + 1)) for i in range(n - len(base))]


def sample_frame(files: list[Path], fields: list[str], date_to: str) -> pd.DataFrame:
    """抽样若干票、砍掉留出段之后的行，用来定阈值和算抽样触发率。"""
    frames = []
    for path in files:
        try:
            df = pd.read_parquet(path, columns=["name", "date", *fields])
        except Exception:
            continue
        if len(df) < 60 or bucket.is_st_name(str(df["name"].iloc[-1] or "")):
            continue
        df = df[df["date"].astype(str) <= date_to]
        if len(df):
            frames.append(df.drop(columns=["name"]))
    out = pd.concat(frames, ignore_index=True)
    return out


def split_kinds(df: pd.DataFrame, labels: dict[str, str]) -> dict[str, list[str]]:
    """按实测取值把字段分成角色，别靠列名猜哪列是 0/1。"""
    kinds: dict[str, list[str]] = {"bool": [], "num": [], "ratio": [], "skip": [],
                                   "price": [], "ref": []}
    for f in labels:
        if f in SKIP_ABS:
            kinds["skip"].append(f)
        elif f in PRICE_COLS:
            kinds["price"].append(f)
        elif f in REF_LINES:
            kinds["ref"].append(f)
        elif f in NORMALIZE:
            kinds["ratio"].append(f)
        else:
            u = np.unique(np.round(df[f].to_numpy(dtype="float64")[:200_000], 6))
            kinds["bool" if all(x in (0.0, 1.0) for x in u) else "num"].append(f)
    return kinds


def _round(v: float) -> float:
    """阈值留 4 位有效：分位数本来就是抽样估计，多写的位数是假精度。-0.0 归成 0.0，
    否则文案里会出现「-0.0」这种看着像另一个数值的鬼影。"""
    out = float(f"{v:.4g}")
    return 0.0 if out == 0 else out


def _cmp(left: str, op: str, q: float) -> str:
    """文法不收负数字面量（`gap_pct < -1.2` 直接报错），负阈值统一改写成加法形式。
    缺口、ROC、MACD 柱这些列的分位数经常是负的，不堵这一步生成器会成片失败。"""
    return f"{left} {op} {q}" if q >= 0 else f"{left} + {abs(q)} {op} 0"


def _band_edges(vals: np.ndarray, bands: int) -> list[float]:
    """等频切点的实测值；抽样里挤在同一个数上（比如 pos60 的低分位全是 0）就退回更少档。"""
    ps = [100.0 * i / bands for i in range(1, bands)]
    qs = [_round(float(np.nanpercentile(vals, p))) for p in ps]
    return [q for i, q in enumerate(qs) if i == 0 or q > qs[i - 1]]


def _band_items(field: str, left: str, cid_prefix: str, label: str, vals: np.ndarray,
                bands: int, kind: str) -> list[dict]:
    """数值列切成**互斥的等频区间**，一条只占约 1/bands 的历史时点。

    为什么不生成 `rsi14 > 34` 这种单边阈值：那条件 95% 的交易日都在响，
    它不是一条买入信号，是「今天是个日子」，笔数会虚高到几百万、每笔均值趋近市场平均。
    切成区间档之后，「RSI 在 10%~20% 分位」和「RSI 在 90%~100% 分位」才是两件事。

    `field` 是入库的列名、`left` 是表达式左值：归一类两者不同（`macd_dif / close`），
    弄混会让阈值按比值取分位、比较却按原始量纲做，单位对不上还不报错。
    """
    qs = _band_edges(vals, bands)
    if not qs:
        return []
    edges: list[tuple] = [(None, qs[0])] \
        + [(qs[i], qs[i + 1]) for i in range(len(qs) - 1)] \
        + [(qs[-1], None)]
    span = 100.0 / len(edges)
    out = []
    for i, (lo, hi) in enumerate(edges):
        p_lo, p_hi = round(i * span, 1), round((i + 1) * span, 1)
        if lo is None:
            expr, rng, text = _cmp(left, "<", hi), [None, hi], f"低于 {hi}"
        elif hi is None:
            expr, rng, text = _cmp(left, ">=", lo), [lo, None], f"高于 {lo}"
        else:
            expr = f"{_cmp(left, '>=', lo)} and {_cmp(left, '<', hi)}"
            rng, text = [lo, hi], f"{lo}~{hi}"
        out.append({"id": f"{cid_prefix}_q{p_lo:g}_{p_hi:g}", "kind": kind,
                    "field": field, "ref": "", "expr": expr,
                    "label": f"{label} {p_lo:g}~{p_hi:g}%：{text}"[:40],
                    "threshold": hi if lo is None else lo, "range": rng,
                    "percentile": f"{p_lo:g}-{p_hi:g}"})
    return out


def build(df: pd.DataFrame, labels: dict[str, str], kinds: dict[str, list[str]],
          bands: int, devs: list[float]) -> tuple[list[dict], list[dict]]:
    """生成基础条件；文法校验不过的进 failures 单独报告，不静默丢。"""
    conds: list[dict] = []

    def add(cid: str, kind: str, field: str, expr: str, label: str, ref: str = "",
            threshold=None, pct=None) -> None:
        conds.append({"id": cid, "kind": kind, "field": field, "ref": ref,
                      "expr": expr, "label": label[:40], "threshold": threshold,
                      "range": None, "percentile": pct})

    for f in kinds["bool"]:
        add(f"b_{f}", "switch", f, f"{f} > 0.5", f"{labels[f]} 响了")

    for f in kinds["num"]:
        conds += _band_items(f, f, f"n_{f}", labels[f], df[f].to_numpy(dtype="float64"),
                             bands, "numeric")

    for f in kinds["ratio"]:
        with np.errstate(divide="ignore", invalid="ignore"):
            vals = df[f].to_numpy(dtype="float64") / df["close"].to_numpy(dtype="float64")
        conds += _band_items(f, f"{f} / close", f"r_{f}", f"{labels[f]}/收盘价",
                             vals, bands, "ratio")

    for p in PRICE_COLS:
        for r in REF_LINES:
            for d in devs:
                lo, hi = _round(1 - d), _round(1 + d)
                add(f"p_{p}_{r}_d{int(d * 100)}_below", "price_vs_line", p,
                    f"{p} < {r} * {lo}", f"{labels[p]} 低于{labels[r]} {d:.0%} 以上",
                    ref=r, threshold=d)
                add(f"p_{p}_{r}_d{int(d * 100)}_above", "price_vs_line", p,
                    f"{p} > {r} * {hi}", f"{labels[p]} 高于{labels[r]} {d:.0%} 以上",
                    ref=r, threshold=d)

    for fast, slow in LINE_PAIRS:
        add(f"x_{fast}_{slow}_above", "line_state", fast, f"{fast} > {slow}",
            f"{labels[fast]} 在{labels[slow]}上方", ref=slow)
        add(f"x_{fast}_{slow}_below", "line_state", fast, f"{fast} < {slow}",
            f"{labels[fast]} 在{labels[slow]}下方", ref=slow)

    ok, bad = [], []
    for c in conds:
        try:
            c["fields"] = sorted(rule_expr.validate(c["expr"]))
            ok.append(c)
        except rule_expr.RuleSyntaxError as exc:
            c["validate_error"] = str(exc)
            bad.append(c)
    return ok, bad


def trigger_rates(conds: list[dict], df: pd.DataFrame) -> None:
    """在同一个抽样上算触发率：0 触发说明阈值或字段有问题，100% 触发说明这条没有区分度。"""
    cols = {f: df[f].to_numpy(dtype="float64") for f in
            {fld for c in conds for fld in c["fields"]}}
    for c in conds:
        try:
            m = rule_expr.eval_matrix(c["expr"], cols)
            c["sample_trigger_pct"] = round(float(np.nanmean(m)) * 100, 3)
        except Exception as exc:
            c["sample_trigger_pct"] = None
            c["validate_error"] = f"求值失败：{exc}"


def to_mongo(conds: list[dict]) -> None:
    """整表覆盖（幂等）+ 建索引：没索引页面按触发率排序会当场卡死。"""
    import database

    col = database.get_db()[COLLECTION]
    col.delete_many({})
    docs = [{**c, "computed_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
             "holdout_start": HOLDOUT_START} for c in conds]
    for i in range(0, len(docs), 2000):
        col.insert_many(docs[i:i + 2000], ordered=False)
    col.create_index([("id", 1)], unique=True)
    col.create_index([("kind", 1), ("field", 1)])
    print(f"已写 Mongo：{database.get_db().name}.{COLLECTION} "
          f"{col.count_documents({}):,} 条")


def render(conds: list[dict], bad: list[dict], bands: int, devs: int,
           rows: int, files: int) -> str:
    lines = [f"基础条件清单：抽样 {files} 只票、{rows:,} 个个股·日（剔 ST，只看 {HOLDOUT_START} 之前），"
             f"数值 {bands} 档 / 价格偏离 {devs} 档",
             f"  生成 {len(conds) + len(bad):,} 条 → 文法校验通过 {len(conds):,} 条、失败 {len(bad):,} 条"]
    by: dict[str, list[dict]] = {}
    for c in conds:
        by.setdefault(c["kind"], []).append(c)
    lines.append(f"  {'类别':<26s} {'条数':>7s} {'触发率中位':>11s} {'零触发':>7s} {'>60%触发':>9s}")
    for kind, name in KIND_NAMES.items():
        g = by.get(kind) or []
        if not g:
            continue
        rates = [c["sample_trigger_pct"] for c in g if c["sample_trigger_pct"] is not None]
        med = float(np.median(rates)) if rates else float("nan")
        lines.append(f"  {name:<26s} {len(g):>7,} {med:>10.2f}% "
                     f"{sum(1 for r in rates if r == 0):>7,} "
                     f"{sum(1 for r in rates if r > 60):>9,}")
    lines.append(f"  {'合计':<26s} {len(conds):>7,}")
    for kind, name in KIND_NAMES.items():
        g = by.get(kind) or []
        if not g:
            continue
        lines.append(f"\n【{name}】抽样 3 条：")
        for c in g[:: max(1, len(g) // 3)][:3]:
            lines.append(f"  {c['id']:<28s} {c['expr']:<34s} 触发 {c['sample_trigger_pct']}%  {c['label']}")
    if bad:
        lines.append(f"\n⚠️ 文法校验失败 {len(bad)} 条（已丢弃，不入库）：")
        for c in bad[:10]:
            lines.append(f"  {c['id']}  {c['expr']}  ← {c['validate_error']}")
    dead = [c for c in conds if not c["sample_trigger_pct"]]
    if dead:
        lines.append(f"\n抽样里 0 触发 {len(dead)} 条（多半是形态/关键价位列在抽样票里没出现过，"
                     f"全量扫描会给真实笔数）：{', '.join(c['id'] for c in dead[:8])}"
                     f"{' …' if len(dead) > 8 else ''}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--step", type=int, default=20, help="每 N 个文件取 1 个做抽样（定阈值用）")
    ap.add_argument("--bands", type=int, default=10, help="数值列等频档数（计划口径 10）")
    ap.add_argument("--devs", type=int, default=6, help="价格偏离参考线的档数")
    ap.add_argument("--to", dest="date_to", default=HOLDOUT_START,
                    help="阈值/分位数只用这天之前的数据")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 Mongo/本地 JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    labels = {e["field"]: e["label"] for e in rule_expr.fields_catalog()}
    files = sorted(Path(args.kline_dir).glob("*.parquet"))[:: max(1, args.step)]
    t0 = time.time()
    df = sample_frame(files, list(labels), args.date_to)
    print(f"抽样 {len(files)} 个文件 → {len(df):,} 行，{time.time() - t0:.0f}s")

    kinds = split_kinds(df, labels)
    total = sum(len(v) for v in kinds.values())
    if total != len(labels):
        print(f"⚠️ 字段角色划分 {total} != 文法白名单 {len(labels)}，parquet 列和 catalog 分家了，先查再信下面的数")

    conds, bad = build(df, labels, kinds, args.bands, ladder(args.devs, DEVIATIONS))
    t1 = time.time()
    trigger_rates(conds, df)
    print(f"触发率算完 {time.time() - t1:.0f}s\n")
    print(render(conds, bad, args.bands, args.devs, len(df), len(files)))

    if not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = LOG_DIR / "base_conditions.json"
        out.write_text(json.dumps({"bands": args.bands, "devs": args.devs, "sample_rows": len(df),
                                   "holdout_start": args.date_to, "conditions": conds,
                                   "failed": bad}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n本地清单：{out}")
        to_mongo(conds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
