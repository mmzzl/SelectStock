"""server.py — 持仓诊断插件服务（独立进程，默认 8001）。

契约：POST /api/portfolio/analyze
  body:   {user_id, natural_day, holdings: [{code,name,quantity,cost_price}]}
  resp:   {results: [{code,name,action,action_reason,latest_price,quantity,
                      holding_pnl,pnl_pct,suggestion}], summary: {...}}
一切判定与收益计算在插件完成（R1-1/1-4）；后端仅代理透传（R1-2）。
"""
from __future__ import annotations

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8001)
