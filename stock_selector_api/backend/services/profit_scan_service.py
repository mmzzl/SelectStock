"""services/profit_scan_service.py — 逐笔盈亏扫描结果的展示层（只读，零计算）。

红线照旧：一个百分点都不在这儿重算。数全部来自 `scripts/profit_scan.py` 写进 Mongo 的结果，
这里只做两件事 —— 拼筛选/分页（在 repositories 里），和把冷字段翻成大白话（给用户的口径
2026-10-16 定过：先报「每笔期望/胜率/盈亏比/笔数」四件，别单报 lift 或「会不会跌」）。

`1pp` 的换算用 run 里存的单笔金额，不在页面硬编码；金额改了重跑即可，文案跟着走。
"""
from __future__ import annotations

from core.exceptions import NotFoundError
from repositories import profit_scan as repo
from core.rule_grammar import trade_gates

# 表头口径：页面顶部一次说清「这批数是怎么来的」，否则读者会把扣费前/后、按日/按笔搞混
SCOPE_DOC = [
    "买入时点：信号当天收盘后才确认，次日开盘才买，开盘顶到涨停买不进就放弃这一笔。",
    "卖出：只有三种机械动作 —— 止损、止盈、拿满 N 天。买入当天不能卖（A 股 T+1），"
    "「持有第几天」从买入日往后数；同一天既够止盈又够止损时按止损算（保守）。"
    "一字板/停牌没有对手盘就顺延到后面第一个能卖的日子，顺延不出来这一笔直接不算。",
    "扣后每笔：一轮手续费按 0.30pp 从每笔里扣掉后，平均每笔赚/亏多少个点（pp=百分点）。",
    "t 值：把同一天的所有票当一个样本算的显著性（按日聚类），它比逐笔 t 严谨，判定用它；"
    "逐笔 t 只作对照。试的组合越多，及格线抬得越高（防「掷三千次骰子总有连六点」）。",
    "封存段（2022 年以后）不参与挑选，挑完只验一次。",
    "票池：只有股票（场内基金/ETF 按代码段剔掉），且买入那天这只票在本库里已有足够多根日线"
    "（次新股的博弈挡在门外）、门槛前后的格子数一起报出来，别只看过筛的那几个。",
]

VERDICT_TIP = {"过": "四道门槛 + 显著性都过了",
               "不过": "至少一道门槛没过，看「卡在」那列",
               "不判": "笔数太少或没有成交，判不了"}


def _pp_money(run: dict, v) -> str:
    """把 pp 翻成钱：用这次跑设定的单笔金额，1pp = 金额/100 元。"""
    amt = run.get("trade_amount") or 0
    if not amt or v is None:
        return ""
    return f"≈{v * amt / 100:+,.0f} 元/笔"


def _num(v, nd: int = 3, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:+.{nd}f}{suffix}" if suffix == "pp" else f"{v:.{nd}f}{suffix}"


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _payoff(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _failed_text(failed) -> str:
    names = {"trades": "笔数", "win": "胜率", "payoff": "盈亏比", "net": "扣后每笔", "sig": "显著性"}
    return "、".join(names.get(k, k) for k in (failed or [])) or "—"


# ------------------------------------------------------------------ 层零：run 与口径

async def resolve_run(run_id: str = "") -> dict:
    run = await repo.latest_run(run_id)
    if not run:
        raise NotFoundError("还没有扫描结果，先在服务器上跑 scripts/profit_scan.py")
    return run


def _run_line(d: dict) -> str:
    stocks = d.get("stocks") or d.get("files") or "?"
    # 及格线按「累计试过多少个组合」抬，不是每轮重置；10-17 之前的 run 没这个字段，按本轮算
    total = d.get("n_trials_total") or d["n_trials"]
    return (f"{d['window'][0]}→{d['window'][1]}（{d['n_days']} 个可买日）｜"
            f"{stocks} 票 × {d['n_conditions']} 条件 × {d['n_profiles']} 卖法 "
            f"= {d['n_trials']:,} 组合｜过 {d['n_pass']} 个｜累计试过 {total:,} 次、要 t≥{d['t_required']}｜"
            f"每笔扣 {d['cost_pp']}pp（{d.get('trade_amount', 0):,} 元/笔）｜耗时 {d['elapsed_s']}s")


def _pool_note(d: dict) -> str:
    p = d.get("pool") or {}
    if not p:
        return ("这一轮还没装票池门槛（2026-10-17 之前跑的）：池子里混着场内基金，"
                "次新股的涨跌也一并算在平均数里，看数时要留个心。")
    live, ok = p.get("cells_live") or 0, p.get("cells_ok") or 0
    cut = 1 - ok / live if live else 0.0
    amount = (f"、近 {p['amount_window_days']} 日日均成交额 ≥ {p['min_amount']:,.0f} 元"
              if p.get("min_amount") else "")
    return (f"票池：按代码段剔掉场内基金 {p.get('dropped_funds', 0):,} 只；"
            f"可买格子里门槛（上市 ≥{p.get('min_tenure', 0)} 根日线{amount}）"
            f"剔掉 {cut:.1%}（{live:,} → {ok:,}），"
            f"{p.get('stocks_ok', 0):,} 只票至少有一段能下单。"
            f"基准和买点用的是同一个池子，所以「比基准超额」不是拿池子外的时点比的。")


async def runs(limit: int = 20) -> list[dict]:
    docs = await repo.list_runs(limit)
    for d in docs:
        d["summary_line"] = f"{d['run_id']}｜{_run_line(d)}"
    return docs


async def overview(run_id: str = "") -> dict:
    run = await resolve_run(run_id)
    stats = await repo.run_stats(run["run_id"])
    f = run.get("fills") or {}
    cells = max(1, f.get("cells") or 1)
    run["summary_line"] = _run_line(run)
    run["scope_doc"] = SCOPE_DOC
    run["verdict_tip"] = VERDICT_TIP
    run["pool_note"] = _pool_note(run)
    run["fill_note"] = (f"有行情的下单格子 {f.get('cells', 0):,} 个：次日没有行情（停牌/退市）"
                        f"{f.get('no_next_quote', 0):,} 个（{f.get('no_next_quote', 0) / cells:.2%}），"
                        f"次日开盘顶涨停或一字板买不进 {f.get('buy_blocked', 0):,} 个"
                        f"（{f.get('buy_blocked', 0) / cells:.2%}）。"
                        f"到期卖不掉的那部分按行记在「出不掉」列里，不假装赚到了。")
    run["stats"] = stats
    return run


# ------------------------------------------------------------------ 层一：一条条件一行

async def rules(run_id: str = "", *, kind: str = "", q: str = "", verdict: str = "",
                min_trades: int | None = None, page: int = 1, size: int = 20,
                order: str = "max_net_pp") -> dict:
    run = await resolve_run(run_id)
    rid = run["run_id"]
    min_trades = (trade_gates().TRADE_MIN_TRADES if min_trades is None else min_trades)
    out = await repo.list_rules(rid, kind=kind, q=q, verdict=verdict, min_trades=min_trades,
                                page=page, size=size, order=order)
    run["fold_note"] = (f"每条条件把 {run['n_profiles']} 种卖法折成一行，「最好的卖法」只在笔数满 "
                        f"{min_trades} 的卖法里挑（笔数不够的卖法再漂亮也不算数）")
    for d in out["items"]:
        n = d.get("best_net_trades") or 0
        money = _pp_money(run, d["max_net_pp"])
        verdict_txt = f"有 {d['n_pass']} 种卖法四道门槛全过" if d["n_pass"] \
            else f"没过：{_failed_text(d['best_failed'])}"
        d["summary_line"] = (
            f"最好的卖法 {d['best_profile']}：扣后每笔 {_num(d['max_net_pp'], 3, 'pp')}"
            f"{('（' + money + '）') if money else ''}，胜率 {_pct(d['best_win_rate'])}，"
            f"盈亏比 {_payoff(d['best_payoff'])}，{n:,} 笔 —— "
            f"满 {min_trades} 笔的卖法有 {d['n_profiles']} 种，{verdict_txt}")
    return {"run": run, "items": out["items"], "total": out["total"]}


# ------------------------------------------------------------------ 层二 / 层三

async def profiles(run_id: str, cond_id: str, order: str = "net_per_trade_pp") -> dict:
    run = await resolve_run(run_id)
    rows = await repo.list_profiles(run["run_id"], cond_id, order)
    for r in rows:
        ex = r.get("excess_pp")
        r["summary_line"] = (f"扣后每笔 {_num(r['net_per_trade_pp'], 3, 'pp')}，"
                             f"胜率 {_pct(r['win_rate'])}，盈亏比 {_payoff(r['payoff'])}，"
                             f"{r['n_trades']:,} 笔；"
                             f"对照：同样这个卖法在任意时点瞎买的平均是 "
                             f"{_num(r.get('bench_net_pp'), 3, 'pp')}，"
                             f"这条条件挑出来的时点比它{'高' if (ex or 0) > 0 else '低'} "
                             f"{_num(abs(ex) if ex is not None else None, 3, 'pp')}")
    return {"run": run, "cond_id": cond_id, "items": rows}


def _why_lines(w: dict, row: dict) -> list[dict]:
    """把 8 项归因翻成「一句结论 + 那行的数」，措辞只复述数字，不给因果故事。"""
    cur = w.get("day_curve_pp") or {}
    curve = "、".join(f"第 {k} 天 {_num(v, 2, 'pp')}" for k, v in
                      sorted(cur.items(), key=lambda x: int(x[0])))
    br = w.get("by_breadth_quintile_pp") or []
    ov = w.get("most_overlapping") or []
    mb = w.get("month_block_boot") or {}
    ste = w.get("sell_too_early_pp")
    out = [
        {"title": "收益落在持有第几天", "value": curve or "—",
         "read": "这是买入后第 k 天的毛收益（没扣手续费）。还在往上长就说明被止盈/到期截断了。"},
        {"title": "卖早了还是卖对了",
         "value": ("—" if ste is None else
                   f"{'卖早了' if ste > 0 else '按这套卖法更划算'}，差 {abs(ste):.3f}pp/笔"),
         "read": "拿满 20 天不动（毛）对比本行扣后每笔：正数=规则少赚，负数=规则多赚。"},
        {"title": "最赚的那几笔占了多少利润", "value": _pct(w.get("top_share_of_gain")),
         "read": "占比高（比如超过 30%）就是靠几只妖股撑的，去掉它们基本不剩。"},
        {"title": "中位数那笔", "value": _num(w.get("median_net_pp"), 3, "pp"),
         "read": "平均值被大赚/大亏拉偏，中位数才是「典型一笔」。止盈档会把典型一笔压到止盈线附近。"},
        {"title": "市场强弱分五档，各档每笔扣后", "value":
            "、".join(f"{i + 1} 档 {_num(v, 2, 'pp') if v is not None else '—'}"
                     for i, v in enumerate(br)) or "—",
         "read": "1 档=当天上涨家数最少，5 档=最多。只在某一档赚钱 ⇒ 那是行情开关，不是这条条件。"},
        {"title": "按月份整块重抽 200 次",
         "value": (f"{mb.get('positive_pct', 0):.0f}% 的重抽为正，最差一成情形 "
                   f"{_num(mb.get('p10_pp'), 2, 'pp')}" if mb else "—"),
         "read": "比例低说明这个数是某几个月撑起来的，换个时间段就可能翻负。"},
        {"title": "跟哪些条件最像（信号重复）",
         "value": "、".join(f"{o['label']}（重合 {o['jaccard'] * 100:.1f}%）" for o in ov) or "—",
         "read": "重合高 ⇒ 同一段行情换了个名字，别当两条独立证据。"},
        {"title": "三段时代分别扣后", "value": (row.get("era_net_pp") or "—").replace(",", " / ")
         + "pp（2015-16 / 2017-19 / 2020-21）",
         "read": "定的口径不要求每段都赚，但三段一路往下衰减，说明最近这段的每笔比全样本小得多。"},
    ]
    if w.get("note"):
        out.append({"title": "这份「为什么」的边界", "value": w["note"], "read": ""})
    return out


async def row_detail(run_id: str, cond_id: str, profile: str) -> dict:
    run = await resolve_run(run_id)
    r = await repo.get_row(run["run_id"], cond_id, profile)
    if not r:
        raise NotFoundError(f"没找到 {cond_id} × {profile} 这一行")
    checks = [{"key": c["key"], "name": c["name"], "need": c["need"], "got": c["got"],
               "ok": bool(c["ok"])} for c in (r.get("checks") or [])]
    w = r.get("why") or {}
    r["four_numbers"] = (f"每笔扣后 {_num(r['net_per_trade_pp'], 3, 'pp')}"
                         f"｜胜率 {_pct(r['win_rate'])}｜盈亏比 {_payoff(r['payoff'])}"
                         f"｜{r['n_trades']:,} 笔")
    r["why_lines"] = _why_lines(w, r) if w else []
    r["checks"] = checks
    r["verdict_tip"] = VERDICT_TIP.get(r.get("verdict") or "", "")
    return {"run": run, "row": r}


async def options(run_id: str = "") -> dict:
    """筛选下拉：卖法档 + 条件类别，都从结果库里现取，别在页面写死。"""
    run = await resolve_run(run_id)
    rid = run["run_id"]
    return {"run": run, "profiles": await repo.profile_options(rid),
            "kinds": await repo.kind_options(rid)}
