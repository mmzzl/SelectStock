"""入口：qlib 选股插件（与 stock_selector 8002 同契约的 HTTP 服务）。

启动：
    python -m uvicorn stock_selector_qlib.api:app --host 172.17.0.1 --port 8003
    # 绑 docker0 网关：nginx 容器经 host.docker.internal(=172.17.0.1) 回连宿主机，绑 127.0.0.1 连不通。
    # 对外查看走 deploy/nginx/selectstock.conf 的 location /qlib/（Basic 认证 + 限流）。

后端切引擎改 `SELECTOR_BASE_URL=http://172.17.0.1:8003` 一个键（`settings.selector_base_url`），
业务代码零改动。注意不要整键切 `selection_plugin_base_url`：新闻与板块那两条服务也复用它，
而本进程只实现选股这三条路由，切过去会让 App 的消息面页和板块热力图一起 404。

红线：本模块零选股逻辑，只做契约适配；打分全在 serving.py，模型全在 artifacts.py。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from stock_selector.models.result import SelectResult
from stock_selector_qlib import artifacts, serving

logger = logging.getLogger("qlib_plugin")

STRATEGY = "qlib_alpha158"

app = FastAPI(title="qlib 插件化量化选股系统", version="0.1.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RunRequest(BaseModel):
    strategies: list[str] | None = Field(
        default=None, description="策略名列表；缺省或含 qlib_alpha158 时出名单，其他名字返回空")
    topk: int | None = Field(default=None, ge=1, le=200, description="取前 N 只，缺省读 QLIB_TOPK")


_STRATEGY_DETAIL = {
    "name": STRATEGY,
    "description": "qlib Alpha158 因子 + LightGBM：预测未来 5 日横截面收益，按分数排名出名单",
    "as_of": True,
    # 与 8002 不同：qlib 读自己的 bin 面板，不吃后端送来的日线，这两项只是元信息
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "params": {"universe": 300, "warmup_days": serving.WARMUP_DAYS, "label": "Ref($close,-5)/Ref($close,-1)-1"},
}


@app.get("/api/strategy/list")
def strategy_list():
    return {"code": 0, "data": [STRATEGY]}


@app.get("/api/strategy/detail")
def strategy_detail():
    return {"code": 0, "data": [_STRATEGY_DETAIL]}


@app.post("/api/selector/run")
def selector_run(req: RunRequest):
    """出最新一个交易日的 qlib 名单。分数只由落盘模型 + 短窗因子决定，不在此处训练。"""
    names = set(req.strategies or [])
    if names and STRATEGY not in names:
        return {"code": 0, "data": []}
    try:
        art = artifacts.load()
        rows = serving.latest_scores(topk=req.topk or serving.DEFAULT_TOPK)
    except Exception as exc:  # noqa: BLE001 - 打分失败必须是 HTTP 错误，不能被当成「今天没票」
        logger.exception("qlib 打分失败")
        raise HTTPException(status_code=502, detail=f"qlib 打分失败：{exc}") from exc

    codes = [r["code"] for r in rows]
    try:
        label_map = serving.stock_names(codes)
    except Exception:  # noqa: BLE001
        label_map = {}
    out = []
    for row in rows:
        out.append(SelectResult(
            code=row["code"],
            name=label_map.get(row["code"], ""),
            signal="预期5日收益排名",
            reason=(f"Alpha158+LightGBM 原始分 {row['score_raw']:.4f}，"
                    f"当日票池 {row['universe']} 只里排第 {row['rank']}；"
                    f"模型 {art.run_id}（训练止于 {art.meta.get('train_end')}，名单日为样本外）"),
            strategy=STRATEGY,
            asof=row["date"],
            price=row["price"],
            score=round(row["score"], 6),
            rank=row["rank"],
            extra={
                "qlib_symbol": row["qlib_symbol"],
                "score_raw": round(row["score_raw"], 6),
                "model_id": art.run_id,
                "universe": row["universe"],
                "feature_count": art.meta.get("feature_count"),
            },
        ).to_dict())
    return {"code": 0, "data": out}


@app.post("/api/admin/refresh")
def admin_refresh():
    """收盘后 bin 面板写完时由 scripts/qlib_update_bin.py 调用：清掉进程内的 qlib 日历/因子
    缓存并认账新的交易日。为什么要这么一个口：根调度器以普通用户跑，重启不了本进程
    （supervisorctl 要 root），而 qlib 把 D.calendar() 的结果在进程内缓存一小时，
    不刷新的话这一小时里它会坚持说「今天还不是交易日」。
    对外查看走 nginx 的 /qlib/，那里已把 /qlib/api/admin/ 挡成 403。"""
    try:
        return {"code": 0, "data": serving.refresh()}
    except Exception as exc:  # noqa: BLE001 - 刷新失败必须让更新脚本非零退出，不能默默回去
        logger.exception("qlib 缓存刷新失败")
        raise HTTPException(status_code=500, detail=f"qlib 缓存刷新失败：{exc}") from exc


@app.get("/api/health")
def health():
    status = {"status": "ok", "engine": STRATEGY}
    try:
        status["model"] = artifacts.load().describe()
    except Exception as exc:  # noqa: BLE001
        status["status"] = "no_model"
        status["message"] = str(exc)
    return status
