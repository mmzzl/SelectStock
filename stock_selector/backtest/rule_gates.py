"""rule_gates.py — 候选判据（卖出/加仓表达式）生效之前必须过的门槛，阈值只有这一处真源。

为什么要写死在代码里、不给页面调（2026-10-16）：可调的门槛等于没有门槛。这次的读数就是证据——
78 个预处理字段随便两两组合都能跑出「lift 1.2、p 很小」的东西，而 2026-10-16 全市场回放里
「全部卖出规则都不触发」这个状态的 lift 只有 1.011（N=927 万个个股·日、配对 t=8.1），
样本大到任何效应都能显著，所以显著性必须用**多重比较后的硬阈值**判，不能看 p 值是否 <0.05。
`出场 0/14`（14 条候选退出规则全部没打赢基线，见 README 与 [[selectstock-payoff-target-audit]]）
是同一件事的另一种说法：没有预注册的筛子，探索出来的规则基本都活不过第二次抽样。

三道纪律：
1. **候选必须带机制说明**。说不出「为什么这条成立」的表达式不进队列——代码能验字符串非空，
   验不了机制真假，所以这里做的是**抬高写理由的成本**，把「拉字段下拉碰运气」这条路堵掉。
2. **候选数量有上限**（`MAX_CANDIDATES`）。一次跑几百条等于把多重比较的预算花光，
   按 Bonferroni 反推，阈值就得跟着上调；固定几十条、跑完不加不减，才是 `MIN_T` 能写死的前提。
3. **筛选段与留出段分开**（见 `scripts/ruleset_posterior.py --to`）。三段同号能挡「只靠某个年代」，
   挡不住「在同一份数据上试了 500 次总有一个对」；留出段（2022 年起）只允许被读一次。

这里只放**阈值和判定函数**，不放回放/统计本身：数怎么算仍在 `ruleset_posterior.py`（日配对）
和 `build_judgement_ledger.py`（点级 + 尾部预警），两边都从这里取门槛，才不会各判各的。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .rule_expr import RuleSyntaxError, validate

SIDES = ("sell", "add")

# ① 样本量：低于这个量级就别离「几天行情撑起来的假象」了（联合状态原来只有 200，
#    那是给「能不能显示一个数字」用的；判一条判据能不能生效要更狠）
MIN_POINTS = 10_000       # 触发的个股·日
MIN_DAYS = 200            # 覆盖的交易日

# ② 日配对显著性：|t| ≥ 4 ≈ 几百次比较下的 Bonferroni 5% 家族错误率（z(2.5e-5) ≈ 3.9）。
#    用 2 的话，20~30 条候选里必出「显著」的假信号。
MIN_T = 4.0

# ④ 效应量下限：一轮卖出+买回的成本（佣金万 2.5 双边 + 印花税 0.05% 卖出 + 过户费 + 预估滑点），
#    与 build_judgement_ledger 用的是同一个数，两边换算钱的尺子必须一样。
ROUND_TRIP_PP = 0.35

# ⑤ 触发频率上限：年均每票触发多少天。A 股 T+1 + 资金上限，一年响 30 天以上的退出规则
#    执行不了；用户自己划过这条边界（「推送有点频繁了，不能频繁买入和卖出」）。
MAX_TRIGGER_DAYS_PER_YEAR = 30.0
TRADING_DAYS_PER_YEAR = 245

# 机制说明至少这么长：「rsi高」这种四个字等于没写。
MIN_WHY_CHARS = 8

# 一次筛查允许的最大候选数（多重比较预算）
MAX_CANDIDATES = 40

GATE_KEYS = ("sample", "direction", "t", "era", "cost", "freq")
GATE_NAMES = {
    "sample": "样本量",
    "direction": "方向",
    "t": "日配对 t",
    "era": "三段时代同号",
    "cost": f"扣一轮换手 {ROUND_TRIP_PP}pp",
    "freq": "触发频率",
}


# —— 逐笔盈亏门槛（`scripts/profit_scan.py` 用；与上面的状态级六道筛是两套，别混用）——
# 状态级判的是「这个状态之后会不会跌」，逐笔判的是「交易这个信号扣完费赚不赚钱」，
# 问的不是一个问题，所以这里不复用 lift/t 那几道。
TRADE_MIN_TRADES = 300        # 笔数下限（用户 2026-10-16 定的口径）
TRADE_MIN_WIN_RATE = 0.50     # 胜率必须过半：过低就是靠极少数大赚撑盈亏比
TRADE_MIN_PAYOFF = 1.0        # 均盈/均亏 > 1
TRADE_MIN_NET_PP = 0.0        # 每笔扣费后 > 0，「只要赚钱都行，不需要赚太多」
TRADE_GATE_KEYS = ("trades", "win", "payoff", "net", "sig")
TRADE_GATE_NAMES = {
    "trades": "笔数",
    "win": "胜率",
    "payoff": "盈亏比",
    "net": "每笔扣费后",
    "sig": "防自欺（按尝试次数）",
}


def other_side(side: str) -> str:
    """候选的 side 是人猜的，猜错时数据会自己纠正：筛查里两条边都判一遍，
     declared 不过而对面过了就报「反侧」，别让它静悄悄躺在「不过」那堆里。"""
    if side not in SIDES:
        raise ValueError(f"side 只能是 {' 或 '.join(SIDES)}，收到 {side!r}")
    return "add" if side == "sell" else "sell"


def expected_sign(side: str) -> int:
    """卖出判据要圈中「后来更差」的点（−1），加仓判据要圈中「后来更好」的点（+1）。"""
    if side not in SIDES:
        raise ValueError(f"side 只能是 {' 或 '.join(SIDES)}，收到 {side!r}")
    return -1 if side == "sell" else 1


def parse_eras(text) -> list[float | None]:
    parts = str(text or "").split(",")
    out: list[float | None] = []
    for p in parts:
        p = p.strip()
        try:
            v = float(p)
        except ValueError:
            out.append(None)
            continue
        out.append(v if v == v else None)      # 'nan' 能转成 float，但要当成「没读数」
    return out


def _check(key: str, need: str, got: str, ok: bool) -> dict:
    # 两套门槛共用这个构造函数，所以按 key 现查名字，别让逐笔那五条撞进状态级的字典里
    return {"key": key, "name": {**GATE_NAMES, **TRADE_GATE_NAMES}[key],
            "need": need, "got": got, "ok": ok}


def evaluate_state(stat: dict, side: str, trigger_days_per_year: float) -> dict:
    """对一条候选的后验读数判六道门槛；返回 verdict ∈ {过, 不过, 不判}。

    `stat` 是 `ruleset_posterior.summarize()` 的产物。样本量那一道先判——量不够就是「不配」；
    量够但没有 `lift`/`vs_base_t`（基线状态在这些日子里没数，脚本刻意不产出配对读数）才判「不判」：
    前者是数据说它不行，后者是这份数据说不了话，混起来会让人以为候选已被否掉。
    """
    want = expected_sign(side)
    n, days = int(stat.get("n") or 0), int(stat.get("days") or 0)
    checks = [_check("sample", f"n≥{MIN_POINTS:,} 且 日≥{MIN_DAYS}",
                     f"n={n:,}/{days}日",
                     n >= MIN_POINTS and days >= MIN_DAYS)]
    if not checks[0]["ok"]:
        return {"verdict": "不过", "failed": ["sample"], "checks": checks, "note": ""}

    lift, t = stat.get("lift"), stat.get("vs_base_t")
    exc, eras = stat.get("exc20_pp"), parse_eras(stat.get("era_flat_diff_pp"))
    if lift is None or t is None:
        return {"verdict": "不判", "failed": [], "checks": checks,
                "note": f"配对读数缺失（只覆盖 {days} 个交易日），这份数据说不了话"}

    checks.append(_check("direction", f"lift {'>' if want > 0 else '<'} 1",
                         f"lift {lift:.3f}",
                         lift > 1 if want > 0 else lift < 1))
    checks.append(_check("t", f"{'正' if want > 0 else '负'} 且 |t|≥{MIN_T}",
                         f"t {t}", (t * want) >= MIN_T))
    same_sign = bool(eras) and len(eras) == 3 and all(
        e is not None and e * want > 0 for e in eras)
    checks.append(_check("era", "三段同号且与方向一致",
                         str(stat.get("era_flat_diff_pp") or ""), same_sign))
    if exc is None:
        checks.append(_check("cost", f"|20日超额| ≥ {ROUND_TRIP_PP}pp 且与方向一致", "无", False))
    else:
        checks.append(_check("cost", f"|20日超额| ≥ {ROUND_TRIP_PP}pp 且与方向一致",
                             f"{exc:+.2f}pp", exc * want >= ROUND_TRIP_PP))
    checks.append(_check("freq", f"年均每票触发 ≤ {MAX_TRIGGER_DAYS_PER_YEAR:g} 天",
                         f"{trigger_days_per_year:.1f} 天/票·年",
                         trigger_days_per_year <= MAX_TRIGGER_DAYS_PER_YEAR))

    failed = [c["key"] for c in checks if not c["ok"]]
    return {"verdict": "不过" if failed else "过", "failed": failed, "checks": checks, "note": ""}


def _inv_norm_sf(p: float) -> float:
    """上侧概率 p 对应的 z（即 P(Z>z)=p）。用二分而不是引 scipy：这台机器上脚本链没带它，
    而且这个函数一分钟只会被调几次，精度到 1e-4 就够写进报告了。"""
    if not 0.0 < p < 0.5:
        raise ValueError(f"p 必须在 (0, 0.5) 之间，收到 {p:g}")
    lo, hi = 0.0, 12.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2.0)) > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def t_required(n_trials: int) -> float:
    """试了 K 个组合之后，第 K+1 个要多大的 t 才不像是白捡的（单侧 5%，Bonferroni）。

    掷骰子掷 10 次没出六个点不稀奇，掷 800 万次一定能掷出一串——试的次数越多及格线必须越严，
    否则「扫得越广找到越多好规则」就只是噪声被喂大的假象。
    """
    if n_trials < 1:
        raise ValueError(f"n_trials 至少 1，收到 {n_trials}")
    return _inv_norm_sf(0.05 / n_trials)


def evaluate_trade(stat: dict, n_trials: int) -> dict:
    """判一条「买入条件 × 卖法」的逐笔盈亏够不够格；返回 verdict ∈ {过, 不过, 不判}。

    `stat` 由 `scripts/profit_scan.py` 汇总：`n_trades`、`win_rate`、`payoff`（均盈/均亏）、
    `net_per_trade_pp`（扣费后每笔，百分点）、`t_stat`（每笔收益的日级配对 t）、
    `era_net_pp`（三段各赚多少，**只展示不判**——用户明确说过不需要每个时代都赚钱）。
    笔数那道先判：量不够就是「不配」；量够但算不出 t（覆盖交易日太少）才是「不判」。
    """
    n = int(stat.get("n_trades") or 0)
    checks = [_check("trades", f"≥{TRADE_MIN_TRADES}", f"{n:,} 笔", n >= TRADE_MIN_TRADES)]
    if not checks[0]["ok"]:
        return {"verdict": "不过", "failed": ["trades"], "checks": checks, "note": ""}

    wr, po, net, t = stat.get("win_rate"), stat.get("payoff"), stat.get("net_per_trade_pp"), stat.get("t_stat")
    if t is None or net is None or wr is None:
        return {"verdict": "不判", "failed": [], "checks": checks,
                "note": "逐笔读数算不出来（覆盖交易日太少或全是单侧结果），这份数据说不了话"}

    checks.append(_check("win", f">{TRADE_MIN_WIN_RATE:.0%}", f"{wr:.1%}", wr > TRADE_MIN_WIN_RATE))
    # 一笔都不亏时均亏是 0，payoff 是 ∞：这是真好看而不是缺数，别判成「不判」
    po_ok = po is None or math.isinf(po) or po > TRADE_MIN_PAYOFF
    checks.append(_check("payoff", f">{TRADE_MIN_PAYOFF:g}",
                         "∞（没有亏损笔）" if po is None or math.isinf(po) else f"{po:.2f}", po_ok))
    checks.append(_check("net", f"> {TRADE_MIN_NET_PP:g}pp", f"{net:+.3f}pp", net > TRADE_MIN_NET_PP))
    need_t = t_required(n_trials)
    checks.append(_check("sig", f"试了 {n_trials:,} 次，要 t≥{need_t:.2f}", f"t {t:.2f}", t >= need_t))

    failed = [c["key"] for c in checks if not c["ok"]]
    return {"verdict": "不过" if failed else "过", "failed": failed, "checks": checks, "note": ""}


def load_candidates(path: Path) -> list[dict]:
    """读候选清单 JSON，逐条校验：side 合法、表达式能过线上同一套文法、机制说明够长。

    清单格式：
        {"name": "...", "candidates": [
            {"id": "ma10_5", "side": "sell", "label": "跌破10日线5%",
             "expr": "close <= ma10 * 0.95", "why": "机制说明（必填）"}]}
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("candidates") or []
    if len(raw) > MAX_CANDIDATES:
        raise ValueError(f"候选 {len(raw)} 条超过上限 {MAX_CANDIDATES}：一次比太多，"
                         "「显著」里有多少是白捡的就没法说了")
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, c in enumerate(raw, start=1):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i} 条候选不是对象")
        side = str(c.get("side") or "").strip()
        if side not in SIDES:
            raise ValueError(f"第 {i} 条候选 side={side!r} 非法，只能是 sell 或 add")
        expr = str(c.get("expr") or "").strip()
        if not expr:
            raise ValueError(f"第 {i} 条候选没有 expr")
        try:
            fields = validate(expr)
        except RuleSyntaxError as exc:
            raise ValueError(f"第 {i} 条候选的表达式不合法：{exc}") from exc
        if not fields:
            raise ValueError(f"第 {i} 条候选的表达式没引用任何字段：{expr!r}")
        why = str(c.get("why") or "").strip()
        if len(why) < MIN_WHY_CHARS:
            raise ValueError(f"第 {i} 条候选缺少机制说明（至少 {MIN_WHY_CHARS} 字，"
                             "「为什么这条成立」写不出来就不要进队列）")
        cid = str(c.get("id") or f"c{i}")
        if cid in seen_ids:
            raise ValueError(f"候选 id 重复：{cid}")
        seen_ids.add(cid)
        out.append({"id": cid, "side": side,
                    "label": str(c.get("label") or cid)[:40], "expr": expr,
                    "why": why, "fields": fields})
    if not out:
        raise ValueError(f"{path} 里没有候选")
    return out


def trigger_days_per_year(n_points: int, judged_points: int) -> float:
    """触发占比 × 245：把「多少个股·日在响」换算成「一年替你把一只票响多少天」。

    这是上限口径——它假设持仓覆盖所有时点。真实持仓只有几只，天天在响的判据只会更早撞到
    T+1 和资金上限，所以按上限卡不会放过执行不了的规则。
    """
    if judged_points <= 0:
        return 0.0
    return n_points / judged_points * TRADING_DAYS_PER_YEAR
