"""从本地成分股快照建 qlib 的 instruments 票池文件（沪深300 / 中证500）。

为什么不是"照原样用 csi300"：qlib 官方 cn_data 里的 `instruments/csi300.txt` 是**带时间区间的历史成分**
（谁在哪段时间在指数里），本机只有 `data/csv/hs300_stocks.csv` 这一份**单日快照**（2026-05-24，无进出日期）。
拿快照跑历史 = 用"今天已经长大的票"回测 2016 年 ⇒ **前视偏差，读数会系统性偏高**。
所以这份文件产出的池只用于「看 qlib 默认配置在它的票池口径下报多少 IC」，
不能用来裁决线上换不换（裁决要用 `qlib_run.py` 那条按训练段流动性挑池的链路）。

写出来的格式和 qlib 官方一致：每行 `SYMBOL<TAB>start<TAB>end`，整池共用一个时间区间。

用法:
    python3 scripts/qlib_make_index_pool.py                 # 两份都建，打印建池结果
    python3 scripts/qlib_make_index_pool.py --index hs300 --end 2026-10-16
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
SNAP_DIR = REPO / "data" / "csv"
INSTRUMENTS = Path("/data/work/qlib_data/cn/instruments")
CSV_PANEL = Path("/data/work/qlib_data/csv")

INDEXES = {
    "hs300": ("hs300_stocks.csv", "csi300"),
    "zz500": ("zz500_stocks.csv", "csi500"),
}


def prefixed(code: str) -> str:
    """6 位代码 → qlib symbol。60/68 归沪市，00/30 归深市；其它前缀（8xx/4xx 北交所）不认，
    本面板里也没有它们的日线，留着只会让 qlib 在加载阶段报缺数据。"""
    if code.startswith(("60", "68")):
        return f"SH{code}"
    if code.startswith(("00", "30")):
        return f"SZ{code}"
    return ""


def name_to_code() -> dict[str, str]:
    """`all_stock.csv` 的 名称→纯数字代码，用来修成分股快照里的脏码。

    快照实测有一处写错（`202475,立讯精密`，真码 002475）——按名字回查权威全表比丢掉这只票好，
    也比猜前缀诚实：修不动的（名字也不在全表里）照样进 skipped 清单，不静默吞掉。
    """
    frame = pd.read_csv(SNAP_DIR / "all_stock.csv", dtype=str)
    out: dict[str, str] = {}
    for code, name in zip(frame["code"], frame["code_name"]):
        code = str(code).strip()
        if "." in code:  # 形如 sh.000001 / sz.002475，指数与股票同表
            market, digits = code.split(".", 1)
            if market.lower() in ("sh", "sz"):
                out.setdefault(str(name).strip(), digits)
    return out


def build(index: str, start: str, end: str) -> dict:
    snap, market = INDEXES[index]
    path = SNAP_DIR / snap
    if not path.exists():
        raise SystemExit(f"没有快照文件 {path}")
    names = pd.read_csv(path, dtype=str)
    codes = names["code"].str.strip()
    pairs = list(zip(codes, names["name"].astype(str).str.strip()))
    by_name = name_to_code()
    symbols, repaired, skipped, missing_data = [], [], [], []
    for code, name in dict.fromkeys(pairs):  # 去重但保持原顺序（快照里 600606 重复了一行）
        symbol = prefixed(code)
        if not symbol:
            fixed = by_name.get(name, "")
            symbol = prefixed(fixed)
            if symbol:
                repaired.append(f"{code}({name})→{fixed}")
        if not symbol:
            skipped.append(f"{code}({name})")
            continue
        if not (CSV_PANEL / f"{symbol}.csv").exists():
            missing_data.append(symbol)
            continue
        symbols.append(symbol)
    out = INSTRUMENTS / f"{market}.txt"
    out.write_text("".join(f"{s}\t{start}\t{end}\n" for s in symbols), encoding="utf-8")
    return {
        "market": market,
        "file": str(out),
        "snapshot": str(path),
        "rows_in_snapshot": int(len(codes)),
        "unique_codes": len(set(codes)),
        "usable": len(symbols),
        "repaired": repaired,
        "skipped": skipped,
        "missing_in_panel": missing_data,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--index", default="hs300,zz500", help="hs300 / zz500 / 逗号分隔两个都要")
    ap.add_argument("--start", default="2015-01-05", help="票池生效区间左端（面板日历起点）")
    ap.add_argument("--end", default="2026-10-16", help="右端（面板日历末尾）")
    args = ap.parse_args()

    for index in [x.strip() for x in args.index.split(",") if x.strip()]:
        info = build(index, args.start, args.end)
        print(f"{info['market']}: 快照 {info['rows_in_snapshot']} 行 → 可用 {info['usable']} 只"
              f"（面板缺数据 {len(info['missing_in_panel'])}、认不出 {len(info['skipped'])}）→ {info['file']}")
        if info["repaired"]:
            print(f"  按 all_stock.csv 修回脏码: {info['repaired']}")
        if info["skipped"]:
            print(f"  修不动、已剔除: {info['skipped']}")
        if info["missing_in_panel"]:
            print(f"  面板里没有: {info['missing_in_panel'][:10]}"
                  f"{' …' if len(info['missing_in_panel']) > 10 else ''}"
                  "（多为退市/摘牌，快照里还挂着名字）")
    print("⚠ 这是单日快照池，跑历史带前视偏差：读数只当「qlib 默认配置在它口径下的样子」，别用来裁决上线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
