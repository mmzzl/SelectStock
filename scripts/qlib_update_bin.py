"""收盘后把票池当天的日线增量写进 qlib 的 bin 面板（选股链路的第一环）。

为什么必须有这一步：8003 打分读的是 /data/work/qlib_data/cn 下的 .bin，不读 Mongo。
爬虫 15:10 把当天 K 线落到 Mongo、15:45 merge_snapshot_amount 把成交额并回去，bin 面板
要是不跟着走，17:30 的选股就会一直出**上一个交易日**的名单，而且它不报错——看起来
就像「今天还是这几只上榜」，这是最难发现的一种坏法。

四条必须照做的口径（前两条与 scripts/qlib_export_kline.py 的头一份文档同源）：
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

用法：
    python3 scripts/qlib_update_bin.py --dry-run     # 只看要不要更新、票池和窗口对不对
    python3 scripts/qlib_update_bin.py               # 真跑（15:45 之后、17:30 选股之前）
    python3 scripts/qlib_update_bin.py --check-plugin  # 顺手让 8003 真出一份名单验日
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent
for p in (str(REPO), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

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
BENCHMARK = "510300"  # 回测基准 SH510300，面板里没有它就没法算超额
PLUGIN_BASE = "http://172.17.0.1:8003"
CSV_UPDATE_DIR = Path("/data/work/qlib_data/csv_update")
SNAPSHOT_ROOT = Path("/data/work/qlib_data/backup_pool")
SNAPSHOT_KEEP = 5

logger = logging.getLogger("qlib_update_bin")


def panel_calendar(qlib_dir: Path) -> list[str]:
    path = qlib_dir / "calendars" / "day.txt"
    if not path.exists():
        raise RuntimeError(f"面板没有交易日历：{path}（先跑一次 dump_all）")
    return [line.strip()[:10] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pool_symbols() -> list[str]:
    """票池取模型产物里的 pool.txt，再加基准——训练用哪批票，增量就补哪批票。"""
    syms = list(artifacts.load().pool)
    bench = symbol(BENCHMARK)
    if bench not in syms:
        syms.append(bench)
    return syms


def export_window(col, codes: list[str], calendar: list[str], lo: int, hi: int,
                  out_dir: Path) -> dict:
    """把 codes 在 calendar[lo:hi+1] 这段稠密地写成 <SYMBOL>.csv，返回行数统计。"""
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 每次只留这一批，dump_update 会把目录里所有 CSV 都读进去
    out_dir.mkdir(parents=True)
    since = calendar[lo]
    written = empty = dirty_dropped = 0
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
            "rows": len(calendar[lo:hi + 1]), "out_dir": str(out_dir)}


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


def refresh_plugin(base_url: str) -> dict:
    """让常驻的 8003 丢掉进程内的旧日历缓存，否则它一小时内还会说「今天不是交易日」。"""
    resp = requests.post(f"{base_url.rstrip('/')}/api/admin/refresh", timeout=120)
    resp.raise_for_status()
    return dict(resp.json().get("data") or {})


def check_plugin(base_url: str, expect_date: str, topk: int = 3) -> int:
    """真出一次名单，确认打分用的就是新交易日。返回名单条数。"""
    resp = requests.post(f"{base_url.rstrip('/')}/api/selector/run",
                         json={"strategies": None, "topk": topk}, timeout=300)
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
    parser.add_argument("--check-plugin", action="store_true", help="更新完真跑一次打分验日期（约 35 秒）")
    parser.add_argument("--no-snapshot", action="store_true", help="不备份票池 bin（省 30 MB/天）")
    parser.add_argument("--dry-run", action="store_true", help="只做检查与导出，不写面板")
    parser.add_argument("--no-alert", action="store_true")
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
    syms = pool_symbols()
    codes = [s[2:] for s in syms]
    print(f"面板日历：{len(panel)} 天，止于 {panel[-1]}")
    print(f"库内日历：{len(cal)} 天，止于 {cal[-1]}")
    print(f"票池：{len(syms)} 只（含基准 {symbol(BENCHMARK)}）")

    if cal[-1] <= panel[-1]:
        print("库里没有比面板更新的日子，无需更新。")
        return 0

    missing = [d for d in cal if d > panel[-1]]
    need = max(args.days, len(missing) + 3)
    if need > len(cal):
        raise RuntimeError(f"窗口要 {need} 天，但库里一共只有 {len(cal)} 个交易日")
    hi = len(cal) - 1
    lo = hi - need + 1
    if cal[lo] > missing[0]:
        raise RuntimeError(f"窗口起点 {cal[lo]} 晚于缺口首日 {missing[0]}，会漏掉 {missing[0]} 之后的对齐")
    print(f"待补交易日 {len(missing)} 天：{missing[0]} → {missing[-1]}；窗口 {cal[lo]} → {cal[hi]}（{need} 天）")

    if args.dry_run:
        print("dry-run：到此为止，不导出也不写面板。")
        return 0

    try:
        stats = export_window(col, codes, cal, lo, hi, csv_dir)
        print("窗口导出：" + ", ".join(f"{k}={v}" for k, v in stats.items()))
        if not stats["files"]:
            raise RuntimeError("一只票都没导出来，票池代码和库里的 code 对不上？")

        snap = None
        if not args.no_snapshot:
            snap = snapshot(qlib_dir, syms, SNAPSHOT_ROOT / f"{panel[-1]}_{pd.Timestamp.now():%Y%m%d%H%M%S}")
            print(f"票池 bin 已备份到 {snap}")

        run_dump_update(csv_dir, qlib_dir, args.max_workers)

        new_panel = panel_calendar(qlib_dir)
        if new_panel[-1] != cal[-1]:
            raise RuntimeError(f"面板更新到 {new_panel[-1]}，但库里最新是 {cal[-1]}")
        print(f"面板日历已延长：{panel[-1]} → {new_panel[-1]}（+{len(new_panel) - len(panel)} 天）")

        if not args.no_refresh:
            info = refresh_plugin(args.plugin_base)
            print(f"8003 已刷新：{info}")
            if str(info.get("calendar_end")) != cal[-1]:
                raise RuntimeError(f"8003 看到的最后一天是 {info.get('calendar_end')}，不是 {cal[-1]}")
        if args.check_plugin:
            n = check_plugin(args.plugin_base, cal[-1])
            print(f"8003 打分自检通过：{n} 条名单，日期 {cal[-1]}")
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
