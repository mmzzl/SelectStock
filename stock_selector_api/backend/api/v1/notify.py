"""通知组件接口：事件目录 / 渠道配置 CRUD+测试发送 / 订阅策略 CRUD。

权限：list 类需要 sys:notify:list，写与测试需要 sys:notify:update。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.dependencies import require_permission
from core.exceptions import BizError
from core.response import ok
from services import notify_service
from services.notify import channels, events, policies

router = APIRouter(prefix="/notify", tags=["通知"])


class ChannelIn(BaseModel):
    type: str
    key: str = Field(min_length=1, max_length=64)
    name: str = ""
    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    host: str = ""
    port: int | None = None
    use_ssl: bool = False
    username: str = ""
    password: str = ""
    from_email: str = ""
    from_name: str = ""


class TestSendIn(BaseModel):
    text: str = "这是一条测试消息"
    to: list[str] = []


class IntervalIn(BaseModel):
    value: int = 0
    unit: str = "m"


class EventSubIn(BaseModel):
    is_on: bool = False
    interval: IntervalIn = Field(default_factory=IntervalIn)
    subject: str = ""


class ChannelSubIn(BaseModel):
    is_on: bool = False
    channel_key: str = ""
    to: list[str] = Field(default_factory=list)
    template: str = ""


class PolicyIn(BaseModel):
    """一条订阅策略：事件订阅（各自节流）+ 渠道绑定。"""

    name: str = Field(min_length=1, max_length=64)
    is_on: bool = True
    events: dict[str, EventSubIn] = Field(default_factory=dict)
    channels: dict[str, ChannelSubIn] = Field(default_factory=dict)


@router.get("/events", summary="事件目录与渠道类型", dependencies=[Depends(require_permission("sys:notify:list"))])
async def event_catalog():
    return ok({
        "events": events.EVENT_DEFS,
        "channel_types": [
            {"type": t, "label": m["label"], "interface": m["interface"],
             "fields": m["fields"], "secret_fields": m["secret_fields"]}
            for t, m in channels.CHANNEL_TYPES.items()
        ],
    })


@router.get("/channels", summary="渠道配置列表", dependencies=[Depends(require_permission("sys:notify:list"))])
async def list_channels(channel_type: str | None = None):
    if channel_type and channel_type not in channels.CHANNEL_TYPES:
        raise BizError("未知渠道类型")
    return ok(channels.list_all(channel_type))


@router.post("/channels", summary="新增渠道配置", dependencies=[Depends(require_permission("sys:notify:update"))])
async def create_channel(body: ChannelIn):
    ch = await _upsert_channel(body)
    return ok(ch, message="渠道配置已保存")


@router.put("/channels/{key}", summary="更新渠道配置", dependencies=[Depends(require_permission("sys:notify:update"))])
async def update_channel(key: str, body: ChannelIn):
    if key != body.key:
        raise BizError("路径与请求体 key 不一致")
    ch = await _upsert_channel(body)
    return ok(ch, message="渠道配置已保存")


async def _upsert_channel(body: ChannelIn) -> dict:
    if body.type not in channels.CHANNEL_TYPES:
        raise BizError("未知渠道类型")
    try:
        return channels.upsert(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise BizError(str(e))


@router.delete("/channels/{key}", summary="删除渠道配置", dependencies=[Depends(require_permission("sys:notify:update"))])
async def delete_channel(key: str):
    if not channels.delete(key):
        raise BizError("渠道不存在")
    return ok(message="渠道已删除")


@router.post("/channels/{key}/test", summary="测试发送", dependencies=[Depends(require_permission("sys:notify:update"))])
async def test_channel(key: str, body: TestSendIn):
    ch = channels.get_by_key(key)
    if not ch:
        raise BizError("渠道不存在")
    if ch.get("type") == "email" and not body.to:
        raise BizError("邮箱渠道测试需填写收件人")
    await notify_service.test_send(key, body.text, to=body.to)
    return ok(message="测试消息已发送")


@router.get("/policies", summary="订阅策略列表", dependencies=[Depends(require_permission("sys:notify:list"))])
async def list_policies():
    return ok(policies.list_all())


@router.get("/policies/{policy_id}", summary="订阅策略详情", dependencies=[Depends(require_permission("sys:notify:list"))])
async def get_policy(policy_id: str):
    policy = policies.get(policy_id)
    if not policy:
        raise BizError("订阅策略不存在")
    return ok(policy)


@router.post("/policies", summary="新增订阅策略", dependencies=[Depends(require_permission("sys:notify:update"))])
async def create_policy(body: PolicyIn):
    try:
        return ok(policies.create(body.model_dump()), message="订阅策略已新增")
    except ValueError as e:
        raise BizError(str(e))


@router.put("/policies/{policy_id}", summary="更新订阅策略", dependencies=[Depends(require_permission("sys:notify:update"))])
async def update_policy(policy_id: str, body: PolicyIn):
    try:
        saved = policies.update(policy_id, body.model_dump())
    except ValueError as e:
        raise BizError(str(e))
    if not saved:
        raise BizError("订阅策略不存在")
    return ok(saved, message="订阅策略已保存")


@router.delete("/policies/{policy_id}", summary="删除订阅策略", dependencies=[Depends(require_permission("sys:notify:update"))])
async def delete_policy(policy_id: str):
    if not policies.delete(policy_id):
        raise BizError("订阅策略不存在")
    return ok(message="订阅策略已删除")
