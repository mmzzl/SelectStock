"""批次 G2「板后低开买这一笔」事件臂的行本体（2026-10-29 用户点名口径）。

用户原话：「模型应该学，今天涨停，或者上涨，明天低开买入后，是否赚钱，
而不是今天涨停、明天买不进这种不符合逻辑的情况」。

对照 batchG_d1/w900（docs §44.2）：那一版把整池都留在训练行里、非事件标签填 0，
结果模型只学会「板 vs 非板」这一刀，每天 900 只塌成 ~3 个不同分值，头 30 从第 4 名起
全是并列。事件臂改成**只在事件行上训练**，输出的是「同样都是板后低开，哪一笔更值得买」。

边界与线上同源，别在这里另立一套：
- `BOARD_PCT = 0.0975`：§41/F2 触发带下界（收盘价口径，不是「一字板」）。
- `GAP_PCT = −0.05`：等价于 `api._dip_levels` 的买线 `round(今收 × 0.95)`，
  判据写成**价比**（明开 < 今收×0.95）而不是收益率比，正好落在 ×0.95 线上时不算事件。
- 不设「跌停一字买不进」的下界：§43 已随挡单一起取消。
"""
from __future__ import annotations

import pandas as pd

BOARD_PCT = 0.0975
GAP_PCT = -0.05

#: 事件行需要的面板列（qlib `D.features` 出来的两层索引 [datetime, instrument] 宽表）
REQUIRED_COLUMNS = ("close", "open")


def event_rows(px: pd.DataFrame, min_chg: float = BOARD_PCT,
               max_gap: float = GAP_PCT) -> pd.DataFrame:
    """从日线面板里挑出「F 涨 ∩ F+1 低开买得进」的事件行，标签 = F+1 开盘→收盘收益。

    索引沿用面板的 (datetime, instrument)，datetime 是**信号日 F**（决策时刻），
    执行与收益都落在 F+1，所以每条事件都要求 F+1 的开盘、收盘同时在场。

    两道阈值是参数不是口径漂移：
    - 严格版（默认 `min_chg=+9.75%`、`max_gap=−5%`）= §44.3 那把尺子，样本 553 笔/10 年；
    - 字面版（`min_chg=+0.1%`、`max_gap=0`）= 用户 2026-10-29 原话「今天涨停**或者上涨**、
      明天**低开**买入」，人群放大到可训练的量级，标签与交易语义一字未动。
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in px.columns]
    if missing:
        raise ValueError(f"面板缺列 {missing}，事件行需要 close/open")

    close, open_ = px["close"], px["open"]
    if not isinstance(close.index, pd.MultiIndex):
        raise ValueError("面板必须是 (datetime, instrument) 两层索引")
    if close.index.names[0] != "datetime":
        # qlib 两个入口层级顺序相反：`D.features` 给 (instrument, datetime)，handler 给
        # (datetime, instrument)。不统一就会与特征表交集为空（batchG_evt 第一次起跑 0 笔）。
        px = px.swaplevel().sort_index()
        close, open_ = px["close"], px["open"]
    by_inst = {"group_keys": False}

    prev_close = close.groupby(level="instrument", **by_inst).shift(1)
    next_open = open_.groupby(level="instrument", **by_inst).shift(-1)
    next_close = close.groupby(level="instrument", **by_inst).shift(-1)

    out = pd.DataFrame(
        {
            "signal_date": close.index.get_level_values("datetime"),
            "exec_date": next_open.index.get_level_values("datetime"),
            "board_chg": close / prev_close - 1.0,
            "gap": next_open / close - 1.0,
            "label": next_close / next_open - 1.0,
        },
        index=px.index,
    )
    is_up = out["board_chg"] >= min_chg
    buys_at_line = next_open < close * (1.0 + max_gap)
    complete = out[["board_chg", "gap", "label"]].notna().all(axis=1)
    rows = out[is_up & buys_at_line & complete]
    return rows.dropna(subset=["label"])
