"""repositories/backtest.py — 回测作业仓储（属主隔离）。"""
from db.sql.backtest import BacktestJob
from repositories._choose import pick
from schemas.backtest import BacktestJobCreate

RepoBase = pick()

# 未结束状态：单飞判定与状态归并都用它
_INFLIGHT = ("queued", "running")


class BacktestJobRepo(RepoBase):
    model = BacktestJob
    table = "backtest_jobs"
    schema = BacktestJobCreate

    async def list_by_user(self, user_id: str, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        return await self.list(filters={"user_id": user_id}, page=page, size=size)

    async def find_inflight(self, user_id: str) -> dict | None:
        """单飞判定走通用过滤助手，不在这里写 SQL/Mongo 专属查询语法。"""
        for status in _INFLIGHT:
            job = await self._find_one({"user_id": user_id, "status": status})
            if job:
                return job
        return None
