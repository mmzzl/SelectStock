"""风险模型的因果特征层：训练脚本与实盘打分共用同一段代码。

与 /tmp/ml1.py 的特征口径逐列对齐（那边验出「后30%分位剔除」是八年唯一稳定的结论），
差别只有两点：
  1) 这里不产出任何前向收益列（r1/r5/r10/r20 只属于训练标签，见 scripts/train_risk_model.py）；
  2) 所有列只允许向过去看（shift(+k)、rolling、ewm），因此任意截断点上的最后一行
     与用全历史算出来的同一行完全相同——tests/test_risk_features.py 用这条性质做回归测试。

窗口下限：`age`（距上次涨停的根数）与 `ret60`/`ma20_sl` 需要较长历史，
少于 MIN_BARS 的行不要打分（会被截成 NaN，模型会给出无意义的分）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_BARS = 260

RAW_COLUMNS = [
    "date", "close", "volume", "pct", "lu", "pos60", "vol_ratio", "vol_z",
    "vol_ma5", "vol_ma20", "ma5", "ma10", "ma20", "ma60", "rsi14", "kdj_j",
    "wr14", "atr14", "macd_hist", "boll_upper", "boll_mid", "boll_lower",
    "obv", "roc10", "adx14", "body_ratio", "upper_ratio", "lower_ratio",
    "gap_pct", "vol_trend", "sup20", "res20",
    "vol_contracting", "vol_ground", "vol_blast", "v_reversal", "w_bottom_break",
    "arc_bottom", "hammer_low", "doji_low", "three_soldiers", "pullback_stabilize",
    "vol_diverge", "box_break_up", "hs_bottom_break",
]

FLAG_COLUMNS = {
    "fl_vol_contracting": "vol_contracting",
    "fl_vol_ground": "vol_ground",
    "fl_vol_blast": "vol_blast",
    "fl_v_reversal": "v_reversal",
    "fl_w_bottom": "w_bottom_break",
    "fl_arc_bottom": "arc_bottom",
    "fl_hammer_low": "hammer_low",
    "fl_doji_low": "doji_low",
    "fl_three_soldiers": "three_soldiers",
    "fl_pullback_stab": "pullback_stabilize",
    "fl_vol_diverge": "vol_diverge",
    "fl_box_break_up": "box_break_up",
    "fl_hs_bottom_break": "hs_bottom_break",
}

FEATURE_NAMES = [
    "pct", "lu", "pos60", "vol_ratio", "vol_z", "body", "upper", "lower", "gap",
    "vol_trend", "rsi14", "kdj_j", "wr14", "adx14", "roc10",
    "ret5", "ret20", "ret60", "bias5", "bias20", "bias60", "ma20_sl", "atr_n",
    "macd_n", "boll_pos", "obv_sl", "updn", "updays", "ur10", "lr10", "box10",
    "box20", "dd_hi20", "vol_regime", "res_gap", "sup_gap", "age", "lu20", "lu5",
    *FLAG_COLUMNS.keys(),
]


def build_features(kline: pd.DataFrame) -> pd.DataFrame:
    """把单只股票的指标宽表转成特征矩阵（行序与入参一致）。

    Args:
        kline: 需要含 RAW_COLUMNS；按 date 升序；不足 MIN_BARS 行时末行特征为 NaN。
    """
    d = kline
    c = d["close"].astype(float)
    v = d["volume"].astype(float)
    lu = d["lu"].astype(float)
    atr = d["atr14"].astype(float)
    pct = d["pct"].astype(float)

    # 距上次涨停的根数：涨停日 age=0，此后逐日 +1
    last_lu = pd.Series(np.where(lu > 0, np.arange(len(d)), np.nan)).ffill()
    v20 = v.rolling(20).mean()
    up_v = v.where(pct > 0).rolling(20, min_periods=5).mean()
    dn_v = v.where(pct < 0).rolling(20, min_periods=5).mean()

    f = pd.DataFrame(index=d.index)
    f["pct"] = pct
    f["lu"] = lu
    f["pos60"] = d["pos60"].astype(float)
    f["vol_ratio"] = d["vol_ratio"].astype(float)
    f["vol_z"] = d["vol_z"].astype(float)
    f["body"] = d["body_ratio"].astype(float)
    f["upper"] = d["upper_ratio"].astype(float)
    f["lower"] = d["lower_ratio"].astype(float)
    f["gap"] = d["gap_pct"].astype(float)
    f["vol_trend"] = d["vol_trend"].astype(float)
    f["rsi14"] = d["rsi14"].astype(float)
    f["kdj_j"] = d["kdj_j"].astype(float)
    f["wr14"] = d["wr14"].astype(float)
    f["adx14"] = d["adx14"].astype(float)
    f["roc10"] = d["roc10"].astype(float)
    f["ret5"] = c / c.shift(5) - 1
    f["ret20"] = c / c.shift(20) - 1
    f["ret60"] = c / c.shift(60) - 1
    f["bias5"] = c / d["ma5"].astype(float) - 1
    f["bias20"] = c / d["ma20"].astype(float) - 1
    f["bias60"] = c / d["ma60"].astype(float) - 1
    f["ma20_sl"] = d["ma20"].astype(float) / d["ma20"].astype(float).shift(20) - 1
    f["atr_n"] = atr / c
    f["macd_n"] = d["macd_hist"].astype(float) / c
    f["boll_pos"] = (c - d["boll_lower"].astype(float)) / (
        d["boll_upper"].astype(float) - d["boll_lower"].astype(float) + 1e-9)
    f["obv_sl"] = (d["obv"].astype(float) - d["obv"].astype(float).shift(20)) / (v20 * 20 + 1)
    f["updn"] = up_v / dn_v
    f["updays"] = (pct > 0).rolling(20).mean()
    f["ur10"] = d["upper_ratio"].astype(float).rolling(10).mean()
    f["lr10"] = d["lower_ratio"].astype(float).rolling(10).mean()
    f["box10"] = c.rolling(10).max() / c.rolling(10).min() - 1
    f["box20"] = c.rolling(20).max() / c.rolling(20).min() - 1
    f["dd_hi20"] = c / c.rolling(20).max() - 1
    f["vol_regime"] = v20 / v20.shift(20) - 1
    f["res_gap"] = d["res20"].astype(float) / c - 1
    f["sup_gap"] = d["sup20"].astype(float) / c - 1
    f["age"] = np.arange(len(d)) - last_lu.values
    f["lu20"] = lu.rolling(20).sum().shift(1)
    f["lu5"] = lu.rolling(5).sum().shift(1)
    for name, col in FLAG_COLUMNS.items():
        f[name] = d[col].astype(float)

    return f[FEATURE_NAMES].astype("float32")
