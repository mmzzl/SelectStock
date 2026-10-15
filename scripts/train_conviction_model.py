"""训练可信度（剧本成功率）模型：LightGBM + 净化式时序验证 + 分位档校准。

为什么要换掉那张 81 格 Beta 表（2026-10-15 审计的四条，本脚本逐条处理）：
1. **样本池混了场内基金**：1,445 只 ETF/LOF/REITs 被旧的 `classify_board` 默认支归进主板，
   占后验分母 5.92%（某一主板格里占 17.12%）。同样 +10%/-5%/10 日屏障，基金止盈率 22.6%
   vs 股票 30.9%，混池把股票格子拉偏 2pp——比它自己印出来的 95%CI 宽度还大。
   → 基金单成一类（`bucket.BOARD_FUND`）进模型当特征，不再和股票抢同一格。
2. **窗口全重叠**：逐日滑动的 10 日窗口里，相邻样本共用 9 根前向 bar，n 被当成独立样本用，
   CI 窄了 3.16 倍（中位），可信格 73→28。→ 训练仍用全部行（重叠不破坏条件均值的一致性），
   但**验证窗口按「代码内序号 % horizon == 0」去重叠**，文案里的 N 取去重叠后的独立窗口数。
3. **时代混池**：2006→2026 一锅后验，前/后段格子止盈率中位漂移 5.55pp，排名相关只有 0.50。
   → 净化式（purged）时序前推验证，逐时代报 AUC/Brier/校准；校准表默认只用 `--calib-from`
     之后的样本外数据，让「概率」对齐当下行情而不是十四年均值。
4. **中性被踢出分母却不说**：→ 契约写死在 meta 和文案里，模型给的是**条件概率**
   P(先触止盈 | 10 日内先触某一侧)，不是期望收益，也不含「都没触」那 44%。

标签沿用 build_conviction_samples.py 的三屏障契约（horizon/stop/take 由命令行落进 meta），
特征只允许线上 provider 取得到的列（见 SERVE_FEATURES 的注释），一根未来 bar 都不碰。

ST 剔除：日线 parquet 只有**当前**简称（实测 6631 个文件的 name 列全表恒定），历史 ST 状态
不可恢复；而 ±5% 涨跌停会让 +10%/-5% 的屏障几何变形。宁可不给分，也不套一个在非 ST
样本上学出来的概率——线上遇 ST 持仓由插件返回 None。

用法:
    python scripts/train_conviction_model.py --smoke            # 前 300 个行组，几分钟
    python scripts/train_conviction_model.py                   # 全量（~6.5M 行，十几分钟）
    python scripts/train_conviction_model.py --calib-from 2023-01-01 --bins 12
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.conviction import bucket  # noqa: E402
from stock_selector.conviction import features as fx  # noqa: E402

SAMPLES_FILE = ROOT / "data" / "conviction" / "samples.parquet"
KLINE_DIR = ROOT / "data" / "stock_kline"
MODEL_FILE = ROOT / "data" / "conviction" / "conviction_model.lgb.txt"
META_FILE = ROOT / "data" / "conviction" / "conviction_model.json"
LOG_DIR = ROOT / "logs"

# 读样本表只要这些列：全量 10.66M 行，多读一列就把可用内存吃掉一截。
READ_COLUMNS = ["date", "code", "close", "board", "bias20", "pos60", "vol_ratio",
                "vol_z", "rsi14", "adx14", "atr_n", "lu", "label"]

# 特征契约（列序 / 必需项）与线上插件同源，见 stock_selector/conviction/features.py：
# 训练和线上各自拼特征迟早会拼出「AUC 好看但概率是错的」这种不报错的偏差。
NUMERIC_FEATURES = fx.NUMERIC_FEATURES
FEATURES = fx.FEATURES
BOARD_COL = fx.BOARD_COL
REQUIRED_FEATURES = fx.REQUIRED_FEATURES

MIN_DATA_IN_LEAF = 5000        # 相关性极高的重叠样本，叶子必须粗，不然拟合的是同一段行情
EMERBAGO_DAYS = 25             # 10 个交易日跨春节能拉到 ~15 个自然日，25 天留得住

logger = logging.getLogger("conviction_model")


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


def st_codes(kline_dir: Path) -> set[str]:
    """当前简称里带 ST 的代码。只读 name 一列，6631 个文件约 40s。"""
    out: set[str] = set()
    files = sorted(kline_dir.glob("*.parquet"))
    for f in files:
        try:
            name = pq.read_table(f, columns=["name"]).column("name").to_pylist()
        except Exception as exc:
            logger.debug("跳过 %s：%s", f.name, exc)
            continue
        if name and bucket.is_st_name(name[-1]):
            out.add(f.stem)
    logger.info("ST 代码 %d 个（共扫描 %d 个日线文件），训练集整体剔除", len(out), len(files))
    return out


def load_samples(path: Path, exclude_codes: set[str], horizon: int,
                 max_groups: int = 0) -> pd.DataFrame:
    """流式读样本 → 训练用行。

    留下的行：label ∈ {0,1}（中性剔除，见模块 docstring）、close 有效、三个必需特征齐全。
    其余数值列允许 NaN——LightGBM 自己学缺失方向，线上缺 rsi14 之类不至于整格不给分。
    `seq` 是该票内第几根样本行，用于验证窗口去重叠（缺陷 2）。
    """
    pf = pq.ParquetFile(path)
    n_groups = pf.metadata.num_row_groups
    if max_groups:
        n_groups = min(n_groups, max_groups)
    frames = []
    t0 = time.time()
    for i in range(n_groups):
        chunk = pf.read_row_group(i, columns=READ_COLUMNS).to_pandas()
        if chunk["code"].nunique() == 1:
            seq = np.arange(len(chunk), dtype=np.int32)
        else:
            code = chunk["code"].astype(str)
            seq = code.groupby(code).cumcount().to_numpy(dtype=np.int32)
        label = chunk["label"].to_numpy()
        keep = np.isin(label, (0, 1))
        if exclude_codes:
            keep &= ~chunk["code"].isin(exclude_codes).to_numpy()
        keep &= np.isfinite(chunk["close"].to_numpy(dtype=float)) & (chunk["close"] > 0)
        for feat in REQUIRED_FEATURES:
            keep &= np.isfinite(chunk[feat].to_numpy(dtype=float))
        if not keep.all():
            chunk = chunk.loc[keep]
            seq = seq[keep]
        if chunk.empty:
            continue
        chunk = chunk.copy()
        chunk["seq"] = seq
        frames.append(chunk[list(dict.fromkeys([*READ_COLUMNS, "seq"]))])
        if (i + 1) % 500 == 0 or i + 1 == n_groups:
            logger.info("读样本 %d/%d 行组，累计 %d 行 (%.0fs)",
                        i + 1, n_groups, sum(len(f) for f in frames), time.time() - t0)
    if not frames:
        raise SystemExit("没有可用样本行")
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    # board 一律按 code 现算：样本文件里的 board 列是旧口径写的（基金并进主板），不能信。
    df["board"] = df["code"].astype(str).map(bucket.classify_board)
    df["lu"] = df["lu"].astype(np.int8)
    logger.info("可用样本 %d 行 / %d 只 / %s→%s", len(df), df["code"].nunique(),
                df["date"].min().date(), df["date"].max().date())
    for b, grp in df.groupby("board", observed=True):
        lab = grp["label"].to_numpy()
        s = int((lab == 1).sum())
        f = int((lab == 0).sum())
        logger.info("  %-6s 行 %9d  独立窗口 %8d  止盈率 %.2f%%",
                    b, len(grp), (grp["seq"] % horizon == 0).sum(), 100.0 * s / (s + f))
    return df


def to_arrays(df: pd.DataFrame):
    """特征矩阵 + 切分要用的三样东西。

    board 走 category codes（列序最后）：线上按 meta.board_levels 查同一个下标，
    不依赖 pandas 的推断顺序。数值列一律 float32——全量 ~6.5M 行，float64 会把内存吃光。
    """
    cols = [df[f].to_numpy(dtype="float32") for f in NUMERIC_FEATURES]
    codes = pd.Categorical(df["board"], categories=bucket.BOARD_CATEGORIES,
                           ordered=True).codes.astype("float32")
    cols.append(codes)
    x = np.asarray(cols).T.astype(np.float32, order="C")
    return x, df["label"].to_numpy(dtype=np.int8), \
        df["date"].map(pd.Timestamp.toordinal).to_numpy(dtype=np.int32), \
        df["seq"].to_numpy(dtype=np.int32)


def lgb_params(args) -> dict:
    return {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "learning_rate": args.lr,
        "num_leaves": args.num_leaves,
        "max_depth": -1,
        "min_data_in_leaf": MIN_DATA_IN_LEAF,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "lambda_l2": 1.0,
        "num_threads": args.threads,
        "seed": args.seed,
        "verbosity": -1,
    }


def fit_fold(x, label, train_idx, valid_idx, params, iterations):
    """一折：train_idx 拟合，valid_idx 早停，返回 (booster, best_iter, 验证行概率)。"""
    import lightgbm as lgb

    tr = lgb.Dataset(x[train_idx], label=label[train_idx],
                     categorical_feature=[BOARD_COL], free_raw_data=False)
    va = lgb.Dataset(x[valid_idx], label=label[valid_idx],
                     categorical_feature=[BOARD_COL], reference=tr, free_raw_data=False)
    booster = lgb.train(params, tr, num_boost_round=iterations, valid_sets=[va],
                        callbacks=[lgb.early_stopping(50, verbose=False),
                                   lgb.log_evaluation(0)])
    best = booster.best_iteration or iterations
    proba = booster.predict(x[valid_idx], num_iteration=best)
    del tr, va
    return booster, int(best), proba


def metrics(y, p):
    from sklearn.metrics import brier_score_loss, roc_auc_score

    out = {"n": int(len(y)), "base_rate": round(float(np.mean(y)), 6)}
    if len(np.unique(y)) > 1:
        out["auc"] = round(float(roc_auc_score(y, p)), 4)
        out["brier"] = round(float(brier_score_loss(y, p)), 6)
    return out


def calibrate(score: np.ndarray, label: np.ndarray, bins: int) -> list[dict]:
    """分位分档 + PAVA 保单调，得到 score→实测止盈率 的阶梯校准。

    输入必须是**去重叠、带 embargo 的样本外行**（缺陷 2/3 的修复就落在这一步的 N 上）。
    档位是阶梯函数：线上取所在档的实测值当概率，所以印出来的 N 是真独立窗口数，
    不再是从重叠行数推出来的假 95%CI。
    """
    edges = np.unique(np.quantile(score, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return []
    idx = np.clip(np.searchsorted(edges, score, side="right") - 1, 0, len(edges) - 2)
    rows = []
    for b in range(len(edges) - 1):
        m = idx == b
        if not m.any():
            continue
        rows.append({"lo": float(edges[b]), "hi": float(edges[b + 1]),
                     "n": int(m.sum()), "n_success": int(label[m].sum()),
                     "rate": float(label[m].mean())})
    # 加权 PAVA：相邻档违序就按独立窗口数合并，保证概率随分数单调不降。
    while True:
        bad = next((i for i in range(len(rows) - 1) if rows[i]["rate"] > rows[i + 1]["rate"]), None)
        if bad is None:
            break
        a, c = rows[bad], rows[bad + 1]
        n = a["n"] + c["n"]
        merged = {"lo": a["lo"], "hi": c["hi"], "n": n,
                  "n_success": a["n_success"] + c["n_success"],
                  "rate": (a["n_success"] + c["n_success"]) / n}
        rows[bad:bad + 2] = [merged]
    logger.info("校准表 %d 档（独立窗口合计 %d）", len(rows), sum(r["n"] for r in rows))
    for r in rows:
        logger.info("  score %.4f-%.4f  实测 %.2f%%  N=%d",
                    r["lo"], r["hi"], r["rate"] * 100, r["n"])
    return rows


def calibrate_lookup(table: list[dict], score: float):
    """样本外概率 → 校准后概率（所在档实测值）；越界用最近档（阶梯两端是开放区间）。"""
    if not table:
        return None
    for r in table:
        if r["lo"] <= score <= r["hi"]:
            return r
    return table[-1] if score > table[-1]["hi"] else table[0]


def level_of(pct: float, base_pct: float) -> str:
    """相对**同类基准**的高/中/低（沿用建表口径：剧本成功率天然 ~30%，绝对阈值会把所有票判成低）。

    基准按板块取（见 base_rate_by_board_pct）：基金摸到 +10% 本来就难，拿全市场均值当基准
    会让所有基金票永远「低」，那一档就不再描述「同类形态里相对更容易达标」。
    """
    if base_pct <= 0:
        return "低"
    if pct >= base_pct * 1.05:
        return "高"
    if pct <= base_pct * 0.95:
        return "低"
    return "中"


def main() -> None:
    ap = argparse.ArgumentParser(description="可信度模型（LightGBM 剧本成功率）")
    ap.add_argument("--samples", default=str(SAMPLES_FILE))
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--out-model", default=str(MODEL_FILE))
    ap.add_argument("--out-meta", default=str(META_FILE))
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--stop", type=float, default=0.05)
    ap.add_argument("--take", type=float, default=0.10)
    ap.add_argument("--valid-start", default="2019-01-01", help="第一折验证窗口起点")
    ap.add_argument("--fold-days", type=int, default=365, help="每个验证窗口的自然日宽度")
    ap.add_argument("--calib-from", default="2022-01-01",
                    help="校准表只用这天及以后的样本外行（对齐当下行情）")
    ap.add_argument("--bins", type=int, default=10, help="校准分位数档数")
    ap.add_argument("--iterations", type=int, default=400, help="每折最大迭代（早停截断）")
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--num-leaves", type=int, default=31)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep-st", action="store_true", help="不剔 ST（默认剔）")
    ap.add_argument("--smoke", action="store_true", help="只读前 300 个行组，快速自检")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    import lightgbm as lgb

    samples_path = Path(args.samples)
    if not samples_path.is_file():
        logger.error("样本表不存在：%s（先跑 build_conviction_samples.py）", samples_path)
        raise SystemExit(2)
    exclude = set() if args.keep_st else st_codes(Path(args.kline_dir))
    df = load_samples(samples_path, exclude, args.horizon, 300 if args.smoke else 0)
    span = (str(df["date"].min().date()), str(df["date"].max().date()))
    n_rows = len(df)
    x, label, day, seq = to_arrays(df)
    del df

    cutoff0 = pd.Timestamp(args.valid_start).toordinal()
    calib_from = pd.Timestamp(args.calib_from).toordinal()

    # 净化式前推：train = 窗口起点前 EMERBAGO_DAYS 自然日及以前；验证窗口内去重叠。
    # embargo 是为了不让训练行 t 的 10 日前向窗口伸进验证段（那样验证就是训练的记忆）。
    oof_score = np.full(n_rows, np.nan)
    best_iters = []
    folds = []
    start = cutoff0
    max_day = int(day.max())
    while start <= max_day:
        end = start + args.fold_days
        in_val = (day >= start) & (day < end) & (seq % args.horizon == 0)
        val_idx = np.flatnonzero(in_val)
        train_idx = np.flatnonzero(day < start - EMERBAGO_DAYS)
        if len(val_idx) < 2000 or len(train_idx) < 100_000:
            start = end
            continue
        booster, best, proba = fit_fold(x, label, train_idx, val_idx,
                                        lgb_params(args), args.iterations)
        best_iters.append(best)
        oof_score[val_idx] = proba
        m = metrics(label[val_idx], proba)
        m["window"] = f"{pd.Timestamp.fromordinal(int(start)).date()}~{pd.Timestamp.fromordinal(int(end - 1)).date()}"
        m["train_n"] = int(len(train_idx))
        m["era"] = "pre" if start < calib_from else "post"
        folds.append(m)
        logger.info("折 %s  训练 %d 行 → 验证 %d 独立窗口  AUC=%.4f Brier=%.5f 基准=%.2f%% best_iter=%d",
                    m["window"], m["train_n"], m["n"], m.get("auc", float("nan")),
                    m.get("brier", float("nan")), m["base_rate"] * 100, best)
        del booster
        start = end
    if not folds:
        logger.error("验证折为空（--valid-start/--fold-days 与数据区间不匹配）")
        raise SystemExit(2)

    have = np.flatnonzero(~np.isnan(oof_score))
    pooled = metrics(label[have], oof_score[have])
    logger.info("样本外合计 %d 独立窗口：AUC=%.4f Brier=%.5f 基准止盈率=%.2f%%",
                pooled["n"], pooled["auc"], pooled["brier"], pooled["base_rate"] * 100)

    # 按板块各自的样本外基准：基金天然比股票难摸到 +10%（22.6% vs 30.9%），
    # 用全市场均值当基准会让所有基金票永远「低」，档位就丢了「同类里相对更容易」的原意。
    board_codes = x[have, BOARD_COL].astype(int)
    base_by_board = {}
    for code, grp_mask in pd.Series(board_codes).value_counts().items():
        idx = have[board_codes == code]
        name = bucket.BOARD_CATEGORIES[int(code)]
        base_by_board[name] = round(float(np.mean(label[idx])) * 100, 2)
        logger.info("板块基准 %-6s 样本外 %6d 独立窗口  止盈率 %.2f%%", name, len(idx),
                    base_by_board[name])

    cal_idx = have[day[have] >= calib_from]
    if len(cal_idx) < 20_000:
        logger.warning("校准窗口样本外行只有 %d（<20k），改用全部折", len(cal_idx))
        cal_idx = have
    table = calibrate(oof_score[cal_idx], label[cal_idx], args.bins)
    cal_metrics = metrics(label[cal_idx], np.array([
        (calibrate_lookup(table, s) or {"rate": float("nan")})["rate"] for s in oof_score[cal_idx]]))
    logger.info("校准后（%s 及以后，%d 独立窗口）：AUC=%.4f Brier=%.5f",
                args.calib_from, cal_metrics["n"], cal_metrics["auc"], cal_metrics["brier"])

    # 终版：全量重拟合，迭代数取各折 best_iteration 中位数（此时没有干净验证集可早停）。
    iters = max(1, int(np.median(best_iters)))
    logger.info("终版拟合：全量 %d 行，num_iteration=%d（各折中位）", x.shape[0], iters)
    train_all = lgb.Dataset(x, label=label, categorical_feature=[BOARD_COL],
                            free_raw_data=False)
    booster = lgb.train(lgb_params(args), train_all, num_boost_round=iters)

    gain = booster.feature_importance(importance_type="gain")
    order = np.argsort(gain)[::-1]
    logger.info("特征重要度（gain）：")
    for i in order:
        logger.info("  %-10s %.1f", FEATURES[i], gain[i])

    base_pct = pooled["base_rate"] * 100.0
    rates = np.array([calibrate_lookup(table, s)["rate"] * 100 for s in oof_score[have]])
    boards = board_codes
    dist = pd.Series(rates)
    counts = pd.Series([level_of(r, base_by_board.get(bucket.BOARD_CATEGORIES[int(b)], base_pct))
                        for r, b in zip(rates, boards)]).value_counts().to_dict()
    logger.info("档位分布（按样本外独立窗口，各自板块基准）：%s，全市场基准 %.2f%%", counts, base_pct)
    logger.info("区分度：校准后概率 %.2f%%→%.2f%%（跨度 %.2fpp）",
                dist.min(), dist.max(), dist.max() - dist.min())

    meta = {
        "version": 1,
        "model_file": Path(args.out_model).name,
        "trained_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label_contract": {
            "horizon": args.horizon, "stop": args.stop, "take": args.take,
            "meaning": ("P(未来 horizon 根内先触 close*(1+take) | 先触 take 或先触 stop 之一；"
                        "同根双触算止损)。中性窗口已剔除，这不是期望收益。"),
            "neutral_excluded": True,
        },
        "universe": {
            "boards": list(bucket.BOARD_CATEGORIES),
            "st_excluded": not args.keep_st,
            "st_codes": len(exclude),
            "funds": "场内基金单成一类（与股票屏障统计不同，不再并进主板）",
        },
        "features": FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "required_features": list(REQUIRED_FEATURES),
        "board_levels": list(bucket.BOARD_CATEGORIES),
        "params": lgb_params(args),
        "num_iteration": iters,
        "best_iterations": best_iters,
        "data": {
            "samples": str(samples_path), "rows": int(n_rows), "range": list(span),
        },
        "validation": {
            "scheme": (f"purged walk-forward，窗口 {args.fold_days} 自然日，"
                       f"embargo {EMERBAGO_DAYS} 自然日，验证内按 seq % {args.horizon} 去重叠"),
            "folds": folds, "pooled": pooled, "calibration_era": args.calib_from,
        },
        "base_rate_pct": round(base_pct, 2),
        "base_rate_by_board_pct": base_by_board,
        "level_thresholds": {"high": 1.05, "low": 0.95},
        "level_counts": {str(k): int(v) for k, v in counts.items()},
        "discrimination": {
            "calibrated_rate_min_pp": round(float(dist.min()), 2),
            "calibrated_rate_max_pp": round(float(dist.max()), 2),
            "spread_pp": round(float(dist.max() - dist.min()), 2),
            "note": "校准后概率的样本外跨度；接近 0 就说明这几列特征基本没有区分度，"
                    "页面该说「和历史同类差不多」而不是给个好看的数",
        },
        "calibration": table,
        "calibrated_metrics": cal_metrics,
        "importance_gain": {FEATURES[i]: round(float(gain[i]), 2) for i in order},
    }

    out_model = Path(args.out_model)
    out_model.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_model), num_iteration=iters)
    Path(args.out_meta).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已写 %s（%d 行 Booster）与 %s", out_model, iters, args.out_meta)


if __name__ == "__main__":
    main()
