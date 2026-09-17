"""通知组件接口：事件目录 / 渠道配置 CRUD+测试发送 / 订阅策略读写。

权限：list 类需要 sys:notify:list，写与测试需要 sys:notify:update。
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.dependencies import require_permission
from core.exceptions import BizError
from core.response import ok
from services import notify_service
from services.notify import channels, events

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


class PolicyIn(BaseModel):
    policy: dict[str, Any] = Field(default_factory=dict)


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


@router.get("/policy", summary="读取订阅策略", dependencies=[Depends(require_permission("sys:notify:list"))])
async def get_policy():
    return ok(notify_service.load_policy())


@router.put("/policy", summary="保存订阅策略", dependencies=[Depends(require_permission("sys:notify:update"))])
async def put_policy(body: PolicyIn):
    saved = notify_service.save_policy(body.policy)
    return ok(saved, message="订阅策略已保存")
