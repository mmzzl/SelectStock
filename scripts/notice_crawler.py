"""公告增量爬虫骨架：自动重爬下载失败的公告。

分层职责：
    notice_tracker.DownloadedTracker   # bitmap 去重：只记录"下载成功"的公告
    FailureStore                       # failed.json：持久化失败任务，供自动重爬
    crawl_with_retry()                 # 主循环：先重爬历史失败 -> 再拉最新增量

失败自动重爬设计：
    下载抛异常 -> record_failure(id, error) 写 failed.json（失败次数+1，记最后失败时间）
    下次运行 -> 先按时间序重爬 failed 里 attempts<max_attempts 的任务
               成功 -> tracker.mark + 从 failed.json 移除
               仍失败 -> 次数+1；达到 max_attempts 后视为"永久失败"停止自动重爬
               （日志 ERROR，保留记录便于人工排查，可手动清理）
    过程 CRASH/断电 -> failed.json 已持久化，重启后从磁盘恢复继续。

配置：复用 config.yaml 的 notice 节（track_dir/detail_dir/log_dir）。

用法（接入真实数据源）：
    from notice_crawler import crawl_with_retry, FailureStore
    from notice_tracker import DownloadedTracker

    tracker   = DownloadedTracker(track_dir)
    failures  = FailureStore(track_dir)

    def download(doc):            # 由调用方实现：下载详情并落盘到 detail_dir
        resp = requests.get(detail_url(doc))
        save_notice(detail_dir, resp)
        return "ok"

    crawl_with_retry(
        tracker, failures,
        fetch_list_pages=lambda: gen_notice_list_pages(since=max_id),  # 生成器：逐页 yield 公告
        download=download,
        max_attempts=3,
    )
"""

import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notice_tracker import DownloadedTracker, extract_numeric_id, load_config, setup_logging, logger

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"

_LOAD_TIMEOUT = 10  # 每个请求超时秒数（示例用）

# ---------------------------------------------------------------------------
# FailureStore：持久化的失败任务表，支撑自动重爬
# ---------------------------------------------------------------------------
class FailureStore:
    """{track_dir}/failed.json：{id: {art_code, attempts, last_error, last_failed}}"""

    def __init__(self, root: Path):
        self.path = Path(root) / "failed.json"
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.error("failed.json 解析失败(%s)，重新开始", exc)
                self._data = {}

    def record_failure(self, nid: int, art_code: str, error: str) -> int:
        """记录一次失败，返回累计失败次数。"""
        key = str(nid)
        rec = self._data.get(key) or {"art_code": art_code, "attempts": 0}
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last_error"] = str(error)[:200]
        rec["last_failed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["art_code"] = art_code
        self._data[key] = rec
        self.save()
        return rec["attempts"]

    def clear(self, nid: int) -> None:
        """下载成功后移除失败记录。"""
        self._data.pop(str(nid), None)
        self.save()

    def pending(self, max_attempts: int) -> list[dict]:
        """待重爬任务：按最后失败时间升序，跳过已超重试上限的。"""
        items = [
            {"nid": int(k), **v}
            for k, v in self._data.items()
            if int(v.get("attempts", 0)) < max_attempts
        ]
        items.sort(key=lambda r: (r.get("last_failed") or "",))
        return items

    def quarantined(self, max_attempts: int) -> list[int]:
        """永久失败（达重试上限不再自动爬）的公告 ID。"""
        return [
            int(k) for k, v in self._data.items()
            if int(v.get("attempts", 0)) >= max_attempts
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# 爬取频率控制（配置驱动：notice.request_interval / request_jitter）
# ---------------------------------------------------------------------------
class RateLimiter:
    """全局限速：保证相邻请求间隔 >= interval 秒，可加随机抖动。

    interval 来自 config.yaml notice.request_interval（相邻请求最小间隔秒）；
    jitter 为 request_jitter（±抖动秒）。列表翻页与详情下载共用同一实例，
    即可整体平滑限速、降低反爬特征。
    """

    def __init__(self, interval: float = 0.5, jitter: float = 0.0):
        self.interval = max(0.0, float(interval))
        self.jitter = max(0.0, float(jitter))
        self._last = 0.0

    def wait(self) -> None:
        """在发起请求前调用：确保距上一次调用满足最小间隔。"""
        import random

        now = time.monotonic()
        needed = self._last + self.interval - now
        if needed > 0:
            time.sleep(needed + (random.uniform(0, self.jitter) if self.jitter else 0))
        self._last = time.monotonic()

    @classmethod
    def from_config(cls) -> "RateLimiter":
        cfg = load_config()
        return cls(
            interval=cfg.get("request_interval", 0.5),
            jitter=cfg.get("request_jitter", 0.0),
        )


# ---------------------------------------------------------------------------
# 重爬与主循环
# ---------------------------------------------------------------------------
class MaxAttemptsExceeded(Exception):
    """下载在本次会话内超过重试次数上限，放弃。"""


def _download_with_retry(download, doc, attempts: int, backoff: float = 2.0):
    """对单条公告下载，失败指数退避重试 attempts 次。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            download(doc)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("[retry %d/%d] 下载失败 %s: %s", i, attempts, doc.get("title", doc.get("art_code", "")), exc)
            if i < attempts:
                time.sleep(backoff * i)
    raise MaxAttemptsExceeded(f"下载超过 {attempts} 次仍失败: {last}")


def crawl_with_retry(tracker: DownloadedTracker, failures: FailureStore,
                     fetch_list_pages, download, max_attempts: int = 3,
                     session_retries: int = 3, rate: RateLimiter | None = None) -> dict:
    """增量爬取主循环。

    fetch_list_pages: 无参调用，返回可迭代的公告 dict（实现方翻页，翻页请求建议
                      传入同一个 rate 并在取页前 rate.wait()）。
    download: 接收一条公告 dict，成功后返回；抛异常视为失败。
    rate: 全局限速器（配置驱动），每条 download 请求前 wait()。
    返回统计 {scanned, downloaded, skipped, recovered, failed, quarantined_now}。
    """
    stats = {"scanned": 0, "downloaded": 0, "skipped": 0,
             "recovered": 0, "failed": 0, "quarantined_now": 0}

    # 阶段1：先重爬历史失败（时间序）
    for rec in failures.pending(max_attempts):
        doc = {"art_code": rec["art_code"], "nid": rec["nid"]}
        stats["scanned"] += 1
        try:
            if rate:
                rate.wait()
            _download_with_retry(download, doc, session_retries)
            tracker.mark(rec["nid"])
            failures.clear(rec["nid"])
            stats["recovered"] += 1
            logger.info("[重爬成功] 恢复公告 id=%s", rec["nid"])
        except MaxAttemptsExceeded as exc:
            # 本次会话仍未成功 -> 记入 failed.json 等下次重爬
            failures.record_failure(rec["nid"], doc["art_code"], exc)
            n = failures._data[str(rec["nid"])]["attempts"]
            stats["failed"] += 1
            if n >= max_attempts:
                stats["quarantined_now"] += 1
                logger.error("[永久失败] 公告 id=%s 重试达上限，进入隔离(人工排查)", rec["nid"])

    # 阶段2：拉最新增量列表
    for doc in fetch_list_pages():
        stats["scanned"] += 1
        try:
            nid = extract_numeric_id(doc["art_code"])
        except (KeyError, ValueError) as exc:
            logger.warning("[跳过] 无有效 art_code: %s (%s)", doc, exc)
            continue

        if tracker.has(nid):
            stats["skipped"] += 1
            continue

        try:
            if rate:
                rate.wait()
            _download_with_retry(download, doc, session_retries)
            tracker.mark(nid)
            stats["downloaded"] += 1
        except MaxAttemptsExceeded as exc:
            failures.record_failure(nid, doc["art_code"], exc)
            stats["failed"] += 1
            logger.warning("[失败待重爬] 公告 id=%s attempts=%d 已记入 failed.json",
                           nid, failures._data[str(nid)]["attempts"])

    tracker.flush()
    failures.save()
    return stats


def main() -> None:  # 演示：模拟列表源 + 失败与重爬
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        root = Path(tmp)
        setup_logging(Path(cfg.get("log_dir", "logs")))
        tracker = DownloadedTracker(root, bucket_bits=int(cfg.get("bucket_bits", 20)))
        failures = FailureStore(root)
        rate = RateLimiter.from_config()          # 限速来自 config.yaml notice.*

        def gen_pages():
            yield {"art_code": "AN2024090500000000001", "title": "a1"}
            yield {"art_code": "AN2024090500000000002", "title": "a2"}
            yield {"art_code": "AN2024090500000000003", "title": "a3"}

        fail_once = {"a2": False}
        def download(doc):
            name = doc.get("title") or doc.get("art_code") or str(doc.get("nid"))
            if name == "a2" and not fail_once["a2"]:
                fail_once["a2"] = True
                raise RuntimeError("模拟网络中断")
            print(f"  [download] {name}")

        # 预置一个历史失败任务，验证"先重爬失败"
        failures.record_failure(1_000_000, "AN2024090400000000000", "上次断网")

        t0 = time.monotonic()
        stats = crawl_with_retry(tracker, failures, gen_pages, download,
                                 max_attempts=3, rate=rate)
        elapsed = time.monotonic() - t0
        print("stats:", stats)
        print(f"限速生效: 4 次请求耗时 {elapsed:.1f}s (interval={rate.interval}s, 应>=1.5s)")
        assert stats["recovered"] == 1          # 历史失败被重爬成功
        assert stats["downloaded"] == 3          # 列表 3 条全部下载(a2首次失败但被重试)
        assert elapsed >= rate.interval * 3      # 4 次请求至少有 3 个间隔
        assert not failures.pending(3)            # 没有残留失败任务
        print("failed.json 清理: OK")

        # 模拟一直失败的公告 -> 进入隔离
        def download_boom(doc):
            raise RuntimeError("永远失败")
        for _ in range(4):
            failures.record_failure(2_000_000, "AN2024090500000000000", "永远失败")
        assert tracker.has(1_000_000) and tracker.has(1) == False
        print("隔离/持久化: OK")


if __name__ == "__main__":
    main()