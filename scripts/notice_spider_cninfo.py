"""巨潮(cninfo)公告爬虫：真实抓取，任务队列放 Redis，天然支持增量与并发。

数据流（BFS 分层队列）：
    列表页任务(list)  --(产出)-->  详情下载任务(detail)  --(下载PDF)-->  bitmap 标记已下载
    notice:list      notice:detail

Redis 键约定（前缀 notice:）：
    list                list   列表页任务 JSON {"date","column","page"}
    detail              list   详情任务 JSON {id,secCode,secName,title,time_ms}
    bitmap              string announcementId 位图（原生 redis setbit），1=已下载
    failcount           hash   公告ID -> 失败次数
    retry               zset   公告ID -> 下次重试时间戳；到期自动回到 detail
    quarantine          zset   重试达上限的隔离公告（人工排查）
    listseen            set    "date:column" 已入队过的列表段，避免重复建任务

增量/断点：
    公告 ID 全局单调递增 -> announcementId 直接做位图索引；已下载的自动跳过。
    失败/崩溃 -> failcount+retry 持久化在 redis，重启后到期任务自动重爬。
    默认只爬最近 N 天（from_date_offset_days），配合位图天然支持"每天增量"。

反爬（沿用早期公告下载器的策略）：
    curl_cffi impersonate 随机浏览器指纹(chrome131/chrome124/edge101/firefox133)
    + 随机小间隔(RateLimiter，interval+jitter 从 config 读)。

配置：stock_selector/config/config.yaml 的 notice 节，扩展了 crawl/redis 子节。

用法:
    python scripts/notice_spider_cninfo.py                          # 默认最近 N 天
    python scripts/notice_spider_cninfo.py --days 3                 # 最近3天
    python scripts/notice_spider_cninfo.py --from 2026-09-01 --to 2026-09-06
    python scripts/notice_spider_cninfo.py --workers 4 --limit-detail 500
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from curl_cffi import requests as crequests
from curl_cffi.const import CurlOpt
import redis

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("notice_spider_cninfo")

# 巨潮常量
CNINFO_BASE = "https://www.cninfo.com.cn"
CNINFO_QUERY = CNINFO_BASE + "/new/hisAnnouncement/query"
CNINFO_DETAIL = CNINFO_BASE + "/new/announcement/bulletin_detail"
CNINFO_FILE = "http://static.cninfo.com.cn"

# redis 键
K_LIST = "notice:list"
K_DETAIL = "notice:detail"
K_BITMAP = "notice:bitmap"
K_FAIL = "notice:failcount"
K_RETRY = "notice:retry"
K_QUARANTINE = "notice:quarantine"
K_LISTSEEN = "notice:listseen"
REDIS_KEYS = [K_LIST, K_DETAIL, K_BITMAP, K_FAIL, K_RETRY, K_QUARANTINE, K_LISTSEEN]

_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# ---------------------------------------------------------------------------
# 稳定请求会话（沿用早期公告下载器的 anti-爬 策略）
# ---------------------------------------------------------------------------
class StableCninfoSession:
    def __init__(self, interval: float, jitter: float):
        self.session = crequests.Session()
        self.session.curl_options = {
            CurlOpt.SSL_VERIFYPEER: 0,
            CurlOpt.SSL_VERIFYHOST: 0,
            CurlOpt.TCP_NODELAY: 1,
            CurlOpt.CONNECTTIMEOUT_MS: 15000,
        }
        self.impersonate_list = ["chrome131", "chrome124", "edge101", "firefox133"]
        self._lock = threading.Lock()
        self._last = 0.0
        self.interval = float(interval)
        self.jitter = float(jitter)

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last + self.interval - now
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.jitter))
            self._last = time.monotonic()

    def _impersonate(self) -> str:
        return random.choice(self.impersonate_list)

    def safe_post(self, url, data=None, params=None, timeout=15):
        """form-encoded POST。巨潮 query 接口必须用表单编码，json body 会忽略
        pageNum/seDate 等参数而返回全库数据。"""
        self._throttle()
        return self.session.post(url, data=data, params=params, timeout=timeout,
                                 impersonate=self._impersonate())

    def safe_get(self, url, timeout=120):
        self._throttle()
        return self.session.get(url, timeout=timeout, impersonate=self._impersonate())


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    return cfg


def notice_cfg(cfg: dict) -> dict:
    return cfg.get("notice", {}) or {}


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(log_dir / f"notice_spider_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def safe_filename(name: str, max_len: int = 60) -> str:
    name = _INVALID_FILENAME.sub("_", name).strip(" _.")
    return name[:max_len] or "untitled"


# ---------------------------------------------------------------------------
# Spider
# ---------------------------------------------------------------------------
class NoticeSpider:
    def __init__(self, cfg: dict, from_date: str, to_date: str,
                 workers: int = 3, page_size: int = 30, max_attempts: int = 3,
                 max_pages: int = 30):
        n = cfg
        self.detail_dir = Path(ROOT / n.get("detail_dir", "data/notices"))
        self.columns = n.get("crawl", {}).get("columns", ["szse", "sse"])
        interval = n.get("request_interval", 0.5)
        jitter = n.get("request_jitter", 0.2)
        self.req = StableCninfoSession(interval, jitter)
        self.page_size = page_size
        self.max_attempts = max_attempts
        self.max_pages = max_pages
        self.from_date = from_date
        self.to_date = to_date
        self.workers = workers

        rcfg = n.get("redis", {}) or {}
        self.r = redis.Redis(
            host=rcfg.get("host", "127.0.0.1"),
            port=int(rcfg.get("port", 6379)),
            db=int(rcfg.get("db", 0)),
            socket_connect_timeout=5,
            decode_responses=True,
        )
        self.r.ping()
        self._stop = threading.Event()
        self._in_flight = 0
        self._lock = threading.Lock()
        self._stats = {"list_pages": 0, "listed": 0, "downloaded": 0,
                       "skipped": 0, "failed": 0, "recovered": 0}

    # -- 列表层：分页扫描巨潮公告列表 ------------------------------------
    def fetch_list_page(self, date: str, column: str, page: int) -> dict:
        payload = {
            "pageNum": str(page),
            "pageSize": str(self.page_size),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{date}~{date}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": True,
        }
        return self._post_json(CNINFO_QUERY, payload)

    def enqueue_list_day(self, date: str, column: str) -> None:
        key = f"{date}:{column}"
        if self.r.sismember(K_LISTSEEN, key):
            return
        self.r.sadd(K_LISTSEEN, key)
        self.r.lpush(K_LIST, json.dumps({"date": date, "column": column, "page": 1}))

    # -- 详情层：取 fileUrl 并下载 PDF -------------------------------------
    def fetch_detail(self, ann_id: int, time_ms: int) -> str:
        date_part = datetime.fromtimestamp(time_ms / 1000).strftime("%Y-%m-%d")
        resp = self.req.safe_post(CNINFO_DETAIL, params={
            "flag": True,
            "announceId": ann_id,
            "announcementTIme": date_part,       # 巨潮参数名原文如此
        })
        data = resp.json()
        file_url = (data.get("fileUrl") or "").strip()
        if not file_url:
            raise RuntimeError(f"公告 {ann_id} 无 fileUrl: {data}")
        return file_url

    def download_pdf(self, file_url: str, save_path: Path) -> None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self.req.safe_get(file_url)
        save_path.write_bytes(resp.content)

    # -- 任务执行 ----------------------------------------------------------
    def _post_json(self, url: str, data: dict, retries: int = 3) -> dict:
        last = None
        for i in range(1, retries + 1):
            try:
                resp = self.req.safe_post(url, data=data)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning("[retry %d/%d] 列表失败 %s: %s", i, retries, data.get("seDate", ""), exc)
                if i < retries:
                    time.sleep(2.0 * i)
        raise RuntimeError(f"列表请求最终失败: {last}")

    def _handle_list_task(self, task: dict) -> None:
        date, column, page = task["date"], task["column"], task["page"]
        data = self.fetch_list_page(date, column, page)
        anns = data.get("announcements") or []
        self._stats["list_pages"] += 1

        pipe = self.r.pipeline()
        for a in anns:
            ann_id = a.get("announcementId")
            if not ann_id:
                continue
            pipe.lpush(K_DETAIL, json.dumps({
                "id": int(ann_id),
                "secCode": a.get("secCode", ""),
                "secName": a.get("secName", ""),
                "title": a.get("shortTitle") or a.get("announcementTitle", ""),
                "time_ms": a.get("announcementTime") or 0,
            }))
            self._stats["listed"] += 1
        pipe.execute()
        logger.info("[list] %s %s page=%d 记录=%d (累计 listed=%d)",
                    date, column, page, len(anns), self._stats["listed"])

        # 翻页结束条件：已到接口返回的 totalpages；max_pages 兜底保护
        total_pages = int(data.get("totalpages") or 0)
        if page >= total_pages:
            logger.info("[list] %s %s 翻完 totalpages=%d", date, column, total_pages)
            return
        if not anns:
            logger.info("[list] %s %s 空页，该日扫描完成", date, column)
            return
        if page >= self.max_pages:
            logger.warning("[list] %s %s page=%d 达兜底上限 %d 页，截断",
                           date, column, page, self.max_pages)
            return
        self.r.lpush(K_LIST, json.dumps(
            {"date": date, "column": column, "page": page + 1}))

    def _handle_detail_task(self, task: dict) -> None:
        ann_id = task["id"]
        if self.r.getbit(K_BITMAP, ann_id):
            self._stats["skipped"] += 1
            return
        path = self.detail_dir / task["secCode"] / (
            f"{time.strftime('%Y%m%d', time.localtime(task['time_ms'] / 1000))}_"
            f"{ann_id}_{safe_filename(task['title'])}.pdf")
        if path.exists():
            self.r.setbit(K_BITMAP, ann_id, 1)
            logger.info("[skip-已下载] %s (%s)", task["title"][:30], path.name)
            return
        try:
            url = self.fetch_detail(ann_id, task["time_ms"])
            self.download_pdf(url, path)
            self.r.setbit(K_BITMAP, ann_id, 1)
            self._stats["downloaded"] += 1
            size_kb = path.stat().st_size / 1024
            logger.info("[ok] %s -> %s (%.1fKB)",
                        task["title"][:40], path.name, size_kb)
        except Exception as exc:  # noqa: BLE001
            self._stats["failed"] += 1
            self._record_failure(ann_id, task["title"], exc)

    # -- 失败处理与自动重爬 ------------------------------------------------
    def _record_failure(self, ann_id: int, title: str, exc: Exception) -> None:
        n = self.r.hincrby(K_FAIL, ann_id, 1)
        if n <= self.max_attempts:
            score = time.time() + 30 * n          # 退避：30s/90s/...
            self.r.zadd(K_RETRY, {ann_id: score})
            logger.warning("[失败→等待重爬] %s attempts=%d 将于 %.0fs 后重试: %s",
                           str(title)[:30], n, 30 * n, exc)
        else:
            self.r.zadd(K_QUARANTINE, {ann_id: time.time()})
            logger.error("[隔离] %s attempts=%d 达上限，转入隔离(人工排查): %s",
                         str(title)[:30], n, exc)

    def _promote_retry(self, now: float) -> None:
        due = self.r.zrangebyscore(K_RETRY, 0, now, start=0, num=200)
        if not due:
            return
        pipe = self.r.pipeline()
        for ann_id in due:
            pipe.lpush(K_DETAIL, json.dumps({
                "id": int(ann_id), "secCode": "", "secName": "",
                "title": f"retry:{ann_id}", "time_ms": int(now * 1000)}))
            pipe.zrem(K_RETRY, ann_id)
            self._stats["recovered"] += 1
        pipe.execute()
        logger.info("[重爬] 把 %d 条到期失败任务重新入队", len(due))

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> dict:
        self.detail_dir.mkdir(parents=True, exist_ok=True)
        # 1. 建立列表任务：日期 × 板块
        days = []
        d = datetime.strptime(self.from_date, "%Y-%m-%d")
        end = datetime.strptime(self.to_date, "%Y-%m-%d")
        while d <= end:
            days.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        for date in days:
            for col in self.columns:
                self.enqueue_list_day(date, col)
        logger.info("已入队列表任务 %d 个 (date×column), 区间 %s ~ %s, workers=%d",
                    len(days) * len(self.columns), self.from_date, self.to_date, self.workers)
        for key in REDIS_KEYS:
            logger.info("redis %s len=%s", key,
                        self.r.llen(key) if key in (K_LIST, K_DETAIL)
                        else self.r.hlen(key) if key == K_FAIL
                        else self.r.zcard(key) if key in (K_RETRY, K_QUARANTINE)
                        else self.r.strlen(key) if key == K_BITMAP
                        else self.r.scard(key))

        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(max(1, self.workers))]
        for w in workers:
            w.start()

        # 主线程全程负责：先从 list 队列取列表任务，再消费 detail 任务
        self._main_loop()
        # 等待工作线程扫尾（详情队列会被它们消费完）
        for w in workers:
            w.join(timeout=30)
        self._flush_stats()
        return dict(self._stats)

    def _worker(self) -> None:
        """detail 消费线程：非阻塞 rpop 轮询（不用 brpop——阻塞命令会在共享连接池上
        临时设置 socket 读超时，redis-py 多线程下会随机抛 TimeoutError 打挂 worker）。"""
        while not self._stop.is_set():
            item = self._poll_pop(K_DETAIL)
            if not item:
                time.sleep(0.3)
                continue
            try:
                self._handle_detail_task(json.loads(item))
            except Exception as exc:  # noqa: BLE001
                logger.error("[worker] 消费详情异常: %s", exc)

    def _poll_pop(self, key: str):
        """带异常兜底的非阻塞出队，返回 task 字符串或 None。"""
        try:
            return self.r.rpop(key) if key == K_DETAIL else self.r.lpop(key)
        except redis.RedisError as exc:  # noqa: BLE001
            logger.warning("[redis] %s 出队失败(自动重连): %s", key, exc)
            time.sleep(1)
            return None

    def _main_loop(self) -> None:
        idle = 0
        while True:
            self._promote_retry(time.time())
            # 优先做列表：BFS 先铺开列表层
            item = self._poll_pop(K_LIST)
            if item:
                idle = 0
                try:
                    self._handle_list_task(json.loads(item))
                except Exception as exc:  # noqa: BLE001
                    logger.error("[list] 处理失败: %s", exc)
                continue
            # 列表空闲时消费详情
            item = self._poll_pop(K_DETAIL)
            if item:
                idle = 0
                try:
                    self._handle_detail_task(json.loads(item))
                except Exception as exc:  # noqa: BLE001
                    logger.error("[detail] 处理失败: %s", exc)
                continue
            # 双队列都空
            idle += 1
            self._flush_stats()
            if idle >= 4:   # 连续 4 轮约2s 无任务，且重试队列无到期任务 -> 完成
                due = self.r.zcount(K_RETRY, 0, time.time())
                if due == 0:
                    logger.info("所有队列已空，爬取完成")
                    self._stop.set()
                    return
            time.sleep(0.5)

    def _flush_stats(self) -> None:
        try:
            pipe = self.r.pipeline()
            pipe.llen(K_DETAIL); pipe.llen(K_LIST); pipe.bitcount(K_BITMAP)
            pipe.zcard(K_RETRY); pipe.zcard(K_QUARANTINE)
            detail, lst, bit, retry, quar = pipe.execute()
            logger.info(
                "[进度] listed=%d downloaded=%d skipped=%d failed=%d recovered=%d | "
                "队列 list=%s detail=%s bitmap=%s retry=%s quarantine=%s",
                self._stats["listed"], self._stats["downloaded"],
                self._stats["skipped"], self._stats["failed"],
                self._stats["recovered"], lst, detail, bit, retry, quar)
        except Exception:  # noqa: BLE001
            pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="巨潮公告爬虫（redis 队列 + 位图增量）")
    p.add_argument("--days", type=int, default=None, help="增量抓最近 N 天（含今天）")
    p.add_argument("--from", dest="from_date", type=str, default=None, help="起始日期 YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", type=str, default=None, help="结束日期 YYYY-MM-DD")
    p.add_argument("--workers", type=int, default=3, help="详情下载并发线程数")
    p.add_argument("--page-size", type=int, default=30, help="列表每页条数")
    p.add_argument("--max-attempts", type=int, default=3, help="单条公告最大失败重试次数")
    p.add_argument("--max-pages", type=int, default=None,
                   help="单日每板块列表最大翻页数（巨潮 total 为全库，须截断）")
    p.add_argument("--flush-queue", action="store_true", help="清空待处理队列(保留位图与隔离)")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    setup_logging(LOG_DIR)
    cfg = load_config()
    n = notice_cfg(cfg)

    if args.flush_queue:
        r = redis.Redis(host=(n.get("redis", {}) or {}).get("host", "127.0.0.1"),
                        port=int((n.get("redis", {}) or {}).get("port", 6379)),
                        db=int((n.get("redis", {}) or {}).get("db", 0)), decode_responses=True)
        for k in (K_LIST, K_DETAIL, K_LISTSEEN, K_FAIL, K_RETRY):
            r.delete(k)
            logger.info("已清空 %s", k)
        return

    to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")
    if args.from_date:
        from_date = args.from_date
    elif args.days:
        from_date = (datetime.now() - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    else:
        off = int(n.get("crawl", {}).get("from_date_offset_days", 1))
        from_date = (datetime.now() - timedelta(days=off)).strftime("%Y-%m-%d")

    max_pages = args.max_pages or int((n.get("crawl", {}) or {}).get("max_pages_per_day", 30))
    spider = NoticeSpider(n, from_date, to_date,
                          workers=args.workers,
                          page_size=args.page_size,
                          max_attempts=args.max_attempts,
                          max_pages=max_pages)
    stats = spider.run()
    logger.info("最终统计: %s", stats)


if __name__ == "__main__":
    main()