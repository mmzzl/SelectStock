"""把训练完的 qlib 模型收敛成「服务进程读得动」的产物。

训练脚本（scripts/qlib_run.py）跑完就退出了，内存里那份 booster + 稳健标准化统计
必须落盘，否则服务进程要么重训一次（3.4 GB、2 分 20 秒），要么自己重算归一化统计——
后者算出来的是「最近 130 天」的中位数/MAD，和训练段（2016→2021）不是一回事，
分数就和已归档的初版读数对不上。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from stock_selector_qlib import artifacts


def export_from_dataset(model, dataset, pool: list[str], meta: dict,
                        dest: Path | None = None) -> Path:
    """从 DatasetH 里取出 RobustZScoreNorm 的拟合结果 + LightGBM booster，落一次盘。"""
    handler = dataset.handler
    # DataHandlerLP 把处理器存在公开属性 infer_processors 上（_infer_processors 不存在）
    norms = [p for p in handler.infer_processors if type(p).__name__ == "RobustZScoreNorm"]
    if len(norms) != 1:
        raise RuntimeError(f"预期 1 个 RobustZScoreNorm，实际 {len(norms)} 个")
    proc = norms[0]
    # get_group_columns 在多级列名下给的是元组（('$close','KMID')），而 handler.fetch
    # 出来的因子表是单层名（'KMID'）；两边必须同一套键，否则服务侧按列对齐直接对不上
    columns = [c[-1] if isinstance(c, tuple) else str(c) for c in proc.cols]
    mean = np.asarray(proc.mean_train, dtype="float64")
    std = np.asarray(proc.std_train, dtype="float64")
    if not (len(columns) == len(mean) == len(std)):
        raise RuntimeError(f"列数/均值/标准差长度不一致：{len(columns)}/{len(mean)}/{len(std)}")
    if not len(columns):
        raise RuntimeError("RobustZScoreNorm 没拟合到任何列（fit 窗口里没数据？）")
    meta = {
        **meta,
        "exported_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "feature_count": len(columns),
    }
    return artifacts.save(booster=model.model, columns=columns, mean=mean, std=std,
                          pool=pool, meta=meta, dest=dest)
