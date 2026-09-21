"""FastAPI 应用入口：启动时初始化 DB + seed + 定时任务，并托管静态前端。"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings, BACKEND_DIR
from api.external import router as external_router
from api.v1.router import api_router
from api.v1.portfolio import router as portfolio_router
from core.logger import setup_logging, log
from core.exceptions import register_exception_handlers

# 静态前端目录（frontend/static），从 backend/main.py 向上两级
FRONTEND_DIR = BACKEND_DIR.parent / "frontend" / "static"


_DEFAULT_JWT_SECRET = "change-me-jwt-secret-at-least-32-chars"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("启动 {}（auth_mode={}, db={}）",
             settings.app_name, settings.auth_mode, settings.db_backend)
    if settings.auth_mode == "jwt" and settings.jwt_secret == _DEFAULT_JWT_SECRET:
        log.warning("检测到 JWT_SECRET 仍为默认值！任何知晓该值者都可伪造令牌，生产部署前务必修改 .env 中的 JWT_SECRET")
    await _init_db()
    if settings.schedule_enabled:
        await _init_scheduler()
    yield
    if settings.schedule_enabled:
        _shutdown_scheduler()
    await _shutdown_db()


async def _init_db() -> None:
    from db import init_database

    await init_database()


async def _shutdown_db() -> None:
    from db import dispose

    await dispose()


async def _init_scheduler() -> None:
    from scheduler.scheduler_ import setup_scheduler

    setup_scheduler()


def _shutdown_scheduler() -> None:
    from scheduler.scheduler_ import shutdown_scheduler

    shutdown_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="开箱即用 FastAPI 模板（RBAC + JWT/Session + SQLite/Mongo + 动态菜单 + inputs.conf 定时任务）",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 生产环境请改为具体域名
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    # 持仓诊断外部契约路由（方案 §2/§3）：POST /api/portfolio/analyze · GET /api/portfolio/runs
    app.include_router(portfolio_router, prefix="/api")
    # M3 外部调度契约：POST /api/external/portfolio/diag
    app.include_router(external_router, prefix="/api")
    # M2 WS 通知端点：/ws/notify（首帧鉴权 → hello → ping/pong；须在静态挂载之前）
    from api.ws_notify import router as ws_router

    app.include_router(ws_router)

    # 托管无构建前端（若 frontend/static 存在）
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
