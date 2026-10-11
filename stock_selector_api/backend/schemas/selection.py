"""schemas/selection.py — 选股任务/结果写白名单。"""
from pydantic import BaseModel, Field


class SelectJobCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    strategies: str = Field(default="[]", max_length=256)
    status: str = Field(default="queued", max_length=16)
    source: str = Field(default="manual", max_length=16)
    error: str = Field(default="", max_length=512)
    result_count: int = 0


class SelectResultCreate(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    signal: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=1024)
    # 选股插件 P1 富化字段；rank_no 对应插件的 rank（rank 是 SQL 保留字）
    strategy: str = Field(default="", max_length=64)
    asof: str = Field(default="", max_length=16)
    price: float | None = None
    score: float | None = None
    rank_no: int | None = None
    risk_ex10_pp: float | None = None    # 买入风险分（百分点），闸门判不了时为 None
    buy_ref_price: float | None = None       # 买点参考位（策略判据里的均线/箱沿）
    buy_invalid_price: float | None = None   # 跌破它则本次信号失效
