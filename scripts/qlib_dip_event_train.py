"""批次 G2：把模型换成「只在『今天涨 → 明天低开买这一笔』事件上训练」的事件臂（2026-10-29 用户点名口径）。

和 `qlib_run.py --label-entry dip`（batchG_d1 / batchW_c3 对照，docs §44.2）的**唯一**区别在训练行：
那一版全池都是训练行、非事件标签填 0，模型只学会「板 vs 非板」一刀，
每天 900 只塌成 ~3 个不同分值，头 30 从第 4 名起是并列噪声。
本臂只在 `stock_selector_qlib.dip_event.event_rows` 定义的事件行上训练 ⇒ 输出「同样低开买，哪一笔更值得买」。

阈值默认取用户原话的字面版（今天涨 ≥+0.1% ∩ 明开低于今收）；严格版（板 + 低开 5%）
在 liq900 池 2016→2026 只有 553 笔，撑不住训练（§44.6 实测，传 `--min-chg 0.0975 --max-gap -0.05` 可复现）。

切分不越过时间边界：训练 ≤2022-12-31 / 验证 2023 全年 / 测试 2024-01-01→2026-10-16
（与 batchG_w900 同池同窗，读数可同尺对照）。归一化 mean/std 只用训练段行。

    python3 -u scripts/qlib_dip_event_train.py
    python3 -u scripts/qlib_dip_event_train.py --min-chg 0.0975 --max-gap -0.05 --out-name batchG_evt_strict
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

QLIB_DIR = Path("/data/work/qlib_data/cn")
OUT_DIR = REPO / "data" / "qlib"
CHUNK = 60
SEED = 42
COST = 0.0005 + 0.0015        # 买 0.05% + 卖 0.15%，与 backtest.EXCHANGE_KWARGS 同源
PARAMS = {
    "objective": "regression",
    "metric": "rmse",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "max_depth": 4,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.5,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l2": 20.0,
    "seed": SEED,
    "verbose": -1,
}


def load_pool(name: str) -> list[str]:
    path = QLIB_DIR / "instruments" / name
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        sym = line.split("\t")[0].strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def build_events(pool: list[str], beg: str, end: str, min_chg: float, max_gap: float):
    """逐批取因子超集 + 日线面板，只保留事件行 ⇒ (X, y)。内存靠切批压住（全池 10 年会 OOM）。"""
    from qlib.data import D

    from qlib_run import LABEL
    from stock_selector_qlib.alpha_style import Alpha158Full
    from stock_selector_qlib.dip_event import event_rows

    xs, ys = [], []
    for start in range(0, len(pool), CHUNK):
        batch = pool[start:start + CHUNK]
        t0 = time.time()
        handler = Alpha158Full(
            instruments=batch, start_time=beg, end_time=end,
            fit_start_time=beg, fit_end_time=end, label=[LABEL],
            infer_processors=[], learn_processors=[],
        )
        feat = handler.fetch(col_set="feature")
        px = D.features(batch, ["$close", "$open"], start_time=beg, end_time=end)
        ev = event_rows(px.rename(columns={"$close": "close", "$open": "open"}),
                        min_chg=min_chg, max_gap=max_gap)
        n_ev = len(ev)
        if n_ev:
            keep = ev.index.intersection(feat.index)
            xs.append(feat.loc[keep].astype("float32"))
            ys.append(ev.loc[keep])
            del ev, feat
        print(f"  [{start + len(batch)}/{len(pool)}] 事件 {n_ev} 笔，{time.time() - t0:.0f}s",
              flush=True)
    X = pd.concat(xs).sort_index()
    y = pd.concat(ys).sort_index()
    assert (X.index == y.index).all(), "特征与事件行对齐失败"
    return X, y


def head_readout(y: pd.DataFrame, score: pd.Series, beg: pd.Timestamp, end: pd.Timestamp) -> dict:
    """区间内按分数在「同日事件」里排头部，比 E1（毛）与扣费后（净）；t = 同日配对。"""
    d = y.index.get_level_values("datetime")
    sel = y[(d >= beg) & (d < end)].assign(score=score[(d >= beg) & (d < end)])
    if not len(sel):
        return {}
    base = sel.groupby(level="datetime")["label"].mean()
    out = {"n": int(len(sel)), "days": int(sel.index.get_level_values("datetime").nunique())}
    for n in (3, 5, 10, 20):
        head = sel.groupby(level="datetime", sort=False).apply(
            lambda g: g.nlargest(min(n, len(g)), "score")["label"].mean(), include_groups=False)
        diff = (head - base).dropna()
        t = float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))) if len(diff) > 2 else float("nan")
        out[f"head{n}"] = {"gross": float(head.mean()), "net": float(head.mean() - COST),
                           "paired_pp": float(diff.mean() * 100), "t": t}
    out["base"] = {"gross": float(sel["label"].mean()), "net": float(sel["label"].mean() - COST)}
    return out


def show(tag: str, ro: dict) -> None:
    if not ro:
        print(f"[{tag}] 区间内 0 笔事件")
        return
    print(f"[{tag}] {ro['n']} 笔 / {ro['days']} 日，全体 E1 毛 {ro['base']['gross'] * 100:+.3f}% "
          f"净 {ro['base']['net'] * 100:+.3f}%")
    for n in (3, 5, 10, 20):
        h = ro[f"head{n}"]
        print(f"    头 {n:>2}：毛 {h['gross'] * 100:+.3f}% 净 {h['net'] * 100:+.3f}%，"
              f"同日配对 {h['paired_pp']:+.3f}pp t {h['t']:+.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="liq900.txt")
    ap.add_argument("--beg", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-16")
    ap.add_argument("--test-beg", default="2024-01-01")
    ap.add_argument("--min-chg", type=float, default=0.001, help="信号日涨幅下界（今天涨）")
    ap.add_argument("--max-gap", type=float, default=0.0, help="次日开盘相对今收的上界（低开）")
    ap.add_argument("--out-name", default="batchG_evt")
    args = ap.parse_args()

    import qlib

    qlib.init(provider_uri=str(QLIB_DIR), kernels=1)
    pool = load_pool(args.pool)
    print(f"[池] {args.pool} → {len(pool)} 只，窗口 {args.beg}→{args.end}，"
          f"事件口径 涨≥{args.min_chg * 100:.2f}% ∩ 明开<{args.max_gap * 100:.2f}%")

    t0 = time.time()
    X, y = build_events(pool, args.beg, args.end, args.min_chg, args.max_gap)
    d = y.index.get_level_values("datetime")
    print(f"[事件] 合计 {len(y)} 笔 / {d.nunique()} 个交易日（{time.time() - t0:.0f}s），"
          f"E1 均值 {y['label'].mean() * 100:+.3f}%、胜率 {(y['label'] > 0).mean() * 100:.1f}%")

    import lightgbm as lgb

    tr = d <= pd.Timestamp("2022-12-31")
    va = (d >= pd.Timestamp("2023-01-01")) & (d < pd.Timestamp(args.test_beg))
    te = d >= pd.Timestamp(args.test_beg)
    print(f"[切分] 训练 {tr.sum()} / 验证 {va.sum()} / 测试 {te.sum()}")
    if min(tr.sum(), va.sum(), te.sum()) < 100:
        raise SystemExit("事件样本太薄，训练不成立")

    # 逐位复刻 qlib RobustZScoreNorm(clip_outlier=True)：median / (MAD+EPS)*1.4826 → clip(−3,3)，
    # 只在训练段事件行上拟合；服务侧 serving.normalize 用同一份 mean/std，两边必须同源。
    from qlib.data.dataset.processor import EPS

    center = np.nanmedian(X.loc[tr].to_numpy(dtype="float64"), axis=0)
    scale = np.nanmedian(np.abs(X.loc[tr].to_numpy(dtype="float64") - center), axis=0) + EPS
    scale *= 1.4826
    Xn = pd.DataFrame(np.clip((X.to_numpy(dtype="float64") - center) / scale, -3, 3),
                      index=X.index, columns=X.columns).fillna(0.0).astype("float32")
    print(f"[归一] 训练段拟合 median/MAD，clip(−3,3)，NaN→0")
    X = Xn

    dtr = lgb.Dataset(X[tr], y.loc[tr, "label"].to_numpy(), free_raw_data=False)
    dva = lgb.Dataset(X[va], y.loc[va, "label"].to_numpy(), reference=dtr, free_raw_data=False)
    booster = lgb.train(PARAMS, dtr, num_boost_round=2000, valid_sets=[dtr, dva],
                        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)])
    iters = int(booster.best_iteration or 400)
    score = pd.Series(booster.predict(X, num_iteration=iters), index=X.index, name="score")
    print(f"[训练] iters={iters} valid_rmse={booster.best_score['valid_1']['rmse']:.5f}")

    t_beg, t_end = pd.Timestamp(args.test_beg), pd.Timestamp("2099-01-01")
    ro_all = head_readout(y, score, t_beg, t_end)
    show("测试 2024→今 · 全部事件", ro_all)
    board = y[y["board_chg"] >= 0.0975]
    ro_board = head_readout(board, score[board.index], t_beg, t_end)
    show("测试 2024→今 · 其中今日板", ro_board)

    name = Path(args.out_name)
    pred = score.rename("score").reset_index()
    pred.columns = ["datetime", "instrument", "score"]
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    pred.to_parquet(OUT_DIR / f"pred_{name}.parquet")

    from stock_selector_qlib import artifacts

    dest = artifacts.save(booster, list(X.columns), center, scale, pool,
                          {"run_id": args.out_name, "label": "dip_event(E1 明开→明收)",
                           "event_spec": {"min_chg": args.min_chg, "max_gap": args.max_gap},
                           "train_end": "2022-12-31", "test_beg": args.test_beg,
                           "iters": iters, "n_events": int(len(y)),
                           "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          dest=OUT_DIR / f"model_{name}")
    (OUT_DIR / f"run_{args.out_name}.json").write_text(json.dumps(
        {"run_id": args.out_name, "pool": args.pool, "window": [args.beg, args.end],
         "event_spec": {"min_chg": args.min_chg, "max_gap": args.max_gap},
         "n": {"train": int(tr.sum()), "valid": int(va.sum()), "test": int(te.sum())},
         "iters": iters, "rmse_valid": float(booster.best_score["valid_1"]["rmse"]),
         "head_all": ro_all, "head_board": ro_board,
         "event_mean_e1": float(y["label"].mean()),
         "built_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[产物] {dest} + pred_{name}.parquet + run_{args.out_name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
