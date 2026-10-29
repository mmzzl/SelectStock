"""复权因子：从**除权除息事件**按交易所公式推乘法因子台阶（A，docs §48）。

不吃行情、不碰 Mongo、不 import qlib——行情侧只做校验，全部纯函数，回测读数和以后的面板写入共用这一份算式。

为什么不让厂商给因子（2026-10-29 实测，§48.1 判决）：

- 东财 `fqt=2` 的后复权价**不是比例复权**。600519 在 2015-01→07 的 120 个交易日上，
  `hfq = 4.5680 ×(裸价 + 19.5942)` 拟合到最大残差 0.0052 元（正好是 2 位小数的舍入量级），
  而「裸价 × 常数因子」的乘法假设最大相对残差 2.701%。它的日涨幅被系统性压缩一成
  （裸价 −2.32% 它给 −2.11%）：拿它当收益基准会把每天涨跌打九折，拿 `hfq/裸价` 当因子得到的
  是一条随价格涨跌漂动的假曲线。
- 腾讯 `ifzq .../fqkline` 的复权价更糟：600608 给 −99.41 的负价、000018 的日间因子抖 43%，
  而且缺 `qfqday` 时静默回落成裸价——`stock_kline` 那 1,227 万行「标 qfq 实为裸价」就是这么来的。

所以唯一的准路是事件驱动：`除权(息)参考价 =(前收盘 + 配股价×配股比 − 每股现金红利)/(1 + 每股送转 + 每股配股)`，
台阶 = 参考价 / 前收盘，因子 = 台阶倒数的前缀积（锚在上市日 ⇒ 时间不变，历史不会因新分红改写；
要前复权口径就在读取时除以最新因子）。

一个已知缺口：`RPT_SHAREBONUS_DET` 只有派息/送股/转增，**不含配股**。配股稀（一年十几起），
真碰上会表现为「除权日裸价实际跌幅和事件隐含跌幅差得离谱」，由 `build_factors` 的 `suspect`
和 `unexplained_gaps` 兜出来，不静默。
"""
from __future__ import annotations

import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXECUTED = "实施分配"      # 预案/决案/股东大会通过/停止实施 都没真除权，进了表就是假台阶
SUSPECT_TOL = 0.20         # 除权日实际跌幅 vs 事件隐含跌幅的容差：主板±10% 涨跌停下不该超这一档
GAP_TOL = 0.02             # 「超过涨跌停」判定的宽限（盘中触及但未封住、复牌补跌这类）


def parse_events(rows, code=None) -> list[dict]:
    """东财分红送配明细（RPT_SHAREBONUS_DET）原始行 → 每股口径、升序、只留已实施的事件。

    接口给的 `PRETAX_BONUS_RMB` 是「每 10 股派 X 元（含税）」、`BONUS_RATIO`/`IT_RATIO` 是
    「每 10 股送/转增 X 股」，全部除以 10 才是每股口径。交易所算参考价用**含税**派息。

    同一天实施多笔是真实存在的（002043 在 2026-06-01 同时派 1.8 和 1.32），交易所那天只出**一个**
    除权参考价 ⇒ 按除权日合并成一项，派息/送/转分加，否则台阶算错还会在同一 (code,date) 写出两行。
    """
    merged: dict[str, dict] = {}
    for row in rows or []:
        if str(row.get("ASSIGN_PROGRESS") or "").strip() != EXECUTED:
            continue
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if not _DATE_RE.match(ex_date):
            continue
        item = merged.setdefault(ex_date, {
            "code": row.get("SECURITY_CODE") or code,
            "ex_date": ex_date,
            "cash": 0.0,
            "bonus": 0.0,
            "transfer": 0.0,
            "allot_price": 0.0,     # 配股不在这张表里，见模块头的缺口说明
            "allot_ratio": 0.0,
        })
        item["cash"] += float(row.get("PRETAX_BONUS_RMB") or 0.0) / 10.0
        item["bonus"] += float(row.get("BONUS_RATIO") or 0.0) / 10.0
        item["transfer"] += float(row.get("IT_RATIO") or 0.0) / 10.0
    return [merged[d] for d in sorted(merged)]


def exref_step(prev_close, cash=0.0, bonus=0.0, transfer=0.0,
               allot_price=0.0, allot_ratio=0.0) -> float:
    """除权(息)参考价 / 前收盘。纯派息给 (P−D)/P，纯送转给 1/(1+G)。"""
    numerator = prev_close + allot_price * allot_ratio - cash
    denominator = prev_close * (1.0 + bonus + transfer + allot_ratio)
    return numerator / denominator


def build_factors(bars, events, suspect_tol=SUSPECT_TOL) -> list[dict]:
    """按事件把因子台阶算出来：每个除权日一行，`factor` 是**该日起**适用的后复权因子（前缀积）。

    `bars` 要按日期升序、`close` 是裸价收盘。前收盘取事件之前**最后一根真实行情**（停牌的那几天
    不存在昨收），事件早于首根行情则整条跳过——没有前收盘就推不出台阶。
    """
    steps = []
    factor = 1.0
    closes = [(b["date"], float(b["close"])) for b in bars]
    for ev in events:
        prior = [c for d, c in closes if d < ev["ex_date"]]
        same = [c for d, c in closes if d == ev["ex_date"]]
        if not prior:
            continue
        prev_close = prior[-1]
        step = exref_step(prev_close, cash=ev.get("cash", 0.0), bonus=ev.get("bonus", 0.0),
                          transfer=ev.get("transfer", 0.0),
                          allot_price=ev.get("allot_price", 0.0),
                          allot_ratio=ev.get("allot_ratio", 0.0))
        factor *= 1.0 / step
        residual = None if not same else same[0] / (prev_close * step) - 1.0
        steps.append({
            "code": ev.get("code"),
            "date": ev["ex_date"],
            "prev_close": prev_close,
            "cash": ev.get("cash", 0.0),
            "bonus": ev.get("bonus", 0.0),
            "transfer": ev.get("transfer", 0.0),
            "step": step,
            "factor": factor,
            "residual": residual,
            "suspect": residual is not None and abs(residual) > suspect_tol,
        })
    return steps


def adjusted_bars(bars, steps, fields=("open", "high", "low", "close")) -> list[dict]:
    """裸价行情 + 因子台阶 → 带 `factor` 的复权行情（复权价 = 裸价 × 因子）。

    台阶只在除权日生效，非除权日的复权涨幅恒等于裸价涨幅；除权日那一截假跌被台阶吃掉。
    """
    out = []
    idx = 0
    factor = 1.0
    for bar in bars:
        while idx < len(steps) and steps[idx]["date"] <= bar["date"]:
            factor = steps[idx]["factor"]
            idx += 1
        row = dict(bar)
        row["factor"] = factor
        for field in fields:
            if bar.get(field) is not None:
                row[field] = float(bar[field]) * factor
        out.append(row)
    return out


def unexplained_gaps(bars, steps, limit_pct=0.10, tol=GAP_TOL) -> list[dict]:
    """跌幅超过涨跌停、却查不到登记事件的交易日＝日历漏了（或那天行情本身就错）。

    这是判断「日历抓全了没有」的唯一可用办法：不依赖厂商因子，只看裸价本身。
    主板 ±10%、ST ±5%、创业板/科创板 ±20%、北交所 ±30% 由 `limit_pct` 传进来，别在函数里猜。
    """
    covered = {s["date"] for s in steps}
    gaps = []
    for prev, cur in zip(bars, bars[1:]):
        if not prev["close"]:
            continue
        raw_ret = float(cur["close"]) / float(prev["close"]) - 1.0
        if abs(raw_ret) > limit_pct + tol and cur["date"] not in covered:
            gaps.append({"date": cur["date"], "raw_ret": raw_ret,
                         "prev_close": float(prev["close"]), "close": float(cur["close"])})
    return gaps
