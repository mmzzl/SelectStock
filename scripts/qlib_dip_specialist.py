#!/usr/bin/env python3
"""板后低吸 specialist（batchF4）：训练人口只留「今日板」行——batchF1/2/3 的判决（§41.3/41.4/41.5）
证明在 300 池×全天的行上，无论标签怎么改，dip 交易 ~0.1% 的正样本都被稀释到树学不动
（lu gain=0 / ic 转负 / 早停 1 棵输出常数）。条件低频交易的正确机器是**人口过滤**：

- 训练行：当日 lu_flag==1（今收/昨收−1 ≥ 0.0975，今天板）；
- 标签：entry=dip（qlib_run.label_formula(1,"dip")）——明天低开进带 (0.905, 0.95) ⇒
  明开→明收收益，否则 0（不交易）。带内正样本占比 ~7%，树有得学；
- 归一化：RobustZScoreNorm 照旧在**全量训练段**拟合（与 serving 同一变换，不因为
  行过滤而漂移）；
- 早停：qlib_run 同款超参（lr 0.04 / leaves 128 / colsample 0.6 / subsample 0.7 /
  α=λ=10 / 600 轮上限 / patience 50），验证段=2023 的板行；
- 产物：data/qlib/model_batchF4/（四文件同线上契约），meta 里带 population=board_today。

线上侧（若换线）：serving 按 meta.population 过滤行（lu_flag==1 是模型自己的训练门，
表达式过滤，不是人工塞票），名单=当日板票按 specialist 分数排名。

用法:
    python3 scripts/qlib_dip_specialist.py                # 全量训练+导出
    python3 scripts/qlib_dip_specialist.py --skip-export  # 只训练看读数
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根：stock_selector_qlib 包

import numpy as np
import pandas as pd

import qlib_run
from qlib_run import build_dataset, label_formula, read_pool_file, write_instruments

QLIB_DIR = "/data/work/qlib_data/cn"
POOL_FILE = "data/qlib/pool_w_mainboard300.txt"
SEGMENTS = {"train": ("2016-01-01", "2022-12-31"),
            "valid": ("2023-01-01", "2023-12-31"),
            "test": ("2024-01-01", "2026-10-23")}
LU_COL = "lu_flag"
POOL_SOURCE_NAME = "batchf300"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--export-model", default="data/qlib/model_batchF4")
    args = ap.parse_args()

    import qlib
    from qlib.constant import REG_CN
    qlib.init(provider_uri=QLIB_DIR, region=REG_CN, kernels=1,
              expression_cache=None, dataset_cache=None)
    label = label_formula(1, "dip")
    dataset = build_dataset(qlib, POOL_SOURCE_NAME, SEGMENTS["train"][1], SEGMENTS,
                            label=label, feature_set="trendlu")
    from qlib.data.dataset.handler import DataHandlerLP

    def frame(seg: str) -> pd.DataFrame:
        """特征（179 列，infer 流）+ 原始标签（learn 流处理前的真值）。"""
        f = dataset.prepare(seg, col_set="feature", data_key=DataHandlerLP.DK_R)
        lab = dataset.prepare(seg, col_set="label", data_key=DataHandlerLP.DK_R)
        f = f.copy()
        f["_lab"] = lab.reindex(f.index).iloc[:, 0]
        return f

    tr, va, te = frame("train"), frame("valid"), frame("test")
    print(f"[全量] train {len(tr)} / valid {len(va)} / test {len(te)} 行")
    trb, vab, teb = (d[d[LU_COL] == 1] for d in (tr, va, te))
    print(f"[板行] train {len(trb)} / valid {len(vab)} / test {len(teb)}")
    for name, d in (("train", trb), ("valid", vab), ("test", teb)):
        nz = d[d._lab != 0]
        print(f"[带内] {name}: 非零标签 {len(nz)}/{len(d)} ({len(nz)/max(len(d),1)*100:.1f}%) "
              f"均值 {d._lab.mean()*100:+.3f}%")

    import lightgbm as lgb
    feat_cols = [c for c in tr.columns if c != "_lab"]
    params = {"objective": "regression", "loss": "mse", "learning_rate": 0.04,
              "num_leaves": 128, "colsample_bytree": 0.6, "subsample": 0.7,
              "reg_alpha": 10.0, "reg_lambda": 10.0, "num_threads": 4,
              "verbose": -1, "seed": 42}
    dtr = lgb.Dataset(trb[feat_cols], label=trb["_lab"], feature_name=feat_cols, free_raw_data=False)
    dva = lgb.Dataset(vab[feat_cols], label=vab["_lab"], feature_name=feat_cols, reference=dtr)
    booster = lgb.train(params, dtr, num_boost_round=600, valid_sets=[dtr, dva],
                        valid_names=["train", "valid"],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    print(f"[早停] best_iteration={booster.best_iteration} 树数={booster.num_trees()}")
    imp = booster.feature_importance(importance_type="gain")
    tot = float(imp.sum()) or 1.0
    pairs = sorted(zip(feat_cols, imp.astype(float)), key=lambda x: -x[1])
    print("[gain] 板热度相关:", [(n, f"{g/tot*100:.1f}%") for n, g in pairs if n.startswith("lu_")])
    print("[gain] Top8:", [(n, f"{g/tot*100:.1f}%") for n, g in pairs[:8]])

    # 测试段读数：板行按分数排头 30 vs 全部板行等权（同日配对差，§11 口径 H=1 不折算）
    teb = teb.copy()
    teb["_score"] = booster.predict(teb[feat_cols])
    rows = []
    for day, sub in teb.groupby(level=0):
        top = sub.nlargest(min(30, len(sub)), "_score")
        rows.append((top._lab.mean(), sub._lab.mean(), len(sub)))
    a = np.array(rows)
    ex = a[:, 0] - a[:, 1]
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
    print(f"[G-B′] 头30板行已实现带内收益 {a[:,0].mean()*100:+.3f}%/笔 vs 全体板行 {a[:,1].mean()*100:+.3f}%/笔 "
          f"超 {ex.mean()*100:+.3f}pp t {t:+.2f}（n={len(a)} 天）")

    if args.skip_export:
        return
    from stock_selector_qlib import artifacts
    from stock_selector_qlib.export import export_from_dataset

    class _M:                                   # export_from_dataset 只取 .model
        model = booster
    pool = read_pool_file(Path(POOL_FILE))
    dest = export_from_dataset(_M(), dataset, pool, {
        "run_id": "batchF4",
        "train_end": SEGMENTS["train"][1],
        "segments": {k: list(v) for k, v in SEGMENTS.items()},
        "label": label,
        "population": "board_today",
        "population_expr": "lu_flag == 1（今收/昨收−1 ≥ 0.0975，与训练过滤同一表达式）",
        "universe": 300,
        "pool_size": len(pool),
        "pool_file": POOL_FILE,
        "fixed_trees": None,
        "seed": 42,
        "qlib_version": getattr(qlib, "__version__", ""),
        "source": "scripts/qlib_dip_specialist.py",
    }, dest=Path(args.export_model))
    print(f"[落盘] {dest}")


if __name__ == "__main__":
    main()
