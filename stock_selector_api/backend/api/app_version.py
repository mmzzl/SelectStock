"""公开版本检查端点（无需鉴权）：
/api/app/latest 供 uniapp 的 H5/App 启动检查；/api/app/version 健康信息。"""
from fastapi import APIRouter

from core.response import ok
from repositories import upgrade_repo

router = APIRouter(prefix="/api/app", tags=["版本检查"])

APP_VERSION = "0.1.0"


@router.get("/latest")
async def latest(platform: str = "h5", version_code: int = 0):
    pkg = await upgrade_repo.latest_published(platform)
    if pkg is None:
        return ok({"has_update": False, "latest": None})
    return ok({
        "has_update": int(pkg["version_code"]) > int(version_code or 0),
        "latest": pkg,
    })


@router.get("/version")
async def version():
    from config import settings

    return ok({"version": APP_VERSION, "name": settings.app_name}, message="ok")
