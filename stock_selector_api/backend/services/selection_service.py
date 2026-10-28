"""选股服务：薄代理选股插件(8002) + job 生命周期 + 完成/失败通知。
红线：不在后端做任何选股逻辑；只有代理与落库。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from config import settings
# band 纪律的卖出线（exit_rank）只有一个出处：回测参数缺省表。持仓页要的「还在榜内吗」
# 必须和回测用的是同一条线，否则页面上的判断和回测结论对不上。
from core.backtest_params import DEFAULTS as BACKTEST_DEFAULTS
from core.exceptions import BizError, NotFoundError
from repositories import select_jobs_repo, select_results_repo
from services import inbox_notify

logger = logging.getLogger("selection_service")

# 进程内单飞表：DB 状态查询与后台任务间存在调度间隙，内存判定保证确定性
_inflight: dict[str, str] = {}
# 两个请求同时进来时，「查 DB → 建 job → 写 _inflight」之间有 await，必须串行化，
# 否则两边都查到没有运行中，各建一条任务。API 是单进程（deploy 注释），这把锁够用。
_start_lock = asyncio.Lock()

_RANK_MAX_CODES = 60
# 名次查询现在是**毫秒级**的：引擎侧那条路由不等打分——当日榜单没算好时它立刻回
# ready=false 并自己起后台预热（放开全市场后冷打分要几百秒，放进同步请求里只会被超时打断，
# 而超时给页面的是「查询失败」，真实原因却是「还在算」）。这里留 20 秒是网络余量，不是打分余量。
_RANK_TIMEOUT = 20
# 后台作业等 8003 出名单的上限。**这不是「用户在页面上等 900 秒」**：start_run 建作业后立即
# 返回 job_id，页面轮询状态，这个数只决定作业多久被判 failed。
# 口径：全市场名单 + 1 亿流动性下限 ≈2,900 只，冷打分按只数从 550 秒（4,976 只实测，docs §17.4）
# 推约 310 秒；900 秒是给「16:00 面板刚增量完、qlib 自己也在冷加载」的日子留余量。
_SELECTOR_RUN_TIMEOUT = 900


async def list_strategies() -> list[str]:
    if settings.selection_plugin_mode == "http":
        url = settings.selector_url + "/api/strategy/list"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        return list(body.get("data") or [])
    return ["trend_rebound", "box_oscillate"]


# 插件未启时的兜底元信息，字段形状与 GET /api/strategy/detail 保持一致
_MOCK_STRATEGY_DETAILS = [
    {"name": "trend_rebound", "description": "趋势回踩：均线多头排列，股价回踩均线企稳", "as_of": True,
     "required_columns": ["date", "open", "high", "low", "close", "volume"], "required_rows": 30,
     "params": {"period": 20, "pullback_pct": 0.05}},
    {"name": "box_oscillate", "description": "箱体震荡：区间突破上沿买入", "as_of": True,
     "required_columns": ["date", "open", "high", "low", "close", "volume"], "required_rows": 40,
     "params": {"period": 30, "breakout_pct": 0.02}},
]


async def list_strategy_details() -> list[dict]:
    """策略元信息（中文说明/取数需求），前端拿它做中文标签，避免在客户端硬编码策略名。"""
    if settings.selection_plugin_mode == "http":
        url = settings.selector_url + "/api/strategy/detail"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        return list(body.get("data") or [])
    return _MOCK_STRATEGY_DETAILS


async def day_ranks(codes: list[str]) -> dict:
    """持仓页要的「当日榜内名次」：薄代理选股引擎的 /api/selector/rank，后端不做任何排名。

    红线：名次只能来自引擎当日对**整个票池**的打分。引擎没有这条路由时（自研内核 8002 会 404，
    它的强度分只在那一批名单里唯一，不是全票池名次）返回 supported=false + 一句原因，
    前端显示「该引擎不提供榜内名次」。这里绝不退化成拿 select_results.rank_no 凑一个数：
    那个 rank_no 只在本次 topk（默认 30）行内唯一，和 qlib 的 300 只全池名次不是一个口径，
    把 7 放进两个口径里读出来的是两件不同的事。
    """
    if settings.selection_plugin_mode != "http":
        return _no_rank("选股插件未启用（mock 模式），没有当日榜可查")
    url = settings.selector_url + "/api/selector/rank"
    async with httpx.AsyncClient(timeout=_RANK_TIMEOUT) as client:
        resp = await client.get(url, params={"codes": ",".join(codes)})
    if resp.status_code == 404:
        return _no_rank(f"当前选股引擎（{settings.selector_url}）不提供榜内名次")
    if resp.status_code != 200:
        # 引擎报错时不能说「都没上榜」：那会让持仓票看起来像集体跌出榜单，
        # 而真实原因是查询失败。抛业务错，页面显示失败原因。
        raise BizError(f"榜内名次查询失败：HTTP {resp.status_code}")
    data = resp.json().get("data") or {}
    ready = bool(data.get("ready", True))
    return {
        "supported": True,
        # ready=false 是「当日榜单还在算」，不是「这些票都没上榜」，也不是查询失败。
        # 放开全市场之后第一次冷打分要几百秒，这条同步链路只能把这个状态原样带给页面。
        "ready": ready,
        "date": str(data.get("date") or ""),
        "pool_size": int(data.get("pool_size") or 0),
        "universe": int(data.get("universe") or 0),
        # 卖出线：回测 band 纪律用的同一个缺省值，前端拿它判「还在榜内 / 已跌出榜外」，
        # 不在前端写死 100
        "exit_rank": int(BACKTEST_DEFAULTS["exit_rank"]),
        "message": "" if ready else str(data.get("message") or "当日榜单还在计算中，稍后再查"),
        "items": [{
            "code": str(it.get("code") or ""),
            "in_pool": bool(it.get("in_pool")),
            # 给不出名次就是 None，不能填 0/999 之类的哨兵值：那是个假名次
            "rank": _num(it.get("rank"), integer=True),
            "universe": int(it.get("universe") or 0),
            "score_raw": _num(it.get("score_raw")),
            "note": str(it.get("note") or ""),
        } for it in (data.get("items") or [])],
    }


def _no_rank(message: str) -> dict:
    """引擎不提供名次时的统一形状：字段齐全、items 空、带一句原因。

    形状保持一致是为了前端不用分支就能读到 message；supported 显式给 False 而不是
    靠「items 为空」推断——空名单在 qlib 引擎下另有含义（持仓全不在池里）。
    """
    return {"supported": False, "date": "", "pool_size": 0, "universe": 0,
            "exit_rank": int(BACKTEST_DEFAULTS["exit_rank"]), "message": message, "items": []}


def split_codes(raw: str) -> list[str]:
    """查询串 → 去重的 6 位代码表，保持给出顺序。

    两处硬失败而不是宽容跳过：格式认不出来的代码（说明前端或数据脏了）和超过 60 只。
    超限必须报错——插件那边是 `[:60]` 静默截断，截断后的持仓页少显示几只票的名次，
    看起来像「那几只没上榜」，而那其实是根本没去查。
    """
    out: list[str] = []
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        # 只认「6 位代码 + 可选市场前缀/后缀」；原来的 search 会把 1234567 截成 123456，
        # 把脏串当成真代码去查，页面就多一个查不到的名次。
        match = re.fullmatch(r"(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ))?", token, re.IGNORECASE)
        if not match:
            raise BizError(f"代码格式不对：{token}（要 6 位数字代码）")
        code = match.group(1)
        if code not in out:
            out.append(code)
    if len(out) > _RANK_MAX_CODES:
        raise BizError(f"榜内名次一次最多查 {_RANK_MAX_CODES} 只，当前 {len(out)} 只")
    return out





async def _mock_run(strategies: list[str]) -> list[dict]:
    """确定性模拟结果（测试/插件未启时）。"""
    return [
        {"code": "600519", "name": "贵州茅台", "signal": "趋势回踩", "reason": "mock: MA20 上穿",
         "strategy": "trend_rebound", "asof": "2026-09-24", "price": 1480.0, "score": 0.82,
         "rank_no": 1},
        {"code": "000858", "name": "五粮液", "signal": "箱体突破", "reason": "mock: 突破箱顶",
         "strategy": "box_oscillate", "asof": "2026-09-24", "price": 128.5, "score": 1.0,
         "rank_no": 2},
    ]


def _num(value, integer: bool = False):
    """插件给的可能是 str/None，落库前收敛成数值或 None（rank 走整数口径）。"""
    try:
        return None if value in (None, "") else (int(float(value)) if integer else float(value))
    except (TypeError, ValueError):
        return None


async def _http_run(strategies: list[str]) -> list[dict]:
    url = settings.selector_url + "/api/selector/run"
    async with httpx.AsyncClient(timeout=_SELECTOR_RUN_TIMEOUT) as client:
        resp = await client.post(url, json={"strategies": strategies or None})
        resp.raise_for_status()
        body = resp.json()
    items = body.get("data") or []
    return [{
        "code": str(it.get("code", "")),
        "name": str(it.get("name", "")),
        "signal": str(it.get("signal", "") or it.get("action", "")),
        "reason": str(it.get("reason", "")),
        "strategy": str(it.get("strategy", "")),
        "asof": str(it.get("asof", "")),
        "price": _num(it.get("price")),
        "score": _num(it.get("score")),
        "rank_no": _num(it.get("rank"), integer=True),
        # 买入风险分在插件的扩展字段里；闸门关闭或历史太短判分时没有这个 key → None，
        # 不能落成 0（0 是"预测超额为零"，是有含义的评价）
        "risk_ex10_pp": _num((it.get("extra") or {}).get("risk_ex10_pp")),
        # 买点参考位/失效位：策略判据里本来就算出来的均线、箱沿、6 日低点，
        # 没有对应判据的策略给 None（不能落成 0，也不能从 reason 文本里抠）
        "buy_ref_price": _num((it.get("extra") or {}).get("buy_ref_price")),
        "buy_invalid_price": _num((it.get("extra") or {}).get("buy_invalid_price")),
        # 第一止盈价 + 口径句子（qlib 榜按近 60 日筹码主峰给；自研内核没这条判据时是 None/空串，
        # 不能落成 0——0 元是一个真实价位）
        "sell_target_price": _num((it.get("extra") or {}).get("sell_target_price")),
        "level_note": str((it.get("extra") or {}).get("level_note") or "")[:160],
        # 风控决策：信号已过风控规则才到这里（被拦的票内核就不返回了）。
        # allow=原样放行、adjust=放行但下单量被单票上限压过；空值=这批结果跑在风控关闭时。
        "risk_verdict": str((it.get("extra") or {}).get("risk_verdict") or "")[:8],
        "risk_reason": str((it.get("extra") or {}).get("risk_reason") or "")[:512],
        "risk_shares": _num((it.get("extra") or {}).get("risk_shares"), integer=True),
        "risk_amount": _num((it.get("extra") or {}).get("risk_amount")),
        "risk_fill_price": _num((it.get("extra") or {}).get("risk_fill_price")),
        "risk_slippage_bp": _num((it.get("extra") or {}).get("risk_slippage_bp")),
        # 判据级历史后验：verdict 已经把「样本够不够」和「换时代/换抽样同不同号」合成一句话，
        # 页面只显示它，单独显示 excess_pp 会把翻号的抽样噪声说成结论。
        # 后验表还没建时三个字段留空/NULL——不能填 0，0 是「超额为零」，那是一项判定。
        "posterior_verdict": str(_sp(it).get("verdict") or "")[:64],
        "posterior_excess_pp": _num(_sp(it).get("excess_pp")),
        "posterior_n": _num(_sp(it).get("n"), integer=True),
        # adx14 闸门留痕：shadow 期要看到「这只当天排第几成、闸门会不会剔它」。
        # 闸门没开或分位取不到 → 两个字段都是空。**不能把 reject 落成 0 分位**：
        # 0 分位是「当日全市场最弱」这个事实，而 reject 可能是分位 83.4 被剔的。
        "adx14_pct": _num((it.get("extra") or {}).get("adx14_pct")),
        "adx14_gate": str((it.get("extra") or {}).get("adx14_gate") or "")[:8],
        # 引擎自己声明「这一项我不算」的字段清单。不能由「那列是空」反推：空值有两种成因
        # （引擎压根不算 / 引擎算了但这只判不出），前端要区分的就是前者。
        "na_fields": _na_fields((it.get("extra") or {}).get("na_fields")),
    } for it in items if it.get("code")]


def _na_fields(value) -> str:
    """插件给的名单收敛成逗号串；给不出清单就返回空串（= 没声明，不等于「声明了零项」）。"""
    if not isinstance(value, (list, tuple)):
        return ""
    return ",".join(str(v).strip() for v in value if str(v).strip())[:128]


def _sp(it: dict) -> dict:
    """内核挂在 extra.signal_posterior 上的策略级后验；缺失时返回空 dict（按「未标定」处理）。"""
    value = (it.get("extra") or {}).get("signal_posterior")
    return value if isinstance(value, dict) else {}


# 单飞位的僵尸任务清理阈值（分钟）。选股实测 1~3 分钟（17:30 那次 356 条），
# 30 分钟还没写回结果就说明跑它的协程已经不在了（进程重启/被杀/异常没兜住）。
STALE_MINUTES = 30
_STALE_MSG = "任务已中断：超过 {min} 分钟没有任何进展（跑它的协程已不在），已自动释放"


def _age_minutes(job: dict) -> float:
    """作业建于多久（分钟）。时间戳缺失按"很旧"处理，宁可释放也不能永久挡路。"""
    raw = job.get("updated_at") or job.get("created_at")
    if not raw:
        return float("inf")
    try:
        when = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 60


async def _release_stale(user_id: str) -> dict | None:
    """把僵尸的 queued/running 作业判失败并返回它；没有僵尸返回 None。

    为什么必须有这一步：单飞位是靠 DB 里的 in-flight 状态挡的，而那个状态是**进程写的**。
    进程在写完结果之前退出（重启、被 OOM 杀、脚本里手动触发）时，状态永远停在 queued，
    用户从此每点一次都是「已有选股任务运行中」，而那条任务其实早就不存在了。
    宁可放行一次重复扫描，也不要把功能永久锁死——扫描是只读的，重跑没有副作用。
    """
    job = await select_jobs_repo.find_running(user_id)
    if not job:
        return None
    if _age_minutes(job) < STALE_MINUTES:
        return job
    await select_jobs_repo.update(job["job_id"],
                                 {"status": "failed", "error": _STALE_MSG.format(min=STALE_MINUTES)})
    # 进程被杀时可能已经写了几行；僵尸任务既然判失败，半截名单也要一起清掉。
    try:
        await select_results_repo.delete_by_job(job["job_id"])
    except Exception:           # noqa: BLE001 - 清理失败不能挡住新任务
        logger.exception("清理僵尸选股结果失败: %s", job["job_id"])
    logger.warning("清理僵尸选股任务 %s（建于 %.0f 分钟前，状态 %s）", job["job_id"],
                   _age_minutes(job), job.get("status"))
    return None


async def start_run(user_id: str, strategies: list[str] | None,
                    source: str = "manual") -> dict:
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    async with _start_lock:
        if user_id in _inflight:
            raise BizError(f"已有选股任务运行中: {_inflight[user_id]}", code=409, status_code=409)
        running = await _release_stale(user_id)
        if running:
            raise BizError(f"已有选股任务运行中: {running['job_id']}", code=409, status_code=409)

        # 策略名单只从插件拿，失败/为空就报错，绝不回退旧名字。回退的后果是插件按契约
        # 返回空名单 → 落库 0 条 → 页面显示「今天没有票」，真实原因却是「名字对不上」。
        available: list[str] | None = None
        if not strategies or settings.selection_plugin_mode == "http":
            try:
                available = await list_strategies()
            except Exception as exc:  # noqa: BLE001
                raise BizError(f"策略列表取不到：{exc}", code=503, status_code=503) from exc
        if strategies:
            if available is not None:
                unknown = [s for s in strategies if s not in available]
                if unknown:
                    raise BizError(f"策略已下线或不可用：{', '.join(unknown)}",
                                   code=400, status_code=400)
            chosen = list(strategies)
        else:
            chosen = list(available or [])
        if not chosen:
            raise BizError("选股插件没有可用策略", code=503, status_code=503)

        job = await select_jobs_repo.create({
            "user_id": user_id,
            "strategies": json.dumps(chosen, ensure_ascii=False),
            "status": "queued",
            "source": source,
        })
        _inflight[user_id] = job["job_id"]
    asyncio.create_task(_run_job(job["job_id"], user_id, chosen))
    return {"job_id": job["job_id"], "status": "queued"}


async def _run_job(job_id: str, user_id: str, strategies: list[str]) -> None:
    try:
        await select_jobs_repo.update(job_id, {"status": "running"})
        if settings.selection_plugin_mode == "http":
            results = await _http_run(strategies)
        else:
            results = await _mock_run(strategies)
        for r in results:
            await select_results_repo.create({"job_id": job_id, **r})
        await select_jobs_repo.update(job_id, {"status": "done", "result_count": len(results)})
    except Exception as exc:    # noqa: BLE001
        logger.exception("选股任务失败: %s", job_id)
        # 失败时清掉已经写进去的半截名单：任务状态是 failed，结果区却躺着 3 行，
        # 页面很容易把「写了一半」当成「今天只有这 3 只」。
        try:
            await select_results_repo.delete_by_job(job_id)
        except Exception:       # noqa: BLE001 - 清理失败不能掩盖原始错误
            logger.exception("清理选股半截结果失败: %s", job_id)
        await select_jobs_repo.update(job_id, {"status": "failed", "error": str(exc)[:512]})
        try:
            await inbox_notify.notify_user(
                user_id, "selection_failed", "选股失败", str(exc)[:512],
                related_type="select_jobs", related_id=job_id)
        except Exception:       # noqa: BLE001
            logger.exception("选股失败通知发送失败: %s", job_id)
        return
    finally:
        if _inflight.get(user_id) == job_id:
            _inflight.pop(user_id, None)

    # 完成通知是旁路：发不出去只记日志，不能把已经 done 的任务翻成 failed。
    try:
        await inbox_notify.notify_user(
            user_id, "selection_completed", "选股完成",
            f"本次选股命中 {len(results)} 只（策略: {'/'.join(strategies)}）",
            related_type="select_jobs", related_id=job_id)
    except Exception:           # noqa: BLE001
        logger.exception("选股完成通知发送失败: %s", job_id)


async def run_status(user_id: str, job_id: str) -> dict:
    job = await select_jobs_repo.get(job_id)
    if not job or job["user_id"] != user_id:
        raise NotFoundError("选股任务不存在")
    # 只有 done 才给结果。running 时写了一半、failed 时残留的旧行都不能当完整名单展示；
    # 失败原因走 job.error，页面另起一行显示。
    job["results"] = (await select_results_repo.list_by_job(job_id)
                      if job.get("status") == "done" else [])
    return job


async def list_runs(user_id: str, page: int = 1, size: int = 20) -> dict:
    items, total = await select_jobs_repo.list_by_user(user_id, page, size)
    for job in items:
        # 未完成/失败的任务不给「现在有几行」：那是写了一半的中间态，不是可用的名单数量。
        rows = (await select_results_repo.list_by_job(job["job_id"])
                if job.get("status") == "done" else [])
        job["result_count_now"] = len(rows)
    return {"items": items, "total": total}
