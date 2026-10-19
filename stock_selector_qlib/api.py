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
import re

from fastapi import FastAPI, HTTPException, Query
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


# qlib 只出横截面排序分：风险剔除层、判据买点、判据后验都在自研内核里，这里没有对应计算。
# 必须显式声明而不是留 null：null 到 App 上是「—」或整行消失，使用者会把空白读成「没有风险」。
_NA_FIELDS = ("risk_ex10_pp", "buy_ref_price", "buy_invalid_price", "posterior_verdict")


def _label_horizon(label: str | None) -> int | None:
    m = re.match(r"Ref\(\$close,\s*-(\d+)\)", str(label or ""))
    return int(m.group(1)) if m else None


_STRATEGY_DETAIL = {
    "name": STRATEGY,
    "description": "qlib Alpha158 因子 + LightGBM：预测未来 N 日横截面收益，按分数排名出名单（N 随线上模型 label，见 params.label_horizon）",
    "as_of": True,
    # 与 8002 不同：qlib 读自己的 bin 面板，不吃后端送来的日线，这两项只是元信息
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "params": {"universe": 300, "warmup_days": serving.WARMUP_DAYS, "label": "由线上模型 meta 提供"},
    # 本引擎不产这几项，逐条随名单一起声明（口径见 _NA_FIELDS）
    "na_fields": list(_NA_FIELDS),
}


@app.get("/api/strategy/list")
def strategy_list():
    return {"code": 0, "data": [STRATEGY]}


@app.get("/api/strategy/detail")
def strategy_detail():
    # universe 不写死：放开全市场之后它是每天变的，报成 300 会把口径说错
    art = artifacts.load()
    h = _label_horizon(art.meta.get("label"))
    _STRATEGY_DETAIL["params"]["universe"] = {
        "source": serving.POOL_SOURCE,
        "pool_size": len(serving.score_pool(art)),
    }
    _STRATEGY_DETAIL["params"]["label"] = art.meta.get("label")
    _STRATEGY_DETAIL["params"]["label_horizon"] = h
    _STRATEGY_DETAIL["params"]["model"] = art.run_id
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
    h = _label_horizon(art.meta.get("label"))
    signal = f"预期{h}日收益排名" if h else "预期收益排名"
    for row in rows:
        out.append(SelectResult(
            code=row["code"],
            name=label_map.get(row["code"], ""),
            signal=signal,
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
                "na_fields": list(_NA_FIELDS),
            },
        ).to_dict())
    return {"code": 0, "data": out}


@app.get("/api/selector/rank")
def selector_rank(codes: str = Query(default="", description="逗号分隔的 6 位代码，最多 60 只")):
    """持仓页要的「当日榜内名次」：名次来自当日全票池打分，池外的票不给名次。

    这条路由不在 8002 的三条契约里，是 qlib 独有的读接口（自研内核没有"榜"这个概念）。
    池外持仓返回 `in_pool=false` + 一句原因，绝不返回一个补出来的名次。

    **这条是同步接口，所以它绝不等打分**：放开全市场之后第一次冷打分要几百秒，
    把它放进请求里只会被 App/后端的超时打断，打断后页面拿到的是「查询失败」或空名单，
    而真实情况是「还在算」。未就绪就立刻返回 `ready=false` 并起一个后台预热线程。
    """
    picked = [c.strip() for c in codes.split(",") if c.strip()][:60]
    if not picked:
        return {"code": 0, "data": {"ready": True, "date": "", "pool_size": 0,
                                    "universe": 0, "items": []}}
    try:
        return {"code": 0, "data": serving.ranks_by_code(picked, wait=False)}
    except serving.RankingNotReady as exc:
        started = serving.warm_ranking()
        logger.info("榜单未就绪，后台预热%s：%s", "已起" if started else "已在跑", exc)
        return {"code": 0, "data": {"ready": False, "warming": started, "date": "",
                                    "pool_size": 0, "universe": 0, "items": [],
                                    "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001 - 查不到名次必须报错，不能返回「都没上榜」
        logger.exception("qlib 名次查询失败")
        raise HTTPException(status_code=502, detail=f"qlib 名次查询失败：{exc}") from exc


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
        status["scoring"] = serving.last_score_stats()
    except Exception as exc:  # noqa: BLE001
        status["status"] = "no_model"
        status["message"] = str(exc)
    return status
