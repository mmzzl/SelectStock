import importlib.util

import pandas as pd

SCRIPT = "scripts/merge_snapshot_amount.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("merge_snapshot_amount", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCollection:
    def __init__(self):
        self.ops = []

    def bulk_write(self, ops, ordered=False):
        self.ops.extend(ops)

        class _Result:
            matched_count = len(ops)
            modified_count = len(ops)

        return _Result()


def test_merge_day_skips_unknown_amount_and_never_upserts(tmp_path, monkeypatch):
    """快照里的 0/空是停牌或退市，不能落成「真·零成交」；日线行归爬虫建，这里只补字段。"""
    mod = _load_script()
    monkeypatch.setattr(mod, "SNAP_DIR", tmp_path)
    pd.DataFrame({
        "code": ["000001", "000002", "600000", "159915"],
        "amount": [1.19e9, 0.0, None, 5.5e7],
    }).to_parquet(tmp_path / "2026-10-14.parquet")

    col = _FakeCollection()
    monkeypatch.setattr(mod, "get_db", lambda: {"stock_kline": col})
    result = mod.merge_day("2026-10-14")

    assert result["candidates"] == 2
    assert len(col.ops) == 2
    filters = [op._filter for op in col.ops]
    assert {"code": "000001", "date": "2026-10-14 15:00", "frequency": 9} in filters
    assert {"code": "159915", "date": "2026-10-14 15:00", "frequency": 9} in filters
    assert all(op._upsert is False for op in col.ops)
    assert all("$set" in op._doc and set(op._doc["$set"]) == {"amount"} for op in col.ops)


def test_days_selection_is_by_existing_snapshot_files(tmp_path, monkeypatch):
    """往回取 N 天按目录里实际存在的文件（周末没有快照），这是 --days 自愈的前提。"""
    mod = _load_script()
    monkeypatch.setattr(mod, "SNAP_DIR", tmp_path)
    for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
        pd.DataFrame({"code": ["000001"], "amount": [1.0]}).to_parquet(
            tmp_path / f"{day}.parquet")
    (tmp_path / "notes.parquet").write_text("x")     # 非日期文件不能混进来
    assert mod.snapshot_days() == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert mod.snapshot_days()[-2:] == ["2026-10-13", "2026-10-14"]
