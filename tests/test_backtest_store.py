"""BacktestStore tests (TDD) — 依赖本地 Mongo，不可达则跳过。"""

import pytest
import pymongo


def _mongo_available() -> bool:
    try:
        c = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _mongo_available(), reason="local mongo unavailable"
)


@pytest.fixture
def store_db():
    client = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
    db = client["eastmoney_news"]
    yield db
    client.close()


@pytest.fixture
def sample_result():
    from stock_selector.backtest.models import BacktestResult

    return BacktestResult(
        task_id="test-task-001",
        strategy="rules",
        params={"capital": 100000},
        start_date="2026-09-01",
        end_date="2026-09-02",
        metrics={"total_return": 0.05, "trade_count": 1},
        trades=[{"code": "000001", "side": "buy", "date": "2026-09-01",
                 "shares": 100, "amount": 1000.0, "fee": 5.0}],
        equity=[{"date": "2026-09-01", "total_value": 100000.0},
                {"date": "2026-09-02", "total_value": 105000.0}],
    )


def _cleanup(store_db, task_id):
    store_db["backtest_results"].delete_many({"task_id": task_id})
    store_db["backtest_trades"].delete_many({"task_id": task_id})
    store_db["backtest_equity"].delete_many({"task_id": task_id})


class TestStore:
    def test_save_result_persists_to_three_collections(self, store_db, sample_result):
        from stock_selector.backtest.store import BacktestStore

        store = BacktestStore(store_db)
        _cleanup(store_db, sample_result.task_id)
        store.save_result(sample_result)

        assert store_db["backtest_results"].find_one(
            {"task_id": sample_result.task_id}
        )["metrics"]["total_return"] == 0.05
        assert store_db["backtest_trades"].count_documents(
            {"task_id": sample_result.task_id}
        ) == 1
        assert store_db["backtest_equity"].count_documents(
            {"task_id": sample_result.task_id}
        ) == 2

    def test_load_summary_trades_equity(self, store_db, sample_result):
        from stock_selector.backtest.store import BacktestStore

        store = BacktestStore(store_db)
        _cleanup(store_db, sample_result.task_id)
        store.save_result(sample_result)

        summary = store.load_summary(sample_result.task_id)
        assert summary["strategy"] == "rules"
        assert len(store.load_trades(sample_result.task_id)) == 1
        assert len(store.load_equity(sample_result.task_id)) == 2
