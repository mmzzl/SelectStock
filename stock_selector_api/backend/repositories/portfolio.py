"""持仓诊断 Repository：run 头 + 明细，按 (user_id, natural_day, batch_id) 幂等。"""

from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun
from repositories._choose import pick
from schemas.portfolio import PortfolioDiagResultCreate, PortfolioDiagRunCreate

RepoBase = pick()


class PortfolioDiagRunRepo(RepoBase):
    model = PortfolioDiagRun
    table = "portfolio_diag_runs"
    schema = PortfolioDiagRunCreate

    async def find_by_key(self, user_id: str, natural_day: str, batch_id: str) -> dict | None:
        return await self._find_one(
            {"user_id": user_id, "natural_day": natural_day, "batch_id": batch_id}
        )

    async def list_by_user(self, user_id: str, page: int = 1, size: int = 20):
        return await self.list(filters={"user_id": user_id}, page=page, size=size)


class PortfolioDiagResultRepo(RepoBase):
    model = PortfolioDiagResult
    table = "portfolio_diag_results"
    schema = PortfolioDiagResultCreate

    async def list_by_run(self, run_id: str) -> list[dict]:
        return await self._find_all(filters={"run_id": run_id})