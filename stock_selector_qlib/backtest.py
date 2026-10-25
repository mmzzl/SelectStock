"""qlib 回测引擎：产出与自研内核**同形状**的摘要，页面与落库不用改。

为什么能塞进 celery worker：自研内核跑全市场几十年也就几百 MB，而
`scripts/qlib_run.py` 全跑峰值 3.39 GB——那是因为要载 2016→2026 的训练面板。
这里只做「给定窗口打分 + 撮合」，不训练，窗口 1 年 ≈ 300 只 × 320 行，
载的是因子表而不是历史全量，实测见 docs §9。

口径差异（必须写清楚，否则两个引擎的数字没法放在一起看）：
- 成交在 T 日收盘，读的是 T−1 日的分数（qlib 日历 shift=1，与 qlib_run.py 一致，不再手动平移）。
- 持仓纪律有两种，由 `hold_rule` 选：
  · `dropout` = qlib 的 TopkDropout，每天**强制**换掉 `n_drop` 只最低分（日双边换手 ~33%）；
  · `band` = 「进榜严、出榜松」：持仓只要还在前 `exit_rank` 名就一直拿，跌出去才卖，
    空位从**前 `entry_rank` 名**里按分高低补满（`topk` 只，买入时等权，之后不每日再平衡）。
  两臂共用同一个 Exchange、同一套挡单/成本/`risk_degree`、同样先卖后买，
  所以数字上的差别只来自纪律本身。
- `position_cap_pct / target_amount / max_hold_days / 规则串` 这些自研内核的参数两个纪律都不看，
  带这些参数提交时 core/backtest_params.py 直接报错，不做静默忽略。
- `stop_loss_pct / take_profit_pct` 只在 `band` 下按「相对买入价」生效（默认 0=关闭），
  `dropout` 没有买入价跟踪，传了直接报错。日频只能按收盘判断/同一收盘成交，是乐观上界；
  后端白名单暂未放行，先给研究脚本用，验证过再决定是否上线上。
- **收益一律按扣成本净值**（`report["account"]` = 现金+持仓市值）。qlib 的 `report["return"]`
  是**扣成本前**的毛收益（`backtest/account.py:283` 把当天手续费加回去了），看毛利会得出完全相反的结论，
  所以毛利只作为 `gross_return` 并排放在一起。
- 成交笔数由**日频持仓变动反推**：同日同票的买卖合并成一笔，卖出的成本价取该股当前加权均价
  （自研内核是逐笔 FIFO）。日频撮合本来就看不到日内顺序，这点不装作能看到。
"""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
from qlib.contrib.strategy.signal_strategy import BaseSignalStrategy

from stock_selector_qlib import artifacts, guardrails, serving

TRADING_DAYS = 243  # 与自研内核同一口径（A 股年交易日）

EXCHANGE_KWARGS = {
    "freq": "day",
    "open_cost": 0.0005,
    "close_cost": 0.0015,
    "min_cost": 5.0,
    # 9.5% 当涨跌停：主板一字板买不进/卖不掉；30x/68x 的 20% 票也被一并挡掉，偏严不造假成交
    "limit_threshold": 0.095,
    "deal_price": "close",
}

HOLD_RULES = ("dropout", "band")
BAND_DEFAULTS = {"hold_rule": "dropout", "entry_rank": 50, "exit_rank": 100}


def run_qlib_backtest(args, progress_cb=None, signal: pd.Series | None = None):
    """跑一次 qlib 回测。

    `signal` 只为批次 G 的四臂 AB 存在（同一份分数喂四臂，打分只算一次，内存也只载一次面板）；
    生产路径不传，走 `score_panel`。
    """
    params = vars(args) if not isinstance(args, dict) else dict(args)
    for key, value in BAND_DEFAULTS.items():
        params.setdefault(key, value)
    hold_rule = str(params["hold_rule"]).strip().lower()
    if hold_rule not in HOLD_RULES:
        raise RuntimeError(f"hold_rule 只支持 {'/'.join(HOLD_RULES)}，收到 {hold_rule!r}")
    task_id = uuid.uuid4().hex
    art = artifacts.load()
    serving.init_qlib()
    _tick(progress_cb, 2, "加载因子面板")

    days = serving.calendar()
    req_beg, req_end = str(params["start"]), str(params["end"])
    beg, end = _snap_window(days, req_beg, req_end)
    # 与线上打分共用同一个票池函数：8003 在 universe 模式下读每日全市场名单，
    # 回测如果还写死 art.pool（训练池 300 只），跑出来的 IR 就不能拿来替线上背书。
    pool = serving.score_pool(art)
    auto_signal = signal is None
    guard_on = bool(params.get("guardrails", False)) and serving.guardrails_enabled()

    if auto_signal:
        signal = score_panel(art, beg, end, pool=pool, guardrails=guard_on,
                             progress_cb=progress_cb)
    else:
        signal = signal.loc[beg:end]
    if signal.empty:
        raise RuntimeError(f"{beg}→{end} 窗口内票池 {len(pool)} 只没有一行分数")

    _tick(progress_cb, 40, f"qlib 撮合中（{hold_rule}，日频没有逐日回调）")
    from qlib.contrib.evaluate import backtest_daily

    strategy = _make_strategy(hold_rule, params, signal)
    report, positions = backtest_daily(start_time=beg, end_time=end, strategy=strategy,
                                       account=float(params["capital"]),
                                       benchmark=str(params["benchmark"]),
                                       exchange_kwargs=EXCHANGE_KWARGS)
    _tick(progress_cb, 85, "汇总读数")

    shares = _shares_frame(positions, pool)
    closes = _close_panel(pool, beg, end)
    trades = _trades(shares, closes)
    capital = float(params["capital"])
    # 净值与收益只能用 report['account']（现金 + 持仓市值，手续费已经扣掉）。
    # 'value' 只是持仓市值；'return' 是**扣成本之前**的毛收益——qlib 源码
    # backtest/account.py:283 写的是 `return_rate = (earning + cost) / last_account_value`，
    # 把当天的手续费又加了回去。本策略日换手两位数，毛/净差掉的就是整年赚头，
    # 用错这一列等于给自己造假成绩。
    values = _account_curve(report, capital)
    name_days = int((shares > 0).sum().sum())
    metrics = _metrics(report, values, capital, trades, name_days=name_days)
    equity = [{"date": str(d)[:10], "total_value": float(v)} for d, v in values.items()]

    return SimpleNamespace(
        task_id=task_id,
        strategy=f"qlib:{art.run_id}:{hold_rule}",
        start_date=beg,
        end_date=end,
        metrics=metrics,
        trades=trades,
        equity=equity,
        executability={
            "engine": "qlib",
            "deal_price": EXCHANGE_KWARGS["deal_price"],
            "open_cost": EXCHANGE_KWARGS["open_cost"],
            "close_cost": EXCHANGE_KWARGS["close_cost"],
            "min_cost": EXCHANGE_KWARGS["min_cost"],
            "limit_threshold": EXCHANGE_KWARGS["limit_threshold"],
            "limit_note": "所有板块按 ±9.5% 挡单：创业板/科创板 20% 的票也被一并挡掉，偏严",
            "only_tradable": False,
            "signal_lag": "T 日收盘成交、读 T−1 日分数（qlib 日历 shift=1）",
            "trade_source": "日频持仓变动反推，同日同票合并一笔；卖出成本为加权均价（自研内核是 FIFO）",
            "return_caliber": "metrics/equity 全是**扣成本净值**（report['account']）；"
                              "gross_return 才是扣成本前的毛收益（qlib 的 return 列把手续费加回去了）",
            "hold_rule": hold_rule,
            "topk": int(params["topk"]),
            "n_drop": int(params["n_drop"]) if hold_rule == "dropout" else None,
            "entry_rank": int(params["entry_rank"]) if hold_rule == "band" else None,
            "exit_rank": int(params["exit_rank"]) if hold_rule == "band" else None,
            "stop_loss_pct": float(params.get("stop_loss_pct") or 0.0),
            "take_profit_pct": float(params.get("take_profit_pct") or 0.0),
            "stop_note": ("按相对买入价、收盘判断/同一收盘成交；盘中/跳空未建模"
                          if hold_rule == "band" else "dropout 不支持"),
            "benchmark": str(params["benchmark"]),
            "requested_window": f"{req_beg}→{req_end}",
            "traded_window": f"{beg}→{end}",
            "requested_end": str(params["end"]),
            "traded_end": end,
            "scored_rows": int(len(signal)),
            # 口径标签：model 是训练池，universe 才是 8003 线上名单；护栏也必须一起看。
            # 没有这两个字段，读者会把 300 池、无护栏的 IR 当成线上成绩。
            "pool_source": serving.POOL_SOURCE,
            "pool_size": len(pool),
            "guardrails": guard_on,
            "guardrails_applied": bool(auto_signal and guard_on),
            "live_parity": bool(serving.POOL_SOURCE == "universe" and auto_signal and guard_on),
            "parity_note": ("与 8003 同池（universe，当前名单；历史窗口有幸存者偏差）+ 同护栏"
                            if serving.POOL_SOURCE == "universe" and auto_signal and guard_on
                            else ("信号由调用方传入，护栏是否已叠加由调用方负责；不能当作线上同口径"
                                  if not auto_signal else
                                  f"票池={serving.POOL_SOURCE}、护栏={'开' if guard_on else '关'}；"
                                  "与线上不一致时不能用本结果替线上背书")),
        },
    )


def score_panel(art, beg: str, end: str, progress_cb=None,
                pool: list[str] | None = None, guardrails: bool = False) -> pd.Series:
    """票池在 [beg, end] 每日的模型分数（MultiIndex: datetime, instrument）。

    `pool` 缺省走 `serving.score_pool`（与 8003 同一个来源），研究脚本要跑固定池时显式传入。
    `guardrails=True` 时逐日剔掉线上那两条名单护栏挡下的票；只用于生产同口径回测，
    四臂 AB 自己会传 signal，不会在这里被重复筛。

    因子窗口要往回多取 `WARMUP_DAYS` 天：Alpha158 里有 60 日窗口的项，从 beg 起取会让
    开头一段全是 NaN。归一化的 mean/std 来自训练产物，不在这里重算。
    """
    days = serving.calendar()
    pool = list(pool or serving.score_pool(art))
    warm_beg = days[max(0, days.index(beg) - serving.WARMUP_DAYS + 1)]
    frame = serving.raw_features(pool, warm_beg, end)
    _tick(progress_cb, 20, "逐日打分")
    pred = pd.Series(art.booster.predict(serving.normalize(frame, art)), index=frame.index)
    pred = pred.reorder_levels(["datetime", "instrument"]).sort_index()
    pred = pred.loc[beg:end]
    if guardrails and serving.guardrails_enabled():
        blocked = _guardrail_panel(pool, beg, end)
        if blocked is not None and not blocked.empty:
            hit = blocked.stack()
            hit.index = hit.index.set_names(["datetime", "instrument"])
            mask = hit.reindex(pred.index).fillna(False).astype(bool)
            pred = pred[~mask]
    return pred


def _guardrail_panel(pool: list[str], beg: str, end: str) -> pd.DataFrame:
    """[beg,end] 每日「被护栏挡下」的布尔宽表（index=日期，columns=代码）。

    与 serving.guardrail_flags 用同一个 guardrails 定义；一次取回整段窗口，
    避免回测逐日调用 D.features 几百次。
    """
    import qlib.data

    days = serving.calendar()
    warm_beg = days[max(0, days.index(beg) - serving.GUARD_WARM)]
    frame = qlib.data.D.features(list(pool), ["$close", "$amount"],
                                 start_time=warm_beg, end_time=end)
    if frame is None or frame.empty:
        raise RuntimeError(f"护栏要的两项行情（{warm_beg}→{end}，{len(pool)} 只）一行都没取到")
    wide = {}
    for field in ("$close", "$amount"):
        w = frame[field].unstack("instrument")
        w.index = pd.to_datetime(w.index)
        wide[field] = w.sort_index()
    blocked = (guardrails.thin_liquidity(wide["$amount"])
               | guardrails.spike_fade(wide["$close"]))
    return blocked.loc[beg:end]


def _make_strategy(hold_rule: str, params: dict, signal: pd.Series):
    """两条纪律各自实例化策略；T−1 日读分数这件事两边都由 qlib 的 shift=1 负责。"""
    from qlib.contrib.strategy import TopkDropoutStrategy

    stop_loss = float(params.get("stop_loss_pct") or 0.0)
    take_profit = float(params.get("take_profit_pct") or 0.0)
    risk_degree = params.get("risk_degree")  # 缺省沿用 qlib 0.95；小数可降仓位、降回撤
    if hold_rule == "dropout":
        if stop_loss or take_profit:
            raise RuntimeError("qlib 止盈止损只支持 hold_rule=band；dropout 没有买入价可跟踪")
        extra = {"signal": signal, "only_tradable": False}
        if risk_degree is not None:
            extra["risk_degree"] = float(risk_degree)
        return TopkDropoutStrategy(topk=int(params["topk"]), n_drop=int(params["n_drop"]), **extra)
    extra = {"stop_loss_pct": stop_loss, "take_profit_pct": take_profit}
    if risk_degree is not None:
        extra["risk_degree"] = float(risk_degree)
    return BandHoldStrategy(topk=int(params["topk"]), entry_rank=int(params["entry_rank"]),
                            exit_rank=int(params["exit_rank"]), signal=signal,
                            entry_gate=params.get("entry_gate"),
                            scan_all=bool(params.get("scan_all", False)), **extra)


def band_plan(scores: pd.Series, held, topk: int, entry_rank: int, exit_rank: int,
              allowed: set | None = None, scan_all: bool = False):
    """「进榜严、出榜松」的名单，返回 `(要卖的代码, 要买的代码)`。

    `scores` 是当日全票池分数（index=代码），`held` 是当前持仓。还在前 `exit_rank` 名的继续拿，
    跌出去的卖掉；空位从**前 `entry_rank` 名**按分高低补，已经拿着的不重复买。
    要求 `exit_rank >= entry_rank`，否则买入候选会把当天刚卖掉的票又买回来（那还是高换手）。

    榜上没有行的票（停牌/断档）算「不在榜」=要卖；真停牌时交易所那关本来就挡单，
    所以这条不会把卖不掉的东西假装成已成交。

    `allowed` / `scan_all` 是批次 O 的入场闸门（docs §18，默认 None / False 时行为与旧版逐字相同）：
    给了 `allowed` 就只有里面的代码能买（不在集合里 = 关门）；`scan_all=True` 时补位扫描
    不限前 `entry_rank` 名，从全榜按分数往下找——卖出/持有永远不看这两个参数。
    """
    ranked = scores.sort_values(ascending=False, kind="mergesort").index.tolist()
    in_band = set(ranked[:exit_rank])
    keep = [code for code in held if code in in_band]
    sells = [code for code in held if code not in in_band]
    slots = max(int(topk) - len(keep), 0)
    keep_set = set(keep)
    scan = ranked if scan_all else ranked[:entry_rank]
    buys = [code for code in scan
            if code not in keep_set and (allowed is None or code in allowed)][:slots]
    return sells, buys


def stop_take_exits(held, entry_px: dict, prices: dict,
                    stop_loss_pct: float = 0.0, take_profit_pct: float = 0.0) -> list[str]:
    """按「相对买入价」的固定止损/止盈挑出要卖的代码。

    只做纯判断，方便单测；价格缺失/非正/不是有限数一律不触发（判不了就不编造成交）。
    阈值是小数（0.08 = -8%），0 或负数表示关闭该边。
    """
    stop = float(stop_loss_pct or 0.0)
    take = float(take_profit_pct or 0.0)
    if stop <= 0 and take <= 0:
        return []
    out: list[str] = []
    for code in held:
        entry, price = entry_px.get(code), prices.get(code)
        if entry is None or price is None:
            continue
        entry, price = float(entry), float(price)
        if not (np.isfinite(entry) and np.isfinite(price)) or entry <= 0 or price <= 0:
            continue
        ret = price / entry - 1.0
        # 92/100 在浮点里是 -7.999999999999996%，阈值边界要能触发
        if (stop > 0 and ret <= -stop + 1e-12) or (take > 0 and ret >= take - 1e-12):
            out.append(code)
    return out


class BandHoldStrategy(BaseSignalStrategy):
    """按 `band_plan` 的名单买卖，撮合与 `TopkDropoutStrategy` 逐行同构：同一个 Exchange、
    同样先卖后买、卖出所得现金按 `risk_degree` 等分给买入名单、涨跌停/停牌一律挡单。

    没走 qlib 的 `WeightStrategyBase`：它按目标权重每天把每只票**再平衡回等权**，
    那笔减仓换手不属于「还在推荐就拿着」这个纪律。

    `entry_gate` 是批次 O 的实验旋钮（docs §18）：一张 (datetime, instrument) 的 0/1 面板，
    在**信号日 F**（= 成交日 T−1，与分数同一信息集）决定该票当天能不能买；NaN/缺行按关门算。
    默认 None 时与旧版逐字相同。闸门只管买入，卖出/持有不受影响。

    `stop_loss_pct` / `take_profit_pct` 按**相对买入价**判断，默认 0 = 关闭，行为与旧版逐字相同。
    日频回测只能拿收盘价判断、按同一个收盘价成交；盘中破位、跳空和跌停卖不掉都不在模型里。
    同一收盘成交偏乐观，但只看收盘触发会漏掉盘中破位，净偏差方向不定，不能当实盘止损价。

    `risk_degree` 是现金缓冲，默认 0.95 = 95% 仓位；改小是**线性压仓位**。
    回撤、超额、换手、成本大致按同一比例缩小；想靠它把回撤打到很低，收益也会跟着掉。
    """

    def __init__(self, *, topk: int, entry_rank: int, exit_rank: int,
                 entry_gate: pd.Series | None = None, scan_all: bool = False,
                 stop_loss_pct: float = 0.0, take_profit_pct: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        self.topk = int(topk)
        self.entry_rank = int(entry_rank)
        self.exit_rank = int(exit_rank)
        self.entry_gate = entry_gate
        self.scan_all = bool(scan_all)
        self.stop_loss_pct = max(0.0, float(stop_loss_pct or 0.0))
        self.take_profit_pct = max(0.0, float(take_profit_pct or 0.0))
        self._entry_px: dict[str, float] = {}

    def generate_trade_decision(self, execute_result=None):
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(trade_step, shift=1)
        pred_score = self.signal.get_signal(start_time=pred_start_time, end_time=pred_end_time)
        if isinstance(pred_score, pd.DataFrame):
            pred_score = pred_score.iloc[:, 0]
        if pred_score is None or pred_score.empty:
            return TradeDecisionWO([], self)

        allowed = None
        if self.entry_gate is not None:
            allowed = set()
            try:
                day_gate = self.entry_gate.xs(pred_start_time, level="datetime")
            except KeyError:
                day_gate = None
            if day_gate is not None:
                allowed = {code for code, value in day_gate.items()
                           if pd.notna(value) and float(value) >= 0.5}

        current = copy.deepcopy(self.trade_position)
        held = current.get_stock_list()
        forced = []
        if self.stop_loss_pct or self.take_profit_pct:
            # 持仓已被卖掉/清零就丢掉买入价，避免把旧成本套到下一次买入上
            for code in list(self._entry_px):
                if code not in held or (current.get_stock_amount(code) or 0.0) <= 0:
                    self._entry_px.pop(code, None)
            prices = {}
            for code in held:
                price = self.trade_exchange.get_deal_price(
                    stock_id=code, start_time=trade_start_time, end_time=trade_end_time,
                    direction=OrderDir.BUY)
                if price is not None and np.isfinite(price) and price > 0:
                    prices[code] = float(price)
                    # 只对「开始跟踪前就持有」的仓位用今天收盘价当成本，当天不触发
                    self._entry_px.setdefault(code, float(price))
            forced = stop_take_exits(held, self._entry_px, prices,
                                     self.stop_loss_pct, self.take_profit_pct)
        band_held = [code for code in held if code not in forced]
        sells, buys = band_plan(pred_score, band_held, self.topk,
                                self.entry_rank, self.exit_rank,
                                allowed=allowed, scan_all=self.scan_all)
        if forced:
            sells = list(dict.fromkeys(forced + sells))
            buys = [code for code in buys if code not in forced]
        cash = current.get_cash()
        orders = []
        for code in sells:
            # direction=None = 涨跌停/停牌都挡：卖不掉就继续拿着，不编造成交
            if not self.trade_exchange.is_stock_tradable(code, start_time=trade_start_time,
                                                         end_time=trade_end_time):
                continue
            order = Order(stock_id=code, amount=current.get_stock_amount(code=code),
                          start_time=trade_start_time, end_time=trade_end_time,
                          direction=Order.SELL)
            if self.trade_exchange.check_order(order):
                orders.append(order)
                trade_val, trade_cost, _ = self.trade_exchange.deal_order(order, position=current)
                cash += trade_val - trade_cost
        value = cash * self.risk_degree / len(buys) if buys else 0.0
        for code in buys:
            if not self.trade_exchange.is_stock_tradable(code, start_time=trade_start_time,
                                                         end_time=trade_end_time):
                continue
            price = self.trade_exchange.get_deal_price(stock_id=code, start_time=trade_start_time,
                                                       end_time=trade_end_time, direction=OrderDir.BUY)
            factor = self.trade_exchange.get_factor(stock_id=code, start_time=trade_start_time,
                                                    end_time=trade_end_time)
            amount = self.trade_exchange.round_amount_by_trade_unit(value / price, factor)
            order = Order(stock_id=code, amount=amount, start_time=trade_start_time,
                          end_time=trade_end_time, direction=Order.BUY)
            if self.trade_exchange.check_order(order):
                orders.append(order)
                if self.stop_loss_pct or self.take_profit_pct:
                    self._entry_px[code] = float(price)
        return TradeDecisionWO(orders, self)


def _tick(progress_cb, percent: int, stage: str) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(percent, stage)
    except Exception:  # noqa: BLE001 - 进度上报失败不能拖垮回测
        pass


def _shares_frame(positions, pool: list[str]) -> pd.DataFrame:
    """qlib 的 positions 收成「日期 × 股票 = 股数」表；认不出来就报错，不给一张空表蒙人。

    实测 `backtest_daily` 返回的是 `{Timestamp(交易日): Position}` 的 dict（不是 Series），
    Position 里带 cash/now_account_value，股数要走 get_stock_amount。
    """
    if isinstance(positions, dict):
        rows = {}
        for day, pos in positions.items():
            if pos is None or not hasattr(pos, "get_stock_list"):
                continue
            rows[pd.Timestamp(day)] = {code: float(pos.get_stock_amount(code) or 0.0)
                                       for code in pos.get_stock_list() if code in pool}
        if not rows:
            raise RuntimeError("qlib 没返回任何持仓记录（positions 里没有 Position 对象）")
        return pd.DataFrame(rows).T.sort_index().reindex(columns=pool).fillna(0.0)
    if isinstance(positions, pd.Series):
        rows = {}
        for day, pos in positions.items():
            if pos is None or not hasattr(pos, "get_stock_list"):
                continue
            rows[day] = {code: float(pos.get_stock_amount(code) or 0.0)
                         for code in pos.get_stock_list() if code in pool}
        if not rows:
            raise RuntimeError("qlib 没返回任何持仓记录（positions 为空）")
        return pd.DataFrame(rows).T.sort_index().reindex(columns=pool).fillna(0.0)
    if isinstance(positions, pd.DataFrame) and not positions.empty:
        frame = positions
        while isinstance(frame.columns, pd.MultiIndex) and frame.columns.nlevels > 1:
            frame = frame.droplevel(0)
        return frame.sort_index().reindex(columns=pool).fillna(0.0)
    raise RuntimeError(f"不认识 qlib positions 的类型：{type(positions)}")


def _close_panel(pool: list[str], beg: str, end: str) -> pd.DataFrame:
    import qlib.data

    frame = qlib.data.D.features(list(pool), ["$close"], start_time=beg, end_time=end)["$close"]
    wide = frame.unstack("instrument")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _trades(shares: pd.DataFrame, closes: pd.DataFrame) -> list[dict]:
    """持仓变动 → 成交明细（code/name/side/date/price/shares/amount/fee/pnl）。"""
    out: list[dict] = []
    cost: dict[str, float] = {}  # 代码 → 持股加权均价（卖一次清一次）
    days = shares.index.intersection(closes.index)
    for i, day in enumerate(days):
        held = shares.loc[day]
        prev = shares.loc[days[i - 1]] if i else pd.Series(0.0, index=shares.columns)
        delta = (held - prev).fillna(0.0)
        for code, change in delta.items():
            if not change:
                continue
            price = closes.at[day, code] if code in closes.columns else np.nan
            if pd.isna(price) or price <= 0:
                continue  # 停牌/没数据：不知道成交价就不编造成交
            change = float(change)
            buy = change > 0
            amount = abs(change) * float(price)
            fee = amount * (EXCHANGE_KWARGS["open_cost"] if buy else EXCHANGE_KWARGS["close_cost"])
            pnl = None
            if buy:
                lots = cost.get(code, 0.0)
                qty = float(prev.get(code, 0.0))
                cost[code] = (qty * lots + change * float(price)) / (qty + change) if (qty + change) > 0 else float(price)
            else:
                pnl = abs(change) * (float(price) - cost.get(code, float(price))) - fee
                if abs(held.get(code, 0.0)) < 1e-9:
                    cost.pop(code, None)
            out.append({
                "code": str(code)[2:],
                "name": "",
                "side": "buy" if buy else "sell",
                "date": str(day)[:10],
                "price": float(price),
                "shares": abs(change),
                "amount": amount,
                "fee": fee,
                "pnl": pnl,
            })
    return out


def _snap_window(days: list[str], req_beg: str, req_end: str) -> tuple[str, str]:
    """把页面选的窗口夹进 qlib 交易日历，返回实际撮合的首末交易日。

    页面能选到周末/节假日（自研内核是按「有数据的行」取数的），所以这里不能要求它正好是交易日。
    末尾还要退一根：qlib 每个决策日要取「下一根 K 线」当区间右端点
    （backtest/utils.py:131 `self._calendar[calendar_index + 1]`），
    撞上日历最后一个交易日就 IndexError——那一天的分数只进选股名单、不参与撮合。
    """
    in_window = [d for d in days if req_beg <= d <= req_end]
    if not in_window:
        raise RuntimeError(f"{req_beg}→{req_end} 之间一个交易日都没有（面板没更新到这段？）")
    beg, end = in_window[0], in_window[-1]
    if end == days[-1]:
        if len(in_window) < 2:
            raise RuntimeError(f"{req_beg}→{req_end} 只剩日历最后一个交易日，没法撮合")
        end = days[days.index(end) - 1]
    return beg, end


def _account_curve(report: pd.DataFrame, capital: float) -> pd.Series:
    """账户净值序列（现金+持仓市值，成本已扣）。缺这列就直接报错，绝不拿毛收益冒充净值。"""
    if "account" not in report.columns:
        raise RuntimeError(f"qlib 报表里没有 account 列（拿到的是 {list(report.columns)}），无法算净收益")
    curve = report["account"].astype(float).dropna().sort_index()
    if curve.empty:
        raise RuntimeError("qlib 的 account 列全是空值，回测没跑出净值")
    return curve


def _metrics(report: pd.DataFrame, values: pd.Series, capital: float, trades: list[dict],
             name_days: int | None = None) -> dict:
    """把 qlib 日报表翻译成自研内核的键名；净值/回撤/夏普一律走 values（已扣成本）。"""
    nav = values / capital
    net = nav.pct_change().fillna(0.0)
    n = int(len(net))
    total = float(nav.iloc[-1] - 1.0)
    gross = float((1.0 + report["return"].fillna(0.0)).prod() - 1.0)
    dd = float((nav / nav.cummax() - 1.0).min())
    sharpe = float(net.mean() / net.std() * np.sqrt(TRADING_DAYS)) if float(net.std()) else None
    bench = report["bench"] if "bench" in report.columns else report.get("benchmark")
    bench_total = float((1.0 + bench.dropna().fillna(0.0)).prod() - 1.0) if bench is not None else None
    sells = [t["pnl"] for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    wins = [p for p in sells if p > 0]
    losses = [p for p in sells if p <= 0]
    annual = float((1.0 + total) ** (TRADING_DAYS / n) - 1.0) if n else None
    annual_bench = (float((1.0 + bench_total) ** (TRADING_DAYS / n) - 1.0)
                    if bench_total is not None and n else None)
    turnover = report["turnover"] if "turnover" in report.columns else None
    cost_rate = report["cost"] if "cost" in report.columns else None
    out = {
        "total_return": round(total, 6),
        "annual_return": None if annual is None else round(annual, 6),
        "max_drawdown": round(dd, 6),
        "sharpe": None if sharpe is None else round(sharpe, 4),
        "win_rate": round(len(wins) / len(sells), 6) if sells else None,
        "payoff_ratio": _payoff(wins, losses),
        "profit_factor": _pf(wins, losses),
        "avg_win": round(float(np.mean(wins)), 2) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 2) if losses else None,
        "expectancy": round(float(np.mean(sells)), 2) if sells else None,
        "trade_count": len(trades),
        "sell_count": len(sells),
        "days": n,
        "benchmark_return": None if bench_total is None else round(bench_total, 6),
        "annual_excess": (None if annual is None or annual_bench is None
                          else round(annual - annual_bench, 6)),
        "turnover_daily": None if turnover is None else round(float(turnover.mean()), 6),
        "cost_daily": None if cost_rate is None else round(float(cost_rate.mean()), 6),
        "cost_annualized": None if cost_rate is None else round(float(cost_rate.mean()) * TRADING_DAYS, 6),
        # 毛利是 qlib 的 return 列（把手续费加回去的那一版），和上面的净值并排放，谁也别糊弄谁
        "gross_return": round(gross, 6),
        "start_value": capital,
        "end_value": round(float(values.iloc[-1]), 2),
        # 平均持有天数 = 持仓票·日 / 卖出笔数（期末还拿着的票没有卖出记录，会略微高估）
        "avg_hold_days": (None if not name_days or not sells
                          else round(name_days / len(sells), 2)),
    }
    return out


def _payoff(wins: list[float], losses: list[float]):
    if not wins or not losses:
        return None
    loss = abs(float(np.mean(losses)))
    return round(float(np.mean(wins)) / loss, 4) if loss else None


def _pf(wins: list[float], losses: list[float]):
    """盈利因子：无亏损笔时返回字符串（自研内核同一个口径，inf 不是 JSON）。"""
    if not wins or not losses:
        return "inf" if wins else None
    loss = abs(float(np.sum(losses)))
    return round(float(np.sum(wins)) / loss, 4) if loss else "inf"
