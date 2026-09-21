# api/v1/upgrade.py
from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field

from core.dependencies import CurrentUserDep, require_roles
from core.exceptions import NotFoundError
from core.response import ok, paged
from repositories import upgrade_repo
from services import upgrade_service

router = APIRouter(prefix="/upgrade/packages", tags=["版本升级"])

_ADMIN = [Depends(require_roles("superadmin"))]


class UpgradeIn(BaseModel):
    platform: str = Field(min_length=2, max_length=16)
    package_type: str = Field(default="", max_length=8)
    version_code: int = Field(ge=1)
    version_name: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=512)
    md5: str = Field(default="", max_length=64)
    size_bytes: int = 0
    release_notes: str = Field(default="", max_length=1024)
    forced: bool = False
    is_published: bool = False


@router.get("", dependencies=_ADMIN, summary="升级包列表")
async def list_packages(platform: str | None = None,
                        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    items, total = await upgrade_repo.list_for_admin(platform, page, size)
    return ok(paged(items, total, page, size))


@router.post("/upload", dependencies=_ADMIN, summary="上传升级文件")
async def upload_file(file: UploadFile = File(...)):
    raw = await file.read()
    return ok(await upgrade_service.save_upload(file.filename or "pkg.bin", raw), message="已上传")


@router.post("", dependencies=_ADMIN, summary="新增升级包")
async def create_package(user: CurrentUserDep, body: UpgradeIn):
    return ok(await upgrade_service.create_package(body.model_dump(), user.username), message="已创建")


@router.put("/{pkg_id}", dependencies=_ADMIN, summary="更新升级包")
async def update_package(user: CurrentUserDep, pkg_id: str, body: dict):
    returning = await upgrade_service.update_package(pkg_id, body, user.username)
    if not returning:
        raise NotFoundError("升级包不存在")
    return ok(returning, message="已更新")


@router.delete("/{pkg_id}", dependencies=_ADMIN, summary="删除升级包")
async def delete_package(pkg_id: str):
    if not await upgrade_service.delete_package(pkg_id):
        raise NotFoundError("升级包不存在")
    return ok(message="已删除")
