"""东财个股研报采集：按发布日逐日分页拉取，落 `data/report/{YYYY-MM-DD}.parquet`。

产出（一天一个文件，重跑幂等，按 `info_code` 去重保留最新）：
    data/report/{YYYY-MM-DD}.parquet    列定义见 stock_selector/report/store.py:REPORT_COLUMNS

数据源：`https://reportapi.eastmoney.com/report/list`（东财数据中心-研究报告-个股研报，
qType=0）。实测 2026-10-26→10-30 全市场 786 份 ≈ 79 份/交易日，一次一页 100 条就够，
比资金流那条链轻得多；但**一页一停仍然保留**——那条链栽过 IP 时间窗节流，同样的便宜不占第二次。

三条口径先说死：

- **窗口只到昨天**。接口只给发布日不给发布时刻，今天挂出来的报告可能是收盘后任何时刻挂的，
  当天用来选股等于偷看当天。要今天的报告，明天再取。
- **只存不改**。这一层只做「取回来、归一、落盘、把取不到的日子报出来」，不判任何选股口径；
  300/688 不剔（§30 是人群口径，剔与不剔留给读时），评级方向由前后数值比出（见 store 的实测依据）。
- **空日与报错日都要出声**。零条不写空文件（否则「那天真没研报」和「那天没请求」在目录里长得一样），
  报错日照实记进 `failed_days` 并以非零退出，让调度器看得见。

用法:
    python3 scripts/fetch_report_em.py                      # 补昨天
    python3 scripts/fetch_report_em.py --days 7             # 补最近 7 个「昨天及更早」的日历日
    python3 scripts/fetch_report_em.py --start 2026-02-01 --end 2026-10-29   # 历史回补
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from stock_selector.report import store

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "report"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("fetch_report_em")

URL = "https://reportapi.eastmoney.com/report/list"
PAGE_SIZE = 100
DELAY_SECONDS = 0.7
# 一天的研报正常就几页；这个上限防的是接口把 pageSize 当摆设、TotalPage 报出天量时死循环
MAX_PAGES = 60


def default_days(today_str: str, count: int) -> list[str]:
    """采集窗口：以「昨天」结尾往前数 count 个日历日，升序返回。

    今天不在窗口里（口径见模块 docstring），所以参数叫 today 也只是基准日。
    """
    base = datetime.strptime(today_str, "%Y-%m-%d")
    return [(base - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(count, 0, -1)]


def build_params(day: str, page: int, page_size: int) -> dict:
    """一页请求的参数。beginTime==endTime==day：一天一次窗口，返回的就是那天的报告。"""
    return {
        "industryCode": "*", "industry": "*", "rating": "*", "ratingChange": "*",
        "beginTime": day, "endTime": day, "qType": "0",
        "pageSize": str(page_size), "pageNo": str(page),
        "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        "orgCode": "", "code": "", "rcode": "", "fields": "",
    }


def fetch_json(params: dict) -> dict:
    resp = requests.get(URL, params=params, timeout=25)
    resp.raise_for_status()
    return resp.json()


@dataclass
class CollectResult:
    days: list[str]
    rows: int = 0
    invalid: int = 0
    empty_days: list[str] = field(default_factory=list)
    failed_days: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_days


def _fetch_day(day: str, api, page_size: int, delay: float, max_pages: int) -> list[dict]:
    """翻完一天的页；任何一页出错就整天作废（由调用方记进 failed_days）。"""
    rows: list[dict] = []
    page = 1
    total_page = 1
    while page <= total_page:
        payload = api(build_params(day, page, page_size))
        total_page = int(payload.get("TotalPage") or 1)
        rows.extend(payload.get("data") or [])
        time.sleep(delay)
        page += 1
        if page > max_pages:
            logger.error("%s 页数超过上限 %d（接口共 %d 页），截断", day, max_pages, total_page)
            break
    return rows


def collect_days(days: list[str], api, out_dir: Path, page_size: int,
                 collected_at: str, delay: float = DELAY_SECONDS,
                 max_pages: int = MAX_PAGES) -> CollectResult:
    """逐日采集并落盘。分区键用请求日，而不是行里的 publishDate：

    接口按 publishDate 过滤，正常情况下两者相同；不相同（回填出更晚日期）的行会被
    `store.normalize_report` 标成 `future_publish` 而留在**请求日**的文件里——换到
    未来那天的文件去，等于把一条异常数据藏进一个当天根本没人会去读的分区。
    """
    result = CollectResult(days=list(days))
    for day in days:
        try:
            raw_rows = _fetch_day(day, api, page_size, delay, max_pages)
        except Exception as exc:  # noqa: BLE001 - 单日失败不该带走整轮
            logger.error("%s 采集失败：%s", day, exc)
            result.failed_days.append(day)
            continue
        if not raw_rows:
            logger.warning("%s 零条研报（不落文件，缺数当天可见）", day)
            result.empty_days.append(day)
            continue
        normalized = [store.normalize_report(r, collected_at) for r in raw_rows]
        frame = store.to_frame(normalized)
        path = store.save_day(frame, day, out_dir)
        result.rows += len(frame)
        result.invalid += int((~frame["valid"]).sum())
        logger.info("%s → %d 条（不可用 %d 条）写入 %s", day, len(frame), result.invalid, path)
    return result


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(
        LOG_DIR / f"report_em_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="东财个股研报采集（按发布日分区落盘）")
    parser.add_argument("--days", type=int, default=1, help="往前补几个日历日（不含今天）")
    parser.add_argument("--start", default=None, help="显式起始日 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="显式结束日 YYYY-MM-DD（含）")
    parser.add_argument("--out-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS, help="每页之间的停等秒数")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--today", default=None, help="基准日（调试用，默认取当前日期）")
    args = parser.parse_args()

    setup_logging()
    today = args.today or datetime.now().strftime("%Y-%m-%d")
    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
        days = [(start + timedelta(days=offset)).strftime("%Y-%m-%d")
                for offset in range((end - start).days + 1)]
    else:
        days = default_days(today, args.days)
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("采集 %d 天：%s … %s（基准日 %s，今天不入窗口）",
                len(days), days[0], days[-1], today)
    result = collect_days(days, fetch_json, Path(args.out_dir), args.page_size,
                          collected_at, args.delay, args.max_pages)
    print(f"共 {result.rows} 条（标记不可用 {result.invalid} 条）；"
          f"零条 {len(result.empty_days)} 天 {result.empty_days}；"
          f"失败 {len(result.failed_days)} 天 {result.failed_days}")
    if not result.ok:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
