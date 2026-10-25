"""repositories/selection.py — 选股任务/结果仓储（属主隔离）。"""
from sqlalchemy import select

from db.sql.selection import SelectJob, SelectResult
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.selection import SelectJobCreate, SelectResultCreate

RepoBase = pick()

# 未结束状态：单飞判定用（queued 已入队未跑也算占用）
_INFLIGHT = ("queued", "running")


class SelectJobRepo(RepoBase):
    model = SelectJob
    table = "select_jobs"
    schema = SelectJobCreate

    async def list_by_user(self, user_id: str, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        return await self.list(filters={"user_id": user_id}, page=page, size=size)

    async def find_running(self, user_id: str) -> dict | None:
        async with self._session() as s:
            stmt = (select(self.model).where(self.model.user_id == user_id,
                                             self.model.status.in_(_INFLIGHT))
                    .order_by(self.model.created_at.desc()).limit(1))
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None

    async def find_latest(self, user_id: str) -> dict | None:
        async with self._session() as s:
            stmt = (select(self.model).where(self.model.user_id == user_id)
                    .order_by(self.model.created_at.desc()).limit(1))
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None


class SelectResultRepo(RepoBase):
    model = SelectResult
    table = "select_results"
    schema = SelectResultCreate

    async def list_by_job(self, job_id: str) -> list[dict]:
        # 必须显式排序：qlib 引擎下 risk_ex10_pp 全是 null，App 的 sortByRiskDesc 对同值保序，
        # 于是页面顺序 = 本接口的顺序 = 模型名次。不写 ORDER BY 就只剩「SQLite 恰好按插入序返回」
        # 这个未定义行为在兜着，一旦换后端存储（Mongo 无序）名次就散了。
        # rank_no 为 NULL 的旧行（自研内核没有全池名次）排最后；SQL/Mongo 的 NULL 默认序不同，
        # 所以在 Python 里收口，别让换存储把「没名次」排到第一。
        rows = await self._find_all(filters={"job_id": job_id})
        # 混合批次里模型榜必须排在旧 hot_pref 前面：两条榜都从 1 开始编号，
        # 只按 rank 排会让旧榜的 #1 抢在模型 #1 前面。页面也按同一规则兜底。
        return sorted(rows, key=lambda r: (0 if r.get("strategy") == "qlib_alpha158" else 1,
                                           r.get("rank_no") is None,
                                           r.get("rank_no") if r.get("rank_no") is not None else 0,
                                           str(r.get("code") or "")))

    async def delete_by_job(self, job_id: str) -> None:
        """任务失败时清掉半截结果：页面不能把「写了一半的名单」当成完整名单展示。"""
        await self._delete_from(self.table, {"job_id": job_id})
