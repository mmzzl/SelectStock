"""持仓诊断双表：run 头 + 明细。收益/市值来自插件透传（R1-2 零算术），后端仅落库。"""

from sqlalchemy import Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin, new_uuid


class PortfolioDiagRun(Base, TimestampMixin):
    __tablename__ = "portfolio_diag_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "natural_day", "batch_id", name="uq_portfolio_diag_runs_key"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)  # holdings_md5
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    natural_day: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/completed/failed
    held_code_count: Mapped[int] = mapped_column(Integer, default=0)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    total_market_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    plugin_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(512), default="")


class PortfolioDiagResult(Base, IdMixin):
    __tablename__ = "portfolio_diag_results"
    __table_args__ = (
        UniqueConstraint("run_id", "code", name="uq_portfolio_diag_result_code"),
    )

    run_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # sell/hold/add
    action_reason: Mapped[str] = mapped_column(String(512), default="")
    latest_price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    holding_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    suggestion: Mapped[str] = mapped_column(String(512), default="")
    # 卖出价格线：插件按同一条 sell 表达式反算出的翻转价；规则不含价格项时为 NULL
    sell_trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 信念度：插件透传的把握度（0-100 分位 + 离散档位 + 原文）
    conviction_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    conviction_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 止盈价：插件按 take-profit 表达式反算出的目标价；规则不含价格项时为 NULL
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 博弈量价：插件输出的 V/S 两轴取值与原文
    game_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_text: Mapped[str | None] = mapped_column(String(512), nullable=True)