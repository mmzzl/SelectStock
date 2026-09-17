"""持仓诊断双表契约：表名 / 列 / 唯一约束（方案 §3）。"""

from sqlalchemy import UniqueConstraint

from db.sql import Base
from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun


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
                "quantity", "holding_pnl", "pnl_pct"):
        assert col in tbl.columns
    uniques = [c for c in tbl.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(uc.columns.keys()) == {"run_id", "code"} for uc in uniques)


def test_models_tablenames():
    assert PortfolioDiagRun.__tablename__ == "portfolio_diag_runs"
    assert PortfolioDiagResult.__tablename__ == "portfolio_diag_results"