"""持仓诊断 schema：analyze 入参 + 落库写白名单 + 出参。"""

from pydantic import BaseModel, Field

from schemas.common import ORMModel


class HoldingItem(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)


class AnalyzeBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    holdings: list[HoldingItem] = Field(min_length=1)
    # 兼容红线锚定测试传入的快照 md5 字段；服务端始终以实际 holdings 计算批次键
    user_holdings_md5: str | None = None
    portfolio_snapshot_md5: str | None = None


class PortfolioDiagRunCreate(BaseModel):
    batch_id: str
    user_id: str
    natural_day: str
    status: str = "queued"
    held_code_count: int = 0
    action_count: int = 0
    total_market_value: float = 0.0
    total_pnl: float = 0.0
    plugin_time_ms: int = 0
    error: str = ""


class PortfolioDiagResultCreate(BaseModel):
    run_id: str
    code: str
    name: str = ""
    action: str
    action_reason: str = ""
    latest_price: float = 0.0
    quantity: int = 0
    holding_pnl: float = 0.0
    pnl_pct: float = 0.0
    suggestion: str = ""
    sell_trigger_price: float | None = None
    conviction_pct: float | None = None
    conviction_level: str | None = None
    conviction_text: str | None = None
    take_profit_price: float | None = None
    game_v: float | None = None
    game_s: float | None = None
    game_text: str | None = None


class PortfolioRunOut(ORMModel):
    run_id: str
    batch_id: str
    user_id: str
    natural_day: str
    status: str
    held_code_count: int = 0
    action_count: int = 0
    total_market_value: float = 0.0
    total_pnl: float = 0.0
    plugin_time_ms: int = 0
    error: str = ""


class PortfolioResultOut(ORMModel):
    run_id: str
    code: str
    name: str = ""
    action: str
    action_reason: str = ""
    latest_price: float = 0.0
    quantity: int = 0
    holding_pnl: float = 0.0
    pnl_pct: float = 0.0
    suggestion: str = ""
    sell_trigger_price: float | None = None
    conviction_pct: float | None = None
    conviction_level: str | None = None
    conviction_text: str | None = None
    take_profit_price: float | None = None
    game_v: float | None = None
    game_s: float | None = None
    game_text: str | None = None