"""抓取个股资金流并落成「按交易日期分区」的可积累存储，含断档自检与有界补抓。

三个数据源：
  1. daily —— 东方财富**全市场单日批量**接口 push2.eastmoney.com/api/qt/clist/get，
     一次翻页（约 60 页 × 100 行）就拿到全部个股当天的主力/超大单/大单/中单/小单净流入
     与成交额。为什么换掉旧的「逐只历史」通道：那个接口在本机 IP 上被严重节流，
     实测跑 3 小时 36 分只写进 134/5,215 只，攒不出「连续 12 个月无断档」；
     批量通道一天 60 个请求，请求量少两个数量级。
     代价必须写清楚：批量接口只给「当下」，收盘后一过就取不到那天。
     所以它是**每天必须准点跑**的增量采集；历史补抓只能落在逐只接口还留着的日子
     （约最近 124 个交易日）上，且每轮只给有界预算（`--repair-budget`）。
  2. rank     —— 个股资金流排行快照：默认同花顺 stock_fund_flow_individual（稳定），
     --source em 切东财 stock_individual_fund_flow_rank。**只在交易日写**，
     否则周末会留下「周六的排行」这种脏文件（旧版就是这么写的）。
  3. industry —— 行业资金流快照（同花顺，即时/3日/5日/10日/20日）。

数据模型：daily 是「交易日 × 个股」的截面，主键 `(trade_date, code)` 唯一；
同一交易日被采多次时**新值非空才覆盖旧值**，任何一次成功都不该被另一次失败抹掉。
净额一律再除以当日成交额存成 `*_ratio`（消除股本体量差异）。
字段口径与防前瞻红线（T 日资金流只能用于 T+1 决策）见
`stock_selector/docs/资金流数据口径与防前瞻红线.md`。

存储布局：
    data/fund_flow/
        daily/YYYY-MM-DD.parquet         # 每个交易日一个分区文件
        rank/YYYY-MM-DD.parquet          # 仅交易日
        industry/YYYY-MM-DD.parquet      # 仅交易日
        status.json                      # 最近一次运行读数 + 告警去重日

用法:
    python scripts/fetch_fund_flow.py                      # 批量当日 + 快照 + 自检补抓
    python scripts/fetch_fund_flow.py --only daily         # 只跑批量
    python scripts/fetch_fund_flow.py --only daily --limit 3   # 冒烟：只抓前 3 页，不落盘
    python scripts/fetch_fund_flow.py --only daily --max-pages 20 --page-sleep 1.0
                                                              # 试运行：降量 + 拉长页间隔
    python scripts/fetch_fund_flow.py --self-check         # 不抓新数据，只自检 + 按预算补抓
    python scripts/fetch_fund_flow.py --repair-thin        # 连薄日一起补（默认薄日只报不补）
    python scripts/fetch_fund_flow.py --days 45            # 自检窗口（默认 30 个交易日）
    python scripts/fetch_fund_flow.py --migrate-legacy     # 一次性：旧 individual/ 摊成日分区
    python scripts/fetch_fund_flow.py --no-alert           # 断采不推钉钉
    python scripts/fetch_fund_flow.py --only daily --transport requests
                                                              # 退回纯 requests 通道（默认 curl_cffi，
                                                              # 换通道的原因与实测见 http_get 上方注释）
"""

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import requests

from stock_selector.fundflow.store import (
    DAILY_SUBDIR,
    expected_trading_days,
    gap_report,
    normalize_and_flag,
    partition_path,
    partition_rows,
    read_partition,
    stored_dates,
    write_partitions,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "fund_flow"
# 站点 JS 种的匿名 Cookie（refresh_em_cookie.py 落盘）。实测硬窗口边缘带它的存活率
# 14/15 vs 裸指纹 7/15（2026-10-20 交错 A/B）；文件不存在就维持原样裸跑。
COOKIE_PATH = OUTPUT_DIR / "em_cookie.txt"
KLINE_DIR = ROOT / "data" / "stock_kline"
SNAPSHOT_DIR = ROOT / "data" / "market_snapshot"
# 旧「逐只一文件」目录。2026-10-17 已全量摊成日分区并整体移到
# archive/old_individual_single_file_20261017/（试运行满 7 个交易日后删），
# 所以 --migrate-legacy 现在是空操作；保留常量是为了归档没删干净时还能重跑一次。
LEGACY_INDIVIDUAL_DIR = OUTPUT_DIR / "individual"
STATUS_FILE = OUTPUT_DIR / "status.json"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("fetch_fund_flow")

TZ_CN = timezone(timedelta(hours=8))

# 网络访问参数
REQUEST_TIMEOUT = 20       # 单次请求超时
MAX_RETRIES = 5            # akshare 通道（快照类，单次调用代价低，可以多试）
RETRY_BACKOFF = 2.0
PAGE_RETRIES = 3           # 批量分页：实测约 1/3 请求被断连、但重试一次即恢复，不必多等
PAGE_BACKOFF = 0.8
# 第 1 页单独用慢梯子：批量接口只给「当下」，第 1 页拿不到就等于这一天永远缺（后面没法重取），
# 所以这里值得等下去。旧版和翻页一样只等 0.8/1.6/3.2 秒（共 5.6s）就整轮放弃，而实测东财的
# 节流窗口是「分钟级」——2026-10-17 22:25 与 23:04 两轮都在 6 秒内放弃，整天不写。
# 这不是硬重试：间隔从 5s 涨到 30s，6 次共约 2 分钟，且只在第 1 页用（翻页缺几页只少些行）。
PAGE1_RETRIES = 6
PAGE1_BACKOFF = 5.0
PAGE1_BACKOFF_MAX = 30.0
# 整轮重来：第 1 页慢梯子跑完仍然拿不到时，等一段再重来（不是同一连接上硬重试）。
# 间隔取 (5min, 15min) 是因为实测的硬窗口就是十几到几十分钟：2026-10-17 22:25 整轮失败、
# 23:04 又失败、23:05~23:06 单发能过、23:07 起再次全断。最坏耗时 3 轮 × 约 2min(梯子)
# + 20min(等待) ≈ 26min，16:30 起跑还来得及，且每轮都是幂等的（分区到水位就跳过）。
ROUND_RETRIES = 2
ROUND_WAIT_SECONDS = (300, 900)
PAGE_SIZE = 100            # 东财 clist 单页上限
PAGE_SLEEP = 0.3           # 页间间距；整轮 60 页约 40 秒，不留间距容易被限流
PAGE_SLEEP_MAX = 5.0       # 连续失败时的降速上限：间隔再大就变成一天跑不完
REPAIR_SLEEP = 1.0         # 逐只补抓间距（daykline 是节流重灾区）
REPAIR_MAX_SECONDS = 180   # 每轮补抓的墙钟上限：补抓不许吃掉主通道的请求配额

# 薄日判定水位：已存分区行数的中位数 × 此系数，低于它算「看着成功、其实缺一半」
THIN_RATIO = 0.8
# 自检窗口：近 N 个应有交易日（阶段 1 定的 30）
DEFAULT_CHECK_DAYS = 30

# ---------------------------------------------------------------------------
# daily：东财全市场单日批量
# ---------------------------------------------------------------------------
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 沪市主板+科创板、深市主板+创业板、场内基金，实测 total≈5,9xx 且全是可交易的个股。
# 这串 fs 是接口侧的板块筛选：把指数挡在截面外（指数没有分档资金流，留着只会稀释样本）。
CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
CLIST_FIELDS = "f12,f14,f2,f3,f6,f62,f66,f72,f78,f84,f124"

# 接口字段 -> 本层列名。占比不用接口给的 f184（只有两位小数），自己用净额÷成交额算。
CLIST_COLUMNS = {
    "f12": "code", "f14": "name", "f2": "close", "f3": "pct_change", "f6": "amount",
    "f62": "main_net_inflow", "f66": "xlarge_net_inflow", "f72": "large_net_inflow",
    "f78": "medium_net_inflow", "f84": "small_net_inflow",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
}

# 传输层：`cffi`（curl_cffi，Chrome TLS/HTTP2 指纹 + 复用连接，默认）或 `requests`。
# 为什么换（2026-10-17 交错 A/B 实测，同一时间窗内轮流发同样的请求）：
#   1) 软节流时段 curl_cffi 能过而 requests 全被掐：探针 1 是 4/8 对 0/8，
#      探针 5（每 10s 一次、两臂交错各 8 次）是 3/8 对 0/8；合并 7/16 对 0/16。
#      push2his 上也出现过「requests 断连、curl_cffi 同一秒 200」。
#   2) 未被封锁时两臂都能过，但 cffi 更快（单页中位 115ms 对 162ms）。
# 它**不是**免死金牌：IP 进入硬窗口时两臂一起 0/10，六个 push2 镜像域名（push2/1/33/92/
# push2delay…）也在同一刻一起断连——说明这道闸按 IP 计，换域名轮换没用（实测过，别再做）。
# 所以换传输层是为了少浪费配额、并把能拿到的日子拿到手，硬窗口只能靠「多个时段重试」兜。
TRANSPORT = "cffi"
_SESSION = None
# 「一次失败请求长什么样」：随通道创建，重试循环据此决定是重试还是记失败。
_CATCH: tuple = (requests.RequestException, ValueError)


def http_session():
    """进程内唯一的 GET 通道（连接复用）。分开写是为了能在不碰网络的情况下测出选的是哪条通道。"""
    global _SESSION, _CATCH
    if _SESSION is None:
        if TRANSPORT == "cffi":
            from curl_cffi import requests as cffi_requests

            _SESSION = cffi_requests.Session(impersonate="chrome")
            # Referer 与真实采集页一致（data.eastmoney.com/zjlx/detail.html）：
            # Cookie 是那页 JS 种的，来源页对不上会被识别成"有身份没来历"的怪会话
            _SESSION.headers.update({"Referer": "https://data.eastmoney.com/zjlx/detail.html"})
            _CATCH = (cffi_requests.exceptions.RequestException, ValueError)
        else:
            _SESSION = requests.Session()
            _SESSION.headers.update(_HEADERS)
            _CATCH = (requests.RequestException, ValueError)
        if COOKIE_PATH.exists():
            # Cookie 头要在 impersonate 的指纹头之后覆盖，与浏览器实际发出顺序一致
            _SESSION.headers["Cookie"] = COOKIE_PATH.read_text(encoding="utf-8").strip()
    return _SESSION


def http_get(url: str, params: dict):
    """走复用连接发请求。curl_cffi 通道只补 Referer，UA 交给 impersonate 保持一致的指纹。"""
    return http_session().get(url, params=params, timeout=REQUEST_TIMEOUT)


def now_iso() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _get_json(url: str, params: dict, *, retries: int = PAGE_RETRIES,
              backoff: float = PAGE_BACKOFF, backoff_max: float = math.inf,
              sleeper=time.sleep) -> dict | None:
    """GET 并取 `data` 节点；重试用尽返回 None（调用方把这一页记成失败，不中断整轮）。

    这里**不能**抛异常：一天 60 页里总有几页被掐，一页失败只该少这一页的行，
    而不是让整天变成断档——那正是本任务要避免的事。
    """
    for attempt in range(retries + 1):
        try:
            resp = http_get(url, params)
            resp.raise_for_status()
            body = resp.json()
        except _CATCH as exc:
            if attempt < retries:
                sleeper(min(backoff * (2 ** attempt), backoff_max))
                continue
            logger.warning("[daily] 第 %s 页最终失败: %s", params.get("pn"), exc)
            return None
        data = body.get("data") if isinstance(body, dict) else None
        return data if data else None
    return None


def fetch_daily_page(page: int, *, sleeper=time.sleep, retries: int = PAGE_RETRIES,
                     backoff: float = PAGE_BACKOFF,
                     backoff_max: float = math.inf) -> tuple[list[dict], int] | None:
    """抓一页全市场资金流，返回 `(行, total)`；失败返回 None。

    `fid=f12` 固定按代码排序：翻页期间列表顺序必须稳定，否则同一只票可能被翻到两次
    而另一只永远翻不到——那是分页采集最隐蔽的错（每页都成功、总数也对，内容却缺人）。
    """
    params = {
        "pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1, "fid": "f12",
        "fs": CLIST_FS, "fltt": 2, "invt": 2,
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fields": CLIST_FIELDS,
        "_": int(time.time() * 1000),
    }
    data = _get_json(CLIST_URL, params, retries=retries, backoff=backoff,
                     backoff_max=backoff_max, sleeper=sleeper)
    if data is None:
        return None
    diff = data.get("diff")
    rows = list(diff.values()) if isinstance(diff, dict) else list(diff or [])
    return rows, int(data.get("total") or 0)


def trading_day_of(rows: list[dict], *, fallback: str) -> tuple[str, str]:
    """从 f124（这批行自己的行情时刻，Unix 秒）推出交易日与数据时刻。

    分区键必须用**数据自己的日期**而不是墙上时钟：周末或收盘后重跑，接口返回的仍是
    上一个交易日的快照，写进「今天」的文件就会造出一天并不存在的数据。
    """
    stamps = [int(row.get("f124") or 0) for row in rows if row.get("f124")]
    if not stamps:
        return fallback, fallback
    moment = datetime.fromtimestamp(max(stamps), TZ_CN)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def water_line(known: dict[str, int]) -> int:
    """「一个分区算完整」的行数下限：已存分区行数的中位数 × THIN_RATIO。

    票池本身会变（新股上市、退市），所以不写死 5,900；分区不足 5 个时返回 0——
    冷启动阶段没有可信水位，此时判「完整」只会让我们跳过真正该抓的那天。
    """
    series = sorted(known.values())
    if len(series) < 5:
        return 0
    return int(series[len(series) // 2] * THIN_RATIO)


def build_daily_frame(rows: list[dict], *, trade_date: str, data_ts: str) -> pd.DataFrame:
    """接口行 -> 本层日区（标准化与质量标记在 store 里做，这里只摊平字段）。"""
    records = []
    for row in rows:
        if not row.get("f12"):
            continue
        item = {column: row.get(field) for field, column in CLIST_COLUMNS.items()}
        item["date"] = trade_date
        records.append(item)
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame.from_records(records)
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    # 翻页期间列表位移会送来重复代码：留最后一条。重复 code 进 combine_first 会让索引
    # 非唯一，合并结果就不可信了。
    frame = frame.drop_duplicates("code", keep="last")
    return normalize_and_flag(frame, data_ts=data_ts, collected_at=now_iso())


def _try_refresh_cookie() -> bool:
    """被节流时现种一份新浏览器身份（headless Chrome 跑站点 JS），成功=True。

    起不了浏览器（没装 selenium/driver 下载失败）不算错误：维持拉长间隔的原路，
    绝不为刷 Cookie 把整轮采集拖死。
    """
    try:
        import refresh_em_cookie
        cookie = refresh_em_cookie.seed_cookie_string()
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOKIE_PATH.with_suffix(".tmp")
        tmp.write_text(cookie, encoding="utf-8")
        tmp.replace(COOKIE_PATH)
        logger.info("[daily] 已现种浏览器身份（%d 项 Cookie），换会话重试",
                    cookie.count("="))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[daily] 现种 Cookie 失败（%s: %s），维持原节奏",
                       type(exc).__name__, exc)
        return False


def run_daily(*, limit_pages: int = 0, max_pages: int = 0, page_sleep: float | None = None,
              force: bool = False, known: dict[str, int] | None = None,
              sleeper=time.sleep) -> dict:
    """全市场当日批量：先抓第 1 页判断该交易日分区是否已完整，完整就整轮跳过（幂等）。

    「已存在的日期直接跳过」不能只看文件名：同一天的分区可能只写了半截（大面积翻页
    失败留下的），所以跳过条件是**行数达到水位**，不是文件存在。

    两个试运行用的旋钮（2026-10-17 拍板：首周降量跑，防止把 IP 再打进黑名单）：
    `max_pages` 只翻前 N 页但**照常落盘**（`--limit` 是冒烟、不写），`page_sleep` 覆盖页间隔。
    必须写清楚 `max_pages` 的代价：批量接口只给「当下」，翻够 60 页之前停手就等于
    **故意留一个部分覆盖的交易日**，而这一天之后没法用批量通道重取，只能靠逐只补抓（很慢）。
    所以降量优先用 `page_sleep`（拉长间隔、票池仍完整），`max_pages` 只在头一两天用。
    """
    daily_dir = OUTPUT_DIR / DAILY_SUBDIR
    known = stored_dates(daily_dir) if known is None else known
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")

    first = None
    for round_no in range(ROUND_RETRIES + 1):
        first = fetch_daily_page(1, sleeper=sleeper, retries=PAGE1_RETRIES,
                                 backoff=PAGE1_BACKOFF, backoff_max=PAGE1_BACKOFF_MAX)
        if first is not None:
            break
        if round_no < ROUND_RETRIES:
            wait = ROUND_WAIT_SECONDS[round_no]
            # 值得等：批量接口只给「最后一个快照」，同一个晚上等多久都还是同一个交易日，
            # 而过了夜里再想取这一天就没通道了（逐只接口慢且只留 124 天）。
            logger.warning("[daily] 第 %d 轮第 1 页取不到（梯子已用尽），%ds 后整轮重来",
                           round_no + 1, wait)
            sleeper(wait)
    if first is None:
        logger.error("[daily] 第 1 页经 %d 轮 × 慢梯子（间隔涨到 %.0fs）仍失败，整天不写；"
                     "自检会把它算成断档，这一天之后只能靠逐只补抓",
                     ROUND_RETRIES + 1, PAGE1_BACKOFF_MAX)
        return {"ok": False, "reason": "page1_failed", "trade_date": "",
                "rounds": ROUND_RETRIES + 1}
    rows, total = first
    trade_date, data_ts = trading_day_of(rows, fallback=today)
    line = water_line(known)
    if not force and line and known.get(trade_date, 0) >= line:
        logger.info("[daily] %s 已存 %d 行（水位 %d），跳过整轮", trade_date,
                    known[trade_date], line)
        return {"ok": True, "skipped": True, "trade_date": trade_date,
                "rows": known[trade_date]}

    full_pages = max(1, math.ceil(total / PAGE_SIZE))
    pages = min(limit_pages, full_pages) if limit_pages else full_pages
    if max_pages:
        pages = min(pages, max_pages)
    collected = list(rows)
    failed_pages: list[int] = []
    sleep = PAGE_SLEEP if page_sleep is None else page_sleep
    streak = 0
    cookie_refreshed = False
    started = time.monotonic()
    for page in range(2, pages + 1):
        got = fetch_daily_page(page)
        if got is None:
            failed_pages.append(page)
            streak += 1
            # 连续两页被掐先换浏览器身份再试（一次为限，别和 JS 较劲）：
            # 同窗实测带站点 Cookie 的存活远高于裸指纹（见 COOKIE_PATH 注释）。
            if (streak >= 2 and not cookie_refreshed and TRANSPORT == "cffi"
                    and _try_refresh_cookie()):
                global _SESSION
                _SESSION = None      # 重建会话，吃进新 Cookie
                streak = 0
                cookie_refreshed = True
                continue             # 立刻重试这一页
            # 连续两页取不到 = 链路在被节流，把间隔拉长而不是把重试打满：
            # 硬重试正是 2026-10-17 那次把 IP 送进黑名单的原因。
            if streak >= 2 and sleep < PAGE_SLEEP_MAX:
                sleep = min(sleep * 3, PAGE_SLEEP_MAX)
                logger.warning("[daily] 连续 %d 页失败，页间隔拉长到 %.1fs（不硬重试）",
                               streak, sleep)
        else:
            streak = 0
            collected.extend(got[0])
        sleeper(sleep)
    elapsed = round(time.monotonic() - started, 1)

    if limit_pages:
        logger.info("[daily] 冒烟：只取前 %d 页共 %d 行（接口 total=%d，%.1fs），不落分区",
                    pages, len(collected), total, elapsed)
        return {"ok": True, "smoke": True, "trade_date": trade_date,
                "rows": len(collected), "total": total, "seconds": elapsed}

    frame = build_daily_frame(collected, trade_date=trade_date, data_ts=data_ts)
    if frame.empty:
        logger.error("[daily] %s 解析后无行，不写分区", trade_date)
        return {"ok": False, "reason": "empty_frame", "trade_date": trade_date}
    partial = bool(max_pages) and max_pages < full_pages
    if partial:
        logger.warning("[daily] 试运行只翻 %d/%d 页，%s 是部分覆盖（%d 行）："
                       "这一天之后无法用批量通道重取，只能靠逐只补抓",
                       max_pages, full_pages, trade_date, len(frame))
    written = write_partitions(daily_dir, frame)
    valid = int(frame["valid"].sum())
    logger.info("[daily] %s：页 %d（失败 %s），写入 %d 行（可算占比 %d 行）-> %s，%.1fs",
                trade_date, pages, failed_pages or "无", len(frame), valid, written, elapsed)
    return {"ok": True, "trade_date": trade_date, "pages": pages,
            "failed_pages": failed_pages, "rows": len(frame), "valid_rows": valid,
            "written": written, "seconds": elapsed, "data_ts": data_ts,
            "page_sleep": sleep, "partial": partial}


# ---------------------------------------------------------------------------
# 漏采补抓：逐只历史接口 + 有界预算 + 轮转游标
# ---------------------------------------------------------------------------
_EASTMONEY_HIST_COLS = [
    "date",
    "main_net_inflow", "small_net_inflow", "medium_net_inflow",
    "large_net_inflow", "xlarge_net_inflow",
    "main_net_inflow_pct", "small_net_inflow_pct", "medium_net_inflow_pct",
    "large_net_inflow_pct", "xlarge_net_inflow_pct",
    "close", "pct_change",
]


def _market_secid(code: str) -> int:
    """股票代码 -> 东财 secid 前缀（沪=1，深/北=0）。"""
    return 1 if code[0] in ("6", "5", "9") else 0


def fetch_individual_history(code: str, *, retries: int = 2) -> pd.DataFrame:
    """单只股票的逐日资金流历史（接口只保留最近约 124 个交易日）。

    只用于补抓，重试比旧版省：这个接口是节流重灾区，一只票最多 3 次请求，
    拿不到就留给下一轮——补抓是长期活儿，不追求一次跑完。
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "lmt": "0", "klt": "101", "secid": f"{_market_secid(code)}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(time.time() * 1000),
    }
    for attempt in range(retries + 1):
        try:
            resp = http_get(url, params)
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            klines = data.get("klines") or []
            if not klines:
                return pd.DataFrame()
            body = [row.split(",") for row in klines]
            frame = pd.DataFrame([row[: len(_EASTMONEY_HIST_COLS)] for row in body],
                                 columns=_EASTMONEY_HIST_COLS)
            for column in frame.columns:
                if column != "date":
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            frame["code"] = code
            frame["name"] = data.get("name")
            return frame
        except _CATCH as exc:
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
            else:
                logger.warning("[repair] %s 取历史失败: %s", code, exc)
    return pd.DataFrame()


def stamp_history(frame: pd.DataFrame) -> pd.DataFrame:
    """历史行（逐只接口/旧文件）的 `data_ts` 就是它自己的交易日。

    接口不给逐行时刻，日级数据能诚实标注的最细粒度就是日期——伪造一个采集时刻会让
    防前瞻审计失去依据。
    """
    frame["data_ts"] = frame["trade_date"]
    return frame


_snapshot_cache: dict[str, pd.Series] = {}


def amount_series(code: str, days: list[str]) -> pd.Series:
    """补齐逐只历史没有的成交额：先取那天的盘后快照，取不到再回日线。

    成交额是标准化唯一分母，缺了它整行占比就是 NaN，所以必须显式补；补不上的行由
    store 打上 `no_amount`（而不是悄悄填 0——「没有分母」和「净流入恰好为零」是两件事）。
    """
    out: dict[str, float] = {}
    pending: list[str] = []
    for day in days:
        if day not in _snapshot_cache:
            path = SNAPSHOT_DIR / f"{day}.parquet"
            _snapshot_cache[day] = (pd.read_parquet(path, columns=["code", "amount"])
                                    .set_index("code")["amount"]
                                    if path.exists() else pd.Series(dtype="float64"))
        series = _snapshot_cache[day]
        if code in series.index:
            out[day] = series[code]
        else:
            pending.append(day)
    if pending:
        path = KLINE_DIR / f"{code}.parquet"
        if path.exists():
            kline = pd.read_parquet(path, columns=["date", "amount"])
            kline["date"] = pd.to_datetime(kline["date"]).dt.strftime("%Y-%m-%d")
            found = kline[kline["date"].isin(pending)].set_index("date")["amount"]
            out.update({day: found.get(day) for day in pending})
    return pd.Series({day: out.get(day) for day in days}, dtype="float64")


def daily_universe(daily_dir: Path) -> list[str]:
    """票池口径：行数最多的那个日分区里的代码集合。

    为什么不用 `data/stock_kline/` 的文件名：那里 6,631 个文件含指数与非个股，而补抓要的
    是「批量接口本应给全的那些人」，用采集数据自己定义才自洽。
    没有任何日分区时返回空——那时既没有水位也没有票池，缺口无从判断。
    """
    paths = sorted(Path(daily_dir).glob("*.parquet"))
    if not paths:
        return []
    return sorted(set(read_partition(max(paths, key=partition_rows))["code"]
                      .astype(str).tolist()))


def run_repair(target_days: list[str], universe: list[str], *, budget: int,
               cursor: int = 0, sleeper=time.sleep, clock=time.monotonic,
               max_seconds: float = REPAIR_MAX_SECONDS) -> dict:
    """有界补抓：本轮最多取 `budget` 只、且不超过 `max_seconds`，把它们落在缺口日里。

    为什么要轮转游标：全市场近 5,900 只、接口又被节流，一轮不可能补完；若每轮都从
    第一只开始，排在后面的票永远补不到。游标让每轮从上次停的地方接着走。

    为什么要墙钟上限：2026-10-17 那次旧版逐只通道跑了 4 小时 22 分、几千次重试，
    直接把本机 IP 打进东财黑名单，连批量通道都取不到数。**补抓不能吃掉主通道要的配额**。
    """
    daily_dir = OUTPUT_DIR / DAILY_SUBDIR
    if not target_days:
        return {"skipped": True, "reason": "无缺口"}
    if not budget or not universe:
        return {"skipped": True, "reason": "预算为 0 或票池未就绪"}
    deadline = clock() + max_seconds

    need: set[str] = set()
    for day in target_days:
        path = partition_path(daily_dir, day)
        have = set(read_partition(path)["code"].astype(str)) if path.exists() else set()
        need.update(c for c in universe if c not in have)
    if not need:
        return {"skipped": True, "reason": "目标日已覆盖全部票池"}

    split = cursor % len(universe)
    batch = [code for code in universe[split:] + universe[:split] if code in need][:budget]
    logger.info("[repair] 目标日 %s，缺口 %d 只，本轮取 %d 只（游标 %d）",
                target_days, len(need), len(batch), cursor)

    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    attempted = 0
    stopped_by_time = False
    for code in batch:
        if clock() > deadline:
            stopped_by_time = True
            logger.warning("[repair] 到时间上限（%ds），剩下 %d 只留给下一轮",
                           int(max_seconds), len(batch) - attempted)
            break
        attempted += 1
        history = fetch_individual_history(code)
        if history.empty:
            failed.append(code)
        else:
            picked = history[history["date"].isin(target_days)]
            if not picked.empty:
                picked = picked.copy()
                picked["amount"] = amount_series(code, picked["date"].tolist()).values
                frames.append(picked)
        sleeper(REPAIR_SLEEP)

    written: dict[str, int] = {}
    if frames:
        frame = pd.concat(frames, ignore_index=True)
        # 接口自带的 *_pct 列本层不用（占比自己算，精度更高，见 store 注释）
        frame = frame[[c for c in frame.columns if not c.endswith("_pct")]]
        frame = normalize_and_flag(frame, data_ts="", collected_at=now_iso())
        written = write_partitions(daily_dir, stamp_history(frame))
    logger.info("[repair] 落分区 %s，失败 %d 只：%s",
                written or "无", len(failed), failed[:20] or "无")
    return {"targets": target_days, "gap": len(need), "batch": attempted,
            "planned": len(batch), "written": written, "failed": failed,
            "stopped_by_time": stopped_by_time,
            "next_cursor": (split + attempted) % len(universe)}


# ---------------------------------------------------------------------------
# rank / industry：同花顺（默认）或东方财富排行快照
# ---------------------------------------------------------------------------
RANK_SCHEMA = [
    "collect_date", "indicator", "source",
    "code", "name", "latest_price",
    "pct_change", "turnover_rate",
    "inflow", "outflow", "net_inflow", "amount",
    "stage_pct_change", "stage_turnover", "stage_net_inflow",
]
RANK_FIELDS = RANK_SCHEMA[1:]
THS_RANK_SYMBOLS = ["即时", "3日排行", "5日排行", "10日排行", "20日排行"]
RANK_EM_INDICATORS = ["今日", "3日", "5日", "10日"]
INDUSTRY_SYMBOLS = ["即时", "3日排行", "5日排行", "10日排行", "20日排行"]


def akshare_module():
    """延迟导入 akshare：只有 rank/industry 用到，而它的 import 要好几秒。

    每天 16:30 的主活儿是批量资金流，不该为两个快照通道每次付这笔钱。
    """
    import akshare
    return akshare


def _call_with_retry(func, *, retries: int = MAX_RETRIES, **kwargs):
    """调用 akshare 函数，失败时指数退避重试；仍失败返回 None（快照可缺不可崩）。"""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(**kwargs)
        except Exception as exc:  # noqa: BLE001 网络/反爬异常一律重试
            last_exc = exc
            if attempt < retries:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                logger.warning("[retry %d/%d] %s(%s) 失败: %s, %.0fs 后重试",
                               attempt, retries, func.__name__, kwargs, exc, wait)
                time.sleep(wait)
    logger.error("[FAIL] %s(%s) 最终失败: %s", func.__name__, kwargs, last_exc)
    return None


def _parse_amount_series(series) -> pd.Series:
    """解析同花顺金额文本如 '5.73亿'/'7664.47万'/'-2.59亿' 为数值(元)。"""
    text = series.astype(str).str.strip()
    values = pd.to_numeric(text.str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")
    has_yi = text.str.contains("亿")
    has_wan = text.str.contains("万")
    return values.where(~has_yi, values * 1e8).where(~has_wan, values * 1e4)


def _parse_pct_series(series) -> pd.Series:
    """解析同花顺百分比文本如 '17.50%' 为数值(%)。"""
    return pd.to_numeric(series.astype(str).str.replace("%", "").str.strip(), errors="coerce")


def fetch_rank_ths(symbol: str) -> pd.DataFrame:
    """同花顺个股资金流快照 -> 标准 RANK_FIELDS 行。"""
    ak = akshare_module()
    raw = _call_with_retry(ak.stock_fund_flow_individual, symbol=symbol)
    if raw is None or raw.empty:
        return pd.DataFrame()

    frame = pd.DataFrame()
    frame["code"] = raw["股票代码"].astype(str).str.zfill(6)
    frame["name"] = raw["股票简称"]
    frame["latest_price"] = pd.to_numeric(raw["最新价"], errors="coerce")
    if symbol == "即时":
        frame["indicator"] = "今日"
        frame["pct_change"] = _parse_pct_series(raw["涨跌幅"])
        frame["turnover_rate"] = _parse_pct_series(raw["换手率"])
        frame["inflow"] = _parse_amount_series(raw["流入资金"])
        frame["outflow"] = _parse_amount_series(raw["流出资金"])
        frame["net_inflow"] = _parse_amount_series(raw["净额"])
        frame["amount"] = _parse_amount_series(raw["成交额"])
        for column in ("stage_pct_change", "stage_turnover", "stage_net_inflow"):
            frame[column] = None
    else:
        frame["indicator"] = symbol.replace("排行", "")
        frame["stage_pct_change"] = _parse_pct_series(raw["阶段涨跌幅"])
        frame["stage_turnover"] = _parse_pct_series(raw["连续换手率"])
        frame["stage_net_inflow"] = _parse_amount_series(raw["资金流入净额"])
        for column in ("pct_change", "turnover_rate", "inflow", "outflow",
                       "net_inflow", "amount"):
            frame[column] = None
    frame["source"] = "ths"
    return frame[RANK_FIELDS].reset_index(drop=True)


def fetch_rank_em(indicator: str) -> pd.DataFrame:
    """东方财富个股资金流排行快照 -> 标准 RANK_FIELDS 行（东财反爬时拿不到）。"""
    ak = akshare_module()
    per = indicator.replace("日", "")
    raw = _call_with_retry(ak.stock_individual_fund_flow_rank, indicator=indicator)
    if raw is None or raw.empty:
        return pd.DataFrame()

    ren = {
        "代码": "code", "名称": "name", "最新价": "latest_price",
        f"{per}涨跌幅": "pct_change",
        f"{indicator}主力净流入-净额": "net_inflow",
    }
    frame = raw.rename(columns=ren)
    frame = frame[[c for c in ren.values() if c in frame.columns]].copy()
    for column in frame.columns:
        if column not in ("code", "name"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["indicator"] = indicator
    frame["source"] = "em"
    for column in ("turnover_rate", "inflow", "outflow", "amount",
                   "stage_pct_change", "stage_turnover", "stage_net_inflow"):
        frame[column] = None
    return frame[RANK_FIELDS].reset_index(drop=True)


_INDUSTRY_RENAME = {
    "序号": "rank", "行业": "industry", "行业指数": "industry_index",
    "行业-涨跌幅": "pct_change", "阶段涨跌幅": "pct_change",
    "流入资金": "inflow", "流出资金": "outflow", "净额": "net_inflow",
    "公司家数": "company_count", "领涨股": "leader_stock",
    "领涨股-涨跌幅": "leader_pct_change", "当前价": "leader_price",
}
INDUSTRY_SCHEMA = [
    "rank", "industry", "industry_index", "pct_change",
    "inflow", "outflow", "net_inflow", "company_count",
    "leader_stock", "leader_pct_change", "leader_price",
]


def fetch_industry(symbol: str) -> pd.DataFrame:
    """行业资金流快照标准化；金额字段单位为亿元（接口原样）。"""
    ak = akshare_module()
    raw = _call_with_retry(ak.stock_fund_flow_industry, symbol=symbol)
    if raw is None or raw.empty:
        return pd.DataFrame()

    frame = raw.rename(columns=_INDUSTRY_RENAME)
    present = [c for c in INDUSTRY_SCHEMA if c in frame.columns]
    frame = frame[present].copy()
    for column in frame.columns:
        if column not in ("industry", "leader_stock"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in set(INDUSTRY_SCHEMA) - set(frame.columns):
        frame[column] = None
    return frame[INDUSTRY_SCHEMA].reset_index(drop=True)


def write_snapshot(out_dir: Path, collect_date: str, frame: pd.DataFrame) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{collect_date}.parquet"
    frame.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 行)", path, len(frame))
    return len(frame)


def run_rank(collect_date: str, source: str = "ths") -> int:
    """个股资金流排行快照：每个交易日一个文件。"""
    symbols = RANK_EM_INDICATORS if source == "em" else THS_RANK_SYMBOLS
    frames = []
    for symbol in symbols:
        frame = fetch_rank_em(symbol) if source == "em" else fetch_rank_ths(symbol)
        if frame.empty:
            logger.warning("[rank/%s] %s 无数据", source, symbol)
        else:
            frames.append(frame)
        time.sleep(1.0)
    if not frames:
        logger.error("[rank] 全部源均无数据")
        return 0
    frame = (pd.concat(frames, ignore_index=True)
             .sort_values(["code", "indicator"]).reset_index(drop=True))
    frame.insert(0, "collect_date", pd.Timestamp(collect_date).normalize())
    return write_snapshot(OUTPUT_DIR / "rank", collect_date, frame[RANK_SCHEMA])


def run_industry(collect_date: str) -> int:
    """行业资金流快照：每个交易日一个文件（即时/3/5/10/20 日五个口径）。"""
    frames = []
    for symbol in INDUSTRY_SYMBOLS:
        frame = fetch_industry(symbol)
        if frame.empty:
            logger.warning("[industry] %s 无数据", symbol)
            continue
        frames.append(frame)
        time.sleep(1.0)
    if not frames:
        logger.error("[industry] 全部 symbol 均无数据")
        return 0
    frame = pd.concat(frames, ignore_index=True).reset_index(drop=True)
    frame.insert(0, "collect_date", pd.Timestamp(collect_date).normalize())
    return write_snapshot(OUTPUT_DIR / "industry", collect_date, frame)


def is_trading_day(kline_dir: Path, today: str) -> bool:
    """今天是不是交易日——rank/industry 的写入闸门。

    以锚票日线为准；日线自己滞后（当天爬虫没跑）时退回工作日判断：
    一次抓取失败不该连累两个快照通道被当成周末整天空掉。
    """
    latest = expected_trading_days(kline_dir, 1)
    if latest:
        if latest[0] == today:
            return True
        if latest[0] > today:
            return False
    return pd.Timestamp(today).weekday() < 5


# ---------------------------------------------------------------------------
# 旧 individual/ 目录一次性迁移
# ---------------------------------------------------------------------------
def run_migration(*, chunk_codes: int = 300) -> dict:
    """把旧「逐只 124 天历史」文件摊成日分区，让已攒下的历史进入同一口径。

    旧接口不给成交额，必须从盘后快照/日线 join 回来——不然这批历史全是 no_amount，
    标准化的占比列一根可用。旧目录本身不动：删不删是之后的决定。
    """
    if not LEGACY_INDIVIDUAL_DIR.exists():
        logger.info("[migrate] 没有 %s，无需迁移", LEGACY_INDIVIDUAL_DIR.name)
        return {"codes": 0, "days": 0}
    paths = sorted(LEGACY_INDIVIDUAL_DIR.glob("*.parquet"))
    if not paths:
        logger.info("[migrate] 旧目录为空")
        return {"codes": 0, "days": 0}

    daily_dir = OUTPUT_DIR / DAILY_SUBDIR
    days: set[str] = set()
    codes = 0
    frames: list[pd.DataFrame] = []
    for index, path in enumerate(paths, start=1):
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        frame = frame.copy()
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        frame["amount"] = amount_series(str(frame["code"].iloc[0]),
                                        frame["date"].tolist()).values
        keep = [c for c in frame.columns if not c.endswith("_pct") and c != "market"]
        frames.append(frame[keep])
        codes += 1
        if len(frames) >= chunk_codes or index == len(paths):
            merged = normalize_and_flag(pd.concat(frames, ignore_index=True),
                                        data_ts="", collected_at=now_iso())
            merged = stamp_history(merged)
            days.update(merged["trade_date"].tolist())
            write_partitions(daily_dir, merged)
            logger.info("[migrate] %d/%d 只，累计摊出 %d 个交易日", index, len(paths), len(days))
            frames = []
    return {"codes": codes, "days": len(days)}


# ---------------------------------------------------------------------------
# 自检、状态与告警
# ---------------------------------------------------------------------------
def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.warning("status.json 读不了，按空状态继续: %s", exc)
    return {}


def save_status(status: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再改名：两轮 cron 之间读到的一定是完整 JSON
    temp = STATUS_FILE.with_name("status.tmp.json")
    temp.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    temp.replace(STATUS_FILE)


def run_self_check(days: int, *, repair_budget: int, repair_cursor: int,
                   repair_thin: bool = False, sleeper=time.sleep) -> tuple[dict, dict]:
    """近 N 个应有交易日的覆盖度，并把缺口交给有界补抓。

    默认只补 `missing`（整天没有文件）——那才是「漏采」。
    `thin`（薄日）默认**只报不补**：把一天从 186 行补到 5,000 行需要 5,000 次逐只请求，
    按今天的节流程度等于每天自我 denial-of-service，正好是把 IP 打进黑名单的那条老路。
    薄日出现在告警里是为了让人看见，真要补用 `--repair-thin` 显式开。
    """
    daily_dir = OUTPUT_DIR / DAILY_SUBDIR
    expected = expected_trading_days(KLINE_DIR, days)
    known = stored_dates(daily_dir)
    report = gap_report(daily_dir, expected, known=known)
    report["expected_days"] = expected
    report["water_line"] = water_line(known)
    logger.info("[check] 应有交易日 %d，已存分区 %d，缺失 %s，薄日 %s（水位 %d 行）",
                len(expected), report["stored"], report["missing"] or "无",
                report["thin"] or "无", report["water_line"])
    targets = sorted(set(report["missing"]) | (set(report["thin"]) if repair_thin else set()))
    if not targets and report["thin"]:
        # 只有薄日、又没开 --repair-thin：补抓没被跳过得「无缺口」，否则告警会说谎
        return report, {"skipped": True, "reason": "薄日只报不补，本轮没有整天缺口"}
    repair = run_repair(targets, daily_universe(daily_dir),
                        budget=repair_budget, cursor=repair_cursor, sleeper=sleeper)
    return report, repair


def gap_alert_text(report: dict, repair: dict) -> tuple[str, str]:
    problems = []
    if report.get("missing"):
        problems.append(f"缺失 {len(report['missing'])} 个交易日")
    if report.get("thin"):
        problems.append(f"薄日 {len(report['thin'])} 个")
    title = "[资金流断采] " + "、".join(problems)
    thin_days = report.get("thin") or {}
    repaired_thin = set(repair.get("targets") or []) & set(thin_days)
    lines = [
        f"- 应有交易日 {len(report.get('expected_days') or [])}，已存分区 {report.get('stored')}",
        f"- 缺失日：{', '.join(report['missing'][:5]) if report.get('missing') else '（无）'}",
        "- 薄日：" + (", ".join(f"{d}({n}行)" for d, n in list(thin_days.items())[:5])
                  if thin_days else "（无）"),
        f"- 本轮补抓：{repair.get('batch', 0)} 只，落分区 {repair.get('written') or '无'}，"
        f"失败 {len(repair.get('failed') or [])} 只"
        + (f"（{repair['reason']}）" if repair.get("skipped") else ""),
    ]
    if thin_days and not repaired_thin:
        lines.append("- 薄日本轮**只报不补**：把一天补到满票池要几千次逐只请求，"
                     "会挤掉主通道配额；确实要补就显式跑 --repair-thin")
    if repair.get("failed"):
        lines.append(f"- 异常个股（前 10）：{', '.join(repair['failed'][:10])}")
    lines.append("- 注意：逐只历史只保留约 124 个交易日，越早的缺口越可能永久补不回来")
    return title, "\n".join(lines)


def maybe_alert(report: dict, repair: dict, status: dict, *, today: str,
                enabled: bool = True, dry_run: bool = False) -> str:
    """断采告警：同一天只推一条。

    采集每天跑一次，「缺 3 天」若连续存在三天，不去重就会推三条同样的话；
    发送失败不记日期（下一轮重试）——与 alert_pusher 的「先发出再记状态」同一条规矩。
    """
    if not (report.get("missing") or report.get("thin")):
        return "no_gap"
    if not enabled:
        return "disabled"
    title, text = gap_alert_text(report, repair)
    if dry_run:
        logger.info("[alert] dry-run：%s\n%s", title, text)
        return "dry_run"
    if status.get("alert_date") == today:
        return "deduped"
    try:
        import alert_pusher
        name, url, secret = alert_pusher.resolve_webhook(alert_pusher.DINGTALK)
    except Exception as exc:  # noqa: BLE001 渠道没配好不能拖垮采集主流程
        logger.warning("[alert] 取不到钉钉渠道，只留日志: %s", exc)
        return "no_channel"
    if alert_pusher.send(alert_pusher.DINGTALK, url, secret, title, text):
        logger.info("[alert] 已推送断采告警（渠道 %s）", name)
        status["alert_date"] = today
        return "sent"
    logger.warning("[alert] 推送失败，下一轮重试")
    return "send_failed"


# ---------------------------------------------------------------------------
def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    handler = logging.FileHandler(LOG_DIR / f"fund_flow_{time.strftime('%Y%m%d')}.log",
                                  encoding="utf-8")
    handler.setFormatter(fmt)
    logger.addHandler(handler)


def main() -> None:
    global TRANSPORT
    parser = argparse.ArgumentParser(
        description="资金流采集：全市场单日批量 + 日分区落盘 + 断档自检")
    parser.add_argument("--only", choices=["daily", "rank", "industry"], default=None,
                        help="只跑某一类；缺省三类全跑")
    parser.add_argument("--limit", type=int, default=0, metavar="PAGES",
                        help="批量通道冒烟：只抓前 N 页，不写分区")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="试运行降量：只翻前 N 页但照常落盘（代价见 run_daily 注释）")
    parser.add_argument("--page-sleep", type=float, default=None,
                        help=f"页间隔秒数，默认 {PAGE_SLEEP}；试运行降速就调大它")
    parser.add_argument("--days", type=int, default=DEFAULT_CHECK_DAYS,
                        help=f"自检窗口，应有交易日的个数（默认 {DEFAULT_CHECK_DAYS}）")
    parser.add_argument("--repair-budget", type=int, default=40, metavar="COUNT",
                        help="本轮逐只补抓的股票只数上限（0=只报缺口不补）")
    parser.add_argument("--repair-thin", action="store_true",
                        help="连薄日一起补（默认只报不补：补满一个薄日要几千次逐只请求）")
    parser.add_argument("--source", choices=["ths", "em"], default="ths",
                        help="排行快照数据源；ths=同花顺(默认稳定)，em=东方财富(常被反爬)")
    parser.add_argument("--backfill", type=int, default=0,
                        help="回补前 N 个自然日的 rank/industry（只补工作日）")
    parser.add_argument("--migrate-legacy", action="store_true",
                        help="一次性把旧 individual/*.parquet 摊成日分区")
    parser.add_argument("--self-check", action="store_true",
                        help="不抓新数据，只做自检与补抓")
    parser.add_argument("--force-daily", action="store_true",
                        help="即使当日分区看起来完整也重抓")
    parser.add_argument("--no-alert", action="store_true", help="断采不推钉钉")
    parser.add_argument("--alert-dry-run", action="store_true", help="告警只打日志")
    parser.add_argument("--transport", choices=["requests", "cffi"], default=TRANSPORT,
                        help="HTTP 通道：cffi=curl_cffi（浏览器 TLS 指纹+复用连接，默认）；"
                             "requests=只复用连接。选通道前先读 http_get 上方的实测记录")
    args = parser.parse_args()
    TRANSPORT = args.transport

    setup_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    status = load_status()
    summary: dict = {"run_at": now_iso()}

    if args.migrate_legacy:
        summary["migrate"] = run_migration()

    if not args.self_check:
        if args.only in (None, "daily"):
            result = run_daily(limit_pages=args.limit, max_pages=args.max_pages,
                               page_sleep=args.page_sleep, force=args.force_daily)
            summary["daily"] = {k: v for k, v in result.items() if k != "written"}
            if not result.get("ok"):
                logger.error("[daily] 本轮未取到数据（%s）", result.get("reason"))
            if result.get("trade_date"):
                stored = stored_dates(OUTPUT_DIR / DAILY_SUBDIR)
                summary["daily"]["stored_rows"] = stored.get(result["trade_date"], 0)

        if args.only in (None, "rank", "industry"):
            dates = [today] if args.backfill <= 0 else [
                (datetime.now(TZ_CN) - timedelta(days=offset)).strftime("%Y-%m-%d")
                for offset in range(args.backfill, -1, -1)
            ]
            for date in dates:
                # 快照只在交易日写：周末的「快照」没有对应交易日，落盘就是脏文件
                if date != today and pd.Timestamp(date).weekday() >= 5:
                    continue
                if date == today and not is_trading_day(KLINE_DIR, today):
                    logger.info("[snapshot] %s 非交易日，跳过 rank/industry", date)
                    continue
                written = summary.setdefault("snapshot", {}).setdefault(date, {})
                if args.only in (None, "rank"):
                    written["rank"] = run_rank(date, source=args.source)
                if args.only in (None, "industry"):
                    written["industry"] = run_industry(date)

    report, repair = run_self_check(args.days, repair_budget=args.repair_budget,
                                    repair_cursor=int(status.get("repair_cursor") or 0),
                                    repair_thin=args.repair_thin)
    summary["check"] = {"expected": len(report["expected_days"]),
                        "stored": report["stored"], "missing": report["missing"],
                        "thin": report["thin"], "water_line": report["water_line"]}
    summary["repair"] = {k: v for k, v in repair.items() if k != "failed"}
    if repair.get("next_cursor") is not None:
        status["repair_cursor"] = repair["next_cursor"]

    summary["alert"] = maybe_alert(report, repair, status, today=today,
                                   enabled=not args.no_alert,
                                   dry_run=args.alert_dry_run)
    status["last"] = summary
    save_status(status)
    logger.info("[done] %s", json.dumps(summary, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
