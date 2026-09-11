"""20日窗口12特征 + 标签规则 + 全市场数据集构建。"""

import multiprocessing as mp
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from stock_selector.features.base_columns import (
    add_base_columns,
    board_limit_threshold,
    is_limit_up,
)

WINDOW = 20
VOL_CLIP_MED = 8


def clean_volume(df: pd.DataFrame) -> pd.DataFrame:
    """Clip volume to 8x rolling(20) median. Returns a copy."""
    out = df.copy()
    med = out["volume"].rolling(20).median()
    out["volume"] = out["volume"].clip(upper=VOL_CLIP_MED * med)
    return out


def compute_window_features(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """Sort by date and compute 12 window features aligned to df rows."""
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    pct = c.pct_change() * 100.0

    feat = pd.DataFrame(index=df.index)
    feat["date"] = df["date"]

    feat["pct_20d"] = c / c.shift(WINDOW) - 1

    def _slope(series, w):
        vals = series.to_numpy(dtype=float)
        out = np.full(len(vals), np.nan)
        x = np.arange(w, dtype=float)
        for i in range(w - 1, len(vals)):
            seg = vals[i - w + 1 : i + 1]
            if np.isnan(seg).any():
                continue
            m = np.polyfit(x, seg, 1)[0]
            out[i] = m / np.nanmean(seg)
        return pd.Series(out, index=series.index)

    feat["slope_20d"] = _slope(c, WINDOW)

    roll60_max = c.rolling(60).max()
    roll60_min = c.rolling(60).min()
    feat["pos_60d"] = (c - roll60_min) / (roll60_max - roll60_min + 1e-9)

    feat["ma5_ma20"] = c.rolling(5).mean() / c.rolling(20).mean()

    vol_ma5 = v.rolling(5).mean()
    vol_ratio = v / vol_ma5.replace(0, np.nan)
    feat["vol_ratio_avg"] = vol_ratio.rolling(WINDOW).mean()

    up_vol = v.where(pct > 0, 0.0)
    dn_vol = v.where(pct < 0, 0.0)
    up_sum = up_vol.rolling(WINDOW).sum()
    dn_sum = dn_vol.rolling(WINDOW).sum()
    feat["up_day_vol_ratio"] = up_sum / dn_sum.replace(0, np.nan)

    feat["vol_slope"] = _slope(v, WINDOW)

    feat["vol_corr_pct"] = pct.rolling(WINDOW).corr(v)

    roll20_max = c.rolling(WINDOW).max()
    feat["drawdown_20d"] = (1 - c / roll20_max).rolling(WINDOW).max()

    thr = board_limit_threshold(code)
    test_lim = ((pct > 7.0) & (pct < thr)).astype(float)
    feat["test_limit_cnt"] = test_lim.rolling(WINDOW).sum().shift(1)

    lu_flag = (pct >= thr).astype(float)
    feat["limit_up_early"] = lu_flag.rolling(WINDOW).sum().shift(1)

    cum3 = pct.rolling(3).sum()
    feat["max_gain_3d"] = cum3.rolling(WINDOW).max().shift(1)

    return feat


def assign_labels(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """Assign labels: 1=positive, 0=negative, NaN=fuzzy/non-candidate."""
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    pct = c.pct_change() * 100.0

    lu = pct.apply(lambda p: is_limit_up(code, p))
    ret20 = c / c.shift(20) - 1

    roll60_max = c.rolling(60).max()
    roll60_min = c.rolling(60).min()
    pos60 = (c - roll60_min) / (roll60_max - roll60_min + 1e-9)

    lu_float = lu.astype(float)
    lu20_past = lu_float.rolling(20).sum().shift(1)
    lu10_future = lu_float.rolling(10).sum().shift(-10)

    candidate = (
        ret20.between(0.02, 0.25)
        & pos60.between(0.15, 0.55)
        & (lu20_past <= 1)
    )

    label = pd.Series(np.nan, index=df.index)
    label[candidate & (lu10_future >= 3)] = 1
    label[candidate & (lu10_future <= 1)] = 0

    return pd.DataFrame({"date": df["date"], "label": label})


def _process_one(path: Path) -> Optional[pd.DataFrame]:
    """Process a single parquet file. Returns DataFrame or None."""
    try:
        df = pd.read_parquet(path)
        if len(df) < 80:
            return None

        code = df["code"].iloc[0]

        df = clean_volume(df)
        df["vol_ma5"] = df["volume"].rolling(5).mean()
        df = add_base_columns(df)

        feat = compute_window_features(df, code)
        lab = assign_labels(df, code)

        merged = feat.merge(lab, on="date", how="inner")
        merged["code"] = code

        feat_cols = [c for c in merged.columns if c not in ("code", "date", "label")]
        merged = merged.dropna(subset=feat_cols)

        return merged[["code", "date"] + feat_cols + ["label"]]
    except Exception as exc:
        import warnings
        warnings.warn(f"skip {path.name}: {exc}")
        return None


def build_dataset(
    kline_dir: Path, processes: int = 8, limit: int = 0
) -> pd.DataFrame:
    """Scan parquet files and build the full market feature+label dataset."""
    paths = sorted(Path(kline_dir).glob("*.parquet"))
    if limit > 0:
        paths = paths[:limit]

    with mp.Pool(processes) as pool:
        results = pool.map(_process_one, paths)

    dfs = [r for r in results if r is not None]
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)
