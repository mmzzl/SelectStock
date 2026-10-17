# api/v1/profit_scan.py
"""逐笔盈亏扫描接口（只读）：三层视图 —— 条件 → 卖法 → 「为什么」。

数据由 worker 在服务器上跑 scripts/profit_scan.py 产出（62,208 行一次写进 Mongo），
这里不回算任何一个百分点，只做筛选、分页、把字段翻成大白话。

权限：profit_scan:list（查看）。页面顶部的「口径」是写死的说明文本，
因为口径一改历史读数就不可比，不能让它跟着页面改。
"""
from fastapi import APIRouter, Depends, Query

from core.dependencies import CurrentUserDep, require_permission
from core.response import ok, paged
from services import profit_scan_service as svc

router = APIRouter(prefix="/profit-scans", tags=["逐笔盈亏扫描"])


@router.get("/runs", summary="历次扫描（含每次的窗口与门槛）",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def list_runs(user: CurrentUserDep, limit: int = Query(20, ge=1, le=50)):
    return ok({"items": await svc.runs(limit)})


@router.get("/overview", summary="某次扫描的口径说明 + 判定分布",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def overview(user: CurrentUserDep, run_id: str = Query("", max_length=40)):
    return ok(await svc.overview(run_id))


@router.get("/options", summary="筛选下拉（卖法档 + 条件类别）",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def options(user: CurrentUserDep, run_id: str = Query("", max_length=40)):
    return ok(await svc.options(run_id))


@router.get("/rules", summary="层一：一条买入条件一行（64 种卖法折起来看最好的）",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def list_rules(user: CurrentUserDep, run_id: str = Query("", max_length=40),
                     kind: str = Query("", max_length=24), q: str = Query("", max_length=40),
                     verdict: str = Query("", max_length=8),
                     order: str = Query("max_net_pp", max_length=32),
                     min_trades: int | None = Query(None, ge=0, le=100000,
                                                    description="折叠时只看笔数达标的卖法；不传=用内核门槛"),
                     page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    data = await svc.rules(run_id, kind=kind, q=q, verdict=verdict, min_trades=min_trades,
                           page=page, size=size, order=order)
    return ok({"run": data["run"],
               "items": paged(data["items"], data["total"], page, size)})


@router.get("/rules/{cond_id}/profiles", summary="层二：这条条件在每种卖法下分别赚多少",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def list_profiles(user: CurrentUserDep, cond_id: str,
                        run_id: str = Query("", max_length=40),
                        order: str = Query("net_per_trade_pp", max_length=32)):
    return ok(await svc.profiles(run_id, cond_id, order))


@router.get("/rows/{cond_id}/{profile}", summary="层三：单行明细 + 8 项「为什么」",
            dependencies=[Depends(require_permission("profit_scan:list"))])
async def row_detail(user: CurrentUserDep, cond_id: str, profile: str,
                     run_id: str = Query("", max_length=40)):
    return ok(await svc.row_detail(run_id, cond_id, profile))
