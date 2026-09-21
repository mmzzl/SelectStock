"""repositories/upgrade.py — 升级包仓储（按平台取最新发布版 / 管理端分页）。"""
from sqlalchemy import select

from db.sql.upgrade import UpgradePackage
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.upgrade import UpgradeCreate

RepoBase = pick()


class UpgradeRepo(RepoBase):
    model = UpgradePackage
    table = "upgrade_packages"
    schema = UpgradeCreate

    async def latest_published(self, platform: str) -> dict | None:
        async with self._session() as s:
            stmt = (select(self.model).where(self.model.platform == platform,
                                             self.model.is_published.is_(True))
                    .order_by(self.model.version_code.desc()).limit(1))
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None

    async def list_for_admin(self, platform: str | None = None, page: int = 1, size: int = 20):
        filters = {"platform": platform} if platform else None
        return await self.list(filters=filters, page=page, size=size)
