"""升级包：落库 / 文件上传 / 删除。上传文件存 settings.upgrade_upload_dir。"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from config import settings
from core.exceptions import BizError
from repositories import upgrade_repo


async def save_upload(filename: str, raw: bytes) -> dict:
    if not filename or not raw:
        raise BizError("文件为空")
    upload_dir = Path(settings.upgrade_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name          # 只取文件名，防路径穿越
    target = upload_dir / f"{hashlib.md5(raw).hexdigest()[:16]}_{safe}"
    target.write_bytes(raw)
    md5 = hashlib.md5(raw).hexdigest()
    rel = f"/uploads/upgrade/{target.name}"
    return {"url": rel, "md5": md5, "size_bytes": len(raw)}


async def create_package(data: dict, admin: str) -> dict:
    data.setdefault("publish_at", "" if not data.get("is_published") else date.today().isoformat())
    return await upgrade_repo.create({**data, "created_by": admin})


async def update_package(pkg_id: str, data: dict, admin: str) -> dict | None:
    if data.get("is_published") is True:
        data.setdefault("publish_at", date.today().isoformat())
    return await upgrade_repo.update(pkg_id, data)


async def delete_package(pkg_id: str) -> bool:
    return await upgrade_repo.delete(pkg_id)
