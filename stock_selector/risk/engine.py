"""risk/engine.py — 风控引擎：信号进来，最终决策出去（策略只产生信号，决策必须独立过风控）。

顺序固定、命中即短路：涨跌停 → T+1 → 同族翻面 → 买入名额 → 流动性 → 仓位 → 可用资金 → 滑点 → 风险分。
前面几条是「这单根本成交不了/这个方向今天压根不该做」，先看；仓位算出下单量，可用资金要拿它
跟账户剩下的钱比，滑点又要用压小后的量估，所以仓位 → 资金 → 滑点这个先后不能换；ML 风险分要跑
700 根日线的特征，最贵，放最后。

短路只影响性能不影响口径：被 block 的规则后面的指标不再计算，决策里 `checks` 记录实际跑过
的环节，复盘时能看清是哪一条拦的、当时的数值是多少。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .context import RiskContext, build
from .rules import RULES, Verdict

logger = logging.getLogger(__name__)

ALLOW, BLOCK, ADJUST = "allow", "block", "adjust"
DEFAULT_CAPITAL = 200000.0            # 未配置时的账户总资金（元）：20 万，量级错了决策就错了
DEFAULT_MODEL_DIR: Path | None = None  # None → 交给 features.risk_score 的默认目录


@dataclass
class RiskConfig:
    enabled: bool = True
    use_limit: bool = True            # 涨跌停可成交性
    capital: float = DEFAULT_CAPITAL
    max_position_pct: float = 0.2     # 单票最大仓位占总资金比例
    min_avg_amount: float = 3e7       # 流动性下限：近 20 日日均成交额（元）
    # 「买得下几只、还有多少钱、今天买的能不能卖」——三条成交现实约束。
    # 默认值对盘后内核是无害的：max_buy_codes=0 表示不限只数，另外两条在上下文没给数据时
    # 自行放行（盘后回测/选股不掌握「今天」的账户处境）。只有盘中链路会把这三项喂满。
    t_plus_1: bool = True             # 今日买入的持仓当天不给卖出建议
    max_buy_codes: int = 0            # 单日最多几只买入建议（0=不限），按盯盘名单名次取前 N
    # 镜像族反向冷却（秒）：同一个指标在这个时长内翻了面，第二条按抖动拦掉。
    # 默认 1800 对盘后内核无害——盘后不传 family，规则自己放行。0 = 关闭。
    flip_cooldown: int = 1800
    cash_check: bool = True           # 买入金额超出当日可用资金就压到可用额度内
    max_participation: float = 0.02   # 单笔下单额占日均成交额上限
    max_slippage_bp: float = 100.0    # 预估滑点上限（万分比）
    max_drift_bp: float = 200.0       # 信号价与最新价偏离上限（万分比）
    min_spread_bp: float = 2.0        # 无五档时的半价差下限（万分比）
    probe_order_amount: float = 5e4   # 未折算出下单量时，滑点用的探测单额（元）
    ml_enabled: bool = True
    ml_model_dir: Path | None = DEFAULT_MODEL_DIR
    # adx14 闸门（第 6 条）。默认关：它是研究出来的准入规则，不是开箱即用的默认项，
    # 打开前请先读 README《结论卡：adx14 高分位闸门》——分母、撮合假设、四项达标口径
    # 都在那里。mode=shadow 时只记录不拦截（灰度期要的正是那个对照）。
    adx14_enabled: bool = False
    adx14_mode: str = "shadow"          # shadow=只记录不拦 | live=低于阈值就拦
    adx14_factor: str = "adx14"
    adx14_min_pct: float = 90.0         # 当日全市场百分位下限

    @classmethod
    def from_dict(cls, raw: dict | None) -> "RiskConfig":
        """只认声明过的键，多余键忽略并留日志；金额键的单位一律是元。"""
        from dataclasses import fields

        data = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(k for k in data if k not in known and k not in ("ml", "adx14_rank"))
        if unknown:
            logger.warning("风控配置里的未知项已忽略: %s（可能拼错或已改名）", ", ".join(unknown))
        ml = data.get("ml") or {}
        gate = data.get("adx14_rank") or {}
        unknown_gate = sorted(k for k in gate if k not in
                              ("enabled", "mode", "factor", "min_pct"))
        if unknown_gate:
            logger.warning("adx14 闸门配置里的未知项已忽略: %s", ", ".join(unknown_gate))
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.ml_enabled = bool(ml.get("enabled", cfg.ml_enabled))
        cfg.ml_model_dir = ml.get("model_dir") or cfg.ml_model_dir
        # 闸门与 ml 段同一写法：settings.yaml 里读 risk.adx14_rank.{enabled,mode,factor,min_pct}
        cfg.adx14_enabled = bool(gate.get("enabled", cfg.adx14_enabled))
        cfg.adx14_mode = str(gate.get("mode", cfg.adx14_mode))
        cfg.adx14_factor = str(gate.get("factor") or cfg.adx14_factor)
        cfg.adx14_min_pct = float(gate.get("min_pct", cfg.adx14_min_pct))
        if cfg.adx14_mode not in ("shadow", "live"):
            raise ValueError(f"risk.adx14_rank.mode 只能是 shadow 或 live，收到 {cfg.adx14_mode!r}")
        return cfg


@dataclass
class Decision:
    """一条信号的最终决策：动作 + 为什么 + 建议下单量 + 预估成交。"""

    code: str
    action: str                       # buy | sell：沿用信号的方向
    verdict: str = ALLOW              # allow | block | adjust
    reasons: list[str] = field(default_factory=list)
    shares: int | None = None
    order_amount: float | None = None
    est_fill_price: float | None = None
    slippage_bp: float | None = None
    metrics: dict = field(default_factory=dict)
    checks: list[Verdict] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == BLOCK

    @property
    def blocked_by(self) -> str:
        """拦住这条信号的那条规则名；放行时是空串。内核按它统计闸门拦了什么。"""
        for verdict in self.checks:
            if verdict.blocked:
                return verdict.name
        return ""

    @property
    def reason(self) -> str:
        return "；".join(self.reasons)

    def extra(self) -> dict:
        """给 SelectResult.extra 的扁平字段：后端只落库、前端只展示，都不要再算。"""
        out = {"risk_verdict": self.verdict, "risk_action": self.action,
               "risk_reason": self.reason[:200], "risk_shares": self.shares,
               "risk_amount": self.order_amount, "risk_fill_price": self.est_fill_price,
               "risk_slippage_bp": self.slippage_bp}
        for key in ("risk_ex10_pp", "avg_amount", "participation", "spread_bp", "impact_bp",
                    "drift_bp", "limit_up", "limit_down",
                    # 闸门的分位与判定：shadow 模式唯一的产出就是这两个字段，
                    # 不落进 extra 就等于闸门在实盘侧"跑过但没留痕"，台账无从对照。
                    "adx14_pct", "adx14_gate", "adx14_min_pct"):
            if self.metrics.get(key) is not None:
                out[key] = self.metrics[key]
        return out


class RiskEngine:
    """把配置 + 规则装成一个可复用对象：选股内核与盘中链路共用同一套判定。"""

    def __init__(self, config: dict | RiskConfig | None = None, rules=RULES):
        self.cfg = config if isinstance(config, RiskConfig) else RiskConfig.from_dict(config)
        self.rules = rules

    @classmethod
    def from_settings(cls, config: dict | None, rules=RULES) -> "RiskEngine":
        """从 settings.yaml 的整份配置里取 `risk:` 子树。"""
        return cls((config or {}).get("risk"), rules=rules)

    def data_requirements(self) -> tuple[list[str], int]:
        """ML 腿需要哪些日线列与回溯根数：由内核统一向数据源声明，规则层不自己拉数。"""
        if not (self.cfg.enabled and self.cfg.ml_enabled):
            return [], 0
        from stock_selector.features import risk_score

        return risk_score.required_columns(), risk_score.required_rows()

    def preflight(self) -> None:
        """启动即校验模型可得。缺失必须在这里炸——「配置说要用风控但实际没用上」是故障，
        不能让规则运行时静默放行、把闸门变成摆设。"""
        if self.cfg.enabled and self.cfg.ml_enabled:
            from stock_selector.features import risk_score

            risk_score.load(self.cfg.ml_model_dir)

    def review(self, ctx: RiskContext) -> Decision:
        """单条信号过风控。`enabled=false` 时直接放行并标注，不跑规则也不读行情。"""
        decision = Decision(code=ctx.code, action=ctx.action)
        if not self.cfg.enabled:
            decision.verdict = ALLOW
            decision.metrics = {"risk_disabled": True}
            return decision

        state: dict = {}
        metrics: dict = {}
        for rule in self.rules:
            verdict = rule(ctx, self.cfg, state)
            decision.checks.append(verdict)
            metrics.update(verdict.metrics or {})
            if verdict.status in (BLOCK, ADJUST) and verdict.reason:
                decision.reasons.append(f"[{verdict.name}] {verdict.reason}")
            if verdict.blocked:
                decision.verdict = BLOCK
                decision.metrics = metrics
                logger.info("风控拦截 %s %s: %s", ctx.code, ctx.action, verdict.reason)
                return decision

        plan = state.get("plan")
        slip = state.get("slippage")
        decision.shares = plan.shares if plan else None
        decision.order_amount = round(plan.amount, 2) if plan else None
        if slip:
            decision.est_fill_price = slip.fill_price
            decision.slippage_bp = slip.total_bp
        decision.metrics = metrics
        any_adjust = any(v.status == ADJUST for v in decision.checks)
        decision.verdict = ADJUST if any_adjust else ALLOW
        return decision

    def review_signal(self, code: str, action: str = "buy", **kw) -> Decision:
        """便捷入口：现拼上下文（盘中链路逐条推送时用；批量场景请自己 build 以复用行情）。"""
        return self.review(build(code, action, **kw))
