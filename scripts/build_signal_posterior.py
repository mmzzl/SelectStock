"""策略信号后验：每次「选股命中」在历史上值多少钱，作为选股依据落盘。

回答的是选股侧的「有依据」：不能只说「命中了 box_oscillate 的四条判据」，还要说
**历史上这条判据打中的点，往后 10/20 个交易日跑赢当天全样本多少**。

口径沿用既有验证方法（见 README「同日基线」与判定台账）：
- 超额 = 该点 T+10 收益 − 同一天样本内所有股票的 T+10 均值。不减掉当天大盘，
  牛市里任何规则都"有效"。
- 上涨概率用 Beta(1,1) 后验 + 95% 区间，区间宽 >2pp 判样本不足。
- 三轴稳定性：全体日期三分位（时代）同号 + 等距抽样换样本（offset）同号，
  两个都过才叫稳；上一轮 pattern_reversal 就是栽在换样本翻号。

产物 `data/signal_posterior/signal_posterior.parquet`，每策略一行（含分时代/分样本明细），
供 8002 插件在每条选股结果上挂「历史后验」。

用法:
    python3 scripts/build_signal_posterior.py
    python3 scripts/build_signal_posterior.py --strategies box_oscillate --universe 400
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KLINE_DIR = ROOT / "data" / "stock_kline"
OUT_DIR = ROOT / "data" / "signal_posterior"
MIN_N = 300
CREDIBLE_WIDTH_PP = 2.0


def _sample(codes: list[str], n: int, offset: int) -> list[str]:
    if n >= len(codes):
        return codes
    step = len(codes) // n
    return [codes[(i * step + offset) % len(codes)] for i in range(n)]


def beta_interval(successes: int, total: int) -> tuple[float, float, float]:
    """Beta(1,1) 后验的 (均值, 2.5%, 97.5%)。"""
    from scipy import stats as st

    a, b = successes + 1.0, (total - successes) + 1.0
    try:
        lo, hi = st.beta.ppf([0.025, 0.975], a, b)
    except Exception:
        p = a / (a + b)
        se = (p * (1 - p) / (a + b + 1)) ** 0.5
        lo, hi = max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)
    return a / (a + b), lo, hi


def load_universe(codes: list[str], start: str, end: str, lookback: int) -> pd.DataFrame:
    """样本股票的 (date, code, fwd10, fwd20) + 策略需要的其余列。

    只保留 `[start-lookback, end]` 这段：切片要够 lookback 行，而 `end` 之后 10 日的
    前向收益无法在窗口内闭合，直接被 NaN 排除——比"借未来的钱"诚实。
    """
    parts = []
    for code in codes:
        path = KLINE_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("date").reset_index(drop=True)
        c = pd.to_numeric(df["close"], errors="coerce")
        df["code"] = code
        df["fwd10"] = c.shift(-10) / c - 1.0
        df["fwd20"] = c.shift(-20) / c - 1.0
        dates = df["date"].to_numpy()
        lo = max(0, int(np.searchsorted(dates, start)) - lookback)
        hi = int(np.searchsorted(dates, end))
        if hi <= lo:
            continue
        parts.append(df.iloc[lo:hi])
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def signal_points(strategy, panel: pd.DataFrame, frames: dict, start: str, end: str,
                  lookback: int) -> list[dict]:
    """逐股按日切片问策略「这天会不会买」，命中就记下当日 T+10/T+20 与当日基准。

    切片窗口固定 `lookback` 行（= 策略声明的 required_rows），与实盘选股取数一致；
    日期是升序 ISO 串，所以用 searchsorted 定位区间端点，不做逐行相等匹配。
    """
    day_ref = panel.groupby("date")["fwd10"].mean()
    hits: list[dict] = []
    for code, df in frames.items():
        dates = df["date"].to_numpy()
        f10 = df["fwd10"].to_numpy(dtype=float)
        f20 = df["fwd20"].to_numpy(dtype=float)
        n = len(df)
        for p in range(int(np.searchsorted(dates, start)), int(np.searchsorted(dates, end))):
            if p < lookback or p + 10 >= n:
                continue
            if strategy.signal_at(df.iloc[p - lookback + 1: p + 1]) is None:
                continue
            ref = day_ref.get(str(dates[p]), np.nan)
            hits.append({
                "date": str(dates[p]), "code": code,
                "fwd10": f10[p],
                "fwd20": f20[p] if p + 20 < n else np.nan,
                "excess10": f10[p] - ref if np.isfinite(ref) else np.nan,
            })
    return hits


def summarize(name: str, era: str, offset: int, hits: list[dict]) -> dict | None:
    if not hits:
        return None
    h = pd.DataFrame(hits)
    ex = h["excess10"].dropna()
    if ex.empty:
        return None
    wins = int((ex > 0).sum())
    mean, lo, hi = beta_interval(wins, len(ex))
    f10 = h["fwd10"].dropna()
    f20 = h["fwd20"].dropna()
    # 日等权口径：先按天取当天命中的均值，再对天求平均。全池行等权会被「命中扎堆的恐慌日」
    # 主导，同一判据在两种加权下能反号（本项目的既定汇报口径是日等权，两个都落盘）。
    daily = h.assign(ex=h["excess10"]).dropna(subset=["ex"]).groupby("date")["ex"].mean()
    return {
        "strategy": name, "era": era, "offset": offset, "n": len(ex),
        "beat_rate_posterior": round(mean, 5),
        "ci_low": round(lo, 5), "ci_high": round(hi, 5),
        "ci_width_pp": round((hi - lo) * 100, 2),
        "credible": bool((hi - lo) * 100 < CREDIBLE_WIDTH_PP),
        "excess10_mean_pp": round(float(ex.mean()) * 100, 3),
        "excess10_median_pp": round(float(ex.median()) * 100, 3),
        "excess10_p5_pp": round(float(ex.quantile(0.05)) * 100, 2),
        "excess10_daymean_pp": round(float(daily.mean()) * 100, 3),
        "excess10_day_pos_rate": round(float((daily > 0).mean()), 4) if len(daily) else np.nan,
        "days": int(len(daily)),
        "fwd10_mean_pct": round(float(f10.mean()) * 100, 3),
        "fwd10_p5_pct": round(float(f10.quantile(0.05)) * 100, 2),
        "fwd20_mean_pct": round(float(f20.mean()) * 100, 3) if len(f20) else np.nan,
        "signals_per_day": round(len(h) / max(1, h["date"].nunique()), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="策略信号后验（选股依据落盘）")
    ap.add_argument("--strategies", default="box_oscillate,pattern_reversal,trend_rebound")
    ap.add_argument("--eras", default="2019-09-01:2020-09-01,2022-09-01:2023-09-01,2025-09-01:2026-09-01")
    ap.add_argument("--offsets", default="0,137,274")
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--out", default=str(OUT_DIR / "signal_posterior.parquet"))
    args = ap.parse_args()

    from stock_selector.core.selector import create_strategy

    all_codes = sorted(p.stem for p in KLINE_DIR.glob("*.parquet"))
    eras = [tuple(e.split(":")) for e in args.eras.split(",")]
    names = args.strategies.split(",")
    rows: list[dict] = []
    for offset_str in args.offsets.split(","):
        offset = int(offset_str)
        codes = _sample(all_codes, args.universe, offset)
        for name in names:
            # 每个时代单独取数：窗口只留 lookback+ era 跨度，避免把 20 年全市场读进内存
            lookback = max(int(create_strategy(name).required_rows() or 0), 60)
            for start, end in eras:
                t0 = time.time()
                panel = load_universe(codes, start, end, lookback)
                if panel.empty:
                    print(f"  {name:18s} 时代{start[:4]} +{offset} 无数据", flush=True)
                    continue
                frames = {c: g.reset_index(drop=True) for c, g in panel.groupby("code")}
                hits = signal_points(create_strategy(name), panel, frames, start, end, lookback)
                r = summarize(name, start[:4], offset, hits)
                if r:
                    rows.append(r)
                    print(f"  {name:18s} 时代{start[:4]} +{offset} n={r['n']:5d} "
                          f"跑赢{r['beat_rate_posterior']*100:5.2f}% "
                          f"[{r['ci_low']*100:.2f},{r['ci_high']*100:.2f}] "
                          f"超额均值{r['excess10_mean_pp']:+6.2f}pp "
                          f"绝对10日{r['fwd10_mean_pct']:+6.2f}% ({time.time()-t0:.0f}s)", flush=True)
                else:
                    print(f"  {name:18s} 时代{start[:4]} +{offset} 无命中", flush=True)
                del frames, panel, hits

    if not rows:
        print("没有任何信号，未写文件")
        return
    out = pd.DataFrame(rows)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out, index=False)
    print(f"\n已写 {args.out}（{len(out)} 行）")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
