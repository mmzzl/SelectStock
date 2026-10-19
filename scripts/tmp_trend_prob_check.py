"""临时诊断：趋势向上的票，未来 5 日上涨的概率到底多高（用户假设检验）。

口径：
- 池 = data/qlib/model_batchQ_lab5/pool.txt（300 只，与批次 Q 同池）。
- 信号日 F（收盘后可知），未来收益 = close(F+5)/close(F+1) - 1（F+1 买入、持有 5 日，
  与线上标签 Ref($close,-5)/Ref($close,-1)-1 同一根尺子）。
- 条件桶：close≥MA20、20 日动量>0、两者都满足、整池基准；另看「20 日涨幅前 10」组合。
- 胜率不算成本，但均值要和单回合往返成本 0.20%（买 0.05% + 卖 0.15%）对齐比。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from qlib.data import D

REPO = Path(__file__).resolve().parents[1]
QLIB_DIR = Path("/data/work/qlib_data/cn")
POOL = (REPO / "data/qlib/model_batchQ_lab5/pool.txt").read_text().split()

BEG, END = "2023-01-01", "2026-10-15"          # 与批次 Q 主窗一致
WARM = "2022-09-01"                             # MA/动量暖机


def main() -> int:
    qlib.init(provider_uri=str(QLIB_DIR), region=qlib.constant.REG_CN, kernels=1,
              expression_instance_num=1, expression_cache_level=0)
    fields = ["$close", "$open", "$high", "$low", "$volume", "$factor"]
    df = D.features(POOL, fields, start_time=WARM, end_time=END)
    df.columns = [c.strip() for c in df.columns]
    close = df["$close"].unstack("instrument")
    calendar = close.index

    def lag(px: pd.DataFrame, k: int) -> pd.DataFrame:
        return px.shift(-k).reindex(calendar)

    fwd5 = lag(close, 5) / lag(close, 1) - 1.0          # 信号日 F → F+1 买 → F+6 卖口径(5日)
    ma20 = close.rolling(20).mean()
    mom20 = close / close.shift(20) - 1.0
    up_limit_hit = lag(close, 1) >= close * 1.095        # 次日近乎一字涨停，买不进（粗筛）

    windows = [("全窗 2023-01→2026-10", BEG, END),
               ("批次 Q 窗 2024-01→2026-10", "2024-01-02", END)]
    for wname, wb, we in windows:
        sl = slice(wb, we)
        c, f, m2, mo = close[sl], fwd5[sl], ma20[sl], mom20[sl]
        mask = f.notna() & mo.notna() & m2.notna()
        buyable = ~up_limit_hit[sl].fillna(False)

        print(f"\n=== {wname}（每格 = 票-日样本；剔除未来收益不可得）===")
        buckets = {
            "整池基准": mask & buyable,
            "close≥MA20": mask & buyable & (c >= m2),
            "20日涨幅>0": mask & buyable & (mo > 0),
            "两者都满足": mask & buyable & (c >= m2) & (mo > 0),
            "跌着(近20日<0)": mask & buyable & (mo < 0),
        }
        for name, bk in buckets.items():
            v = f[bk].values.ravel()
            v = v[np.isfinite(v)]
            if len(v) == 0:
                continue
            print(f"  {name:14s} n={len(v):7d} 涨概率 {(v > 0).mean():.1%}  "
                  f"均值 {v.mean():+.2%}  中位 {np.median(v):+.2%}  "
                  f"均值超成本? {'是' if v.mean() > 0.002 else '否'}")

        # 「买热门」组合版：每天挑 20 日涨幅前 10（可买的），等权
        days = c.index
        basket, hits = [], []
        for d in days:
            row_mo, row_f = mo.loc[d], f.loc[d]
            ok = row_mo.notna() & row_f.notna() & buyable.loc[d] & (c.loc[d] >= m2.loc[d])
            cand = row_mo[ok]
            if len(cand) < 10:
                continue
            top = cand.nlargest(10).index
            r = row_f[top].dropna()
            basket.append(r.mean())
            hits.append((r > 0).mean())
        b = pd.Series(basket, days[:len(basket)])
        print(f"\n  每日「20 日涨幅前 10（且站上 MA20）」组合：样本 {len(b)} 天")
        print(f"    日均收益 {b.mean():+.3%}（往返成本 0.20%，净额 {b.mean()-0.002:+.3%}）"
              f"  日胜率 {(b>0).mean():.1%}  年化 {b.mean()*238:.1%}（未扣成本 "
              f"{(b.mean())*238:.1%} → 扣 {((b.mean())-0.002)*238:.1%}）")
        print(f"    票级平均涨概率 {np.mean(hits):.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
