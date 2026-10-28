"""「明日榜」影子盘的纯算子（docs §37.6 开关①）。

只放最容易算错、最需要单测的四件事：取前 K、两条可执行入场腿、扣费、台账幂等。
打分与落库都在 `scripts/qlib_nextday_shadow.py`，这里不碰 qlib、不碰库、不碰网络。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 一次往返的成本（百分点），与 §35/§36/§37 读数同一把尺子
ROUNDTRIP_COST_PP = 0.12
SIDE_COST_PP = ROUNDTRIP_COST_PP / 2.0


def top_by_score(scores: pd.Series, k: int) -> list[str]:
    """取分数最高的 k 支；同分按代码升序 ⇒ 同一份分数每天重跑都是同一张榜，前瞻账才对得上。"""
    if k <= 0:
        raise ValueError(f"k 必须为正，收到 {k}")
    s = scores.dropna()
    if s.empty:
        return []
    ordered = sorted(s.items(), key=lambda cv: (-cv[1], cv[0]))
    return [code for code, _ in ordered[:k]]


def _pct(entry, exit_) -> float | None:
    """缺腿返回 None，**不按 0 入账**：0 的意思是"没跌"，缺数的意思是"不知道"。"""
    if not entry or not exit_:
        return None
    return (exit_ / entry - 1.0) * 100.0


def e1_pct(next_open: float | None, next_close: float | None) -> float | None:
    """E1 = T+1 开盘买 → T+1 收盘看（今晚看榜、明早动手、当天见分晓）。"""
    return _pct(next_open, next_close)


# qlib bin 面板是**未复权裸价**：主板一天最多 ±10%，跨夜的价对跳出这个带宽只可能是除权
EXRIGHT_TOLERANCE = 0.102


def _overnight_pct(entry, exit_, tol: float = EXRIGHT_TOLERANCE) -> float | None:
    v = _pct(entry, exit_)
    return None if v is None or abs(v) > tol * 100.0 else v


def hold_pct(signal_close: float | None, next_close: float | None) -> float | None:
    """续持腿 = T 收盘 → T+1 收盘（扛一整夜）；除权日返回 None，**不是记成一个巨大的跌幅**。"""
    return _overnight_pct(signal_close, next_close)


def gap_pct(signal_close: float | None, next_open: float | None) -> float | None:
    """隔夜腿 = T 收盘 → T+1 开盘（持仓票被卖出的那天，赚/亏的就是这一段；裸价 ⇒ 挡除权）。"""
    return _overnight_pct(signal_close, next_open)


def e2_pct(next_open: float | None, next_next_open: float | None) -> float | None:
    """E2 = T+1 开盘买 → T+2 开盘卖（同样跨一夜，所以也要挡除权）。"""
    return _overnight_pct(next_open, next_next_open)


def net_after_cost(gross_pct: float) -> float:
    return gross_pct - ROUNDTRIP_COST_PP


# 台账里那条"同池等权"基准腿的代号：不是真股票代码，不会被选进榜
POOL_ROW = "__POOL__"
# band 组合规则（§37.9 L3）每天那条账：持仓路径重放出来的组合收益，同样不是股票代码
BAND_ROW = "__BAND__"


def is_pick(row: dict) -> bool:
    """这一行是不是"名单里的一支"：排除两条记账行，并排除榜面扩到 band 之后多出来的不可买段。

    榜面要记到前 60 名（band 判定要用），但 E1 那本账预注册的是**前 30**——不隔开就会把
    没人预注册过的 31–60 名悄悄算进 E1 读数里。
    """
    if row.get("instrument") in (POOL_ROW, BAND_ROW):
        return False
    return row.get("buyable", True) is not False


def pool_baseline(bars: dict[str, tuple]) -> dict:
    """同池等权基准：对池内每支各算 E1/E2，再取均值（缺腿的那支只从对应那条腿上缺席）。

    §37 的判据是**超额**（名单 − 同池），不是绝对收益；前瞻账若不存这条腿，
    涨市里天天"达标"、跌市里天天"不达标"，和回测里那个 +0.099pp 完全没法对。
    给第 4 个分量（T 日收盘价）时多算一条 `e0_pct`＝同池 T 收→T+1 收，那是 band 账的基准。
    """
    e1 = [v for v in (e1_pct(*b[:2]) for b in bars.values()) if v is not None]
    e2 = [v for v in (e2_pct(b[0], b[2]) for b in bars.values()) if v is not None]
    e0 = [v for v in (hold_pct(b[3], b[1]) if len(b) > 3 else None for b in bars.values())
          if v is not None]
    return {"signal_date": None, "instrument": POOL_ROW, "rank": 0, "score": None,
            "e1_pct": float(np.mean(e1)) if e1 else None,
            "e2_pct": float(np.mean(e2)) if e2 else None,
            "e0_pct": float(np.mean(e0)) if e0 else None,
            "settled": bool(e1)}


def excess(picks: list[dict], base: dict, field: str) -> float | None:
    """名单均值 − 同池基准；任何一边缺数都返回 None，**不当 0 用**。"""
    vals = [r[field] for r in picks if r.get(field) is not None]
    if not vals or base is None or base.get(field) is None:
        return None
    return float(np.mean(vals)) - float(base[field])


def daily_excess(rows: list[dict]) -> pd.DataFrame:
    """按天把名单和**当天**那条 `__POOL__` 基准腿配平 ⇒ 得到超额序列（§37 的判据是超额）。

    只吃已结算的笔；基准腿缺数或那天没有已结算的笔 ⇒ 该腿 NaN（不当 0、也不拿绝对值冒充）。
    """
    dates = sorted({r["signal_date"] for r in rows})
    out = []
    for d in dates:
        day = [r for r in rows if r["signal_date"] == d]
        base = next((r for r in day if r["instrument"] == POOL_ROW), None)
        picks = [r for r in day if is_pick(r) and r.get("settled")]
        out.append({"signal_date": d,
                    "e1": excess(picks, base, "e1_pct"),
                    "e2": excess(picks, base, "e2_pct"),
                    "绝对e1": float(np.mean([r["e1_pct"] for r in picks
                                             if r.get("e1_pct") is not None])) if picks else None,
                    "笔": len(picks)})
    return pd.DataFrame(out).set_index("signal_date")


def band_plan(held: list[str], ranked: list[str], k: int = 30, band: int = 60) -> dict:
    """band 组合规则的纯决策（docs §37.9 L3）：**前 k 名建仓、只有跌出前 band 名才卖**。

    为什么要有这条：每天全换手（E1）的往返成本 0.12pp 比日内选股超额本身还大，扣费后是负的；
    把卖出门槛放宽到 band，换手降下来才付得起成本。规则本体：
    - 名次只由当日打分决定（`ranked` 按分数降序，只用 T 日及之前的信息）；
    - 手里的票名次 > band、或今天没打到分（判不了）⇒ 卖；其余一律不动，**涨跌都不中途换**；
    - 补仓只从前 k 名里取，取到把组合填回 k 支为止；前 k 名已在手里就不买第 k+1 名；
    - 打分覆盖不足 k 支 ⇒ 组合就短着，不硬填（`hold` 长度如实反映）。
    返回 `{"exits","keep","entries","hold"}`，都按名次升序，跟传入顺序无关（纯函数）。
    """
    rank = {c: i + 1 for i, c in enumerate(ranked)}
    uniq = [c for i, c in enumerate(held) if c not in held[:i]]
    key = lambda c: rank.get(c, 10 ** 9)
    exits = sorted((c for c in uniq if key(c) > band), key=key)
    keep = sorted((c for c in uniq if key(c) <= band), key=lambda c: rank[c])
    slots = max(0, k - len(keep))
    entries = [c for c in ranked[:k] if c not in keep][:slots]
    return {"exits": exits, "keep": keep, "entries": entries,
            "hold": sorted(keep + entries, key=lambda c: rank[c])}


def band_day(held: list[str], rows: list[dict], k: int = 30, band: int = 60,
             fetch=None) -> dict | None:
    """把 band 规则的一天结算成一条账（§37.9 L3 的前瞻版），按**开盘再平衡**的两条腿串起来算。

    `rows` 是**同一个信号日**的全部行（榜面行 + 那条 `__POOL__` 基准腿）。口径钉死（§37.13 修的就是这段）：
    - 一天 = 隔夜腿 × 日内腿：`隔夜 = mean(gap_pct)` 摊在**开盘簿**（昨天收在手里、今天开盘才卖的都在里面），
      `日内 = mean(e1_pct)` 摊在**收盘簿**（留下的 + 今早新买的）；`gross = ((1+隔夜)(1+日内)−1)`。
      卖出那支**必须算隔夜那条腿**——它 T 收盘还在手里、T+1 开盘才走；早先把它算成"当天没有收益"，
      在这个隔夜为负的窗口里等于凭空给策略加分（§37.13 量出来是 300 池 +0.093→+0.060、大人口 +0.116→+0.077pp）。
    - 今早新买的票本来就不扛隔夜（名单 17:30 才出，§35.5 那条红线）⇒ 它只进日内那条腿。
    - 缺腿的票从**它所在的那条腿**的均值里掉出去（不当 0），掉多少看得见：`n_open` vs `n_open_priced`。
    - `fetch(signal_date, instrument)` 给榜面上没有的名字现取行情（掉出前 band 名的持仓就是这种）；
      不给这个钩子时那些票的隔夜腿只能掉出去，所以**影子盘每晚必须传**。
    - 基准用 `__POOL__` 的 `e0_pct`（同池 T 收→T+1 收，同一人群口径）；这条腿没落账时毛收益照记、
      超额留 None、`settled=False`，等下次补——绝不拿绝对收益冒充超额。
    一天连一条腿都算不出来（多半是 T+1 还没走完）⇒ 返回 None，不落账也不动持仓。
    """
    named = [r for r in rows if r.get("instrument") not in (POOL_ROW, BAND_ROW)]
    ranked = [r["instrument"] for r in sorted(named, key=lambda r: r["rank"])]
    base = next((r for r in rows if r.get("instrument") == POOL_ROW), None)
    plan = band_plan(held, ranked, k=k, band=band)
    by = {r["instrument"]: r for r in named}
    d = next((r["signal_date"] for r in rows if r.get("signal_date")), None)

    def leg(inst: str, field: str) -> float | None:
        v = by.get(inst, {}).get(field)
        if v is None and fetch is not None and d is not None:
            v = fetch(d, inst).get(field)
        return None if v is None else float(v)

    opening = [c for i, c in enumerate(held) if c not in held[:i]]
    ov = [v for v in (leg(c, "gap_pct") for c in opening) if v is not None]
    inn = [v for v in (leg(c, "e1_pct") for c in plan["hold"]) if v is not None]
    if not inn:
        return None
    ov_mean = float(np.mean(ov)) if ov else 0.0
    in_mean = float(np.mean(inn))
    gross = ((1.0 + ov_mean / 100.0) * (1.0 + in_mean / 100.0) - 1.0) * 100.0
    n_side = len(plan["entries"]) + len(plan["exits"])
    cost = n_side * SIDE_COST_PP / len(inn)
    pool = base.get("e0_pct") if base else None
    ex = None if pool is None else gross - float(pool)
    return {"signal_date": d, "instrument": BAND_ROW, "rank": 0, "score": None,
            "hold": plan["hold"], "exits": plan["exits"], "entries": plan["entries"],
            "n_hold": len(inn), "n_new": len(plan["entries"]), "n_exit": len(plan["exits"]),
            "n_open": len(opening), "n_open_priced": len(ov),
            "overnight_pct": ov_mean, "intraday_pct": in_mean,
            "gross_pct": gross, "pool_pct": pool, "excess_pp": ex,
            "cost_pp": cost, "turnover": n_side / (2.0 * len(inn)),
            "net_pp": None if ex is None else ex - cost,
            "settled": ex is not None}


def replay_band(rows: list[dict], k: int = 30, band: int = 60, fetch=None) -> list[dict]:
    """按信号日顺序重放 band 组合：持仓从上一天的目标组合传来，全程只用账上已有的名次。

    每次跑都从头重放（不另存状态文件）⇒ 同一本账反复跑得同一串结果，补数据后也不会串位。
    某天还没有任何腿 ⇒ 跳过这一天、持仓不动。`fetch` 是给"掉出榜面、账上没有行情"的持仓现取腿用的。
    """
    held: list[str] = []
    out = []
    for d in sorted({r["signal_date"] for r in rows}):
        res = band_day(held, [r for r in rows if r["signal_date"] == d], k=k, band=band, fetch=fetch)
        if res is None:
            continue
        held = res["hold"]
        out.append(res)
    return out


def merge_ledger_rows(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """按 (signal_date, instrument) 幂等合并：补跑不重复记账，也不会用"还不知道"覆盖已结算的数。"""
    out: dict[tuple, dict] = {}
    for row in list(existing) + list(incoming):
        key = (row["signal_date"], row["instrument"])
        old = out.get(key)
        if old is None:
            out[key] = dict(row)
            continue
        merged = dict(old)
        for field, value in row.items():
            if field == "settled":
                merged["settled"] = bool(old.get("settled")) or bool(value)
            elif value is None and old.get("settled"):
                continue          # 已结算的数不能被"这次缺数"冲掉
            else:
                merged[field] = value
        out[key] = merged
    return [out[k] for k in sorted(out)]
