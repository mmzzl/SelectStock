# api/v1/ruleset.py
"""规则集接口：CRUD + 「应用」（写生效文件并投递全市场回放）。

权限：查看 rule_set:list，写 rule_set:create/update/delete，应用/重跑回放 rule_set:apply。
后端不计算：可信度的数由 worker 跑 scripts/ruleset_posterior.py 产出，这里只回传状态。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.dependencies import CurrentUserDep, require_permission
from core.response import ok, paged
from services import ruleset_service

router = APIRouter(prefix="/rule-sets", tags=["规则集"])


class RuleItemIn(BaseModel):
    """一条规则：标签 + 若干结构化条件（同一条内 AND）。"""

    label: str = Field(default="", max_length=40)
    conditions: list[dict] = Field(default_factory=list)


class RuleSetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    remark: str = Field(default="", max_length=200)
    disaster_stop_pct: float = Field(default=5.0, ge=1.0, le=30.0)
    sell_conditions: list[RuleItemIn] = Field(default_factory=list)
    add_conditions: list[RuleItemIn] = Field(default_factory=list)


@router.get("/options", summary="表单初始化（可选预处理字段目录 + 运算符 + 边界）",
            dependencies=[Depends(require_permission("rule_set:list"))])
async def options(user: CurrentUserDep):
    return ok(ruleset_service.options())


@router.get("/active", summary="当前生效的规则集快照（回测页面预填用）",
            dependencies=[Depends(require_permission("rule_set:list"))])
async def active(user: CurrentUserDep):
    return ok(await ruleset_service.active_snapshot())


@router.get("", summary="规则集列表", dependencies=[Depends(require_permission("rule_set:list"))])
async def list_rule_sets(user: CurrentUserDep, page: int = Query(1, ge=1),
                         size: int = Query(20, ge=1, le=100)):
    data = await ruleset_service.list_rule_sets(page, size)
    return ok(paged(data["items"], data["total"], page, size))


@router.get("/{rule_set_id}", summary="规则集详情",
            dependencies=[Depends(require_permission("rule_set:list"))])
async def detail(user: CurrentUserDep, rule_set_id: str):
    return ok(await ruleset_service.get_rule_set(rule_set_id))


@router.post("", summary="新增规则集", dependencies=[Depends(require_permission("rule_set:create"))])
async def create(user: CurrentUserDep, body: RuleSetIn):
    row = await ruleset_service.create_rule_set(body.model_dump(), user.id)
    return ok(row, message="规则集已新增")


@router.put("/{rule_set_id}", summary="修改规则集",
            dependencies=[Depends(require_permission("rule_set:update"))])
async def update(user: CurrentUserDep, rule_set_id: str, body: RuleSetIn):
    row = await ruleset_service.update_rule_set(rule_set_id, body.model_dump())
    return ok(row, message="规则集已保存")


@router.delete("/{rule_set_id}", summary="删除规则集（删掉生效那条即退回默认规则）",
               dependencies=[Depends(require_permission("rule_set:delete"))])
async def delete(user: CurrentUserDep, rule_set_id: str):
    data = await ruleset_service.delete_rule_set(rule_set_id)
    return ok(data, message="已退回默认规则" if data["reverted_to_default"] else "规则集已删除")


@router.post("/{rule_set_id}/apply", summary="应用（判定与可信度改用这套，并投递全市场回放）",
             dependencies=[Depends(require_permission("rule_set:apply"))])
async def apply(user: CurrentUserDep, rule_set_id: str):
    data = await ruleset_service.apply_rule_set(rule_set_id, user.id)
    replay = data.get("replay") or {}
    if replay.get("status") == "failed":
        # 判定已经切过去了，只是可信度没数：页面要把这两件事分开说
        return ok(data, message=f"已应用，但回放投递失败：{replay.get('error', '')}")
    return ok(data, message="已应用，全市场回放已投递（跑完才有持有可信度）")


@router.post("/{rule_set_id}/replay", summary="重跑全市场回放",
             dependencies=[Depends(require_permission("rule_set:apply"))])
async def replay(user: CurrentUserDep, rule_set_id: str):
    data = await ruleset_service.replay_rule_set(rule_set_id, user.id)
    return ok(data, message="回放任务已投递")
