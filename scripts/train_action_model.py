"""训练「持仓动作」模型 + 按预注册的格子验收：这个动作到底值不值得做。

口径预注册在 `stock_selector_api/README.md` §6「持仓动作模型批次 C」与「批次 D」，样本来自
`scripts/build_action_samples.py`。这里只做两件事：

1. **净化式 walk-forward 回归**（H=5/10 各一版）。卖出与加仓是同一个量的两侧（卖侧=预测低到某个分位、
   加侧=高到某个分位），所以不分别训「卖出模型/加仓模型」。
2. **验收只看逐笔五道门槛**（`rule_gates.evaluate_trade`）：≥300 笔、胜率>50%、盈亏比>1、
   扣后每笔>0、按交易日聚类的 t ≥ `t_required(累计试次)`。RMSE / 相关 / 符号命中率只当参考——
   「有依据」的定义是扣完成本还能赚钱，不是拟合得好。

三个臂（`--arms`，一次运行里跑完才算同一批预注册）：
- `c`：批 C 原样——标签 = 原始 `r_H`，特征 = 13 个个股自身列 + 板块。
- `d1`：**只换标签**——训练目标 = `r_H − 同日同 H 全部决策行 r_H 的中位数`（把 β 从学习目标里剥掉）。
- `d2`：`d1` 的标签 + 9 列横截面/市场状态（`build_cross_section.py` 产出），**主判**。
  判定端三个臂都用**未截断的真实 `r_H`** 换算扣费优势 ⇒ 换标签只改模型学什么，不改那把尺子。

三道防自欺的口径：
- **同一个 episode 只算第一次触发**：真实仓位卖一次就没了，同一波里连着 6 天都想卖不能记 6 笔
  （与 `profit_scan.cooldown` 同一个理由）。
- **两条参考线同屏**：`net_per_trade_pp` 是「见了就动」相对不动值多少（判门槛用），
  `excess_pp` 是选出来的子集比「当天所有持仓」平均好多少（α）。只有前者为正、后者≈0，
  那就是挑了个本身在涨的日子，不是动作有依据。
- **格数在跑之前定死**：`臂数 × 2 侧 × len(horizons) 个窗口 × 3 个分位`，整批一次烧进试次台账，
  及格线按「这批预注册的总格数」抬（不是每个臂各抬一次）；读数出来后不许再从格子里挑顺眼的当规则。

用法:
    python3 scripts/train_action_model.py --samples /tmp/act_smoke.parquet --smoke       # 管道自检
    python3 scripts/train_action_model.py --run-id 20261017-action-v1                    # 批 C
    python3 scripts/train_action_model.py --arms d1,d2 --run-id 20261017-action-d-v1     # 批 D（24 格）
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
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import profit_scan  # noqa: E402  复用内存水位读数口径（脚本同目录，不引它的扫描内核）
from stock_selector.backtest import rule_gates  # noqa: E402
from stock_selector.backtest.broker import SELL_FEE_RATE  # noqa: E402
from stock_selector.backtest.executability import REPAIR_MAX_DAYS  # noqa: E402
from stock_selector.backtest.rule_gates import ROUND_TRIP_PP  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402

SAMPLES_FILE = ROOT / "data" / "actions" / "decision_samples.parquet"
OUT_DIR = ROOT / "data" / "actions"
LOG_DIR = ROOT / "logs" / "actions"
TRIAL_LEDGER = ROOT / "logs" / "profit_scan" / "trials.jsonl"
TRIAL_BUDGET = 300_000
MIN_AVAIL_GB = 2.5               # 全量样本 3M 行 × 14 特征 float32 ≈ 170M，加上 booster 与验证折峰值到 2G 级
CUTOFFS = (30, 20, 10)           # 三个分位阈值：卖侧取最低 30/20/10%，加侧取最高 30/20/10%

NUMERIC_FEATURES = ["hold_days", "pnl_pp", "pnl_atr", "bias20", "pos60", "vol_ratio",
                    "vol_z", "rsi14", "adx14", "atr_n", "pct", "dd_hi20", "lu"]
# 批次 D 的新输入（`build_cross_section.py` 产出；`mkt_n` 刻意不要——它是家数计数，只会让模型认出年代）
XS_FEATURES = ["xs_bias20", "xs_pos60", "xs_vol_ratio", "xs_vol_z",
               "xs_board_bias20", "xs_board_pos60"]
MKT_FEATURES = ["mkt_up_ratio", "mkt_med_pct", "mkt_vol20"]
D_FEATURES = XS_FEATURES + MKT_FEATURES
ARMS = ("c", "d1", "d2")
ARM_DOC = {"c": "标签=原始 r_H，特征=个股自身 13 列 + 板块（批 C 原样）",
           "d1": "标签=r_H − 同日同 H 中位数（剥 β），特征同批 C",
           "d2": "标签同 d1 + 9 列横截面/市场状态（主判）"}


def features_for(arm: str) -> list[str]:
    """一个臂用哪些列：D 的两臂只是在批 C 之上加横截面列，个股自身列一列都不动。"""
    base = NUMERIC_FEATURES + (D_FEATURES if arm == "d2" else [])
    return base + ["board"]


# 缺这些就不是一个可比的持仓状态：入场成本、均线位置、波动、量能
REQUIRED = ["pnl_pp", "bias20", "pos60", "vol_ratio", "atr_n"]

EMBARGO_DAYS = 25                # 与可信度模型同一条：训练行的前向窗口不许伸进验证段
MIN_DATA_IN_LEAF = 20_000        # 同一 episode 的 10 行高度相关，叶子必须粗，否则拟合的是同一段行情
ERAS = 3
EPOCH = pd.Timestamp("1970-01-01")   # 「日」在所有时间口径里都是这一个整数：Unix 纪元日序号

logger = logging.getLogger("action_model")


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fh = logging.FileHandler(LOG_DIR / "action_model.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)


def cumulative_trials(path: Path = TRIAL_LEDGER) -> int:
    """从试次台账取累计试次：优先用最后一行的 `trials_total`，没有就按 combos 累加。

    及格线必须与逐笔扫描共用一条累计线——换个脚本就把试次清零，等于同一批行情能重新试一遍。
    """
    if not path.is_file():
        return 0
    total, last = 0, None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        total += int(rec.get("combos") or 0)
        if rec.get("trials_total") is not None:
            last = int(rec["trials_total"])
    return max(last or 0, total)


def headroom_gb(path: Path = Path("/proc/meminfo")) -> float:
    """可用内存（G）。读数口径与逐笔扫描共用 `profit_scan.mem_headroom`——两条链都在同一台
    机器上跑几个 G 的活儿，把页缓存算成可用与否不该有两种算法。"""
    return profit_scan.mem_headroom(path).get("avail_gb", float("nan"))


def _epoch_day(ts) -> int:
    """Timestamp → Unix 纪元日序号（向量化口径，全量 290 万行不能按行调 Python 函数）。"""
    return int((pd.Timestamp(ts) - EPOCH).days)


def _day_date(ordinal: int) -> str:
    return str((EPOCH + pd.Timedelta(days=int(ordinal))).date())


def _day_ordinal(ts: np.ndarray) -> np.ndarray:
    return ts.astype("datetime64[D]").astype(np.int32)


def matrix_from(cols: dict[str, np.ndarray], names: list[str]) -> np.ndarray:
    """按给定列名拼训练矩阵：三个臂共用同一份底层数组，只有列的集合不同。"""
    x = np.empty((len(next(iter(cols.values()))), len(names)), dtype=np.float32)
    for j, f in enumerate(names):
        x[:, j] = cols[f]
    return x


def join_cross_section(day: np.ndarray, code_id: np.ndarray, codes: dict[str, int],
                       want: list[str], xs_dir: Path) -> tuple[dict[str, np.ndarray], dict]:
    """按 (交易日, 票) 把批次 D 的横截面/市场状态列贴到决策行上。

    用排序 + `searchsorted` 而不是 pandas merge：290 万 × 366 万两张表按 (日期, 代码) 字符串合并
    会把内存推到 2G 级，而这台机器上还跑着 Mongo 与选股内核。
    **连不上的行留 NaN，不丢行**——丢行等于用「特征可用性」重新定义样本（README 批次 D 口径）。
    """
    out: dict[str, np.ndarray] = {}
    cov: dict[str, float] = {}
    n = day.size
    key_slot = 2_000_000                       # 代码序号远小于这个数，日期序号×它 + 代码序号 = 唯一键
    qkey = day.astype(np.int64) * key_slot + code_id.astype(np.int64)
    if any(w in XS_FEATURES for w in want):
        xs = pd.read_parquet(Path(xs_dir) / "cross_section.parquet",
                             columns=["date", "code", *XS_FEATURES])
        xid = xs["code"].map(codes).to_numpy(dtype=np.float64)   # 样本表里没有的票 → NaN，下面剔掉
        keep = ~np.isnan(xid)
        xday = _day_ordinal(xs["date"].to_numpy()[keep])
        xkey = xday.astype(np.int64) * key_slot + xid[keep].astype(np.int64)
        order = np.argsort(xkey, kind="stable")
        sk = xkey[order]
        pos = np.minimum(np.searchsorted(sk, qkey), len(sk) - 1)
        hit = sk[pos] == qkey
        for col in XS_FEATURES:
            if col not in want:
                continue
            vals = xs[col].to_numpy(dtype=np.float32)[keep][order]
            arr = np.full(n, np.nan, dtype=np.float32)
            arr[hit] = vals[pos[hit]]
            out[col] = arr
        del xs, xkey, order, sk, pos
    if any(w in MKT_FEATURES for w in want):
        mk = pd.read_parquet(Path(xs_dir) / "market_state.parquet",
                             columns=["date", *MKT_FEATURES])
        mday = _day_ordinal(mk["date"].to_numpy())
        mkey = mday.astype(np.int64)          # 市场列每天一个值，直接按日序号查
        pos = np.minimum(np.searchsorted(mkey, day.astype(np.int64)), len(mkey) - 1)
        hit = mkey[pos] == day.astype(np.int64)
        for col in MKT_FEATURES:
            if col not in want:
                continue
            vals = mk[col].to_numpy(dtype=np.float32)
            arr = np.full(n, np.nan, dtype=np.float32)
            arr[hit] = vals[pos[hit]]
            out[col] = arr
    for col, arr in out.items():
        cov[col] = round(float(np.isfinite(arr).mean()) * 100, 2)
    return out, cov


def excess_labels(day: np.ndarray, r_by_h: dict[int, np.ndarray]) -> tuple[dict[int, np.ndarray], dict]:
    """训练目标换成「比当天全部持仓好多少」：`y = r_H − 同日同 H 全部决策行 r_H 的中位数`。

    中位数是**当天在场的所有持仓**（含自己，日内数千行、影响可忽略）——它就是批 C 里那条
    `day_benchmark` 的同日基准，只是这次在标签层面剥掉，而不是判完再减。目的不是新增信息，
    是不让树把容量花在大盘涨跌上（β 学不准也不需要学）。判定照旧用原始 `r_H`，尺度不变。
    """
    out, info = {}, {}
    ds = pd.Series(day)
    for h, r in r_by_h.items():
        s = pd.Series(r)
        med = s.groupby(ds).transform("median").to_numpy(dtype=float)
        ok = np.isfinite(r) & np.isfinite(med)
        out[h] = np.where(ok, r - med, np.nan)
        info[str(h)] = {"bench_mean_pp": round(float(np.nanmean(med[ok])) * 100, 3),
                        "bench_sd_pp": round(float(np.nanstd(med[ok])) * 100, 3),
                        "usable_rows": int(ok.sum())}
    return out, info


def load(path: Path, horizons: list[int], max_rows: int = 0,
         extra: list[str] | None = None, xs_dir: Path | None = None) -> dict:
    """流式读样本表 → 训练数组。列一律 float32，`board` 在读的时候就翻成固定类别下标。

    留下的行：必需特征齐全，且至少一个 H 的标签可算。`ep_key = 代码序号 × 1e6 + ep_idx`
    是全局仓位键，评测按它做「同一仓位只算第一次触发」。`extra` 非空时顺带把批次 D 的
    横截面/市场状态列贴上来（一次读表跑三个臂，免得把 290 万行读三遍）。
    """
    extra = list(extra or [])
    if extra and xs_dir is None:
        raise SystemExit("批次 D 的列要先跑 scripts/build_cross_section.py 生成")
    pf = pq.ParquetFile(path)
    # hold_days / pnl_atr 也在 NUMERIC_FEATURES 里，去重后只取一次：列名重复会让 read_row_group 直接报错
    cols = list(dict.fromkeys(
        ["date", "code", "ep_idx", "buyable", *NUMERIC_FEATURES,
         *[f"r{h}" for h in horizons]]))
    frames: list[pd.DataFrame] = []
    codes: dict[str, int] = {}
    boards: dict[str, float] = {}
    got, n_groups, t0 = 0, pf.metadata.num_row_groups, time.time()
    for i in range(n_groups):
        c = pf.read_row_group(i, columns=cols).to_pandas()
        for code in c["code"].unique():
            if code not in codes:
                codes[code] = len(codes)
                idx = bucket.board_index(bucket.classify_board(code))
                boards[code] = float(idx if idx is not None else -1)
        keep = np.isfinite(c[REQUIRED].to_numpy(dtype=np.float32)).all(axis=1)
        keep &= np.isfinite(c["pnl_atr"].to_numpy(dtype=np.float32))
        keep &= np.isfinite(np.column_stack(
            [c[f"r{h}"].to_numpy(dtype=np.float32) for h in horizons])).any(axis=1)
        if not keep.any():
            continue
        g = pd.DataFrame({
            "date": c["date"].to_numpy()[keep],
            "code_id": c["code"].map(codes).to_numpy(dtype=np.int32)[keep],
            "ep_idx": c["ep_idx"].to_numpy(dtype=np.int32)[keep],
            "buyable": c["buyable"].to_numpy()[keep],
            "board": c["code"].map(boards).to_numpy()[keep],
        })
        for f in NUMERIC_FEATURES:
            g[f] = c[f].to_numpy(dtype=np.float32)[keep]
        for h in horizons:
            g[f"r{h}"] = c[f"r{h}"].to_numpy(dtype=np.float32)[keep]
        frames.append(g)
        got += len(g)
        if (i + 1) % 400 == 0 or i + 1 == n_groups:
            logger.info("读样本 %d/%d 行组，累计 %s 行 (%.0fs)",
                        i + 1, n_groups, f"{got:,}", time.time() - t0)
        if max_rows and got >= max_rows:
            break
    if not frames:
        raise SystemExit("没有可用样本行")
    df = pd.concat(frames, ignore_index=True)
    n = len(df)
    day = _day_ordinal(df["date"].to_numpy())
    arr = {f: df[f].to_numpy(dtype=np.float32) for f in NUMERIC_FEATURES}
    arr["board"] = df["board"].to_numpy(dtype=np.float32)
    coverage: dict[str, float] = {}
    if extra:
        arr_join, coverage = join_cross_section(day, df["code_id"].to_numpy(dtype=np.int32),
                                                codes, extra, xs_dir)
        arr.update(arr_join)
        logger.info("横截面列贴完：%s", ", ".join(f"{k}={v}%非空" for k, v in coverage.items()))
    out = {
        "cols": arr,
        "day": day,
        "ep_key": (df["code_id"].to_numpy(dtype=np.int64) * 1_000_000
                   + df["ep_idx"].to_numpy(dtype=np.int64)),
        "buyable": df["buyable"].to_numpy(dtype=np.int8),
        "n": n, "codes": len(codes),
        "span": (_day_date(int(day.min())), _day_date(int(day.max()))),
        "r": {h: df[f"r{h}"].to_numpy(dtype=float) for h in horizons},
        "xs_coverage": coverage,
    }
    logger.info("可用样本 %s 行 / %d 只 / %s→%s", f"{n:,}", out["codes"], *out["span"])
    for h in horizons:
        v = out["r"][h]
        ok = np.isfinite(v)
        logger.info("  标签 r%-3d 可算 %s 行：均值 %+0.3fpp、标准差 %.2fpp、为正占 %.1f%%",
                    h, f"{ok.sum():,}", v[ok].mean() * 100, v[ok].std() * 100,
                    float((v[ok] > 0).mean()) * 100)
    return out


def lgb_params(args) -> dict:
    return {"objective": "regression", "metric": ["rmse"], "learning_rate": args.lr,
            "num_leaves": args.num_leaves, "min_data_in_leaf": MIN_DATA_IN_LEAF,
            "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 5,
            "lambda_l2": 1.0, "num_threads": args.threads, "seed": args.seed, "verbosity": -1}


def train_horizon(x, y, day, keep_lab, args, h: int, arm: str = "c",
                  ref: np.ndarray | None = None):
    """一个臂 × 一个 H：净化式前推（embargo 25 自然日）出样本外预测。

    `ref` 是未剥标签的原始 `r_H`：预注册的止损线看的是「预测与真实 r 的相关」，不是与训练目标的
    相关——D 两臂的目标已经被减掉当期中位数，只报目标相关会把 β 剥掉带来的虚高算成信号。
    """
    import lightgbm as lgb

    idx = np.flatnonzero(keep_lab)
    oof = np.full(len(y), np.nan)
    folds, best_iters = [], []
    start, max_day = args.valid_start_ordinal, int(day.max())
    while start <= max_day:
        end = start + args.fold_days
        val = idx[(day[idx] >= start) & (day[idx] < end)]
        tr = idx[day[idx] < start - EMBARGO_DAYS]
        if len(val) < args.min_val or len(tr) < args.min_train:
            start = end
            continue
        dtr = lgb.Dataset(x[tr], label=y[tr], free_raw_data=False)
        dva = lgb.Dataset(x[val], label=y[val], reference=dtr, free_raw_data=False)
        booster = lgb.train(lgb_params(args), dtr, num_boost_round=args.iterations,
                            valid_sets=[dva],
                            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)])
        best = booster.best_iteration or args.iterations
        p = booster.predict(x[val], num_iteration=best)
        oof[val] = p
        best_iters.append(int(best))
        t = y[val]
        fold = {"window": f"{_day_date(start)}~{_day_date(end - 1)}",
                "n": int(len(val)), "train_n": int(len(tr)), "best_iter": int(best),
                "rmse": round(float(np.sqrt(np.mean((p - t) ** 2))), 6),
                "corr": round(float(np.corrcoef(p, t)[0, 1]), 4),
                "hit_sign": round(float(np.mean(np.sign(p) == np.sign(t))), 4)}
        if ref is not None:
            fold["corr_r"] = round(float(np.corrcoef(p, ref[val])[0, 1]), 4)
        folds.append(fold)
        logger.info("%s H=%d 折 %s 验证 %s 行  RMSE=%.3fpp 与目标相关=%.4f%s 符号命中=%.1f%% iter=%d",
                    arm, h, fold["window"], f"{fold['n']:,}", fold["rmse"] * 100, fold["corr"],
                    f" 与真实r相关={fold['corr_r']:.4f}" if ref is not None else "",
                    fold["hit_sign"] * 100, best)
        del booster, dtr, dva
        start = end
    if not folds:
        raise SystemExit(f"{arm} H={h} 验证折为空：--valid-start/--fold-days/--min-val 与数据量不匹配")
    have = np.flatnonzero(~np.isnan(oof))
    corr_r = (float(np.corrcoef(oof[have], ref[have])[0, 1]) if ref is not None
              else float(np.corrcoef(oof[have], y[have])[0, 1]))
    logger.info("%s H=%d 样本外合计 %s 行：RMSE=%.3fpp、与目标相关=%.4f、与真实 r 相关=%.4f、符号命中=%.1f%%",
                arm, h, f"{len(have):,}",
                float(np.sqrt(np.mean((oof[have] - y[have]) ** 2))) * 100,
                float(np.corrcoef(oof[have], y[have])[0, 1]), corr_r,
                float(np.mean(np.sign(oof[have]) == np.sign(y[have]))) * 100)
    return oof, folds, best_iters, corr_r


def day_stat(adv: np.ndarray, d_idx: np.ndarray, n_days: int, era_cuts) -> dict:
    """逐笔优势 → 读数（笔数/均值/胜率/盈亏比/按日聚类 t/三段），与 `profit_scan.Accum.stat` 同形。"""
    nn = float(adv.size)
    if nn == 0:
        return {"n": 0, "days": 0}
    cnt = np.bincount(d_idx, minlength=n_days).astype(float)
    dsum = np.bincount(d_idx, weights=adv, minlength=n_days).astype(float)
    live = cnt > 0
    dn = int(live.sum())
    mean = float(adv.sum() / nn)
    sd = float(np.std(adv, ddof=1)) if nn > 1 else 0.0
    day_mean = t_day = None
    if dn >= 2:
        dm = dsum[live] / cnt[live]
        day_mean = float(dm.mean())
        var = float(np.var(dm, ddof=1))
        if var > 0:
            t_day = day_mean / (var / dn) ** 0.5
    gain = float(np.maximum(adv, 0).sum())
    loss = float(np.minimum(adv, 0).sum())
    rng = np.arange(n_days)
    era = []
    for lo, hi in ((0, era_cuts[0]), (era_cuts[0], era_cuts[1]), (era_cuts[1], n_days)):
        m = live & (rng >= lo) & (rng < hi)
        cc = float(cnt[m].sum())
        era.append(round(float(dsum[m].sum() / cc), 3) if cc >= 30 else None)
    return {"n": int(nn), "mean": mean, "sd": sd, "days": dn, "day_mean": day_mean,
            "t_day": t_day, "t_trade": (mean / (sd / nn ** 0.5)) if sd > 0 else None,
            "win": float((adv > 0).mean()),
            "payoff": float("inf") if loss == 0 else gain / -loss, "era": era}


def day_benchmark(adv: np.ndarray, d_idx: np.ndarray, elig: np.ndarray,
                  n_days: int) -> np.ndarray:
    """每个交易日「所有可判持仓（不挑）」的平均优势；那天没有行就留 0（不会被取到）。"""
    cnt = np.bincount(d_idx[elig], minlength=n_days).astype(float)
    ssum = np.bincount(d_idx[elig], weights=adv[elig], minlength=n_days).astype(float)
    out = np.zeros(n_days)
    live = cnt > 0
    out[live] = ssum[live] / cnt[live]
    return out


def first_act_per_episode(acted: np.ndarray, ep_key: np.ndarray) -> np.ndarray:
    """同一仓位只算第一次触发：每个 episode 保留最早的那一行（样本已按 (代码, episode, h) 有序，
    `np.unique` 的首次出现自然是最小 h）。"""
    idx = np.flatnonzero(acted)
    if idx.size == 0:
        return idx
    _, pos = np.unique(ep_key[idx], return_index=True)
    return idx[pos]


def rank_mask(p: np.ndarray, elig: np.ndarray, frac: float, lowest: bool) -> tuple[np.ndarray, float]:
    """在可动的行里按预测名次取最极端的那 `frac`（卖侧取最低、加侧取最高）。

    用名次而不是 `p <= np.quantile(p, 0.3)`：模型退化成常数预测时（没有信号就会被早停压成一条
    水平线），分位数值和预测值处处相等，`<=` 会选中 **全部** 行——那一格读的就不是「最挑的
    30%」，而是「见了就动」，跟它旁边的 20%/10% 两格还会变成同一个数。名次法永远恰好取 k 行。

    返回 (掩码, 边界预测值)：边界值写进报告，读数时才看得出三格是不是其实没分开。
    """
    idx = np.flatnonzero(elig)
    out = np.zeros(p.size, dtype=bool)
    if idx.size == 0:
        return out, float("nan")
    k = min(idx.size, max(1, int(round(idx.size * frac))))
    order = idx[np.argsort(p[idx], kind="stable")]
    sel = order[:k] if lowest else order[-k:]
    out[sel] = True
    return out, float(p[sel].max() if lowest else p[sel].min())


def evaluate_cells(data: dict, pred_by_h: dict, n_trials: int, arm: str = "c") -> tuple[list[dict], float]:
    """2 侧 × 每个 H × 3 个分位：逐格算读数并过 `rule_gates.evaluate_trade`。

    `n_trials` 是**判这一批格时用**的累计试次（含本批），由调用方决定：同一批预注册格重跑一次
    （改了展示口径、没加新格）不该再抬一次及格线，否则试次会被自己的复跑灌水。
    判定一律用未截断的真实 `r_H`——训练标签怎么换都不改这把尺子（README 批次 D）。
    `arm` 只盖在读数行上做归因，不参与任何计算。
    """
    day, ep_key, buyable = data["day"], data["ep_key"], data["buyable"]
    days_uniq, d_all = np.unique(day, return_inverse=True)
    n_days = len(days_uniq)
    rows = []
    for h, pred in pred_by_h.items():
        r = data["r"][h]
        have = np.flatnonzero(~np.isnan(pred) & np.isfinite(r))
        if have.size == 0:
            continue
        p, dl, elp, bl = pred[have], d_all[have], ep_key[have], buyable[have].astype(bool)
        # 三段按「这一格真有读数的日子」切，不按样本表的全日期范围切（与 profit_scan 同一口径）：
        # 验证折从 2018 年才开始（2017 折的训练行不够），按全范围切的话第一段永远是空的，
        # 报告上就只剩两段，看不出「这个动作只在前一段赚」。
        judged = np.unique(dl)
        era_cuts = (int(judged[0] + (judged[-1] - judged[0]) // ERAS),
                    int(judged[0] + 2 * (judged[-1] - judged[0]) // ERAS))
        adv_all = {"sell": -r[have] * (1.0 - SELL_FEE_RATE) * 100.0,
                   "add": r[have] * 100.0 - ROUND_TRIP_PP}
        for side in ("sell", "add"):
            adv = adv_all[side]
            # 卖侧不分可买（持仓已经在手上）；加侧要 T+1 之外还有钱买得进，用样本里的 buyable
            elig = np.ones(p.size, dtype=bool) if side == "sell" else bl
            bench = day_benchmark(adv, dl, elig, n_days)
            for pct in CUTOFFS:
                mask, cut = rank_mask(p, elig, pct / 100.0, lowest=(side == "sell"))
                if not mask.any():
                    continue
                keep = first_act_per_episode(mask, elp)
                st = day_stat(adv[keep], dl[keep], n_days, era_cuts)
                if st["n"] == 0:
                    continue
                bench_pp = float(bench[dl[keep]].mean())
                row = {"arm": arm, "horizon": h, "side": side, "cutoff_pct": pct,
                       "threshold": round(float(cut), 6),
                       "n_trades": st["n"], "days": st["days"],
                       "act_share_pct": round(100.0 * int(mask.sum()) / int(elig.sum()), 2),
                       "win_rate": round(st["win"], 4),
                       "payoff": None if st["payoff"] == float("inf") else round(st["payoff"], 3),
                       "net_per_trade_pp": round(st["mean"], 3),
                       "day_mean_pp": None if st["day_mean"] is None else round(st["day_mean"], 3),
                       "t_day": None if st["t_day"] is None else round(st["t_day"], 2),
                       "t_trade": None if st["t_trade"] is None else round(st["t_trade"], 2),
                       "bench_day_pp": round(bench_pp, 3),
                       "excess_pp": round(st["mean"] - bench_pp, 3),
                       "era_net_pp": ",".join("nan" if e is None else f"{e:+.2f}"
                                              for e in st["era"])}
                g = rule_gates.evaluate_trade(
                    {"n_trades": row["n_trades"], "win_rate": row["win_rate"],
                     "payoff": row["payoff"], "net_per_trade_pp": row["net_per_trade_pp"],
                     "t_stat": row["t_day"]}, n_trials)
                row.update({"verdict": g["verdict"], "failed": g["failed"], "checks": g["checks"]})
                rows.append(row)
    need_t = rule_gates.t_required(n_trials)
    return rows, need_t


def log_cells(rows: list[dict], n_trials: int, need_t: float) -> None:
    """一批预注册格子的读数全部打屏：按臂分组、按 t_day 降序，过几格说几格，不做「只看最好那格」。"""
    logger.info("本批 %d 格验收（及格线 t≥%.2f，按累计试次 %s 抬）：过 %d 格",
                len(rows), need_t, f"{n_trials:,}",
                sum(1 for x in rows if x["verdict"] == "过"))
    for arm in sorted({x["arm"] for x in rows}):
        sub = [x for x in rows if x["arm"] == arm]
        logger.info("  [%s] %s ⇒ 过 %d/%d 格", arm, ARM_DOC[arm],
                    sum(1 for x in sub if x["verdict"] == "过"), len(sub))
    for x in sorted(rows, key=lambda z: (z["arm"], -(z["t_day"] if z["t_day"] is not None else -99))):
        logger.info("  %s H=%-3d %-4s 阈值 %s%%  笔数 %9s 胜率 %5.1f%% 盈亏比 %5s 每笔 %+6.2fpp "
                    "日均 %+6.2fpp 同日基准 %+6.2fpp 超额 %+5.2fpp t_day %6s → %s%s",
                    x["arm"], x["horizon"], x["side"], x["cutoff_pct"], f"{x['n_trades']:,}",
                    (x["win_rate"] or 0) * 100,
                    "∞" if x["payoff"] is None else f"{x['payoff']:.2f}",
                    x["net_per_trade_pp"], x["day_mean_pp"] or 0.0, x["bench_day_pp"],
                    x["excess_pp"], x["t_day"], x["verdict"],
                    "" if x["verdict"] == "过" else f" ←{','.join(x['failed'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description="持仓动作模型（回归再拿 H 天的前向收益）")
    ap.add_argument("--samples", default=str(SAMPLES_FILE))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--xs-dir", default=str(OUT_DIR),
                    help="批次 D 的 cross_section/market_state 表所在目录")
    ap.add_argument("--arms", default="c",
                    help="逗号分隔的臂（c/d1/d2）；批 D 用 d1,d2，一批 24 格跑完才算预注册")
    ap.add_argument("--horizons", default="5,10")
    ap.add_argument("--valid-start", default="2017-01-01")
    ap.add_argument("--fold-days", type=int, default=365)
    ap.add_argument("--iterations", type=int, default=400)
    ap.add_argument("--min-val", type=int, default=20_000, help="一个验证折至少多少行")
    ap.add_argument("--min-train", type=int, default=200_000, help="一个验证折至少多少训练行")
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--num-leaves", type=int, default=63)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--smoke", action="store_true", help="只读 20 万行 + 放宽折下限，管道自检")
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--trial-budget", type=int, default=TRIAL_BUDGET)
    ap.add_argument("--no-book-trials", dest="book_trials", action="store_false",
                    help="不烧试次（只在调试时用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]
    arms = [a.strip() for a in str(args.arms).split(",") if a.strip()]
    if not arms or any(a not in ARMS for a in arms):
        logger.error("--arms 只能是 %s 的组合，收到 %r", "/".join(ARMS), args.arms)
        raise SystemExit(2)
    if len(set(arms)) != len(arms):
        logger.error("--arms 有重复（%r）：同一臂跑两遍会把同一批格子重复记账", args.arms)
        raise SystemExit(2)
    path = Path(args.samples)
    if not path.is_file():
        logger.error("样本表不存在：%s（先跑 build_action_samples.py）", path)
        raise SystemExit(2)
    n_cells = len(arms) * 2 * len(horizons) * len(CUTOFFS)
    n_before = cumulative_trials()
    if n_before + n_cells > args.trial_budget:
        logger.error("累计试次 %s 加这一批 %d 格会超出预算 %s：拒绝再试",
                     f"{n_before:,}", n_cells, f"{args.trial_budget:,}")
        raise SystemExit(3)
    avail = headroom_gb()
    logger.info("起跑前内存可用 %.1fG；臂 %s；累计试次 %s（这批 %d 格）",
                avail, "+".join(arms), f"{n_before:,}", n_cells)
    if not avail >= MIN_AVAIL_GB:      # 读不到也当不够：nan 走这里，别在未校验的情况下硬跑
        logger.error("可用内存不足 %.1fG，先退出（别把这台机器压穿）", MIN_AVAIL_GB)
        raise SystemExit(4)

    if args.smoke:
        args.max_rows = args.max_rows or 200_000
        args.min_val = min(args.min_val, 3_000)
        args.min_train = min(args.min_train, 20_000)
        if args.book_trials:
            # 自检跑在截断的样本上，这一批格子的读数不作数；烧进台账会把累计试次永久抬高
            args.book_trials = False
            logger.info("--smoke：不记试次（管道自检用的截断样本不算一次尝试）")
    args.valid_start_ordinal = _epoch_day(args.valid_start)
    data = load(path, horizons, args.max_rows,
                extra=D_FEATURES if "d2" in arms else None, xs_dir=Path(args.xs_dir))

    # 超额标签只算一次，d1/d2 共用：两臂的差别必须在特征上，不能藏在标签算法的细微不同里
    want_excess = any(a != "c" for a in arms)
    y_excess, excess_info = excess_labels(data["day"], data["r"]) if want_excess else ({}, {})
    for h, info in excess_info.items():
        logger.info("  超额标签 H=%s：%s 行可算，被减掉的当期中位数日均 %+0.3fpp（跨日标准差 %.2fpp）",
                    h, f"{info['usable_rows']:,}", info["bench_mean_pp"], info["bench_sd_pp"])

    run_id = args.run_id or (time.strftime("%Y%m%d-%H%M%S") + "-action")
    import lightgbm as lgb

    # 只有真要把这批格烧进台账时才抬及格线；调试/复跑用台账里已有的累计数
    n_trials = n_before + (n_cells if args.book_trials else 0)
    rows: list[dict] = []
    per_arm: dict[str, dict] = {}
    need_t = float("nan")
    for arm in arms:
        names = features_for(arm)
        x = matrix_from(data["cols"], names)
        pred_by_h, folds_by_h, iters_by_h, imp_by_h, corr_by_h = {}, {}, {}, {}, {}
        model_files = {}
        for h in horizons:
            raw = data["r"][h]
            y = raw if arm == "c" else y_excess[h]
            keep_lab = np.isfinite(y)
            yv = np.where(keep_lab, y, 0.0)
            oof, folds, best_iters, corr_r = train_horizon(
                x, yv, data["day"], keep_lab, args, h, arm=arm, ref=raw)
            pred_by_h[h], folds_by_h[h] = oof, folds
            corr_by_h[h] = round(float(corr_r), 4)
            iters_by_h[h] = max(1, int(np.median(best_iters)))
            booster = lgb.train(lgb_params(args), lgb.Dataset(x[keep_lab], label=yv[keep_lab]),
                                num_boost_round=iters_by_h[h])
            stem = f"action_model_h{h}" if arm == "c" else f"action_model_{arm}_h{h}"
            out_model = Path(args.out_dir) / f"{stem}.lgb.txt"
            out_model.parent.mkdir(parents=True, exist_ok=True)
            booster.save_model(str(out_model), num_iteration=iters_by_h[h])
            model_files[str(h)] = out_model.name
            gain = booster.feature_importance(importance_type="gain")
            order = np.argsort(gain)[::-1]
            imp_by_h[h] = {names[i]: round(float(gain[i]), 1) for i in order}
            logger.info("%s H=%d 终版 %d 轮已写 %s；特征重要度前 5：%s",
                        arm, h, iters_by_h[h], out_model.name,
                        ", ".join(f"{names[i]}={gain[i]:.0f}" for i in order[:5]))
            del booster
        per_arm[arm] = {"doc": ARM_DOC[arm], "label": "r_H" if arm == "c" else "r_H - med_H(day)",
                        "features": names, "model_files": model_files,
                        "num_iterations": iters_by_h, "oof_corr_with_real_r": corr_by_h,
                        "validation": {str(h): folds_by_h[h] for h in horizons},
                        "importance_gain": {str(h): imp_by_h[h] for h in horizons}}
        del x
        arm_rows, need_t = evaluate_cells(data, pred_by_h, n_trials, arm=arm)
        rows.extend(arm_rows)
        del pred_by_h, arm_rows

    log_cells(rows, n_trials, need_t)
    meta = {"run_id": run_id, "trained_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "samples": str(path), "n_rows": data["n"], "codes": data["codes"],
            "range": list(data["span"]), "arms": arms,
            "numeric_features": NUMERIC_FEATURES,
            "board_levels": list(bucket.BOARD_CATEGORIES),
            "horizons": horizons, "cutoffs_pct": list(CUTOFFS),
            "params": lgb_params(args),
            "xs_columns": D_FEATURES if "d2" in arms else [],
            "xs_coverage": data["xs_coverage"], "excess_label": excess_info,
            "cost": {"sell_fee_rate": SELL_FEE_RATE, "round_trip_pp": ROUND_TRIP_PP},
            "label_contract": {
                "meaning": (f"r_H = 从决策日收盘起再拿 H 个交易日的卖价收益（到期卖不掉最多顺移 "
                            f"{REPAIR_MAX_DAYS} 行）；卖出提前见 = -r_H*(1-卖出费率"
                            f"{SELL_FEE_RATE:g})，加仓一份 = r_H - {ROUND_TRIP_PP}pp。"
                            f"沉没成本只在特征里，不在标签里。判定端三个臂都用未截断的真实 r_H。"),
                "first_act_per_episode": True},
            "per_arm": per_arm, "cells": rows,
            "gate_t_required": round(float(need_t), 3),
            "trials": {"ledger_before": n_before, "judged_with": n_trials,
                       "booked": n_cells if args.book_trials else 0}}
    # 批 C 的 `action_model_meta.json` 是那次 0/12 的读数存档，别的臂不许覆盖它
    meta_name = "action_model_meta.json" if arms == ["c"] else f"action_model_meta_{run_id}.json"
    out_meta = Path(args.out_dir) / meta_name
    out_meta.parent.mkdir(parents=True, exist_ok=True)
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = LOG_DIR / f"cells_{run_id}.csv"
    if rows:
        pd.DataFrame(rows).drop(columns=["checks"]).to_csv(csv_path, index=False)
    logger.info("已写 %s 与 %s", out_meta, csv_path)

    if args.book_trials:
        rec = {"ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "run_id": run_id,
               "combos": n_cells, "trials_total": n_before + n_cells, "dry_run": False,
               "n_pass": sum(1 for x in rows if x["verdict"] == "过"),
               "window": list(data["span"]), "chain": "action_model", "arms": arms,
               "samples": str(path), "n_rows": data["n"]}
        with TRIAL_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("已记试次：%d 格 → 累计 %s", n_cells, f"{n_before + n_cells:,}")


if __name__ == "__main__":
    main()
