"""生成「今天要给哪些票打分」的全市场名单：data/qlib/universe.txt。

为什么要有这个脚本：8003 打分的池一直读模型产物里那份冻结的 pool.txt（300 只，按**训练段**
流动性挑一次就不动），于是两次重训之间新变热的票永远进不了榜。名单放开成全市场之后，
「谁进池」变成每天按客观规则重算，而不是跟着上一次训练冻住。

五条筛（全部可复现，缺任何一条都会静默放进打分不可信的票）：
1. **只要股票**：代码前缀 sh.60 / sh.68 / sz.00 / sz.30，基金（sh.5x / sz.15）一律不要——
   模型是在股票上学出来的，基金的价量结构不在它的输入分布里。
2. **剔 ST/*ST/退**：与 8002 那条链的 selector.exclude_name_keywords 同口径，名字带 ST 或「退」的不打。
   名字必须问**现名**（默认走腾讯快照）：本仓那份 data/csv/all_stock.csv 是 2026-05-24 的快照、
   Mongo bk_stocks 最近一次 imported_at 是 2026-05-19，两份都过期了 5 个月，拿旧表判 ST 等于没判
   ——2026-10-18 实测这样漏进名单的有 113 只（含当日榜内第 16 名 *ST卓然、第 28 名 ST龙大）。
3. **上市满 --min-bars 根日线**（默认 130 = serving.WARMUP_DAYS）：Alpha158 最长滚动窗口 60 日，
   不足 130 根的票，窗口里全是 NaN，归一化后被填成 0——那是一行「看起来像中性票」的假因子，
   模型会给它一个像模像样的分数。这类票必须挡在名单外，而不是靠打分阶段兜。
4. **面板里有这只的日线文件**：候选代码清单本身就取自 csv 面板（缺文件=数据链没覆盖到它，
   进不了名单），不再像以前那样以名字表为准——旧表不认识的新上市票会静默进不了名单，
   连计数都没有。
5. **最近 20 个交易日日均成交额 ≥ --min-amount**（默认 1e8 元 = 1 亿）：全市场名单里四成的票
   钱进不去——2026-10-18 按 67 个交易日样本外重放实测，1 亿门槛只留下 **60.6%** 的名单
   （日均 2,993 只），而池外前 30 名的 20 日均成交额中位有 4.59 亿（模型本来就偏爱流动票）。
   这道筛**按可成交性选，不按准确度选**：那 67 个交易日窗口里「榜内 IC」是负的
   （不设下限 −0.0056），所以不能拿它证明「筛掉低流动就更准」。
   取不到成交额的票（面板 amount 列整列空/非正，或 20 天里有效行不足 10 天）**单独计数并剔除**——
   「判不了流动性」不等于「过了流动性筛」，和第 2 条对「取不到名字」的处理是同一个原则。

用法：
    python3 scripts/qlib_daily_universe.py                 # 生成名单并打印各类计数
    python3 scripts/qlib_daily_universe.py --dry-run       # 只算数不写文件
    python3 scripts/qlib_daily_universe.py --min-bars 250  # 抬高上市时长门槛
    python3 scripts/qlib_daily_universe.py --names file    # 离线：只用旧名字表（会打出警告口径）
    python3 scripts/qlib_daily_universe.py --min-amount 3e8  # 收紧流动性下限到 3 亿
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

logger = logging.getLogger("qlib_universe")

# 面板里的符号形状（qlib_export_kline.symbol 的产物），等价于原来的带点前缀判据
ASHARE_PREFIXES = ("SH60", "SH68", "SZ00", "SZ30")
OUT_DEFAULT = REPO / "data" / "qlib" / "universe.txt"
NAME_FILE = REPO / "data" / "csv" / "all_stock.csv"
CSV_PANEL = Path("/data/work/qlib_data/csv")

AMT_COL = 6           # 面板列序 date,open,high,low,close,volume,amount,vwap,factor
AMT_WINDOW = 20       # 流动性看 20 个交易日，和 G-N3 分档用的 amt20 同一个口径
AMT_MIN_DAYS = 10     # 有效行不足一半就判不了流动性
MIN_AMOUNT = 1e8      # 20 日均成交额下限（元）。依据见 docs §17.9


def read_names(path: Path) -> dict[str, str]:
    """qlib 符号（SH600000 / SZ000001）-> 名称（旧快照，只当兜底用）。

    必须带市场前缀做键：六位数字在不同市场会撞车（sh.000022 是「沪公司债」、
    sz.000022 是另一只票），只按数字建表会让深市代码拿到沪市的名字。
    """
    import csv

    out: dict[str, str] = {}
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or "").strip().lower()
            name = (row.get("code_name") or row.get("name") or "").strip()
            market, _, num = code.partition(".")
            sym = f"{market.upper()}{num}" if num else code.upper()
            if sym and name:
                out[sym] = name
    return out


def is_st(name: str) -> bool:
    """现名带 ST/*ST 或「退」= 不打分。与 8002 那条链的 exclude_name_keywords 同口径。"""
    return "ST" in name.upper() or "退" in name


def panel_stocks() -> list[str]:
    """csv 面板里所有 A 股符号（SH60/SH68/SZ00/SZ30 开头），排序后返回。

    候选清单取自面板而不是名字表：面板是「数据链覆盖到谁」，名字表是「上次快照认识谁」，
    用后者当入口会让新上市的票静默进不了名单。
    """
    return sorted(p.stem for p in CSV_PANEL.glob("*.csv") if p.stem.startswith(ASHARE_PREFIXES))


def panel_stats(symbols: list[str]) -> dict[str, tuple[int, float | None]]:
    """每只票 -> (日线根数, 最近 20 个交易日的日均成交额【元】)。

    成交额取自面板 csv 的 amount 列（`qlib_export_kline` 落的就是元口径）。
    两种情况给 None：整列都是空/非正（东财断供那几天 amount 就是空值），或有效行不足
    `AMT_MIN_DAYS` 天。**判不了流动性就等于不可成交**，不能当成「过了流动性筛」放过去——
    这和本脚本对「取不到名字的票」的处理是同一个原则。
    """
    out: dict[str, tuple[int, float | None]] = {}
    for sym in symbols:
        path = CSV_PANEL / f"{sym}.csv"
        if not path.exists():
            continue
        with path.open("rb") as fh:
            rows = fh.read().splitlines()[1:]     # 减掉表头
        values: list[float] = []
        for raw in rows[-AMT_WINDOW:]:
            fields = raw.split(b",")
            if len(fields) <= AMT_COL:
                continue
            try:
                value = float(fields[AMT_COL])
            except ValueError:                     # 空串（断供）或非数值
                continue
            if value > 0:
                values.append(value)
        amount = sum(values) / len(values) if len(values) >= AMT_MIN_DAYS else None
        out[sym] = (len(rows), amount)
    return out


def resolve_names(symbols: list[str], mode: str, timeout: float) -> tuple[dict[str, str], dict]:
    """符号 -> 现名，并说明这个名字到底是哪来的。

    live：先问腾讯快照，问不到的用旧表补；整批问不到（网络全挂）就整份退到旧表并计数，
    因为「没有名字」不等于「不是 ST」——退回去至少还能筛掉 5 月那批旧 ST，但必须在报告里
    看得见，否则又回到「拿过期表判 ST、以为筛过了」这个坑。
    """
    codes = [s[2:] for s in symbols]
    stale = read_names(NAME_FILE)
    live: dict[str, str] = {}
    error = ""
    if mode == "live":
        from stock_selector.quote import tencent

        try:
            live = tencent.fetch_names(codes, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络失败要退旧表，不能整条链断
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("现名整批取不到，退回 %s：%s", NAME_FILE, error)
    names = {sym: (live.get(code) or stale.get(sym, "")) for code, sym in zip(codes, symbols)}
    used_live = sum(1 for code in codes if live.get(code))
    return names, {
        "names_mode": mode,
        "names_live": used_live,
        "names_from_stale_file": sum(1 for v in names.values() if v) - used_live,
        "names_empty": sum(1 for v in names.values() if not v),
        "stale_file_rows": len(stale),
        "live_error": error,
    }


def build(min_bars: int, keep_st: bool, names_mode: str = "live",
          timeout: float = 8.0, min_amount: float = MIN_AMOUNT) -> tuple[list[str], dict]:
    symbols = panel_stocks()
    names, name_report = resolve_names(symbols, names_mode, timeout)
    stats = panel_stats(symbols)
    report = {
        "panel_stocks": len(symbols),
        **name_report,
        "dropped_st": 0,
        "dropped_no_name": 0,
        "short_history": 0,
        "dropped_illiquid": 0,
        "dropped_no_amount": 0,
        "min_bars": min_bars,
        "min_amount": min_amount,
    }
    kept: list[str] = []
    for sym in symbols:
        name = names.get(sym, "")
        if not name:
            # 现名和旧表都给不出名字 = 没法判 ST，只能挡在名单外（退市/代码变更就在这里）
            report["dropped_no_name"] += 1
            continue
        if not keep_st and is_st(name):
            report["dropped_st"] += 1
            continue
        bars, amount = stats.get(sym, (0, None))
        if bars < min_bars:
            report["short_history"] += 1
            continue
        if amount is None:
            # 20 天里有效成交额不足一半：不是「不流动」，是「判不了」——同样不进名单，但单独计数，
            # 否则某天面板 amount 整列断供时，名单会无声缩掉一截，看起来像「筛过了」。
            report["dropped_no_amount"] += 1
            continue
        if amount < min_amount:
            report["dropped_illiquid"] += 1
            continue
        kept.append(sym)
    report["universe"] = len(kept)
    return kept, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-bars", type=int, default=130,
                    help="最少日线根数，默认=serving.WARMUP_DAYS(130)")
    ap.add_argument("--keep-st", action="store_true", help="调试用：不剔 ST/退")
    ap.add_argument("--names", choices=["live", "file"], default="live",
                    help="ST 判据的名字来源：live=腾讯现名（默认），file=只用 2026-05-24 那份旧表")
    ap.add_argument("--timeout", type=float, default=8.0, help="单批现名请求的秒数")
    ap.add_argument("--min-amount", type=float, default=MIN_AMOUNT,
                    help="20 日均成交额下限（元），默认 1e8=1 亿；0 = 关掉这道筛。依据见 docs §17.9")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    kept, report = build(args.min_bars, args.keep_st, args.names, args.timeout,
                         args.min_amount)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.names == "live" and report["names_live"] < 0.9 * report["panel_stocks"]:
        print(f"警告：只有 {report['names_live']}/{report['panel_stocks']} 只拿到现名，"
              f"其余按 {NAME_FILE}（{_snapshot_date()} 的快照）兜底，ST 判据可能过期",
              file=sys.stderr)
    if report["dropped_no_amount"] > 0.1 * report["panel_stocks"]:
        print(f"警告：{report['dropped_no_amount']}/{report['panel_stocks']} 只判不了流动性被剔除，"
              f"先查 {CSV_PANEL} 的 amount 列是不是整列断供，别当成「筛掉了不流动的票」",
              file=sys.stderr)
    if not kept:
        raise SystemExit("名单是空的——先查 csv 面板在不在（/data/work/qlib_data/csv）")
    if not args.dry_run:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"已写 {out}（{len(kept)} 只）")
    return 0


def _snapshot_date() -> str:
    """旧名字表的落盘日期，用于警告文案（别把兜底说成现名）。"""
    import datetime

    try:
        return datetime.date.fromtimestamp(NAME_FILE.stat().st_mtime).isoformat()
    except OSError:
        return "未知日期"



if __name__ == "__main__":
    sys.exit(main())
