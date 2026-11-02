import sys
import os
import argparse
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

# 腾讯日线的两个入口，同一个数据的两种域名：
# - 主源 ifzq.gtimg.cn 会被 WAF 整域挡（HTTP 501 + stgw，见 `_is_waf_page`）；
# - 镜像 proxy.finance.qq.com 的 newfqkline 给 10 列，下标 0-5 与主源同构，
#   下标 8 是成交额（万元，见 backfill_kline_amount.py 的核对）。
# 用「域名 + 取哪一列」成对描述，好让同一个解析函数吃两种响应。
TENCENT_ENDPOINTS = (
    ("ifzq", "https://ifzq.gtimg.cn/appstock/app/fqkline/get"),
    ("proxy", "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"),
)
# 撞过 WAF 的端点名。一次运行内熔断，避免 6922 只每只都去撞一次墙、每只白等 6 秒。
_TENCENT_DEAD: set = set()

# 复权口径写死裸价（§48.1）：静默回落 qfqday 会让同一天的两根 K 线来自两个复权基准，
# 因子表里 600272 那批错标就是这么来的。requests 一改口径，`adjust` 字段和入库的
# `adjust` 标签都要跟着变，所以做成常量而不是散落的字面量。
TENCENT_ADJUST = "raw"


def _tencent_token(market: str, code: str, count: int, adjust: Optional[str] = None) -> str:
    """拼腾讯 K 线的 `param`。第 6 个字段不能省，省了回 `bad params`。

    实测（2026-11-02）三种写法：
      `sh600519,day,,,320`     → {"code":1,"msg":"bad params"}，只有 version
      `sh600519,day,,,320,`    → code=0，读 `day`（裸价）
      `sh600519,day,,,320,qfq` → code=0，读 `qfqday`（前复权）
    注意 qfq 与裸价是同一天两个数（600519 在 2025-07-08：qfq=1358.73 / raw=1410.71），
    所以口径必须显式选，不能两个都试。

    `adjust` 默认取 None 再回读 `TENCENT_ADJUST`，而不是写成 `adjust=TENCENT_ADJUST`：
    默认参数在 **def 那一刻**就绑死了，运行期改常量不会生效（2026-11-02 自己踩过）。
    """
    adjust = TENCENT_ADJUST if adjust is None else adjust
    tail = "qfq" if adjust == "qfq" else ""
    return f"{market}{code},day,,,{count},{tail}"


# 当天落库只数的下限：2026-11-02 那轮 6,922 只里只落了 595 只（8.6%），一路静默到 17:16
# 才由面板那层的覆盖率告警喊出来，榜上已经只剩 2 只了。采集这层自己必须会喊。
DAY_BAR_MIN_RATIO = 0.9


def day_bar_problem(day: str, fetched: int, with_bar: int,
                    min_ratio: float = DAY_BAR_MIN_RATIO) -> Optional[Dict[str, Any]]:
    """当天真落库的只数明显少于应抓只数 ⇒ 给出读数；否则 None。

    `with_bar == 0` 不算问题：周末/休市那天全场都不该有柱子，报出来就是天天刷屏。
    """
    if fetched <= 0 or with_bar <= 0:
        return None
    ratio = with_bar / fetched
    if ratio >= min_ratio:
        return None
    return {"day": day, "with_bar": with_bar, "fetched": fetched,
            "ratio": round(ratio, 3), "min_ratio": min_ratio}


def codes_with_bar(col, day: str) -> int:
    """库里当天有日线的只数（读库，不读循环计数）。

    循环里的 `success` 数的是「这次带回了新柱子」的票：重跑/补跑时全场都是 0，
    而榜单依赖的是「库里有那天的行」——两者不能混。
    """
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    pipe = [{"$match": {"date": {"$gte": day, "$lt": nxt}, "frequency": 9}},
            {"$group": {"_id": None, "n": {"$sum": 1}}}]
    for row in col.aggregate(pipe, allowDiskUse=True):
        return int(row.get("n") or 0)
    return 0


def alert_day_bar_problem(problem: Optional[Dict[str, Any]]) -> bool:
    """把当天的采集缺口推钉钉（和 qlib_update_bin 同一个机器人）。返回 True = 真发了。"""
    if not problem:
        return False
    text = (f"{problem['day']} 日线落库 {problem['with_bar']}/{problem['fetched']} 只"
            f"（{problem['ratio'] * 100:.1f}%，下限 {problem['min_ratio'] * 100:.0f}%）\n"
            "主源（腾讯 ifzq.gtimg.cn）当天多半回的是 200 空 body，日志里看是 skipped 不是 failed；"
            "备源 sina 没有成交额列，当日精确值还要等 merge_snapshot_amount 从盘后快照并回。\n"
            "后果：17:15 的面板增量、17:30 的选股名单都只看得见这一小片市场，"
            "别把今天的名单当全市场结论用。")
    try:
        import alert_pusher

        name, url, secret = alert_pusher.resolve_webhook(alert_pusher.DINGTALK, "ding_ops")
        if alert_pusher.send(alert_pusher.DINGTALK, url, secret, "K线当日落库缺口", text):
            logging.info("已推钉钉（%s）：当日落库缺口", name)
            return True
        logging.warning("钉钉返回失败，缺口只在日志里")
    except Exception as exc:  # noqa: BLE001 - 告警发不出去也不能把爬虫带崩
        logging.warning("钉钉告警发不出去：%s", exc)
    return False


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


def _is_waf_page(r) -> bool:
    """腾讯 WAF 拦截页：HTTP 501 + `Server: stgw` + 一段跳 `waf.tencent.com/501page.html` 的 HTML。

    2026-11-02 22:4x 实测：`ifzq.gtimg.cn` 整域被挡（含实时行情路径），603/688/605/601
    号段 72 只全 501，连 22:36 还成功过的 600519 也 501；而 `proxy.finance.qq.com` 与
    `qt.gtimg.cn` 同时都还是 200——按域名/路径的封，不是封 IP。
    """
    if getattr(r, "status_code", None) == 501:
        return True
    try:
        return "waf.tencent.com" in (r.text or "")
    except Exception:  # noqa: BLE001 - 读不出 body 就当不是 WAF 页
        return False


def _tencent_kline(code: str, count: int = TENCENT_MAX, retries: int = MAX_RETRIES) -> Optional[List[Dict]]:
    """主源 + 镜像轮询。返回 None = 所有腾讯端点都拿不到（该让 sina 上场）。

    主源 `ifzq.gtimg.cn` 会被 WAF 整域挡（见 `_is_waf_page`），镜像 `proxy.finance.qq.com`
    是同一个数据（`backfill_kline_amount.py` 已核实下标 0-5 与主源同构）。所以：
    - 某端点一旦回 WAF 页，**立刻**换下一个、不重试（WAF 不是抖动，重试只是白等；
      旧代码每只白等 2s+4s，6922 只就是 11 小时）；
    - 撞过 WAF 的端点记进 `_TENCENT_DEAD` 熔断，后续 code 直接跳过，不再每只都再撞一遍。
    """
    market = "bj" if code.startswith("8") else ("sh" if code.startswith(("6", "5")) else "sz")
    token = _tencent_token(market, code, count)
    # 和第 6 字段一一对应：要 qfq 就读 qfqday，要裸价就读 day，不互相兜底。
    field = "qfqday" if TENCENT_ADJUST == "qfq" else "day"
    live = [ep for ep in TENCENT_ENDPOINTS if ep[0] not in _TENCENT_DEAD]
    if not live:  # 全被判死过：给一次复活机会，别真就一直不用了
        _TENCENT_DEAD.clear()
        live = list(TENCENT_ENDPOINTS)

    last_exc = None
    for name, url in live:
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(url, params={"param": token}, timeout=10)
                if _is_waf_page(r):
                    logging.error(f"tencent[{name}] {code}: WAF 拦截页"
                                  f"（HTTP {getattr(r, 'status_code', '?')}），换下一个端点，不重试")
                    _TENCENT_DEAD.add(name)
                    break
                r.raise_for_status()
                d = r.json()
                # code!=0 是「请求本身不合规」（bad params 等），不是「这只票今天没柱子」。
                # 不单独认出来的话，它会掉进下面的空 payload 分支，日志只剩一句
                # 「200 but no day series」——2026-11-02 那次就是靠猜才找到根因的。
                if d.get("code") not in (0, None):
                    logging.error(f"tencent[{name}] {code}: api error code={d.get('code')} "
                                  f"msg={d.get('msg')!r} param={token}")
                    break
                data = list((d.get("data") or {}).values())
                data = data[0] if data and isinstance(data[0], dict) else {}
                # 只认裸价那一组：静默回落 qfqday 会让同一天的两根 K 线来自两个复权基准（§48.1）
                bars = data.get(field)
                if not isinstance(bars, list) or not bars:
                    # 2026-11-02 实测：源回 HTTP 200，body 里只有 {"version": ...}。
                    # 空 payload 是「这个源今天没给数据」，不是「这根 K 线不存在」——
                    # 当成成功会让备源永远不上场（那轮 6922 只里 6304 只静默记成 skipped）。
                    logging.warning(f"tencent[{name}] {code}: 200 but no `{field}` series "
                                    f"(keys={sorted(data.keys())[:4]}), treating as source failure")
                    break
                records = _parse_api_response(code, bars, source="tencent")
                if not records:
                    logging.warning(f"tencent[{name}] {code}: `{field}` series had no parsable bar")
                    break
                if attempt > 1:
                    logging.info(f"tencent[{name}] {code}: retry {attempt} succeeded, {len(records)} bars")
                return records
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    backoff = RETRY_BACKOFF * (2 ** (attempt - 1))
                    logging.warning(f"tencent[{name}] {code}: attempt {attempt}/{retries} failed ({e}), "
                                    f"retry in {backoff:.1f}s")
                    time.sleep(backoff)
    logging.error(f"tencent {code}: 所有端点都没拿到数据，最后错误: {last_exc}")
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
        # total 一起带出去：__main__ 的当日覆盖率自检要分母，别让它再读一遍 all_stock.csv
        results = {"success": 0, "skipped": 0, "failed": 0, "total": total}

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
    parser = argparse.ArgumentParser(description="全市场日线增量落库 + 当日覆盖率自检")
    parser.add_argument("--no-alert", dest="alert", action="store_false",
                        help="只写日志，不推钉钉（手动试跑用；定时任务默认要推）")
    args = parser.parse_args()
    spider = StockKlineScraper()
    stats = spider.fetch_daily_klines()
    today = datetime.now().strftime("%Y-%m-%d")
    problem = day_bar_problem(today, stats.get("total", 0), codes_with_bar(spider.storage, today))
    if problem:
        logging.warning("当日落库缺口：%s/%s 只有当天的柱子（%.1f%%，下限 %.0f%%）",
                        problem["with_bar"], problem["fetched"],
                        problem["ratio"] * 100, problem["min_ratio"] * 100)
        if args.alert:
            alert_day_bar_problem(problem)
