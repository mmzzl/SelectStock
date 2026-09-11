"""连板吸筹效应识别 —— GaussianNB 训练/预测 CLI。

用法:
    python scripts/train_bayes.py --train                          # 全流程训练+验证报告
    python scripts/train_bayes.py --predict --top-n 100 [--date 2026-09-08]
    python scripts/train_bayes.py --code 003040
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from stock_selector.features.modeling import TOP_NS, evaluate_time_split  # noqa: E402
from stock_selector.features.window import FEATURE_COLUMNS, assign_labels, build_dataset, clean_volume  # noqa: E402
from stock_selector.features.base_columns import add_base_columns  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = ROOT / "data" / "stock_kline"
MODELS_DIR = ROOT / "models"
REPORTS_DIR = ROOT / "reports"
CAND_DIR = ROOT / "data" / "bayes_candidates"

logger = logging.getLogger("train_bayes")
DEFAULT_PRIOR = 0.15


def _g(new_dir: Path) -> None:
    new_dir.mkdir(parents=True, exist_ok=True)


def diag_code(code: str, kline_dir: Path):
    """单票诊断：打印最新交易日的 12 特征 + 前窗计数 + 打分（若有模型）。"""
    path = Path(kline_dir) / f"{code}.parquet"
    df = pd.read_parquet(path)
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df = clean_volume(df)
    df = add_base_columns(df)
    from stock_selector.features.window import compute_window_features

    feat = compute_window_features(df, code)
    lab = assign_labels(df, code)
    row = df.merge(feat, on="date").merge(lab, on="date")
    row[FEATURE_COLUMNS] = row[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan)
    row = row.dropna(subset=FEATURE_COLUMNS)
    if row.empty:
        logger.info("[DIAG] %s 无完整特征窗口", code)
        return
    tail = row.iloc[-1]
    lines = ["code=%s  date=%s" % (code, tail["date"]), "  label=%s" % tail.get("label")]
    for f in FEATURE_COLUMNS:
        lines.append("  %s=%.4g" % (f, tail[f]))
    logger.info("\n".join(lines))


def train(kline_dir: Path, models_dir: Path, reports_dir: Path,
          model_file: Path, report_file: Path, prior_pos: float):
    t0 = time.time()
    ds = build_dataset(kline_dir)
    ds = ds.dropna(subset=["label"])
    if ds.empty:
        raise SystemExit("无可用训练样本，检查 kline_dir=%s" % kline_dir)

    X = ds[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan)
    fill_vals = X.median().fillna(0.0)
    X = X.fillna(fill_vals).to_numpy(dtype=float)
    y = ds["label"].to_numpy(dtype=int)
    dates = pd.to_datetime(ds["date"]).to_numpy()

    res = evaluate_time_split(X, y, dates, prior_pos=prior_pos, top_ns=TOP_NS)
    payload = {
        "model": res["model"],
        "features": FEATURE_COLUMNS,
        "prior_pos": prior_pos,
        "fill_vals": fill_vals,
        "train_start": pd.Timestamp(dates.min()).strftime("%Y-%m-%d"),
        "train_end": pd.Timestamp(dates.max()).strftime("%Y-%m-%d"),
    }
    _g(models_dir)
    _g(reports_dir)
    with open(model_file, "wb") as fh:
        joblib.dump(payload, fh)

    base = res["base_rate"]
    rows = ["# 连板吸筹效应识别 · 训练验证报告", "",
            "- 训练样本: %d (正 %d / 负 %d, 正样本率 %.3f%%)"
            % (len(y), int(y.sum()), int((y == 0).sum()), 100.0 * base),
            "- 时间切分: 前 80%% 日期训练 / 后 20%% 验证 (test=%d, 验证集正样本 %d)"
            % (res["test_size"], res["n_pos_valid"]),
            "- 先验(正类): %.2f" % prior_pos,
            "",
            "| 指标 | 值 | 随机基线 |",
            "|---|---|---|",
            "| AUC | %.4f | 0.5000 |" % res["auc"],
            "| Average Precision | %.4f | %.4f |" % (res["avg_precision"], base),
            "| precision@50 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_50"], base),
            "| precision@100 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_100"], base),
            "| precision@200 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_200"], base),
            "",
            "### 误报复盘 (验证集 top200 false-positive)",
            "见 `reports/miss_predictions.csv`。"]
    report_file.write_text("\n".join(rows), encoding="utf-8")

    miss = res["misses"].sort_values("proba", ascending=False).head(200)
    miss = miss[miss["label"] == 0]
    miss.to_csv(reports_dir / "miss_predictions.csv", index=False, encoding="utf-8-sig")
    logger.info("train done in %.1fs. model=%s report=%s",
                time.time() - t0, model_file, report_file)


def predict(kline_dir: Path, cands_dir: Path, model_file: Path, top_n: int, asof: str | None):
    with open(model_file, "rb") as fh:
        payload = joblib.load(fh)
    model = payload["model"]
    features = payload["features"]
    fill_vals = payload.get("fill_vals")

    cand_rows = []
    for path in sorted(Path(kline_dir).glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
            if len(df) < 80:
                continue
            df["vol_ma5"] = df["volume"].rolling(5).mean()
            df = clean_volume(df)
            df = add_base_columns(df)
            code = str(df["code"].iloc[0])
            from stock_selector.features.window import compute_window_features

            feat = compute_window_features(df, code)
            lab = assign_labels(df, code)
            row = df.merge(feat, on="date").merge(lab, on="date")
            keep = row[features].notna().all(axis=1)
            row = row[keep]
            if row.empty:
                continue
            if asof is not None:
                row = row[row["date"] <= asof]
            latest = row.iloc[-1]
            x = pd.Series(
                latest[features].to_numpy(dtype=float).astype(float), index=features
            ).replace([np.inf, -np.inf], np.nan)
            if fill_vals is not None:
                x = x.fillna(fill_vals.fillna(0.0))
            proba = float(model.predict_proba(x.to_numpy(dtype=float).reshape(1, -1))[0, 1])
            cand_rows.append({
                "code": code, "date": latest["date"], "label": latest.get("label"),
                "proba": proba,
            })
        except Exception as exc:  # 单票失败跳过
            logger.warning("[SKIP] %s: %s", path.name, exc)

    out = pd.DataFrame(cand_rows).sort_values("proba", ascending=False).head(top_n)
    _g(cands_dir)
    fname = (asof or pd.Timestamp.today().strftime("%Y-%m-%d"))
    target = cands_dir / f"{fname}.parquet"
    out.to_parquet(target, index=False, compression="zstd")
    logger.info("Top%d 候选已写入 %s", top_n, target)
    print(out[["code", "date", "proba"]].to_string(index=False))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="连板吸筹效应识别 · GaussianNB 训练/预测")
    parser.add_argument("--train", action="store_true", help="全流程训练+验证报告")
    parser.add_argument("--predict", action="store_true", help="输出当日 TopN 候选")
    parser.add_argument("--code", type=str, default=None, help="单票特征/打分诊断")
    parser.add_argument("--top-n", type=int, default=100, help="预测候选数(默认100)")
    parser.add_argument("--date", type=str, default=None, help="预测基准日 (YYYY-MM-DD)")
    parser.add_argument("--prior-pos", type=float, default=DEFAULT_PRIOR, help="正类先验(默认0.15)")
    parser.add_argument("--kline-dir", type=str, default=str(KLINE_DIR))
    parser.add_argument("--models-dir", type=str, default=str(MODELS_DIR))
    parser.add_argument("--reports-dir", type=str, default=str(REPORTS_DIR))
    parser.add_argument("--model-file", type=str, default=None)
    parser.add_argument("--report-file", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    models_dir = Path(args.models_dir)
    reports_dir = Path(args.reports_dir)
    model_file = Path(args.model_file) if args.model_file else models_dir / "bayes_limitup.joblib"
    report_file = Path(args.report_file) if args.report_file else reports_dir / "bayes_train_report.md"

    if sum([args.train, args.predict, args.code is not None]) != 1:
        parser.error("--train/--predict/--code 三者必须且只能选一个")
    if args.code:
        diag_code(args.code, Path(args.kline_dir))
    elif args.train:
        train(Path(args.kline_dir), models_dir, reports_dir, model_file, report_file, args.prior_pos)
    elif args.predict:
        predict(Path(args.kline_dir), CAND_DIR, model_file, args.top_n, args.date)


if __name__ == "__main__":
    main()
