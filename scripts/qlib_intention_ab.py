"""批次 S 读数（只读）：QuantPosition 那套「主力意图」判据，在我们的池子里到底有没有前向信息。

用法：
    python3 -u scripts/qlib_intention_ab.py > /tmp/intention_ab.log 2>&1 &

口径（docs §29.2 写死的，读数出来不许回头改）：
- 信号日 = 分时数据覆盖的 61 个交易日（2026-07-06→2026-09-29）；当日判定只数不足中位数一半的天剔掉并写出来。
- 臂 A = 线上模型 `batchQ_lab10` 留出档案的 300 只训练池，逐日按档案分数取前 30（App 那张名单）/
  前 100（他设想的二次打分段）；基准 = 当日 300 池等权（与 §26/§27 同口径）。
  臂 B = 当日真有 5 分钟线的全市场票，基准 = 当日这些票等权（只给量级，不单独判「接」）。
- 判定输入：日线四层只用 ≤T 的行（qlib 前复权面板，最后 40 个交易日）；分时只用 T 当日那批 bar。
- 入场 = T 收盘；前向 = T+5 / T+10 收盘；成本一笔来回 0.20%；分组差用逐日差值序列的 t 再 ÷√10。
- 门槛（§29.3）：|t÷√10| ≥ 2、每组 ≥3,000 只·日、前 30 名单里该意图日均 ≥3 席，三条同时满足才判「够格接」。
- 不发钉钉、不写生产库、不动线上名单；明细只落 /tmp。
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from stock_selector_qlib import artifacts, intention, serving  # noqa: E402

BEG, END = "2026-07-06", "2026-09-29"
HOLDS = (5, 10)
COST = 0.002           # 买 0.05% + 卖 0.15%，一笔来回
LOOKBACK = 40          # 日线四层喂多少个交易日（原作者取 60 自然日 ≈ 40 个交易日）
MIN_BARS = 20          # 分时少于 20 根就不判（原代码形态层本身也要 ≥20 根）
MIN_DAILY = 10         # 日线少于 10 根量能层就是「数据不足」，不如不判
TOP = (30, 100)
BUY = ("吸筹", "洗盘")
SELL = ("真出货", "出货风险")
ORDER = ["吸筹", "洗盘", "假出货诱空", "震荡", "高位震荡", "出货风险", "真出货"]
FIELDS = ["$open", "$high", "$low", "$close", "$volume"]


def to_instrument(code: str) -> str:
    if code.startswith(("60", "68", "90", "99")):
        return f"SH{code}"
    if code.startswith(("00", "30", "12", "16")):
        return f"SZ{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return ""


def mongo_db():
    from pymongo import MongoClient

    from stock_selector.datasource.mongo_source import _load_mongo_config

    cfg = _load_mongo_config()
    client = MongoClient(host=cfg.get("host", "127.0.0.1"),
                         port=int(cfg.get("port", 27017)),
                         serverSelectionTimeoutMS=5000)
    return client, client[cfg.get("database", "eastmoney_news")]


def panels(codes: list[str]) -> pd.DataFrame:
    """一次把这段区间的日线收盘价拉成宽表（日期 × 代码），判定和收益都用它。"""
    import qlib.data

    cal = serving.calendar()
    start = cal[max(0, cal.index(BEG) - LOOKBACK - 5)]
    end = cal[min(len(cal) - 1, cal.index(END) + max(HOLDS) + 5)]
    t0 = time.time()
    frame = qlib.data.D.features(list(codes), FIELDS, start_time=start, end_time=end)
    out = {}
    for field in FIELDS:
        wide = frame[field].unstack("instrument")
        wide.index = pd.to_datetime(wide.index)
        out[field] = wide.sort_index()
    close = out["$close"]
    print(f"日线面板：{len(codes):,} 只请求 / {close.shape[1]:,} 只有数据 × {len(close)} 个交易日 "
          f"（{start}→{end}），{time.time() - t0:.0f} s", flush=True)
    return out


def day_bars(db, code: str) -> dict[str, list[dict]]:
    """单只票这段区间的分时，按日切好（走 code_1_date_1 索引，逐日约 48 根）。"""
    cur = db["stock_kline_5m"].find(
        {"code": code, "date": {"$gte": f"{BEG} 00:00", "$lte": f"{END} 23:59"}},
        {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ).sort("date", 1)
    out: dict[str, list[dict]] = defaultdict(list)
    for row in cur:
        out[row["date"][:10]].append(row)
    return out


def judge_all(instruments: list[str], pan: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    """逐票扫分时 → 判定，只留轻量记录；同时统计覆盖与自查结果。参数是 qlib 代码（SH600000 这种）。"""
    client, db = mongo_db()
    days = list(pan["$close"].index)
    day_str = [d.strftime("%Y-%m-%d") for d in days]
    pos = {d: i for i, d in enumerate(day_str)}
    day_arr = np.array(day_str)
    close, op = pan["$close"], pan["$open"]
    hi, lo, vol = pan["$high"], pan["$low"], pan["$volume"]

    rows: list[dict] = []
    stats = defaultdict(int)
    t0 = time.time()
    for n, inst in enumerate(instruments, 1):
        code = inst[2:]
        if inst not in close.columns:
            stats["no_panel"] += 1
            continue
        bars_by_day = day_bars(db, code)
        if not bars_by_day:
            stats["no_5m"] += 1
            continue
        c, o, h, l, v = (close[inst].to_numpy(), op[inst].to_numpy(), hi[inst].to_numpy(),
                         lo[inst].to_numpy(), vol[inst].to_numpy())
        alive = ~np.isnan(c) & ~np.isnan(o) & ~np.isnan(h) & ~np.isnan(l) & (v > 0)
        idx_alive = np.flatnonzero(alive)
        for day, bars in sorted(bars_by_day.items()):
            if len(bars) < MIN_BARS:
                stats["few_bars"] += 1
                continue
            t = pos.get(day)
            if t is None:
                stats["not_a_trading_day"] += 1
                continue
            window = idx_alive[idx_alive <= t][-LOOKBACK:]        # 位置切片
            # 自查：换一条路（对日期数组二分）再切一次，两次必须完全一致，且最后一行不许晚于 T
            j = int(np.searchsorted(day_arr, day, side="right")) - 1
            alt = idx_alive[idx_alive <= j][-LOOKBACK:]
            stats["judged"] += 1
            if not np.array_equal(window, alt):
                stats["selfcheck_slice_mismatch"] += 1
            if day_str[window[-1]] > day:
                stats["selfcheck_future_row"] += 1
            if len(window) < MIN_DAILY:
                stats["few_daily"] += 1
                continue
            daily = [{"date": day_str[i], "open": float(o[i]), "high": float(h[i]),
                      "low": float(l[i]), "close": float(c[i]), "volume": float(v[i])}
                     for i in window]
            res = intention.judge(daily, bars)
            if res is None:
                stats["no_judgement"] += 1
                continue
            rows.append({"day": day, "instrument": inst, "code": code, **res})
        if n % 800 == 0:
            print(f"  扫过 {n:,}/{len(instruments):,} 只，已判定 {len(rows):,} 只·日 "
                  f"（{time.time() - t0:.0f} s）", flush=True)
    client.close()
    print(f"判定完成：{stats['judged']:,} 只·日进判定器，出结果 {len(rows):,} 行，"
          f"{time.time() - t0:.0f} s", flush=True)
    return pd.DataFrame(rows), dict(stats)


def add_excess(frame: pd.DataFrame, fwd: dict[int, pd.DataFrame],
               base_pop: dict[str, list[str]]) -> pd.DataFrame:
    """加 ret{h}/ex{h}：ex = 该票 T→T+h 收益 − 当日同人口等权收益。"""
    if not len(frame):
        return frame
    first = fwd[HOLDS[0]]
    day_pos = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(first.index)}
    col_pos = {c: j for j, c in enumerate(first.columns)}
    base = {}
    for day, pop in base_pop.items():
        if day not in day_pos:
            continue
        cols = [col_pos[p] for p in pop if p in col_pos]
        if not cols:
            continue
        for h in HOLDS:
            arr = fwd[h].iloc[day_pos[day], cols].to_numpy(dtype=float)
            arr = arr[~np.isnan(arr)]
            base[(day, h)] = float(arr.mean()) if arr.size else np.nan

    di = frame["day"].map(day_pos)
    ci = frame["instrument"].map(col_pos)
    ok = (di.notna() & ci.notna()).to_numpy()
    di = di.to_numpy(dtype=float)
    ci = ci.to_numpy(dtype=float)
    for h in HOLDS:
        mat = fwd[h].to_numpy()
        r = np.full(len(frame), np.nan)
        r[ok] = mat[di[ok].astype(int), ci[ok].astype(int)]
        frame[f"ret{h}"] = r
        bser = pd.Series([(base.get((d, h), np.nan)) for d in frame["day"]], index=frame.index)
        frame[f"ex{h}"] = r - bser.to_numpy(dtype=float)
    return frame


def group_table(df: pd.DataFrame, title: str) -> None:
    print(f"\n=== {title}（超额=减当日同人口等权；净=毛−0.20%）===")
    print(f"{'意图':<7} {'只·日':>7} {'日均':>5} {'T+5毛':>7} {'T+10毛':>7} {'T+10净':>7} "
          f"{'T+10胜率':>8} {'高置信占比':>9}")
    cats = pd.Categorical(df["intention"], categories=ORDER, ordered=True)
    for name, grp in df.assign(intention=cats).groupby("intention", observed=True):
        ok = grp["ex10"].dropna()
        win = (ok > 0).mean() if len(ok) else np.nan
        print(f"{str(name):<7} {len(grp):>7,} {grp.groupby('day').size().mean():>5.1f} "
              f"{grp['ex5'].mean():>+6.2%} {grp['ex10'].mean():>+6.2%} "
              f"{grp['ex10'].mean() - COST:>+6.2%} {win:>7.1%} "
              f"{(grp['intention_confidence'] == '高').mean():>8.1%}")


def contrast(df: pd.DataFrame, g1: tuple, g2: tuple, label: str) -> None:
    a, b = df[df["intention"].isin(g1)], df[df["intention"].isin(g2)]
    print(f"  {label}：{len(a):,} vs {len(b):,} 只·日"
          f"（日均 {a.groupby('day').size().mean() if len(a) else 0:.1f} 只）")
    if len(a) < 30 or len(b) < 30:
        print("    样本太薄，不出 t")
        return
    for h in HOLDS:
        d = pd.concat([a.groupby("day")[f"ex{h}"].mean(),
                       b.groupby("day")[f"ex{h}"].mean()], axis=1,
                      keys=["g1", "g2"]).dropna()
        diff = d["g1"] - d["g2"]
        sd = float(diff.std())
        t_day = float(diff.mean()) / sd * np.sqrt(len(diff)) if sd else float("nan")
        x, y = a[f"ex{h}"].dropna(), b[f"ex{h}"].dropna()
        se = float(np.sqrt(x.var(ddof=1) / len(x) + y.var(ddof=1) / len(y)))
        t_pool = (float(x.mean()) - float(y.mean())) / se if se else float("nan")
        print(f"    T+{h}: 差 {diff.mean():+.2%}/日（{len(diff)} 个交易日）"
              f"  t(日)={t_day:+.2f}  **t÷√10={t_day / np.sqrt(10):+.2f}**"
              f"  只·日合并 t={t_pool:+.2f}  扣费后 {diff.mean() - COST:+.2%}")


def main() -> int:
    t_all = time.time()
    art = artifacts.load()
    serving.init_qlib()
    pred = pd.read_parquet(REPO / "data" / "qlib" / "pred_batchQ_lab10.parquet")
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    pool = sorted(pred["instrument"].unique())
    print(f"档案：{art.run_id}（train_end {art.meta.get('train_end')}）  {len(pool)} 只训练池",
          flush=True)

    client, db = mongo_db()
    codes5m = sorted(db["stock_kline_5m"].distinct("code"))
    client.close()
    inst5m = {i for i in (to_instrument(c) for c in codes5m) if i}
    universe = sorted(inst5m | set(pool))
    print(f"分时覆盖 {len(codes5m):,} 个代码（能对上 qlib 股票的 {len(inst5m):,} 只，"
          f"其余是基金/指数之类）；日线面板请求 {len(universe):,} 只", flush=True)

    pan = panels(universe)
    df, stats = judge_all(universe, pan)

    day_str = [d.strftime("%Y-%m-%d") for d in pan["$close"].index]
    days = [d for d in day_str if BEG <= d <= END]
    cover = df.groupby("day").size()
    med = float(cover.median())
    keep = [d for d in days if cover.get(d, 0) >= med / 2]
    drop = {d: int(cover.get(d, 0)) for d in days if d not in keep}
    print(f"\n=== 覆盖与自查 ===\n信号日 {len(days)} 天；每日判定只数 中位 {med:.0f} "
          f"最少 {int(cover.min())} 最多 {int(cover.max())}")
    print(f"剔除（不足中位一半）：{drop if drop else '无'}")
    print("计数器：" + "  ".join(f"{k}={v:,}" for k, v in sorted(stats.items())))
    bad = stats.get("selfcheck_slice_mismatch", 0) + stats.get("selfcheck_future_row", 0)
    print(f"前视自查：切片不一致 {stats.get('selfcheck_slice_mismatch', 0)} 处、"
          f"窗口里出现 T 之后的行 {stats.get('selfcheck_future_row', 0)} 处 "
          f"{'← 有前视，读数作废' if bad else '（通过）'}", flush=True)

    df = df[df["day"].isin(keep)].copy()
    fwd = {h: (pan["$close"].shift(-h) / pan["$close"] - 1.0) for h in HOLDS}
    pool_by_day = {d: [c for c in pool] for d in keep}
    pop_by_day = {d: sorted(g["instrument"].unique()) for d, g in df.groupby("day")}

    b = add_excess(df.copy(), fwd, pop_by_day)
    b = b[b["ret10"].notna() & b["ex10"].notna()]
    group_table(b, f"臂 B｜全市场有分时的票：{b['day'].nunique()} 天 × "
                   f"{b['instrument'].nunique()} 只（基准=当日这些票等权）")

    scores = pred.pivot(index="datetime", columns="instrument", values="score")
    tops: dict[int, set] = {t: set() for t in TOP}
    for day, row in scores.iterrows():
        d = day.strftime("%Y-%m-%d")
        if d not in keep:
            continue
        ranked = row.dropna().sort_values(ascending=False).index
        for t in TOP:
            tops[t] |= {(d, c) for c in ranked[:t]}
    key = list(zip(df["day"], df["instrument"]))
    frames = {}
    for t in TOP:
        sub = add_excess(df[pd.Series(key, index=df.index).isin(tops[t])].copy(),
                         fwd, pool_by_day)
        frames[t] = sub[sub["ret10"].notna() & sub["ex10"].notna()]
        group_table(frames[t], f"臂 A｜档案前 {t}（基准=当日 300 池等权）")

    print("\n=== 预注册门槛读数（§29.3：|t÷√10|≥2、每组≥3,000 只·日、前 30 日均≥3 席）===")
    for arm, frame in (("B 全市场底色", b), ("A 前 30（App 名单）", frames[30]),
                       ("A 前 100（二次打分段）", frames[100])):
        print(f"[{arm}] 可评估 {len(frame):,} 只·日 / {frame['day'].nunique()} 天")
        contrast(frame, ("吸筹",), ("震荡",), "H1 吸筹 − 震荡")
        contrast(frame, BUY, ("震荡",), "H1b 吸筹+洗盘 − 震荡")
        contrast(frame, SELL, ("震荡",), "H2 真出货+出货风险 − 震荡")

    print("\n可操作面（臂 A 前 30 名单里各意图的日均席位，61 天口径）：")
    seats = frames[30].groupby(["day", "intention"]).size().unstack(fill_value=0)
    for name in ORDER:
        if name in seats.columns:
            print(f"    {name:<7} 日均 {seats[name].mean():>4.1f} 席  "
                  f"有席位的交易日 {int((seats[name] > 0).sum())}/{len(seats)}")

    a100 = frames[100]
    share = a100.assign(flag=a100["intention"].isin(BUY)).groupby("day").agg(
        share=("flag", "mean"), ex10=("ex10", "mean"))
    if len(share) >= 8:
        rho = float(share["share"].corr(share["ex10"], method="spearman"))
        print(f"\nH3 前读：前 100 里「吸筹+洗盘」占比与该日 T+10 超额的 Spearman = {rho:+.2f}"
              f"（{len(share)} 个交易日，占比中位 {share['share'].median():.1%}）")

    star = b[b["code"].str.startswith(("300", "688"))]
    print(f"\n涨停口径差自查：300/688 判定 {len(star):,} 只·日（占 {len(star) / max(len(b), 1):.1%}）"
          f"；其吸筹组 T+10 超额 "
          f"{star.loc[star['intention'] == '吸筹', 'ex10'].mean():+.2%}"
          f" vs 震荡 {star.loc[star['intention'] == '震荡', 'ex10'].mean():+.2%}"
          f"；非 300/688 的对应差 "
          f"{b.loc[b['intention'] == '吸筹', 'ex10'].mean() - b.loc[b['intention'] == '震荡', 'ex10'].mean():+.2%}"
          f"（全体）")

    out = Path("/tmp/intention_ab_readings.parquet")
    df.drop(columns=["daily_patterns", "sequences"], errors="ignore").to_parquet(out)
    print(f"\n明细（未加收益的判定记录）落 {out}，{len(df):,} 行")
    print(f"总耗时 {time.time() - t_all:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
