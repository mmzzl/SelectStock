"""server.py — 持仓诊断插件服务（独立进程，默认 8001）。

契约：POST /api/portfolio/analyze
  body:   {user_id, natural_day, holdings: [{code,name,quantity,cost_price}]}
  resp:   {results: [{code,name,action,action_reason,latest_price,quantity,
                      holding_pnl,pnl_pct,suggestion}], summary: {...}}
一切判定与收益计算在插件完成（R1-1/1-4）；后端仅代理透传（R1-2）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)   # judge.py 复用 stock_selector.backtest.rule_eval

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from portfolio.analyzer import analyze_holdings
from portfolio.pricing import PricingError
from portfolio.provider import KlineProvider

app = FastAPI(title="持仓诊断插件", version="0.2.0")


class HoldingIn(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)


class AnalyzeBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    holdings: list[HoldingIn] = Field(min_length=1)


class TradeIn(BaseModel):
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)
    price: float = Field(ge=0)
    quantity: int = Field(ge=0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)


class PnlBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    trades: list[TradeIn] = Field(default_factory=list)
    holdings: list[HoldingIn] = Field(default_factory=list)


@app.post("/api/portfolio/analyze")
async def analyze(body: AnalyzeBody) -> dict:
    try:
        out = analyze_holdings(
            [h.model_dump() for h in body.holdings],
            KlineProvider(natural_day=body.natural_day),
        )
    except PricingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return out


@app.post("/api/portfolio/pnl")
async def pnl(body: PnlBody) -> dict:
    from portfolio.pricing import latest_price
    from portfolio.realized import compute_pnl

    try:
        return compute_pnl(
            [t.model_dump() for t in body.trades],
            [h.model_dump() for h in body.holdings],
            lambda code: latest_price(code, body.natural_day),
        )
    except PricingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8001)
