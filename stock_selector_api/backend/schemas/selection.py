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
    buy_ref_price: float | None = None       # 买点参考位（策略判据里的均线/箱沿、筹码主峰下沿）
    buy_invalid_price: float | None = None   # 跌破它则本次信号失效
    sell_target_price: float | None = None   # 第一止盈价（qlib 榜=筹码主峰上沿）
    level_note: str = Field(default="", max_length=160)   # 这三档价位的口径，页面原样显示
    # 风控决策：内核风控放行后的最终口径，被拦下的票不会出现在这里。
    # 空串/None 表示这批结果是风控关闭时跑的（不能落成 0，0 是"建议买 0 股"）。
    risk_verdict: str = Field(default="", max_length=8)
    risk_reason: str = Field(default="", max_length=512)
    risk_shares: int | None = None
    risk_amount: float | None = None
    risk_fill_price: float | None = None
    risk_slippage_bp: float | None = None
    # 判据级历史后验（不是个股评分）。verdict 已含「样本不足/翻号未标定」，
    # 所以它是页面唯一该显示的字段；excess_pp/n 为 None 表示后验表还没建。
    posterior_verdict: str = Field(default="", max_length=64)
    posterior_excess_pp: float | None = None
    posterior_n: int | None = None
    # adx14 闸门留痕：当日全市场分位（0~100）与 pass/reject。空 = 闸门没开或分位取不到。
    adx14_pct: float | None = None
    adx14_gate: str = Field(default="", max_length=8)
    # 引擎显式声明算不出来的字段名，逗号分隔（qlib 插件给；自研内核不给）。
    na_fields: str = Field(default="", max_length=128)
