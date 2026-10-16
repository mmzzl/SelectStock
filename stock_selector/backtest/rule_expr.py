"""rule_expr.py — 自定义规则的唯一语法：结构化条件 ⇄ 表达式串 ⇄ 向量化求值。

为什么要有这个模块（2026-10-16）：持仓判定用 `RuleEvaluator` 逐行 eval，全市场台账/回放必须
向量化（12M 行逐行 eval 跑不动），管理页面又要让用户填「字段/运算/右值」。三条路各写一套的话，
出来的就是「台账说这只要卖、线上判成持有」这种不报错的偏差。所以三边共用这里的同一份文法：

    item := arith op arith                     op ∈ {<, <=, >, >=}
    arith := term (('+'|'-') term)*
    term  := atom (('*'|'/') atom)*
    atom  := number | field
    expr  := item (('and'|'or') item)*          # 规则内 AND；多条规则之间由 join_rules 用 OR 连

刻意不支持的东西，都是为了「两条求值路径必然同结果」：
- `==`/`!=`：浮点阈值相等没有业务含义，而逐行 eval 是精确相等、向量化只能近似，两边会分家；
- 括号/函数/`not`/属性访问：`RuleEvaluator` 那侧本来就只暴露白名单列，提前在入口拒绝，
  比存进去之后永远静默返回 False（等于这条规则不存在）好。

波动率归一是这里刻意支持的形状：`close <= ma10 * 0.95`（固定百分比，A 3% 和 1% 的票用同一把尺子）
想改成按波动走就写 `close <= ma10 - atr14 * 1.0`——这类表达式必须由同一个解析器认得，
否则页面上给不出、台账里算不出。
"""

from __future__ import annotations

import re

from .rule_eval import _ALLOWED_COLUMNS, _ALIASES

OPS = ("<", "<=", ">", ">=")
_CMP = re.compile(r"^(.*?)(<=|>=|<|>)(.*)$", re.S)
_TOKEN = re.compile(r"\s*(?:(\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+)|(\w+)|([+\-*/]))\s*")
_FIELD_CHARS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleSyntaxError(ValueError):
    """规则不合法（接口据此报 422，而不是存进去再等运行时静默 False）。"""


def _field(name: str) -> str:
    field = _ALIASES.get(name, name)
    if field not in _ALLOWED_COLUMNS:
        raise RuleSyntaxError(f"字段 {name!r} 不在预处理指标白名单里")
    return field


# ------------------------------------------------------------------ 结构化 → 串

def _term(name: str, coef: float) -> str:
    """系数 1 不写出来：`close > ma20 * 1` 这种串会在卖点文案和规则列表里多一层噪声。"""
    col = _field(name)
    return col if coef == 1.0 else f"{col} * {coef:g}"


def render(cond: dict) -> str:
    """结构化条件 → 表达式串。右值 = 阈值 或 `列 × 系数`，可再 ± `列 × 系数`（波动归一）。"""
    left = _field(str(cond.get("field") or ""))
    op = str(cond.get("op") or "")
    if op not in OPS:
        raise RuleSyntaxError(f"运算符 {op!r} 不合法，只支持 {' '.join(OPS)}")
    ref, value = cond.get("ref"), cond.get("value")
    if ref:
        coef = float(cond.get("coef") if cond.get("coef") is not None else 1.0)
        right = _term(str(ref), coef)
    elif value is not None:
        right = f"{float(value):g}"
    else:
        raise RuleSyntaxError("条件既没有 ref（参照列）也没有 value（阈值）")
    plus = cond.get("plus")
    if isinstance(plus, dict) and plus.get("field"):
        sign = "-" if str(plus.get("sign")) == "-" else "+"
        pcoef = float(plus.get("coef") if plus.get("coef") is not None else 1.0)
        right = f"{right} {sign} {_term(str(plus['field']), pcoef)}"
    return f"{left} {op} {right}"


def build_expression(conditions: list[dict]) -> str:
    """一条规则的多个条件 → AND 串；空列表 → ''（表示没有这条规则）。"""
    return " and ".join(render(c) for c in conditions) if conditions else ""


def join_rules(expressions: list[str]) -> str:
    """同侧（sell/add）多条规则 → OR 串；空串跳过。"""
    parts = [e for e in expressions if e]
    return " or ".join(parts) if parts else ""


# ------------------------------------------------------------------ 字段目录
# 管理页面「预处理字段」下拉的数据源。顺序就是页面分组的顺序；一份清单同时喂
# 卖出/加仓两侧，避免页面自己抄一份列名（抄的那份永远是上个版本的列名）。
_FIELD_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("价格与成交", (
        ("close", "收盘价"), ("open", "开盘价"),
        ("high", "最高价"), ("low", "最低价"), ("last_close", "昨收价"),
        ("pct", "当日涨跌幅%"), ("volume", "成交量"), ("amount", "成交额"),
    )),
    ("均线", (
        ("ma5", "5日均线"), ("ma10", "10日均线"), ("ma20", "20日均线"), ("ma60", "60日均线"),
        ("ema12", "12日指数均线"), ("ema26", "26日指数均线"),
    )),
    ("技术指标", (
        ("rsi14", "RSI14"), ("kdj_k", "KDJ-K"), ("kdj_d", "KDJ-D"), ("kdj_j", "KDJ-J"),
        ("wr14", "W%R14"), ("macd_dif", "MACD-DIF"), ("macd_dea", "MACD-DEA"),
        ("macd_hist", "MACD柱"), ("adx14", "ADX14"), ("roc10", "ROC10"),
    )),
    ("波动与通道", (
        ("atr14", "ATR14"), ("boll_upper", "布林上轨"), ("boll_mid", "布林中轨"),
        ("boll_lower", "布林下轨"),
    )),
    ("量能", (
        ("vol_ma5", "5日均量"), ("vol_ma20", "20日均量"), ("vol_ratio", "量比"),
        ("vol_z", "量能Z分数"), ("vol_trend", "量能趋势(5日/20日均量)"), ("obv", "OBV"),
        ("vol_expanding", "放量"), ("vol_contracting", "缩量"),
        ("vol_ground", "地量"), ("vol_blast", "爆量"), ("vol_diverge", "量价背离"),
    )),
    ("位置", (("pos60", "60日位置分位"),)),
    ("关键价位", (
        ("neckline_sup", "颈线支撑"), ("neckline_res", "颈线压力"),
        ("box_upper", "箱体上沿"), ("box_lower", "箱体下沿"),
        ("sup20", "20日支撑"), ("res20", "20日压力"),
    )),
    ("单根形态", (
        ("body_ratio", "实体占振幅比"), ("upper_ratio", "上影占振幅比"),
        ("lower_ratio", "下影占振幅比"), ("gap_pct", "跳空幅度"),
        ("hammer_low", "低位锤子线"), ("hanging_man_high", "高位吊颈线"),
        ("shooting_star_high", "高位射击之星"), ("doji_low", "低位十字星"),
        ("doji_high", "高位十字星"), ("doji_high_blast", "高位十字星且爆量"),
    )),
    ("组合形态", (
        ("bullish_engulf", "看涨吞没"), ("bearish_engulf", "看跌吞没"),
        ("morning_star", "启明星"), ("evening_star", "黄昏星"),
        ("piercing_line", "刺透"), ("dark_cloud", "乌云盖顶"), ("harami", "孕线"),
        ("three_soldiers", "红三兵"), ("three_crows", "黑三鸦"),
    )),
    ("中长期形态", (
        ("w_bottom_break", "W底突破"), ("m_top_break", "M顶跌破"),
        ("hs_bottom_break", "头肩底突破"), ("hs_top_break", "头肩顶跌破"),
        ("v_reversal", "V形反转"), ("arc_bottom", "圆弧底"),
        ("box_break_up", "箱体向上突破"), ("box_break_dn", "箱体向下跌破"),
        ("tri_break_up", "三角向上突破"), ("tri_break_dn", "三角向下跌破"),
        ("flag_up_break", "旗形向上突破"), ("flag_dn_break", "旗形向下跌破"),
        ("pullback_stabilize", "回踩企稳"),
    )),
)


def fields_catalog() -> list[dict]:
    """可选字段清单（含分组与中文名）。

    成员资格由 `rule_eval._ALLOWED_COLUMNS` 决定，标签表只是给它加中文：patterns.py
    以后新增的列会带着裸列名出现在下拉里（组=「其他」），而不是因为这里没写标签就
    从页面上消失——那种「白名单能用、页面选不到」的哑字段最难查。
    """
    out: list[dict] = []
    labeled: set[str] = set()
    for group, items in _FIELD_GROUPS:
        for field, label in items:
            labeled.add(field)
            out.append({"field": field, "label": label, "group": group})
    for name in sorted(_ALLOWED_COLUMNS):
        # 别名（price/adx）不单列：它们只是同一列的另一种写法，页面上出现两个选项
        # 只会让人以为「收盘价」和「price」是两个字段。
        if name in labeled or name in _ALIASES:
            continue
        out.append({"field": name, "label": name, "group": "其他"})
    return out


def group_of(field: str) -> str:
    """给一条已存规则找回它属于哪一组（前端渲染标签用，找不到就是「其他」）。"""
    for group, items in _FIELD_GROUPS:
        if any(f == field for f, _ in items):
            return group
    return "其他"


# ------------------------------------------------------------------ 解析

def _tokens(text: str) -> list[str]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            if text[i:].strip() == "":
                break
            raise RuleSyntaxError(f"表达式含不支持的字符：{text[i:i + 12]!r}")
        out.append(next(g for g in m.groups() if g is not None))
        i = m.end()
    return out


def _split_items(expr: str) -> list[tuple[str, str]]:
    """切成 (与前一项的连接词, 本项原文)；首项连接词为 ''。"""
    groups = [g for g in re.split(r"\s+or\s+", expr) if g.strip()]
    if not groups:
        raise RuleSyntaxError(f"表达式 {expr!r} 有多余的 or")
    parts: list[tuple[str, str]] = []
    for gi, group in enumerate(groups):
        raw = re.split(r"\s+and\s+", group)
        items = [it for it in raw if it.strip()]
        if len(items) != len(raw):
            raise RuleSyntaxError(f"表达式 {expr!r} 有多余的 and")
        for ii, item in enumerate(items):
            conn = "" if (gi == 0 and ii == 0) else ("or" if ii == 0 else "and")
            parts.append((conn, item.strip()))
    return parts


def _parse_arith(toks: list[str], pos: int):
    """arith := term (('+'|'-') term)*；返回 (字段集合, 新位置)。"""
    fields, pos = _parse_term(toks, pos)
    while pos < len(toks) and toks[pos] in ("+", "-"):
        pos += 1
        f2, pos = _parse_term(toks, pos)
        fields |= f2
    return fields, pos


def _parse_term(toks: list[str], pos: int):
    fields, pos = _parse_atom(toks, pos)
    while pos < len(toks) and toks[pos] in ("*", "/"):
        pos += 1
        f2, pos = _parse_atom(toks, pos)
        fields |= f2
    return fields, pos


def _parse_atom(toks: list[str], pos: int):
    if pos >= len(toks):
        raise RuleSyntaxError("表达式在运算符后缺右值")
    tok = toks[pos]
    if _FIELD_CHARS.match(tok):
        # 收成规范化列名（price→close）：调用方拿这个清单去 parquet 里取列，
        # 返回别名会让它按 `price` 找列然后报「缺列」。
        return {_field(tok)}, pos + 1
    if re.match(r"^[\d.]", tok):
        return set(), pos + 1
    raise RuleSyntaxError(f"不支持的 token {tok!r}（只允许指标列、数字、+ - * /）")


def _split_cmp(item: str) -> tuple[str, str, str]:
    m = _CMP.match(item)
    if not m:
        raise RuleSyntaxError(f"条件 {item!r} 缺少比较运算符（< <= > >=）")
    left, op, right = m.group(1), m.group(2), m.group(3)
    if right.lstrip().startswith(("=", "!")):
        raise RuleSyntaxError(f"条件 {item!r} 用了相等比较：浮点阈值上相等没有业务含义，"
                              f"且逐行求值与向量化会分家")
    if not (left.strip() and right.strip()):
        raise RuleSyntaxError(f"条件 {item!r} 运算符两侧都要有内容")
    return left.strip(), op, right.strip()


def _parse_item(item: str) -> set[str]:
    left, _op, right = _split_cmp(item)
    fields: set[str] = set()
    for side in (left, right):
        toks = _tokens(side)
        f, pos = _parse_arith(toks, 0)
        if pos != len(toks):
            raise RuleSyntaxError(f"条件 {item!r} 右侧有多余内容")
        fields |= f
    if not fields:
        raise RuleSyntaxError(f"条件 {item!r} 不含任何指标列")
    return fields


def validate(expr: str) -> list[str]:
    """校验表达式串，返回它引用的字段列表；不合法抛 RuleSyntaxError。"""
    if not expr:
        return []
    fields: set[str] = set()
    for _conn, item in _split_items(expr):
        fields |= _parse_item(item)
    return sorted(fields)


# ------------------------------------------------------------------ 向量化求值

class _ArrayEval:
    """把同一份文法在 numpy 数组上跑一遍：与 RuleEvaluator 的逐行结果必须一致。"""

    def __init__(self, cols: dict):
        self.cols = cols

    def resolve(self, name: str):
        import numpy as np

        # 不复制也不降精度：调用方给什么 dtype 就用什么 dtype 算。逐行路径喂的是
        # parquet 的 float64，所以测试里必须一致；台账的稠密矩阵是 float32（6000 票 × 5000 天，
        # float64 会多占一倍内存），边界差只出现在两列数学上相等的那些格。
        field = _field(name)
        if field in self.cols:
            return np.asarray(self.cols[field])
        if name in self.cols:                     # 调用方直接给了别名（price）
            return np.asarray(self.cols[name])
        raise RuleSyntaxError(f"矩阵里缺列 {field}")

    def arith(self, toks: list[str], pos: int):
        import numpy as np

        value, pos = self.term(toks, pos)
        while pos < len(toks) and toks[pos] in ("+", "-"):
            op = toks[pos]
            rhs, pos = self.term(toks, pos + 1)
            value = value + rhs if op == "+" else value - rhs
        return value, pos

    def term(self, toks: list[str], pos: int):
        import numpy as np

        value, pos = self.atom(toks, pos)
        while pos < len(toks) and toks[pos] in ("*", "/"):
            op = toks[pos]
            rhs, pos = self.atom(toks, pos + 1)
            with np.errstate(divide="ignore", invalid="ignore"):
                value = value * rhs if op == "*" else value / rhs
        return value, pos

    def atom(self, toks: list[str], pos: int):
        import numpy as np

        if pos >= len(toks):
            raise RuleSyntaxError("表达式在运算符后缺右值")
        tok = toks[pos]
        if _FIELD_CHARS.match(tok):
            return self.resolve(tok), pos + 1
        if re.match(r"^[\d.]", tok):
            return float(tok), pos + 1
        raise RuleSyntaxError(f"不支持的 token {tok!r}")


def eval_matrix(expr: str, cols: dict):
    """表达式串 → 布尔矩阵。`cols` 是 {字段名: ndarray}，缺字段抛 RuleSyntaxError。

    NaN 参与比较天然得 False，也就是「这一行没触发」——和 `RuleEvaluator` 逐行 try/except
    返回 False 一致，两边的判定才对得上。
    """
    import numpy as np

    if not expr:
        return None
    ev = _ArrayEval(cols)
    out = group = None
    for conn, item in _split_items(expr):
        term = _cmp_matrix(ev, item)
        if group is None:
            group = term
        elif conn == "and":
            group = group & term
        else:                       # 'or' 与首项之后的分组边界
            # 优先级和 Python 一致：and 绑得比 or 紧，否则逐行 eval 与向量化会分家
            out = group if out is None else (out | group)
            group = term
    if group is not None:
        out = group if out is None else (out | group)
    return np.asarray(out, dtype=bool)


def _cmp_matrix(ev: _ArrayEval, item: str):
    left, op, right = _split_cmp(item)
    a, pos = ev.arith(_tokens(left), 0)
    if pos != len(_tokens(left)):
        raise RuleSyntaxError(f"条件 {item!r} 左侧有多余内容")
    b, pos = ev.arith(_tokens(right), 0)
    if pos != len(_tokens(right)):
        raise RuleSyntaxError(f"条件 {item!r} 右侧有多余内容")
    import numpy as np

    with np.errstate(invalid="ignore"):
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
