"""回测结果 MongoDB 落库与读取。"""

from __future__ import annotations

from datetime import datetime, timezone

from stock_selector.backtest.models import BacktestResult

COLLECTION_RESULTS = "backtest_results"


class BacktestStore:
    """将回测结果写入 MongoDB（结果、交易、净值三个集合）。"""

    def __init__(self, db) -> None:
        # db: pymongo Database 对象；便于测试注入或替换
        self.db = db

    def save_result(self, result: BacktestResult) -> None:
        summary = {
            "task_id": result.task_id,
            "strategy": result.strategy,
            "params": result.params,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "metrics": result.metrics,
            "executability": result.executability,
            "saved_at": datetime.now(timezone.utc),
        }
        self.db[COLLECTION_RESULTS].replace_one(
            {"task_id": result.task_id}, summary, upsert=True
        )
        if result.trades:
            trades = [dict(t, task_id=result.task_id) for t in result.trades]
            self.db["backtest_trades"].delete_many({"task_id": result.task_id})
            self.db["backtest_trades"].insert_many(trades)
        if result.equity:
            equity = [dict(e, task_id=result.task_id) for e in result.equity]
            self.db["backtest_equity"].delete_many({"task_id": result.task_id})
            self.db["backtest_equity"].insert_many(equity)

    def load_summary(self, task_id: str) -> dict | None:
        return self.db[COLLECTION_RESULTS].find_one({"task_id": task_id})

    def load_trades(self, task_id: str) -> list[dict]:
        return list(self.db["backtest_trades"].find({"task_id": task_id}, {"_id": 0}))

    def load_equity(self, task_id: str) -> list[dict]:
        return list(
            self.db["backtest_equity"].find({"task_id": task_id}, {"_id": 0}).sort("date", 1)
        )
