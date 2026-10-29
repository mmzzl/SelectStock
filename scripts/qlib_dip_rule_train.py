"""批次 H：模型学「今天涨得多 → 明天低开开盘买 → 涨了就卖 / 跌 5% 止损」这笔交易的结局。

2026-10-29 用户逐条点名的口径（原话与本体的对应关系写在 `stock_selector_qlib/dip_rule.py`）：
人口 今收涨幅 ≥+5% ∩ 次日开盘低于今收 ⇒ **开盘价买入**；之后哪天收盘 > 买入价就哪天卖，
跌 5% 触及即按 买入价×0.95 卖；持有上限 10 个交易日（我替他补的数，`--max-hold` 可改）。

与 batchG_evt（§44，已上线的低开榜）的**唯一**区别是标签：那一臂只量「明开→明收」一条腿，
本臂量**按他的规则走完这笔交易**的收益（路径依赖 ⇒ qlib 表达式写不出来，逐笔走日线）。
池、窗口、切分、归一化、超参与 batchG_evt 逐位相同，两臂读数可以同尺对照。

    python3 -u scripts/qlib_dip_rule_train.py
    python3 -u scripts/qlib_dip_rule_train.py --min-chg 0.0975 --out-name batchH_board
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
BOARD_PCT = 0.0975            # 对照子集：信号日涨停的那批
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
    out: list[str] = []
    for line in (QLIB_DIR / "instruments" / name).read_text(encoding="utf-8").splitlines():
        sym = line.split("\t")[0].strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def build_events(pool, beg, end, min_chg, max_gap, stop_pct, max_hold):
    """逐批取因子超集 + 日线面板 ⇒ 只保留走完规则的事件行（内存靠切批压住）。"""
    from qlib.data import D

    from qlib_run import LABEL
    from stock_selector_qlib.alpha_style import Alpha158Full
    from stock_selector_qlib.dip_rule import rule_event_rows

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
        px = D.features(batch, ["$close", "$open", "$low"], start_time=beg, end_time=end)
        ev = rule_event_rows(px.rename(columns={"$close": "close", "$open": "open",
                                                "$low": "low"}),
                             min_chg=min_chg, max_gap=max_gap,
                             stop_pct=stop_pct, max_hold=max_hold)
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
    """区间内按分数在「同日事件」里排头部，比毛/净收益；t = 同日配对（头 N 减当天全体）。"""
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
    print(f"[{tag}] {ro['n']} 笔 / {ro['days']} 日，全体这笔的均值 毛 {ro['base']['gross'] * 100:+.3f}% "
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
    ap.add_argument("--min-chg", type=float, default=0.05, help="信号日涨幅下界（今天涨得多）")
    ap.add_argument("--max-gap", type=float, default=0.0, help="次日开盘相对今收的上界（下跌就买）")
    ap.add_argument("--stop-pct", type=float, default=-0.05, help="止损线（相对买入价）")
    ap.add_argument("--max-hold", type=int, default=10, help="持有上限交易日（含买入那天）")
    ap.add_argument("--out-name", default="batchH_r1")
    args = ap.parse_args()

    import qlib

    qlib.init(provider_uri=str(QLIB_DIR), kernels=1)
    pool = load_pool(args.pool)
    print(f"[池] {args.pool} → {len(pool)} 只，窗口 {args.beg}→{args.end}，规则 "
          f"涨≥{args.min_chg * 100:.1f}% ∩ 明开<{args.max_gap * 100:.1f}% 开盘买 → "
          f"收盘>买入价卖 / 跌{args.stop_pct * 100:.1f}%止损 / 上限{args.max_hold}天")

    t0 = time.time()
    X, y = build_events(pool, args.beg, args.end, args.min_chg, args.max_gap,
                        args.stop_pct, args.max_hold)
    d = y.index.get_level_values("datetime")
    mix = y["reason"].value_counts(normalize=True).to_dict()
    print(f"[事件] 合计 {len(y)} 笔 / {d.nunique()} 个交易日（{time.time() - t0:.0f}s），"
          f"均值 毛 {y['label'].mean() * 100:+.3f}%、胜率 {(y['label'] > 0).mean() * 100:.1f}%、"
          f"持有中位 {y['days'].median():.0f} 根；出场构成 {json.dumps(mix, ensure_ascii=False)}")

    import lightgbm as lgb

    tr = d <= pd.Timestamp("2022-12-31")
    va = (d >= pd.Timestamp("2023-01-01")) & (d < pd.Timestamp(args.test_beg))
    te = d >= pd.Timestamp(args.test_beg)
    print(f"[切分] 训练 {tr.sum()} / 验证 {va.sum()} / 测试 {te.sum()}")
    if min(tr.sum(), va.sum(), te.sum()) < 100:
        raise SystemExit("事件样本太薄，训练不成立")

    # 逐位复刻 qlib RobustZScoreNorm(clip_outlier=True)：median / (MAD+EPS)*1.4826 → clip(−3,3)，
    # 只在训练段事件行上拟合；服务侧 serving.normalize 用同一份 median/scale，两边必须同源。
    from qlib.data.dataset.processor import EPS

    center = np.nanmedian(X.loc[tr].to_numpy(dtype="float64"), axis=0)
    scale = np.nanmedian(np.abs(X.loc[tr].to_numpy(dtype="float64") - center), axis=0) + EPS
    scale *= 1.4826
    Xn = pd.DataFrame(np.clip((X.to_numpy(dtype="float64") - center) / scale, -3, 3),
                      index=X.index, columns=X.columns).fillna(0.0).astype("float32")
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
    board = y[y["signal_chg"] >= BOARD_PCT]
    ro_board = head_readout(board, score[board.index], t_beg, t_end)
    show("测试 2024→今 · 其中今日涨停", ro_board)

    name = Path(args.out_name)
    pred = score.rename("score").reset_index()
    pred.columns = ["datetime", "instrument", "score"]
    pred["datetime"] = pd.to_datetime(pred["datetime"])
    pred.to_parquet(OUT_DIR / f"pred_{name}.parquet")

    from stock_selector_qlib import artifacts

    dest = artifacts.save(booster, list(X.columns), center, scale, pool,
                          {"run_id": args.out_name,
                           "label": "rule(T+1开盘买→收盘涨卖/跌5%止损/上限10天)",
                           "rule_spec": {"min_chg": args.min_chg, "max_gap": args.max_gap,
                                         "stop_pct": args.stop_pct, "max_hold": args.max_hold,
                                         "cost": COST},
                           "train_end": "2022-12-31", "test_beg": args.test_beg,
                           "iters": iters, "n_events": int(len(y)),
                           "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          dest=OUT_DIR / f"model_{name}")
    (OUT_DIR / f"run_{args.out_name}.json").write_text(json.dumps(
        {"run_id": args.out_name, "pool": args.pool, "window": [args.beg, args.end],
         "rule_spec": {"min_chg": args.min_chg, "max_gap": args.max_gap,
                       "stop_pct": args.stop_pct, "max_hold": args.max_hold, "cost": COST},
         "n": {"train": int(tr.sum()), "valid": int(va.sum()), "test": int(te.sum())},
         "iters": iters, "rmse_valid": float(booster.best_score["valid_1"]["rmse"]),
         "event_mean_label": float(y["label"].mean()),
         "exit_mix_all": mix,
         "exit_mix_test": y[te]["reason"].value_counts(normalize=True).to_dict(),
         "hold_days_median_test": float(y[te]["days"].median()),
         "head_all": ro_all, "head_board": ro_board,
         "built_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[产物] {dest} + pred_{name}.parquet + run_{args.out_name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
