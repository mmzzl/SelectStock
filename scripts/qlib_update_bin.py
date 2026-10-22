"""收盘后把票池当天的日线增量写进 qlib 的 bin 面板（选股链路的第一环）。

为什么必须有这一步：8003 打分读的是 /data/work/qlib_data/cn 下的 .bin，不读 Mongo。
爬虫 15:10 把当天 K 线落到 Mongo、15:45 merge_snapshot_amount 把成交额并回去，bin 面板
要是不跟着走，17:30 的选股就会一直出**上一个交易日**的名单，而且它不报错——看起来
就像「今天还是这几只上榜」，这是最难发现的一种坏法。

六条必须照做的口径（前三条与 scripts/qlib_export_kline.py 的头一份文档同源）：
1. **窗口要稠密**：按 Mongo 交易日历把停牌/缺采的日子填 NaN 再落 CSV。dump_bin 在
   update 模式下是「往 .bin 尾部直接追加 N 个数」，它不检查日期格对不对；窗口里少一天，
   这只票之后每一行都会整体错位一格——不是丢数据，是把周一的价格写到周二的日期上。
2. **只导票池 + 基准**，不导全市场：dump_bin 的 DumpDataUpdate 会把 data_path 目录里的
   **所有** CSV 一次性 concat 进内存（源码注释自己写着 "NOTE: Need more memory"）。
   全量面板 6,937 只，票池只有 301 只。
3. **字段集和脏数据过滤必须和当初 dump_all 那次完全一样**：少给一个字段，那只票的这一列
   从此不再更新；少过滤一次「价格不成行」的脏行，就等于往训练时没有数据的格子里写数。
4. **窗口必须覆盖 panel_end 之后的每一天**：库里比面板新 N 天而窗口只取 N−1 天时，
   直接报错退出，不要静默地把面板推到中间某天。
5. **中间的空洞不会自己长回来**：dump_update 对已存在的票只追加「比它的结束日更晚」的行，
   所以行情迟到一步（爬虫队尾是沪市，2026-10-21/22 实测 16:24~16:31 才落库）就会把那天
   永久留成 NaN，而打分读到 NaN 只是把这只票悄悄漏掉。因此每批追加完都要 `fill_holes`
   回补窗口里的空洞（只填 NaN 格、绝不覆盖已有读数），并按「日 × 市场」核一遍覆盖率，
   缺到不能出名单就推钉钉。
6. **成交额缺了要单独看出来**：`dump_update` 那一步在 `merge_snapshot_amount` 之前跑完时，
   写进去的是「有价无额」的行（close 有、amount 空），而 vwap 是 amount/volume 算的，
   缺额那天的 vwap 会兜底成 close，于是 Alpha158 里唯一用 $vwap 的那个因子（VWAP0）
   变成常数 1。所以覆盖率同时统计 amount 列，`fill_holes` 补回某天的 amount 时**连带把
   同一天的 vwap 格子一起改对**（只改这次补过额的那天，别的格子一律不动）。

用法：
    python3 scripts/qlib_update_bin.py --dry-run     # 只看要不要更新、票池和窗口对不对
    python3 scripts/qlib_update_bin.py               # 真跑（成交额 16:50 并回之后、17:30 选股之前；定时 17:15）
    python3 scripts/qlib_update_bin.py --repair-only # 不追加，只回补窗口里的空洞
    python3 scripts/qlib_update_bin.py --check-plugin  # 顺手让 8003 真出一份名单验日
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent
for p in (str(REPO), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from qlib_export_kline import (  # noqa: E402  口径同源，这里一个常量都不重新定义
    CSV_FIELDS,
    FREQUENCIES,
    dense_frame,
    mongo_db,
    symbol,
    trading_calendar,
)
from stock_selector_qlib import artifacts  # noqa: E402

DUMP_BIN = Path("/data/work/qlib/scripts/dump_bin.py")
# 和当初 dump_all 用的字段集一字不差；vwap 由 amount/volume 现算，缺 amount 时退回 close
INCLUDE_FIELDS = "open,high,low,close,volume,amount,vwap,factor"
BIN_FIELDS = INCLUDE_FIELDS.split(",")
BENCHMARK = "510300"  # 回测基准 SH510300，面板里没有它就没法算超额
PLUGIN_BASE = "http://172.17.0.1:8003"
CSV_UPDATE_DIR = Path("/data/work/qlib_data/csv_update")
SNAPSHOT_ROOT = Path("/data/work/qlib_data/backup_pool")
SNAPSHOT_KEEP = 5
UNIVERSE_FILE = REPO / "data" / "qlib" / "universe.txt"
# 某个市场在某天缺到只剩零头，名单就等于只有另一个市场（2026-10-22 沪市整批缺席即如此）
COVERAGE_MIN = float(os.environ.get("QLIB_COVERAGE_MIN", "0.9"))
# 名单里只有几只票的市场（北交所）一两只停牌就把比例打到 0，判它只是噪音
COVERAGE_MIN_NAMES = int(os.environ.get("QLIB_COVERAGE_MIN_NAMES", "20"))
MARKET_CN = {"SH": "沪市", "SZ": "深市", "BJ": "北交所"}
# 预热那一步是**真的等一次打分**：训练池 300 只约 35 秒，放开全市场 5,000 只实测 550 秒，
# 300 秒的旧超时会在服务端还算对的情况下把脚本判成失败。
CHECK_TIMEOUT = int(os.environ.get("QLIB_CHECK_TIMEOUT", "1200"))

logger = logging.getLogger("qlib_update_bin")


def panel_calendar(qlib_dir: Path) -> list[str]:
    path = qlib_dir / "calendars" / "day.txt"
    if not path.exists():
        raise RuntimeError(f"面板没有交易日历：{path}（先跑一次 dump_all）")
    return [line.strip()[:10] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pool_symbols(scope: str) -> list[str]:
    """增量要补哪批票。scope=pool 用模型产物里的 pool.txt（训练用哪批补哪批）；
    scope=universe 用 scripts/qlib_daily_universe.py 每天重算的全市场名单。
    放开全市场时必须走后者，否则 17:30 会给票池外的票打**上一个交易日**的因子，
    而且分数看着完全正常——这是本脚本头一段写的最难发现的那种坏法。"""
    if scope == "universe":
        if not UNIVERSE_FILE.exists():
            raise RuntimeError(f"缺少全市场名单 {UNIVERSE_FILE}，先跑 scripts/qlib_daily_universe.py")
        syms = [line.strip() for line in UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    else:
        syms = list(artifacts.load().pool)
    bench = symbol(BENCHMARK)
    if bench not in syms:
        syms.append(bench)
    return syms


def export_window(col, codes: list[str], calendar: list[str], lo: int, hi: int,
                  out_dir: Path) -> dict:
    """把 codes 在 calendar[lo:hi+1] 这段稠密地写成 <SYMBOL>.csv，返回行数与覆盖率统计。

    coverage 是「日期 -> 市场(SH/SZ/BJ) -> {have, total}」：total 是这批里属于该市场的只数，
    have 是当天真有收盘价的只数。停牌本来就会让 have 小于 total，所以它是按市场整体看的
    比例，不是逐只票的检查（判据见 coverage_problems）。
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 每次只留这一批，dump_update 会把目录里所有 CSV 都读进去
    out_dir.mkdir(parents=True)
    since = calendar[lo]
    written = empty = dirty_dropped = 0
    coverage: dict[str, dict[str, dict[str, int]]] = {}
    days = calendar[lo:hi + 1]
    for i, code in enumerate(codes, 1):
        rows = list(col.find(
            {"code": code, "frequency": {"$in": FREQUENCIES}, "date": {"$gte": since}},
            {"_id": 0, "date": 1, "crawl_time": 1, "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1, "amount": 1},
        ).sort("date", 1))
        # 与全量导出同一道脏数据过滤：开高低收都得 >0，否则 qlib 的因子链会拿假 K 线往下算
        keep = []
        for r in rows:
            try:
                ok = all(float(r.get(k)) > 0 for k in ("open", "high", "low", "close"))
            except (TypeError, ValueError):
                ok = False
            if ok:
                keep.append(r)
            else:
                dirty_dropped += 1
        frame = dense_frame(keep, calendar, lo, hi)
        market = symbol(code)[:2]
        has_amount = frame["amount"].notna() & (frame["amount"] > 0)
        for day, has_bar, has_amt in zip(days, frame["close"].notna().tolist(),
                                         has_amount.tolist()):
            cell = coverage.setdefault(day, {}).setdefault(
                market, {"have": 0, "total": 0, "have_amount": 0})
            cell["have"] += int(has_bar)
            cell["have_amount"] += int(has_amt)
            cell["total"] += 1
        frame = frame.reset_index()
        frame["date"] = frame["date"].astype(str)
        frame.to_csv(out_dir / f"{symbol(code)}.csv", columns=CSV_FIELDS, index=False,
                     na_rep="", float_format="%.6g")
        bars = int(frame["close"].notna().sum())
        written += 1
        if bars == 0:
            empty += 1
        if i % 100 == 0:
            logger.info("导出窗口进度 %d/%d", i, len(codes))
    return {"files": written, "all_nan": empty, "dirty_dropped": dirty_dropped,
            "rows": len(days), "out_dir": str(out_dir), "coverage": coverage}


def coverage_problems(coverage: dict, dates: list[str], min_ratio: float = COVERAGE_MIN,
                      min_names: int = COVERAGE_MIN_NAMES) -> list[dict]:
    """这几天里哪个市场的哪一列缺到不能出名单。返回的每条都带着分子分母，别只给个百分比。

    两列分开看：`close` 缺 = 那天行情根本没到（名单会缺半个市场）；`amount` 缺而 close 齐
    = 15:45 的快照合并跑在了落库前面，因子链会拿 close 当 vwap 用（`dense_frame` 的兜底），
    分数照出、看起来正常，坏得看不见。一个市场当天已经报了 close 缺就不再报 amount 缺，
    那是同一件事的下游，两条只会让人去查错的地方。
    """
    out = []
    for day in dates:
        for market, cell in sorted((coverage.get(day) or {}).items()):
            total = cell["total"]
            if total < min_names:
                continue
            if cell["have"] / total < min_ratio:
                out.append({"date": day, "market": market, "field": "close",
                            "have": cell["have"], "total": total,
                            "ratio": round(cell["have"] / total, 4)})
                continue
            have_amount = cell.get("have_amount")
            if have_amount is None or total <= 0:
                continue          # 老形状的读数（没这一列）不去猜，交给调用方
            if have_amount / total < min_ratio:
                out.append({"date": day, "market": market, "field": "amount",
                            "have": have_amount, "total": total,
                            "ratio": round(have_amount / total, 4)})
    return sorted(out, key=lambda p: (p["date"], p["ratio"]))


FIELD_CN = {"close": "有日线", "amount": "有成交额"}


def coverage_alert_text(problems: list[dict]) -> str:
    lines = []
    for p in problems:
        name = MARKET_CN.get(p["market"], p["market"])
        what = FIELD_CN.get(p["field"], p["field"])
        lines.append(f"{p['date']} {name}（{p['market']}）只有 {p['have']}/{p['total']} 只"
                     f"{what}（{p['ratio'] * 100:.1f}%）")
    return "\n".join(lines)


def merge_coverage(dest: dict, src: dict) -> dict:
    """分批导出时每批只知道自己那几只票，按「日 × 市场」把只数加回去。"""
    for day, markets in src.items():
        for market, cell in markets.items():
            total = dest.setdefault(day, {}).setdefault(
                market, {"have": 0, "total": 0, "have_amount": 0})
            total["have"] += cell["have"]
            total["have_amount"] += cell.get("have_amount", 0)
            total["total"] += cell["total"]
    return dest


def snapshot(qlib_dir: Path, syms: list[str], dest: Path) -> Path:
    """只备份票池那批 features 目录 + 日历/清单，出错能原样放回去。

    为什么不全盘备份：整个面板 518 MB，而 update 只会往票池这 301 只的 .bin 尾部追加，
    票池的 features 加起来约 30 MB，天天存 5 份也才 150 MB。
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "calendars").mkdir(parents=True, exist_ok=True)
    (dest / "instruments").mkdir(parents=True, exist_ok=True)
    shutil.copy2(qlib_dir / "calendars" / "day.txt", dest / "calendars" / "day.txt")
    shutil.copy2(qlib_dir / "instruments" / "all.txt", dest / "instruments" / "all.txt")
    feats = dest / "features"
    feats.mkdir(parents=True, exist_ok=True)
    for sym in syms:
        src = qlib_dir / "features" / sym.lower()
        if src.exists():
            shutil.copytree(src, feats / src.name)
    keep = sorted(SNAPSHOT_ROOT.glob("*/"), reverse=True)[:SNAPSHOT_KEEP]
    for old in sorted(SNAPSHOT_ROOT.glob("*/")):
        if old not in keep:
            shutil.rmtree(old, ignore_errors=True)
    return dest


def run_dump_update(csv_dir: Path, qlib_dir: Path, max_workers: int) -> None:
    cmd = [sys.executable, str(DUMP_BIN), "dump_update",
           "--data_path", str(csv_dir), "--qlib_dir", str(qlib_dir),
           "--freq", "day", "--max_workers", str(max_workers),
           "--include_fields", INCLUDE_FIELDS]
    logger.info("dump_update：%s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()[-3:] + (proc.stderr or "").strip().splitlines()[-8:]
    print("\n".join(tail))
    if proc.returncode != 0:
        raise RuntimeError(f"dump_bin dump_update 退出码 {proc.returncode}")
    # dump_bin 把单只票的失败收进 error_code 字典后只打日志、不改退出码，必须自己抓
    if "dump bin errors: {}" not in (proc.stdout or "") + (proc.stderr or ""):
        raise RuntimeError("dump_bin 报了单只票的失败（见上面 error_code），别当成功")


def _write_bin_atomically(path: Path, values: np.ndarray) -> None:
    """整只 .bin 一次替换：中途崩了不能留下半截文件（qlib 读它就是按字节偏移读的）。"""
    tmp = path.with_name(path.name + ".tmp")
    values.astype("<f").tofile(str(tmp))
    os.replace(tmp, path)


def fill_holes(qlib_dir: Path, csv_dir: Path, calendar: list[str],
               fields: list[str]) -> dict:
    """用窗口 CSV 回补 .bin 里的 NaN 格子——只填空格，已有读数一个字都不改。

    bin 的形状是「首格 = 这天在面板日历里的下标，后面连续一格一天」，所以某天的格子
    要么已经存在（值为 NaN，可以补），要么根本在 bin 的范围外。**范围外的一律不追加**：
    往后垫一格会把这只票之后每一天都挪一位，那是比空洞严重得多的坏法，只能计数报出来。

    一个例外：`vwap` 是 `amount/volume` 推出来的派生列，不是独立读数。缺 amount 的那天
    `dense_frame` 会把 vwap 兜底成 close 写进面板，第二天 amount 补回来了（那格是 NaN），
    vwap 那格却因为「已经有值」而永久留着 close ⇒ VWAP0=`$vwap/$close` 被钉在 1.0。
    所以凡是这次补了 amount 的 (票,日)，同一天的 vwap 一并跟着 CSV 走，`derived` 单独计数。

    日历用面板自己的 day.txt（调用方在追加之后重新读一遍传进来）：bin 的偏移量是照着
    它算的，拿 Mongo 日历当尺子会在两者对不齐时把数写进隔壁那天。
    """
    index_of = {day: i for i, day in enumerate(calendar)}
    filled = derived = out_of_range = no_dir = symbols = 0
    by_date: Counter = Counter()
    derived_dates: Counter = Counter()
    out_of_range_symbols: list[str] = []
    # vwap 排到最后：要先知道这次补了哪些天的 amount，才谈得上跟着改它
    ordered = [f for f in fields if f != "vwap"] + (["vwap"] if "vwap" in fields else [])
    for path in sorted(csv_dir.glob("*.csv")):
        feats = qlib_dir / "features" / path.stem.lower()
        if not feats.is_dir():
            # 面板里没有这只票（新股要走 dump_all/dump_fix，不是这里造文件）
            no_dir += 1
            continue
        frame = pd.read_csv(path, dtype={"date": str})
        positions = [index_of.get(str(d)[:10]) for d in frame["date"]]
        touched = 0
        miss = 0
        amount_days: set[str] = set()
        for field in ordered:
            bin_path = feats / f"{field.lower()}.day.bin"
            if not bin_path.exists() or field not in frame.columns:
                continue
            values = frame[field].to_numpy(dtype="float64")
            arr = np.fromfile(bin_path, dtype="<f")
            start = int(arr[0])
            span = arr.size - 1
            changed = False
            for k, value in zip(positions, values):
                # k 为 None = 这天不在面板日历里，和「在范围外」一样只能报出来，不能猜位置
                if k is None or not np.isfinite(value):
                    continue
                offset = k - start
                if offset < 0 or offset >= span:
                    out_of_range += 1
                    miss += 1
                    continue
                cell = offset + 1
                day = calendar[k]
                if np.isnan(arr[cell]):
                    arr[cell] = np.float32(value)
                    changed = True
                    touched += 1
                    filled += 1
                    by_date[day] += 1
                    if field == "amount":
                        amount_days.add(day)
                elif field == "vwap" and day in amount_days:
                    # 额是这次补的 → 那天原来的 vwap 是 close 兜底，留着就是错数
                    arr[cell] = np.float32(value)
                    changed = True
                    touched += 1
                    derived += 1
                    derived_dates[day] += 1
            if changed:
                _write_bin_atomically(bin_path, arr)
        symbols += int(touched > 0)
        # 补不上的那些要说得出名字：只有一个总数，下一次还是得从头猜是哪只票的 bin 短了
        if miss and len(out_of_range_symbols) < 20:
            out_of_range_symbols.append(path.stem)
    return {"filled": filled, "derived": derived, "by_date": dict(by_date),
            "derived_dates": dict(derived_dates), "out_of_range": out_of_range,
            "out_of_range_symbols": out_of_range_symbols,
            "no_feature_dir": no_dir, "symbols": symbols}


def refresh_plugin(base_url: str) -> dict:
    """让常驻的 8003 丢掉进程内的旧日历缓存，否则它一小时内还会说「今天不是交易日」。"""
    resp = requests.post(f"{base_url.rstrip('/')}/api/admin/refresh", timeout=120)
    resp.raise_for_status()
    return dict(resp.json().get("data") or {})


def check_plugin(base_url: str, expect_date: str, topk: int = 3) -> int:
    """真出一次名单，确认打分用的就是新交易日。返回名单条数。

    这一步同时是 17:30 那轮的预热：缓存键是 (run_id, date)，预热过就不再冷算。
    """
    resp = requests.post(f"{base_url.rstrip('/')}/api/selector/run",
                         json={"strategies": None, "topk": topk}, timeout=CHECK_TIMEOUT)
    resp.raise_for_status()
    rows = list(resp.json().get("data") or [])
    if not rows:
        raise RuntimeError("插件返回空名单")
    bad = sorted({r["asof"] for r in rows if r["asof"] != expect_date})
    if bad:
        raise RuntimeError(f"插件名单日期 {bad} 与面板最新日 {expect_date} 不一致（缓存没清干净）")
    return len(rows)


def alert(title: str, text: str) -> None:
    """失败要出声：面板停在旧日期不会报错，只会让选股名单看起来正常。"""
    try:
        import alert_pusher

        name, url, secret = alert_pusher.resolve_webhook(alert_pusher.DINGTALK, "ding_ops")
        alert_pusher.send(alert_pusher.DINGTALK, url, secret, title, text)
        logger.info("已推钉钉（%s）", name)
    except Exception as exc:  # noqa: BLE001 - 告警发不出去也不能把日志盖掉
        logger.warning("钉钉告警发不出去：%s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--qlib-dir", default=None, help="面板目录，缺省读 qlib_run.QLIB_DIR")
    parser.add_argument("--days", type=int, default=12, help="导出窗口取最近 N 个交易日（要覆盖缺口）")
    parser.add_argument("--csv-dir", default=str(CSV_UPDATE_DIR), help="窗口 CSV 的临时目录")
    parser.add_argument("--max-workers", type=int, default=2, help="dump_bin 的进程数（内存有限，别用默认 16）")
    parser.add_argument("--plugin-base", default=PLUGIN_BASE)
    parser.add_argument("--no-refresh", action="store_true", help="不通知 8003 清缓存")
    parser.add_argument("--check-plugin", action="store_true", help="更新完真跑一次打分验日期（训练池约 35 秒，全市场约 9 分钟；这一步同时预热 17:30 的缓存）")
    parser.add_argument("--no-snapshot", action="store_true", help="不备份票池 bin（省 30 MB/天）")
    parser.add_argument("--dry-run", action="store_true", help="只做检查与导出，不写面板")
    parser.add_argument("--repair-only", action="store_true", help="不追加新交易日，只用窗口 CSV 回补面板里已有的 NaN 空洞")
    parser.add_argument("--no-repair", action="store_true", help="不回补空洞（默认每批追加完就补一次）")
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--scope", choices=("pool", "universe"),
                        default=os.environ.get("QLIB_UPDATE_SCOPE", "pool"),
                        help="pool=只补模型票池那 300 只（现状）；universe=补 data/qlib/universe.txt 的全市场名单。"
                             "也可以由 QLIB_UPDATE_SCOPE 给：根调度器解析 script:// 的正则里字符类没有 =，"
                             "写 --scope=universe 会被静默截成 --scope 而报错，环境变量这条路不会被截")
    parser.add_argument("--chunk", type=int, default=800,
                        help="每批送进 dump_update 的只数；全市场必须分批，见导出那段的注释")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from qlib_run import QLIB_DIR

    qlib_dir = Path(args.qlib_dir or QLIB_DIR)
    csv_dir = Path(args.csv_dir)
    panel = panel_calendar(qlib_dir)
    db = mongo_db()
    col = db["stock_kline"]
    cal = trading_calendar(col)
    syms = pool_symbols(args.scope)
    print(f"面板日历：{len(panel)} 天，止于 {panel[-1]}")
    print(f"库内日历：{len(cal)} 天，止于 {cal[-1]}")
    print(f"名单（scope={args.scope}，chunk={args.chunk}）：{len(syms)} 只（含基准 {symbol(BENCHMARK)}）")

    missing = [d for d in cal if d > panel[-1]]
    if not missing and not args.repair_only:
        print("库里没有比面板更新的日子，无需更新。")
        return 0

    need = max(args.days, len(missing) + 3)
    if need > len(cal):
        raise RuntimeError(f"窗口要 {need} 天，但库里一共只有 {len(cal)} 个交易日")
    hi = len(cal) - 1
    lo = hi - need + 1
    if missing and cal[lo] > missing[0]:
        raise RuntimeError(f"窗口起点 {cal[lo]} 晚于缺口首日 {missing[0]}，会漏掉 {missing[0]} 之后的对齐")
    if missing:
        print(f"待补交易日 {len(missing)} 天：{missing[0]} → {missing[-1]}；窗口 {cal[lo]} → {cal[hi]}（{need} 天）")
    else:
        print(f"只补洞（--repair-only）：窗口 {cal[lo]} → {cal[hi]}（{need} 天），面板止于 {panel[-1]}")

    if args.dry_run:
        print("dry-run：到此为止，不导出也不写面板。")
        return 0

    try:
        snap = None
        if not args.no_snapshot:
            snap = snapshot(qlib_dir, syms, SNAPSHOT_ROOT / f"{panel[-1]}_{pd.Timestamp.now():%Y%m%d%H%M%S}")
            print(f"票池 bin 已备份到 {snap}")

        # 分批：dump_bin 的 DumpDataUpdate 会把 csv 目录里的**所有**文件一次性 concat 进内存
        # （源码注释自己写着 "NOTE: Need more memory"），所以全市场名单必须切着喂，
        # 每批导完立刻落盘，临时目录里永远只留这一批。
        chunks = [syms[i: i + args.chunk] for i in range(0, len(syms), args.chunk)]
        totals = {"files": 0, "all_nan": 0, "dirty_dropped": 0}
        repair = {"filled": 0, "derived": 0, "out_of_range": 0, "no_feature_dir": 0,
                  "symbols": 0}
        repair_dates: Counter = Counter()
        derived_dates: Counter = Counter()
        repair_unplaceable: list[str] = []
        coverage: dict = {}
        for ci, chunk in enumerate(chunks, 1):
            stats = export_window(col, [s[2:] for s in chunk], cal, lo, hi, csv_dir)
            for key in totals:
                totals[key] += stats[key]
            merge_coverage(coverage, stats["coverage"])
            print(f"[{ci}/{len(chunks)}] 批次 {len(chunk)} 只："
                  + ", ".join(f"{k}={stats[k]}" for k in ("files", "all_nan", "dirty_dropped")))
            if not stats["files"]:
                raise RuntimeError("一只票都没导出来，票池代码和库里的 code 对不上？")
            if args.repair_only:
                print(f"[{ci}/{len(chunks)}] --repair-only：跳过 dump_update，不追加新行")
            else:
                run_dump_update(csv_dir, qlib_dir, args.max_workers)
            if not args.no_repair:
                # 必须在 run_dump_update 之后、下一批把临时目录清掉之前：这批的 CSV 还在，
                # 而上一步刚把新交易日追加进日历，两边对得上的空洞才补得动。
                rep = fill_holes(qlib_dir, csv_dir, panel_calendar(qlib_dir), BIN_FIELDS)
                for key in repair:
                    repair[key] += rep[key]
                repair_dates.update(rep["by_date"])
                derived_dates.update(rep["derived_dates"])
                repair_unplaceable.extend(rep["out_of_range_symbols"])
        print(f"窗口导出合计：{totals}，窗口行数={stats['rows']}")
        if not args.no_repair:
            print("回补空洞：" + ", ".join(f"{k}={repair[k]}" for k in repair)
                  + (f"，按日 {dict(sorted(repair_dates.items()))}" if repair_dates else "（没有可补的格子）"))
            if repair["derived"]:
                print(f"  跟着补回的 amount 一起改对的 vwap：{repair['derived']} 格，按日 "
                      f"{dict(sorted(derived_dates.items()))}")
            if repair_unplaceable:
                print(f"  bin 范围外、这次没补上的 {repair['out_of_range']} 格，涉及 "
                      f"{len(set(repair_unplaceable))} 只（最多列 20 只/批）："
                      f"{sorted(set(repair_unplaceable))[:20]}")

        new_panel = panel_calendar(qlib_dir)
        if new_panel[-1] != cal[-1]:
            raise RuntimeError(f"面板更新到 {new_panel[-1]}，但库里最新是 {cal[-1]}")
        print(f"面板日历：{panel[-1]} → {new_panel[-1]}（+{len(new_panel) - len(panel)} 天）")

        if not args.no_refresh:
            info = refresh_plugin(args.plugin_base)
            print(f"8003 已刷新：{info}")
            if str(info.get("calendar_end")) != cal[-1]:
                raise RuntimeError(f"8003 看到的最后一天是 {info.get('calendar_end')}，不是 {cal[-1]}")
        if args.check_plugin:
            n = check_plugin(args.plugin_base, cal[-1])
            print(f"8003 打分自检通过：{n} 条名单，日期 {cal[-1]}")

        # 覆盖率的账要出名单之前算完才管用：只补洞（没有新交易日）时看的就是名单真正
        # 用的那一天（面板最后一天），否则看这次追加进来的那几天。
        gate_days = missing or [cal[-1]]
        problems = coverage_problems(coverage, gate_days)
        if problems:
            text = coverage_alert_text(problems)
            print("★ 日线覆盖率缺口：\n" + text)
            extra = ""
            if any(p["field"] == "amount" for p in problems):
                extra = ("\n这一列缺的是成交额（有价无额）：先查 merge_snapshot_amount "
                         "是不是跑在行情落库前面——vwap 由 amount/volume 算，"
                         "缺额那天会兜底成 close，补回 amount 时这一步会跟着把 vwap 改对。")
            if not args.no_alert:
                alert("qlib 面板日线缺口",
                      f"{text}\n面板已写到 {cal[-1]}，缺的那些格子按 NaN 落进了 bin，"
                      "下一次增量会再试着补一次（只填 NaN、不动已有读数）。"
                      f"{extra}\n回补结果：{repair}，按日 "
                      f"{dict(sorted(repair_dates.items())) if repair_dates else '无'}\n"
                      "17:30 的名单可能只剩半个市场，别当全市场结论用。")
            return 1
        print("更新完成。")
        return 0
    except Exception as exc:  # noqa: BLE001 - 调度器只看退出码，坏在要说什么坏在哪
        print(f"★ qlib 面板更新失败：{exc}")
        if snap is not None:
            print(f"  面板回滚件在 {snap}（把 features/calendars/instruments 拷回去即可）")
        if not args.no_alert:
            alert("qlib 面板更新失败", f"{exc}\n面板止于 {panel_calendar(qlib_dir)[-1]}，库里已到 {cal[-1]}\n"
                                       "17:30 的选股名单会继续停在旧交易日，先别当今天的用。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
