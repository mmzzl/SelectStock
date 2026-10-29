"""批次 H「今天涨得多 → 明天低开开盘买 → 涨了就卖 / 跌 5% 止损」的规则本体（纯函数）。

用户 2026-10-29 逐条点名的口径（他先否掉了上一版「今天涨停、当天收盘卖」）：
  ① 人口：信号日收盘涨幅 ≥ +5%（`UP_MIN_CHG`）
  ② 买入：次日**只要下跌就买**（开盘 < 今收，`DOWN_GAP=0`，不再要求低到 −5%），**取开盘价**
  ③ 卖出：从买入那天起，**哪天收盘 > 买入价就哪天卖**（收盘成交）
  ④ 兜底：**跌 5% 就卖**（挂在 买入价×0.95，触及即按该价成交，跌穿也不按更低的收盘算）
  ⑤ 持有上限 `MAX_HOLD=10` 个交易日（含买入那天）：既没涨也没触止损，到期按当天收盘卖。
     这个数是我替他补的——没有上限，「横盘不走」的事件无法结算，回测会只统计已赚钱出场的那批。

两处保守选择，都是为了不把读数做高：
- 同一天既触止损又收盘高于买入价 ⇒ **记止损**（日线看不出日内顺序）。
- 买入日之前没有足够的后续行情走满规则（`reason="open"`）⇒ **丢掉该事件**，不拿最后一根冒充成交。

与 `dip_event.py` 的分工：那份是 §44 已上线的事件臂（明开→明收一条腿），这份是它的后继口径，
标签是**路径依赖**的，qlib 表达式写不出来，只能在日线面板上逐笔走。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

UP_MIN_CHG = 0.05
DOWN_GAP = 0.0
STOP_PCT = -0.05
MAX_HOLD = 10

#: 面板需要这三列；索引两层 (datetime, instrument)，`D.features` 给的反序也能吃
REQUIRED_COLUMNS = ("close", "open", "low")

OPEN_REASON = "open"


def rule_exit(entry_price: float, lows: np.ndarray, closes: np.ndarray, *,
              stop_pct: float = STOP_PCT, max_hold: int = MAX_HOLD) -> dict:
    """从买入那天起（含当天）向后走这条规则，返回第几根出场、什么价、为什么出。

    `lows`/`closes` 是同一只票按时间升序的数组，第 0 根就是买入日。
    """
    if not (entry_price > 0) or np.isnan(entry_price):
        raise ValueError(f"买入价不可用：{entry_price}")
    line = entry_price * (1.0 + stop_pct)
    n = min(max_hold, len(lows))
    for k in range(n):
        if lows[k] <= line:                  # ④ 止损优先（同日两条件都满足时按亏的记）
            return {"days": k, "price": line, "reason": "stop"}
        if closes[k] > entry_price:          # ③ 哪天收盘比买入价高就哪天卖
            return {"days": k, "price": float(closes[k]), "reason": "up"}
    if len(lows) >= max_hold:                # ⑤ 到期收盘卖
        return {"days": max_hold - 1, "price": float(closes[max_hold - 1]), "reason": "expired"}
    return {"days": -1, "price": float("nan"), "reason": OPEN_REASON}


def rule_event_rows(px: pd.DataFrame, min_chg: float = UP_MIN_CHG,
                    max_gap: float = DOWN_GAP, *, stop_pct: float = STOP_PCT,
                    max_hold: int = MAX_HOLD) -> pd.DataFrame:
    """把面板压成事件行：一行=一笔「T 涨得多 ∩ T+1 低开买」并按规则走完了的交易。

    索引 `(datetime=信号日 T, instrument)`，与 qlib 特征表同层序，可直接按行对齐。
    标签 `label` = 出场价 / 买入价 − 1，**未扣手续费**（脚本侧再减成本）。
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in px.columns]
    if missing:
        raise ValueError(f"面板缺列 {missing}，规则需要 close/open/low")
    if not isinstance(px.index, pd.MultiIndex):
        raise ValueError("面板必须是 (datetime, instrument) 两层索引")
    if px.index.names[0] != "datetime":
        # `D.features` 给 (instrument, datetime)，handler 给 (datetime, instrument)
        px = px.swaplevel().sort_index()

    rows: list[dict] = []
    for inst, g in px.sort_index(level="datetime").groupby(level="instrument", sort=False):
        dates = g.index.get_level_values("datetime")
        close = g["close"].to_numpy("float64")
        open_ = g["open"].to_numpy("float64")
        low = g["low"].to_numpy("float64")
        for i in range(len(close) - 1):
            prev = close[i - 1] if i > 0 else np.nan
            if not (prev > 0) or not (close[i] / prev - 1 >= min_chg):
                continue
            entry, today_close = open_[i + 1], close[i]
            if not (entry > 0) or not (entry < today_close * (1.0 + max_gap)):
                continue                      # ② 次日没下跌 = 不买，也不是事件
            ex = rule_exit(entry, low[i + 1:], close[i + 1:], stop_pct=stop_pct,
                           max_hold=max_hold)
            if ex["reason"] == OPEN_REASON:
                continue                        # 走不完规则的日子不当训练行
            rows.append({
                "signal_date": dates[i],
                "exec_date": dates[i + 1],
                "exit_date": dates[i + 1 + ex["days"]],
                "instrument": inst,
                "signal_chg": close[i] / prev - 1,
                "gap": entry / today_close - 1,
                "entry": entry,
                "exit_price": ex["price"],
                "days": ex["days"],
                "reason": ex["reason"],
                "label": ex["price"] / entry - 1,
            })
    if not rows:
        cols = ["signal_date", "exec_date", "exit_date", "signal_chg", "gap",
                "entry", "exit_price", "days", "reason", "label"]
        return pd.DataFrame(columns=cols).set_index(pd.MultiIndex.from_tuples([], names=["datetime", "instrument"]))
    out = pd.DataFrame(rows).set_index(["signal_date", "instrument"])
    return out.rename_axis(index={"signal_date": "datetime"}).sort_index()
