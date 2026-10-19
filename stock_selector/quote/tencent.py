"""quote/tencent.py — 腾讯实时快照解析（涨跌停价与流动性的唯一口径来源）。

接口：GET https://qt.gtimg.cn/q=sh600000,sz000001  （GBK 编码，`~` 分隔，实测 88 段）
批量：200 只一次请求约 63ms，未见截断；本模块按 60 只分片。

为什么必须用这个接口而不是自己算阈值：字段 47/48 是**交易所下发的涨停价/跌停价**，
ST、退市整理期、北交所、ETF 各有各的规则，按「主板 10% / 创业 20%」硬推一定会算错。
2026-10-12 实测：创业板 300750 昨收 297.75 → 357.30/238.20（±20%）、科创板 688981
108.42 → 130.10/86.74（±20%）、北交所 920002 50.00 → 65.00/35.00（±30%）、
ETF 510300 4.385 → 4.824/3.947（±10%）。退市/停牌（000004 国华退、600001 邯郸钢铁）
两个字段给 **-1**，是「不可得」哨兵而不是价格，必须丢弃。

单位约定：amount 是**元**；volume 是**手**，**但科创板 688/689 是「股」**（腾讯对这两个号段
给股，和日线 kline 接口一样）。落盘的 data/market_snapshot/*.parquet 沿用这个口径没做事后
折算，所以别把快照的 volume 当统一单位用；流动性判据走 amount，不受影响。
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

URL = "https://qt.gtimg.cn/q="
BATCH = 60
UA = "Mozilla/5.0"

# 记录内字段下标（0 基，按 `~` 切分后）
F_NAME, F_CODE, F_PRICE, F_PREV_CLOSE, F_OPEN = 1, 2, 3, 4, 5
F_VOLUME, F_OUTER, F_INNER = 6, 7, 8
F_BID_START, F_ASK_START, BOOK_LEVELS = 9, 19, 5
F_TIME, F_CHANGE, F_CHANGE_PCT, F_HIGH, F_LOW = 30, 31, 32, 33, 34
F_TRIPLES, F_AMOUNT_WAN, F_TURNOVER, F_PE = 35, 37, 38, 39
F_AMPLITUDE, F_FLOAT_MV, F_TOTAL_MV, F_PB = 43, 44, 45, 46
F_LIMIT_UP, F_LIMIT_DOWN, F_VOL_RATIO, F_OD_DIFF, F_AVG_PRICE = 47, 48, 49, 50, 51
_MIN_FIELDS = 52


def symbol(code: str) -> str:
    """六位代码 → 腾讯带市场前缀的代码。

    920 是北交所新代码段，必须排在「9 开头归沪」之前判，否则会被当成上海 B 股。
    """
    code = str(code).strip()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if code.startswith(("92", "8", "4")):
        return f"bj{code}"
    if code.startswith(("5", "6", "9", "7")):
        return f"sh{code}"
    if code.startswith(("0", "1", "2", "3")):
        return f"sz{code}"
    return f"bj{code}"


def _num(fields: list[str], idx: int) -> float | None:
    """越界/空串/非数一律 None，不猜 0 —— 0 在行情里是合法值。"""
    try:
        raw = fields[idx].strip()
    except IndexError:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _levels(fields: list[str], start: int) -> list[tuple[float, int]]:
    """五档：(价, 量[手])，从买一/卖一由近及远。缺档跳过而不是补 (0,0)。"""
    out = []
    for i in range(BOOK_LEVELS):
        price = _num(fields, start + i * 2)
        vol = _num(fields, start + i * 2 + 1)
        if price and vol and price > 0 and vol > 0:
            out.append((price, int(vol)))
    return out


def _limit(fields: list[str], idx: int) -> float | None:
    """涨跌停价：-1 是「无此数据」（退市/停牌/未开盘），不能当价格参与比较。"""
    value = _num(fields, idx)
    return value if value and value > 0 else None


def _amount(fields: list[str]) -> float | None:
    """成交额（元）。字段 35 是 `价/量/额` 三段，末段精确到元；退到字段 37（万元）。"""
    triple = None
    try:
        triple = fields[F_TRIPLES]
    except IndexError:
        pass
    if triple and "/" in triple:
        try:
            return float(triple.split("/")[-1])
        except ValueError:
            pass
    wan = _num(fields, F_AMOUNT_WAN)
    return wan * 1e4 if wan is not None else None


def parse_name(line: str) -> tuple[str, str] | None:
    """只取 (六位代码, 现名)，不管价格是否有效。

    为什么不能复用 parse()：它在 price 缺失/≤0 时返回 None，而停牌、退市整理期的票恰恰
    就是 price 无效、名字最要紧的那一批（「退」字只在现名里看得到）。
    """
    body = line.split('="', 1)[1] if '="' in line else line
    fields = body.rstrip('";\r\n ').split("~")
    if len(fields) < 3:
        return None
    code, name = fields[F_CODE].strip(), fields[F_NAME].strip()
    return (code, name) if code and name else None


def fetch_names(codes: list[str], timeout: float = 8.0, retries: int = 2) -> dict[str, str]:
    """批量取现名，返回 {六位代码: 名称}；取不到的代码不出现在结果里，由调用方决定兜底。

    只读接口，一次 60 只；名字表过期时（本仓 data/csv/all_stock.csv 是 2026-05-24 的快照）
    ST/*ST/退 的判定只能靠这里，否则「剔 ST」这道筛事实上什么都没筛。
    """
    syms = [symbol(c) for c in codes if str(c).strip()]
    out: dict[str, str] = {}
    for start in range(0, len(syms), BATCH):
        chunk = syms[start:start + BATCH]
        for attempt in range(retries + 1):
            try:
                for line in _request(chunk, timeout).splitlines():
                    if line.strip() and (hit := parse_name(line)):
                        out[hit[0]] = hit[1]
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt >= retries:
                    logger.warning("腾讯现名分片失败（%d 只，已重试 %d 次）: %s",
                                   len(chunk), attempt, exc)
    return out


def parse(line: str) -> dict | None:
    """解析一条 `v_sh600000="1~浦发银行~..."` 记录；结构不符返回 None。"""
    body = line.split('="', 1)[1] if '="' in line else line
    fields = body.rstrip('";\r\n ').split("~")
    if len(fields) < _MIN_FIELDS:
        return None
    code = fields[F_CODE].strip()
    price = _num(fields, F_PRICE)
    if not code or not price or price <= 0:
        return None
    return {
        "code": code,
        "name": fields[F_NAME].strip(),
        "price": price,
        "prev_close": _num(fields, F_PREV_CLOSE),
        "open": _num(fields, F_OPEN),
        "high": _num(fields, F_HIGH),
        "low": _num(fields, F_LOW),
        "volume": _num(fields, F_VOLUME),           # 手
        "amount": _amount(fields),                  # 元
        "outer_volume": _num(fields, F_OUTER),      # 外盘 = 主动买
        "inner_volume": _num(fields, F_INNER),      # 内盘 = 主动卖
        "bids": _levels(fields, F_BID_START),
        "asks": _levels(fields, F_ASK_START),
        "quote_time": fields[F_TIME].strip() if len(fields) > F_TIME else "",
        "pct": _num(fields, F_CHANGE_PCT),          # 百分数
        "turnover_pct": _num(fields, F_TURNOVER),   # 换手率，百分数
        "vol_ratio": _num(fields, F_VOL_RATIO),     # 量比
        "float_mv": _num(fields, F_FLOAT_MV),       # 流通市值，亿元
        "total_mv": _num(fields, F_TOTAL_MV),
        "limit_up": _limit(fields, F_LIMIT_UP),
        "limit_down": _limit(fields, F_LIMIT_DOWN),
        "avg_price": _num(fields, F_AVG_PRICE),
    }


def _request(syms: list[str], timeout: float) -> str:
    req = urllib.request.Request(URL + ",".join(syms), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", "replace")


def fetch(codes: list[str], timeout: float = 6.0, retries: int = 2) -> dict[str, dict]:
    """批量取快照，返回 {六位代码: 快照}。

    拿不到的代码**不出现在结果里**（调用方据此判「行情缺失」），也不返回半成品快照；
    整批失败重试耗尽后返回空字典，由调用方决定降级还是报错——这里不静默补数。
    """
    syms = [symbol(c) for c in codes if str(c).strip()]
    out: dict[str, dict] = {}
    for start in range(0, len(syms), BATCH):
        chunk = syms[start:start + BATCH]
        for attempt in range(retries + 1):
            try:
                body = _request(chunk, timeout)
                for line in body.splitlines():
                    if not line.strip():
                        continue
                    item = parse(line)
                    if item:
                        out[item["code"]] = item
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt >= retries:
                    logger.warning("腾讯快照分片失败（%d 只，已重试 %d 次）: %s",
                                   len(chunk), attempt, exc)
                else:
                    logger.info("腾讯快照重试 %d/%d: %s", attempt + 1, retries, exc)
    missing = len(syms) - len(out)
    if missing:
        logger.info("快照缺失 %d/%d 只（退市/停牌/代码无效均在此列）", missing, len(syms))
    return out
