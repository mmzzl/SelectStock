"""把 Mongo 修好的那几根日线传进 qlib 面板的 `.bin`（docs §48.5，任务 #89）。

为什么必须单独做这一步：8003 打分、回测、训练读的都是 `/data/work/qlib_data/cn/features/*/*.day.bin`，
**不读 Mongo**；而 qlib 的 `dump_update` 只对已有票追加「比结束日更晚」的行（读源码确认过），
已有格子它不碰。所以 Mongo 改完不传播等于没改——实测 2026-10-29 修完之后面板还留着 `SZ300803`
2026-09-21 的假收盘 56.862（真裸价 82.45）。

`.bin` 的形状：首格 = 这只票第一天在面板日历里的下标，之后连续一格一天。
所以定点改格只能按 `下标 − 首格 + 1` 定位；算错一位就把这只票之后每一天都挪一格，
那比一根假收盘坏得多。本脚本因此：**只在格子里已经有数（含 NaN）时改值，范围外一律拒绝**，
整只文件原子替换（`_write_bin_atomically` 复用 `qlib_update_bin.py` 那一份），改前把原文件备份。

默认 dry-run 只报「哪一格现值多少、要改成多少」，`--apply` 才写。写完用 qlib 读回这几只复核。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qlib_update_bin import _write_bin_atomically, panel_calendar  # noqa: E402

QLIB_DIR = Path("/data/work/qlib_data/cn")
PRICE_FIELDS = ("open", "high", "low", "close")
SH_FIRST = set("569")
CELL_TOL = 1e-4          # float32 存的，比这个小的差不算差（别把精度噪声报成改动）


def instrument(code: str) -> str:
    return f"SH{code}" if code[:1] in SH_FIRST else f"SZ{code}"


def vwap_of(row: dict):
    """面板自己的口径：vwap = amount/volume。没额就回 None，不硬编成 close。"""
    amount, volume = row.get("amount"), row.get("volume")
    if amount is None or volume in (None, 0, 0.0):
        return None
    return float(amount) / float(volume)


def cell_of(arr: np.ndarray, index_of: dict, day: str):
    """这天在这只票 bin 里的格位；不在范围内返回 None（宁可报出来，不猜位置）。"""
    k = index_of.get(day)
    if k is None:
        return None
    offset = k - int(arr[0])
    if offset < 0 or offset >= arr.size - 1:
        return None
    return offset + 1


def patch(arr: np.ndarray, index_of: dict, day_values: dict) -> dict:
    """按日历下标改格。返回 {changed, same, out_of_range}；首格永不改，范围外拒绝。"""
    changed, same, out_of_range = 0, 0, []
    for day, value in sorted(day_values.items()):
        cell = cell_of(arr, index_of, day)
        if cell is None or value is None:
            out_of_range.append(day)          # 日历不认这天，或这只票的 bin 没覆盖到
            continue
        cur = float(arr[cell])
        if abs(cur - float(value)) <= CELL_TOL * max(abs(float(value)), 1.0):
            same += 1
            continue
        arr[cell] = np.float32(value)
        changed += 1
    return {"changed": changed, "same": same, "out_of_range": out_of_range}


def load_targets(path: Path) -> dict:
    """`kline_seam_repair` 的 .applied.jsonl → {instrument: {field: {day: value}}}。"""
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if not rec.get("matched"):
            continue
        inst = instrument(rec["code"])
        day = str(rec["date"])[:10]
        bucket = out.setdefault(inst, {})
        for field in PRICE_FIELDS:
            if rec["new"].get(field) is not None:
                bucket.setdefault(field, {})[day] = float(rec["new"][field])
    return out


def apply_patches(qlib_dir: Path, targets: dict, *, write: bool, backup: Path) -> dict:
    index_of = {d: i for i, d in enumerate(panel_calendar(qlib_dir))}
    report = {"files": 0, "cells": 0, "same": 0, "out_of_range": []}
    for inst, fields in sorted(targets.items()):
        for field, day_values in sorted(fields.items()):
            bin_path = qlib_dir / "features" / inst.lower() / f"{field}.day.bin"
            if not bin_path.exists():
                report["out_of_range"].append(f"{inst}.{field}: 没这个文件")
                continue
            arr = np.fromfile(bin_path, dtype="<f")
            before = {d: float(arr[cell_of(arr, index_of, d)])
                      for d in day_values if cell_of(arr, index_of, d) is not None}
            got = patch(arr, index_of, day_values)
            report["cells"] += got["changed"]
            report["same"] += got["same"]
            report["out_of_range"].extend(f"{inst}.{field}:{d}" for d in got["out_of_range"])
            for d, cur in before.items():
                want = day_values[d]
                mark = "写" if write and got["changed"] else "dry"
                print(f"  [{mark}] {inst} {field} {d} 现 {cur:.4f} → 要 {want:.4f}")
            if got["changed"] and write:
                backup.mkdir(parents=True, exist_ok=True)
                dest = backup / inst.lower() / f"{field}.day.bin"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bin_path, dest)
                _write_bin_atomically(bin_path, arr)
                report["files"] += 1
    return report


def verify(qlib_dir: Path, targets: dict) -> list[str]:
    """写完用 qlib 读回这几格：$close 要对上 Mongo 修好的值，$vwap 要等于 amount/volume。

    后一条是防 §31 那个 VWAP0 退化的老口子：改过收盘价的那格，vwap 不能还是 close 兜底。
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(qlib_dir), kernels=1)
    bad = []
    for inst, fields in targets.items():
        days = sorted(fields.get("close", {}))
        if not days:
            continue
        frame = D.features([inst], ["$close", "$vwap", "$amount", "$volume"],
                           start_time=days[0], end_time=days[-1])
        frame.index = [str(t)[:10] for t in frame.index.get_level_values(1)]
        for d, want in fields["close"].items():
            got = frame["$close"].get(d)
            if got is None or abs(float(got) - want) > 0.02:
                bad.append(f"{inst} {d}: qlib 读回 close={got}，要 {want}")
                continue
            expect = vwap_of({"amount": frame["$amount"].get(d),
                              "volume": frame["$volume"].get(d)})
            now = frame["$vwap"].get(d)
            if expect and (now is None or abs(float(now) - expect) > max(0.02, abs(expect) * 0.01)):
                bad.append(f"{inst} {d}: vwap={now}，按面板口径应为 {expect:.4f}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", required=True,
                    help="`kline_seam_repair` 产出的 .applied.jsonl（票·日 + 新值）")
    ap.add_argument("--qlib-dir", default=str(QLIB_DIR))
    ap.add_argument("--apply", action="store_true", help="真写 .bin（默认只报现值/要值）")
    ap.add_argument("--backup", default=None, help="原 .bin 备份目录（默认同一次时间戳目录）")
    args = ap.parse_args(argv)

    qlib_dir = Path(args.qlib_dir)
    targets = load_targets(Path(args.src))
    print(f"[panel] 目标 {len(targets)} 只票、"
          f"{sum(len(v) for t in targets.values() for v in t.values())} 格")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup = Path(args.backup) if args.backup else ROOT / "data" / "qlib_panel_backup" / stamp
    report = apply_patches(qlib_dir, targets, write=args.apply, backup=backup)
    print(f"  改格 {report['cells']}、本来就一致 {report['same']}、写文件 {report['files']} 只"
          f"、范围外/缺文件 {len(report['out_of_range'])}")
    for x in report["out_of_range"][:10]:
        print(f"    范围外：{x}")
    if args.apply:
        bad = verify(qlib_dir, targets)
        print(f"  qlib 读回复核：{'全部对上' if not bad else '对不上 ' + str(len(bad)) + ' 格'}")
        for x in bad[:10]:
            print(f"    {x}")
        print(f"  原 .bin 备份在 {backup}")
    else:
        print("  DRY-RUN：没动面板（要写加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
