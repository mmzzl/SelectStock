"""FF-1 预注册读数（只读）：主力占比 `ratio_1d` / `ratio_3d` 有没有前向信息、对现榜有没有增量。

用法（正式读数，qlib 打分段约 10~20 分钟）：
    python3 -u scripts/fund_flow_signal_read.py > /tmp/ff_signal_read.log 2>&1 &
冒烟（只验管道通不通，读数不作数）：
    python3 -u scripts/fund_flow_signal_read.py --smoke

口径全部写死在 `stock_selector/docs/资金流数据口径与防前瞻红线.md` §八，
**读数出来之前不许改**；判据函数在 `tests/test_fund_flow_signal_read.py` 里逐条钉住。
不发钉钉、不写生产库、不动 `config/inputs.conf`、不改打分链；明细只落 /tmp。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DAILY_DIR = REPO / "data" / "fund_flow" / "daily"
RAW_DIR = REPO / "data" / "stock_kline"          # 未复权日线：封板判据的来源

# ---------------------------------------------------------------------------
# 窗口与门槛（§八.1 / §八.5）
# ---------------------------------------------------------------------------
MAIN = ("2026-04-14", "2026-08-31")        # 主窗 96 个交易日：判门槛
HOLDOUT = ("2026-09-01", "2026-10-13")     # 留出 25 天（20.7%）：不参与任何挑选
CONTROL = ("2026-10-20", "2026-10-27")     # 满票池 6 天：只报「口径换成今天的池子变不变」
M5 = ("2026-07-06", "2026-09-29")          # 本机 5m 分时的覆盖段（§29 实测 61 个交易日）
HALF_DAYS = ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19")   # 半截天，整段排除
MIN_ROWS = 5000                            # 满票池日的行数线（实测 5,147 起）

OOS_SCORE = "2026-07-01"                   # 落盘模型训练止于 6/30，门槛④ 的样本外段从这里起
HOLDS = (5, 10)                            # T+1 开盘 → T+1+h 收盘
COST = 0.002                               # 买 0.05% + 卖 0.15%，一笔来回
LIMIT_TOL = 0.098                          # 主板 ±10%，留 0.2pp 给最小变动价位取整
MAX_DELAY = 10                             # 跌停顺延超过 10 个交易日截断
RATIO3_DAYS = 3

T_FLOOR = 2.0                              # 门槛①：|t ÷ √10|
MIN_BUCKET_DAYS = 3000                     # 门槛②：每桶只·日
RHO_FLOOR = 0.6                            # 门槛③：十分桶 Spearman
OVERLAP = math.sqrt(10)                    # 5/10 日持有窗重叠的修正

BOARD_EXCLUDE = ("300", "301", "302", "688", "689")   # 无交易权限（线上 300/301/688/689 + 那只 302）
# 上面那三段是 §八.2 点名的；再加北交所/新三板/B 股：它们不是 ±10%，9.8% 这条封板线对不上。
# 满票池对照日（10-20 起）的名单里混进了 362 只 920/810/832/833，主窗那 121 天一只都没有。
NO_TRADE_PREFIX = BOARD_EXCLUDE + ("43", "81", "83", "87", "92", "200", "900")


def board_ok(code: str) -> bool:
    """这只票在不在本次读数的可交易段里（主板 ±10%，剔无权限段与北交所/B 股）。"""
    return not str(code).startswith(NO_TRADE_PREFIX)


def hole_days(counts: pd.Series, ratio: float = 0.85, window: int = 21) -> list[str]:
    """采集洞 = 某天票池数掉到**局部中位数**的 85% 以下（只看行数序列，不看任何行情结果）。

    用局部中位数而不是全窗中位数，是为了把「整段塌了一天的洞」和「采集口径换了个台阶」分开：
    2026-05-21 沪市 1,618 只整段没采到（当天只剩深市 1,406 只）是洞；
    04-14~05-06 每天稳定 2,173 只、05-07 起稳定 3,021 只是台阶，不是洞（台阶照 §八.1 留在主窗里，
    另在「票池口径对照」单独报）。两端凑不齐参照窗口的日子不判。
    """
    med = counts.rolling(window, center=True, min_periods=window).median()
    bad = counts.notna() & med.notna() & (counts < ratio * med)
    return [str(d) for d, k in bad.items() if k]

BEG, END = MAIN[0], HOLDOUT[1]


# ---------------------------------------------------------------------------
# 判据（纯函数，先写失败测试再实现）
# ---------------------------------------------------------------------------

def signal_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """资金流分区行 → 每票每天的 `ratio_1d` / `ratio_3d`（只吃 ≤T 的行）。

    - `ratio_1d` = 当日 `main_net_inflow ÷ amount`；
    - `ratio_3d` = T、T-1、T-2 三天 `Σ主力净额 ÷ Σ成交额`（成交额加权，不是三个比率的平均）；
    - 窗口不满 3 天、或三天里有任何一天成交额 ≤0（停牌/无成交）一律留 NaN，不填 0。
    """
    df = rows.loc[rows["valid"].astype(bool), ["code", "trade_date", "amount", "main_net_inflow"]].copy()
    df = df.sort_values(["code", "trade_date"], kind="mergesort").reset_index(drop=True)
    amt = df["amount"].where(df["amount"] > 0)
    net = df["main_net_inflow"].where(amt.notna())
    out = df[["code", "trade_date"]].copy()
    out["ratio_1d"] = net / amt
    by = out["code"]
    amt3 = amt.groupby(by).rolling(RATIO3_DAYS).sum().droplevel(0)
    net3 = net.groupby(by).rolling(RATIO3_DAYS).sum().droplevel(0)
    total = amt3.where(amt3 > 0)
    out["ratio_3d"] = net3 / total
    return out


def _is_bar(bar: dict | None) -> bool:
    if not bar:
        return False
    return not any(pd.isna(bar.get(k)) for k in ("open", "high", "close"))


def entry_state(prev_close: float, open_price: float, high: float) -> str:
    """T+1 开盘买不买得到（§八.4′）。

    分「开盘就封住」（买不进）与「开盘没封、盘中才封」（开盘价真实成交，不算买不进）——
    涨停要经过一段时间才封死，把后者一起剔掉是拿日线当瞬时。
    """
    got = entry_states(pd.Series([prev_close]), pd.Series([open_price]), pd.Series([high]))
    return str(got.iloc[0])


def entry_states(prev_close: pd.Series, open_price: pd.Series, high: pd.Series) -> pd.Series:
    """`entry_state` 的向量化版本（同一份判据，逐票一份截面）。"""
    ok = (prev_close.notna() & (prev_close > 0) & open_price.notna() & high.notna())
    prev = prev_close.where(ok)
    jump_open = open_price / prev - 1.0
    jump_high = high / prev - 1.0
    out = pd.Series("no_bar", index=prev_close.index, dtype="object")
    sealed = ok & jump_open.ge(LIMIT_TOL)
    later = ok & ~sealed & jump_high.ge(LIMIT_TOL)
    out[sealed] = "sealed_open"
    out[later] = "opened_then_sealed"
    out[ok & ~sealed & ~later] = "tradable"
    return out


def exit_plan(first_prev_close: float, bars: list[dict], max_delay: int = MAX_DELAY) -> dict:
    """出场日的跌停卖不出是真代价：顺延到下一个可成交日按开盘价卖，并记下多吃了多少跌幅。

    `bars` 是从原定出场日起、按交易顺序排的 {open, high, close}（停牌那天给 NaN）。
    返回 `reason`：
      normal         正常收盘出场
      closed_limit   收盘跌停但盘中打开过（按收盘价出场，偏保守，单独计数）
      deferred       全天封死跌停/停牌 → 顺延，price 是顺延后的开盘价
      truncated      顺延超过 max_delay 个交易日仍卖不出（price 留 NaN，明写条数）
    """
    scheduled_close = np.nan
    last_close = first_prev_close
    for i, bar in enumerate(bars[:max_delay + 1]):
        if _is_bar(bar):
            if pd.isna(scheduled_close):
                scheduled_close = bar["close"]
            sealed = bar["high"] / last_close - 1 <= -LIMIT_TOL
            if not sealed:
                if i == 0:
                    closed_limit = bar["close"] / last_close - 1 <= -LIMIT_TOL
                    return {"price": bar["close"], "delay_days": 0, "extra_drop": 0.0,
                            "reason": "closed_limit" if closed_limit else "normal"}
                drop = bar["open"] / scheduled_close - 1 if not pd.isna(scheduled_close) else np.nan
                return {"price": bar["open"], "delay_days": i, "extra_drop": drop,
                        "reason": "deferred"}
            last_close = bar["close"]
    return {"price": np.nan, "delay_days": max_delay, "extra_drop": np.nan, "reason": "truncated"}


def assign_deciles(values: pd.Series) -> pd.Series:
    """当日截面十分位（1=最低 … 10=最高）；只用当天这一列，绝不跨日取分位（那是未来信息）。"""
    pct = values.rank(method="first", ascending=True, pct=True)
    bucket = np.ceil(pct * 10).clip(1, 10)
    return pd.Series(bucket, index=values.index, dtype="Float64").astype("float64")


def adjusted_t(diffs: pd.Series) -> float:
    """逐日超额差序列的 t，再 ÷√10（5/10 日持有窗重叠的修正，与 §21/§29 同一把尺）。"""
    x = pd.to_numeric(diffs, errors="coerce").dropna()
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    if not sd or pd.isna(sd) or sd == 0:
        return math.copysign(math.inf, x.mean()) if x.mean() else np.nan
    return (x.mean() / (sd / math.sqrt(len(x)))) / OVERLAP


def signal_pass(m: dict) -> str:
    """一个信号的裁决：门槛 ①②③ 全过才算「过」；过线但留出段反号算「不稳」。"""
    t, rho = m["t_adj"], m["rho"]
    if pd.isna(t) or pd.isna(rho):
        return "不过"
    monotony_agrees = (t > 0) == (rho > 0)
    if not (abs(t) >= T_FLOOR and m["min_bucket_days"] >= MIN_BUCKET_DAYS
            and abs(rho) >= RHO_FLOOR and monotony_agrees):
        return "不过"
    if m["sign_holdout"] is None or pd.isna(m["sign_holdout"]):
        return "过"
    if m["sign_main"] != m["sign_holdout"]:
        return "不稳"
    return "过"


def incremental_gate(t_all: float, t_oos: float) -> bool:
    """门槛④：全窗 ≥2 **且** 样本外段（模型没见过的那段）单独也 ≥2。"""
    if pd.isna(t_all) or pd.isna(t_oos):
        return False
    return abs(t_all) >= T_FLOOR and abs(t_oos) >= T_FLOOR


def overall(per_signal: dict[str, str], incremental_ok: bool) -> str:
    """两个信号一起判：都过才谈接；只一个过按弱证据记；都不过就明说没有。"""
    passed = [k for k, v in per_signal.items() if v == "过"]
    if not passed:
        return "没有前向信号"
    if len(passed) < len(per_signal):
        return "弱证据"
    return "够格接" if incremental_ok else "只当展示与否决提示"


# ---------------------------------------------------------------------------
# 下面是读数管道（只读）：不发钉钉、不写生产库、不动 config/inputs.conf
# ---------------------------------------------------------------------------

FIELDS = ["$amount"]
COLUMNS = ["code", "name", "trade_date", "amount", "main_net_inflow", "close", "pct_change",
           "valid", "exclude_reason", "data_ts", "collected_at"]


def to_instrument(code: str) -> str:
    return ("SH" if code.startswith(("6", "9")) else "SZ") + code


def pool_counts(beg: str, end: str) -> pd.Series:
    """[beg, end] 每天的票池只数（valid 且板块可交易），只读 `code/valid` 两列。"""
    out = {}
    for path in sorted(DAILY_DIR.glob("*.parquet")):
        day = path.stem
        if not (beg <= day <= end):
            continue
        df = pd.read_parquet(path, columns=["code", "valid"])
        codes = df.loc[df["valid"].astype(bool), "code"]
        out[day] = int((~codes.str.startswith(NO_TRADE_PREFIX)).sum())
    return pd.Series(out).sort_index()


def stored_days(beg: str, end: str) -> list[str]:
    """[beg, end] 里的满票池信号日：分区存在、行数过线、且不是半截天（§八.1）。"""
    out = []
    for path in sorted(DAILY_DIR.glob("*.parquet")):
        day = path.stem
        if not (beg <= day <= end) or day in HALF_DAYS:
            continue
        if len(pd.read_parquet(path, columns=["code"])) >= MIN_ROWS:
            out.append(day)
    return out


def load_rows(signal_days: list[str], skip: set[str] = frozenset()) -> tuple[pd.DataFrame, list[str]]:
    """信号日的分区 + 往前多取 2 天（`ratio_3d` 的窗口），半截天与采集洞一律不进窗口。"""
    alld = sorted(p.stem for p in DAILY_DIR.glob("*.parquet"))
    lo = alld[max(0, alld.index(signal_days[0]) - RATIO3_DAYS + 1)]
    picked = [d for d in alld if lo <= d <= signal_days[-1] and d not in (HALF_DAYS + tuple(skip))]
    frames = [pd.read_parquet(DAILY_DIR / f"{d}.parquet", columns=COLUMNS) for d in picked]
    rows = pd.concat(frames, ignore_index=True)
    rows["instrument"] = rows["code"].map(to_instrument)
    return rows, picked


def selfcheck_future_invariance(rows: pd.DataFrame, sig: pd.DataFrame, cut: str) -> int:
    """防前瞻自查①：把 cut 之后的行全删掉重算，cut 及以前的信号必须逐位相同（§八.7）。"""
    past = rows[rows["trade_date"] <= cut]
    again = signal_frame(past)
    key = ["code", "trade_date"]
    j = again.merge(sig, on=key, suffixes=("_cut", "_full"), how="left")
    bad = 0
    for col in ("ratio_1d", "ratio_3d"):
        a, b = j[f"{col}_cut"], j[f"{col}_full"]
        bad += int(((a.notna() != b.notna()) | (a.fillna(0.0) != b.fillna(0.0))).sum())
    if bad:
        raise RuntimeError(f"防前瞻自查失败：{cut} 之后删掉 {bad} 个格子变了信号值")
    return len(j)


def selfcheck_data_ts(rows: pd.DataFrame) -> dict:
    """防前瞻自查②：`data_ts` 不得晚于它标注的交易日；违规直接报错退出（§八.7）。"""
    ts = rows["data_ts"].astype(str).str[:10]
    late = int((ts > rows["trade_date"].astype(str)).sum())
    if late:
        raise RuntimeError(f"{late} 行的 data_ts 晚于所属交易日，不能进任何回测")
    return {"rows": len(rows), "data_ts_le_trade_date": 0,
            "collected_at_missing": int(rows["collected_at"].isna().sum())}


def qlib_panels(instruments: list[str], beg: str, end: str) -> dict[str, pd.DataFrame]:
    """[beg, end]（外加顺延余量）的日线宽表；index=交易日字符串，columns=qlib 代码。"""
    import qlib.data

    from stock_selector_qlib import serving

    serving.init_qlib()
    cal = serving.calendar()
    start = cal[max(0, cal.index(beg) - 5)]
    stop = cal[min(len(cal) - 1, cal.index(end) + MAX_DELAY + max(HOLDS) + 3)]
    t0 = __import__("time").time()
    frame = qlib.data.D.features(list(instruments), FIELDS, start_time=start, end_time=stop)
    out = {}
    for field in FIELDS:
        wide = frame[field].unstack("instrument")
        wide.index = pd.to_datetime(wide.index).strftime("%Y-%m-%d")
        out[field] = wide.sort_index()
    key = FIELDS[0]
    print(f"面板：请求 {len(instruments):,} 只 / 有数据 {out[key].shape[1]:,} 只 × "
          f"{len(out[key])} 个交易日（{start}→{stop}），"
          f"{__import__('time').time() - t0:.0f} s", flush=True)
    return out


def em_pct_pivot(days: list[str]) -> pd.DataFrame:
    """东财每票每日涨跌幅（百分数）宽表——除权日它说的是「对参考价」的真涨幅。"""
    frames = []
    for path in sorted(DAILY_DIR.glob("*.parquet")):
        day = path.stem
        if not (days[0] <= day <= days[-1]):
            continue
        df = pd.read_parquet(path, columns=["code", "pct_change"])
        df["trade_date"] = day
        frames.append(df)
    long = pd.concat(frames, ignore_index=True)
    long["instrument"] = long["code"].map(to_instrument)
    wide = long.pivot_table(index="trade_date", columns="instrument", values="pct_change",
                            aggfunc="last")
    return wide.reindex(days)


def adjusted_frames(raw: dict[str, pd.DataFrame], pct: pd.DataFrame) -> tuple[dict, dict]:
    """裸价 × 复权链：只在「东财涨幅与裸价涨幅差 >0.1pp」的那天乘一次修正系数。

    本机 qlib 面板是**未复权**的（实测：面板收盘价与 `data/stock_kline` 的裸价逐日相同、
    `$factor` 恒为 1.0）。直接拿面板价算前向收益会把除权日记成暴跌——
    实测 603061 在 2026-04-16 裸价 −26.28%、东财涨幅口径 +7.02%，
    这种格子落在十分位的尾巴上，足以凭空造出「资金流出后续跌」的假信号。
    封板判据也在这一套价上判：交易所的涨跌停是相对**除权后的参考价**算的，
    裸价空间里除权日会被误判成「全天封死跌停」（冒烟实测 8,692 格里 2 格）。

    顺带把**停牌冻结行**作废：`data/stock_kline` 在停牌那天会补一行 `volume=0`、
    收盘价照抄前一天的假行情（实测 2026-05-20 的 000608/002047/002629），
    不拦就会读成「那天平盘、还能正常买卖」；拦掉之后走顺延那条路。
    """
    rcl, rlc = raw["$close"], raw["$last_close"]
    traded = raw["$volume"].gt(0) if "$volume" in raw else pd.Series(True, index=rcl.index)
    raw_ret = rcl / rlc - 1.0
    em = (pct / 100.0).reindex(index=raw_ret.index, columns=raw_ret.columns)
    gap = (em - raw_ret).abs()
    fix = ((em + 1.0) / (raw_ret + 1.0)).where(gap > 0.001).fillna(1.0)
    cum = fix.cumprod()
    cov = em.notna().mean(axis=1)
    full_day = cov >= 0.9                              # 这天东财名单基本覆盖票池（半截天/存档早期不算）
    stats = {"除权修正格数": int((gap > 0.001).sum().sum()),
             "东财覆盖不足天数": int((~full_day).sum()),
             "覆盖不足的日子": [f"{d}({c:.0%})" for d, c in cov[~full_day].items()][:12],
             "覆盖足_但个股无东财行": int((raw_ret.notna() & em.isna() & full_day).sum().sum()),
             "停牌冻结格数": int((~traded & rcl.notna()).sum().sum())}
    adj = {k: raw[k].mul(cum, axis=0).where(traded) for k in ("$open", "$high", "$close")}
    return adj, stats


def forward_one(adj: dict[str, pd.DataFrame], day_pos: int, hold: int,
                pool_today: pd.Index) -> pd.DataFrame:
    """一个信号日、一个持有期 → 每票的入场价/出场价/可成交性记账（§八.4′）。

    全部算在**复权链**上（裸价 × 东财涨幅修正）：本机面板未复权、裸价的 `last_close`
    就是昨天那个 raw close，除权日会被读成「−26% 跌停」而交易所参考价其实是 +7%
    （实测 603061 2026-04-16）。判封板要相对参考价判，所以判据也走复权链，
    只是它和裸价是逐日等比的两套刻度、买卖侧的相对位置完全一致。
    """
    acl, aop, ahi = adj["$close"], adj["$open"], adj["$high"]
    ref = acl.ffill().shift(1)             # 参考价 = 上一**有成交**交易日收盘（停牌后复牌日也对）
    days = list(acl.index)
    idx = acl.columns
    e = day_pos + 1 + hold
    if e >= len(days):
        return pd.DataFrame()
    entry = aop.iloc[day_pos + 1].reindex(idx)
    states = entry_states(acl.iloc[day_pos].reindex(idx), aop.iloc[day_pos + 1].reindex(idx),
                          ahi.iloc[day_pos + 1].reindex(idx))
    prev_ref = ref.iloc[e].reindex(idx)
    sealed = (ahi.iloc[e].reindex(idx) / prev_ref - 1.0).le(-LIMIT_TOL).fillna(False)
    no_bar = acl.iloc[e].isna() | aop.iloc[e].isna() | ahi.iloc[e].isna()
    price = acl.iloc[e].reindex(idx).copy()
    delay = pd.Series(0, index=idx, dtype="int64")
    extra = pd.Series(0.0, index=idx)
    reason = pd.Series("normal", index=idx, dtype="object")
    reason[(~sealed & ~no_bar & (acl.iloc[e].reindex(idx) / prev_ref - 1.0)
            .le(-LIMIT_TOL).fillna(False))] = "closed_limit"
    for inst in idx[sealed | no_bar]:
        bars = [{"open": aop.iloc[e].get(inst), "high": ahi.iloc[e].get(inst),
                 "close": acl.iloc[e].get(inst)}]
        for k in range(1, MAX_DELAY + 1):
            if e + k >= len(days):
                break
            bars.append({"open": aop.iloc[e + k].get(inst), "high": ahi.iloc[e + k].get(inst),
                         "close": acl.iloc[e + k].get(inst)})
        plan = exit_plan(prev_ref.get(inst), bars)
        delay[inst] = plan["delay_days"]
        extra[inst] = plan["extra_drop"]
        reason[inst] = plan["reason"]
        if plan["reason"] == "truncated" and len(bars) < MAX_DELAY + 1:
            reason[inst] = "out_of_data"        # 数据末尾不够顺延，别记成「十个交易日卖不出」
        if plan["reason"] in ("deferred", "truncated"):
            k = min(plan["delay_days"], len(days) - 1 - e)
            price[inst] = aop.iloc[e + k].get(inst) if e + k < len(days) else np.nan
    out = pd.DataFrame({
        "instrument": idx, "trade_date": days[day_pos], "hold": hold,
        "entry": entry.to_numpy(), "exit_price": price.to_numpy(),
        "delay_days": delay.to_numpy(), "extra_drop": extra.to_numpy(),
        "reason": reason.to_numpy(), "entry_state": states.to_numpy(),
    })
    out["gross"] = out["exit_price"] / out["entry"] - 1.0
    out["net"] = out["gross"] - COST
    out["in_pool"] = out["instrument"].isin(pool_today)
    return out


def bucket_metrics(recs: pd.DataFrame, sig_col: str, main_days: set[str],
                   holdout_days: set[str]) -> tuple[dict, pd.DataFrame]:
    """当日截面十分桶 → 每桶逐日超额（减当日同池等权）→ 四条门槛里的 ①②③ 读数。"""
    d = recs[recs["ok"]].dropna(subset=[sig_col]).copy()
    if d.empty:
        return {}, pd.DataFrame()
    d["bucket"] = d.groupby("trade_date")[sig_col].transform(assign_deciles)
    bench = d.groupby("trade_date")["net"].transform("mean")
    d["excess"] = d["net"] - bench
    per_day = d.pivot_table(index="trade_date", columns="bucket", values="excess", aggfunc="mean")
    spread = per_day[10] - per_day[1] if 10 in per_day and 1 in per_day else pd.Series(dtype=float)
    means = per_day.mean()
    rho = float(means.reset_index(drop=True).corr(
        pd.Series(range(1, len(means) + 1), dtype=float), method="spearman"))
    in_main = spread[spread.index.isin(main_days)]
    in_hold = spread[spread.index.isin(holdout_days)]
    m = {"t_adj": adjusted_t(spread),
         "min_bucket_days": int(d.groupby("bucket").size().min()),
         "rho": rho,
         "d10_net_mean": float(per_day[10].mean()), "d1_net_mean": float(per_day[1].mean()),
         "spread_daily_mean": float(spread.mean()),
         "sign_main": float(np.sign(in_main.mean())) if len(in_main) else np.nan,
         "sign_holdout": float(np.sign(in_hold.mean())) if len(in_hold) else np.nan,
         "days": int(len(spread))}
    m["verdict"] = signal_pass(m)
    return m, per_day


def incremental_readings(recs: pd.DataFrame, sig_col: str, score: pd.Series) -> dict:
    """门槛④：同日按现榜分数分 5 档，**同档内**再比资金流顶/底（各取该档前后 25%）。"""
    d = recs[recs["ok"] & recs[sig_col].notna()].copy()
    d["score"] = score.reindex(pd.MultiIndex.from_arrays([d["trade_date"], d["instrument"]])).to_numpy()
    d = d.dropna(subset=["score"])
    daily: dict[str, list[float]] = {}
    for day, blk in d.groupby("trade_date"):
        diffs = []
        blk = blk.copy()
        blk["band"] = pd.qcut(blk["score"].rank(method="first"), 5, labels=False) + 1
        for _, band in blk.groupby("band"):
            if len(band) < 40:
                continue
            band = band.sort_values(sig_col)
            k = max(5, int(len(band) * 0.25))
            diffs.append(band["net"].iloc[-k:].mean() - band["net"].iloc[:k].mean())
        if diffs:
            daily[day] = float(np.mean(diffs))
    series = pd.Series(daily, dtype=float).sort_index()
    if series.empty:
        return {"t_adj": np.nan, "t_out_of_sample": np.nan, "days": 0, "oos_days": 0,
                "mean_spread": np.nan}
    oos = series[series.index >= OOS_SCORE]
    return {"t_adj": adjusted_t(series), "t_out_of_sample": adjusted_t(oos),
            "days": int(len(series)), "oos_days": int(len(oos)),
            "mean_spread": float(series.mean())}


def attach_scores(pool_insts: list[str], beg: str, end: str) -> tuple[pd.Series, dict]:
    """落盘模型对 [beg, end] 每票的分数：直接调 `serving.score_frame`，与生产名单同一条取数路径。"""
    from stock_selector_qlib import artifacts, serving

    art = artifacts.load()
    cal = serving.calendar()
    warm_beg = cal[max(0, cal.index(beg) - serving.WARMUP_DAYS + 1)]
    pred, stats = serving.score_frame(art, warm_beg, end, pool=pool_insts, chunk=500)
    idx = pred.index
    pred.index = pd.MultiIndex.from_arrays(
        [idx.get_level_values(0).strftime("%Y-%m-%d"), idx.get_level_values(1)])
    stats["run_id"] = art.run_id
    stats["窗口"] = f"{warm_beg}→{end}"
    return pred.sort_index(), stats


def report_block(title: str, rows) -> None:
    print(f"\n=== {title} ===")
    items = rows.items() if isinstance(rows, dict) else rows
    for k, v in items:
        if isinstance(v, float):
            print(f"  {k:<34} {v:,.6g}")
        elif isinstance(v, int):
            print(f"  {k:<34} {v:,}")
        else:
            print(f"  {k:<34} {v}")


def arm_5m(pool: pd.DataFrame, out_dir: Path) -> dict:
    """5m 副臂：T 日尾盘最后 4 根的量加权方向，与当日 `ratio_1d` 的截面 Spearman。

    地位同 §29 的臂 B：**只给方向，不进 §八.5 的四条门槛**。
    代理口径：`(Σ涨bar量 − Σ跌bar量) ÷ Σ尾盘量`（5m 没有分档净额，只有价与量）。
    """
    from collections import defaultdict

    from pymongo import MongoClient

    from stock_selector.datasource.mongo_source import _load_mongo_config

    cfg = _load_mongo_config()
    client = MongoClient(host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 27017)),
                         serverSelectionTimeoutMS=5000)
    db = client[cfg.get("database", "eastmoney_news")]
    days = sorted(set(pool["trade_date"]))
    beg, end = days[0], days[-1]
    codes = sorted({inst[2:] for inst in pool["instrument"].unique()})
    per_day: dict[str, dict[str, float]] = defaultdict(dict)
    t0 = __import__("time").time()
    for n, code in enumerate(codes, 1):
        cur = db["stock_kline_5m"].find(
            {"code": code, "date": {"$gte": f"{beg} 00:00", "$lte": f"{end} 23:59"}},
            {"_id": 0, "date": 1, "open": 1, "close": 1, "volume": 1}).sort("date", 1)
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in cur:
            by_day[row["date"][:10]].append(row)
        inst = ("SH" if code.startswith(("6", "9")) else "SZ") + code
        for day, bars in by_day.items():
            tail = bars[-4:]
            if len(tail) < 4:
                continue
            vol = sum(b.get("volume") or 0 for b in tail)
            if vol <= 0:
                continue
            signed = sum((b["close"] - b["open"]) / b["open"] * (b.get("volume") or 0)
                         for b in tail if b.get("open"))
            per_day[day][inst] = signed / vol
        if n % 400 == 0:
            print(f"  [5m] {n:,} 只，累计 {len(per_day)} 天，{__import__('time').time() - t0:.0f} s",
                  flush=True)
    client.close()
    frame = pd.DataFrame(per_day).T.stack().rename("tail_proxy").reset_index()
    frame.columns = ["trade_date", "instrument", "tail_proxy"]
    joined = frame.merge(pool[["trade_date", "instrument", "ratio_1d"]],
                         on=["trade_date", "instrument"]).dropna()
    rhos = []
    for day, blk in joined.groupby("trade_date"):
        if len(blk) < 200:
            continue
        rhos.append({"trade_date": day, "n": len(blk),
                     "rho": blk["tail_proxy"].corr(blk["ratio_1d"], method="spearman"),
                     "disagree": float(((blk["tail_proxy"] > 0) != (blk["ratio_1d"] > 0)).mean())})
    out = pd.DataFrame(rhos)
    if not out.empty:
        out.to_csv(out_dir / "ff_signal_read_5m_arm.csv", index=False)
    return {"days_covered": int(len(out)), "codes_asked": int(len(codes)),
            "rho_mean": float(out["rho"].mean()) if not out.empty else np.nan,
            "rho_positive_share": float((out["rho"] > 0).mean()) if not out.empty else np.nan,
            "disagree_share": float(out["disagree"].mean()) if not out.empty else np.nan}


def raw_panels(insts: list[str], days: list[str]) -> dict[str, pd.DataFrame]:
    """未复权日线宽表（`data/stock_kline/`）：合成复权链的原料。

    面板里的 `$factor` 在本机恒为 1.0（实测 2026-04-01→10-27 三只票全是 1），
    面板收盘价与这里的裸价逐日相同 ⇒ 面板没复权，除权日会显示成「暴跌」。
    """
    want = set(days)
    frames = []
    for inst in insts:
        code = inst[2:]
        path = RAW_DIR / f"{code}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["date", "open", "high", "close", "last_close", "volume"])
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        df = df[df["date"].isin(want)]
        if df.empty:
            continue
        df["instrument"] = inst
        frames.append(df)
    if not frames:
        raise RuntimeError(f"{RAW_DIR} 里读不到任何票的日线，封板判据没法算")
    long = pd.concat(frames, ignore_index=True)
    out = {}
    for field in ("open", "high", "close", "last_close", "volume"):
        wide = long.pivot(index="date", columns="instrument", values=field)
        out[f"${field}"] = wide.reindex(index=days, columns=insts)
    return out


def cross_check(pool: pd.DataFrame, adj: dict[str, pd.DataFrame],
                raw: dict[str, pd.DataFrame], pct: pd.DataFrame) -> dict:
    """§八.4′ 的两处核对：裸价涨幅 vs 东财 `pct_change`；复权链修完之后还剩多少分歧。"""
    def stack(frame, name):
        got = frame.stack().to_frame(name).reset_index()
        got.columns = ["trade_date", "instrument", name]
        return got

    raw_ret = raw["$close"] / raw["$last_close"] - 1.0
    adj_ret = adj["$close"] / adj["$close"].shift(1) - 1.0
    em = pct / 100.0
    joined = (pool.merge(stack(raw_ret, "raw_ret"), on=["trade_date", "instrument"], how="inner")
                  .merge(stack(adj_ret, "adj_ret"), on=["trade_date", "instrument"], how="inner")
                  .merge(stack(em, "em_ret"), on=["trade_date", "instrument"], how="inner"))
    gap_raw = (joined["raw_ret"] - joined["em_ret"]).abs()
    gap_adj = (joined["adj_ret"] - joined["em_ret"]).abs()
    hit = joined["em_ret"].notna()
    return {
        "compared": int(hit.sum()),
        "裸价_vs_东财_gt0p5pp": int((gap_raw > 0.005)[hit].sum()),
        "复权链后_vs_东财_gt0p5pp": int((gap_adj > 0.005)[hit].sum()),
        "涨停判定分歧": int(((joined["raw_ret"] >= LIMIT_TOL) != (joined["em_ret"] >= LIMIT_TOL))[hit].sum()),
        "跌停判定分歧": int(((joined["raw_ret"] <= -LIMIT_TOL) != (joined["em_ret"] <= -LIMIT_TOL))[hit].sum()),
        "复权后涨停分歧": int(((joined["adj_ret"] >= LIMIT_TOL) != (joined["em_ret"] >= LIMIT_TOL))[hit].sum()),
        "复权后跌停分歧": int(((joined["adj_ret"] <= -LIMIT_TOL) != (joined["em_ret"] <= -LIMIT_TOL))[hit].sum()),
        "除权样本_抽 10 条": joined.loc[gap_raw > 0.005,
                                     ["trade_date", "code", "raw_ret", "adj_ret", "em_ret"]] \
            .head(10).round(5).to_dict("records"),
    }


def control_block(main_pool: pd.DataFrame, out_dir: Path) -> dict:
    """§八.1 的满票池对照段（10-20~10-27）：只答「池子换成今天这套 5,925 只的名单，信号侧变不变」。

    这几天**出不了前向收益**：h=5 的出场要到 11-04、h=10 要到 11-11。
    所以这一栏只比池子构成与 `ratio_1d` 的分布，是描述性的，不进 §八.5 任何门槛。
    """
    days = stored_days(CONTROL[0], CONTROL[1])
    if not days:
        return {"对照日": "无（10-20 之后还没有满票池分区）"}
    crows, cpicked = load_rows(days)
    csig = signal_frame(crows)
    base = crows[crows["valid"].astype(bool)]
    base = base[base["code"].map(board_ok)]
    with_bj = crows[(crows["valid"].astype(bool))
                    & crows["code"].str.startswith(("43", "81", "83", "87", "92"))]
    ctl = base[base["trade_date"].isin(days)][["trade_date", "code", "instrument"]].merge(
        csig, on=["trade_date", "code"], how="left")
    old = set(main_pool["code"])
    new = ctl[~ctl["code"].isin(old)]
    per_day = ctl.groupby("trade_date").size()

    def dist(s: pd.Series) -> str:
        q = s.quantile([0.1, 0.5, 0.9])
        return (f"n={int(s.notna().sum()):,} 均值={s.mean():,.5f} "
                f"p10={q[0.1]:,.4f} p50={q[0.5]:,.4f} p90={q[0.9]:,.4f}")

    ctl.to_csv(out_dir / "ff_signal_read_control.csv", index=False)
    return {
        "对照日": f"{days[0]}→{days[-1]}（{len(days)} 天，读 {len(cpicked)} 个分区）",
        "对照段每天主板池": f"{per_day.mean():,.0f}（主窗 {main_pool.groupby('trade_date').size().mean():,.0f}）",
        "今天名单里的北交所_已按板块口径剔掉": int(len(with_bj)),
        "对照段里主窗从没出现过的票": int(new["code"].nunique()),
        "新票占比": round(float(new["code"].nunique()) / max(1, ctl["code"].nunique()), 4),
        "ratio_1d 对照段": dist(ctl["ratio_1d"]),
        "ratio_1d 主窗": dist(main_pool["ratio_1d"]),
        "ratio_1d 只有新票": dist(new["ratio_1d"]),
        "ratio_3d 在对照段的可用率": round(float(ctl["ratio_3d"].notna().mean()), 4),
        "为什么不出收益": "h=5 出场要到 2026-11-04、h=10 要到 2026-11-11，现在一天都还没到",
    }


def main(argv: list[str] | None = None) -> int:
    import time

    from stock_selector_qlib import guardrails

    ap = argparse.ArgumentParser(description="FF-1 预注册读数（只读）")
    ap.add_argument("--smoke", action="store_true", help="只取 4 个信号日，跳过打分与 5m 副臂")
    ap.add_argument("--skip-scores", action="store_true")
    ap.add_argument("--skip-5m", action="store_true")
    ap.add_argument("--out", default="/tmp")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    t_start = time.time()

    counts = pool_counts(MAIN[0], HOLDOUT[1])
    holes = set(hole_days(counts))
    signal_days = [d for d in stored_days(MAIN[0], HOLDOUT[1]) if d not in holes]
    if args.smoke:
        signal_days = signal_days[:4]
    main_set = {d for d in signal_days if d <= MAIN[1]}
    hold_set = {d for d in signal_days if HOLDOUT[0] <= d <= HOLDOUT[1]}
    full_pool = {d for d in signal_days if counts.get(d, 0) >= 0.95 * counts.max()}
    report_block("窗口", {"signal_days": len(signal_days), "main_days": len(main_set),
                         "holdout_days": len(hold_set), "excluded_half_days": len(HALF_DAYS),
                         "采集洞剔除": sorted(holes) or "无",
                         "票池完整段天数(≥95%峰值)": len(full_pool)})

    rows, picked = load_rows(signal_days, skip=holes)
    report_block("资金流输入", {"partitions_read": len(picked), "rows": len(rows),
                                "selfcheck_rows": selfcheck_future_invariance(
                                    rows, signal_frame(rows),
                                    signal_days[len(signal_days) // 2]),
                                **selfcheck_data_ts(rows)})
    sig = signal_frame(rows)

    base = rows[rows["valid"].astype(bool)]
    base = base[base["code"].map(board_ok)]
    pool = base[base["trade_date"].isin(signal_days)][
        ["trade_date", "code", "instrument", "name", "pct_change", "close"]
    ].merge(sig, on=["trade_date", "code"], how="left")
    insts = sorted(pool["instrument"].unique())
    per_day = pool.groupby("trade_date").size()
    report_block("票池", {"valid_and_board_ok": len(pool), "unique_instruments": len(insts),
                         "per_day_mean": round(float(per_day.mean()), 1),
                         "per_day_min": int(per_day.min()), "per_day_max": int(per_day.max())})
    report_block("满票池对照段（§八.1 末行：只比信号侧，不进裁决）", control_block(pool, out_dir))

    pan = qlib_panels(insts, signal_days[0], signal_days[-1])
    days = list(pan["$amount"].index)
    raw = raw_panels(insts, days)
    print(f"未复权日线：{len(raw['$close'].columns):,} 只 × {len(raw['$close'])} 天", flush=True)
    pct = em_pct_pivot(days)
    adj, adj_stats = adjusted_frames(raw, pct)
    report_block("复权链（面板未复权 ⇒ 收益与封板判据都走东财涨幅合成的复权价）", adj_stats)
    thin = guardrails.thin_liquidity(pan["$amount"])
    thin_long = thin.stack().rename("thin").to_frame().reset_index()
    thin_long.columns = ["trade_date", "instrument", "thin"]

    day_pos = {d: i for i, d in enumerate(days)}
    frames = []
    missing_panel = 0
    for day in signal_days:
        if day not in day_pos:
            missing_panel += 1
            continue
        today = pool.loc[pool["trade_date"] == day, "instrument"]
        for hold in HOLDS:
            block = forward_one(adj, day_pos[day], hold, set(today))
            if not block.empty:
                frames.append(block)
    recs = pd.concat(frames, ignore_index=True).merge(
        pool[["trade_date", "instrument", "ratio_1d", "ratio_3d"]],
        on=["trade_date", "instrument"], how="left")
    recs = recs.merge(thin_long, on=["trade_date", "instrument"], how="left")
    recs["thin"] = recs["thin"].fillna(False).astype(bool)
    recs["ok"] = (recs["in_pool"] & ~recs["thin"]
                  & recs["entry_state"].isin(["tradable", "opened_then_sealed"])
                  & recs["net"].notna())
    counts = recs[recs["in_pool"]].groupby(["hold", "entry_state"]).size().unstack(fill_value=0)
    report_block("可成交性（买入侧，票池内每持有期计数）",
                 {"signal_days_without_panel_row": missing_panel,
                  **{f"state_{c}_h{h}": int(counts.loc[h, c]) for h in counts.index for c in counts.columns}})
    exits = recs[recs["ok"]].groupby(["hold", "reason"]).agg(
        n=("reason", "size"), delay=("delay_days", "mean"), extra=("extra_drop", "mean"))
    report_block("可成交性（卖出侧）", {f"{r}_h{h}": f"n={int(v.n):,} 顺延均值={v.delay:.3f} "
                                    f"多吃跌幅={v.extra:,.5f}" for (h, r), v in exits.iterrows()})
    sealed_later = recs[recs["ok"] & (recs["entry_state"] == "opened_then_sealed")]
    report_block("开盘未封、盘中才封（不剔，单独报）",
                 {"只·日": len(sealed_later),
                  "占可建仓比例": round(len(sealed_later) / max(1, int(recs["ok"].sum())), 4),
                  "该组净收益 − 同池均值": float(sealed_later["net"].mean()
                                                   - recs[recs["ok"]]["net"].mean())})
    report_block("面板涨幅 vs 东财 pct_change 交叉核对", cross_check(pool, adj, raw, pct))
    if not args.skip_5m:
        m5 = pool[(pool["trade_date"] >= M5[0]) & (pool["trade_date"] <= M5[1])]
        report_block("5m 副臂（只给方向，不进 §八.5 门槛）", arm_5m(m5, out_dir))

    per_signal: dict[str, str] = {}
    results: dict[tuple[str, int], dict] = {}
    buckets_out = []
    for col in ("ratio_1d", "ratio_3d"):
        for hold in HOLDS:
            m, per_day = bucket_metrics(recs[recs["hold"] == hold], col, main_set, hold_set)
            results[(col, hold)] = m
            if not per_day.empty:
                wide = per_day.copy()
                wide.columns = [f"{col}_h{hold}_D{c}" for c in wide.columns]
                buckets_out.append(wide)
    if buckets_out:
        pd.concat(buckets_out, axis=1).to_csv(out_dir / "ff_signal_read_buckets.csv")
    for (col, hold), m in results.items():
        report_block(f"{col} × 持有 {hold} 日", list(m.items()))

    # 票池口径子读数（不进门槛）：只用「池子达到峰值 95%」的那些日子重算一次
    sub = recs[recs["trade_date"].isin(full_pool)]
    sub_main = {d for d in full_pool if d <= MAIN[1]}
    sub_hold = {d for d in full_pool if d >= HOLDOUT[0]}
    sub_rows = []
    for col in ("ratio_1d", "ratio_3d"):
        for hold in HOLDS:
            m, _ = bucket_metrics(sub[sub["hold"] == hold], col, sub_main, sub_hold)
            sub_rows.append((f"{col}_h{hold}", f"t_adj={m.get('t_adj', float('nan')):,.3f} "
                               f"rho={m.get('rho', float('nan')):,.3f} "
                               f"桶最小={m.get('min_bucket_days', 0):,} 天数={m.get('days', 0):,}"))
    report_block("票池口径子读数（只用池子完整段，描述性、不参与裁决）", sub_rows)

    incremental_ok = False
    if not args.skip_scores:
        t0 = time.time()
        score, sstats = attach_scores(insts, signal_days[0], signal_days[-1])
        report_block("现榜分数（门槛④）", {**sstats, "秒": int(time.time() - t0)})
        gate4: list[dict] = []
        for col in ("ratio_1d", "ratio_3d"):
            for hold in HOLDS:
                inc = incremental_readings(recs[recs["hold"] == hold], col, score)
                report_block(f"门槛④ {col} × {hold} 日", list(inc.items()))
                inc["signal"], inc["hold"] = col, hold
                pd.DataFrame([inc]).to_csv(
                    out_dir / f"ff_signal_read_incremental_{col}_h{hold}.csv", index=False)
                if hold == 10:
                    gate4.append(inc)
        headline = [results[("ratio_1d", 10)]["verdict"], results[("ratio_3d", 10)]["verdict"]]
        incremental_ok = all(incremental_gate(g["t_adj"], g["t_out_of_sample"]) for g in gate4)
    else:
        headline = [results[("ratio_1d", 10)]["verdict"], results[("ratio_3d", 10)]["verdict"]]

    per_signal = dict(zip(("ratio_1d", "ratio_3d"), headline))
    flagged = recs[(~recs["ok"] & recs["in_pool"]) | (recs["reason"] != "normal")]
    flagged.to_csv(out_dir / "ff_signal_read_flagged.csv", index=False)
    report_block("裁决（持有 10 日，门槛见 §八.5）",
                 {"两个信号": per_signal, "门槛④": incremental_ok,
                  "结论": overall(per_signal, incremental_ok) if not args.skip_scores
                          else f"（跳过④）{overall(per_signal, False)}",
                  "用时秒": int(time.time() - t_start)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
