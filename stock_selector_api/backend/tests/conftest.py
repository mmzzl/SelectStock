"""pytest fixtures：内存 SQLite(StaticPool 共享连接) + seed + AsyncClient。

测试固定使用 sqlite 内存库，保证无外部依赖可离线跑通。
AUTH_MODE 可在用例内切换 settings.auth_mode（默认 jwt）。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from config import settings


@pytest.fixture(scope="session", autouse=True)
def _test_env(tmp_path_factory):
    """强制测试环境：sqlite 内存 + 关闭调度器 + 通知配置/策略落到临时目录（不污染仓库 conf 样例）。"""
    settings.db_backend = "sqlite"
    settings.database_url = None  # get_engine 被替换，此值不再使用
    settings.schedule_enabled = False
    settings.plugin_mode = "mock"
    settings.notify_channels_file = str(tmp_path_factory.mktemp("ch") / "channels.json")
    settings.notify_policy_file = str(tmp_path_factory.mktemp("po") / "policy.json")
    settings.notify_state_file = str(tmp_path_factory.mktemp("st") / "state.json")
    from services.notify_service import save_policy

    save_policy({})
    yield


@pytest.fixture(scope="session", autouse=True)
async def _init_db():
    """用共享内存引擎建表 + seed。"""
    import db.base as dbb
    from db.sql import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbb._engine = engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from db.seed import seed_all

    await seed_all()
    yield
    await engine.dispose()


@pytest.fixture()
async def client():
    from main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean_login_locks():
    """每个测试后清空锁定表：避免共享测试 IP 使 ip 维度计数跨用例污染。"""
    yield
    from repositories import login_locks

    rows = await login_locks._find_all()
    for r in rows:
        await login_locks.clear_key(r["lock_key"])


@pytest.fixture(autouse=True)
async def _reset_notify_each():
    """每个测试前重置通知策略与节流状态：避免订阅开关/发送时间点跨用例污染。"""
    from services import notify_service

    notify_service._save_state({})
    notify_service.save_policy({})
    yield


@pytest.fixture()
async def admin_token(client):
    """超管管理员登录令牌（走验证码链路）。"""
    from .helpers import login_with_captcha

    r = await login_with_captcha(client, "admin", "admin123")
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


@pytest.fixture()
async def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
