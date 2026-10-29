import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import traceback
import requests
import pandas as pd
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from pymongo import UpdateOne

from systems.logs import Log
from systems.single import ScriptSingle
from systems.sys import home
from database import get_db


TENCENT_MAX = 320
TODAY_FLAG = " 15:00"
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
RATE_LIMIT_PER_SEC = 5
SINA_BATCH_SIZE = 80
GAP_THRESHOLD_DAYS = 3

# all_stock.csv 是「全市场」清单，指数(如 sh.000001)、可转债(sz.12xxxx)混在里面，
# 所以按号段白名单挑出真正有日线的品种：股票 + 场内基金。
# 基金号段与 scripts/build_kline_features.py 的 FUND_PREFIXES 保持一致（ETF/LOF/REITs）。
STOCK_FIRST = {"sh": "6", "sz": "03", "bj": "48"}
FUND_TWO = ("15", "16", "18", "50", "51", "52", "53", "55", "56", "58")


def in_universe(market: str, pure: str) -> bool:
    """该代码是否该抓日线。market 是 all_stock.csv 的 sh/sz/bj 前缀，pure 是纯数字代码。"""
    if not pure.isdigit():
        return False
    if pure[0] in STOCK_FIRST.get(market, ""):
        return True
    return market in ("sh", "sz") and pure[:2] in FUND_TWO


SHARES_PER_LOT = 100.0          # 一手的股数：sina 全按「股」给，腾讯只有科创板按「股」给
VOLUME_UNIT = "lot"             # 入库口径标记，让单位折算的补数脚本可以被反复执行
STAR_PREFIXES = ("688", "689")  # 689 是 CDR（九号公司），腾讯和 688 一样给「股」


def _tencent_volume(code: str, raw: float) -> int:
    """腾讯日线的成交量单位按板块变：688/689 科创板是「股」，其余是「手」。

    2026-10-15 用盘后快照对过：`成交额/(volume×price)` 主板/创业/ETF 都是 100（手），
    68x 字头是 1.01（股）。不按号段折算的话，同一根 K 线换个源就差 100 倍，
    量能指标（vol_ratio）会全是锯齿。
    """
    return int(round(raw / SHARES_PER_LOT)) if code.startswith(STAR_PREFIXES) else int(raw)


def _parse_api_response(code: str, bars: List, source: str = "tencent") -> List[Dict[str, Any]]:
    records = []
    for bar in bars:
        if not isinstance(bar, (list, tuple)) or len(bar) < 6:
            continue
        date_str = str(bar[0]).strip()
        if not date_str:
            continue
        try:
            o, c, h, l = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
            v = _tencent_volume(code, float(bar[5])) if bar[5] else 0
        except (ValueError, TypeError):
            continue
        records.append({
            "code": code,
            "date": f"{date_str}{TODAY_FLAG}",
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            # 这个 K 线接口只给 6 列（开收高低量），没有成交额，只能留 null。
            # 写 0.0 会被下游当成「真·零成交」，让 risk/context.py 的 amount>0 判断失去意义；
            # null 会落成 NaN，那里会正常退回 volume×close 估算。当日精确值由
            # scripts/merge_snapshot_amount.py 从交易所盘后快照回写（兄弟接口
            # newfqkline 的第 9 列有成交额，但只到万元两位小数，见 backfill_kline_amount.py）。
            "amount": None,
            "frequency": 9,
            "adjust": "raw",
            "source": source,
            "vol_unit": VOLUME_UNIT,
            "crawl_time": datetime.now().isoformat(),
        })
    return records


def _tencent_kline(code: str, count: int = TENCENT_MAX, retries: int = MAX_RETRIES) -> Optional[List[Dict]]:
    market = "bj" if code.startswith("8") else ("sh" if code.startswith(("6", "5")) else "sz")
    url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{market}{code},day,,,{count}"}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            d = r.json()
            if not (d.get("data")):
                return []
            data = list(d["data"].values())[0]
            # 只认裸价那一组：静默回落 qfqday 会让同一天的两根 K 线来自两个复权基准（§48.1）
            bars = data.get("day")
            if not bars or not isinstance(bars, list):
                return []
            records = _parse_api_response(code, bars, source="tencent")
            if attempt > 1:
                logging.info(f"tencent {code}: retry {attempt} succeeded, {len(records)} bars")
            return records
        except Exception as e:
            last_exc = e
            if attempt < retries:
                backoff = RETRY_BACKOFF * (2 ** (attempt - 1))
                logging.warning(f"tencent {code}: attempt {attempt}/{retries} failed ({e}), retry in {backoff:.1f}s")
                time.sleep(backoff)
    logging.error(f"tencent {code}: all {retries} attempts failed, last error: {last_exc}")
    return None


def _sina_kline(code: str, count: int = 200) -> Optional[List[Dict]]:
    market_code = f"sh{code}" if code.startswith(("6", "5")) else f"sz{code}"
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": market_code, "scale": "240", "ma": "no", "datalen": count}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        raw = r.json()
        if not isinstance(raw, list) or not raw:
            logging.debug(f"sina {code}: empty response")
            return []
        records = []
        for bar in raw:
            try:
                date_str = str(bar.get("day", "")).strip()
                if not date_str:
                    continue
                o, c, h, l = float(bar["open"]), float(bar["close"]), float(bar["high"]), float(bar["low"])
                # 折算成手再入库：备源和主源单位不一致时，同一根 K 线的量能会凭空差两个数量级
                v = int(round(float(bar["volume"]) / SHARES_PER_LOT)) if bar.get("volume") and bar["volume"] != "0" else 0
            except (ValueError, TypeError, KeyError):
                continue
            records.append({
                "code": code,
                "date": f"{date_str}{TODAY_FLAG}",
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "amount": None,      # sina 日线接口无成交额，同 _parse_api_response
                "frequency": 9,
                "adjust": "raw",
                "source": "sina",
                "vol_unit": VOLUME_UNIT,
                "crawl_time": datetime.now().isoformat(),
            })
        return records
    except Exception as e:
        logging.warning(f"sina {code}: HTTP error: {e}")
        return None


def _multi_source_kline(code: str, count: int = TENCENT_MAX) -> Optional[List[Dict]]:
    primary = _tencent_kline(code, count=count)
    if primary is not None:
        return primary
    logging.warning(f"tencent {code}: failed, falling back to sina")
    return _sina_kline(code, count=count)


class StockKlineScraper:
    def __init__(self, rate_limit: float = RATE_LIMIT_PER_SEC):
        self.storage = get_db()["stock_kline"]
        self._rate_limit = rate_limit
        self._semaphore = None

    @staticmethod
    def _cutoff_date() -> str:
        now = datetime.now()
        if now.hour < 15 or (now.hour == 15 and now.minute == 0):
            cutoff = now - timedelta(days=1)
        else:
            cutoff = now
        return cutoff.strftime("%Y-%m-%d")

    def _get_latest_bar_time(self, code: str, frequency: int) -> Optional[str]:
        try:
            doc = self.storage.find_one(
                {"code": code, "frequency": frequency},
                sort=[("date", -1)],
            )
            return doc.get("date") if doc else None
        except Exception as e:
            logging.warning(f"Failed to get latest bar time for {code}: {e}")
            return None

    def _get_all_stock_codes(self) -> List[str]:
        codes = set()
        path = os.path.join(home(), "config", "all_stock.csv")
        try:
            df = pd.read_csv(path)
            for _, row in df.iterrows():
                code = str(row.get("code", "")).strip()
                market, _, pure = code.partition(".")
                if in_universe(market, pure):
                    codes.add(pure)
            logging.info(f"Loaded {len(codes)} codes (股票+场内基金) from all_stock.csv")
        except Exception as e:
            logging.error(f"Failed to load all_stock.csv: {e}")
            raise
        return sorted(list(codes))

    def _check_data_gaps(self, code: str):
        try:
            stored = list(self.storage.find(
                {"code": code, "frequency": 9},
                {"date": 1},
                sort=[("date", 1)]
            ))
            if not stored or len(stored) < 2:
                return
            dates = [doc["date"][:10] for doc in stored]
            for i in range(1, len(dates)):
                d1 = datetime.strptime(dates[i - 1], "%Y-%m-%d")
                d2 = datetime.strptime(dates[i], "%Y-%m-%d")
                gap = (d2 - d1).days
                if gap > GAP_THRESHOLD_DAYS:
                    logging.warning(f"data gap for {code}: {dates[i - 1]} → {dates[i]} ({gap} days)")
        except Exception as e:
            logging.debug(f"gap check skipped for {code}: {e}")

    def _fetch_kline(self, code: str) -> List[Dict[str, Any]]:
        if self._semaphore:
            self._semaphore.acquire()
        try:
            records = _multi_source_kline(code)
            if records is None:
                return None

            cutoff = self._cutoff_date()
            records = [r for r in records if r["date"][:10] <= cutoff]

            latest = self._get_latest_bar_time(code, 9)
            if latest:
                records = [r for r in records if r["date"] > latest]

            logging.debug(f"multi_source {code}: {len(records)} new bars (cutoff={cutoff})")
            if len(records) > 1:
                code_obj = code
                dates = [r["date"][:10] for r in records]
                for j in range(1, len(dates)):
                    gap = (datetime.strptime(dates[j], "%Y-%m-%d") -
                           datetime.strptime(dates[j - 1], "%Y-%m-%d")).days
                    if gap > GAP_THRESHOLD_DAYS:
                        logging.warning(f"fetch gap for {code}: {dates[j - 1]} → {dates[j]} ({gap} days)")
            return records
        finally:
            if self._semaphore:
                self._semaphore.release()

    def save_klines(self, records: List[Dict[str, Any]]):
        if not records:
            return
        try:
            operations = [
                UpdateOne(
                    {"code": r["code"], "date": r["date"], "frequency": r["frequency"]},
                    {"$set": r},
                    upsert=True,
                )
                for r in records
            ]
            result = self.storage.bulk_write(operations, ordered=False)
            saved_count = result.upserted_count + result.modified_count
            if saved_count:
                logging.debug(f"Saved {saved_count}/{len(records)} kline records")
        except Exception as e:
            logging.error(f"Failed to save {len(records)} klines: {e}")

    def fetch_stock(self, code: str) -> Optional[List[Dict[str, Any]]]:
        pure = code.split(".")[-1] if "." in code else code
        records = _multi_source_kline(pure)
        if records is None:
            logging.warning(f"fetch_stock {code}: no data from any source")
            return None
        cutoff = self._cutoff_date()
        return [r for r in records if r["date"][:10] <= cutoff]

    def batch_update(self, codes: List[str] = None) -> int:
        if codes is None:
            codes = self._get_all_stock_codes()
        codes = [c.split(".")[-1] if "." in c else c for c in codes]
        total = len(codes)
        workers = 10
        self._semaphore = Semaphore(self._rate_limit)
        results = {"success": 0, "skipped": 0, "failed": 0}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._fetch_kline, code): code for code in codes}
            pending = []
            pending_lock = Lock()
            for i, future in enumerate(as_completed(futures)):
                code = futures[future]
                try:
                    records = future.result()
                    if records is None:
                        results["failed"] += 1
                    elif not records:
                        results["skipped"] += 1
                    else:
                        with pending_lock:
                            pending.extend(records)
                        results["success"] += 1
                except Exception as e:
                    logging.error(f"Error processing {code}: {e}")
                    results["failed"] += 1

                if len(pending) >= 2000:
                    with pending_lock:
                        self.save_klines(pending)
                        pending = []

                if (i + 1) % 500 == 0:
                    logging.info(
                        f"Progress: {i + 1}/{total}, "
                        f'success={results["success"]}, skipped={results["skipped"]}, failed={results["failed"]}'
                    )

        with pending_lock:
            if pending:
                self.save_klines(pending)

        logging.info(
            f"batch_update completed: total={total}, "
            f'success={results["success"]}, skipped={results["skipped"]}, failed={results["failed"]}'
        )
        return results["success"]

    def fetch_daily_klines(self, codes: List[str] = None):
        codes = codes or self._get_all_stock_codes()
        codes = [c.split(".")[-1] if "." in c else c for c in codes]
        total = len(codes)
        workers = 10
        self._semaphore = Semaphore(self._rate_limit)
        results = {"success": 0, "skipped": 0, "failed": 0}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._fetch_kline, code): code for code in codes}
            pending = []
            pending_lock = Lock()
            for i, future in enumerate(as_completed(futures)):
                code = futures[future]
                try:
                    records = future.result()
                    if records is None:
                        results["failed"] += 1
                    elif not records:
                        results["skipped"] += 1
                    else:
                        with pending_lock:
                            pending.extend(records)
                        results["success"] += 1
                except Exception as e:
                    logging.error(f"Error processing {code}: {e}")
                    results["failed"] += 1

                if len(pending) >= 2000:
                    with pending_lock:
                        self.save_klines(pending)
                        pending = []

                if (i + 1) % 500 == 0:
                    logging.info(
                        f"Progress: {i + 1}/{total}, "
                        f'success={results["success"]}, skipped={results["skipped"]}, failed={results["failed"]}'
                    )

        with pending_lock:
            if pending:
                self.save_klines(pending)

        logging.info(
            f"Daily kline fetch completed: total={total}, "
            f'success={results["success"]}, skipped={results["skipped"]}, failed={results["failed"]}'
        )
        return results


if __name__ == "__main__":
    Log("kline_spider", log_type=Log.TYPE_FILE, level=logging.INFO)
    pid_file = os.path.join(home(), "var", "run", "kline_spider.pid")
    single = ScriptSingle(pid_file)

    if single.is_running():
        logging.error("there is script lock {}".format(pid_file))
        sys.exit(0)
    spider = StockKlineScraper()
    klines = spider.fetch_daily_klines()
