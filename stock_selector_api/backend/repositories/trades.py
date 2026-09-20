from sqlalchemy import select

from db.sql.trades import TradeRecord
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.trades import TradeCreate

RepoBase = pick()


class TradeRepo(RepoBase):
    model = TradeRecord
    table = "trade_records"
    schema = TradeCreate

    async def list_by_user(
        self, user_id: str, *, code: str | None = None,
        direction: str | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[dict]:
        async with self._session() as s:
            stmt = select(self.model).where(self.model.user_id == user_id)
            if code:
                stmt = stmt.where(self.model.stock_code == code)
            if direction:
                stmt = stmt.where(self.model.direction == direction)
            if date_from:
                stmt = stmt.where(self.model.trade_date >= date_from)
            if date_to:
                stmt = stmt.where(self.model.trade_date <= date_to)
            stmt = stmt.order_by(self.model.trade_date.desc(), self.model.created_at.desc()).limit(1000)
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]

    async def list_by_batch(self, batch_id: str) -> list[dict]:
        return await self._find_all(filters={"batch_id": batch_id}, order_by="trade_date")

    async def list_all_by_user(self, user_id: str) -> list[dict]:
        """利润/推导入参：全部按代码+日期正序。"""
        async with self._session() as s:
            stmt = (
                select(self.model)
                .where(self.model.user_id == user_id)
                .order_by(self.model.stock_code.asc(), self.model.trade_date.asc(), self.model.created_at.asc())
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]


trades_repo = TradeRepo()