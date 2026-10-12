"""判定记账台账：把「每一天、每一只票、按持仓插件同一套规则判成什么」连同学业一起落盘。

为什么要这张表：诊断里的 hold/sell/add 从来没有被核对过——库里存了判定，没存「这条判定后来
怎么样了」。没有学业，可信度就只能靠感觉；有了它，任何状态都能算出后验区间（见 posterior.parquet）。

产出（整表覆盖，重跑幂等）：
    data/judgement_ledger/ledger_<year>.parquet  —— 逐日逐票一行：判定 + 状态 + T+10/T+20 学业
    data/judgement_ledger/posterior.parquet      —— 按 (action × 热度箱) 聚合的 Beta 后验

口径与坑（读数字前必看）：
- 判定串直接 import 插件的 `load_rules()`，与 8001 持仓诊断同一真源，不在这里抄常量；
- 热度 = 板块近 3 日成分等权涨幅在同日所有板块间的**横截面百分位**（因果），个股取其所属板块
  里最高那个。**成分表用 member.parquet 的最新一天**，即拿今天的成员回推历史——一只票现在被
  归进热门概念，可能正是因为它涨了，这是已知的前视，量级未知，所以热度只能当门控不能当证据；
- `dist_to_trigger`（距卖点线）没有入台账：`nearest_price_trigger` 是逐行反解，12M 行跑不动，
  且此前分箱实测它对 10/20 日均值的区分度 <0.5pp，先不占容量；
- 学业 `fwd10` 是 T+10 **交易日**收盘收益，不含任何交易成本；最后 10 天没有学业，那些行不落盘，
  所以台账天然比 K 线短 10 个交易日，这是对的而不是 bug；
- 台账**只有趋势线判定，没有灾难线**：−8% 那条要成本价，全市场没有「假想成本价」这个量。所以
  线上持仓的 sell 会比台账多（跌得深的那批），后验表里的 sell 行不代表线上完整口径。

用法:
    python scripts/build_judgement_ledger.py                    # 全量重建
    python scripts/build_judgement_ledger.py --limit 200        # 抽样调试
    python scripts/build_judgement_ledger.py --report           # 只打印后验表，不落盘
    python scripts/build_judgement_ledger.py --years 10         # 只留最近 10 年
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stock_selector_api" / "plugin"))

from portfolio.rules import load_rules  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
MEMBER_FILE = ROOT / "data" / "sector_heat" / "member.parquet"
OUT_DIR = ROOT / "data" / "judgement_ledger"
LOG_DIR = ROOT / "logs"

KLINE_COLUMNS = ["date", "close", "ma5", "ma10", "rsi14"]
FWD_LABEL = 10          # 学业窗口：T+10 交易日
FWD_FLIP = 20           # 「这条 hold 多久会变成 sell」的观察窗
MAX_BOARD = 600         # 成分太多的板块是分类噪声，不当「板块」
MIN_BOARD = 5
MIN_POSTERIOR_N = 300   # 低于这个样本量的格子不输出可信度数字
MIN_ERA_N = 300         # 单段时代样本太少就不参与同号判定，宁可不给结论
CREDIBLE_WIDTH_PP = 2.0  # 95% 区间宽于 2pp 就判「样本不足」

# 热度箱：下界（含），最后一个上界 100。「无归属」单独一箱，不和冷板块混
HEAT_BINS: tuple[tuple[str, float, float], ...] = (
    ("无板块归属", np.nan, np.nan),
    ("0-44", 0.0, 44.0),
    ("44-70", 44.0, 70.0),
    ("70-90", 70.0, 90.0),
    ("90-100", 90.0, 100.0),
)
ACTION_NAME = {1: "sell", 2: "add", 3: "hold"}

logger = logging.getLogger("judgement_ledger")


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(LOG_DIR / f"ledger_{time.strftime('%Y%m%d')}.log",
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def load_kline(files: list[Path]) -> tuple[list[str], list[str], dict[str, np.ndarray]]:
    """读全市场日线到稠密矩阵（日期 × 代码）。返回 (dates, codes, {列名: ndarray})。

    分两遍扫：第一遍只收日期轴，第二遍逐票填完就把 DataFrame 扔掉。一遍存不下——6,600 个
    DataFrame 连字符串日期就上 GB，而这台机器上还跑着线上服务。
    """
    seen_dates: set[str] = set()
    universe: dict[str, Path] = {}
    for path in files:
        try:
            dates = pd.read_parquet(path, columns=["date"])["date"]
        except Exception as exc:  # 坏文件不能让整张表起不来
            logger.debug("跳过 %s: %s", path.name, exc)
            continue
        if len(dates) < 60:
            continue
        seen_dates.update(pd.to_datetime(dates).dt.strftime("%Y-%m-%d").tolist())
        universe[path.stem] = path
    date_list = sorted(seen_dates)
    code_list = sorted(universe)
    axis = np.array(date_list, dtype="datetime64[ns]")
    shape = (len(date_list), len(code_list))
    arr = {c: np.full(shape, np.nan, dtype=np.float32) for c in ("close", "ma5", "ma10", "rsi14")}
    for j, code in enumerate(code_list):
        try:
            df = pd.read_parquet(universe[code], columns=KLINE_COLUMNS)
        except Exception as exc:
            logger.debug("跳过 %s: %s", universe[code].name, exc)
            continue
        rows = np.searchsorted(axis, pd.to_datetime(df["date"]).to_numpy())
        for col in arr:
            arr[col][rows, j] = df[col].to_numpy(dtype=np.float32)
    logger.info("矩阵 %d 交易日 × %d 代码", shape[0], shape[1])
    return date_list, code_list, arr


def judge_matrix(arr: dict[str, np.ndarray], rules: dict[str, str]) -> tuple[np.ndarray, np.ndarray]:
    """按插件规则串逐格判定 → int8：1=sell 2=add 3=hold 0=不能判。

    规则串与插件同真源（`load_rules()`），但台账是全市场稠密矩阵，只对**可向量化**的形状做批量。
    规则被 JSON 改成复合表达式（含 and/or）时宁可报错停下来，也不要在这里静默降级成另一种口径——
    台账和线上判定不一致，后验就成了假数。
    """
    close, ma5, ma10, rsi = arr["close"], arr["ma5"], arr["ma10"], arr["rsi14"]
    simple = {
        "price <= ma10 * 0.95": lambda: close <= ma10 * np.float32(0.95),
        "": lambda: np.zeros(close.shape, dtype=bool),
        "close >= ma5 * 1.03 and rsi14 <= 65": lambda: (close >= ma5 * np.float32(1.03)) & (rsi <= 65),
    }
    for name, fn in (("sell", rules["sell"]), ("add", rules["add"])):
        if fn not in simple:
            raise SystemExit(
                f"台账只能批量求值已知形状的规则，{name}={fn!r} 不在其中。"
                f"要么改回默认（与回测同构的简单比较），要么把台账口径一起改成向量化实现，"
                f"不能在这里悄悄按另一套规则算。")
    sell = simple[rules["sell"]]()
    add = simple[rules["add"]]()
    action = np.zeros(close.shape, dtype=np.int8)
    judged = np.isfinite(close) & np.isfinite(ma10)
    action[judged] = 3
    action[judged & add] = 2
    action[judged & sell] = 1          # sell 优先，和 judge_holding 一致
    return action, sell


def heat_matrix(code_list: list[str], close: np.ndarray) -> np.ndarray:
    """个股热度 = 所属板块近 3 日等权涨幅的当日横截面百分位（取最高那个板块）。"""
    if not MEMBER_FILE.is_file():
        logger.warning("缺 %s，热度全部记为「无板块归属」", MEMBER_FILE)
        return np.full(close.shape, np.nan, dtype=np.float32)
    mem = pd.read_parquet(MEMBER_FILE, columns=["board_code", "stock_code", "end_date"])
    mem = mem[mem["end_date"] == mem["end_date"].max()]
    mem["stock_code"] = mem["stock_code"].astype(str).str.zfill(6)
    ci = {c: i for i, c in enumerate(code_list)}
    boards = []
    for board_code, grp in mem.groupby("board_code"):
        cols = np.array([ci[c] for c in grp["stock_code"].tolist() if c in ci], dtype=np.int64)
        if MIN_BOARD <= cols.size <= MAX_BOARD:
            boards.append(cols)
    logger.info("板块 %d 个（成分数 %d~%d）", len(boards), MIN_BOARD, MAX_BOARD)
    with np.errstate(divide="ignore", invalid="ignore"):
        r3 = np.full(close.shape, np.nan, dtype=np.float32)
        r3[3:] = close[3:] / close[:-3] - 1.0
    heat = np.full(close.shape, np.nan, dtype=np.float32)
    live = np.isfinite(r3)
    for cols in boards:
        lv = live[:, cols]
        # 手写 mean 而不是 nanmean：整行全 NaN 时 nanmean 会刷 RuntimeWarning，日调度日志会被淹
        tot = np.where(lv, np.nan_to_num(r3[:, cols], nan=0.0), 0.0).sum(axis=1)
        cnt = lv.sum(axis=1)
        b3 = np.full(close.shape[0], np.nan, dtype=np.float32)
        np.divide(tot, cnt, out=b3, where=cnt > 0)
        ok = np.isfinite(b3)
        if ok.sum() < 30:
            continue
        n = int(ok.sum())
        local = np.empty(n, dtype=np.float32)
        local[b3[ok].argsort()] = np.arange(n, dtype=np.float32)
        rank = np.full(len(b3), np.nan, dtype=np.float32)
        rank[ok] = local / max(n - 1, 1) * 100.0
        cur = heat[:, cols]
        heat[:, cols] = np.fmax(cur, rank[:, None])
    return heat


def labels(action: np.ndarray, sell: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """学业：T+10 收盘收益、T+20 收盘收益、20 日内是否自己变成 sell。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd10 = np.full(close.shape, np.nan, dtype=np.float32)
        fwd10[: close.shape[0] - FWD_LABEL] = (close[FWD_LABEL:] / close[: close.shape[0] - FWD_LABEL] - 1.0)
        fwd20 = np.full(close.shape, np.nan, dtype=np.float32)
        fwd20[: close.shape[0] - FWD_FLIP] = (close[FWD_FLIP:] / close[: close.shape[0] - FWD_FLIP] - 1.0)
    n_rows, n_cols = close.shape
    cum = np.zeros((n_rows + 1, n_cols), dtype=np.int32)
    np.cumsum(sell.astype(np.int32), axis=0, out=cum[1:])
    flip = np.zeros(close.shape, dtype=bool)
    # 未来 FWD_FLIP 根内是否出现过 sell（不含当天）；末尾不足窗口的行留 False，那些行也没有学业
    stop = n_rows - FWD_FLIP
    flip[:stop] = (cum[FWD_FLIP + 1: FWD_FLIP + 1 + stop] - cum[1: 1 + stop]) > 0
    return fwd10, fwd20, flip


def heat_bin(heat: np.ndarray) -> np.ndarray:
    """热度 → 箱号（int8，-1 之外都对应 HEAT_BINS 下标）。"""
    out = np.full(heat.shape, 0, dtype=np.int8)
    finite = np.isfinite(heat)
    out[finite & (heat <= 44.0)] = 1
    out[finite & (heat > 44.0) & (heat <= 70.0)] = 2
    out[finite & (heat > 70.0) & (heat < 90.0)] = 3
    out[finite & (heat >= 90.0)] = 4
    return out


def build_rows(date_list: list[str], code_list: list[str], action: np.ndarray,
               heat: np.ndarray, fwd10: np.ndarray, fwd20: np.ndarray,
               flip: np.ndarray) -> pd.DataFrame:
    """稠密矩阵 → 逐日逐票一行，只留「有判定且有学业」的格子。

    date/code 存成 category，分箱只在抽出来的一维切片上做：全市场矩阵有 2,500 万格，
    多留一个 int8 矩阵就是 100 MB，而这台机器上还跑着线上服务。
    """
    flat = np.flatnonzero((action != 0) & np.isfinite(fwd10))
    n_cols = len(code_list)
    heat_1d = heat.ravel()[flat]
    return pd.DataFrame({
        "date": pd.Categorical.from_codes(flat // n_cols, categories=date_list),
        "code": pd.Categorical.from_codes(flat % n_cols, categories=code_list),
        "action": action.ravel()[flat],
        "heat": heat_1d,
        "heat_bin": heat_bin(heat_1d),
        "fwd_ret_10": fwd10.ravel()[flat],
        "fwd_ret_20": fwd20.ravel()[flat],
        "flip_sell_20": flip.ravel()[flat].astype(np.int8),
    })


def beta_interval(successes: int, total: int, prior_a: float = 1.0,
                  prior_b: float = 1.0) -> tuple[float, float, float]:
    """Beta(prior) 后验的 (均值, 2.5%, 97.5%)。scipy 不在就退化成正态近似。"""
    a, b = prior_a + successes, prior_b + total - successes
    mean = a / (a + b)
    try:
        from scipy.stats import beta as _beta
        return mean, float(_beta.ppf(0.025, a, b)), float(_beta.ppf(0.975, a, b))
    except Exception:
        se = (mean * (1 - mean) / (a + b + 1)) ** 0.5
        return mean, max(mean - 1.96 * se, 0.0), min(mean + 1.96 * se, 1.0)


def summarize(ledger: pd.DataFrame) -> pd.DataFrame:
    """按 (action × 热度箱) 聚合后验，另附每个状态的「全部」行。

    「全部」行是给诊断兜底用的：线上一只票今天算不出板块热度时，仍然要有一个可核对的数字，
    而不是整条可信度缺失。
    """
    ledger = ledger.assign(era=_era_ids(pd.to_datetime(ledger["date"]).astype("int64").to_numpy()))
    rows = []
    for (act, bin_id), grp in ledger.groupby(["action", "heat_bin"], observed=True):
        rows.append(_cell_row(grp, int(act), HEAT_BINS[int(bin_id)][0]))
    for act, grp in ledger.groupby("action", observed=True):
        rows.append(_cell_row(grp, int(act), "全部"))
    out = pd.DataFrame([r for r in rows if r is not None])
    if not out.empty:
        out = out.sort_values(["action", "heat_bin"]).reset_index(drop=True)
    return out


def _cell_row(grp: pd.DataFrame, act: int, bin_label: str) -> dict | None:
    """一个格子的后验：上涨频率 + 尾部 + 转 sell 频率 + 区间宽度 + 可信标记。样本不足返回 None。"""
    n = len(grp)
    if n < MIN_POSTERIOR_N:
        return None
    up = int((grp["fwd_ret_10"] > 0).sum())
    mean, lo, hi = beta_interval(up, n)
    width_pp = (hi - lo) * 100
    tail = grp["fwd_ret_10"]
    flip_rate = float(grp["flip_sell_20"].mean()) if act == 3 else np.nan
    eras = _era_means(grp)
    return {
        "action": ACTION_NAME[act],
        "heat_bin": bin_label,
        "n": n,
        "up_rate_posterior": round(mean, 5),
        "ci_low": round(lo, 5),
        "ci_high": round(hi, 5),
        "ci_width_pp": round(width_pp, 2),
        "credible": bool(width_pp < CREDIBLE_WIDTH_PP),
        "fwd10_mean_pct": round(float(tail.mean()) * 100, 3),
        "fwd10_median_pct": round(float(tail.median()) * 100, 3),
        "fwd10_p5_pct": round(float(tail.quantile(0.05)) * 100, 2),
        "fwd20_mean_pct": round(float(grp["fwd_ret_20"].mean()) * 100, 3),
        "flip_sell_20_pct": round(flip_rate * 100, 2) if np.isfinite(flip_rate) else np.nan,
        "era_fwd_mean_pct": ",".join("nan" if not np.isfinite(v) else f"{v*100:+.2f}" for v in eras),
        "era_sign_stable": bool(_stable(eras)),
    }


def _era_ids(ord_ns: np.ndarray) -> np.ndarray:
    """三段时代的编号（0/1/2）。

    分界取**全体台账**日期分布的三分位，不是每格自己的：否则热箱和冷箱的「同一时代」
    对应不同年份，跨格的 gap 就没法比。
    """
    cuts = np.quantile(ord_ns, [1 / 3, 2 / 3])
    out = np.ones(ord_ns.shape, dtype=np.int8)
    out[ord_ns < cuts[0]] = 0
    out[ord_ns >= cuts[1]] = 2
    return out


def _era_means(grp: pd.DataFrame) -> list[float]:
    """三段时代的前看收益均值（跨代同号才敢用）。

    判据取收益而非上涨频率：频率只能告诉你抛不抛得过硬币，均值才是能不能付得起换手费。
    """
    part = grp["era"].to_numpy()
    ret = grp["fwd_ret_10"].to_numpy()
    return [float(ret[part == e].mean()) if (part == e).sum() >= MIN_ERA_N else np.nan
            for e in (0, 1, 2)]


def _stable(eras: list[float]) -> bool:
    """至少两段、且可见段全正或全负（缺段不算否）。"""
    vals = [e for e in eras if np.isfinite(e)]
    if len(vals) < 2:
        return False
    return all(v > 0 for v in vals) or all(v < 0 for v in vals)


def write_outputs(ledger: pd.DataFrame, summary: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("ledger_*.parquet"):
        old.unlink()
    for year, grp in ledger.groupby(pd.to_datetime(ledger["date"]).dt.year):
        path = OUT_DIR / f"ledger_{int(year)}.parquet"
        grp.to_parquet(path, index=False)
    summary.to_parquet(OUT_DIR / "posterior.parquet", index=False)
    logger.info("台账 %d 行 → %s/ledger_<year>.parquet；后验 %d 行 → posterior.parquet",
                len(ledger), OUT_DIR, len(summary))


def render_report(summary: pd.DataFrame) -> str:
    lines = ["状态 × 热度 → 后验上涨概率（Beta(1,1) 先验，学业=T+10 交易日，不含成本）"]
    for _, r in summary.iterrows():
        tag = "可信" if r["credible"] else "样本不足"
        if r["action"] == "add" and not r["credible"]:
            tag += "·不可用"
        lines.append(
            f"  {r['action']:4s} {str(r['heat_bin']):12s} n={r['n']:9,}  "
            f"上涨 {r['up_rate_posterior']*100:5.2f}% [{r['ci_low']*100:.2f},{r['ci_high']*100:.2f}] "
            f"宽{r['ci_width_pp']:5.2f}pp {tag}  "
            f"均值{r['fwd10_mean_pct']:+6.2f}% 中位{r['fwd10_median_pct']:+6.2f}% "
            f"p5={r['fwd10_p5_pct']:+6.2f}%"
            + ("" if pd.isna(r["flip_sell_20_pct"]) else f" 20日转卖{r['flip_sell_20_pct']:.1f}%")
            + f"  时代{'同号' if r['era_sign_stable'] else '不同号'}"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="判定记账台账 + 后验可信度表")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 只票（调试用）")
    ap.add_argument("--years", type=int, default=0, help="只保留最近 N 个自然年")
    ap.add_argument("--report", action="store_true", help="只打印后验表，不写文件")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    rules = load_rules()
    logger.info("判定规则（与插件同真源）: sell=%r add=%r 灾难线=-%s%%",
                rules["sell"], rules["add"], rules.get("disaster_stop_pct"))

    files = sorted(KLINE_DIR.glob("*.parquet"))
    if args.limit:
        files = files[: args.limit]
    t0 = time.time()
    date_list, code_list, arr = load_kline(files)
    action, sell = judge_matrix(arr, rules)
    del arr["ma5"], arr["ma10"], arr["rsi14"]   # 上 GB 的稠密矩阵，用完一段扔一段
    heat = heat_matrix(code_list, arr["close"])
    fwd10, fwd20, flip = labels(action, sell, arr["close"])
    del arr, sell
    ledger = build_rows(date_list, code_list, action, heat, fwd10, fwd20, flip)
    del action, heat, fwd10, fwd20, flip
    if args.years:
        cut = pd.Timestamp.now().normalize() - pd.DateOffset(years=args.years)
        ledger = ledger[pd.to_datetime(ledger["date"]) >= cut]
    summary = summarize(ledger)
    logger.info("台账 %d 行，用时 %.0fs", len(ledger), time.time() - t0)
    print(render_report(summary))
    if not args.report:
        write_outputs(ledger, summary)


if __name__ == "__main__":
    main()
