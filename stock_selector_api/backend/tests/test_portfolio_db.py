"""持仓诊断双表契约：表名 / 列 / 唯一约束（方案 §3）+ 老库补列迁移幂等。"""

from sqlalchemy import UniqueConstraint

from db.sql import Base
from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun

# 插件新增透传字段（冻结契约，不得改名）
DIAG_EXTRA_COLUMNS = (
    "conviction_pct", "conviction_level", "conviction_text",
    "take_profit_price", "game_v", "game_s", "game_text",
)


def test_tables_registered():
    tables = Base.metadata.tables
    assert "portfolio_diag_runs" in tables
    assert "portfolio_diag_results" in tables


def test_run_columns_and_unique_key():
    tbl = Base.metadata.tables["portfolio_diag_runs"]
    for col in ("run_id", "batch_id", "user_id", "natural_day", "status",
                "held_code_count", "total_market_value"):
        assert col in tbl.columns
    uniques = [c for c in tbl.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(uc.columns.keys()) == {"user_id", "natural_day", "batch_id"} for uc in uniques)


def test_result_columns_and_unique():
    tbl = Base.metadata.tables["portfolio_diag_results"]
    for col in ("run_id", "code", "name", "action", "latest_price",
                "quantity", "holding_pnl", "pnl_pct", "sell_trigger_price",
                *DIAG_EXTRA_COLUMNS):
        assert col in tbl.columns
    uniques = [c for c in tbl.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(uc.columns.keys()) == {"run_id", "code"} for uc in uniques)


def test_models_tablenames():
    assert PortfolioDiagRun.__tablename__ == "portfolio_diag_runs"
    assert PortfolioDiagResult.__tablename__ == "portfolio_diag_results"


def _legacy_diag_table_sql() -> str:
    """老库 DDL：缺 sell_trigger_price 与 7 个扩展字段，用来看迁移是否补齐。"""
    return (
        "CREATE TABLE portfolio_diag_results ("
        "id VARCHAR(36) PRIMARY KEY, created_at DATETIME, updated_at DATETIME, "
        "run_id VARCHAR(36), code VARCHAR(16), name VARCHAR(64) DEFAULT '', "
        "action VARCHAR(8), action_reason VARCHAR(512) DEFAULT '', "
        "latest_price FLOAT DEFAULT 0, quantity INTEGER DEFAULT 0, "
        "holding_pnl FLOAT DEFAULT 0, pnl_pct FLOAT DEFAULT 0, "
        "suggestion VARCHAR(512) DEFAULT '')"
    )


def test_ensure_diag_result_columns_idempotent_on_fresh_db(tmp_path):
    """老库补列：7 个扩展字段 + sell_trigger_price 幂等补齐（同一连接跑两次不报错，数据保留）。"""
    import sqlite3

    from sqlalchemy import create_engine, inspect, text

    from db import _ensure_diag_result_columns

    db_file = tmp_path / "old_diag.db"
    con = sqlite3.connect(str(db_file))
    con.execute(_legacy_diag_table_sql())
    con.execute("INSERT INTO portfolio_diag_results (id, run_id, code, action, name) "
                "VALUES ('h1', 'r1', '600000', 'hold', '浦发银行')")
    con.commit()
    con.close()

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            _ensure_diag_result_columns(conn)
            _ensure_diag_result_columns(conn)  # 幂等：第二次不报错
            cols = {c["name"] for c in inspect(conn).get_columns("portfolio_diag_results")}
            assert {"sell_trigger_price", *DIAG_EXTRA_COLUMNS} <= cols
            # 存量行保留
            assert conn.execute(
                text("SELECT name FROM portfolio_diag_results WHERE id='h1'")).scalar() == "浦发银行"
            # 新字段可写可读
            conn.execute(text(
                "INSERT INTO portfolio_diag_results (run_id, code, action, conviction_pct,"
                " conviction_level, conviction_text, take_profit_price, game_v, game_s, game_text)"
                " VALUES ('r2', '000001', 'sell', 66.5, '中', '逆势', 12.3, 0.1, 0.2, 'g')"))
            row = conn.execute(text(
                "SELECT conviction_pct, conviction_level, conviction_text, take_profit_price,"
                " game_v, game_s, game_text FROM portfolio_diag_results WHERE run_id='r2'")).one()
            assert tuple(row) == (66.5, "中", "逆势", 12.3, 0.1, 0.2, "g")
    finally:
        engine.dispose()


async def test_create_all_twice_upgrades_old_diag_db(tmp_path):
    """走完整 create_all：老库启动两次补列都成功，且 7 个扩展字段齐全。"""
    import sqlite3

    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    import db.base as dbb
    from db import create_all

    db_file = tmp_path / "old_diag_full.db"
    con = sqlite3.connect(str(db_file))
    con.execute(_legacy_diag_table_sql())
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
                lambda c: {x["name"] for x in inspect(c).get_columns("portfolio_diag_results")}
            )
            assert {"sell_trigger_price", *DIAG_EXTRA_COLUMNS} <= cols
    finally:
        dbb._engine = prev
        await engine.dispose()