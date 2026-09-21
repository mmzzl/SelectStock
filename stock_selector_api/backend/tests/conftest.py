"""pytest fixtures：会话级 SQLite 测试库（临时文件 + WAL） + seed + AsyncClient。

测试固定使用 sqlite，保证无外部依赖可离线跑通。
AUTH_MODE 可在用例内切换 settings.auth_mode（默认 jwt）。
M3 起背景任务（asyncio.create_task）会与测试等待并发访问 DB：
  - 单连接 StaticPool：并发 AsyncSession 交错事务，偶发提交丢失；
  - 共享内存多连接：并发写锁导致 "database table is locked"。
文件库 + WAL + busy_timeout 等价生产连接池语义，读写互不阻塞。
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from config import settings

_TEST_DB: str | None = None


@pytest.fixture(scope="session", autouse=True)
def _test_env(tmp_path_factory):
    """强制测试环境：sqlite 文件库 + 关闭调度器 + 通知配置/策略落到临时目录（不污染仓库 conf 样例）。"""
    global _TEST_DB
    settings.db_backend = "sqlite"
    _TEST_DB = str(tmp_path_factory.mktemp("db") / "test.db")
    settings.database_url = None  # get_engine 被替换，此值不再使用
    settings.schedule_enabled = False
    settings.plugin_mode = "mock"
    settings.selection_plugin_mode = "mock"
    # as_posix()：保持「路径含 /upgrade」契约（test_config_keys），与 config.py 默认值行为一致
    from pathlib import Path as _P

    settings.upgrade_upload_dir = _P(str(tmp_path_factory.mktemp("upgrade") / "upgrade")).as_posix()
    settings.notify_channels_file = str(tmp_path_factory.mktemp("ch") / "channels.json")
    settings.notify_policy_file = str(tmp_path_factory.mktemp("po") / "policy.json")
    settings.notify_state_file = str(tmp_path_factory.mktemp("st") / "state.json")
    from services.notify_service import save_policy

    save_policy({})
    yield
    _TEST_DB = None


@pytest.fixture(scope="session", autouse=True)
async def _init_db(_test_env):
    """用临时文件引擎建表 + seed（多连接 + busy_timeout，并发安全）。"""
    import db.base as dbb
    from db.sql import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{_TEST_DB}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=AsyncAdaptedQueuePool,
        pool_size=10,
        max_overflow=0,
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
async def _clean_holdings_each():
    """每个测试前后清空 holdings 表：避免共享内存库残留持仓行跨用例污染。"""
    from repositories import holdings_repo

    await holdings_repo._delete_from("holdings", {})
    yield
    await holdings_repo._delete_from("holdings", {})


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
