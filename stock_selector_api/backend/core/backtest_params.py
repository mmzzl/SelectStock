"""回测参数白名单、缺省值与校验。

后端只做入参校验与投递（不落任何回测计算），worker 用同一份定义把 JSON 参数
组装成内核 CLI 的 Namespace。两处共用一份，避免加参数时只改一边导致
「前端能填、回测跑不到」。
"""
from __future__ import annotations

import re

from core.exceptions import BizError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 缺省值与 scripts/backtest_main.py 的 argparse 默认值保持一致
DEFAULTS: dict = {
    "start": "2025-09-01",
    "end": "2026-09-01",
    "capital": 100000.0,
    "max_positions": 5,
    "position_cap_pct": 0.2,
    "target_amount": 15000.0,
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax_rate": 0.0005,
    "strategy": None,        # 空 = 走 MongoDB trading_rules 规则回测
    "universe": 0,           # 仅 strategy 模式生效，0 = 全市场
    "max_hold_days": 10,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.0,
    "no_save": False,
    # 规则集快照：页面「回测」把当前生效的卖出/加仓串带进这一次回测（只在 rules 模式生效，
    # 即 strategy 为空）。带着串跑策略模式会被内核静默忽略，所以那种组合在 validate 里直接拒。
    "rule_set_id": "",
    "sell_rule": "",
    "add_rule": "",
    # 引擎开关：回测没有 HTTP 插件缝（Celery 任务名 backtest.run 就是那条缝），
    # 所以「换插件」在这里表现为一个参数，worker 按它 import 不同内核。
    "engine": "self",       # self = scripts/backtest_main.py；qlib = stock_selector_qlib.backtest
    # 默认按线上「只买最高分一只」的口径；要看多票组合再显式传 topk。
    "topk": 1,              # 仅 engine=qlib：持仓只数
    "n_drop": 5,            # 仅 engine=qlib（hold_rule=dropout）：每日强制换掉最低分的几只
    "benchmark": "SH510300",  # 仅 engine=qlib：qlib 面板里的基准（指数 399xxx 只有 2025 年后数据）
    # 持仓纪律（仅 engine=qlib）：dropout = 每天换掉 n_drop 只；
    # band = 还在前 exit_rank 名就继续拿，空位从前 entry_rank 名补。自研内核不读这三个键。
    # 单票默认用 band：进榜严、出榜松，不每天追着 #1 换票。
    "hold_rule": "band",
    "entry_rank": 50,       # 买入候选 = 当日分数前 entry_rank 名
    "exit_rank": 100,       # 跌出前 exit_rank 名才卖
    # 仅 engine=qlib：是否在回测信号上叠加线上那两条名单护栏（薄流动/拉升后阴跌）。
    # 关闭只用于研究对照；默认开着，避免回测与线上差一层筛子却没人知道。
    "guardrails": True,
}

ENGINES = ("self", "qlib")
# 与 stock_selector_qlib/backtest.py 的 HOLD_RULES 一致（后端不 import qlib，所以这里抄一份）
HOLD_RULES = ("dropout", "band")

# engine=qlib 看不懂的参数：TopkDropout 只按分数等权分仓，没有止盈止损/持有天数/规则串。
# 收着这些参数还照跑 = 「前端能填、回测跑不到」，所以 validate 直接拒。
_QLIB_UNUSED_KEYS = ("strategy", "rule_set_id", "sell_rule", "add_rule", "max_hold_days",
                     "stop_loss_pct", "take_profit_pct", "max_positions", "position_cap_pct",
                     "target_amount", "universe")
# 反过来：band 的键只有 qlib 引擎认，自研内核一条都不读。
# topk/n_drop/benchmark 也要列进来：worker 是 `SimpleNamespace(**merged)` 整包喂内核的
# （tasks/backtest_tasks.py:108），自研内核没有这三个属性，收了就静默忽略——
# 那正好是本模块开头要避免的「前端能填、回测跑不到」。
_SELF_UNUSED_KEYS = ("hold_rule", "entry_rank", "exit_rank", "topk", "n_drop", "benchmark",
                     "guardrails")

# 字符串参数：{键: 长度上限}
_STR_SPEC: dict = {"rule_set_id": 36, "sell_rule": 2000, "add_rule": 2000,
                   "engine": 8, "benchmark": 16, "hold_rule": 8}

# 数值参数：(类型, 下界, 上界)，None 表示该侧不校验
_NUM_SPEC: dict = {
    "capital": (float, 1000.0, 1_000_000_000.0),
    "max_positions": (int, 1, 50),
    "position_cap_pct": (float, 0.01, 1.0),
    "target_amount": (float, 100.0, 100_000_000.0),
    "commission_rate": (float, 0.0, 0.01),
    "commission_min": (float, 0.0, 1000.0),
    "stamp_tax_rate": (float, 0.0, 0.01),
    "universe": (int, 0, 6000),
    "max_hold_days": (int, 0, 250),
    "stop_loss_pct": (float, 0.0, 0.5),
    "take_profit_pct": (float, 0.0, 1.0),
    "topk": (int, 1, 300),
    "n_drop": (int, 0, 300),
    "entry_rank": (int, 1, 300),
    "exit_rank": (int, 1, 300),
}


def form_spec() -> dict:
    """给前端表单的单一事实来源：缺省值 + 数值区间 + 每个引擎看不懂的键。

    `engine_excludes` 是给页面收字段用的：collect() 会把这些键从提交体里剔掉。
    后端 validate 对同一批键直接 400，所以两处必须来自同一份定义——
    在页面里另抄一遍清单，加参数时就会变成「前端能填、后端拒收」。
    """
    return {
        "defaults": dict(DEFAULTS),
        "numeric": {
            key: {"type": "int" if typ is int else "float", "min": lo, "max": hi}
            for key, (typ, lo, hi) in _NUM_SPEC.items()
        },
        "engines": list(ENGINES),
        "hold_rules": list(HOLD_RULES),
        "engine_excludes": {"self": list(_SELF_UNUSED_KEYS), "qlib": list(_QLIB_UNUSED_KEYS)},
    }


def merge(params: dict | None) -> dict:
    """缺省值 + 白名单内的非空覆盖，供 worker 组装内核参数。"""
    out = dict(DEFAULTS)
    for key, value in (params or {}).items():
        if key in DEFAULTS and value is not None:
            out[key] = value
    return out


def validate(raw: dict | None) -> dict:
    """校验前端提交的参数，返回归一化后的字段（未提交的键不出现）。"""
    params = raw or {}
    unknown = sorted(k for k in params if k not in DEFAULTS)
    if unknown:
        raise BizError(f"未知回测参数: {', '.join(unknown)}", code=400, status_code=400)

    out: dict = {}
    for key, value in params.items():
        if value is None:
            continue
        if key in ("start", "end"):
            out[key] = _date(key, value)
        elif key == "strategy":
            name = str(value).strip()
            if len(name) > 64:
                raise BizError("策略名过长", code=400, status_code=400)
            out[key] = name
        elif key == "engine":
            name = str(value).strip().lower()
            if name not in ENGINES:
                raise BizError(f"回测引擎只支持 {'/'.join(ENGINES)}，收到 {name!r}",
                               code=400, status_code=400)
            out[key] = name
        elif key == "hold_rule":
            name = str(value).strip().lower()
            if name not in HOLD_RULES:
                raise BizError(f"持仓纪律只支持 {'/'.join(HOLD_RULES)}，收到 {name!r}",
                               code=400, status_code=400)
            out[key] = name
        elif key in _STR_SPEC:
            out[key] = _text(key, value)
        elif key in ("no_save", "guardrails"):
            out[key] = bool(value)
        else:
            typ, lo, hi = _NUM_SPEC[key]
            out[key] = _num(key, value, typ, lo, hi)

    effective = merge(out)
    _check_window(effective)
    _check_position_sizing(effective)
    _check_rule_snapshot(effective)
    _check_engine(params, effective)
    _check_hold_rule(effective)
    return out


def _check_engine(raw: dict, effective: dict) -> None:
    """每个引擎只吃自己实现了的参数，其余一律拒绝。

    静默忽略比报错更坏：页面填了止损 5%，qlib 根本没实现止损，读数却是「设了止损的回测」；
    反过来给自研内核传 band 的三个键也一样，它只会走自己的 max_hold_days/止损。
    """
    engine = effective["engine"]
    if engine == "qlib":
        keys = _QLIB_UNUSED_KEYS
        why = "qlib 内核只按分数排序建仓，没有止损/持有天数/规则串"
    else:
        keys = _SELF_UNUSED_KEYS
        why = "自研内核不读 qlib 的持仓纪律参数"
    given = [key for key in keys
             if raw.get(key) is not None and raw.get(key) != "" and key != "engine"]
    if given:
        raise BizError(f"engine={engine} 不支持这些参数（{why}）：{', '.join(given)}",
                       code=400, status_code=400)
    # n_drop 只属于 dropout：band 不每天强制换票，收着它照跑就会把读数标成「换过 5 只」。
    # 缺省表单会把 n_drop=5 一起提交，band 默认下放行这个缺省值；显式改成别的数才拒。
    if (engine == "qlib" and effective["hold_rule"] == "band"
            and raw.get("n_drop") is not None and raw.get("n_drop") != DEFAULTS["n_drop"]):
        raise BizError("hold_rule=band 不看 n_drop（它只卖跌出 exit_rank 的持仓，不每天强制换票）；"
                       "要跑 TopkDropout 请设 hold_rule=dropout", code=400, status_code=400)


def _check_hold_rule(params: dict) -> None:
    """band 纪律内部自洽才能跑：出榜比进榜松、候选够填满仓位。

    exit_rank < entry_rank 会让「刚卖掉的票又出现在买入候选里」，退化成高换手还自称低换手；
    entry_rank < topk 则仓位永远凑不满，写在页面上的持仓只数是个假数。
    两条都是「参数互相矛盾」，照跑就会产出没法解释的读数，所以直接拒。
    """
    if params["engine"] != "qlib":
        return
    if params["hold_rule"] == "dropout":
        return
    if params["exit_rank"] < params["entry_rank"]:
        raise BizError(
            f"hold_rule=band 要求出榜线不比进榜线严：exit_rank({params['exit_rank']}) "
            f"需 ≥ entry_rank({params['entry_rank']})，否则跌出去的票当天又会被买回来",
            code=400, status_code=400)
    if params["entry_rank"] < params["topk"]:
        raise BizError(
            f"买入候选前 {params['entry_rank']} 只少于持仓 {params['topk']} 只，仓位永远凑不满；"
            "请把 entry_rank 调到不小于 topk",
            code=400, status_code=400)


def _date(key: str, value) -> str:
    text = str(value).strip()
    if not DATE_RE.match(text):
        raise BizError(f"{key} 需为 YYYY-MM-DD 格式", code=400, status_code=400)
    return text


_RULE_CN = {"sell_rule": "卖出规则串", "add_rule": "加仓规则串"}


def _text(key: str, value) -> str:
    from core import rule_grammar

    text = str(value).strip()
    if len(text) > _STR_SPEC[key]:
        raise BizError(f"{key} 过长（上限 {_STR_SPEC[key]} 字符）", code=400, status_code=400)
    if key in _RULE_CN and text:
        # 当场校验语法：坏串存进内核不会报错，只会让那条规则永远不触发（RuleEvaluator 静默 False）
        mod = rule_grammar.rule_expr()
        try:
            mod.validate(text)
        except mod.RuleSyntaxError as exc:
            raise BizError(f"{_RULE_CN[key]}不合法：{exc}", code=400, status_code=400) from None
    return text


def _num(key: str, value, typ: type, lo, hi):
    try:
        number = typ(value)
    except (TypeError, ValueError):
        raise BizError(f"{key} 需为数字", code=400, status_code=400) from None
    if lo is not None and number < lo:
        raise BizError(f"{key} 不能小于 {lo:g}", code=400, status_code=400)
    if hi is not None and number > hi:
        raise BizError(f"{key} 不能大于 {hi:g}", code=400, status_code=400)
    return number


def _check_window(params: dict) -> None:
    if params["start"] >= params["end"]:
        raise BizError("起始日期需早于结束日期", code=400, status_code=400)


def _check_position_sizing(params: dict) -> None:
    """单票上限按总资产算：单笔金额贴边或超出时，净值一回撤成交就会被上限静默拒绝，
    回测看起来像「没有信号」。CLI 对此只打警告，后台提交则直接拒绝，免得产出空结果。
    """
    cap = params["capital"] * params["position_cap_pct"]
    if params["target_amount"] >= cap:
        raise BizError(
            f"单笔买入金额 {params['target_amount']:g} 需小于单票上限 "
            f"{cap:g}（初始资金 × 单票仓位上限）",
            code=400,
            status_code=400,
        )


def _check_rule_snapshot(params: dict) -> None:
    """规则集串只有「规则模式」（不选策略）才消费。

    选了选股策略时出场走 max_hold_days/止损，内核根本不读 sell/add 表达式；
    那种组合下页面却写着「按规则集退出」，产出的赢比就不能拿去当规则集的依据。
    """
    if params["strategy"] and (params["sell_rule"] or params["add_rule"]):
        raise BizError(
            "策略模式的出场由持有天数与止损决定，不读规则集的卖出/加仓串；"
            "要验规则集请先清空策略",
            code=400,
            status_code=400,
        )
