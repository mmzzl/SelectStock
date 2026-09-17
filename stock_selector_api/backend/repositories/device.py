"""设备（示例模块）Repository：通用 CRUD + 状态统计。"""

from db.sql.device import Device
from repositories._choose import pick
from schemas.device import DeviceCreate

RepoBase = pick()


class DeviceRepo(RepoBase):
    model = Device
    table = "devices"
    schema = DeviceCreate  # 写字段白名单（SQL/Mongo 两侧一致，防 mass-assignment）

    async def status_count(self) -> dict[str, int]:
        """按 status 字段统计数量（跨 DB 通用实现：内存统计）。"""
        rows = await self._find_all()
        from collections import Counter

        counter = Counter(r.get("status", "") for r in rows)
        return {k: v for k, v in counter.items()}
