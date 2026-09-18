"""test_holdings_db.py"""
import pytest

from db.sql import Base
from db.sql.holdings import Holding

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