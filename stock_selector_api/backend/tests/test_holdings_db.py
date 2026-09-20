"""test_holdings_db.py"""
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from db.sql import Base
from db.sql.holdings import Holding
from repositories import holdings_repo

TABLE = "holdings"


def test_table_registered():
    assert TABLE in Base.metadata.tables


def test_columns():
    cols = {c.name: c for c in Holding.__table__.columns}
    assert {"user_id", "code", "name", "quantity", "cost_price", "enabled"} <= set(cols)
    assert cols["quantity"].type.python_type is int
    assert cols["cost_price"].type.python_type is float
    assert cols["enabled"].default.arg is True


def test_unique_user_code():
    uniq = [c.name for c in Holding.__table__.constraints
            if getattr(c, "name", "") == "uq_holdings_user_code"]
    assert uniq


def test_holdings_schemas_marker_fields():
    from schemas.holdings import HoldingCreate, HoldingIn, HoldingOut, HoldingUpdate

    for cls in (HoldingCreate, HoldingIn, HoldingOut):
        f = cls.model_fields
        assert {"derived_from_trades", "last_derived_at"} <= set(f)
        assert f["derived_from_trades"].default is False
        assert f["last_derived_at"].default is None
    upd = HoldingUpdate.model_fields
    assert {"derived_from_trades", "last_derived_at"} <= set(upd)
    assert upd["derived_from_trades"].default is None
    assert upd["last_derived_at"].default is None


async def test_derived_marker_columns():
    from db.sql.holdings import Holding
    from db.sql.base import Base
    cols = {c.name for c in Holding.__table__.columns}
    assert {"derived_from_trades", "last_derived_at"} <= cols

    created = await holdings_repo.create({
        "user_id": "der-1", "code": "600519", "name": "茅台",
        "quantity": 100, "cost_price": 1680.5,
        "derived_from_trades": True,
    })
    assert created["derived_from_trades"] is True

    ts = datetime.now(timezone.utc)
    updated = await holdings_repo.update(created["id"], {"last_derived_at": ts})
    assert updated["last_derived_at"] is not None


async def test_create_all_migrates_old_holdings_db(tmp_path):
    """老库启动补列：既有 holdings 表缺推导标记列，create_all 幂等 ALTER 补列且数据保留。"""
    import sqlite3

    from sqlalchemy import inspect, text

    import db.base as dbb
    from db import create_all

    db_file = tmp_path / "old_schema.db"
    con = sqlite3.connect(str(db_file))
    con.execute(
        "CREATE TABLE holdings ("
        "id VARCHAR(36) PRIMARY KEY, created_at DATETIME, updated_at DATETIME, "
        "user_id VARCHAR(64), code VARCHAR(16), name VARCHAR(64), "
        "quantity INTEGER NOT NULL, cost_price FLOAT NOT NULL DEFAULT 0, "
        "enabled BOOLEAN NOT NULL DEFAULT 1)"
    )
    con.execute(
        "INSERT INTO holdings (id, user_id, code, name, quantity, cost_price) "
        "VALUES ('h1', 'legacy-1', '600000', '浦发银行', 10, 10.5)"
    )
    con.commit()
    con.close()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    prev = dbb._engine
    dbb._engine = engine
    try:
        await create_all()
        await create_all()  # 幂等：第二次调用不报错

        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda c: {x["name"] for x in inspect(c).get_columns("holdings")}
            )
            assert {"derived_from_trades", "last_derived_at"} <= cols
            name = (
                await conn.execute(text("SELECT name FROM holdings WHERE id = 'h1'"))
            ).scalar_one()
            assert name == "浦发银行"
    finally:
        dbb._engine = prev
        await engine.dispose()