"""因子条件筛选：买入腿的横截面条件分布（第六条路线，为 ① 盈利与 ④ 胜率）。

前五条路线（出场参数 / 样本外剔除层 / 入场折价纪律 / 择市闸门 / 板块热度闸门）
都只在「同一条判据、同一批日线特征」上调阈值，结论一致：买入腿日等权超额 ≈0，
所以任何只动卖出或只动下单的网格都拿不到「赢比 2.0 + 赚钱」。这一条换的是**机制**：
不再问「这条判据要不要开/关」，而是问「判据命中之后，**这只票当时处在什么状态**
决定了后面 10 日的横截面超额」——即条件期望 E[超额 | 特征分位]，而不是 E[超额]。

口径与本项目既有的验证方法严格一致（见 README《判据的历史值多少钱》《判定台账》）：

- **同日基线**：超额 = 该点 T+10 收益 − 同一天样本内全部股票的 T+10 均值。
  不减掉当天大盘，牛市里任何规则都"有效"。
- **日等权**：先按天取当天命中的均值，再对天求平均。全池行等权会被「命中扎堆的恐慌日」
  主导，同一条判据在两种加权下能反号（本项目一律按日等权报）。
- **分箱用当日横截面百分位**：每个特征在**当天**样本内排百分位再分五箱（0~20/20~40/…）。
  绝对水位跨时代不可比（2019 的「位置 0.3」和 2025 的「位置 0.3」不是一回事），
  百分位每天重新归一，所以同一个箱在 2019/2022/2025 之间说的是同一件事。
  代价是分位依赖样本：换 offset 就换了一批股票，所以 **offset 轴是这一条的主要证伪轴**。
- **三轴同号 + 留一验证**：3 时代 × 3 抽样 = 9 格全部同号才算稳；再把三个 offset
  轮流留一（用另两个 offset 当"选中时的估计"，看留出那个 offset 的符号是否一致），
  3 个留一里过 2 个才算过了「80 个候选里挑中了噪声」这一关。

多重比较必须显式计数：一次筛 16 特征 × 5 箱 = 80 个候选，即使全是噪声也会有几个
在 9 格上碰巧同号。所以报告里同时印出**每一道筛子各自留下多少个候选**——
只报"找到 1 个好的"而报不出"80 个里筛了几道"就是自欺。

产物：
- `data/factor_screen/screen_<strategy>.parquet`：每 (特征, 箱) 一行，含 9 格明细、
  合并均值/合并 t、符号稳定性、留一验证一致数、覆盖率。
- `--emit-spec` 才写 `data/factor_screen/gate_spec.json`（给 backtest_factor_gate.py 当闸门），
  默认不写：闸门规格必须由人看过表之后显式生成，不能脚本顺手就把赢家发出去。

用法:
    python3 scripts/screen_signal_factors.py --strategies pattern_reversal
    python3 scripts/screen_signal_factors.py --strategies pattern_reversal --universe 400
    # 看完表再生成闸门规格（默认只取最强的一条规则）
    python3 scripts/screen_signal_factors.py --strategies pattern_reversal --emit-spec --max-rules 1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 与 backtest_exit_scan.py 同一套抽样口径，避免两处漂移
import importlib.util

_spec = importlib.util.spec_from_file_location("backtest_exit_scan", ROOT / "scripts" / "backtest_exit_scan.py")
_scan = importlib.util.module_from_spec(_spec)
sys.modules["backtest_exit_scan"] = _scan
_spec.loader.exec_module(_scan)

from stock_selector.backtest.provider import ParquetProvider            # noqa: E402
from stock_selector.core.selector import create_strategy                # noqa: E402

OUT_DIR = ROOT / "data" / "factor_screen"
KLINE_DIR = ROOT / "data" / "stock_kline"

# 候选特征：都是日线 parquet 里已经算好的列（键即列名），没有需要现算的形态列，
# 所以每个时代/抽样用的都是同一份数据，跨时代比较的是同一件事。
# 派生列在 `_add_derived` 里现算，公式全部写在旁边，便于复核。
FACTORS: dict[str, str] = {
    "roc10": "10日涨幅",
    "pos60": "60日区间位置",
    "pct": "当日涨幅",
    "gap_pct": "当日跳空",
    "vol_ratio": "量比(对5日均量)",
    "vol_z": "量能z",
    "vol_trend": "量趋势",
    "body_ratio": "实体占比",
    "upper_ratio": "上影占比",
    "lower_ratio": "下影占比",
    "adx14": "趋势强度ADX",
    "rsi14": "RSI14",
    "atr_pct": "ATR14/收盘",
    "ma5_gap": "收盘/MA5-1",
    "ma20_gap": "收盘/MA20-1",
    "liq": "成交额(亿)",
}

# 派生列（`_add_derived` 现算）：键与 FACTORS 一样是列名，公式就写在那个函数里
DERIVED_COLS = ["atr_pct", "ma5_gap", "ma20_gap", "liq"]

# signal_at 的切片只带这几列：策略自己声明的 required_columns 加上价格/日期/名称。
# 判据读不到别的列（tests/test_screen_factors.py 里逐策略核对了裁剪前后命中一致），
# 而整张 parquet 82 列时 `iloc` 切 90 行要 10ms/次，裁到十几列只要 0.16ms/次：
# 一格 6 万次调用差 60 倍，九格跑完从 1.5 小时变成 2 分钟。
SLICE_EXTRA = ["date", "name", "open", "high", "low", "close", "volume"]

DEFAULT_ERAS = ["2019-09-01:2020-09-01", "2022-09-01:2023-09-01", "2025-09-01:2026-09-01"]
BINS = 5
FWD = 10                       # 前视天数，与既有口径（后 10 日）一致
MIN_HITS = 30                  # 单格（时代×抽样×箱）命中数下限，低于此不参与判定
MIN_POOLED_HITS = 150          # 九格合计命中数下限
MIN_ABS_PP = 0.30              # 合并日等权超额绝对下限（pp）
MIN_T = 2.0                    # 合并日级 t 下限
NEED_CELLS = 9                 # 符号稳定要求的格子数（3 时代 × 3 抽样）


def _add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """派生列：atr_pct / ma5_gap / ma20_gap / liq。

    close>0 才做除法，脏行留 NaN（后面按缺失剔除，不当 0 处理——把缺失当最低箱
    会凭空造出一个"最差的一箱"，那是筛选最容易骗自己的地方）。
    """
    close = pd.to_numeric(df["close"], errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        return pd.DataFrame({
            "atr_pct": pd.to_numeric(df["atr14"], errors="coerce") / close,
            "ma5_gap": close / pd.to_numeric(df["ma5"], errors="coerce") - 1.0,
            "ma20_gap": close / pd.to_numeric(df["ma20"], errors="coerce") - 1.0,
            # volume 单位是手（100 股），成交额 ≈ volume×100×close，这里折成亿元
            "liq": pd.to_numeric(df["volume"], errors="coerce") * close / 1e8,
        }, index=df.index).replace([np.inf, -np.inf], np.nan)


def load_panel(provider: ParquetProvider, strategy, codes: list[str], start: str, end: str,
               lookback: int) -> pd.DataFrame:
    """样本股票的 (date, code, fwd10, 特征…, 判据要用的列)。

    切 `[start-lookback, end]`：切片要够 lookback 行给判据用。**fwd10 从整份文件算**
    （不是从切片算），所以时代最后 10 天的命中照样有前视收益——这不是前视：
    命中只由 ≤p 的数据决定，fwd10 用的是 >p 的数据，量的就是"命中之后发生什么"。
    主动砍掉这 10 天只会白扔样本。
    """
    # 派生列不在 parquet 里（由 `_add_derived` 现算），所以这里只挑原生列
    cols = sorted({*(set(FACTORS) - set(DERIVED_COLS)), *(strategy.required_columns() or []),
                   "date", "code", "close"})
    parts = []
    for code in codes:
        df = provider._load(code)
        if df is None or df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        c = pd.to_numeric(df["close"], errors="coerce")
        block = df[cols].copy()
        block["fwd10"] = c.shift(-FWD) / c - 1.0
        block = pd.concat([block, _add_derived(df)], axis=1)
        block["code"] = code
        dates = block["date"].astype(str).to_numpy()
        lo = max(0, int(np.searchsorted(dates, start)) - lookback)
        hi = int(np.searchsorted(dates, end))
        if hi <= lo:
            continue
        parts.append(block.iloc[lo:hi])
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def rank_col(factor: str) -> str:
    return f"rk_{factor}"


def add_daily_ranks(panel: pd.DataFrame) -> pd.DataFrame:
    """给每个特征加一列**当日样本内百分位**（`rk_<特征名>`），原始列原样保留。

    当日横截面而不是全时代分位：绝对水位跨时代不可比，而百分位每天重新归一，
    所以「roc10 落在当日最低两成」这句话在 2019/2022/2025 说的是同一件事。
    缺失值保持 NaN（停牌/指标未成形），后面按"这一箱这天没有票"处理。

    百分位必须另起一列，**不能就地覆盖**：`pos60` / `pct` / `vol_ratio` 既是筛选特征
    也是判据自己要读的列，覆盖成 0~100 之后 `pos60 > 0.35` 会把全部命中判成不合格
    （实测：覆盖后同一年 20 只票 35 个命中变成 0 个）。原始列是实盘口径，不能动。
    """
    out = panel.copy()
    for f in FACTORS:
        vals = pd.to_numeric(out[f], errors="coerce")
        out[rank_col(f)] = vals.groupby(out["date"]).rank(pct=True, method="average") * 100.0
    return out


def signal_hits(strategy, panel: pd.DataFrame, start: str, end: str, lookback: int,
                max_hold: int) -> pd.DataFrame:
    """逐股按日切片问策略「这天会不会买」，命中就带回：

    - 当日 T+10 与同日基线超额（第一套口径：条件均值）；
    - 入场后 `max_hold` 个交易日的**收盘相对收益** `r1..rH`（第二套口径：条件分布形状）。

    第二套为什么能直接算准：回测引擎的止损/止盈/持有到期**全是按收盘价判**
    （`KernelStrategy.evaluate_holdings` 拿的是 `prices[code]`），所以给定入场收盘价，
    后面 H 天的收盘序列就能把机械出场规则一字不差地重演一遍——不需要引擎、不受
    仓位上限与 max_positions 干扰，测到的就是"这只票按这套出场参数会赚多少"。
    这能把「选股准不准」和「仓位管理好不好」分开：前者只该看这一层。
    """
    day_ref = panel.groupby("date")["fwd10"].mean()
    keep = list(dict.fromkeys([*(strategy.required_columns() or []), *SLICE_EXTRA]))
    keep = [c for c in keep if c in panel.columns]
    feat_cols = [rank_col(f) for f in FACTORS]
    path_cols = [f"r{d}" for d in range(1, max_hold + 1)]
    hits: list[pd.DataFrame] = []
    for code, raw in panel.groupby("code", sort=False):
        dates = raw["date"].astype(str).to_numpy()
        f10 = pd.to_numeric(raw["fwd10"], errors="coerce").to_numpy(dtype=float)
        close = pd.to_numeric(raw["close"], errors="coerce").to_numpy(dtype=float)
        df = raw[keep].reset_index(drop=True)
        n = len(df)
        picks: list[int] = []
        for p in range(max(int(np.searchsorted(dates, start)), lookback),
                       int(np.searchsorted(dates, end))):
            if not np.isfinite(f10[p]) or not np.isfinite(close[p]) or close[p] <= 0:
                continue
            if p + FWD >= n:
                continue
            if strategy.signal_at(df.iloc[p - lookback + 1: p + 1]) is None:
                continue
            picks.append(p)
        if not picks:
            continue
        idx = np.asarray(picks)
        sub = raw.set_index(raw["date"].astype(str)).reindex([str(dates[p]) for p in picks])
        sub = sub.reset_index(drop=True)
        sub["fwd10"] = f10[idx]
        sub["excess"] = f10[idx] - day_ref.reindex([str(dates[p]) for p in picks]).to_numpy(dtype=float)
        for d in range(1, max_hold + 1):
            j = idx + d
            ok = j < n
            ratio = np.full(len(idx), np.nan)
            ratio[ok] = close[j[ok]] / close[idx[ok]] - 1.0
            sub[f"r{d}"] = ratio
        hits.append(sub[["date", "code", "fwd10", "excess", *feat_cols, *path_cols]])
    if not hits:
        return pd.DataFrame()
    return pd.concat(hits, ignore_index=True)


def exit_pnl(path: np.ndarray, stop: float, take: float) -> np.ndarray:
    """逐条命中按 (止损, 止盈, 持有) 重演机械出场，返回每条的收益率。

    顺序与引擎一致：每个持有日先看止损、再看止盈，都不触发就持有到期按当日收盘出。
    停牌/数据结束用最后一个有效收盘兜底（引擎那边同样只能按最后能拿到的价出）。
    触发的日子**按当天收盘价成交**，不按开盘、不打折——引擎就是这么记的，
    这里若改成"次日开盘"就等于偷偷换了一套更严的口径，结论会串到别的实验上去。
    """
    n, h = path.shape
    pnl = np.full(n, np.nan)
    active = np.ones(n, dtype=bool)
    for d in range(h):
        x = path[:, d]
        alive = active & np.isfinite(x)
        stop_hit = alive & (x <= -stop)
        take_hit = alive & (take > 0) & (x >= take)
        pnl[stop_hit] = x[stop_hit]
        pnl[take_hit] = x[take_hit]
        active &= ~(stop_hit | take_hit)
    # 未触发的按持有期内最后一个有效收盘
    finite = np.isfinite(path)
    any_finite = finite.any(axis=1)
    last_idx = h - 1 - np.argmax(finite[:, ::-1], axis=1)
    tail = path[np.arange(n), np.where(any_finite, last_idx, 0)]
    tail = np.where(any_finite, tail, np.nan)
    pnl = np.where(active, tail, pnl)
    return pnl


def _day_stats(dates: np.ndarray, pnl: np.ndarray) -> dict:
    """一组命中的**日级**统计：先把同一天压成一个数，再对天平均。

    为什么要压到日级再平均：命中严重扎堆（恐慌日一天几十条），点级平均会被扎堆的日子
    主导——本项目已经吃过这个亏（"sell 点事后平均是涨的"其实是那几天水位抬高）。
    payoff / 利润因子只有"某天有盈有亏"才估得出来，这类天直接剔除并单独记天数，
    不能当成 0 混进平均（那会把 payoff 压到 1 附近，看着像没效果，其实是估不出来）。
    """
    order = np.argsort(dates, kind="stable")
    d, p = dates[order], pnl[order]
    edges = np.flatnonzero(np.r_[True, d[1:] != d[:-1], True])
    exp_day, wr_day, pay_day, pf_day = [], [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        v = p[a:b]
        v = v[np.isfinite(v)]
        if v.size == 0:
            continue
        exp_day.append(float(v.mean()))
        wr_day.append(float((v > 0).mean()))
        win, loss = v[v > 0], v[v < 0]
        if win.size and loss.size:
            pay_day.append(float(win.mean() / abs(loss.mean())))
            pf_day.append(float(win.sum() / abs(loss.sum())))
    def stat(arr, scale=1.0):
        if not arr:
            return float("nan"), float("nan"), 0
        a = np.asarray(arr, dtype=float) * scale
        sd = float(a.std(ddof=1)) if a.size > 1 else 0.0
        return float(a.mean()), sd, int(a.size)

    exp_pp, exp_sd, d_exp = stat(exp_day, 100.0)
    wr, wr_sd, d_wr = stat(wr_day)
    payoff, payoff_sd, d_pay = stat(pay_day)
    pf, pf_sd, d_pf = stat(pf_day)
    return {"exp_pp": exp_pp, "exp_sd_pp": exp_sd, "days_exp": d_exp,
            "wr": wr, "wr_sd": wr_sd, "days_wr": d_wr,
            "payoff": payoff, "payoff_sd": payoff_sd, "days_payoff": d_pay,
            "pf": pf, "pf_sd": pf_sd, "days_pf": d_pf}


def aggregate_exit(hits: pd.DataFrame, strategy: str, era: str, offset: int,
                   cfg: tuple[float, float, int]) -> list[dict]:
    """出场口径的条件分布统计：一个 (时代 × 抽样 × 出场参数) 格里的每个 (特征, 箱)。

    每行同时给出**该箱自身**的日级 win_rate / payoff / 利润因子 / 单笔期望，
    以及与「同格全部命中」的差值（delta）。差值才是闸门的价值：闸门只能改变买哪一批，
    改变不了整体的运气基准，所以要判的是"这一箱比整体好多少"，而不是"这一箱绝对多少"，
    也不该把整个时代的 β 算到自己头上。
    """
    stop, take, hold = cfg
    hold = int(hold)
    if hits.empty or "excess" not in hits.columns:
        return []
    path_cols = [f"r{d}" for d in range(1, hold + 1)]
    path_cols = [c for c in path_cols if c in hits.columns]
    if len(path_cols) < 1:
        return []
    pnl = exit_pnl(hits[path_cols].to_numpy(dtype=float), stop, take)
    dates = hits["date"].astype(str).to_numpy()
    ok = np.isfinite(pnl)
    frame = hits[ok].copy()
    frame["pnl"] = pnl[ok]
    d_ok = dates[ok]
    base = _day_stats(d_ok, frame["pnl"].to_numpy(dtype=float))
    rows: list[dict] = []
    label = f"{stop:.2f}:{take:.2f}:{hold:.0f}"
    for f in FACTORS:
        vals = pd.to_numeric(frame[rank_col(f)], errors="coerce").to_numpy(dtype=float)
        total = int(np.isfinite(vals).sum())
        if total < MIN_HITS:
            continue
        bins = np.where(np.isfinite(vals),
                        np.clip(np.floor(np.nan_to_num(vals) / (100.0 / BINS)), 0, BINS - 1), -1)
        for i in range(BINS):
            sel = bins == i
            if int(sel.sum()) < MIN_HITS:
                continue
            st = _day_stats(d_ok[sel], frame.loc[sel, "pnl"].to_numpy(dtype=float))
            if st["days_exp"] < 5:
                continue
            rows.append({
                "strategy": strategy, "era": era, "offset": offset, "cfg": label,
                "factor": f, "bin": i, "n": int(sel.sum()), "factor_hits": total,
                "stop": stop, "take": take, "hold": hold,
                "exp_pp": round(st["exp_pp"], 4), "exp_sd_pp": round(st["exp_sd_pp"], 4),
                "wr": round(st["wr"], 4), "wr_sd": round(st["wr_sd"], 4),
                "payoff": round(st["payoff"], 4) if np.isfinite(st["payoff"]) else np.nan,
                "payoff_sd": round(st["payoff_sd"], 4) if np.isfinite(st["payoff_sd"]) else np.nan,
                "pf": round(st["pf"], 4) if np.isfinite(st["pf"]) else np.nan,
                "days_payoff": st["days_payoff"], "days": st["days_exp"],
                "base_exp_pp": round(base["exp_pp"], 4),
                "base_wr": round(base["wr"], 4),
                "base_payoff": round(base["payoff"], 4) if np.isfinite(base["payoff"]) else np.nan,
                "base_pf": round(base["pf"], 4) if np.isfinite(base["pf"]) else np.nan,
                "d_exp_pp": round(st["exp_pp"] - base["exp_pp"], 4),
                "d_wr": round(st["wr"] - base["wr"], 4),
                "d_payoff": round(st["payoff"] - base["payoff"], 4)
                if np.isfinite(st["payoff"]) and np.isfinite(base["payoff"]) else np.nan,
                "d_pf": round(st["pf"] - base["pf"], 4)
                if np.isfinite(st["pf"]) and np.isfinite(base["pf"]) else np.nan,
            })
    return rows


def cross_exit(rows: list[dict], offsets: list[int], eras: list[str],
               wr_target: float, payoff_target: float) -> pd.DataFrame:
    """把 9 格并成每个 (特征, 箱, 出场参数) 一行。

    判定的三个轴与第一套口径一致：九格样本够 → 九格同号 → 留一验证。
    这里同号判的是 **d_exp_pp**（相对同格整体的日等权单笔期望差），因为闸门的作用
    就是"换一批票买"，整体水位是它换不掉的。
    """
    df = pd.DataFrame(rows)
    years = [e.split(":")[0][:4] for e in eras]
    out: list[dict] = []
    for (f, i, cfg), g in df.groupby(["factor", "bin", "cfg"], sort=True):
        g = g.set_index(["era", "offset"])
        days = g["days"].to_numpy(dtype=float)
        mus = g["d_exp_pp"].to_numpy(dtype=float)
        sds = g["exp_sd_pp"].to_numpy(dtype=float)
        pooled = float((mus * days).sum() / max(days.sum(), 1e-9))
        denom = math.sqrt(max(float((sds ** 2 * days).sum()), 0.0))
        pooled_t = pooled * math.sqrt(max(days.sum(), 1e-9)) / denom if denom > 0 else 0.0
        n_total = int(g["n"].sum())
        cells = int((g["n"] >= MIN_HITS).sum())
        sign_ok = cells >= NEED_CELLS and bool((mus > 0).all() or (mus < 0).all())
        agree = 0
        for held in offsets:
            keep_fit = ~g.index.get_level_values("offset").isin([held])
            fit, val = g[keep_fit], g[~keep_fit]
            if fit.empty or val.empty:
                continue
            fm = float((fit["d_exp_pp"] * fit["days"]).sum() / max(fit["days"].sum(), 1e-9))
            vm = float((val["d_exp_pp"] * val["days"]).sum() / max(val["days"].sum(), 1e-9))
            if abs(vm) > 0 and (fm > 0) == (vm > 0):
                agree += 1
        wdays = g["days_payoff"].to_numpy(dtype=float)
        wsum = np.where(np.isfinite(g["payoff"].to_numpy(dtype=float)), wdays, 0.0)
        payoff_pooled = (float((np.nan_to_num(g["payoff"].to_numpy(dtype=float)) * wsum).sum()
                               / max(wsum.sum(), 1e-9)))
        rec = {
            "strategy": g["strategy"].iloc[0], "factor": f, "bin": int(i), "cfg": cfg,
            "n_total": n_total, "days_total": int(days.sum()),
            "coverage": round(n_total / max(int(g["factor_hits"].sum()), 1), 4),
            "d_exp_pp": round(pooled, 4), "d_exp_t": round(pooled_t, 3),
            "cell_min_pp": round(float(mus.min()), 4), "cell_max_pp": round(float(mus.max()), 4),
            "wr_pooled": round(float((g["wr"] * wdays).sum() / max(wdays.sum(), 1e-9)), 4),
            "wr_min": round(float(g["wr"].min()), 4),
            "payoff_pooled": round(payoff_pooled, 4),
            "payoff_min": round(float(np.nanmin(g["payoff"].to_numpy(dtype=float))), 4),
            "pf_pooled": round(float(np.nanmean(g["pf"].to_numpy(dtype=float))), 4),
            "d_wr_pooled": round(float((g["d_wr"] * days).sum() / max(days.sum(), 1e-9)), 4),
            "base_exp_pp": round(float((g["base_exp_pp"] * days).sum() / max(days.sum(), 1e-9)), 4),
            "base_wr": round(float((g["base_wr"] * days).sum() / max(days.sum(), 1e-9)), 4),
            "base_payoff": round(float(np.nanmean(g["base_payoff"].to_numpy(dtype=float))), 4),
            "base_pf": round(float(np.nanmean(g["base_pf"].to_numpy(dtype=float))), 4),
            "cells": cells, "sign_stable": sign_ok, "holdout_agree": agree,
            "label": f"{f}[{int(i * 100 / BINS)},{int((i + 1) * 100 / BINS)})",
        }
        for era in years:
            for off in offsets:
                hit = (era, off) in g.index
                rec[f"c_{era}_{off}"] = round(float(g.loc[(era, off), "d_exp_pp"]), 3) if hit else float("nan")
        # 「这一箱自己就达标」：九格里最差的一格也满足胜率与赔率目标。
        # 这是目标①+④的字面口径，但它比 d_exp 同号严格得多，先看它有没有，
        # 没有再看差值同号（后者只保证闸门有用，不保证绝对水平）。
        rec["hit_target"] = bool(rec["cells"] >= NEED_CELLS
                                 and rec["wr_min"] >= wr_target
                                 and rec["payoff_min"] >= payoff_target)
        out.append(rec)
    return pd.DataFrame(out)


def aggregate(hits: pd.DataFrame, strategy: str, era: str, offset: int) -> list[dict]:
    """一个 (时代 × 抽样) 格里，每个 (特征, 箱) 的日等权统计。

    日级配对 t：`daily` 是「当天该箱命中的超额均值」这条日序列对它自己 0 的 t。
    点级均值（`point_pp`）与点级胜率（`wr`）并列落盘但只当参考——点级会被扎堆的日子主导，
    同一判据两种加权能反号，本项目一律按日等权判。
    """
    rows: list[dict] = []
    if hits.empty or "excess" not in hits.columns:
        return rows
    ex = hits["excess"].to_numpy(dtype=float)
    usable = hits[np.isfinite(ex)]
    for f in FACTORS:
        vals = pd.to_numeric(usable[rank_col(f)], errors="coerce").to_numpy(dtype=float)
        total = int(np.isfinite(vals).sum())
        if total < MIN_HITS:
            continue
        b = np.clip(np.floor(np.nan_to_num(vals, nan=-1.0) / (100.0 / BINS)), 0, BINS - 1)
        bins = pd.Series(np.where(np.isfinite(vals), b, -1), index=usable.index)
        for i in range(BINS):
            sub = usable[bins == i]
            if len(sub) < MIN_HITS:
                continue
            daily = sub.groupby(sub["date"])["excess"].mean().dropna()
            if len(daily) < 5:
                continue
            mu = float(daily.mean()) * 100.0
            sd = float(daily.std(ddof=1)) * 100.0 if len(daily) > 1 else 0.0
            t = mu / (sd / math.sqrt(len(daily))) if sd > 0 else 0.0
            rows.append({
                "strategy": strategy, "era": era, "offset": offset,
                "factor": f, "bin": i, "n": len(sub), "days": int(len(daily)),
                "factor_hits": total,
                "daymean_pp": round(mu, 4), "sd_pp": round(sd, 4), "t": round(t, 3),
                "point_pp": round(float(sub["excess"].mean()) * 100.0, 4),
                "wr": round(float((sub["excess"] > 0).mean()), 4),
                "day_pos_rate": round(float((daily > 0).mean()), 4),
            })
    return rows


def cross_cells(rows: list[dict], strategy: str, offsets: list[int], eras: list[str]) -> pd.DataFrame:
    """把 9 格并成每个 (特征, 箱) 一行：合并均值、合并 t、符号稳定性、留一验证。

    合并 t 用日级方差池化：t = Σ(μᵢ·dᵢ) / sqrt(Σσᵢ²·dᵢ)，dᵢ 是该格的天数。
    留一验证：三个 offset 轮流留一，用另两个（跨时代合并）当"选中时的估计"，
    看留出 offset 的符号是否一致——9 格全同号也可能只是三个抽样各自碰巧同向，
    留一一致才说明"换一批股票这个结论还在"。
    """
    df = pd.DataFrame(rows)
    # aggregate 落盘的 era 是年份（start[:4]），而 eras 传进来是 "起:止" 全串——
    # 这里统一按年份对齐，否则九格明细会整列查不到、全成 NaN
    years = [e.split(":")[0][:4] for e in eras]
    out: list[dict] = []
    for (f, i), g in df.groupby(["factor", "bin"], sort=True):
        g = g.set_index(["era", "offset"])
        days = g["days"].to_numpy(dtype=float)
        mus = g["daymean_pp"].to_numpy(dtype=float)
        sds = g["sd_pp"].to_numpy(dtype=float)
        pooled = float((mus * days).sum() / max(days.sum(), 1e-9))
        denom = math.sqrt(max(float((sds ** 2 * days).sum()), 0.0))
        pooled_t = pooled * math.sqrt(max(days.sum(), 1e-9)) / denom if denom > 0 else 0.0
        n_total = int(g["n"].sum())
        cells = int((g["n"] >= MIN_HITS).sum())
        sign_ok = cells >= NEED_CELLS and bool((mus > 0).all() or (mus < 0).all())
        agree = 0
        for held in offsets:
            keep_fit = ~g.index.get_level_values("offset").isin([held])
            fit, val = g[keep_fit], g[~keep_fit]
            if fit.empty or val.empty:
                continue
            fm = float((fit["daymean_pp"] * fit["days"]).sum() / max(fit["days"].sum(), 1e-9))
            vm = float((val["daymean_pp"] * val["days"]).sum() / max(val["days"].sum(), 1e-9))
            if abs(vm) > 0 and (fm > 0) == (vm > 0):
                agree += 1
        rec = {
            "strategy": strategy, "factor": f, "bin": int(i),
            "n_total": n_total, "days_total": int(days.sum()),
            "coverage": round(n_total / max(int(g["factor_hits"].sum()), 1), 4),
            "pooled_pp": round(pooled, 4), "pooled_t": round(pooled_t, 3),
            "cell_min_pp": round(float(mus.min()), 4), "cell_max_pp": round(float(mus.max()), 4),
            "wr_pooled": round(float((g["wr"] * g["n"]).sum() / max(n_total, 1)), 4),
            "day_pos_pooled": round(float((g["day_pos_rate"] * g["n"]).sum() / max(n_total, 1)), 4),
            "cells": cells, "sign_stable": sign_ok, "holdout_agree": agree,
            "label": f"{f}[{int(i * 100 / BINS)},{int((i + 1) * 100 / BINS)})",
        }
        for era in years:
            for off in offsets:
                hit = (era, off) in g.index
                rec[f"c_{era}_{off}"] = round(float(g.loc[(era, off), "daymean_pp"]), 3) if hit else float("nan")
        out.append(rec)
    return pd.DataFrame(out)


def passes(rec, args, need_agree: int) -> bool:
    """全筛子：样本够 → 九格同号 → 量够 → t 够 → 留一一致。

    顺序从"最便宜、最不依赖运气"到"最贵"：留一验证放最后，因为它是最严的一道，
    也是唯一能挡住"80 个候选里挑中了噪声"的那道。
    """
    if rec["cells"] < NEED_CELLS or rec["n_total"] < args.min_pooled_hits:
        return False
    if not rec["sign_stable"]:
        return False
    if abs(rec["pooled_pp"]) < args.min_pp or abs(rec["pooled_t"]) < args.min_t:
        return False
    return int(rec["holdout_agree"]) >= need_agree


def render(df: pd.DataFrame, args, need_agree: int, n_cells: int) -> str:
    """三段：多重比较计数 → 九格明细 → 通过者。

    计数那段是这张表的可信度前提：80 个候选里如果"九格同号"就留下 20 个，
    那"九格同号"本身就没有信息量；必须看到它一路收敛到个位数。
    """
    keep9 = df["cells"] >= NEED_CELLS
    mask = pd.Series([bool(v) for _, v in zip(df.index,
                                              [passes(r, args, need_agree) for _, r in df.iterrows()])],
                     index=df.index)
    out = ["", f"===== 多重比较：{len(df)} 个 (特征×箱) 候选，逐道筛子各留多少 ====="]
    steps = [
        ("九格都有样本", keep9),
        ("九格符号同号", keep9 & df["sign_stable"]),
        (f"合并|超额|≥{args.min_pp}pp", keep9 & df["sign_stable"]
         & (df["pooled_pp"].abs() >= args.min_pp)),
        (f"合并|t|≥{args.min_t}", keep9 & df["sign_stable"]
         & (df["pooled_pp"].abs() >= args.min_pp) & (df["pooled_t"].abs() >= args.min_t)),
        (f"留一验证 ≥{need_agree}/{args.n_offsets}", mask),
    ]
    for label, m in steps:
        out.append(f"  {label:28s} 剩 {int(np.asarray(m).sum()):3d} 个")

    table = df[keep9].copy()
    table["abs_pp"] = table["pooled_pp"].abs()
    table = table.sort_values(["sign_stable", "abs_pp"], ascending=[False, False])
    grid = [c for c in table.columns if c.startswith("c_")]
    out += ["", "===== 九格明细（日等权 T+10 超额 pp，按 |合并| 降序；负=事后跑输当日全样本） =====",
            "  " + "候选".ljust(22) + " 合并pp     t  覆盖  "
            + " ".join(g[2:] for g in grid) + "  同号 留一"]
    for _, r in table.head(args.top).iterrows():
        txt = " ".join(f"{r[g]:+6.2f}" if np.isfinite(r[g]) else "    ——" for g in grid)
        out.append(f"  {r['label']:22s}{r['pooled_pp']:+7.3f} {r['pooled_t']:+6.2f} {r['coverage']:5.1%}  "
                   f"{txt}  {'✓' if r['sign_stable'] else '×'}   {int(r['holdout_agree'])}/{args.n_offsets}")
    winners = df[mask]
    out += ["", f"===== 过全部筛子：{len(winners)} 个 ====="]
    for _, r in winners.sort_values("pooled_pp", ascending=False).iterrows():
        out.append(f"  {r['label']:22s} 合并{r['pooled_pp']:+6.3f}pp t={r['pooled_t']:+6.2f} "
                   f"点级胜率{r['wr_pooled']:.1%} 日为正{r['day_pos_pooled']:.1%} "
                   f"覆盖{r['coverage']:5.1%} 九格[{r['cell_min_pp']:+.2f},{r['cell_max_pp']:+.2f}]")
    if winners.empty:
        out.append("  （无：这一族特征里没有跨时代跨抽样同号的条件分布，"
                   "与前五条路线是同一个结论）")
    out += ["",
            f"  下限：单格 ≥{MIN_HITS} 命中、九格合计 ≥{args.min_pooled_hits}、"
            f"时代×抽样 = {n_cells} 格、留一 ≥{need_agree}/{args.n_offsets}。",
            "  覆盖那一栏是这一箱占该特征全部命中的比例：覆盖 5% 的规则再好看，"
            "回到实盘也只是每天少看几只票。",
            "  分位是当日样本内的横截面百分位，换 offset 就换了一批股票——"
            "留一那一列是这一条路线唯一能自证清白的轴。",
            "  正超额 = 事后跑赢当日全样本均值，不是跑赢绝对涨幅；牛市里所有规则在"
            "时代均值口径下都会变正，这里刻意不给它这个机会。",
            "  负超额同号也是结果：它说的是「命中之后处在这一箱的票更该被剔掉」，"
            "可以直接拿去做剔除层（比新造一条买点判据便宜得多）。"]
    return "\n".join(out)


def render_exit(df: pd.DataFrame, args, need_agree: int, n_cells: int) -> str:
    """出场口径的候选表：三段——直接达标 / 差值同号 / 九格明细。

    第一段是目标 ①+④ 的字面口径（九格里每格胜率≥目标、赔率≥目标）；第二段是闸门口径
    （相对整体的日等权单笔期望九格同号 + 留一一致），它只说明"这一箱该买"，不说明
    "这样买就达标"——两段必须分开印，否则很容易把第二段当成第一段卖。
    """
    keep9 = df["cells"] >= NEED_CELLS
    mask = pd.Series([bool(v) for v in
                      [exit_passes(r, args, need_agree) for _, r in df.iterrows()]],
                     index=df.index)
    out = ["", f"===== 出场口径：{len(df)} 个 (特征×箱×出场参数) 候选 ====="]
    out.append(f"  目标口径：九格每格 胜率≥{args.wr_target:.0%} 且 赔率≥{args.payoff_target:.1f}")
    hit = df[df["hit_target"]]
    out.append(f"  直接达标（九格最差那格也过）：{len(hit)} 个")
    for _, r in hit.sort_values("d_exp_pp", ascending=False).iterrows():
            out.append(f"    {r['label']:22s} 出场{r['cfg']:>14s} 胜率{r['wr_min']:.0%}~{r['wr_pooled']:.0%} "
                   f"赔率{r['payoff_min']:.2f}~{r['payoff_pooled']:.2f} "
                   f"d期望{r['d_exp_pp']:+.3f}pp t={r['d_exp_t']:+.2f} 覆盖{r['coverage']:.1%}")
    stable = df[keep9 & mask]
    out.append(f"\n  差值同号 + 留一≥{need_agree}/{args.n_offsets}（闸门口径）：{len(stable)} 个")
    for _, r in stable.sort_values("d_exp_pp", ascending=False).head(args.top).iterrows():
        out.append(f"    {r['label']:22s} 出场{r['cfg']:>14s} d期望{r['d_exp_pp']:+.3f}pp "
                   f"t={r['d_exp_t']:+5.2f} d胜率{r['d_wr_pooled']:+.1%} "
                   f"胜率{r['wr_min']:.0%}~{r['wr_pooled']:.0%} 赔率{r['payoff_min']:.2f}~"
                   f"{r['payoff_pooled']:.2f} 覆盖{r['coverage']:5.1%} 留一{int(r['holdout_agree'])}/{args.n_offsets}")
    table = df[keep9].copy()
    table["abs_pp"] = table["d_exp_pp"].abs()
    table = table.sort_values(["sign_stable", "abs_pp"], ascending=[False, False])
    grid = [c for c in table.columns if c.startswith("c_")]
    out += ["", "  九格明细：每格 = 该箱日等权单笔期望 − 同格全部命中（pp），正=这一箱更好",
            "  " + "候选".ljust(30) + "d期望     t 覆盖  " + " ".join(g[2:] for g in grid) + "  同号 留一"]
    for _, r in table.head(args.top).iterrows():
        txt = " ".join(f"{r[g]:+6.2f}" if np.isfinite(r[g]) else "    ——" for g in grid)
        out.append(f"  {r['label'] + '@' + r['cfg']:30s}{r['d_exp_pp']:+7.3f} {r['d_exp_t']:+6.2f} "
                   f"{r['coverage']:5.1%}  {txt}  {'✓' if r['sign_stable'] else '×'}   "
                   f"{int(r['holdout_agree'])}/{args.n_offsets}")
    out += ["",
            f"  同格整体基准：单笔期望 {table['base_exp_pp'].iloc[0]:+.3f}pp、"
            f"胜率 {table['base_wr'].iloc[0]:.1%}、赔率 {table['base_payoff'].iloc[0]:.2f}"
            if len(table) else "  （没有九格样本足够的候选）",
            "  胜率/赔率都是**日级**统计：同一天先压成一个数再对天平均，"
            "所以扎堆的恐慌日不会把点级口径抬起来（点级口径本项目已多次翻号）。",
            "  赔率与利润因子只在「当天有盈也有亏」的日子估得出来，这类天单独计数"
            "（days_payoff），不估成 0。",
            f"  下限：单格 ≥{MIN_HITS} 命中、九格合计 ≥{args.min_pooled_hits}、"
            f"时代×抽样 = {n_cells} 格。",
            "  覆盖 = 这一箱占该特征全部命中的比例；覆盖 5% 的规则再好看，"
            "回到实盘也只是每天少看几只票。",
            "  本表按收盘价重演机械出场，与回测引擎同一口径（引擎也只看收盘）；"
            "它不含仓位上限与 max_positions 的排队效应，所以绝对值与回测会有差，"
            "读相对差值（d 开头那几列）。"]
    return "\n".join(out)


def exit_passes(rec, args, need_agree: int) -> bool:
    """差值同号 + 留一一致的闸门判据（与第一套口径同构）。"""
    if rec["cells"] < NEED_CELLS or rec["n_total"] < args.min_pooled_hits:
        return False
    if not rec["sign_stable"]:
        return False
    if rec["d_exp_pp"] <= 0 or abs(rec["d_exp_t"]) < args.min_t:
        return False
    return int(rec["holdout_agree"]) >= need_agree


def emit_spec(df: pd.DataFrame, args, need_agree: int) -> Path:
    """过筛的**正向**候选写闸门规格，只取前 `--max-rules` 条。

    默认 1 条：多规则并集（OR）看着能把三四个因子的效应叠起来，但那是把同一批
    命中用四条规则各挑一遍，命中率虚高、回测里期望会好看，样本外会散。真的要多条，
    显式调 `--max-rules`，并在 README 里同时写清是哪几条。
    """
    picked = df[[passes(r, args, need_agree) for _, r in df.iterrows()]]
    picked = picked[picked["pooled_pp"] > 0].sort_values("pooled_pp", ascending=False)
    picked = picked.head(args.max_rules)
    if picked.empty:
        raise SystemExit("没有过筛的正向规则，不写规格（闸门要人工确认后生成）")
    spec = {
        "strategy": args.strategies,
        "features": {k: v for k, v in FACTORS.items()},
        "bins": BINS,
        "mode": "union" if len(picked) > 1 else "single",
        "rules": [{"factor": str(r["factor"]), "bin": int(r["bin"]), "label": str(r["label"]),
                   "pooled_pp": float(r["pooled_pp"]), "pooled_t": float(r["pooled_t"]),
                   "coverage": float(r["coverage"]),
                   "cell_range_pp": [float(r["cell_min_pp"]), float(r["cell_max_pp"])]}
                  for _, r in picked.iterrows()],
        "note": "分位=当日样本内横截面百分位；闸门只放行落在规则箱内的命中；"
                "取不到分位（停牌/指标缺失）按放行，与实盘「信息不够就不判」一致。",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "gate_spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def emit_exit_spec(df: pd.DataFrame, args, need_agree: int) -> Path:
    """出场口径过筛的正向规则写闸门规格。

    与 `emit_spec` 的差别只有一处要紧的：规格里带 `selected_under_exit`。
    规则是**在某组出场参数下**选出来的，换一组出场参数再拿它当闸门，就是拿 A 的筛子
    去筛 B——所以闸门脚本读到规格里的出场参数与自己的 `--exits` 不一致时要喊出来，
    不能默默跑出一个"闸门有用"的结论。
    """
    picked = df[[exit_passes(r, args, need_agree) for _, r in df.iterrows()]]
    picked = picked.sort_values("d_exp_pp", ascending=False).head(args.max_rules)
    if picked.empty:
        raise SystemExit("出场口径没有过筛的正向规则，不写规格")
    spec = {
        "strategy": str(picked["strategy"].iloc[0]),
        "screen": "exit",
        "features": {k: v for k, v in FACTORS.items()},
        "bins": BINS,
        "selected_under_exit": str(picked["cfg"].iloc[0]),
        "rules": [{"factor": str(r["factor"]), "bin": int(r["bin"]), "label": str(r["label"]),
                   "d_exp_pp": float(r["d_exp_pp"]), "d_exp_t": float(r["d_exp_t"]),
                   "wr_range": [float(r["wr_min"]), float(r["wr_pooled"])],
                   "payoff_range": [float(r["payoff_min"]), float(r["payoff_pooled"])],
                   "coverage": float(r["coverage"]),
                   "cell_range_pp": [float(r["cell_min_pp"]), float(r["cell_max_pp"])]}
                  for _, r in picked.iterrows()],
        "note": "分位=当日样本内横截面百分位；闸门只放行落在规则箱内的命中；"
                "取不到分位（停牌/指标缺失）按放行，与实盘「信息不够就不判」一致。",
    }
    path = OUT_DIR / "gate_spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run(args) -> None:
    provider = ParquetProvider(KLINE_DIR)
    eras = args.eras.split(",")
    offsets = [int(x) for x in args.offsets.split(",")]
    all_codes = provider.codes()
    exits = [tuple(float(x) for x in item.split(":")) for item in args.exits.split(",")] if args.exits else []
    max_hold = max([int(h) for _, _, h in exits], default=0)
    need_agree = args.need_agree if args.need_agree >= 0 else max(len(offsets) - 1, 1)
    args.n_offsets = len(offsets)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in args.strategies.split(","):
        strategy = create_strategy(name)
        lookback = max(int(strategy.required_rows() or 0), 60)
        rows: list[dict] = []
        exit_rows: list[dict] = []
        t_all = time.time()
        for offset in offsets:
            codes = _scan._sample(all_codes, args.universe, offset)
            for era in eras:
                start, end = era.split(":")
                t0 = time.time()
                panel = load_panel(provider, strategy, codes, start, end, lookback)
                if panel.empty:
                    print(f"  {name:17s} 时代{start[:4]} 样本+{offset} 无数据", flush=True)
                    continue
                panel = add_daily_ranks(panel)
                hits = signal_hits(strategy, panel, start, end, lookback, max(max_hold, FWD))
                rows.extend(aggregate(hits, name, start[:4], offset))
                for cfg in exits:
                    exit_rows.extend(aggregate_exit(hits, name, start[:4], offset, cfg))
                mean_ex = hits["excess"].mean() * 100 if len(hits) else float("nan")
                print(f"  {name:17s} 时代{start[:4]} 样本+{offset:3d} 命中{len(hits):4d} "
                      f"总体超额{mean_ex:+6.3f}pp ({time.time() - t0:.0f}s)", flush=True)
                del panel, hits
        n_cells = len(eras) * len(offsets)
        if rows:
            table = cross_cells(rows, name, offsets, eras)
            out = OUT_DIR / f"screen_{name}.parquet"
            table.to_parquet(out, index=False)
            print(render(table, args, need_agree, n_cells))
            print(f"\n已写 {out}（{len(table)} 行，用时 {(time.time() - t_all) / 60:.1f} 分钟）")
            if args.emit_spec:
                print(f"已写闸门规格 {emit_spec(table, args, need_agree)}")
        else:
            print(f"{name}: 条件均值口径没有可用候选（单格命中数都不到 {MIN_HITS}）")
        if exit_rows:
            etable = cross_exit(exit_rows, offsets, eras, args.wr_target, args.payoff_target)
            out = OUT_DIR / f"screen_exit_{name}.parquet"
            etable.to_parquet(out, index=False)
            print(render_exit(etable, args, need_agree, n_cells))
            print(f"\n已写 {out}（{len(etable)} 行）")
            if args.emit_exit_spec:
                print(f"已写闸门规格 {emit_exit_spec(etable, args, need_agree)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="因子条件筛选：买入腿的横截面条件分布")
    ap.add_argument("--strategies", default="pattern_reversal")
    ap.add_argument("--eras", default=",".join(DEFAULT_ERAS))
    ap.add_argument("--offsets", default="0,137,274")
    ap.add_argument("--universe", type=int, default=250)
    ap.add_argument("--features", default="", help="只筛这几个特征（逗号分隔），默认全部")
    ap.add_argument("--min-pp", type=float, default=MIN_ABS_PP, help="合并日等权超额绝对下限（pp）")
    ap.add_argument("--min-t", type=float, default=MIN_T)
    ap.add_argument("--min-pooled-hits", type=int, default=MIN_POOLED_HITS)
    ap.add_argument("--need-agree", type=int, default=-1, help="留一验证至少几个一致（默认 offset 数-1）")
    ap.add_argument("--top", type=int, default=30, help="明细表印前几条")
    ap.add_argument("--exits", default="0.05:0.00:10,0.04:0.10:15",
                    help="出场参数（止损:止盈:持有日数），逗号分隔多个；留空则不跑出场口径")
    ap.add_argument("--wr-target", type=float, default=0.50, help="胜率目标（达标口径用）")
    ap.add_argument("--payoff-target", type=float, default=2.0, help="赔率目标（达标口径用）")
    ap.add_argument("--emit-spec", action="store_true", help="把过筛的正向规则写成闸门规格（条件均值口径）")
    ap.add_argument("--emit-exit-spec", action="store_true",
                    help="把出场口径过筛的正向规则写成闸门规格")
    ap.add_argument("--max-rules", type=int, default=1, help="规格里最多放几条规则（默认 1）")
    args = ap.parse_args()
    if args.features:
        wanted = [f.strip() for f in args.features.split(",") if f.strip()]
        unknown = [f for f in wanted if f not in FACTORS]
        if unknown:
            raise SystemExit(f"未知特征 {','.join(unknown)}；可用：{','.join(FACTORS)}")
        for f in [f for f in list(FACTORS) if f not in wanted]:
            del FACTORS[f]
    run(args)


if __name__ == "__main__":
    main()
