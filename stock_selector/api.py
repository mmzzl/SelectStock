"""入口2：Web HTTP 接口服务（FastAPI）。

向 Web 前端、Go、Java 等跨语言系统提供 HTTP 接入。
本模块为薄薄的 HTTP 适配层，零业务逻辑，仅做参数接收和结果返回，
内部统一调用内核 run_select()，保证与命令行结果 100% 一致。

启动：
    python -m uvicorn stock_selector.api:app --host 0.0.0.0 --port 8002
"""

import logging

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .core.selector import list_strategies, run_select, strategy_details
from .fundflow.rank import FundDataMissing as FundRankDataMissing
from .fundflow.rank import rank as fund_rank
from .news import read as news_read
from .sector_heat import HeatDataMissing, board_detail, heat, stock_kline

# 内核用 logger("stock_selector") 打进度与风险剔除明细；本进程是入口，负责挂 handler，
# 否则 uvicorn 只配自己的 logger，"命中被谁剔掉了"在生产日志里是看不见的。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="插件化量化选股系统", version="0.1.0")

# 跨域适配：允许 Web 前端调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    strategies: list[str] | None = Field(
        default=None, description="需要执行的策略名称列表；缺省使用配置默认策略"
    )


@app.get("/api/strategy/list")
def strategy_list():
    """获取所有可用选股策略（前端下拉展示）。"""
    return {"code": 0, "data": list_strategies()}


@app.get("/api/strategy/detail")
def strategy_detail():
    """策略元数据：中文名/描述/判据形态/所需数据，供前端做标签映射。"""
    return {"code": 0, "data": strategy_details()}


@app.post("/api/selector/run")
def selector_run(req: RunRequest):
    """执行选股，返回标准化结果。"""
    results = run_select(strategies=req.strategies)
    return {"code": 0, "data": [r.to_dict() for r in results]}


@app.get("/api/sector/heat")
def sector_heat(window: int = Query(3, ge=1, le=250), kind: str = Query("all", pattern="^(all|industry|concept)$")):
    """板块热力图格子（区间涨幅降序），窗口单位是交易日。"""
    try:
        return {"code": 0, "data": heat(window, kind)}
    except HeatDataMissing as exc:
        return {"code": 1, "message": str(exc), "data": None}


@app.get("/api/sector/board")
def sector_board(board_code: str = Query(..., min_length=2, max_length=16), window: int = Query(3, ge=1, le=250)):
    """点进格子后的板块详情与成分股区间表现。"""
    try:
        return {"code": 0, "data": board_detail(board_code, window)}
    except HeatDataMissing as exc:
        return {"code": 1, "message": str(exc), "data": None}
    except ValueError as exc:
        return {"code": 2, "message": str(exc), "data": None}


@app.get("/api/sector/kline")
def sector_kline(code: str = Query(..., min_length=6, max_length=6), days: int = Query(120, ge=20, le=500)):
    """个股日 K 尾部若干根，前端 canvas 自己画蜡烛图。"""
    try:
        return {"code": 0, "data": stock_kline(code, days)}
    except HeatDataMissing as exc:
        return {"code": 1, "message": str(exc), "data": None}
    except ValueError as exc:
        return {"code": 2, "message": str(exc), "data": None}


@app.get("/api/news/daily")
def news_daily(day: str = Query("", max_length=10, description="YYYY-MM-DD；留空取最近一批"),
               limit: int = Query(60, ge=1, le=200)):
    """消息面当日全貌：利好/利空/风险/关注四类清单 + LLM 分析健康位。"""
    db = news_read.connect()
    if db is None:
        return {"code": 1, "message": "消息面数据源不可用", "data": None}
    return {"code": 0, "data": news_read.daily(db, day or None, limit=limit)}


@app.get("/api/news/feed")
def news_feed(limit: int = Query(30, ge=1, le=100),
              before: str = Query("", max_length=24, description="上一页末条的 id；留空从最新开始")):
    """原始消息流（东财 10 秒线，每 30 秒在采），按采集序倒序一页。"""
    db = news_read.connect()
    if db is None:
        return {"code": 1, "message": "消息面数据源不可用", "data": None}
    try:
        return {"code": 0, "data": news_read.feed(db, limit=limit, before=before or None)}
    except ValueError as exc:
        return {"code": 2, "message": str(exc), "data": None}


@app.get("/api/news/item")
def news_item(id: str = Query(..., min_length=24, max_length=24)):
    """一条原始消息的详情：标题 + 摘要 + 关联标的。"""
    db = news_read.connect()
    if db is None:
        return {"code": 1, "message": "消息面数据源不可用", "data": None}
    try:
        doc = news_read.item(db, id)
    except ValueError as exc:
        return {"code": 2, "message": str(exc), "data": None}
    if doc is None:
        return {"code": 3, "message": "没有这条消息（可能已被源清理）", "data": None}
    return {"code": 0, "data": doc}


@app.get("/api/fund/rank")
def fund_rank_list(window: int = Query(3, ge=1, le=250),
                   side: str = Query("in", pattern="^(in|out)$"),
                   limit: int = Query(50, ge=1, le=200)):
    """主力资金榜：最近 N 个交易日的累计净流入/净流出（单位元，换算交给前端）。"""
    try:
        return {"code": 0, "data": fund_rank(window=window, side=side, limit=limit)}
    except FundRankDataMissing as exc:
        return {"code": 1, "message": str(exc), "data": None}
    except ValueError as exc:
        return {"code": 2, "message": str(exc), "data": None}


@app.get("/api/health")
def health():
    return {"status": "ok"}