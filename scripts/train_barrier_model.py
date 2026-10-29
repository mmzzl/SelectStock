"""训练**成本锚**下跌概率臂（§50）：barrier（还要跌多少才碰到那条线）当输入特征。

用户点名的口径（原话）：「10 元成本买入，我要的是这个下跌 10%，不是当前价格再下跌 10% 的概率」。
所以线是一个**钉死的绝对价** = 成本×0.9，服务时把「离那条线还差多少」当成特征喂进来：

    x = 1 − 成本×0.9 / 现价            # 相对现价的剩余跌幅
    P = model(特征(现价), x)           # 未来 horizon 个交易日内最低价 ≤ 成本×0.9 的概率

为什么这样成立（不需要新采集任何数据）：历史样本里没有「他的成本」这一维，也不需要——
每行样本 (code, date) 复制 K 份、每份配一个网格上的 barrier，标签 `y = 1(fwd_low ≤ close*(1−x))`。
训练与服务两侧的锚都是「当日收盘」，(特征, x) 因此同构；成本只通过 x 的换算进来。

三条不可省的量（都在 tests/test_barrier_model.py 里有测试）：
1. **单调约束**只加在 barrier 列（−1）：现价越靠近线，概率只能升。这是他要的那件事本身。
2. **整组同侧**：同一 (code, date) 的 K 份复制共用同一根 fwd_low，拆到训练/验证两侧就是
   自己考自己，AUC 虚高。折划分走 `fold_indices`，按组踢。
3. **校准按 barrier 分层**各出一条 isotonic 曲线（连续内插），因为 x=2% 层实测 74.5%、
   x=15% 层实测 7.7% 差着量级；混一条曲线会被 barrier 密度主导，而「快碰线」那一端（x 小、
   P 高）正是他要看的读数。现臂那 10 档阶梯就是 600272 连续 26 天印同一个数的成因。

样本规模：全量 10.33M 行 × K 份放不进这台 15GB 机器 ⇒ 先按 (code,date) 内容哈希取 `--frac`
（默认一半，可复现、与行序/时间/板块无关），再复制 barrier。子样比例只影响噪声，不影响口径。

对照/防作弊：
- `x=10%` 那一层与现 −10% 臂是**同一事件**，AUC 必须同尺（R3），否则说明复制行带来泄漏；
- `--fix-barrier 0.10` 把 barrier 列强制成常量重训（只 1/7 数据，很快）：AUC 应落回现臂量级，
  明显更高就是「查表机」，整批读数作废（R5）。

用法：
    python scripts/train_barrier_model.py --smoke              # 前 300 行组，几分钟，先跑这个
    python scripts/train_barrier_model.py                      # 全量（几十分钟）
    python scripts/train_barrier_model.py --fix-barrier 0.10   # R5 反作弊对照（不写 meta）

回滚：`mv data/conviction/barrier_model.lgb.txt{,.off}` —— 插件读不到该文件就自动回退
「当日收盘 −10%」档，不删文件、不改 conf、不重启服务。
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# loader / 折 / 度量 / ST 名单一律复用上涨-下行那套：抄第二份就等着它和上游漂移。
import train_conviction_model as base                                  # noqa: E402
from stock_selector.conviction import bucket, features as fx           # noqa: E402

BARRIER_MODEL = ROOT / "data" / "conviction" / "barrier_model.lgb.txt"
META_FILE = base.META_FILE
LOG_DIR = base.LOG_DIR

# barrier 网格（相对现价的跌幅）：覆盖「明天就碰线」到「还差得远」，含 10% 那条与现臂同事件。
BARRIER_GRID = [0.02, 0.03, 0.05, 0.075, 0.10, 0.125, 0.15]
BARRIER_COL = len(fx.FEATURES)          # X 里 barrier 列的下标（board 之后、排最后）

logger = logging.getLogger("barrier_model")


def barrier_labels(close, fwd_low, grid=BARRIER_GRID) -> np.ndarray:
    """(n, k) int8：`y[i, j] = 1(fwd_low[i] <= close[i]*(1-grid[j]))`，即「碰到那条线」。"""
    c = np.asarray(close, dtype=np.float64)[:, None]
    low = np.asarray(fwd_low, dtype=np.float64)[:, None]
    g = np.asarray(grid, dtype=np.float64)[None, :]
    return (low <= c * (1.0 - g)).astype(np.int8)


def expand_barriers(x_base: np.ndarray, close, fwd_low, grid=BARRIER_GRID):
    """每行配 K 个 barrier：X=(n·k, d+1)（barrier 排在最后一列），组号 = 原始行号重复 K 次。

    顺序是「同一行的 K 份连在一起」，所以 `group` 天然连续、`X[:, -1]` 是 tile(grid)。
    服务侧装配走 `features.row_to_vector_with_barrier`，列序与此一致（FEATURES + barrier）。
    """
    k = len(grid)
    lab = barrier_labels(close, fwd_low, grid)
    X = np.empty((x_base.shape[0] * k, x_base.shape[1] + 1), dtype=np.float32)
    X[:, :-1] = np.repeat(x_base, k, axis=0)
    X[:, -1] = np.tile(np.asarray(grid, dtype=np.float32), x_base.shape[0])
    return X, lab.reshape(-1), np.repeat(np.arange(x_base.shape[0], dtype=np.int64), k)


def fold_indices(day, group, seq, start, end, horizon, embargo):
    """净化前推的一折：验证 = 窗口内且 `seq % horizon == 0`（去重叠），训练 = embargo 之前。

    验证窗口里出现过的组，整组从训练侧踢掉——按现口径（同 day 同组）本来不会跨侧，
    但这条不许省：哪天有人把划分改成按行号随机，同一根 fwd_low 就会同时出现在两侧。
    """
    valid_idx = np.flatnonzero((day >= start) & (day < end) & (seq % horizon == 0))
    bad = np.zeros(int(group.max()) + 1, dtype=bool)
    bad[np.unique(group[valid_idx])] = True
    train_idx = np.flatnonzero((day < start - embargo) & ~bad[group])
    return train_idx, valid_idx


def lgb_params_with_barrier(args) -> dict:
    """现臂的参数 + **只给 barrier 列**的单调递减约束（其余列一律不约束方向）。"""
    params = dict(base.lgb_params(args))
    params["monotone_constraints"] = [0] * (BARRIER_COL) + [-1]
    return params


def monotonicity_violations(predict_fn, base_vectors, grid=BARRIER_GRID) -> int:
    """R1 的仪器：同一行特征、barrier 从小到大，分数只能不增。返回违例次数（跨相邻档）。"""
    viol = 0
    for v in base_vectors:
        scores = [float(predict_fn([*v, x])) for x in grid]
        viol += sum(1 for a, b in zip(scores, scores[1:]) if b > a + 1e-9)
    return viol


def fit_isotonic(score: np.ndarray, label: np.ndarray, bins: int = 16) -> dict:
    """样本外 (分数, 事件) → 单调**连续**校准曲线（节点之间由插件线性内插）。

    先分位分箱再做 PAVA（不是逐点 isotonic）：逐点拟合的两端会留下单点块，印出来就是
    0.0% 或 100% 这种历史上没发生过的数——和现臂 10 档阶梯「同值 26 天」是两个方向的同一个
    毛病（一个是量化过头，一个是量化不足）。箱子至少 50 行，端点值是箱平均。
    没区分度时曲线只剩一两个块 ⇒ 接近一条平线，那正是我们想要的诚实。
    """
    from sklearn.isotonic import IsotonicRegression

    score = np.asarray(score, dtype=np.float64)
    label = np.asarray(label, dtype=np.float64)
    edges = np.unique(np.quantile(score, np.linspace(0, 1, bins + 1)))
    xs, ys, ns = [], [], []
    if len(edges) >= 3:
        idx = np.clip(np.searchsorted(edges, score, side="right") - 1, 0, len(edges) - 2)
        for b in range(len(edges) - 1):
            m = idx == b
            if m.sum() < 50:
                continue
            xs.append(float(score[m].mean()))
            ys.append(float(label[m].mean()))
            ns.append(int(m.sum()))
    else:                                       # 分数全一样：没有区分度，一条常数线
        return {"xs": [float(score.mean())], "ys": [float(label.mean())], "n": int(len(label))}
    ir = IsotonicRegression(out_of_bounds="clip", increasing=True)
    ir.fit(np.asarray(xs), np.asarray(ys), sample_weight=np.asarray(ns, dtype=float))
    return {"xs": [float(v) for v in ir.X_thresholds_],
            "ys": [float(v) for v in ir.y_thresholds_],
            "n": int(len(label))}


def sample_groups(codes, dates, frac: float) -> np.ndarray:
    """按 (code, date) 内容哈希决定去留：可复现、与行序/时间/板块无关。"""
    c = pd.to_numeric(pd.Series(codes).astype(str), errors="coerce").fillna(0).to_numpy("uint64")
    d = pd.to_datetime(pd.Series(dates)).map(pd.Timestamp.toordinal).to_numpy("int64").astype("uint64")
    u = c * np.uint64(40503) + d                          # uint64 环绕乘法，不报溢出
    u ^= u >> np.uint64(33)                               # splitmix 混合：裸乘积在相邻 code/date 上会露出结构
    u *= np.uint64(0xFF51_AF7D_ED55_8CCD)
    u ^= u >> np.uint64(33)
    return (u % np.uint64(100_000)) < np.uint64(round(frac * 100_000))


def main() -> None:
    ap = argparse.ArgumentParser(description="成本锚下跌概率臂（barrier 当特征）")
    ap.add_argument("--samples", default=str(base.SAMPLES_FILE))
    ap.add_argument("--kline-dir", default=str(base.KLINE_DIR))
    ap.add_argument("--out-model", default=str(BARRIER_MODEL))
    ap.add_argument("--meta", default=str(META_FILE))
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--drop", type=float, default=0.10, help="线 = 成本×(1−drop)")
    ap.add_argument("--frac", type=float, default=0.5, help="(code,date) 子样比例")
    ap.add_argument("--valid-start", default="2019-01-01")
    ap.add_argument("--fold-days", type=int, default=365)
    ap.add_argument("--calib-from", default="2022-01-01", help="校准只用这段之后的样本外行")
    ap.add_argument("--eval-from", default="2024-07-01",
                    help="R4 留出段：曲线用 calib-from~eval-from 拟合，偏差用这之后的行评")
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--iterations", type=int, default=400)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--num-leaves", type=int, default=31)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--probes", type=int, default=200, help="R1 单调性扫描用的真实特征行数")
    ap.add_argument("--smoke", action="store_true", help="只读前 300 个行组（口径演练，不当读数）")
    ap.add_argument("--keep-st", action="store_true")
    ap.add_argument("--fix-barrier", type=float, default=None,
                    help="R5 反作弊：barrier 列强制成这个常量重训，只报读数不写 meta")
    args = ap.parse_args()

    base.setup_logging()
    # base 只给它自己的 logger 挂了输出；本文件的 logger 不挂就整段静默——
    # 几十分钟的跑看不到进度，等于跑完也不知道中间发生过什么。共用同一批 handler，不重复格式。
    logger.setLevel(base.logger.level)
    for _h in base.logger.handlers:
        if _h not in logger.handlers:
            logger.addHandler(_h)
    samples_path = Path(args.samples)
    if not samples_path.is_file():
        logger.error("样本文件缺失：%s", samples_path)
        raise SystemExit(2)
    exclude = set() if args.keep_st else base.st_codes(Path(args.kline_dir))
    grid = BARRIER_GRID
    t0 = time.time()
    df = base.load_samples(samples_path, exclude, args.horizon,
                           300 if args.smoke else 0, resolved_only=False, drop=args.drop)
    keep = sample_groups(df["code"].to_numpy(), df["date"].to_numpy(), args.frac)
    logger.info("子样：全量 %d 行 → 按 (code,date) 哈希留 %d 行（%.1f%%，--frac %s）",
                len(df), int(keep.sum()), keep.mean() * 100, args.frac)
    df = df.loc[keep]
    x, _, day, seq = base.to_arrays(df)
    close = df["close"].to_numpy(dtype=np.float32)
    fwd_low = df["fwd_low"].to_numpy(dtype=np.float32)
    del df

    cutoff0 = pd.Timestamp(args.valid_start).toordinal()
    calib_from = pd.Timestamp(args.calib_from).toordinal()
    eval_from = pd.Timestamp(args.eval_from).toordinal()
    k = len(grid)

    if args.fix_barrier is not None:
        _run_fix_barrier(args, x, close, fwd_low, day, seq, grid, cutoff0, calib_from)
        return

    X, y, group = expand_barriers(x, close, fwd_low, grid)
    del close, fwd_low                          # x 留着：R1 单调性扫描要用真实特征行做探针
    day_rep = np.repeat(day, k)
    seq_rep = np.repeat(seq, k)
    barrier_of = X[:, BARRIER_COL]
    logger.info("展开：%d 组 × %d 个 barrier = %d 训练行（%.0fs）",
                len(y) // k, k, len(y), time.time() - t0)
    for j, g in enumerate(grid):
        idx = slice(j, len(y), k)
        logger.info("  barrier %.1f%%  实测率 %.2f%%", g * 100, float(y[idx].mean()) * 100)

    oof = np.full(len(y), np.nan)
    best_iters, folds = [], []
    start, max_day = cutoff0, int(day_rep.max())
    while start <= max_day:
        end = start + args.fold_days
        train_idx, valid_idx = fold_indices(day_rep, group, seq_rep, start, end,
                                           args.horizon, base.EMERBAGO_DAYS)
        if len(valid_idx) < 2000 or len(train_idx) < 100_000:
            start = end
            continue
        booster, best, proba = base.fit_fold(X, y, train_idx, valid_idx,
                                             lgb_params_with_barrier(args), args.iterations)
        best_iters.append(best)
        oof[valid_idx] = proba
        m = base.metrics(y[valid_idx], proba)
        m["window"] = (f"{pd.Timestamp.fromordinal(int(start)).date()}"
                       f"~{pd.Timestamp.fromordinal(int(end - 1)).date()}")
        m["train_n"] = int(len(train_idx))
        m["era"] = "pre" if start < calib_from else "post"
        m["auc_by_barrier"] = _auc_by_barrier(y[valid_idx], proba, barrier_of[valid_idx], grid)
        folds.append(m)
        logger.info("折 %s 训练 %d → 验证 %d 独立窗口×%d  AUC=%.4f Brier=%.5f 基准=%.2f%% "
                    "x=10%%层AUC=%.4f  best_iter=%d", m["window"], m["train_n"], m["n"], k,
                    m.get("auc", float("nan")), m.get("brier", float("nan")),
                    m["base_rate"] * 100, m["auc_by_barrier"].get("0.1", float("nan")), best)
        del booster
        start = end
    if not folds:
        logger.error("验证折为空（--valid-start/--fold-days 与数据区间不匹配）")
        raise SystemExit(2)

    have = np.flatnonzero(~np.isnan(oof))
    pooled = base.metrics(y[have], oof[have])
    logger.info("样本外合计 %d 行（独立窗口×%d）：AUC=%.4f Brier=%.5f 基准=%.2f%%",
                pooled["n"], k, pooled["auc"], pooled["brier"], pooled["base_rate"] * 100)

    # ---------- 分层校准：每档 barrier 一条 isotonic 曲线 ----------
    cal = have[(day_rep[have] >= calib_from) & (day_rep[have] < eval_from)]
    ev = have[day_rep[have] >= eval_from]
    if len(cal) < 20_000:
        logger.warning("校准行只有 %d（<20k），改用全部样本外行（R4 将带在样乐观）", len(cal))
        cal, ev = have, have
    curves, layer_stats, r4 = [], {}, {}
    for g in grid:
        sel = cal[barrier_of[cal] == g]
        if len(sel) < 2_000:
            logger.warning("barrier %.1f%% 校准行不足（%d），该层不出曲线", g * 100, len(sel))
            continue
        curve = fit_isotonic(oof[sel], y[sel])
        curve["x"] = float(g)
        curve["base_rate_pct"] = round(float(y[sel].mean()) * 100, 2)
        curves.append(curve)
        sel_ev = ev[barrier_of[ev] == g]
        m_ev = base.metrics(y[sel_ev], oof[sel_ev]) if len(sel_ev) >= 500 else {}
        layer_stats[f"{g:g}"] = {"calib_n": int(len(sel)),
                                 "base_rate_pct": curve["base_rate_pct"],
                                 "holdout_auc": m_ev.get("auc"),
                                 "holdout_n": m_ev.get("n"),
                                 "score_auc_calib": base.metrics(y[sel], oof[sel]).get("auc")}
        r4[f"{g:g}"] = _max_calibration_deviation_pp(curve, oof[sel_ev], y[sel_ev]) \
            if len(sel_ev) >= 500 else None
        logger.info("校准 barrier %.1f%%：拟合 %d 行（基准 %.2f%%）留出 %s 行 AUC=%s 最大偏差 %s pp",
                    g * 100, len(sel), curve["base_rate_pct"], m_ev.get("n"),
                    m_ev.get("auc"), r4[f"{g:g}"])

    # ---------- 终版全量重拟合（迭代数取各折中位）+ R1 单调性 ----------
    iters = max(1, int(np.median(best_iters)))
    logger.info("终版拟合：全量 %d 行，num_iteration=%d（各折中位）", len(y), iters)
    import lightgbm as lgb

    ds = lgb.Dataset(X, label=y, categorical_feature=[fx.BOARD_COL], free_raw_data=False)
    final = lgb.train(lgb_params_with_barrier(args), ds, num_boost_round=iters)
    rng = np.random.default_rng(args.seed)
    probes = x[rng.choice(len(x), size=min(args.probes, len(x)), replace=False)]
    probes = probes[np.isfinite(probes).all(axis=1)]
    viols = monotonicity_violations(
        lambda v: float(final.predict(np.asarray([v], dtype=np.float32),
                                      num_iteration=iters)[0]), probes, grid)
    logger.info("R1 单调性（%d 行真实特征 × %d 档 barrier）：违例 %d", len(probes), len(grid), viols)

    auc10 = layer_stats.get("0.1", {}).get("score_auc_calib")
    acceptance = {
        "preregistered_in": "docs §50.4",
        "R1_monotonicity_violations": int(viols),
        "R1_probes": int(len(probes)),
        "R3_auc_at_barrier_10pct": auc10,
        "R3_reference_current_downside_arm_auc": _current_downside_auc(Path(args.meta)),
        "R4_max_deviation_pp_by_barrier": r4,
        "R5_constant_barrier_control": "见 --fix-barrier 0.1 那次运行的日志",
        "note": "R2/R6/R7 是服务侧行为，测试在 tests/test_barrier_model.py 与 "
                "plugin/tests/test_conviction_barrier.py；R2（600272 十行读数）要等模型落盘后跑。",
    }
    logger.info("验收：R1 违例 %d，R3 x=10%% 层 AUC %s（对照现下臂 %s），R4 最大偏差 %s",
                viols, auc10, acceptance["R3_reference_current_downside_arm_auc"], r4)

    out_model = Path(args.out_model)
    out_model.parent.mkdir(parents=True, exist_ok=True)
    final.save_model(str(out_model), num_iteration=iters)
    logger.info("已写 %s（%d 行 Booster）", out_model, iters)

    block = {
        "version": 1,
        "model_file": out_model.name,
        "num_iteration": iters,
        "best_iterations": best_iters,
        "anchor": "cost",
        "drop": args.drop,
        "horizon": args.horizon,
        "barriers": [float(g) for g in grid],
        "features": fx.FEATURES_WITH_BARRIER,
        "numeric_features": fx.NUMERIC_FEATURES,
        "required_features": list(fx.REQUIRED_FEATURES),
        "board_levels": list(bucket.BOARD_CATEGORIES),
        "monotone_constraints": lgb_params_with_barrier(args)["monotone_constraints"],
        "label_contract": {
            "horizon": args.horizon, "drop": args.drop, "anchor": "cost",
            "meaning": (f"P(未来 {args.horizon} 个交易日内最低价 <= 成本×(1−{args.drop}))；"
                        "服务时 x=1−成本×(1−drop)/现价，barrier 列带单调递减约束"
                        "（现价越靠近线 ⇒ 概率只能升）。现价已在线下时不给概率。"),
            "neutral_included": True,
        },
        "params": lgb_params_with_barrier(args),
        "data": {"samples": str(samples_path), "groups": int(len(y) // k),
                 "rows": int(len(y)), "frac": float(keep.mean()),
                 "range": [str(pd.Timestamp.fromordinal(int(day.min())).date()),
                           str(pd.Timestamp.fromordinal(int(day.max())).date())]},
        "validation": {"scheme": (f"purged walk-forward，窗口 {args.fold_days} 自然日，"
                                  f"embargo {base.EMERBAGO_DAYS} 自然日，验证内按 seq % "
                                  f"{args.horizon} 去重叠，同 (code,date) 的 {k} 份复制整组同侧"),
                       "folds": folds, "pooled": pooled},
        "base_rate_by_barrier_pct": {kk: v["base_rate_pct"] for kk, v in layer_stats.items()},
        "layer_stats": layer_stats,
        "calibration": curves,
        "calibration_fit_window": [args.calib_from, str(pd.Timestamp.fromordinal(int(eval_from)).date())],
        "acceptance": acceptance,
        "importance_gain": _importance(final, iters),
    }
    _merge_meta(Path(args.meta), block, dry_run=args.smoke)
    logger.info("完成（%.0fs）", time.time() - t0)


def _run_fix_barrier(args, x, close, fwd_low, day, seq, grid, cutoff0, calib_from):
    """R5 反作弊对照：barrier 列一律同一个常量（不复制行），只报读数、不写 meta。"""
    X = np.empty((x.shape[0], x.shape[1] + 1), dtype=np.float32)
    X[:, :-1] = x
    X[:, -1] = args.fix_barrier
    y = barrier_labels(close, fwd_low, [args.fix_barrier])[:, 0]
    logger.info("[R5] barrier 固定 %.1f%%：%d 行，实测率 %.2f%%", args.fix_barrier * 100,
                len(y), float(y.mean()) * 100)
    oof = np.full(len(y), np.nan)
    start, max_day = cutoff0, int(day.max())
    while start <= max_day:
        end = start + args.fold_days
        valid_idx = np.flatnonzero((day >= start) & (day < end) & (seq % args.horizon == 0))
        train_idx = np.flatnonzero(day < start - base.EMERBAGO_DAYS)
        if len(valid_idx) < 2000 or len(train_idx) < 100_000:
            start = end
            continue
        _, best, proba = base.fit_fold(X, y, train_idx, valid_idx,
                                       lgb_params_with_barrier(args), args.iterations)
        oof[valid_idx] = proba
        m = base.metrics(y[valid_idx], proba)
        logger.info("[R5] 折 %s AUC=%.4f 基准=%.2f%% best_iter=%d",
                    f"{pd.Timestamp.fromordinal(int(start)).date()}~"
                    f"{pd.Timestamp.fromordinal(int(end - 1)).date()}",
                    m.get("auc", float("nan")), m["base_rate"] * 100, best)
        start = end
    have = np.flatnonzero(~np.isnan(oof))
    m = base.metrics(y[have], oof[have])
    logger.info("[R5] 合计 %d 独立窗 AUC=%.4f 基准=%.2f%%（应与现 −10%% 臂同量级：0.71 上下；"
                "明显更高就是复制行泄漏）", m["n"], m.get("auc", float("nan")),
                m["base_rate"] * 100)


def _auc_by_barrier(y_valid, proba, barrier_valid, grid):
    out = {}
    for g in grid:
        sel = barrier_valid == g
        if len(np.unique(y_valid[sel])) > 1:
            out[f"{g:g}"] = base.metrics(y_valid[sel], proba[sel]).get("auc")
    return out


def _max_calibration_deviation_pp(curve, score, label, bins: int = 10):
    """R4：留出段按校准后的概率切 bins 段，段内 |预测−实测| 的最大值（pp）。

    曲线是在 calib~eval 那段拟合的，这里只在 eval 之后评——同段自评会带上 isotonic 的在样乐观。
    """
    if len(score) < bins * 50 or not curve.get("xs"):
        return None
    p = np.interp(score, curve["xs"], curve["ys"])
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    worst = 0.0
    for b in range(len(edges) - 1):
        m = idx == b
        if m.sum() < 100:
            continue
        worst = max(worst, abs(float(p[m].mean()) - float(label[m].mean())))
    return round(worst * 100, 2)


def _importance(booster, iters):
    gain = booster.feature_importance(importance_type="gain")
    order = np.argsort(gain)[::-1]
    return {fx.FEATURES_WITH_BARRIER[i]: round(float(gain[i]), 2) for i in order}


def _current_downside_auc(meta_path: Path):
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")) \
            .get("downside", {}).get("validation", {}).get("pooled", {}).get("auc")
    except Exception:                                   # 对照数字拿不到不影响本臂落盘
        return None


def _merge_meta(meta_path: Path, block: dict, dry_run: bool = False) -> None:
    """把 barrier 块并进 conviction_model.json（插件按 meta["barrier"] + 同目录文件才走这档）。

    先备份原 meta：回滚只要 `mv barrier_model.lgb.txt{,.off}`，但万一这个块写坏了，
    备份是唯一能把上涨/下行两档的口径原样拿回来的东西。
    """
    if dry_run:
        logger.info("[smoke] 跳过写 meta（假读数会污染验收表）")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(meta_path, meta_path.with_name(f"conviction_model.{stamp}.bak.json"))
    meta["barrier"] = block
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已把 barrier 块写进 %s（原文件备份为 conviction_model.%s.bak.json）",
                meta_path.name, stamp)


if __name__ == "__main__":
    main()
