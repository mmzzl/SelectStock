"""验证码 Repository：通用 CRUD + 会话绑定查询。"""

from db.sql.captcha import Captcha
from repositories._choose import pick

RepoBase = pick()


class CaptchaRepo(RepoBase):
    model = Captcha
    table = "captchas"

    async def get_by_id(self, captcha_id: str) -> dict | None:
        return await self._find_one({"id": captcha_id})

    async def mark_used(self, captcha_id: str) -> bool:
        """原子烧码（乐观锁）：仅当未使用才置 used=True，返回是否命中。"""
        return await self._update_where_one(
            "captchas", {"id": captcha_id, "used": False}, {"used": True}
        )
