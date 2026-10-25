"""realtime/pool.py — 盘中监控池：最近一次选股命中 + 当前持仓，从 app.db 只读取出。

为什么要收敛：全市场 6600 只里，盘中真正 actionable 的只有两类——「选股命中的那批」（等
买点）和「已经拿在手里的持仓」（等卖点）。剩下的票每拍转一遍快照，只是把免费接口的配额
吃光、让下游引擎算没人看的东西。

池的真源是 app 侧的库而不是第二份清单：批次在 `select_jobs`/`select_results`，持仓在
`holdings`，页面里改了下一交易日就生效。注意选股是**盘后**产的（17:30），所以「今天开盘
要盯的池」读的是昨天收盘后那一批——这是设计，不是拿错了批次。

只读（`mode=ro`）：这个模块在行情链路里，绝不能因为读池子把 app 的库锁住或写坏。读不到
就明确报错，宁可退回人工指定的池，也不要静默变成「一只都没监控」。
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 相对本文件定位，不用环境变量也不用后端配置：盘中链路和 app 在同一台机器、同一个仓库。
DEFAULT_DB = Path(__file__).resolve().parents[2] / "stock_selector_api" / "data" / "app.db"

# 批次超过这个小时数就 WARN：跨一个周末的正常空档约 64 小时（周五 17:30 → 周一 09:25），
# 阈值要留在这个之上，否则每个周一早上都白报一次；真正的调度器故障（多天没出批次）才会越线。
STALE_HOURS = 96.0


class PoolError(RuntimeError):
    """池读不出来（库缺失、表缺失、没有任何代码）。调用方要停下来问人，不能猜一个池。"""


def _naive(moment: dt.datetime) -> dt.datetime:
    """注入的时钟可能带时区，批次时间一律本地 naive：比龄前先统一。"""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


@dataclass
class MonitorPool:
    codes: list[str] = field(default_factory=list)
    held: dict[str, float] = field(default_factory=dict)   # {代码: 成本市值(元)}
    selected_at: dt.datetime | None = None                 # 批次时间，本地
    job_id: str | None = None
    generated_at: dt.datetime = field(default_factory=dt.datetime.now)
    # 买入名额与 T+1 要的两项「今天」的事实。都在 app.db 里，读不到就留空/留 None，
    # 由风控那两条规则自己按「判不了就放行」处理——池不能因为查不到成交就拒绝开工。
    ranks: dict[str, int] = field(default_factory=dict)    # {代码: 批次名次（0 起）}
    bought_today: frozenset = field(default_factory=frozenset)   # 今日有买入成交的代码

    def cash_left(self, capital: float) -> float | None:
        """账户当日还能动多少钱（元）：总资金 − 现有持仓成本。

        用成本口径而不是市值口径，和 `positions()` 同一个理由：额度不该随行情自己涨跌。
        当天卖出的钱理论上当日就能用来买（T+1 只限股票不限资金），但持仓是否已经反映这笔
        卖出取决于交割单导没导——没导就按「钱还压在那只票上」估，宁可少推买入，也不给出
        一个账户里根本拿不出来的下单量。`capital` 没配（0）时返回 None = 没有资金口径。
        """
        if not capital:
            return None
        return max(0.0, float(capital) - sum(self.held.values()))

    @property
    def age_hours(self) -> float | None:
        if self.selected_at is None:
            return None
        # generated_at 可能是注入的带时区时钟，selected_at 一律是本地 naive，先对齐再减
        return round((_naive(self.generated_at) - self.selected_at).total_seconds() / 3600, 1)

    @property
    def stale(self) -> bool:
        age = self.age_hours
        return age is None or age > STALE_HOURS

    def describe(self) -> str:
        stamp = self.selected_at.strftime("%Y-%m-%d %H:%M") if self.selected_at else "没有成功批次"
        note = f"{self.age_hours} 小时前" if self.age_hours is not None else "—"
        return f"盘中池 {len(self.codes)} 只：批次 {stamp}（{note}），持仓 {len(self.held)} 只"


def _connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise PoolError(f"找不到 app.db：{path}（用 --app-db 指定路径，或改用 --code/--all 手工给池）")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise PoolError(f"只读打开 app.db 失败：{path}（{exc}）") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error as exc:              # 「no such table」多半是库版本对不上，说清是哪张表
        raise PoolError(f"读 app.db 失败：{sql.strip().splitlines()[0]}（{exc}）") from exc


def latest_batch(conn: sqlite3.Connection) -> tuple[str | None, dt.datetime | None, list[str]]:
    """最近一次成功选股的代码，按 App 里的展示顺序（rank_no）排。

    只认 `status='done'`：跑失败的批次留下的 select_results 是上一次的残留，按 job_id 取
    才不会把「失败任务」当成「最新命中」。
    """
    jobs = _query(conn, "SELECT job_id, created_at FROM select_jobs WHERE status='done' "
                        "ORDER BY created_at DESC LIMIT 1")
    if not jobs:
        return None, None, []
    job = jobs[0]
    rows = _query(conn, "SELECT code FROM select_results WHERE job_id=? "
                        "AND COALESCE(strategy, '') <> 'hot_pref' "
                        "ORDER BY rank_no IS NULL, rank_no", (job["job_id"],))
    created = _to_local(job["created_at"])
    # hot_pref 已下线；旧批次里残留的偏好榜行不进盘中池，否则会和模型榜混排名次、占买入名额。
    # 保序去重：旧批次可能同时有两条榜，同一只票会有两行；留第一次出现=它最好的那个名次。
    codes = list(dict.fromkeys(str(r["code"]) for r in rows if str(r["code"]).strip()))
    return job["job_id"], created, codes


def _to_local(raw) -> dt.datetime | None:
    """库里存的是 UTC naive，盘中口径要的是本地时间（批次新旧按人看到的钟点算）。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            moment = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        moment = raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone().replace(tzinfo=None)


def positions(conn: sqlite3.Connection) -> dict[str, float]:
    """启用中的持仓 → {代码: 成本市值}。多个用户持有同一只就累加。

    市值用 `quantity × cost_price`（成本口径），和风控的单票已用额度同量纲；风控要的是
    「这只票还买得下多少」，用成本而不是现价估，误差只有一个涨跌停的量级，且不会随着
    行情波动把额度算成会自己涨跌的东西。
    """
    rows = _query(conn, "SELECT code, SUM(quantity * cost_price) AS value FROM holdings "
                        "WHERE enabled=1 GROUP BY code")
    out: dict[str, float] = {}
    for r in rows:
        code = str(r["code"]).zfill(6)
        value = float(r["value"] or 0.0)
        if value > 0:
            out[code] = out.get(code, 0.0) + value
    return out


def _bought_today(conn: sqlite3.Connection, day: str) -> frozenset:
    """今日有买入成交的代码（T+1 用）。查不到就当没有——判不了由风控放行，不在这儿炸。

    真源只有交割单：`holdings` 里同一只票可能拿的是上个月的成本，`created_at` 是建行时间
    而不是成交日，都不够回答「今天买过没买过」。所以当天没导对账单时这里必然是空的，
    风控会照给卖出建议并留一条「no_trade_date」指标。
    """
    try:
        rows = _query(conn, "SELECT DISTINCT stock_code FROM trade_records "
                            "WHERE trade_date=? AND direction IN ('buy', '买入')", (day,))
    except PoolError as exc:
        logger.warning("读不到今日买入成交（%s）：T+1 判定跳过", exc)
        return frozenset()
    return frozenset(str(r["stock_code"]).zfill(6) for r in rows if str(r["stock_code"]).strip())


def load(db_path: Path | str = DEFAULT_DB, *, limit: int = 0, extra: list[str] = (),
         now: dt.datetime | None = None) -> MonitorPool:
    """取池：手工代码在前，然后选股命中（按 rank），持仓补齐。

    `limit` 只截命中，**永远不会截掉持仓与手工代码**——盯不过来的是几百只候选，不是那几只
    要卖的票。
    """
    conn = _connect(db_path)
    try:
        job_id, selected_at, hits = latest_batch(conn)
        held = positions(conn)
        moment = now or dt.datetime.now()
        bought = _bought_today(conn, moment.strftime("%Y-%m-%d"))
    finally:
        conn.close()

    hits = [c.zfill(6) for c in hits]
    if limit:
        hits = hits[:limit]
    # 名次按批次原序（App 展示序=预期超额降序）记，手工代码与纯持仓票没有名次 → 不占买入名额
    ranks = {code: i for i, code in enumerate(hits)}
    codes: list[str] = []
    seen: set[str] = set()
    for code in [str(c).zfill(6) for c in extra if str(c).strip()] + hits + sorted(held):
        if code not in seen:                  # 保序去重：持仓里已命中的票不该出现两次
            seen.add(code)
            codes.append(code)

    if not codes:
        raise PoolError(f"app.db 里既没有成功的选股批次也没有启用中的持仓：{db_path}")
    plan = MonitorPool(codes=codes, held=held, selected_at=selected_at, job_id=job_id,
                       generated_at=moment, ranks=ranks, bought_today=bought)
    if plan.stale:
        logger.warning("选股批次已超过 %.0f 小时（%s），盘中盯的可能是过期名单——检查调度器",
                       STALE_HOURS, plan.selected_at)
    logger.info("%s", plan.describe())
    return plan
