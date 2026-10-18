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

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Alpha158 的滚动窗口最长 60 日，留一倍余量让首行也是「看过足够多历史」的状态
WARMUP_DAYS = 130
DEFAULT_TOPK = int(os.environ.get("QLIB_TOPK", "30"))

_cache: dict = {}
_cache_lock = threading.Lock()
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


def score_frame(art: artifacts.Artifacts, beg: str | None = None,
                end: str | None = None) -> pd.Series:
    """给 [beg, end] 区间内票池每只股票打分，返回 (datetime, instrument) 两层索引的 Series。"""
    days = calendar()
    end = end or days[-1]
    if end not in days:
        raise RuntimeError(f"结束日 {end} 不在 qlib 交易日历里")
    idx = days.index(end)
    beg = beg or days[max(0, idx - WARMUP_DAYS + 1)]
    frame = raw_features(art.pool, beg, end)
    pred = pd.Series(art.booster.predict(normalize(frame, art)), index=frame.index)
    return pred.reorder_levels(["datetime", "instrument"]).sort_index()


def latest_scores(topk: int = DEFAULT_TOPK, refresh: bool = False) -> list[dict]:
    """最近一个交易日的选股名单（含价格、分位分、原始分）。同一天内命中缓存。"""
    art = artifacts.load()
    init_qlib()
    date = calendar()[-1]
    key = (art.run_id, date, topk)
    with _cache_lock:
        if not refresh and _cache.get("key") == key:
            return list(_cache["rows"])
    started = time.time()
    pred = score_frame(art, end=date)
    day = pd.Timestamp(date)
    if not (pred.index.get_level_values("datetime") == day).any():
        raise RuntimeError(f"{date} 票池里没有可打分的股票（日线断档或票池过期）")
    tail = pred.xs(day, level="datetime").sort_values(ascending=False)
    top = tail.head(topk)
    closes = _closes(list(top.index), date)
    rows = []
    # score 是给页面排序用的 0~1 分位，用「整只票池里的位置」而不是「名单内的名次」，
    # 否则 topk 一改分位数就变，前后两批结果没法比
    total = len(tail)
    for position, (inst, raw) in enumerate(top.items()):
        rows.append({
            "code": str(inst)[2:],
            "qlib_symbol": str(inst),
            "date": date,
            "score_raw": float(raw),
            "score": float((total - position) / total),
            "rank": position + 1,
            "price": closes.get(str(inst)),
            "universe": len(tail),
        })
    with _cache_lock:
        _cache["key"] = key
        _cache["rows"] = rows
    logger.info("qlib 打分 %s：%d/%d 只，耗时 %.1f 秒", date, len(rows), len(tail),
                time.time() - started)
    return rows


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
