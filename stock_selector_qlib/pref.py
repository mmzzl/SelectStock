"""热门偏好名单（`hot_pref`）：他点名的四条——热门板块、新闻利好、排除小市值、趋势向上。

和模型名单的关系：这条**不重新打分**。它只在 `serving.day_ranking()` 已经进榜的票里，
按他的口径重排一遍，所以它自动继承两条护栏（薄流动、拉升后阴跌在 day_ranking 里就挡掉了），
也继承「池外不给名次」的规矩。

必须说在后面的红线（不是免责，是口径本身）：
- **板块热度、新闻利好、流通市值这三项在本机没有历史面板**——`board.parquet`/`member.parquet`
  每天覆盖成最新一份，`data/market_snapshot/` 只有 2026-10-12 起的日快照，
  新闻从 2026-09-30 起逐日留存。所以这条名单**没法回测**，它是「按你的偏好展示」的一条榜，
  不是「经验证有超额」的一条榜。
- 趋势那一路例外：`data/stock_kline/*.parquet` 有全历史（含 ma5/ma10/ma20/ma60），能测。
  批次 O/O2 已经测过「只留趋势向上」当硬闸会亏钱（docs §18/§19），所以这里趋势是**加分项 + 硬闸**
  一起用，而且热度权重排在趋势前面——这是他的要求，不是拟合出来的结论。
- 数据时点：板块热度是 18:30 落的（选股 17:30 跑），所以名单里用的热度**滞后一个交易日**；
  新闻 LLM 22:00 落，用的也是前一晚那批。`stats` 里带 `heat_lag_days`/`news_lag_days`，
  页面和台账都要如实显示，不能假装是当日的。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"

STRATEGY = "hot_pref"
LABEL = "热门偏好（板块+利好+大市值+趋势）"

# --- 可调阈值：全部走环境变量，改口径不用改代码（PREF_* 前缀，和 QLIB_* 分开） ---
MIN_BOARD_STOCKS = int(os.environ.get("PREF_MIN_BOARD_STOCKS", "10"))
TOP_BOARDS = int(os.environ.get("PREF_TOP_BOARDS", "30"))
# 「排除小市值」的线：2026-10-22 全市场流通市值中位数 51.4 亿，50 亿就是「不要一半小票」
MIN_FLOAT_MV = float(os.environ.get("PREF_MIN_FLOAT_MV", "50"))
DEFAULT_TOPK = int(os.environ.get("PREF_TOPK", "30"))
# 板块热度取哪一窗：board.parquet 每个板块存 3/7/30 三行，「当日热门」看的是 3 日
HEAT_WINDOW = int(os.environ.get("PREF_HEAT_WINDOW", "3"))

TREND_STRENGTH_SPAN = 0.10   # 收盘高出 MA20 10% 就算「强」，再高不加分（不奖励追高）
TREND_MA_SLOPE_DAYS = 5      # MA20 要和 5 个交易日前比，才算「抬头」
TREND_BLENDS = (0.7, 0.3)    # 趋势分 = 0.7×强度 + 0.3×均线多头

NEWS_CAP = 10.0              # 单日利好分累加到 10 分封顶
HEAT_WEIGHTS = {"ret_pct": 0.5, "turnover_ratio": 0.3, "limit_up_count": 0.2}
WEIGHTS = {"heat": 0.45, "trend": 0.35, "news": 0.20}


@dataclass
class PrefResult:
    rows: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def latest_day_not_after(days: list[str], day: str) -> str | None:
    """挑「不晚于给定日」的最近一个有数据的日期；一天都没有就返回 None（不编日期）。"""
    ok = sorted(d for d in days if d <= day)
    return ok[-1] if ok else None


def days_between(earlier: str, later: str) -> int:
    def _d(s: str) -> date:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    return (_d(later) - _d(earlier)).days


# --------------------------------------------------------------------------- 板块热度

def hot_boards(board: pd.DataFrame, min_stocks: int = MIN_BOARD_STOCKS,
               top: int = TOP_BOARDS) -> pd.DataFrame:
    """板块热度表：三项当日排好之后的加权分位，成员太少的板块直接不当热门。

    只看得见的三项：近 3 日涨幅（`ret_pct`）、放量倍数（`turnover_ratio`）、涨停家数。
    用分位不用原始值，因为这三项量纲差太多（8% vs 2 倍 vs 5 家）。
    """
    d = board[board["stock_count"].astype(float) >= min_stocks].copy()
    r = d[["ret_pct", "turnover_ratio", "limit_up_count"]].rank(pct=True)
    d["heat"] = sum(w * r[c] for c, w in HEAT_WEIGHTS.items())
    d = d.sort_values(["heat", "ret_pct"], ascending=False).head(top).reset_index(drop=True)
    d["heat_rank"] = range(1, len(d) + 1)
    return d


def best_board_per_stock(member: pd.DataFrame, hot: pd.DataFrame) -> dict[str, dict]:
    """每只票只认它所属板块里最热的那个（同票多板块不重复计分）。

    板块名从 `member` 侧取：`hot` 只带判定用的三列，两边同名列 merge 之后会变成
    `board_name_x/_y`，那种错在测试里才暴露，不如一开始就不重复。
    """
    cols = [c for c in ("board_code", "heat", "heat_rank", "ret_pct") if c in hot.columns]
    m = member.merge(hot[cols], on="board_code", how="inner")
    if m.empty:
        return {}
    m = m.sort_values(["stock_code", "heat"], ascending=[True, False]).drop_duplicates("stock_code")
    return {str(r.stock_code): {"board_code": r.board_code, "board_name": r.board_name,
                                "heat": float(r.heat), "heat_rank": int(r.heat_rank),
                                "board_ret_pct": float(r.ret_pct) if hasattr(r, "ret_pct") else None}
            for r in m.itertuples(index=False)}


# --------------------------------------------------------------------------- 新闻利好

def news_positive(rows) -> dict[str, dict]:
    """把利好拆成四格：板块码 / 板块中文名 / 六位个股码 / 个股名。

    为什么拆四格：LLM 抄的代码不可靠——实测 45 条利好里只有 4 条给了六位代码，其余是 BK 码或空，
    空的时候它把 BK 码写在 `name` 里（见 news_service 的同类处理）。只按 code 匹会几乎全漏。
    负分与零分**不进这条榜**（那是利空/中性，不是"利好为零"）。
    """
    out = {"boards": {}, "board_names": {}, "stocks": {}, "names": {}}
    for it in rows or []:
        score = int(it.get("score") or 0)
        if score <= 0:
            continue
        code = str(it.get("code") or "").strip().upper()
        name = str(it.get("name") or "").strip()
        category = str(it.get("category") or "").strip()
        if re.fullmatch(r"\d{6}", code):
            out["stocks"][code] = out["stocks"].get(code, 0) + score
        elif code.startswith("BK") or (not code and name.startswith("BK")):
            key = code or name
            out["boards"][key] = out["boards"].get(key, 0) + score
        elif category == "板块" and name:
            out["board_names"][name] = out["board_names"].get(name, 0) + score
        elif category == "个股" and name:
            out["names"][name] = out["names"].get(name, 0) + score
        elif name:
            out["names"][name] = out["names"].get(name, 0) + score
    return out


# --------------------------------------------------------------------------- 趋势向上

def trend_state(kline: pd.DataFrame, day: str | None = None) -> dict | None:
    """趋势向上 = 收盘站上 MA20 且 MA20 比 5 个交易日前高。判不了返回 None。

    `day` 给定时只用那天及以前的 K 线（回看口径，剪掉未来不改变判定）。
    均线缺失返回 **None** 而不是 False：None 是"没判"，False 是"判了且向下"，
    闸掉两类都一样，但台账里必须分得清是缺数还是走坏。
    """
    df = kline
    if day is not None and len(df):
        df = df[pd.to_datetime(df["date"]).le(pd.Timestamp(day))]
    need = TREND_MA_SLOPE_DAYS + 1
    if df is None or len(df) < need:
        return None
    last = df.iloc[-1]
    ma20 = pd.to_numeric(df["ma20"], errors="coerce")
    cur, prev = float(last["ma20"]), float(ma20.iloc[-need])
    close = float(last["close"])
    if any(pd.isna(v) for v in (cur, prev, close)):
        return None
    strength = min(1.0, max(0.0, (close / cur - 1.0) / TREND_STRENGTH_SPAN))
    ma5, ma10 = last.get("ma5"), last.get("ma10")
    alignment = bool(ma5 is not None and ma10 is not None
                     and not pd.isna(ma5) and not pd.isna(ma10)
                     and float(ma5) > float(ma10) > cur)
    return {"up": bool(close > cur and cur > prev), "strength": float(strength),
            "alignment": alignment, "ma20": cur, "gap": close / cur - 1.0}


def trend_score(state: dict) -> float:
    return TREND_BLENDS[0] * state["strength"] + TREND_BLENDS[1] * float(state["alignment"])


# --------------------------------------------------------------------------- 合成分

def compose_score(heat: float, trend: float, news: float) -> float:
    """三路加权（热度 45% / 趋势 35% / 新闻 20%），输出 0~1。

    落库和 App 都按 0~1 走；「满分 100」是展示层乘出来的，不在这里两口径。
    """
    return WEIGHTS["heat"] * heat + WEIGHTS["trend"] * trend + WEIGHTS["news"] * news


def _norm_news(score: int | float) -> float:
    return min(1.0, float(score or 0) / NEWS_CAP)


# --------------------------------------------------------------------------- 名单组装

def build_pref_list(rows: list[dict], best: dict, boards: dict, names: dict, mv: dict,
                    trends: dict, min_float_mv: float = MIN_FLOAT_MV, top: int = DEFAULT_TOPK,
                    board_names: dict | None = None, stocks: dict | None = None,
                    display: dict | None = None) -> PrefResult:
    """在已进榜的票上跑四条判据，返回他的偏好榜。

    三道硬闸的顺序就是「先便宜后贵」：先看在不在热门板块（不看的票连行情都不读），
    再卡流通市值，最后卡趋势。每一路各挡了多少都记进 stats——静默筛数是缺陷。
    """
    stats = {"in": len(rows), "no_hot_board": 0, "small_cap": 0, "mv_missing": 0,
             "trend_down": 0, "trend_unknown": 0, "news_hit": 0, "out": 0}
    picked = []
    for row in rows:
        code = str(row["code"])
        b = best.get(code)
        if not b:
            stats["no_hot_board"] += 1
            continue
        cap = mv.get(code)
        if cap is None:
            stats["mv_missing"] += 1
            continue
        if float(cap) < min_float_mv:
            stats["small_cap"] += 1
            continue
        st = trends.get(code)
        if st is None:
            stats["trend_unknown"] += 1
            continue
        if not st["up"]:
            stats["trend_down"] += 1
            continue

        board_name = b["board_name"]
        show = str(row.get("name") or (display or {}).get(code) or "").strip()
        news_score = (boards.get(b["board_code"], 0)
                      + (board_names or {}).get(board_name, 0)
                      + (stocks or {}).get(code, 0)
                      + names.get(show, 0))
        if news_score:
            stats["news_hit"] += 1
        score = compose_score(heat=float(b["heat"]), trend=trend_score(st),
                              news=_norm_news(news_score))
        picked.append({
            "code": code,
            "name": row.get("name") or "",
            "score": score,
            "asof": row.get("date"),
            "price": row.get("price"),
            "qlib_symbol": row.get("qlib_symbol"),
            "reason": _reason(board_name, b, news_score, cap, st, min_float_mv, row),
            "extra": {"board_code": b["board_code"], "board_name": board_name,
                      "board_heat": round(float(b["heat"]), 4),
                      "board_heat_rank": b.get("heat_rank"),
                      "news_score": news_score, "float_mv": float(cap),
                      "min_float_mv": float(min_float_mv),
                      "trend_strength": round(float(st["strength"]), 4),
                      "trend_alignment": bool(st["alignment"]),
                      "model_rank": row.get("rank"), "model_score": row.get("score")},
        })
    picked.sort(key=lambda r: (-r["score"], r["code"]))
    for i, r in enumerate(picked[:top]):
        r["rank"] = i + 1
    stats["out"] = len(picked[:top])
    return PrefResult(rows=picked[:top], stats=stats)


def _reason(board_name, b, news_score, cap, st, min_float_mv, row) -> str:
    rank = b.get("heat_rank")
    ret = b.get("board_ret_pct")
    head = f"热门板块「{board_name}」热度第 {rank if rank is not None else '—'}"
    if ret is not None:
        head += f"（近 3 日板块 {float(ret):+.2f}%）"
    gap = st.get("gap")
    trend = (f"站上 MA20 {gap * 100:.1f}%" if gap is not None
             else f"强度 {st['strength'] * 100:.0f}%")
    parts = [head,
             f"新闻利好 +{news_score} 分" if news_score else "新闻利好 0 分（当日没被点名）",
             f"流通市值 {float(cap):.0f} 亿（≥ {min_float_mv:.0f} 亿闸）",
             f"趋势向上：{trend}、均线{'多头排列' if st['alignment'] else '未完全跟上'}",
             f"模型榜第 {row.get('rank')} 名"]
    return " · ".join(parts)


# --------------------------------------------------------------------------- 装载（读文件）

_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _files(dir_path: Path) -> list[str]:
    """目录里形如 `2026-10-22.parquet` 的文件 → 日期列表（升序）。

    按日期格式认，不按名字长度猜：`2026-10-22.parquet` 只有 18 个字符，
    用长度筛会把每一天都判成「不是数据文件」，然后整条榜静默空掉。
    """
    return sorted(p.name[:10] for p in dir_path.glob("*.parquet") if _DATED.match(p.name[:10]))


def load_boards(day: str) -> tuple[pd.DataFrame, dict, str | None]:
    """板块热度：`board.parquet` 只有最新一份（每天覆盖），所以这里只读不算历史。

    每个板块在文件里存 3/7/30 日三行，这里只取 `HEAT_WINDOW` 那一窗：
    不筛等于把同一个板块数三遍，热度排名会被 30 日窗的老行情顶上去。

    返回的第三个值是这份热度的 `asof`——它可能就是昨天（17:30 选股时今天的还没落），
    调用方要把它当滞后如实报出来，不能当成当日的。
    """
    path = DATA / "sector_heat" / "board.parquet"
    member = DATA / "sector_heat" / "member.parquet"
    if not (path.exists() and member.exists()):
        return pd.DataFrame(), {}, None
    board = pd.read_parquet(path)
    asof = latest_day_not_after([str(x)[:10] for x in board["asof"]], day) or str(day)
    day_board = board[board["asof"].astype(str).str.slice(0, 10) == asof]
    if "window" in day_board.columns:
        day_board = day_board[day_board["window"] == HEAT_WINDOW]
    hot = hot_boards(day_board)
    mem = pd.read_parquet(member)
    if "window" in mem.columns:
        mem = mem[mem["window"] == HEAT_WINDOW]
    best = best_board_per_stock(mem, hot)
    return hot, best, asof


def load_snapshot(day: str) -> tuple[dict, dict, str | None]:
    """流通市值（亿元）与展示名：只有 2026-10-12 起的日快照，往前没有。"""
    d = DATA / "market_snapshot"
    use = latest_day_not_after(_files(d), day)
    if not use:
        return {}, {}, None
    df = pd.read_parquet(d / f"{use}.parquet", columns=["code", "name", "float_mv", "asset"])
    df = df[df["asset"] == "stock"]
    mv = {str(c).zfill(6): (None if pd.isna(v) else float(v))
          for c, v in zip(df["code"], df["float_mv"])}
    names = {str(c).zfill(6): str(n) for c, n in zip(df["code"], df["name"])}
    return mv, names, use


def load_news(day: str) -> tuple[dict, str | None]:
    """前一晚 22:00 那批 LLM 利好（`data/news_analysis/YYYY-MM-DD.parquet`）。

    这里直接读 parquet，不走 8002 的 mongo 读取层：插件进程不该依赖另一个库的在线状态。
    `bullish` 列是每条新闻的利好对象清单，逐条摊平后交给 `news_positive`。
    """
    d = DATA / "news_analysis"
    use = latest_day_not_after(_files(d), day)
    if not use:
        return news_positive([]), None
    rows = []
    for items in pd.read_parquet(d / f"{use}.parquet", columns=["bullish"])["bullish"]:
        if items is None or isinstance(items, float):  # None/NaN = 这条新闻没解析出利好
            continue
        for it in list(items):
            if isinstance(it, dict):
                rows.append(it)
    return news_positive(rows), use


def load_trends(codes: list[str], day: str) -> dict[str, dict]:
    """只对候选票读日线（全市场逐个读要几分钟，候选通常几百只）。

    读 `data/stock_kline/{code}.parquet` 的尾部（含现成 ma5/ma10/ma20），取到 `day` 为止。
    """
    out: dict[str, dict] = {}
    d = DATA / "stock_kline"
    for code in codes:
        path = d / f"{str(code).zfill(6)}.parquet"
        if not path.exists():
            out[str(code)] = None  # type: ignore[assignment]
            continue
        try:
            df = pd.read_parquet(path, columns=["date", "close", "ma5", "ma10", "ma20"]).tail(80)
        except Exception:  # noqa: BLE001 - 单票坏档不能带崩整张名单
            out[str(code)] = None  # type: ignore[assignment]
            continue
        out[str(code)] = trend_state(df, day=day)
    return out


def build_from_rows(rows: list[dict], day: str, top: int = DEFAULT_TOPK,
                    min_float_mv: float = MIN_FLOAT_MV) -> PrefResult:
    """把装载和判据串起来：输入是 `serving.day_ranking()` 的全池行，输出是他的偏好榜。

    stats 里必须带三项滞后（热度、新闻、市值各自是哪天落的）——17:30 跑的时候今天的
    板块热度（18:30）和新闻（22:00）都还没落地，用昨天的就得说昨天的。
    """
    hot, best, heat_day = load_boards(day)
    mv, display, mv_day = load_snapshot(day)
    news, news_day = load_news(day)
    candidates = [r["code"] for r in rows if r["code"] in best]
    trends = load_trends(candidates, day)
    res = build_pref_list(rows, best=best, boards=news["boards"], names=news["names"],
                          mv=mv, trends=trends, min_float_mv=min_float_mv, top=top,
                          board_names=news["board_names"], stocks=news["stocks"],
                          display=display)
    res.stats.update({
        "hot_boards": int(len(hot)), "candidates": len(candidates),
        "heat_day": heat_day, "heat_lag_days": None if not heat_day else days_between(heat_day, day),
        "news_day": news_day, "news_lag_days": None if not news_day else days_between(news_day, day),
        "mv_day": mv_day, "min_float_mv": float(min_float_mv),
        "news_boards": len(news["boards"]), "news_stocks": len(news["stocks"]),
    })
    # 静默缺数是缺陷：三路输入里任何一路整天空着，出榜就会是 0 只——那看起来像「今天没有票」，
    # 真实原因是数据没落。这一格非空就必须由调用方说出口（serving 里 WARNING，脚本里打在表头）。
    res.stats["data_missing"] = [k for k, v in (("板块热度", heat_day), ("新闻利好", news_day),
                                                ("流通市值", mv_day)) if not v]
    return res
