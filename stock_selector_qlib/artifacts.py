"""qlib 模型产物的落盘与读取：训练侧写一次，服务与回测只读。

为什么 mean/std 必须单独存：qlib 的 RobustZScoreNorm 是在**已加载的那段面板**上算
中位数/MAD 的（`RobustZScoreNorm.fit` 里 `np.nanmedian(X, 0)`）。服务进程只加载最近
一百多个交易日，自己重算出来的均值和训练段（2016→2021）差得很远，分数就不再是
`scripts/qlib_run.py` 那个模型了。存下统计量，两边才是同一个模型。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.environ.get("QLIB_MODEL_DIR", str(REPO / "data" / "qlib" / "model")))

MODEL_FILE = "model.txt"
NORM_FILE = "norm.npz"
POOL_FILE = "pool.txt"
META_FILE = "meta.json"


class Artifacts:
    """一份可服务化的 qlib 模型：booster + 稳健标准化统计 + 票池 + 元信息。"""

    def __init__(self, booster, columns: list[str], mean: np.ndarray, std: np.ndarray,
                 pool: list[str], meta: dict):
        self.booster = booster
        self.columns = list(columns)
        self.mean = np.asarray(mean, dtype="float64")
        self.std = np.asarray(std, dtype="float64")
        self.pool = list(pool)
        self.meta = dict(meta)

    @property
    def run_id(self) -> str:
        return str(self.meta.get("run_id", ""))

    def describe(self) -> dict:
        """给 /api/health 与日志用，不含大数组。"""
        return {
            "run_id": self.run_id,
            "features": len(self.columns),
            "pool_size": len(self.pool),
            "exported_at": self.meta.get("exported_at"),
            "train_end": self.meta.get("train_end"),
            "label": self.meta.get("label"),
            "qlib_version": self.meta.get("qlib_version"),
        }


def save(booster, columns, mean, std, pool, meta: dict, dest: Path | None = None) -> Path:
    """原子化落盘：先写临时目录再换名，避免半份产物被服务进程读到。"""
    dest = Path(dest or MODEL_DIR)
    tmp = dest / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    columns = [str(c) for c in columns]
    mean = np.asarray(mean, dtype="float64")
    std = np.asarray(std, dtype="float64")
    if len(columns) != len(mean) or len(columns) != len(std):
        raise ValueError(f"列数 {len(columns)} 与 mean/std 长度 {len(mean)}/{len(std)} 不一致")
    booster.save_model(str(tmp / MODEL_FILE))
    # 列序也是产物的一部分：LightGBM 按列名对齐，缺列必须当场报错而不是静默补 0
    np.savez(tmp / NORM_FILE, mean=mean, std=std, columns=np.array(columns, dtype=object),
             allow_pickle=True)
    (tmp / POOL_FILE).write_text("\n".join(pool) + "\n", encoding="utf-8")
    (tmp / META_FILE).write_text(json.dumps(meta, indent=2, ensure_ascii=False, default=str),
                                 encoding="utf-8")
    if dest.exists():
        for name in (MODEL_FILE, NORM_FILE, POOL_FILE, META_FILE):
            (dest / name).unlink(missing_ok=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    for name in (MODEL_FILE, NORM_FILE, POOL_FILE, META_FILE):
        (tmp / name).rename(dest / name)
    tmp.rmdir()
    return dest


def exists(dest: Path | None = None) -> bool:
    dest = Path(dest or MODEL_DIR)
    return all((dest / name).exists() for name in (MODEL_FILE, NORM_FILE, POOL_FILE, META_FILE))


def load(dest: Path | None = None) -> Artifacts:
    import lightgbm as lgb

    dest = Path(dest or MODEL_DIR)
    missing = [n for n in (MODEL_FILE, NORM_FILE, POOL_FILE, META_FILE)
               if not (dest / n).exists()]
    if missing:
        raise FileNotFoundError(
            f"qlib 模型产物不全（缺 {missing}）：{dest}；"
            "先跑 python3 -u scripts/qlib_run.py --universe 300 --backtest --export-model")
    norm = np.load(dest / NORM_FILE, allow_pickle=True)
    meta = json.loads((dest / META_FILE).read_text(encoding="utf-8"))
    pool = [line.strip() for line in (dest / POOL_FILE).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    booster = lgb.Booster(model_file=str(dest / MODEL_FILE))
    return Artifacts(booster=booster, columns=list(map(str, norm["columns"])),
                     mean=norm["mean"], std=norm["std"], pool=pool, meta=meta)
