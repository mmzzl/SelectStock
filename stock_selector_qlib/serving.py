"""qlib 打分服务：短窗 Alpha158 + 落盘模型 → 当日分数。不训练、不读全历史。

口径必须与 scripts/qlib_run.py 逐字对齐，所以这里不重新定义任何常量：
LABEL / 面板目录 / Alpha158 配置都从那个脚本 import（Celery 任务先用过同一套
sys.path 手法引 scripts/backtest_main.py）。

内存：Alpha158 最长滚动窗口是 60 日，取 WARMUP_DAYS 根 K 线足够让最后一个交易日的
158 个因子和全历史算法一致；300 只 × 130 天 ≈ 3.9 万行，比训练段的千万行小两个数量级。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from stock_selector_qlib import artifacts

logger = logging.getLogger("qlib_serving")
# 每天靠这一条 INFO 确认「真的给整个名单打了分、几只进榜、耗时多少」，而模块自建的 logger
# 没有 handler 时，logging 的 lastResort 只放 WARNING 以上 ⇒ 这条会被静默丢掉（2026-10-19 04:2x
# 上线复验时就是这样：预热跑完了，日志里只有 qlib 自己的计时行）。补一个 stderr handler，
# 并关掉 propagate，避免 uvicorn 之后接管根 logger 时同一行打两遍。
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(levelname)s %(asctime)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Alpha158 的滚动窗口最长 60 日，留一倍余量让首行也是「看过足够多历史」的状态
WARMUP_DAYS = 130
DEFAULT_TOPK = int(os.environ.get("QLIB_TOPK", "30"))
# 打分名单的来源：model=模型产物里冻结的 pool.txt（训练段流动性前 300，历史口径）；
# universe=data/qlib/universe.txt（scripts/qlib_daily_universe.py 每天按规则重算的全市场名单）
POOL_SOURCE = os.environ.get("QLIB_POOL_SOURCE", "model")
UNIVERSE_FILE = REPO / "data" / "qlib" / "universe.txt"
# 全市场约 5,000 只一次算会吃 2.5 GB 以上，切批送因子引擎；打分/归一化都是逐行运算，
# 切批与整批逐位等价（rank 在拼回之后才算），已经用 300 只的池验过
SCORE_CHUNK = int(os.environ.get("QLIB_SCORE_CHUNK", "1000"))
# 一行因子里 NaN 超过这个数，说明这只票的窗口根本没被行情填满（长停牌/刚上市/日线断档），
# 归一化会把这些格子填成 0，模型就得到一个「看起来中性」的假分数——这类票不进榜，只计数
MAX_NAN_FACTORS = int(os.environ.get("QLIB_MAX_NAN", "20"))

_cache: dict = {}
_cache_lock = threading.Lock()
# 单飞锁：全市场冷打分要几百秒、约 1 GB，两个请求各算一遍会把内存翻倍、把 earlyoom 叫醒。
# 拿锁后再看一次缓存，排队的人直接命中前一个人的结果。
_score_lock = threading.Lock()
_warming = False
_qlib_ready = False
# (日历文件的 mtime_ns, 大小, 解析出来的日期表)：面板每天增量更新后文件必变，靠这个键决定重读
_calendar_cache: tuple[int, int, list[str]] | None = None


def init_qlib(provider_uri: str | None = None) -> None:
    """幂等初始化 qlib 数据层（服务进程只读 bin 面板，不碰 mlflow）。"""
    global _qlib_ready
    with _cache_lock:
        if _qlib_ready:
            return
        import qlib
        from qlib.constant import REG_CN

        qlib.init(provider_uri=str(provider_uri or _qlib_dir()), region=REG_CN, kernels=1,
                  expression_cache=None, dataset_cache=None)
        _qlib_ready = True


def _qlib_dir() -> Path:
    from qlib_run import QLIB_DIR  # 面板目录的唯一出处

    return Path(os.environ.get("QLIB_PROVIDER_URI", str(QLIB_DIR)))


def calendar_dir() -> Path:
    return _qlib_dir() / "calendars"


def calendar() -> list[str]:
    """交易日历直接读 calendars/day.txt，按 (mtime, 大小) 决定要不要重读。

    为什么不走 qlib.data.D.calendar()：那条结果在进程内缓存一小时（qlib 的
    mem_cache_expire=3600），而收盘后的增量更新恰恰就是往这个文件里追加一天。
    常驻进程在这一小时里会坚持认为「今天还不是交易日」，选股名单停在昨天。
    """
    global _calendar_cache
    path = calendar_dir() / "day.txt"
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _calendar_cache is not None and _calendar_cache[:2] == key:
            return _calendar_cache[2]
    if not path.exists():
        raise RuntimeError(f"qlib 面板没有交易日历文件：{path}（先跑 dump_all）")
    with path.open() as fp:
        days = [line.strip()[:10] for line in fp if line.strip()]
    if not days:
        raise RuntimeError(f"qlib 面板的交易日历是空的：{path}")
    with _cache_lock:
        _calendar_cache = (key[0], key[1], days)
    return days


def refresh() -> dict:
    """面板刚写完时清掉进程内缓存，让常驻服务认账新的交易日。

    为什么必须显式清：除了上面那个自己管自己的日历文件缓存，qlib 内部把
    D.calendar()/D.instruments()/表达式 的结果都存在 MemCache（H）里，过期前要一小时。
    根调度器是普通用户跑的，重启不了本进程（supervisorctl 要 root），所以只能自己清。
    """
    global _qlib_ready, _calendar_cache
    with _cache_lock:
        _cache.clear()
    _calendar_cache = None
    from qlib.data.cache import H

    H.clear()
    _qlib_ready = False
    init_qlib()
    days = calendar()
    logger.info("qlib 缓存已刷新：日历 %d 天，最后一天 %s", len(days), days[-1])
    return {"calendar_days": len(days), "calendar_end": days[-1], "model": artifacts.load().describe()}


def raw_features(pool: list[str], beg: str, end: str) -> pd.DataFrame:
    """未归一化的 Alpha158 因子表：index=(datetime, instrument)，columns=158 个因子名。"""
    from qlib_run import LABEL
    from qlib.contrib.data.handler import Alpha158

    handler = Alpha158(
        instruments=list(pool),
        start_time=beg,
        end_time=end,
        # 服务段不做 fit（归一化用的是训练段存下来的 mean/std），这两个参数只是 handler 必填项
        fit_start_time=beg,
        fit_end_time=end,
        label=[LABEL],
        infer_processors=[],
        learn_processors=[],
    )
    return handler.fetch(col_set="feature")


def normalize(frame: pd.DataFrame, art: artifacts.Artifacts) -> np.ndarray:
    """复刻训练期的 RobustZScoreNorm(clip_outlier=True) + Fillna(0)：
    (X-mean)/std → clip(−3,3) → NaN 填 0。顺序不能换，clip 之前/之后的 NaN 行为不同。"""
    missing = [c for c in art.columns if c not in frame.columns]
    if missing:
        raise RuntimeError(f"因子缺列 {missing[:5]}（共 {len(missing)} 个），与训练产物不一致")
    X = frame.reindex(columns=art.columns).to_numpy(dtype="float64", copy=True)
    X = (X - art.mean) / art.std
    X = np.clip(X, -3.0, 3.0)
    X[np.isnan(X)] = 0.0
    return X


def score_pool(art: artifacts.Artifacts) -> list[str]:
    """今天给哪些票打分。universe 模式读每天重算的全市场名单，model 模式沿用训练池。"""
    if POOL_SOURCE == "universe":
        if not UNIVERSE_FILE.exists():
            raise RuntimeError(f"QLIB_POOL_SOURCE=universe，但没有 {UNIVERSE_FILE}"
                               "（先跑 scripts/qlib_daily_universe.py）")
        syms = [line.strip() for line in UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        if not syms:
            raise RuntimeError(f"{UNIVERSE_FILE} 是空名单")
        return syms
    return list(art.pool)


def score_frame(art: artifacts.Artifacts, beg: str | None = None,
                end: str | None = None, pool: list[str] | None = None,
                chunk: int | None = None) -> tuple[pd.Series, dict]:
    """给 [beg, end] 区间内票池每只股票打分，返回 (datetime, instrument) 两层索引的 Series 和计数。

    切批只为了压内存：normalize 是逐行、predict 是逐行，批次之间没有横截面运算，
    所以切批结果与整批一次算逐位一致（名次在拼回之后才排）。
    """
    days = calendar()
    end = end or days[-1]
    if end not in days:
        raise RuntimeError(f"结束日 {end} 不在 qlib 交易日历里")
    idx = days.index(end)
    beg = beg or days[max(0, idx - WARMUP_DAYS + 1)]
    pool = list(pool or score_pool(art))
    chunk = chunk or SCORE_CHUNK
    parts: list[pd.Series] = []
    thin = missing = 0
    for start in range(0, len(pool), chunk):
        batch = pool[start:start + chunk]
        frame = raw_features(batch, beg, end)
        keep = frame.isna().sum(axis=1) <= MAX_NAN_FACTORS
        thin += int((~keep).sum())
        if not keep.any():
            missing += len(batch)
            continue
        kept = frame[keep]
        parts.append(pd.Series(art.booster.predict(normalize(kept, art)), index=kept.index))
    pred = pd.concat(parts).reorder_levels(["datetime", "instrument"]).sort_index()
    stats = {"pool": len(pool), "chunk": chunk, "thin_rows": thin, "no_row": missing,
             "scored": int(len(pred)), "max_nan": MAX_NAN_FACTORS}
    return pred, stats


class RankingNotReady(RuntimeError):
    """当日全票池的分数还没算好，而这条请求明确说了「不等」。

    为什么要专门有这么个异常而不是让它慢慢算：`/api/selector/rank` 是持仓页**同步在等**的接口
    （App 侧 60 秒、后端 120 秒），而放开全市场之后第一次冷打分要几百秒。把它打死的方式是
    让 App 显示「都没上榜」——那是假结论。未就绪必须是看得见的一个状态。
    """


def _cached_rows(key: tuple) -> list[dict] | None:
    with _cache_lock:
        return list(_cache["rows"]) if _cache.get("key") == key else None


def day_ranking(refresh: bool = False, wait: bool = True) -> list[dict]:
    """最近一个交易日**整个票池**的分数与名次（不含价格）。同一天内命中缓存。

    名次是分数降序的位次，所以「今日名单」= 前 topk 条、「某只票排第几」= 在这张表里查一行，
    两者共用同一次打分。以前缓存键里带着 topk，名单和查名次会各算一遍全池因子。

    `wait=False`：缓存没命中就抛 `RankingNotReady`，不在这条调用里算几百秒（同步接口用）；
    `wait=True`（默认，给 17:30 那条后台作业用）：在 `_score_lock` 上排队，让同一时刻只有
    一个人在算全池。
    """
    art = artifacts.load()
    init_qlib()
    date = calendar()[-1]
    key = (art.run_id, date)
    if not refresh:
        rows = _cached_rows(key)
        if rows is not None:
            return rows
        if not wait:
            raise RankingNotReady(f"{date} 的全票池打分还在计算中，稍后再查名次")
    with _score_lock:
        # 拿到锁之后再看一次：排队这几分钟里，前一个人多半已经把结果放好了
        if not refresh:
            rows = _cached_rows(key)
            if rows is not None:
                return rows
        started = time.time()
        pred, score_stats = score_frame(art, end=date)
        day = pd.Timestamp(date)
        if not (pred.index.get_level_values("datetime") == day).any():
            raise RuntimeError(f"{date} 票池里没有可打分的股票（日线断档或票池过期）")
        tail = pred.xs(day, level="datetime").sort_values(ascending=False)
        total = len(tail)
        rows = [{
            "code": str(inst)[2:],
            "qlib_symbol": str(inst),
            "date": date,
            "score_raw": float(raw),
            # score 是给页面排序用的 0~1 分位，用「整只票池里的位置」而不是「名单内的名次」，
            # 否则 topk 一改分位数就变，前后两批结果没法比
            "score": float((total - position) / total),
            "rank": position + 1,
            "universe": total,
        } for position, (inst, raw) in enumerate(tail.items())]
        with _cache_lock:
            _cache["key"] = key
            _cache["rows"] = rows
            _cache["stats"] = {**score_stats, "universe": total,
                               "seconds": round(time.time() - started, 1)}
        logger.info("qlib 打分 %s：%d/%d 只进榜（因子不足 %d 行、整批无数据 %d 只），耗时 %.1f 秒",
                    date, total, score_stats["pool"], score_stats["thin_rows"],
                    score_stats["no_row"], time.time() - started)
        return rows


def warm_ranking() -> bool:
    """后台把当日榜单算出来填进缓存。返回 False = 已经有一个预热线程在跑，不重复起。

    起后台线程是为了让同步接口立刻回「未就绪」：冷打分要几百秒，把它放在 HTTP 请求里
    一定被超时打断，而打断后 App 拿到的是失败，不是「还没算好」。
    """
    global _warming
    with _cache_lock:
        if _warming:
            return False
        _warming = True
    threading.Thread(target=_warm_job, daemon=True, name="qlib-warm").start()
    return True


def _warm_job() -> None:
    global _warming
    try:
        day_ranking()
    except Exception:  # noqa: BLE001 - 预热失败不能带崩线程；下一次请求会再触发，且日志里有
        logger.exception("qlib 后台预热失败：榜单仍未就绪")
    finally:
        with _cache_lock:
            _warming = False


def last_score_stats() -> dict:
    """最近一次打分的计数，给 /health 用：放开全市场之后，「进了多少只、为什么少」必须看得见。"""
    with _cache_lock:
        return dict(_cache.get("stats") or {})


def latest_scores(topk: int = DEFAULT_TOPK, refresh: bool = False) -> list[dict]:
    """最近一个交易日的选股名单（前 topk 只，补上基准日收盘价）。"""
    rows = [dict(row) for row in day_ranking(refresh)[:topk]]
    if rows:
        closes = _closes([row["qlib_symbol"] for row in rows], rows[0]["date"])
        for row in rows:
            row["price"] = closes.get(row["qlib_symbol"])
    return rows


def ranks_by_code(codes: list[str], wait: bool = True) -> dict:
    """按 6 位代码查「当日榜内名次」，给持仓页用。

    红线：票池外的代码**不编名次**。持仓里没进候选池的票（基金、ST、上市太新、成交额太薄）
    模型从来没给它们打过分，硬排一个"第 301 名"就是把猜测说成结论。
    在池里但当日没有可打分行情（停牌/日线断档）也照样不给名次，只说原因。

    `wait=False` 时可能抛 `RankingNotReady`（当日还没算过），由调用方回「未就绪 + 后台预热」。
    """
    art = artifacts.load()
    rows = day_ranking(wait=wait)
    by_code = {row["code"]: row for row in rows}
    # 成员判定必须跟 day_ranking 用的是同一份名单：universe 模式下 art.pool 是训练池（300 只），
    # 实际打分的是 universe.txt（约 5,000 只），拿 art.pool 判会把「今天真被打过分」的票说成没打过。
    scored_pool = score_pool(art)
    pool_codes = {str(symbol)[2:] for symbol in scored_pool}
    universe = rows[0]["universe"] if rows else 0
    pool_size = len(scored_pool)
    items = []
    for code in codes:
        hit = by_code.get(code)
        if hit is not None:
            items.append({"code": code, "in_pool": True, "rank": hit["rank"],
                          "universe": universe, "score_raw": round(hit["score_raw"], 6)})
        elif code in pool_codes:
            items.append({"code": code, "in_pool": True, "rank": None, "universe": universe,
                          "note": f"在 {pool_size} 只候选池里，但当日没有可打分的行情（停牌或日线断档）"})
        else:
            items.append({"code": code, "in_pool": False, "rank": None, "universe": universe,
                          "note": f"不在当前 {pool_size} 只候选池里，模型没给这只打过分"})
    return {"ready": True, "date": rows[0]["date"] if rows else calendar()[-1],
            "pool_size": pool_size, "universe": universe, "items": items}


def _closes(symbols: list[str], date: str) -> dict[str, float]:
    import qlib.data

    frame = qlib.data.D.features(list(symbols), ["$close"], start_time=date, end_time=date)
    if frame is None or frame.empty:
        return {}
    frame = frame.droplevel("datetime")
    return {str(inst): (None if pd.isna(v) else float(v))
            for inst, v in frame["$close"].items()}


def stock_names(codes: list[str]) -> dict[str, str]:
    """股票名取自 Mongo 的 bk_stocks（与 8002 插件同源）。取不到就返回空表，名字是展示字段。"""
    try:
        import pymongo

        db = pymongo.MongoClient("127.0.0.1", 27017,
                                 serverSelectionTimeoutMS=4000)["eastmoney_news"]
        cursor = db["bk_stocks"].find({}, {"_id": 0, "stock_code": 1, "stock_name": 1})
        wanted = {str(c).zfill(6) for c in codes}
        out: dict[str, str] = {}
        for doc in cursor:
            code = str(doc.get("stock_code", "")).zfill(6)[-6:]
            if code in wanted and doc.get("stock_name"):
                out.setdefault(code, str(doc["stock_name"]))
        return out
    except Exception as exc:  # noqa: BLE001 - 名称缺失不该让整次选股失败
        logger.warning("股票名取不到（bk_stocks）：%s", exc)
        return {}
