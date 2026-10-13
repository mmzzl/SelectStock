"""训练「买入风险分」模型：预测同日基线口径下的 10 日超额收益，用来**剔除**候选，不用来排序。

为什么只做剔除（见 README《涨停与风险分》）：换成全样本可成交数据训练后，高分腿其实
也稳定为正（逐年 8/8、零成本年化超额 +8~12%），所以"模型只会挑不能买"是早期在有偏子样本
（只用板后池）里训练出的假象。真正判死买入腿的是成本：10 日轮动的换手成本 = 243×往返费/持有天数
≈ 12%/年，单边 25bp 就把超额吃平，而能装下仓位的又是日成交不到 1 亿的小票。
剔除腿则相反——只减候选不加换手，扣不扣成本结论都一样（全市场 +5.4%/年、trend_rebound 池
+0.28pp/10 日），所以交付形态是闸门，不是买点。

产出：
  data/models/risk_ex10.joblib      最终模型（全样本重训）
  data/models/risk_ex10.meta.json   特征清单、绝对剔除阈值、walk-forward 验证结果

用法：
  python3 scripts/train_risk_model.py                 # 默认 1/3 抽样，约 1700 只
  python3 scripts/train_risk_model.py --stride 1      # 全市场（内存需要 ~6GB）
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from stock_selector.features.risk_features import (  # noqa: E402
    FEATURE_NAMES, MIN_BARS, RAW_COLUMNS, build_features,
)

DEFAULT_OUT = ROOT / "data" / "models"
LABEL = "r10"
# 标签向前看 10 根 bar，所以折间留 40 天空窗，相邻样本不重叠泄漏
START_DATE = "2016-01-01"
FOLDS = [(pd.Timestamp("2018-12-31"), pd.Timestamp("2019-03-01"), pd.Timestamp("2020-12-31")),
         (pd.Timestamp("2020-12-31"), pd.Timestamp("2021-03-01"), pd.Timestamp("2022-12-31")),
         (pd.Timestamp("2022-12-31"), pd.Timestamp("2023-03-01"), pd.Timestamp("2024-12-31")),
         (pd.Timestamp("2024-12-31"), pd.Timestamp("2025-03-01"), pd.Timestamp("2026-12-31"))]


def build_panel(files: list[Path]) -> pd.DataFrame:
    """逐只算特征 + 前向收益，拼成一张票-日长表。"""
    feats, labels = [], []
    t0 = time.time()
    for i, path in enumerate(files, 1):
        try:
            d = pd.read_parquet(path, columns=RAW_COLUMNS)
        except Exception as exc:
            print(f"  跳过 {path.name}: {exc}")
            continue
        if "date" not in d.columns:
            continue
        d["date"] = pd.to_datetime(d["date"].astype(str).str.slice(0, 10))
        d = d.sort_values("date").reset_index(drop=True)
        if len(d) < MIN_BARS:
            continue
        f = build_features(d)
        c = d["close"].astype(float)
        lab = pd.DataFrame({
            "code": path.stem,
            "date": d["date"],
            "r1": (c.shift(-1) / c - 1).values,
            "r5": (c.shift(-5) / c - 1).values,
            "r10": (c.shift(-10) / c - 1).values,
            "r20": (c.shift(-20) / c - 1).values,
        })
        keep = (lab["r10"].notna() & (lab["date"] >= pd.Timestamp(START_DATE))).values
        feats.append(f[keep].reset_index(drop=True))
        labels.append(lab[keep].reset_index(drop=True))
        if i % 200 == 0:
            print(f"  {i}/{len(files)} 只，累计 {sum(len(x) for x in labels):,} 行"
                  f"，{time.time() - t0:.0f}s", flush=True)
    if not feats:
        raise SystemExit("没有生成任何样本，检查 data/stock_kline 是否已构建")
    fx = pd.concat(feats, ignore_index=True)
    lb = pd.concat(labels, ignore_index=True)
    del feats, labels
    panel = pd.concat([fx, lb], axis=1)
    del fx, lb
    return panel.astype({**{c: "float32" for c in FEATURE_NAMES},
                         "r1": "float32", "r5": "float32", "r10": "float32", "r20": "float32"})


def day_matched_excess(panel: pd.DataFrame) -> pd.DataFrame:
    """ex10 = r10 - 同日全体票-日 r10 均值。扣掉市场 β，模型才只能学相对强弱。"""
    panel["ex10"] = panel["r10"] - panel.groupby("date")["r10"].transform("mean")
    return panel


def fit(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(
        max_iter=150, learning_rate=0.07, max_leaf_nodes=15, min_samples_leaf=300,
        l2_regularization=2.0, early_stopping=False, random_state=0)
    model.fit(X, y)
    return model


def daily_ic(sub: pd.DataFrame, col: str) -> tuple[float, float, int]:
    s = sub.groupby("date").apply(
        lambda x: spearmanr(x[col], x["ex10"]).correlation if len(x) > 40 else np.nan,
        include_groups=False).dropna()
    if not len(s):
        return float("nan"), float("nan"), 0
    return float(s.mean()), float(s.mean() / (s.std() / np.sqrt(len(s)))), len(s)


def leg_table(e: pd.DataFrame, mask, label: str) -> None:
    """打印剔除批次的时间稳定性：日序 t、分年符号、四个子时期符号。"""
    rej, keep = e[mask], e[~mask]
    d = rej.groupby("date")["ex10"].mean()
    dk = keep.groupby("date")["ex10"].mean()
    print(f"\n  [{label}] 剔除 {len(rej):,} 行（{len(rej) / len(e):.0%}）"
          f" ex10 {100 * d.mean():+.2f}pp (t{d.mean() / (d.std() / np.sqrt(len(d))):+6.1f})"
          f" | 保留 {len(keep):,} 行 ex10 {100 * dk.mean():+.2f}pp")
    years = rej.groupby(rej["date"].dt.year)["ex10"].mean()
    print("      分年 ex10(pp): " + "  ".join(f"{int(k)}:{100 * v:+.2f}" for k, v in years.items()))
    print(f"      {int((years < 0).sum())}/{len(years)} 年为负")
    for name, a, b in (("19H2-20H1", "2019-07-01", "2020-06-30"),
                       ("20H2-21", "2020-07-01", "2021-12-31"),
                       ("22-23", "2022-01-01", "2023-12-31"),
                       ("24-26", "2024-01-01", "2026-12-31")):
        g = rej[(rej["date"] >= pd.Timestamp(a)) & (rej["date"] <= pd.Timestamp(b))]
        if len(g) < 200:
            continue
        dd = g.groupby("date")["ex10"].mean()
        print(f"      {name:<10} n={len(g):>7,} 剔除批次 ex10 {100 * dd.mean():+.2f}pp"
              f"(t{dd.mean() / (dd.std() / np.sqrt(len(dd))):+5.1f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "data" / "stock_kline"))
    ap.add_argument("--stride", type=int, default=3, help="每隔 N 个文件取一只（1=全市场）")
    ap.add_argument("--cap", type=int, default=1_500_000, help="单次训练最多样本数（内存保护）")
    ap.add_argument("--reject-pct", type=float, default=0.30)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--oof-out", default="",
                    help="把 walk-forward 拼接的样本外打分另存成 parquet（回测装剔除层要用它）")
    ap.add_argument("--no-final", action="store_true",
                    help="只出样本外打分，不重训/不覆盖 data/models/ 里的现役模型")
    args = ap.parse_args()

    all_files = [p for p in sorted(Path(args.dir).glob("*.parquet"))
                 if not p.stem.startswith(("1", "5"))]
    files = all_files[::args.stride]
    print(f"股票文件 {len(all_files)} 只，本次抽样 {len(files)} 只（stride={args.stride}）")

    panel = day_matched_excess(build_panel(files))
    print(f"面板 {len(panel):,} 行 / {panel['code'].nunique():,} 只 /"
          f" {panel['date'].nunique():,} 个交易日（{panel['date'].min():%Y-%m} ~ {panel['date'].max():%Y-%m}）")

    # 可成交口径：涨停当日收盘价买不进，评估与阈值都只用 lu==0
    eval_df = panel[(panel["lu"] == 0) & panel["ex10"].notna()].reset_index(drop=True)
    print(f"评估集（lu==0）{len(eval_df):,} 行")

    print("\n=== walk-forward 四折（折间空窗 40 天）===")
    stitched = np.full(len(eval_df), np.nan)
    fold_meta = []
    for tr_end, te_start, te_end in FOLDS:
        fitm = panel[panel["date"] <= tr_end]
        if len(fitm) > args.cap:
            fitm = fitm.sample(args.cap, random_state=0)
        model = fit(fitm[FEATURE_NAMES].values.astype("float32"),
                    fitm["ex10"].values.astype("float32"))
        te = eval_df[(eval_df["date"] >= te_start) & (eval_df["date"] <= te_end)]
        pred = model.predict(te[FEATURE_NAMES].values.astype("float32"))
        stitched[te.index.values] = pred
        ic, t, days = daily_ic(te.assign(score=pred), "score")
        print(f"  {te_start:%Y}-{te_end:%Y} 训练 {len(fitm):>9,} 评估 {len(te):>8,}"
              f" 日IC {ic:+.4f}(t{t:+5.1f}, {days}天)")
        fold_meta.append({"fold": f"{te_start:%Y-%m}~{te_end:%Y-%m}", "n_train": int(len(fitm)),
                          "n_eval": int(len(te)), "daily_ic": round(ic, 4), "ic_t": round(t, 1)})

    ok = np.isfinite(stitched)
    print(f"\n拼接样本外 {int(ok.sum()):,} 行 / {eval_df[ok]['date'].nunique():,} 天")

    # 两种剔除口径：每日分位（需要当天全市场打分）vs 绝对阈值（单只就能判，实盘用这个）
    e = eval_df[ok].copy()
    e["score"] = stitched[ok]
    abs_thr = float(np.quantile(e["score"], args.reject_pct))
    print(f"绝对阈值（样本外打分 {args.reject_pct:.0%} 分位）= {abs_thr:.6f}"
          f"  即预测 10 日超额低于 {100 * abs_thr:+.2f}pp 就剔")

    for name, mask in (("每日分位", e["score"].rank(pct=True) <= args.reject_pct),
                       ("绝对阈值", e["score"] <= abs_thr)):
        leg_table(e, mask, name)

    if args.oof_out:
        # 回测装剔除层只能用这份拼接样本外分：现役模型是全样本重训的，拿它打历史=前视。
        e.assign(reject=(e["score"] <= abs_thr).astype("int8")).to_parquet(args.oof_out, index=False)
        print(f"\n样本外打分 → {args.oof_out}"
              f"（{len(e):,} 行 / {e['code'].nunique():,} 只 / 阈值 {abs_thr:+.6f}）")

    print("\n=== 放行腿对照（前30%，证明它不能当排序用）===")
    top = e[e["score"].rank(pct=True) >= 1 - args.reject_pct]
    d = top.groupby("date")["ex10"].mean()
    print(f"  ex10 {100 * d.mean():+.2f}pp (t{d.mean() / (d.std() / np.sqrt(len(d))):+6.1f})")
    yr = top.groupby(top["date"].dt.year)["ex10"].mean()
    print("  分年 " + "  ".join(f"{int(k)}:{100 * v:+.2f}" for k, v in yr.items()))

    if args.no_final:
        print("\n=== --no-final：跳过全样本重训与落盘，现役 data/models/ 未改动 ===")
        return

    print("\n=== 最终模型：全样本重训并落盘 ===")
    fitm = panel
    if len(fitm) > args.cap:
        fitm = fitm.sample(args.cap, random_state=0)
    final = fit(fitm[FEATURE_NAMES].values.astype("float32"),
                 fitm["ex10"].values.astype("float32"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, out_dir / "risk_ex10.joblib")
    meta = {
        "model": "risk_ex10",
        "trained_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "label": "ex10 = 10日前向收益 - 同日全体票-日均值（扣市场β）",
        "n_stocks": int(panel["code"].nunique()),
        "n_rows": int(len(panel)),
        "n_train_rows": int(len(fitm)),
        "date_range": [str(panel["date"].min().date()), str(panel["date"].max().date())],
        "features": FEATURE_NAMES,
        "reject_threshold_ex10": abs_thr,
        "reject_pct": args.reject_pct,
        "score_semantics": "预测 10 日同日基线超额收益（小数）。低于阈值=建议剔除；高分不代表可买。",
        "walk_forward": fold_meta,
        "file_stride": args.stride,
    }
    (out_dir / "risk_ex10.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {out_dir / 'risk_ex10.joblib'}")
    print(f"  {out_dir / 'risk_ex10.meta.json'}  阈值 {abs_thr:+.6f}")
    print(f"  面板留档 {out_dir / 'risk_panel.parquet'}")
    panel.to_parquet(out_dir / "risk_panel.parquet")


if __name__ == "__main__":
    main()
