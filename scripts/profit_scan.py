"""profit_scan.py — 把基础条件铺开成「买入条件 × 卖法」的逐笔盈亏，结果进 Mongo 供后台查。

为什么这么算（2026-10-16，用户定的验收口径）：以前那套判据读的是「这个状态后来会不会跌」，
跟「照着它买卖一笔能不能赚钱」是两回事。这次尺子换成**每笔扣完成本赚多少个百分点**，
四条及格线写死在 `rule_gates.evaluate_trade`：≥300 笔、胜率>50%、盈亏比>1、每笔扣费后>0，
再加一条按尝试次数自动调严的显著性（试 6 万次和试 6 百次，及格线不该一样高）。

成交口径（每一条都会改变数字，所以写清楚）：
- **信号日收盘算、次日开盘买**：收盘后才知道信号，当天买是回测白拿的进场优势。
  次日开盘就顶到涨停、或一字涨停板 ⇒ **买不进**，这笔不算；次日停牌没行情也算买不到。
- 持有 H 个交易日，中途碰止盈/止损提前走：**同一天两条都碰按止损优先**（按止盈算会白捡差价）。
  跳空穿越按**开盘价**成交（`min(止损价, 开盘)`、`max(止盈价, 开盘)`），到期按收盘走。
  买入当天**不能卖**（A 股 T+1），监控从买入日的次日起；「持有第几天」一律从买入日往后数，
  第 1 天 = 买入次日，到期 = 第 H 天收盘。
- 一字板（最高=最低）当天**没有对手盘** ⇒ 卖不掉，顺移到后面第一个有对手盘的交易日按开盘卖，
  最多顺移 `REPAIR_MAX` 天；还出不掉就记「没成交」丢掉（`n_no_fill` = 买不到 + 出不掉的合计）。
- **冷却**：同一条件在同一只票的前 H 个交易日里**成立过**就不再买（锚在「上一次响」而不是「上一次买」，
  这样判定与当天买不买得进无关，不会因停牌/涨停把买点整体挪位）。A 股 T+1 + 手上只有几只仓，
  连响十天天天“买入”是同一次行情、不是十笔独立交易；不冷却笔数会虚高好几倍、均值趋近市场平均。
- 成本：每笔扣 `COST_PP` = 0.30pp（佣金万 2.5 双边 + 印花税 0.05% 卖出 + 过户费 ≈0.10pp，
  加预估滑点 0.20pp）。口径按 `--amount 20000` 元/笔；单笔金额更小要再摊 5 元最低佣金。
- 显著性按**交易日聚类**算 t（每天先算当日均值，再对这些日均值做单样本 t）：同一天几百只票一起响，
  它们高度相关，按笔算 t 会把样本量虚报几十倍。`t_trade`（按笔）只作对照，不用来判。
- 网格一律存 **float64**。存 float32 时 976 笔里有 5 笔的触发日/止损判定会翻
  （`kdj_k < kdj_d` 这种临界比较、以及最低价与止损价相等的情况），一翻就顺着冷却期把后面几笔挪位；
  每块才 200 只票，float64 多占的几十 MB 换掉这个尾巴值得。
- `bench_net_pp` = 同一段窗口里**票池内全体时点**按同一种卖法的扣后均值（市场本身赚多少），
  `excess_pp` 才是这条条件比基准多赚的部分——只看绝对值容易把牛市当成能力。
  基准与买点共用同一张票池掩码，否则「超额」是拿池子外的时点比的。

票池（2026-10-17 加的，因为上一轮全量的利润全部来自 2016-2017 的次新/创业板追涨）：
- 代码段不是 60/68/00/30 的**场内基金**（15/51/52/53/55/56/58 打头，共 1,445 只）在读盘前就剔掉；
- 信号日这只票在本库里至少已有 `--min-tenure`（默认 250）根日线，逐格生效不是整只票剔除；
- 可选 `--min-amount`：信号日前 20 个交易日日均成交额下限（元），把「冷门到买不进卖不出」挡住。
门槛剔了多少格子写进 run doc（`pool.cells_live / cells_ok`），页面据此报出被剔掉的比例。

**及格线按累计试次抬，不按每轮重置**：`logs/profit_scan/trials.jsonl` 记每一轮跑了多少组合
（`--dry-run` 也记，它同样看过这份数据），跑之前把 Mongo 历史 run 里没记过的 `n_trials` 一起加上。
`--trial-budget`（默认 30 万次）用满就拒绝再扫——反复扫同一批行情总能凑出一条「过」的，
那不是规则，是掷骰子。

数据只看留出段之前：止于 2021-12-31，且**末尾 `max(H)+2` 个交易日不做买点**
（卖出要落在窗口内，跨过留出段起点的数据不许用来选规则）。2022 之后是留出段，
只在过筛之后用 `--from 2022-01-01` 读它一次。

**候选批次可以从文件读**（`--inventory-file`）：合取判据那批由
`scripts/conjunction_candidates.py` 预注册到 `config/rule_candidates_conj.json`，不能寄存在
Mongo 的 `profit_scan_base_conditions` 里 —— `base_conditions.py` 每次入库前先 `delete_many({})`，
批次会随时被清掉。批次文件必须整批跑：`--limit-conditions` 遇到批次文件直接报错
（从 60 条里挑 10 条看，挑的动作本身就回到了数据里，预注册就没意义了）。

**数据深度是个硬约束**（2026-09-28 那轮说的「2006→2021」是错的，10-17 全池实测）：
`data/stock_kline/*.parquet` 是分批滚动重建的，5,186 只股票票深浅不一（92~2,637 行、中位 1,827 行），
其中 1,641 只的第一根日线在 2015 年、1,019 只在 2016 年——那是重建深度，不是上市日期。
另有 622 只在 2015-06→2021-12 里有一个 >120 天的洞（一部分是真停牌，一部分是那段没补上），
904 只 2023-2025 三年一行都没有、直接跳到 2026。
留出段前的可用样本因此比「16 年 × 全市场」小得多，三段时代切分也只是这 6 年里的三段。
`--min-tenure` 量的就是这个库里有日线的根数（不是真实上市日期），老票被截断的历史会让它
在窗口头一年显得像次新——所以门槛剔了多少要报出来，别藏在平均数里。

算法上一个不显然的点：卖出模拟与「买入条件」无关，所以每块票对每种持有天数只需在
**整张（交易日 × 票）网格**上跑一遍 16 个卖法，条件那一侧只是去网格上按自己的触发点做减法/查表。
按条件逐笔模拟会贵一个数量级（972 条条件的触发点合起来是全表的 20 多倍）。

产物三张表（后台管理端直接查，不用等文件）：`profit_scan_runs`（一次跑一行：窗口/规模/耗时）、
`profit_scan_rows`（一个「条件 × 卖法」一行：笔数/胜率/盈亏比/扣后每笔/t/三段/判定/归因 why）、
`profit_scan_base_conditions`（清单，由 `scripts/base_conditions.py` 生成）。

用法:
    python3 scripts/profit_scan.py --limit-conditions 10 --step 20 --dry-run   # 试水：先看口径
    python3 scripts/profit_scan.py --limit-conditions 10 --step 20 --explain 3 --dump-trades 3
    python3 scripts/profit_scan.py                                             # 全量 6.2 万组合
    python3 scripts/profit_scan.py --min-tenure 500 --min-amount 3e7          # 再收紧票池
    python3 scripts/profit_scan.py --from 2022-01-01 --trial-budget 0         # 留出段只验一次
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from base_conditions import COLLECTION as BASE_COLL, HOLDOUT_START  # noqa: E402
from ruleset_posterior import scan_universe, shift_fwd  # noqa: E402
from stock_selector.backtest import executability, rule_expr, rule_gates  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402
from stock_selector.features.base_columns import board_limit_threshold  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
LOG_DIR = ROOT / "logs" / "profit_scan"
RUNS_COLL = "profit_scan_runs"
ROWS_COLL = "profit_scan_rows"

DATE_FROM = "2015-06-01"           # 见上面「数据深度」：这是实际能用的起点，不是想要的
HOLDS = [3, 5, 10, 20]
TAKES = [None, 0.06, 0.12, 0.20]
STOPS = [None, -0.04, -0.08, -0.12]
COST_PP = 0.30
REPAIR_MAX = executability.REPAIR_MAX_DAYS   # 一字板卖不掉最多顺移几天（判据真源见 backtest/executability.py）
BLOCK = 80                       # 每块几只票：内存随它走（列 + 掩码位图 + 16 张网格）。
# 只改分块，不改判定：一只票只落在一个块里，块内是完整的日线，跨块只做标量/数组累加。
# 2026-10-17 从 200 降到 80：那天 BLOCK=200 的全量扫把 MemAvailable 压穿，earlyoom 在
# 10:19/10:26 连着杀掉 IDE 进程——扫描是自己的事，不该拿整台机器换。
CURVE_K = [1, 2, 3, 5, 10, 20]   # 归因：收益落在持有第几天
BOOT_N = 20                      # 归因：按月块重抽次数
TRADING_DAYS = 245
TRIAL_LEDGER = LOG_DIR / "trials.jsonl"
FULL_STOCKS = 6300                 # 全量跑大约多少只票入格（6631 个文件剔 ST/短史）
BASE_FIELDS = ["open", "high", "low", "close", "pct", "amount"]

# 票池门槛。10-17 那轮全量扫出 315 条「过」，拆开看利润全部来自 2016-2017 的次新/创业板
# 追涨（上市 ≤41 个交易日的格子），成熟票在每一条过筛条件下都是负的；池子里还混着
# 1,445 只场内基金（15/51/52/53/55/56/58 打头）。所以先把「买的是股票、而且散户
# 真买得到」摆正，再谈规则好不好——不然调出来的判据是在调次新股博弈那一段行情。
AMOUNT_WINDOW = 20                        # 流动性按买入信号日前 20 个交易日的日均成交额判
DEFAULT_MIN_TENURE = 250                  # 上市满一年（约 250 个交易日）才算进池
POOL = {"min_tenure": DEFAULT_MIN_TENURE, "min_amount": 0.0}   # main() 按命令行覆盖

logger = logging.getLogger("profit_scan")


def filter_funds(files: list[Path]) -> tuple[list[Path], int]:
    """按代码段剔掉场内基金与北交所（文件名就是代码，不用读盘）。返回 (留下的, 剔掉的只数)。

    代码段真源在 `conviction/bucket.is_stock_code`——动作模型的样本要用**同一个池子**，
    不然两边的「超额」比的不是同一批票。
    """
    keep = [f for f in files if bucket.is_stock_code(f.stem)]
    return keep, len(files) - len(keep)


def trailing_mean(col: np.ndarray, days: int) -> np.ndarray:
    """近 `days` 行（含当天）的均值，NaN 当缺数跳过；整窗没有有效值 → NaN（门槛判不过）。

    amount 有爬虫留下的 null（见 10-15 那条修复），所以不能把 NaN 当 0，否则一只票会因为
    缺数被误判成「没流动性」而踢出池子。
    """
    v = np.where(np.isfinite(col), col, 0.0)
    n = np.isfinite(col).astype(np.float64)
    cs_v, cs_n = np.cumsum(v, axis=0), np.cumsum(n, axis=0)
    pad = np.zeros((days, col.shape[1]))
    s = cs_v - np.vstack([pad, cs_v[:-days]])
    c = cs_n - np.vstack([pad, cs_n[:-days]])
    return np.where(c > 0, s / np.maximum(c, 1), np.nan)


def profiles() -> list[dict]:
    """64 种卖法 = 持有天数 × 止盈档 × 止损档。它与买入条件无关，所以整张表只算一次。"""
    out = []
    for h in HOLDS:
        for tp in TAKES:
            for sl in STOPS:
                tag = (f"H{h}" + ("TP-" if tp is None else f"TP{int(tp * 100)}")
                       + ("_SL-" if sl is None else f"SL{abs(int(sl * 100))}"))
                out.append({"tag": tag, "hold": h, "tp": tp, "sl": sl})
    return out


# ------------------------------------------------------------------ 数据块与窗口

def needed_fields(conds: list[dict]) -> list[str]:
    fs = {f for c in conds for f in (c.get("fields") or rule_expr.validate(c["expr"]))}
    return BASE_FIELDS + sorted(fs - set(BASE_FIELDS))


class Block:
    """一块票的日线网格（行=交易日、列=票），缺行留 NaN（停牌）。"""

    def __init__(self, files: list[Path], fields: list[str], axis: np.ndarray,
                 axis_dates: list[str]):
        n_days = len(axis_dates)
        mat = {f: np.full((n_days, len(files)), np.nan, dtype=np.float64) for f in fields}
        codes, kept = [], 0
        for path in files:
            try:
                df = pd.read_parquet(path, columns=["code", "name", "date", *fields])
            except Exception as exc:
                logger.debug("跳过 %s：%s", path.name, exc)
                continue
            if len(df) < 60 or bucket.is_st_name(str(df["name"].iloc[-1] or "")):
                continue
            dts = pd.to_datetime(df["date"]).to_numpy()
            rows = np.searchsorted(axis, dts)
            # 只认「这一天确实在轴上」的行：留出段之后的行被 window 截掉了，
            # 而 searchsorted 对不在轴上的日期会给插入位置，直接写会串位
            ok_rows = (rows < n_days) & (axis[np.minimum(rows, n_days - 1)] == dts)
            if not ok_rows.all():
                df = df.loc[ok_rows]
                rows = rows[ok_rows]
            if len(df) < 60:
                continue
            for f in fields:
                mat[f][rows, kept] = df[f].to_numpy(dtype=np.float64)
            codes.append(str(df["code"].iloc[-1]))
            kept += 1
            del df
        self.cols = {f: mat[f][:, :kept] for f in fields}
        self.n_days, self.codes = n_days, codes
        self.n_stock = kept
        # 涨停线是板块属性，按票存一行百分数，网格比较时直接广播
        self.limit = (np.array([board_limit_threshold(c) for c in codes], dtype=np.float64)
                      if codes else np.zeros(0, dtype=np.float64))[None, :]
        self.ok = self._pool_mask()

    def _pool_mask(self) -> np.ndarray:
        """可下单格子：有日线 + 本库里它已有 `min_tenure` 根日线 + 近 20 日日均成交额过线。

        门槛是**逐格**而不是整只票剔除：一只次新股在第 251 根日线之前不该下单、之后就该，
        前后两段本来就在这张网格里。买点掩码、基准（bench）、下单分母共用这一张，
        「比基准超额」才是同一个池子的对比，不是拿池子外的时点当基准。

        量的是「本库里有日线的根数」而不是真实上市日期：一部分老票的重建深度只到 2015 年，
        窗口头一年会被当成新票剔掉——那是数据深度的问题不是判据的问题，所以门槛剔了多少格子
        要报进 run doc，别让它藏在平均数里。
        """
        close = self.cols["close"]
        ok = np.isfinite(close)
        if POOL["min_tenure"] > 0:
            ok &= np.cumsum(ok, axis=0) >= POOL["min_tenure"]
        if POOL["min_amount"] > 0:
            ok &= trailing_mean(self.cols["amount"], AMOUNT_WINDOW) >= POOL["min_amount"]
        return ok


def window(files: list[Path], fields: list[str], date_from: str, date_to: str
           ) -> tuple[np.ndarray, list[str], int, int]:
    """日期轴（截到 date_to）+ 可做买点的行区间 [day_lo, day_hi)。"""
    axis_all, _ = scan_universe(files, fields)
    cut = int(np.searchsorted(axis_all, date_to)) + 1
    day_lo = int(np.searchsorted(axis_all, date_from))
    day_hi = max(day_lo + 1, cut - (max(HOLDS) + 2))
    return np.array(axis_all[:cut], dtype="datetime64[ns]"), axis_all[:cut], day_lo, day_hi


# ------------------------------------------------------------------ 成交模拟（整张网格）

def entry_gate(blk: Block) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """次一交易日开盘买入：返回 (买入价网格, 买得进的网格, 次日有行情的网格)。

    「贴着涨停线就买不进」的容差与组合回测同源（`executability.LIMIT_TOL_PP`）。
    原来还有一条「开盘一字涨停且当日涨幅也到线」才拦——一字当天 open==close，
    跳空与 `pct` 同一个分母、同一个值，那条必然已被这条覆盖，所以合掉（口径不变）。
    """
    o, c = blk.cols["open"], blk.cols["close"]
    e = shift_fwd(o, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        gap_pp = (e / c - 1.0) * 100.0
    have = np.isfinite(e) & (e > 0) & np.isfinite(c) & (c > 0) & np.isfinite(gap_pp)
    can = have & (gap_pp < blk.limit - executability.LIMIT_TOL_PP)
    return np.where(have, e, np.nan), can, have


def simulate(blk: Block, entry_px: np.ndarray, can_buy: np.ndarray, plist: list[dict],
             with_day: bool = False) -> dict[str, tuple[np.ndarray, ...]]:
    """对整张网格跑这几种卖法的退出：{tag: (毛收益pp, 是否成交[, 买入后第几天走])}。

    天序全部**从买入日往后数**：买入日是第 0 天（网格行 = 信号日 +1），
    A 股 T+1 ⇒ 最早第 1 天卖，持有 H 天就是第 H 天收盘走。
    """
    h, l, o, c = blk.cols["high"], blk.cols["low"], blk.cols["open"], blk.cols["close"]
    ok0 = np.isfinite(entry_px) & (entry_px > 0) & can_buy
    out: dict[str, tuple[np.ndarray, ...]] = {}
    for prof in plist:
        tp, sl, hold = prof["tp"], prof["sl"], prof["hold"]
        tp_px = entry_px * (1.0 + tp) if tp is not None else None
        sl_px = entry_px * (1.0 + sl) if sl is not None else None
        ret = np.zeros(entry_px.shape, np.float64)
        done = np.zeros(entry_px.shape, bool)
        pending = np.zeros(entry_px.shape, bool)
        day = np.zeros(entry_px.shape, np.int16) if with_day else None
        mon = min(hold + 1, blk.n_days - 1)          # k 是相对信号日的位移，买入日=1
        for k in range(2, mon + 1):                  # 第 1 天是买入当天，T+1 不能卖
            hk, lk, ok_k, ck = shift_fwd(h, k), shift_fwd(l, k), shift_fwd(o, k), shift_fwd(c, k)
            have = np.isfinite(ck)
            # 一字板（最高=最低）当天没有对手盘：判据真源在 backtest/executability.py，
            # 组合回测引擎用的是同一个定义，只是成交时点不同
            sell = have & (hk != lk) & np.isfinite(ok_k) & (ok_k > 0)
            base = ok0 & have & ~done
            if sl_px is not None:
                hit = base & (lk <= sl_px)
                f = hit & sell
                fill = np.minimum(sl_px, ok_k)
                ret = np.where(f, (fill / entry_px - 1.0) * 100.0, ret)
                done |= f
                if with_day:
                    day[f] = k - 1
                pending |= hit & ~sell
            if tp_px is not None:
                hit = base & ~done & (hk >= tp_px)          # 同一天两条都碰：止损优先
                f = hit & sell
                fill = np.maximum(tp_px, ok_k)
                ret = np.where(f, (fill / entry_px - 1.0) * 100.0, ret)
                done |= f
                if with_day:
                    day[f] = k - 1
                pending |= hit & ~sell
            del hk, lk, ok_k, ck, have, sell, base
        if mon == hold + 1:                                 # 到期按收盘走
            ck = shift_fwd(c, hold + 1)
            ok_k = shift_fwd(o, hold + 1)
            hk, lk = shift_fwd(h, hold + 1), shift_fwd(l, hold + 1)
            sell = np.isfinite(ck) & (hk != lk) & np.isfinite(ok_k) & (ok_k > 0)
            due = ok0 & ~done & sell
            ret = np.where(due, (ck / entry_px - 1.0) * 100.0, ret)
            done |= due
            if with_day:
                day[due] = hold
            # 到期日没行情（停牌）和到期日一字板是同一件事：仓位还押着，等后面第一个有对手盘的日子
            pending |= ok0 & ~done & ~sell
        for j in range(hold + 2, min(hold + 2 + REPAIR_MAX, blk.n_days)):
            ok_j = shift_fwd(o, j)
            cj, hj, lj = shift_fwd(c, j), shift_fwd(h, j), shift_fwd(l, j)
            sell = np.isfinite(cj) & (hj != lj) & np.isfinite(ok_j) & (ok_j > 0)
            f = pending & ~done & sell
            if f.any():
                ret = np.where(f, (ok_j / entry_px - 1.0) * 100.0, ret)
                done |= f
                pending &= ~f
                if with_day:
                    day[f] = j - 1
        closed = (done & ok0).astype(np.uint8)
        out[prof["tag"]] = (ret, closed, day) if with_day else (ret, closed)
    return out


# ------------------------------------------------------------------ 触发点与读数

def cooldown(mask: np.ndarray, gap: int, day_lo: int, day_hi: int) -> np.ndarray:
    """冷却：这只票在前 `gap` 个交易日内该条件已经响过 ⇒ 这次不是新买点。

    调用方给 `gap = hold` 正好让两笔的持仓期不重叠：上一笔到期在「触发日+1+H」，
    新触发要晚于 H 天 ⇒ 新买入日至少晚一天。同一波行情连响 10 天只算 1 笔，
    而不是 10 笔「独立交易」。锚点是**触发日**而非成交日，
    这样判定与当天买不买得进无关，不会因停牌/涨停把买点整体挪位。
    """
    rows = np.arange(mask.shape[0], dtype=np.int32)[:, None]
    # 空档哨兵必须和 prev[0] 一样大：用 -1 的话，row-1 ~ row-gap 这几天会被当成
    # 「上一次响在 row-1」而误冷却掉，每只票在面板起点的头一笔就凭空丢了
    last = np.maximum.accumulate(np.where(mask, rows, np.int32(-10 ** 6)), axis=0)
    prev = np.empty_like(last)
    prev[0] = np.int32(-10 ** 6)          # 首行没有「上一次」，用大负数而不是 NaN（整型减法会溢出）
    prev[1:] = last[:-1]
    acc = mask & ((rows - prev) > gap)
    acc[:day_lo] = False
    acc[day_hi:] = False
    return acc


def band_key(c: dict) -> tuple:
    """同字段档位按分位起点排序（字符串序会把 q100 排到 q20 前面）。"""
    pct = str(c.get("percentile") or "")
    start = float(pct.split("-")[0]) if "-" in pct else 0.0
    return (c["kind"], c["field"], start, c["id"])


class Accum:
    """跨票块的累加器：一个「卖法 × 条件」一份读数，块只往里加，全部块跑完再一次性结算。

    为什么必须累加而不是逐块出读数：同一条件在块 A、块 B 各出一份读数的话，
    ① 落库时 `(run_id, cond_id, profile)` 会撞唯一索引（一半数据静默插不进去）；
    ② 按日聚类的 t 会被拆成两次小样本判定，门槛形同虚设。
    日均值要合并所有票之后再算，所以存的是「每个交易日的笔数 + 收益和」，不是各块的 t。
    """

    def __init__(self, n_tags: int, n_conds: int, n_days: int):
        self.dcnt = np.zeros((n_tags, n_conds, n_days), np.uint16)
        self.dsum = np.zeros((n_tags, n_conds, n_days), np.float32)
        self.n = np.zeros((n_tags, n_conds), np.float64)
        self.s1 = np.zeros((n_tags, n_conds), np.float64)
        self.s2 = np.zeros((n_tags, n_conds), np.float64)
        self.win = np.zeros((n_tags, n_conds), np.float64)
        self.gain = np.zeros((n_tags, n_conds), np.float64)
        self.loss = np.zeros((n_tags, n_conds), np.float64)

    def add(self, sl: slice, ci: int, di: np.ndarray, vals: np.ndarray, oks: np.ndarray,
            n_days: int) -> None:
        if not len(di):
            return
        v = vals * oks
        k = vals.shape[0]
        cnt = np.empty((k, n_days), np.float64)
        dsum = np.empty((k, n_days), np.float64)
        for pi in range(k):
            cnt[pi] = np.bincount(di, weights=oks[pi], minlength=n_days)
            dsum[pi] = np.bincount(di, weights=v[pi], minlength=n_days)
        self.dcnt[sl, ci] += cnt.astype(np.uint16)      # 单日单条件最多每票一笔，上限=票数<65535
        self.dsum[sl, ci] += dsum.astype(np.float32)
        self.n[sl, ci] += oks.sum(axis=1)
        self.s1[sl, ci] += v.sum(axis=1)
        self.s2[sl, ci] += (v * v).sum(axis=1)
        self.win[sl, ci] += (v > 0).sum(axis=1)
        self.gain[sl, ci] += np.maximum(v, 0).sum(axis=1)
        self.loss[sl, ci] += np.minimum(v, 0).sum(axis=1)

    def stat(self, ti: int, ci: int, era_cuts: tuple[int, int], n_days: int) -> dict:
        """将累加量转成一份读数（笔数/均值/标准差/胜率/盈亏比/按日聚类 t/三段）。"""
        nn = float(self.n[ti, ci])
        if nn == 0:
            return {"n": 0, "days": 0}
        cnt = self.dcnt[ti, ci].astype(np.float64)
        dsum = self.dsum[ti, ci].astype(np.float64)
        live = cnt > 0
        dn = int(live.sum())
        mean = float(self.s1[ti, ci] / nn)
        sd = (math.sqrt(max(float(self.s2[ti, ci] / nn - mean * mean) * nn / (nn - 1), 0.0))
              if nn > 1 else 0.0)
        t_day = None
        day_mean = None
        if dn >= 2:
            dm = dsum[live] / cnt[live]
            day_mean = float(dm.mean())
            var = float(np.var(dm, ddof=1))
            if var > 0:
                t_day = day_mean / math.sqrt(var / dn)
        rng = np.arange(n_days)
        era = []
        for lo, hi in ((0, era_cuts[0]), (era_cuts[0], era_cuts[1]), (era_cuts[1], n_days)):
            m = live & (rng >= lo) & (rng < hi)
            cc = float(cnt[m].sum())
            era.append(round(float(dsum[m].sum() / cc - COST_PP), 3) if cc >= 30 else None)
        return {"n": int(nn), "mean": mean, "sd": sd, "win": float(self.win[ti, ci]) / nn,
                "gain": float(self.gain[ti, ci]), "loss": float(self.loss[ti, ci]),
                "days": dn, "day_mean": day_mean, "t_day": t_day,
                "t_trade": (mean / (sd / math.sqrt(nn))) if sd > 0 else None, "era": era}


def make_row(c: dict, prof: dict, st: dict, n_trials: int, n_stock: int, n_days: int,
             n_cells: int) -> dict:
    """一条「条件 × 卖法」→ 读数 + 门槛判定。"""
    n = st.get("n", 0)
    gross = st.get("mean")
    payoff = None
    if n:
        payoff = float("inf") if st["loss"] == 0 else round(float(st["gain"] / -st["loss"]), 3)
    net = round(float(gross) - COST_PP, 3) if gross is not None else None

    def rnd(key: str, nd: int = 2) -> float | None:
        v = st.get(key)
        return round(float(v), nd) if v is not None and math.isfinite(v) else None

    row = {"cond_id": c["id"], "cond_label": c["label"], "kind": c["kind"], "field": c["field"],
           "expr": c["expr"], "profile": prof["tag"], "hold_days": prof["hold"],
           "take_profit": prof["tp"], "stop_loss": prof["sl"],
           "n_cells": n_cells, "n_trades": n, "n_no_fill": n_cells - n, "days": st.get("days", 0),
           "gross_pp": rnd("mean", 3), "net_per_trade_pp": net,
           "day_mean_pp": rnd("day_mean", 3),
           "win_rate": round(float(st["win"]), 4) if n else None, "payoff": payoff,
           "sd_pp": rnd("sd", 3), "t_day": rnd("t_day"), "t_trade": rnd("t_trade"),
           "era_net_pp": ",".join("nan" if e is None else f"{e:+.2f}" for e in st.get("era", [None] * 3)),
           "trades_per_stock_year": round(n / max(1, n_stock) / max(1, n_days) * TRADING_DAYS, 2)}
    g = rule_gates.evaluate_trade({"n_trades": n, "win_rate": row["win_rate"], "payoff": payoff,
                                   "net_per_trade_pp": net, "t_stat": row["t_day"]}, n_trials)
    row.update({"verdict": g["verdict"], "failed": g["failed"], "checks": g["checks"],
                "note": g["note"]})
    return row


# ------------------------------------------------------------------ 清单

def load_inventory(limit: int, inventory_file: str = "") -> tuple[list[dict], dict]:
    """取候选条件，返回 (条件列表, 批次信息)。

    默认从 Mongo 取基础条件；试水时按类别分层抽，别让 720 条价格条件把样本占满。
    `--inventory-file` 读预注册批次文件（合取那批）：**整批取，不抽**，
    从固定条数里挑几条看等于事后挑数据。
    """
    keys = ("id", "kind", "field", "ref", "expr", "label", "percentile", "sample_trigger_pct")
    if inventory_file:
        path = Path(inventory_file)
        if not path.is_file():
            raise SystemExit(f"--inventory-file 找不到文件：{path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        items = doc.get("conditions") or []
        if not items:
            raise SystemExit(f"{path} 里没有 conditions")
        declared = doc.get("n_conditions")
        if declared is not None and int(declared) != len(items):
            raise SystemExit(f"{path} 声明 {declared} 条、实际 {len(items)} 条："
                             f"预注册批次的条数不许改（改了就等于重新挑数据）")
        holdout = str(doc.get("holdout_start") or HOLDOUT_START)
        if holdout != HOLDOUT_START:
            raise SystemExit(f"{path} 的 holdout_start={holdout} 与扫描口径 {HOLDOUT_START} 不一致")
        conds = sorted(({k: c.get(k) for k in keys} for c in items), key=band_key)
        if limit and limit < len(conds):
            raise SystemExit(f"预注册批次（{path.name}，{len(conds)} 条）要整批跑，"
                             f"--limit-conditions={limit} 请去掉：从固定条数里挑几条看，"
                             f"挑的动作本身就回到了数据里")
        return conds, {"source": str(path), "batch_id": doc.get("batch_id"),
                       "batch_name": doc.get("name")}

    import database

    col = database.get_db()[BASE_COLL]
    conds = sorted(({k: d.get(k) for k in keys}
                    for d in col.find({}, {"_id": 0})), key=band_key)
    if not conds:
        raise SystemExit(f"{BASE_COLL} 是空的，先跑 scripts/base_conditions.py")
    batch = {"source": f"mongo:{BASE_COLL}"}
    if not limit or limit >= len(conds):
        return conds, batch
    by: dict[str, list[dict]] = {}
    for c in conds:
        by.setdefault(c["kind"], []).append(c)
    kinds = sorted(by)
    if limit < len(kinds):
        raise SystemExit(f"--limit-conditions 至少要 {len(kinds)}（每个类别 1 条），"
                         f"否则分层就没意义了：现有类别 {kinds}")
    # 名额按各类别占比分（最大余数法），每类保底 1 条：
    # 之前是「每类 round 一下，不足 limit 就按排序补齐」，结果 10 条全落在同一个类别里，
    # 试水看到的分布其实是单类分布，别的东西有没有问题一概没验到。
    raw = {k: len(by[k]) * limit / len(conds) for k in kinds}
    quota = {k: max(1, int(raw[k])) for k in kinds}
    order = sorted(kinds, key=lambda k: -raw[k])
    i = 0
    while sum(quota.values()) < limit:                    # 有空位：占比大的类先补
        quota[order[i % len(order)]] += 1
        i += 1
    while sum(quota.values()) > limit:                    # 超了：从最小的类退，保底 1 条
        for k in reversed(order):
            if quota[k] > 1:
                quota[k] -= 1
                break
    picked: list[dict] = []
    for kind in kinds:
        g = by[kind]
        # 类内也摊开取（按分位序等距），别全取排在最前面的那一档
        picked += [g[int(round(x))] for x in np.linspace(0, len(g) - 1, min(quota[kind], len(g)))]
    return sorted(picked, key=band_key), batch


# ------------------------------------------------------------------ 主扫描

def scan(files: list[Path], conds: list[dict], profs: list[dict], args) -> tuple[list[dict], dict]:
    global BLOCK
    BLOCK = max(50, args.block)
    by_h: dict[int, list[dict]] = {}
    for p in profs:
        by_h.setdefault(p["hold"], []).append(p)
    fields = needed_fields(conds)
    axis, dates, day_lo, day_hi = window(files, fields, args.date_from, args.date_to)
    n_days = len(dates)
    era_cuts = (day_lo + (day_hi - day_lo) // 3, day_lo + 2 * (day_hi - day_lo) // 3)
    logger.info("轴 %d 天，买点第 %d~%d 行（%s → %s）：%d 票 × %d 条件 × %d 卖法 = %d 组合，"
                "每块 %d 票；票池门槛 上市≥%d 根日线、近%d日日均成交额≥%s 元",
                n_days, day_lo, day_hi - 1, dates[day_lo], dates[day_hi - 1],
                len(files), len(conds), len(profs), len(conds) * len(profs), BLOCK,
                POOL["min_tenure"], AMOUNT_WINDOW, f"{POOL['min_amount']:,.0f}")

    rows: list[dict] = []
    bench = {p["tag"]: np.zeros(4) for p in profs}     # n/s1/s2/win：同池子全体时点同卖法 = 基准
    fills = {"cells": 0, "buy_blocked": 0, "no_next_quote": 0}
    # 门槛前后的格子数：报进 run doc，这样「票池收紧了多少」是可核对的数，而不是日志里一句话
    pool = {"cells_live": 0, "cells_ok": 0, "stocks_ok": 0}
    timing = {"load": 0.0, "masks": 0.0, "simulate": 0.0, "gather": 0.0}
    stocks = 0
    n_trials = args.trials_total          # 跨轮累计的试次数，main() 里算好（见 prior_trials）
    # 每种持有天数占 profs 里连续 16 个槽位（profiles() 按 h→tp→sl 生成），按跨度累加
    span: dict[int, slice] = {}
    for i, p in enumerate(profs):
        s0, _ = span.get(p["hold"], (i, i))
        span[p["hold"]] = (s0, i + 1)
    span = {h: slice(a, b) for h, (a, b) in span.items()}
    acc = Accum(len(profs), len(conds), n_days)
    cells = np.zeros((len(HOLDS), len(conds)), np.int64)      # 冷却后真正下单的格子数
    hold_row = {h: i for i, h in enumerate(HOLDS)}
    t0 = time.time()
    blocks = [files[i:i + BLOCK] for i in range(0, len(files), BLOCK)]
    for bi, bf in enumerate(blocks):
        tp = time.time()
        blk = Block(bf, fields, axis, dates)
        timing["load"] += time.time() - tp
        if blk.n_stock == 0:
            continue
        # 掩码只存位图：972 条 × 每张 bool 网格会吃掉 900MB，位图 1/8
        tp = time.time()
        bits = []
        for c in conds:
            try:
                # 票池门槛在这里就 AND 进买点掩码：条件响在一只基金上、或响在它上市第 30 天，
                # 都不算买点。基准那一侧用的是同一张 blk.ok，两边同池子才叫超额。
                m = rule_expr.eval_matrix(c["expr"], blk.cols) & blk.ok
            except Exception as exc:
                logger.warning("%s 求值失败：%s", c["id"], exc)
                bits.append(None)
                continue
            bits.append(np.packbits(m.ravel(), bitorder="little"))
        timing["masks"] += time.time() - tp
        entry_px, can_buy, have_quote = entry_gate(blk)
        in_win = (np.arange(n_days)[:, None] >= day_lo) & (np.arange(n_days)[:, None] < day_hi)
        win_cells = np.broadcast_to(in_win, (n_days, blk.n_stock))
        # 分母只数「这只票当天真的有日线、且过票池门槛」的格子。整块网格是矩形（交易日 × 票），
        # 票没上市的那些天全是 NaN，把它们算进来会把「还没上市」报成「买不到」——
        # 实测这样误报出来的无行情率是 34.65%，而真实值只有 0.2%
        live = blk.ok & win_cells
        fills["cells"] += int(live.sum())
        fills["no_next_quote"] += int((live & ~have_quote).sum())
        fills["buy_blocked"] += int((live & have_quote & ~can_buy).sum())
        # 门槛前后的格子数都按买点窗口算（和上面的下单分母同一个口径），差值就是门槛剔掉的
        pool["cells_live"] += int((np.isfinite(blk.cols["close"]) & win_cells).sum())
        pool["cells_ok"] += int(live.sum())
        pool["stocks_ok"] += int(live.any(axis=0).sum())
        for hold, plist in by_h.items():
            tp = time.time()
            grids = simulate(blk, entry_px, can_buy, plist)
            ret16 = np.stack([grids[p["tag"]][0] for p in plist])       # (16, n_days, n_stock)
            ok16 = np.stack([grids[p["tag"]][1] for p in plist])
            sel = ok16.astype(bool) & blk.ok[None] & win_cells[None]
            for pi, p in enumerate(plist):
                bv = np.where(sel[pi], ret16[pi], 0.0)
                b = bench[p["tag"]]
                b[0] += float(sel[pi].sum())
                b[1] += float(bv.sum())
                b[2] += float((bv * bv).sum())
                b[3] += float((bv > 0).sum())
            del grids, sel
            timing["simulate"] += time.time() - tp
            tp = time.time()
            for ci, (c, bt) in enumerate(zip(conds, bits)):
                if bt is None:
                    continue
                m = np.unpackbits(bt, bitorder="little")[: n_days * blk.n_stock] \
                    .reshape(n_days, blk.n_stock).astype(bool)
                a = cooldown(m, hold, day_lo, day_hi)
                del m
                di, si = np.nonzero(a)
                del a
                cells[hold_row[hold], ci] += di.size
                acc.add(span[hold], ci, di, ret16[:, di, si], ok16[:, di, si], n_days)
                del di, si
            del ret16, ok16
            timing["gather"] += time.time() - tp
        del win_cells, in_win
        stocks += blk.n_stock
        logger.info("块 %d/%d（%d 票，累计入格 %d 票）完成，累计 %.0fs", bi + 1,
                    len(blocks), blk.n_stock, stocks, time.time() - t0)
        del blk, bits, entry_px, can_buy

    n_win_days = day_hi - day_lo
    for ti, prof in enumerate(profs):
        sl = span[prof["hold"]]
        for ci, c in enumerate(conds):
            st = acc.stat(ti, ci, era_cuts, n_days)
            rows.append(make_row(c, prof, st, n_trials, stocks, n_win_days,
                                 int(cells[hold_row[prof["hold"]], ci])))
    meta = {"window": (dates[day_lo], dates[day_hi - 1]), "n_days": n_win_days,
            "bench": {k: dict(zip(("n", "s1", "s2", "win"), v)) for k, v in bench.items()},
            "fills": fills, "pool": pool, "trials_total": n_trials,
            "timing": {k: round(v, 2) for k, v in timing.items()},
            "elapsed": time.time() - t0, "blocks": len(blocks),
            "files": len(files), "stocks": stocks, "block": BLOCK}
    return rows, meta


# ------------------------------------------------------------------ 归因（第二趟，只跑过筛项）

def explain(files: list[Path], conds: list[dict], profs: list[dict], targets: dict[str, str],
            args) -> tuple[dict[str, dict], list[dict]]:
    """8 项「为什么」，全是能在原始日线上核对的数：收益落在第几天 / 卖早了没有 /
    是不是靠几笔妖股 / 在什么环境下赚 / 挪一档还在不在（在主表 neighbour_bands_pp）/
    月块重抽还赚不赚 / 跟谁重复 / 三段（主表 era_net_pp）+ 留出段（另跑 --from 2022-01-01）。
    """
    global BLOCK
    pick = [c for c in conds if c["id"] in targets]
    if not pick:
        return {}, []
    by_id = {c["id"]: c for c in pick}
    prof_by_tag = {p["tag"]: p for p in profs}
    fields = needed_fields(pick)
    axis, dates, day_lo, day_hi = window(files, fields, args.date_from, args.date_to)
    n_days = len(dates)
    month_ids = np.asarray(pd.factorize(pd.to_datetime(pd.Index(dates)).strftime("%Y%m"))[0])
    rows_by_tag: dict[str, dict[str, list[np.ndarray]]] = {}
    cells: dict[str, list[np.ndarray]] = {c["id"]: [] for c in pick}
    up = np.zeros(n_days)
    tot = np.zeros(n_days)
    dump: list[dict] = []
    t0 = time.time()
    blocks = [files[i:i + BLOCK] for i in range(0, len(files), BLOCK)]
    for bi, bf in enumerate(blocks):
        blk = Block(bf, fields, axis, dates)
        if blk.n_stock == 0:
            continue
        pc = blk.cols["pct"]
        # 市场环境（涨跌家数）也只数池子里的票：门槛外的基金/次新不该进「当天大盘强弱」的分母
        live = blk.ok & np.isfinite(pc)
        up += np.where(live, (pc > 0).astype(np.float32), 0.0).sum(axis=1)
        tot += live.sum(axis=1)
        entry_px, can_buy, _ = entry_gate(blk)
        tags = {targets[c["id"]] for c in pick}
        grids = {tag: simulate(blk, entry_px, can_buy, [prof_by_tag[tag]], with_day=True)[tag]
                 for tag in tags}
        fwd_close = {k: shift_fwd(blk.cols["close"], k + 1) for k in CURVE_K}   # 买入后第 k 天的收盘
        for c in pick:
            tag = targets[c["id"]]
            prof = prof_by_tag[tag]
            m = rule_expr.eval_matrix(c["expr"], blk.cols) & blk.ok   # 与主扫描同一张票池掩码
            acc = cooldown(m, prof["hold"], day_lo, day_hi)
            del m
            di, si = np.nonzero(acc)
            # 全局格子键要把块号也编进去，否则不同块里「同一天不同票」会撞成同一个数
            cells[c["id"]].append((di.astype(np.int64) * (BLOCK + 1) + si.astype(np.int64))
                                  * len(blocks) + bi)
            if not len(di):
                continue
            ret, closed, day = grids[tag]
            sel = closed[di, si].astype(bool)
            d0, s0 = di[sel], si[sel]
            rr = ret[d0, s0]
            ep = entry_px[d0, s0].astype(np.float64)
            g = rows_by_tag.setdefault(c["id"], {"ret": [], "d0": [], "fwd": []})
            g["ret"].append(rr)
            g["d0"].append(d0)
            g["fwd"].append(np.stack([np.where(np.isfinite(fwd_close[k][d0, s0]),
                                               (fwd_close[k][d0, s0] / ep - 1.0) * 100.0, np.nan)
                                      for k in CURVE_K]))
            for j in np.flatnonzero(sel)[: max(0, args.dump_trades - len(dump))]:
                d, s = int(d0[j]), int(s0[j])
                cl = float(blk.cols["close"][d, s])
                ed = d + 1 + int(day[d, s])          # 买入日 = d+1，day 是买入后第几天
                dump.append({"code": blk.codes[s], "cond_id": c["id"], "profile": tag,
                             "trigger_date": dates[d], "trigger_close": round(cl, 3),
                             "entry_date": dates[min(d + 1, n_days - 1)],
                             "entry_px": round(float(ep[j]), 3),
                             "open_gap_pp": round(float(ep[j] / cl - 1.0) * 100.0, 3),
                             "exit_after_days": int(day[d, s]),
                             "exit_date": dates[min(ed, n_days - 1)],
                             "ret_pp": round(float(rr[j]), 3),
                             "net_pp": round(float(rr[j]) - COST_PP, 3)})
        del blk, entry_px, can_buy, grids, fwd_close
        logger.info("归因块 %d/%d 完成，累计 %.0fs", bi + 1, len(blocks), time.time() - t0)

    breadth = np.where(tot > 0, up / np.maximum(tot, 1), np.nan)
    out: dict[str, dict] = {}
    for cid, g in rows_by_tag.items():
        if not g["ret"] or not sum(len(x) for x in g["ret"]):
            continue
        ret = np.concatenate(g["ret"]).astype(np.float64)
        d0 = np.concatenate(g["d0"])
        fwd = np.concatenate(g["fwd"], axis=1)
        net = ret - COST_PP
        res: dict = {"profile": targets[cid], "n_trades": int(ret.size),
                     "day_curve_pp": {str(k): round(float(np.nanmean(fwd[ki] - COST_PP)), 3)
                                      for ki, k in enumerate(CURVE_K)},
                     "sell_too_early_pp": None, "top_share_of_gain": None,
                     "median_net_pp": round(float(np.median(net)), 3),
                     "by_breadth_quintile_pp": [], "month_block_boot": {},
                     "most_overlapping": [], "note": ""}
        last = float(np.nanmean(fwd[len(CURVE_K) - 1] - COST_PP))
        res["sell_too_early_pp"] = round(last - float(net.mean()), 3)  # 拿到第 20 天比按规则走多/少赚
        pos = np.sort(net)[::-1]
        tot_gain = float(pos[pos > 0].sum())
        top = max(5, int(net.size * 0.01))
        if tot_gain > 0:
            res["top_share_of_gain"] = round(float(pos[:top][pos[:top] > 0].sum() / tot_gain), 3)
        fb = breadth[np.isfinite(breadth)]
        if fb.size > 10:
            edges = np.quantile(fb, [0.2, 0.4, 0.6, 0.8])
            bucket = np.digitize(breadth[d0], edges)
            res["by_breadth_quintile_pp"] = [round(float(net[bucket == i].mean()), 3)
                                             if (bucket == i).sum() >= 50 else None for i in range(5)]
        pool: dict[int, list[float]] = {}
        for mm, vv in zip(month_ids[d0], net):
            pool.setdefault(int(mm), []).append(float(vv))
        months = np.array(list(pool))
        rng = np.random.default_rng(7)
        means = [float(np.concatenate([pool[int(x)] for x in
                                       rng.choice(months, months.size, replace=True)]).mean())
                 for _ in range(BOOT_N)]
        res["month_block_boot"] = {"positive_pct": round(100.0 * sum(1 for x in means if x > 0) / BOOT_N, 1),
                                   "p10_pp": round(float(np.quantile(means, 0.1)), 3)}
        mine = _sample_set(np.concatenate(cells[cid]))
        jac = []
        for other, lst in cells.items():
            if other == cid or not lst:
                continue
            theirs = _sample_set(np.concatenate(lst))
            if mine and theirs:
                jac.append({"cond_id": other, "label": by_id[other]["label"],
                            "jaccard": round(len(mine & theirs) / len(mine | theirs), 3)})
        res["most_overlapping"] = sorted(jac, key=lambda x: -x["jaccard"])[:3]
        res["note"] = ("这些是能对着原始日线核对的数，不是因果解释。「主力为什么这么干」这类话"
                       "我做不出来；文案只会把上面的数字复述一遍")
        out[cid] = res
    return out, dump


def _sample_set(arr: np.ndarray, cap: int = 3000) -> set[int]:
    """重叠度抽 3000 个点估：几百万触发点全比是 O(n²) 的白工。"""
    if not len(arr):
        return set()
    return set(arr[:: max(1, arr.size // cap)].tolist())


# ------------------------------------------------------------------ 入库与报告

def to_mongo(run: dict, rows: list[dict]) -> None:
    import database

    db = database.get_db()
    runs, rws = db[RUNS_COLL], db[ROWS_COLL]
    runs.delete_many({"run_id": run["run_id"]})
    runs.insert_one(run)
    rws.delete_many({"run_id": run["run_id"]})
    docs = [{**r, "run_id": run["run_id"]} for r in rows]
    for i in range(0, len(docs), 2000):
        rws.insert_many(docs[i:i + 2000], ordered=False)
    rws.create_index([("run_id", 1), ("cond_id", 1), ("profile", 1)], unique=True)
    rws.create_index([("run_id", 1), ("net_per_trade_pp", -1)])
    rws.create_index([("run_id", 1), ("verdict", 1), ("t_day", -1)])
    logger.info("已写 Mongo：%s.%s run=%s %d 行", db.name, ROWS_COLL, run["run_id"], len(docs))


def trials_used(exclude_run_id: str = "") -> int:
    """累计试过多少个组合 = 本地台账 + Mongo 里台账没记到的历史 run（按 run_id 去重）。

    及格线必须按**累计**试次抬，不是每轮重置：同一批行情反复扫，试的次数是加在一起的，
    否则一轮不行就扫二十轮，总能靠运气凑出一条判定「过」的规则。本地台账连 `--dry-run`
    一起记（干跑不落 Mongo，但它确实看过这份数据，不记就是给试次留后门）。
    """
    total, seen = 0, set()
    if TRIAL_LEDGER.is_file():
        for line in TRIAL_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            total += int(rec.get("combos") or 0)
            if rec.get("run_id"):
                seen.add(str(rec["run_id"]))
    try:
        import database

        for d in database.get_db()[RUNS_COLL].find({}, {"run_id": 1, "n_trials": 1}):
            rid = str(d.get("run_id") or "")
            if rid and rid not in seen and rid != exclude_run_id:
                total += int(d.get("n_trials") or 0)
    except Exception as exc:
        logger.warning("Mongo 历史试次没读到，这一轮只按本地台账算：%s", exc)
    return total


def append_trials(rec: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with TRIAL_LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def attach_bench_and_neighbours(rows: list[dict], meta: dict, conds: list[dict]) -> None:
    """补两列：同卖法全体时点的基准，与同字段相邻档在同种卖法下的扣后每笔。"""
    for r in rows:
        b = meta["bench"].get(r["profile"])
        if b and b["n"] > 0:
            base_pp = float(b["s1"] / b["n"]) - COST_PP
            r["bench_net_pp"] = round(base_pp, 3)
            r["excess_pp"] = round((r["net_per_trade_pp"] or 0.0) - base_pp, 3)
    per = {c["id"]: c for c in conds}
    order: dict[str, list[str]] = {}
    for c in sorted(conds, key=band_key):
        order.setdefault(c["field"], []).append(c["id"])
    bands: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        bands.setdefault((per[r["cond_id"]]["field"], r["profile"]), {})[r["cond_id"]] = r["net_per_trade_pp"]
    for r in rows:
        ids = order.get(per[r["cond_id"]]["field"], [])
        i = ids.index(r["cond_id"]) if r["cond_id"] in ids else -1
        r["neighbour_bands_pp"] = [
            {"cond_id": ids[j], "band": per[ids[j]]["label"],
             "net_pp": bands.get((per[r["cond_id"]]["field"], r["profile"]), {}).get(ids[j])}
            for j in (i - 1, i + 1) if 0 <= j < len(ids)]


def render(rows: list[dict], meta: dict, conds: list[dict], profs: list[dict], args) -> str:
    combos = len(conds) * len(profs)
    f = meta["fills"]
    t = meta["timing"]
    # 全量外推：读盘与网格模拟只随票数走，条件求值/取数随票数×条件数走
    s = FULL_STOCKS / max(1, meta["stocks"])
    k = 972 / max(1, len(conds))
    est = (t["load"] + t["simulate"]) * s + (t["masks"] + t["gather"]) * s * k
    pool = meta["pool"]
    total = meta["trials_total"]
    L = [f"窗口 {meta['window'][0]} → {meta['window'][1]}（{meta['n_days']} 个交易日做买点；"
         f"止于 {args.date_to}，末尾 {max(HOLDS) + 2} 天留作卖出窗口，留出段不碰）",
         f"{len(conds)} 条件 × {len(profs)} 卖法 = {combos:,} 组合；每笔扣 {COST_PP}pp"
         f"（{args.amount:,} 元/笔口径）；累计尝试 {total:,} 次（本轮 {combos:,}）⇒ 显著性要 t≥"
         f"{rule_gates.t_required(total):.2f}（按交易日聚类）",
         f"票池：有日线 {pool['cells_live']:,} 格 → 过门槛 {pool['cells_ok']:,} 格"
         f"（门槛剔掉 {1 - pool['cells_ok'] / max(1, pool['cells_live']):.1%}），"
         f"入格 {meta['stocks']:,} 只票里 {pool['stocks_ok']:,} 只至少有一段可下单；"
         f"门槛=上市≥{POOL['min_tenure']} 根日线"
         + (f"、近{AMOUNT_WINDOW}日日均成交额≥{POOL['min_amount']:,.0f} 元" if POOL['min_amount'] else ""),
         f"读数 {len(rows):,} 行，"
         f"耗时 {meta['elapsed']:.1f}s = 读盘 {t['load']:.1f}s + 条件求值 {t['masks']:.1f}s"
         f" + 网格模拟 {t['simulate']:.1f}s + 取数读数 {t['gather']:.1f}s",
         f"外推全量（约 {FULL_STOCKS:,} 票 × 972 条件 = {972 * len(profs):,} 组合）"
         f"≈ {est / 60:.0f} 分钟：票数 ×{s:.1f}、条件 ×{k:.1f}，"
         f"读盘/模拟只按票数放大、求值/取数按两者一起放大（线性估，误差会在 1.5 倍内）",
         f"下单格子 {f['cells']:,} 个：次日没有行情（停牌/退市）{f['no_next_quote']:,} 个"
         f"（{f['no_next_quote'] / max(1, f['cells']):.2%}），次日开盘顶涨停或一字板买不进 "
         f"{f['buy_blocked']:,} 个（{f['buy_blocked'] / max(1, f['cells']):.2%}）",
         "判定分布：" + "、".join(f"{v} {sum(1 for r in rows if r['verdict'] == v):,}"
                                   for v in ("过", "不过", "不判")),
         "卡在哪条：" + "、".join(f"{k} {sum(1 for r in rows if k in r['failed']):,}"
                                 for k in rule_gates.TRADE_GATE_KEYS)]
    good = [r for r in rows if r["verdict"] == "过"]
    shown = good or sorted((r for r in rows if (r["net_per_trade_pp"] or -9) > 0),
                           key=lambda r: -r["net_per_trade_pp"])[:20]
    L.append(f"\n{'条件':<28s} {'卖法':<12s} {'笔数':>8s} {'胜率':>6s} {'盈亏比':>6s} "
             f"{'扣后每笔':>9s} {'比基准':>7s} {'t日':>6s} {'t笔':>6s} 判定")
    for r in sorted(shown, key=lambda x: -(x["net_per_trade_pp"] or -9))[:30]:
        po = r["payoff"]
        po_txt = "∞" if po == float("inf") else (f"{po:.2f}" if po else "-")
        L.append(f"{(r['cond_label'] or '')[:26]:<28s} {r['profile']:<12s} {r['n_trades']:>8,} "
                 f"{(r['win_rate'] or 0):>6.1%} {po_txt:>6s} "
                 f"{(r['net_per_trade_pp'] or 0):>+8.2f}p {(r['excess_pp'] or 0):>+6.2f}p "
                 f"{(r['t_day'] if r['t_day'] is not None else 0):>6.2f} "
                 f"{(r['t_trade'] if r['t_trade'] is not None else 0):>6.2f} {r['verdict']}"
                 f"{' ←' + ','.join(r['failed']) if r['failed'] else ''}")
    if not good:
        L.append("\n一条都没过。这和 10-16 那轮一致：条件随便组合都能跑出「看着像」的读数，"
                 "把及格线按尝试次数摆正之后基本都活不下来。")
    return "\n".join(L)


def export_csv(path: Path, rows: list[dict]) -> None:
    keys = ["cond_id", "cond_label", "kind", "field", "expr", "profile", "n_cells", "n_trades",
            "n_no_fill", "days", "gross_pp", "net_per_trade_pp", "day_mean_pp", "win_rate", "payoff", "sd_pp",
            "t_day", "t_trade", "bench_net_pp", "excess_pp", "era_net_pp",
            "trades_per_stock_year", "verdict", "failed"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        wt = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        wt.writeheader()
        for r in rows:
            wt.writerow({k: (",".join(map(str, r[k])) if isinstance(r.get(k), list) else r.get(k))
                         for k in keys})


# ------------------------------------------------------------------ CLI

MEM_MIN_AVAIL_GB = 3.0
MEM_MIN_SWAP_FREE_PCT = 10.0
MEMINFO = Path("/proc/meminfo")


def mem_headroom(path: Path = MEMINFO) -> dict:
    """从 /proc/meminfo 取可用内存和交换余量。`MemAvailable` 才是能给这块用的，
    `MemFree` 会把可回收的页缓存算成没有；没配交换时 swap_free_pct=None（那条不做判定）。"""
    kb: dict[str, float] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                kb[key.strip()] = float(parts[0])
    except OSError:
        return {}
    total_sw = kb.get("SwapTotal", 0.0)
    return {"avail_gb": kb.get("MemAvailable", 0.0) / 1048576.0,
            "swap_free_pct": (100.0 * kb.get("SwapFree", 0.0) / total_sw) if total_sw > 0 else None,
            "swap_total_gb": total_sw / 1048576.0}


def check_mem_headroom(skip: bool = False, path: Path = MEMINFO) -> dict:
    """起跑前拦一道：这一轮要同时握着 BLOCK 只票的列、972 条掩码位图和 16 张收益网格，
    峰值几个 G。内存压穿时 earlyoom 不会挑责任进程下手，它按 oom_score 杀，被杀的不一定是扫描
    （2026-10-17 那天两次把 IDE 杀了），所以宁可不跑。"""
    m = mem_headroom(path)
    if not m:
        logger.warning("读不到 /proc/meminfo，内存水位检查跳过")
        return m
    swap = m["swap_free_pct"]
    logger.info("起跑前内存：可用 %.1fG，交换剩余 %s",
                m["avail_gb"], "没有交换分区" if swap is None else f"{swap:.0f}%")
    if skip:
        return m
    bad = []
    if m["avail_gb"] < MEM_MIN_AVAIL_GB:
        bad.append(f"可用内存 {m['avail_gb']:.1f}G < {MEM_MIN_AVAIL_GB:.0f}G")
    if swap is not None and swap < MEM_MIN_SWAP_FREE_PCT:
        bad.append(f"交换只剩 {swap:.0f}% < {MEM_MIN_SWAP_FREE_PCT:.0f}%")
    if bad:
        raise SystemExit(
            f"起跑前内存不够：{'；'.join(bad)}。先让出内存再跑（关掉吃内存的进程，或把 --step 调大"
            f"先扫一小批）；确认过风险、就要在这点余量上硬跑，再加 --skip-mem-guard。")
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="买入条件 × 卖法 的逐笔盈亏扫描")
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--step", type=int, default=1, help="每 N 个文件取 1 只票（试水用 20）")
    ap.add_argument("--block", type=int, default=BLOCK)
    ap.add_argument("--skip-mem-guard", action="store_true",
                    help="不等内存水位（默认可用不足 3G、或交换剩余不足一成时直接拒绝起跑）")
    ap.add_argument("--limit-conditions", type=int, default=0, help="只扫 N 条（分层抽，试水）")
    ap.add_argument("--inventory-file", default="",
                    help="预注册批次 JSON（scripts/conjunction_candidates.py 生成），整批读、不抽")
    ap.add_argument("--from", dest="date_from", default=DATE_FROM)
    ap.add_argument("--to", dest="date_to", default=HOLDOUT_START)
    ap.add_argument("--amount", type=int, default=20_000, help="每笔金额（元），成本档口径")
    ap.add_argument("--explain", type=int, default=0, help="对判定「过」的前 N 条算 8 项归因")
    ap.add_argument("--explain-force", default="",
                    help="强制归因的 cond_id=卖法（逗号分隔）。试水时一条没过，用它把归因趟跑通")
    ap.add_argument("--dump-trades", type=int, default=0, help="打印 N 笔逐笔明细供人工复算")
    ap.add_argument("--min-tenure", type=int, default=DEFAULT_MIN_TENURE,
                    help="信号日这只票在本库里至少已有多少根日线（把次新博弈挡在外面）；0=不设")
    ap.add_argument("--min-amount", type=float, default=0.0,
                    help=f"信号日前 {AMOUNT_WINDOW} 个交易日日均成交额下限（元）；0=不设")
    ap.add_argument("--trial-budget", type=int, default=300_000,
                    help="累计试次预算：用满就收手不再扫；0=不设限")
    ap.add_argument("--export-csv", default="")
    ap.add_argument("--dry-run", action="store_true", help="不写 Mongo")
    ap.add_argument("--run-id", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    check_mem_headroom(args.skip_mem_guard)

    conds, batch = load_inventory(args.limit_conditions, args.inventory_file)
    for c in conds:
        c["fields"] = rule_expr.validate(c["expr"])
    profs = profiles()
    files = sorted(Path(args.kline_dir).glob("*.parquet"))
    files, dropped_funds = filter_funds(files)
    if dropped_funds:
        logger.info("票池：按代码段剔掉场内基金 %s 只，剩 %s 个日线文件",
                    f"{dropped_funds:,}", f"{len(files):,}")
    files = files[:: max(1, args.step)]
    POOL["min_tenure"] = max(0, args.min_tenure)
    POOL["min_amount"] = max(0.0, args.min_amount)

    combos = len(conds) * len(profs)
    prior = trials_used(args.run_id)
    if args.trial_budget and prior >= args.trial_budget:
        raise SystemExit(
            f"累计已经试过 {prior:,} 个组合，超出试次预算 {args.trial_budget:,}：再扫下去「找出一条过的"
            f"规则」越来越像是试出来的，不是行情里真有的。要接着找得换样本（另一段行情、另一批字段），"
            f"而不是把门槛再降一档。")
    args.trials_total = prior + combos
    if args.trial_budget and args.trials_total > args.trial_budget:
        logger.warning("这一轮跑完累计 %s 次，超出预算 %s：这是最后一轮",
                       f"{args.trials_total:,}", f"{args.trial_budget:,}")
    rows, meta = scan(files, conds, profs, args)
    attach_bench_and_neighbours(rows, meta, conds)
    print(render(rows, meta, conds, profs, args))

    run_id = args.run_id or pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
    good = [r for r in rows if r["verdict"] == "过"]
    whys: dict[str, dict] = {}
    dump: list[dict] = []
    targets: dict[str, str] = {}
    for spec in str(args.explain_force).split(","):
        cid, _, tag = spec.strip().partition("=")
        if cid and tag:
            targets[cid] = tag
    if args.explain:
        limit = max(args.explain, len(targets))
        for r in sorted(good, key=lambda x: -(x["net_per_trade_pp"] or 0)):
            targets.setdefault(r["cond_id"], r["profile"])
            if len(targets) >= limit:
                break
    if targets:
        logger.info("归因第二趟（过筛 %d 条）：%s", len(good),
                    "、".join(f"{k}={v}" for k, v in targets.items()))
        whys, dump = explain(files, conds, profs, targets, args)
        for cid, w in whys.items():
            for r in rows:
                if r["cond_id"] == cid and r["profile"] == targets[cid]:
                    r["why"] = w
        print("\n【为什么】\n" + json.dumps(whys, ensure_ascii=False, indent=1)[:6000])
    if dump:
        print("\n【逐笔抽样：拿去原始日线复算】")
        for t in dump:
            print("  " + json.dumps(t, ensure_ascii=False))
    if args.export_csv:
        export_csv(Path(args.export_csv), rows)
        print(f"CSV：{args.export_csv}")
    pool_doc = {"min_tenure": POOL["min_tenure"], "min_amount": POOL["min_amount"],
                "amount_window_days": AMOUNT_WINDOW, "dropped_funds": dropped_funds,
                "files": len(files), **meta["pool"]}
    # 干跑也记试次：它同样看过这份数据，不记账就等于给「反复扫到出一条过的」留后门
    append_trials({"ts": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "run_id": "" if args.dry_run else run_id, "combos": combos,
                   "trials_total": args.trials_total, "dry_run": bool(args.dry_run),
                   "n_pass": len(good), "window": [args.date_from, args.date_to],
                   "batch_id": batch.get("batch_id"), "inventory": batch.get("source"),
                   "pool": pool_doc})
    if args.dry_run:
        logger.info("dry-run：不写 Mongo（累计试次已记账到 %s 次）", f"{args.trials_total:,}")
        return 0
    run = {"run_id": run_id, "created_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
           "window": list(meta["window"]), "n_days": meta["n_days"],
           "date_from": args.date_from, "date_to": args.date_to,
           "n_conditions": len(conds), "n_profiles": len(profs), "n_trials": combos,
           "n_trials_total": args.trials_total, "trial_budget": args.trial_budget,
           "t_required": round(rule_gates.t_required(args.trials_total), 3), "cost_pp": COST_PP,
           "trade_amount": args.amount, "step": args.step, "files": len(files),
           "stocks": meta["stocks"], "pool": pool_doc, "batch": batch,
           "n_pass": len(good), "elapsed_s": round(meta["elapsed"], 1), "fills": meta["fills"],
           "holdout_start": HOLDOUT_START}
    to_mongo(run, rows)
    if whys:
        import database
        for cid, w in whys.items():
            database.get_db()[ROWS_COLL].update_many(
                {"run_id": run_id, "cond_id": cid, "profile": targets[cid]},
                {"$set": {"why": w}})
        out = LOG_DIR / f"explain_{run_id}.json"
        out.write_text(json.dumps({"why": whys, "trades": dump}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"归因：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
