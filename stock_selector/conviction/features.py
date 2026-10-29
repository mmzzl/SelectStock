"""可信度模型的特征契约：训练侧和线上侧共用同一个装配函数。

为什么单独一个模块（和 bucket.py 同一理由）：模型吃的是「线上取到的那一行指标」，
只要训练与线上的取列/换算不一致（比如线上用 close/ma20-1 而训练用样本里的 bias20 列，
或 atr 一边是比值一边是绝对值），AUC 好看也是假的，而且不会报错——只会悄悄给出错的概率。
所以 `FEATURES` 的列序、缺失处理、`row_to_vector` 都只写这一份，
scripts/train_conviction_model.py 与 plugin/portfolio/conviction.py 都从这里取。

`row_to_vector` 只允许 provider 最新指标行里有的列（`lu` 也在 provider 的取列清单里）。
任一列缺失 → NaN 交给 LightGBM 的缺失分支；但 bias20/pos60/vol_ratio 三个核心任一不可判时
`ok=False`，调用方据此退化，不硬猜。
"""

from __future__ import annotations

import math

from stock_selector.conviction.bucket import classify_board

NUMERIC_FEATURES = ["bias20", "pos60", "vol_ratio", "vol_z", "rsi14", "adx14", "atr_n", "lu"]
FEATURES = [*NUMERIC_FEATURES, "board"]
FEATURES_WITH_BARRIER = [*FEATURES, "barrier"]
BOARD_COL = len(NUMERIC_FEATURES)          # row_to_vector 返回向量里 board 的下标
REQUIRED_FEATURES = ("bias20", "pos60", "vol_ratio")


def _f(value):
    """非有限值（None/NaN/inf/字符串）→ None：让调用方走缺失分支而不是塞 0。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def bias20(row: dict):
    """样本表里 bias20 是 close/ma20-1 的现算列，线上没有该列，同式现算。"""
    v = _f(row.get("bias20"))
    if v is not None:
        return v
    close, ma20 = _f(row.get("close")), _f(row.get("ma20"))
    if close is None or ma20 is None or ma20 <= 0:
        return None
    return close / ma20 - 1.0


def atr_n(row: dict):
    """ATR 归一到价格（绝对 ATR 在不同价位的票之间不可比，屏障是按比例定义的）。"""
    atr, close = _f(row.get("atr14")), _f(row.get("close"))
    if atr is None or close is None or close <= 0:
        return None
    return atr / close


def board_code(row: dict):
    from stock_selector.conviction.bucket import board_index

    return board_index(classify_board(row.get("code", "")))


def row_to_vector(row: dict) -> tuple[list[float], bool]:
    """最新指标行 → 模型特征向量（顺序即 FEATURES）。

    返回 (向量, 可用标志)。ok=False 表示核心特征不齐，调用方必须退化而不是喂半行给模型。
    """
    values = [bias20(row), _f(row.get("pos60")), _f(row.get("vol_ratio")),
              _f(row.get("vol_z")), _f(row.get("rsi14")), _f(row.get("adx14")),
              atr_n(row), _f(row.get("lu"))]
    ok = all(v is not None for k, v in zip(REQUIRED_FEATURES, values[:3]))
    vec = [(v if v is not None else float("nan")) for v in values]
    b = board_code(row)
    vec.append(float(b) if b is not None else float("nan"))
    return vec, ok


def row_to_vector_with_barrier(row: dict, barrier: float) -> tuple[list[float], bool]:
    """成本锚臂（§50）的装配：`FEATURES` 之后**再挂一列 barrier**，列序即 FEATURES_WITH_BARRIER。

    `barrier` = 「还要跌多少才碰到那条线」，相对现价（`1 − 线价/现价`）。训练时同一行样本复制
    K 份、每份配一个网格上的 barrier，服务时喂成本换算出来的那个 x——两边的锚都是当日收盘，
    所以这两侧的 (特征, barrier) 是同构的。这一列必须排在最后，否则 barrier 臂按错位打分（不报错）。
    """
    vec, ok = row_to_vector(row)
    vec.append(float(barrier))
    return vec, ok
