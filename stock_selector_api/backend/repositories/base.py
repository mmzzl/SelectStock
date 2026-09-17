"""Repository 协议：数据访问的统一行为契约（跨 sqlite/mongodb）。

业务层（services）只依赖本协议：传入 dict，返回 dict。
所有 ID 为 UUID 字符串，时间戳为 datetime（序列化层统一转 ISO 字符串）。
"""

from typing import Any, Protocol


class BaseRepository(Protocol):
    async def create(self, data: dict) -> dict: ...
    async def get(self, id: str) -> dict | None: ...
    async def list(
        self,
        *,
        filters: dict | None = None,
        keyword: str | None = None,
        keyword_fields: list[str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict], int]: ...
    async def update(self, id: str, data: dict) -> dict | None: ...
    async def delete(self, id: str) -> bool: ...
    async def count(self, filters: dict | None = None) -> int: ...
    async def exists(self, filters: dict) -> bool: ...
