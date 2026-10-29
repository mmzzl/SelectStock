"""线上模型 batchH_r1 的组合级回测：收益 + 回撤 + IR（信息比率）。

这条臂的形状是「信号日涨 ≥+5% ∩ 次日开盘低于今收 ⇒ 开盘买 ⇒ 哪天收盘高过买入价哪天卖
⇒ 跌 5% 止损 ⇒ 最多持 10 日」，所以**不能**用 backtest.py 的 TopkDropout/band 撮合去量它：
那套是「T−1 分数、T 日收盘成交、每天强制换仓」，既没有人群闸门也没有这条出场规则，
跑出来的 IR 量的是另一个策略。这里按事件逐笔走完 `dip_rule`，再把持仓摊成**槽位账本**的
每日净值曲线，IR 才从曲线上出。

账本口径（都跟着数字一起交回去）：
- 变体：每个信号日按模型分取头 3/5/10 只，外加「当天全部触发事件」（= base，人群同尺、无排序技巧）。
- 槽位：资金分 `slots` 份，一份一个仓位；当天没有空槽就**放弃**该信号（计入 `放弃` 数）；
  出场那根收盘才腾槽，次日开盘才能接新信号。满槽时资金占用 100%，任何时刻不加杠杆。
  槽内权重按买入当天的 1/slots 冻结（赢家的实际权重漂移不重估，日内/日内前后顺序不建模）。
- 成本：买 0.05% 记在买入那根、卖 0.15% 记在出场那根，按费后市值复利（口径 = §45 的双边 0.20%）。
- 停牌（收盘 NaN）那根不产生收益，走势记到恢复交易的那根上（跨多日合一根，不丢账）。
- **不做**的事，所以读数偏乐观：没有资金容量与滑点、涨跌停不挡单（§43 拍板）、
  没叠线上那两条名单护栏、池是训练池 liq900 而不是 8003 每日重算的 universe 名单。
- IR = 日均超额 / 日超额标准差 × √243，两个基准：
  ① 池内等权（liq900 当日全体等权，未复权裸价 ⇒ 除权日算成大跌，账本与基准同一污染）；
  ② 同槽位的 base 账本（同窗、同规则、当天全部触发事件）⇒ 这一列才回答「模型的排序值多少钱」。

用法：python3 -u scripts/qlib_h_r1_backtest_ir.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

QLIB_DIR = Path("/data/work/qlib_data/cn")
OUT_DIR = REPO / "data" / "qlib"
TRADING_DAYS = 243
CHUNK = 60
BUY_COST = 0.0005
SELL_COST = 0.0015
COST = BUY_COST + SELL_COST
HEADS = (3, 5, 10)


def load_pool(name: str) -> list[str]:
    out: list[str] = []
    for line in (QLIB_DIR / "instruments" / name).read_text(encoding="utf-8").splitlines():
        sym = line.split("\t")[0].strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def fetch_px(pool: list[str], beg: str, end: str) -> pd.DataFrame:
    from qlib.data import D

    frames = []
    for start in range(0, len(pool), CHUNK):
        batch = pool[start:start + CHUNK]
        t0 = time.time()
        frames.append(D.features(batch, ["$close", "$open", "$low"],
                                 start_time=beg, end_time=end))
        print(f"  [{start + len(batch)}/{len(pool)}] {time.time() - t0:.0f}s", flush=True)
    px = pd.concat(frames)
    return px.rename(columns={"$close": "close", "$open": "open", "$low": "low"})


def position_returns(day_ix: dict, closes: pd.DataFrame, ev: pd.DataFrame,
                     buy_cost: float = BUY_COST, sell_cost: float = SELL_COST) -> pd.DataFrame:
    """每笔交易拆成「它开着的那几根」的日收益，长表 (date, pos, ret)。

    第 0 根从开盘买入价起算、买成本按费后市值扣在这一根，出场根按 `exit_price` 结算、卖成本扣在
    这一根，中间走收盘对收盘。逐根复利乘回去正好等于 (出场/买入)×(1−买)×(1−卖) − 1，
    默认买 0.05% / 卖 0.15% 时与 §45 存档的「毛 − 0.20%」差在 1e-4 量级（费上再计费）。
    `buy_cost`/`sell_cost` 抬高就是加滑点：单边 10bp ⇒ 双边多 20bp。
    买入根与出场根同一根（实测占多数，`days == 0`）时只出一行。
    `pos` = 这一根事件在 `ev` 里的行号，`slot_book` 用它把持仓对回槽位。
    """
    rows = []
    for k, e in enumerate(ev.itertuples()):
        seg = closes[e.instrument].iloc[day_ix[e.exec_date]:day_ix[e.exit_date] + 1]
        prev, first = float(e.entry), True
        for d, c in seg.items():
            last = d == e.exit_date
            if last:
                price = float(e.exit_price)
            elif pd.isna(c) or not (c > 0):
                continue                      # 停牌：这根没有可算的价，走势并到下一根
            else:
                price = float(c)
            keep = ((1.0 - buy_cost) if first else 1.0) * ((1.0 - sell_cost) if last else 1.0)
            rows.append((d, k, price / prev * keep - 1.0))
            prev, first = price, False
            if last:
                break
    return pd.DataFrame(rows, columns=["date", "pos", "ret"])


def slot_book(daily: pd.DataFrame, ev: pd.DataFrame, days: pd.DatetimeIndex,
              slots: int, picks: dict, rmap: dict | None = None,
              xmap: dict | None = None) -> dict:
    """固定槽位账本：每天开盘按 `picks` 顺序往空槽里放 1/slots 的钱，出场那根收盘腾槽。

    `picks` = {执行日: [pos, ...]}（顺序就是当天的买入优先级）。`pos` 是 `ev` 的行号。
    没有空槽的信号记进 `skipped` —— 这是「买不下」的事实，不能假装成交，更不能加杠杆。
    `rmap`/`xmap` 是同一批事件的日收益/出场日查表，安慰剂几百次重抽时复用一次算好的结果。
    """
    r_by_day = rmap if rmap is not None else {
        d: dict(zip(g["pos"], g["ret"])) for d, g in daily.groupby("date")}
    exit_day = xmap if xmap is not None else {
        k: pd.Timestamp(e.exit_date) for k, e in enumerate(ev.itertuples())}
    open_w: dict[int, float] = {}
    rets, concs, useds = [], [], []
    skipped = 0
    w = 1.0 / slots
    for d in days:
        free = slots - len(open_w)
        for pos in picks.get(d, ()):
            if free <= 0:
                skipped += 1
                continue
            open_w[pos] = w
            free -= 1
        day_r = r_by_day.get(d, {})
        rets.append(sum(weight * day_r.get(pos, 0.0) for pos, weight in open_w.items()))
        concs.append(len(open_w))
        useds.append(sum(open_w.values()))
        for pos in [p for p in open_w if exit_day[p] == d]:
            open_w.pop(pos)
    return {"ret": pd.Series(rets, index=days),
            "conc": pd.Series(concs, index=days, dtype="float64"),
            "used": pd.Series(useds, index=days),
            "skipped": skipped}


def picks_for(ev_sorted: pd.DataFrame, n: int | None, seed: int | None = None) -> dict:
    """{执行日: [pos…]}：`seed=None` 按模型分取头 n 只；给了 seed 就**同日随机抽 n 只**。

    `n=None` = 当天全部（人群账本）。对照必须用随机序：上一版让 base 沿用分数序，槽位不够时
    它自动变成「按分取前 slots 只」，与 head 行逐位相同，等于自己给自己当对照组。
    """
    out: dict = {}
    for d, g in ev_sorted.groupby("exec_date", sort=True):
        if n is None:
            out[d] = (list(g["pos"].sample(len(g), random_state=seed)) if seed is not None
                      else g["pos"].tolist())
        elif seed is None:
            out[d] = g.nlargest(min(n, len(g)), "score")["pos"].tolist()
        else:
            out[d] = list(g["pos"].sample(min(n, len(g)), random_state=seed))
    return out


def perf(ret: pd.Series, bench: pd.Series) -> tuple[float, float]:
    """(年化, 对池内等权的 IR)。"""
    nav = (1.0 + ret).cumprod()
    ann = float(nav.iloc[-1] ** (TRADING_DAYS / len(ret)) - 1.0)
    ex = ret - bench.reindex(ret.index).fillna(0.0)
    ir = (float(ex.mean() / ex.std(ddof=1) * np.sqrt(TRADING_DAYS))
          if ex.std(ddof=1) > 0 else float("nan"))
    return ann, ir


def summarize(name: str, slots: int, bk: dict, bench: pd.Series, n_pick: float) -> dict:
    ret, used = bk["ret"], bk["used"]
    nav = (1.0 + ret).cumprod()
    ex_mkt = ret - bench.reindex(ret.index).fillna(0.0)
    ann, ir = perf(ret, bench)
    return {
        "账本": name, "槽位": slots, "信号/日": n_pick,
        "投几天": int((bk["conc"] > 0).sum()),
        "放弃": int(bk["skipped"]),
        "日均持仓": float(bk["conc"].mean()),
        "资金占用": float(used.mean()),
        "总收益": float(nav.iloc[-1] - 1.0),
        "年化": ann,
        "最大回撤": float((nav / nav.cummax() - 1.0).min()),
        "日波动%": float(ret.std(ddof=1) * 100),
        "sharpe": (float(ret.mean() / ret.std(ddof=1) * np.sqrt(TRADING_DAYS))
                   if ret.std(ddof=1) > 0 else float("nan")),
        "IR_池等权": ir,
        "年化超额_池": float(ex_mkt.mean() * TRADING_DAYS),
        "前10天占比": float((ret.nlargest(10).sum() / ret.sum()) if ret.sum() > 0 else float("nan")),
    }


def placebo(ev_sorted: pd.DataFrame, daily: pd.DataFrame, days: pd.DatetimeIndex,
            slots: int, n: int, bench: pd.Series, draws: int, rmap, xmap) -> dict:
    """同日随机抽 n 只、重抽 `draws` 次：模型排序到底比「闭眼在同一天的人群里抓 n 只」强多少。

    这是剔掉「人群红利」之后的唯一干净对照——账本收益的大部分来自「今天涨≥5% 明天低开的票」
    这一群人本身会反弹，而不是模型在这群人里的排序。
    """
    anns, irs, acc = [], [], pd.Series(0.0, index=days)
    for seed in range(draws):
        bk = slot_book(daily, ev_sorted, days, slots, picks_for(ev_sorted, n, seed),
                       rmap, xmap)
        a, ir = perf(bk["ret"], bench)
        anns.append(a)
        irs.append(ir)
        acc = acc + bk["ret"]
    mean_rand = acc / draws
    return {"anns": np.array(anns), "irs": np.array(irs), "mean_ret": mean_rand}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="batchH_r1")
    ap.add_argument("--pool", default="liq900.txt")
    ap.add_argument("--beg", default="2023-11-01", help="面板最早取到这里，够算昨收")
    ap.add_argument("--test-beg", default="2024-01-01")
    ap.add_argument("--end", default="2026-10-16", help="与 §45 预注册同一右端（不含）")
    ap.add_argument("--slots", default="10,20", help="逗号分隔：各跑一档固定槽位")
    ap.add_argument("--draws", type=int, default=100, help="安慰剂重抽次数")
    ap.add_argument("--min-chg", type=float, default=0.05)
    ap.add_argument("--max-gap", type=float, default=0.0)
    ap.add_argument("--stop-pct", type=float, default=-0.05)
    ap.add_argument("--max-hold", type=int, default=10)
    args = ap.parse_args()

    import qlib
    from stock_selector_qlib.dip_rule import rule_event_rows

    qlib.init(provider_uri=str(QLIB_DIR), kernels=1)
    pool = load_pool(args.pool)
    print(f"[面板] {args.pool} {len(pool)} 只 {args.beg}→{args.end}", flush=True)
    px = fetch_px(pool, args.beg, args.end)

    tb, end = pd.Timestamp(args.test_beg), pd.Timestamp(args.end)
    ev_all = rule_event_rows(px, min_chg=args.min_chg, max_gap=args.max_gap,
                             stop_pct=args.stop_pct, max_hold=args.max_hold)
    d = ev_all.index.get_level_values("datetime")
    ev = ev_all[(d >= tb) & (d < end)].copy()
    print(f"[事件] {args.test_beg}→{args.end}（不含右端）触发 {len(ev)} 笔 / "
          f"{ev.index.get_level_values(0).nunique()} 个信号日", flush=True)

    pred = pd.read_parquet(OUT_DIR / f"pred_{args.run}.parquet")
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    score = pred.set_index(["datetime", "instrument"])["score"].sort_index()
    ev = ev.join(score, how="left")
    miss = int(ev["score"].isna().sum())
    if miss:
        print(f"[对齐] {miss} 笔事件在 pred_{args.run}.parquet 里没有分数 ⇒ 丢掉")
        ev = ev[ev["score"].notna()]

    # 自证：同一批事件 + 同一份分数，必须重现 §45 预注册的头 N 净收益与配对 t
    run = json.loads((OUT_DIR / f"run_{args.run}.json").read_text(encoding="utf-8"))
    print("\n[自证 vs §45 存档]（逐笔口径，净=毛−0.20%，配对=头 N 减当天全体）")
    base_by_day = ev.groupby(level="datetime", sort=False)["label"].mean()
    ev_sorted = ev.reset_index().sort_values(["datetime", "score"], ascending=[True, False])
    ev_sorted = ev_sorted.reset_index(drop=True).assign(pos=lambda x: x.index)
    for n in (3, 5, 10, 20):
        head = ev_sorted[ev_sorted.groupby("datetime", sort=False).cumcount() < n]
        diff = (head.groupby("datetime")["label"].mean() - base_by_day).dropna()
        t = float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))))
        ref = run["head_all"][f"head{n}"]
        print(f"  头 {n:>2}：净 {(head['label'].mean() - COST) * 100:+.3f}% "
              f"（存档 {ref['net'] * 100:+.3f}%），配对 {diff.mean() * 100:+.3f}pp "
              f"t {t:+.2f}（存档 {ref['t']:+.2f}）")
    print(f"  base：净 {(ev['label'].mean() - COST) * 100:+.3f}% "
          f"（存档 {run['head_all']['base']['net'] * 100:+.3f}%）")
    hold = ev["days"] + 1
    print(f"  持有根数：中位 {hold.median():.0f}、均值 {hold.mean():.2f}，"
          f"当天了结占 {(hold <= 1).mean() * 100:.1f}%")

    # 日期轴取到面板最后一根（含）：信号日右端不含，但最后一天也可能正好是出场日
    days = pd.DatetimeIndex(sorted(px.index.get_level_values("datetime").unique()))
    days = days[days >= tb]
    day_ix = {dte: i for i, dte in enumerate(days)}
    closes = px["close"].unstack("instrument").reindex(days).reindex(columns=pool)
    bench = closes.pct_change().mean(axis=1, skipna=True).fillna(0.0)

    daily = position_returns(day_ix, closes, ev_sorted)
    rmap = {d: dict(zip(g["pos"], g["ret"])) for d, g in daily.groupby("date")}
    xmap = {k: pd.Timestamp(e.exit_date) for k, e in enumerate(ev_sorted.itertuples())}
    n_pick = float(ev_sorted.groupby("exec_date").size().mean())
    print(f"\n[账本] 固定槽位、每槽 1/slots 资金、满槽放弃信号；窗口 {len(days)} 个交易日 "
          f"{str(days[0])[:10]}→{str(days[-1])[:10]}；当天平均触发 {n_pick:.1f} 笔信号",
          flush=True)
    rows, pl_rows = [], []
    for slots in [int(s) for s in str(args.slots).split(",")]:
        books = {f"head{n}": slot_book(daily, ev_sorted, days, slots, picks_for(ev_sorted, n),
                                       rmap, xmap) for n in HEADS}
        for n in HEADS:
            rows.append(summarize(f"head{n}", slots, books[f"head{n}"], bench, n))
        rows.append(summarize("rand5(单次)", slots,
                              slot_book(daily, ev_sorted, days, slots,
                                        picks_for(ev_sorted, 5, 0), rmap, xmap), bench, 5))
        rows.append(summarize("人群全部(随机序)", slots,
                              slot_book(daily, ev_sorted, days, slots,
                                        picks_for(ev_sorted, None, 0), rmap, xmap),
                              bench, n_pick))
        for n in HEADS:
            pl = placebo(ev_sorted, daily, days, slots, n, bench, args.draws, rmap, xmap)
            head_ret = books[f"head{n}"]["ret"]
            diff = head_ret - pl["mean_ret"]
            t = (float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))))
                 if diff.std(ddof=1) > 0 else float("nan"))
            h_ann, h_ir = perf(head_ret, bench)
            pl_rows.append({
                "账本": f"head{n}", "槽位": slots,
                "模型年化": h_ann, "随机年化中位": float(np.median(pl["anns"])),
                "随机年化p90": float(np.percentile(pl["anns"], 90)),
                "模型分位": float((pl["anns"] <= h_ann).mean()),
                "模型IR": h_ir, "随机IR中位": float(np.median(pl["irs"])),
                "随机IRp90": float(np.percentile(pl["irs"], 90)),
                "日差配对t": t, "日均差bp": float(diff.mean() * 1e4),
            })
        for n in (3, 5):
            nav = (1.0 + books[f"head{n}"]["ret"]).cumprod()
            print(f"[曲线] {slots} 槽 head{n}：净值 1.000 → {float(nav.iloc[-1]):.3f}"
                  f"（峰 {float(nav.max()):.3f} / 谷 {float(nav.min()):.3f}）", flush=True)

    tbl = pd.DataFrame(rows)
    pd.set_option("display.width", 250, "display.max_columns", 40)
    out = tbl.copy()
    for c in ("总收益", "年化", "最大回撤", "年化超额_池", "资金占用", "前10天占比"):
        out[c] = (out[c].astype(float) * 100).round(2)
    for c in ("sharpe", "IR_池等权", "日均持仓", "信号/日", "日波动%"):
        out[c] = out[c].astype(float).round(2)
    print()
    print(out.to_string(index=False))
    print("\n单位：%（「日均持仓/信号每日/资金占用%/前10天占比%/sharpe/IR」除外）。"
          "「年化超额_池」= 对池内等权的年化差；「前10天占比」= 最好的 10 天占了日收益合计的多少"
          "（越大越说明曲线靠几天运气）；「放弃」= 满槽买不下的信号笔数")

    plt = pd.DataFrame(pl_rows)
    bnav = (1.0 + bench).cumprod()
    print(f"[曲线] 池内等权基准：净值 1.000 → {float(bnav.iloc[-1]):.3f}（同期、未复权裸价）")
    p = plt.copy()
    for c in ("模型年化", "随机年化中位", "随机年化p90"):
        p[c] = (p[c].astype(float) * 100).round(2)
    for c in ("模型分位", ):
        p[c] = (p[c].astype(float) * 100).round(1)
    for c in ("模型IR", "随机IR中位", "随机IRp90", "日差配对t", "日均差bp"):
        p[c] = p[c].astype(float).round(2)
    print(f"\n[安慰剂] 同日随机抽同样只数、重抽 {args.draws} 次：模型排序在人群里值多少钱")
    print(p.to_string(index=False))
    print("「模型分位」= 模型的年化收益排在随机分布里的位置（100%=比所有随机抽法都好）；"
          "「日差配对t」= 模型账本减随机账本均值 的日收益配对 t（243 日年化前的天数为分母）")

    # 滑点敏感性：这条臂的逐笔edge 是 0.2%~0.4% 量级，任何真实冲击成本都要先看这一栏
    slip_slots = int(str(args.slots).split(",")[0])
    head_label = {n: float(ev_sorted[ev_sorted.groupby("datetime", sort=False).cumcount() < n]
                           ["label"].mean()) for n in (5, 10)}
    print(f"\n[滑点] 双边手续费之外、每边再加这么高冲击成本（{slip_slots} 槽账本；"
          f"「逐笔净」= §45 那个每笔均值口径，同一把尺）")
    srows = []
    for slip in (0.0, 0.0005, 0.001, 0.002, 0.003):
        d2 = position_returns(day_ix, closes, ev_sorted, BUY_COST + slip, SELL_COST + slip)
        m2 = {d: dict(zip(g["pos"], g["ret"])) for d, g in d2.groupby("date")}
        for n in (5, 10):
            bk = slot_book(d2, ev_sorted, days, slip_slots, picks_for(ev_sorted, n), m2, xmap)
            ann, ir = perf(bk["ret"], bench)
            nav = (1.0 + bk["ret"]).cumprod()
            srows.append({"单边加bp": slip * 1e4, "账本": f"head{n}",
                          "逐笔净%": (head_label[n] - COST - 2 * slip) * 100,
                          "年化%": ann * 100, "总收益%": (float(nav.iloc[-1]) - 1) * 100,
                          "IR_池等权": ir, "最大回撤%": float((nav / nav.cummax() - 1).min()) * 100})
    st = pd.DataFrame(srows)
    print(st.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    be5 = (head_label[5] - COST) / 2 * 1e4
    be10 = (head_label[10] - COST) / 2 * 1e4
    print(f"\n[盈亏平衡] 逐笔均值口径下，每边还能承受的冲击成本：head5 ≈ {be5:.0f}bp、"
          f"head10 ≈ {be10:.0f}bp（账本曲线因复利与槽位占用，归零点会略有出入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
