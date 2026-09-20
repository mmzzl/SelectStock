from pydantic import BaseModel, Field


class TradeCreate(BaseModel):
    """仓储写白名单（含 user_id；供 _assign 落库）。"""
    user_id: str = Field(min_length=1, max_length=64)
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)   # buy|sell
    price: float = Field(ge=0)
    quantity: int = Field(ge=0)
    amount: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)
    source: str = Field(default="manual", max_length=16)
    batch_id: str = Field(default="", max_length=64)


class TradeRow(BaseModel):
    """交割单一条（API 双步：preview 返回 / commit 回传）。"""
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=6, max_length=6)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)
    price: float = Field(ge=0)
    quantity: int = Field(gt=0)
    amount: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)