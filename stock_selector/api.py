"""入口2：Web HTTP 接口服务（FastAPI）。

向 Web 前端、Go、Java 等跨语言系统提供 HTTP 接入。
本模块为薄薄的 HTTP 适配层，零业务逻辑，仅做参数接收和结果返回，
内部统一调用内核 run_select()，保证与命令行结果 100% 一致。

启动：
    python -m uvicorn stock_selector.api:app --host 0.0.0.0 --port 8002
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .core.selector import list_strategies, run_select, strategy_details

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


@app.get("/api/health")
def health():
    return {"status": "ok"}