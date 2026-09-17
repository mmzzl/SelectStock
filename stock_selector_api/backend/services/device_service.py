"""设备（示例模块）服务：演示业务层如何快速组装分页/搜索 CRUD。"""

from core.exceptions import NotFoundError
from repositories import devices


async def list_devices(page: int, size: int, keyword: str | None, status: str | None):
    return await devices.list(
        page=page,
        size=size,
        keyword=keyword,
        keyword_fields=["name", "ip", "vendor"],
        filters={"status": status} if status else None,
    )


async def get_device(device_id: str) -> dict:
    d = await devices.get(device_id)
    if not d:
        raise NotFoundError("设备不存在")
    return d


async def create_device(data: dict) -> dict:
    return await devices.create(data)


async def update_device(device_id: str, data: dict) -> dict:
    d = await devices.update(device_id, data)
    if not d:
        raise NotFoundError("设备不存在")
    return d


async def delete_device(device_id: str) -> None:
    if not await devices.delete(device_id):
        raise NotFoundError("设备不存在")


async def summary() -> dict:
    total = await devices.count()
    by_status = await devices.status_count()
    return {"total": total, "by_status": by_status}
