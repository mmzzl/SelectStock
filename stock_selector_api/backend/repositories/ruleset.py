"""repositories/ruleset.py — 规则集仓储：列表/详情/当前生效行/应用切换。

「当前生效」只有一行（is_active=True），这是插件读 active.json 之前唯一的真源；
写文件由 services/ruleset_service 负责，这里只管行。
"""
from db.sql.ruleset import RuleSet
from repositories._choose import pick
from schemas.ruleset import RuleSetCreate

RepoBase = pick()


class RuleSetRepo(RepoBase):
    model = RuleSet
    table = "rule_sets"
    schema = RuleSetCreate

    async def list_all(self, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        # 不带 filters：_list 的排序键是 created_at desc，最新建的排最前，生效那条靠 is_active 标出
        return await self.list(page=page, size=size)

    async def find_active(self) -> dict | None:
        return await self._find_one({"is_active": True})

    async def deactivate_all(self, except_id: str = "") -> int:
        """把除 except_id 以外的所有行置为未生效，返回被改掉的行数。

        逐行 update 而不是批量 SQL：基类只暴露跨 DB 的通用助手（这里还有 Mongo 后端），
        写一条 `UPDATE ... WHERE id != ?` 会让 Mongo 侧永远走不到同一语义。
        """
        rows = await self._find_all({"is_active": True})
        changed = 0
        for row in rows:
            if row["id"] == except_id:
                continue
            await self.update(row["id"], {"is_active": False})
            changed += 1
        return changed

    async def find_replaying(self) -> list[dict]:
        """排队/运行中的回放：投递前用它挡住重复点击（一台机器只扛得住一路全市场回放）。"""
        out: list[dict] = []
        for status in ("queued", "running"):
            out.extend(await self._find_all({"replay_status": status}))
        return out
