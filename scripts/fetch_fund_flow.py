"""抓取三类资金流数据，按数据集分区写入 parquet，支持增量(按日)更新。

三个数据源（来自 akshare）：
  1. 个股资金流向历史日线：东方财富接口 stock_individual_fund_flow。
     东财反爬严重时不可用，直接走东财 HTTP 接口并指数退避重试，网络恢复即自动补齐。
  2. 个股资金流向排行快照：默认同花顺 stock_fund_flow_individual（稳定）；
     可用 --source em 切东方财富 stock_individual_fund_flow_rank。
  3. 行业资金流向：同花顺 stock_fund_flow_industry（即时/3日/5日/10日/20日 快照）。

数据模型：三类数据各自是一个 DataFrame，底层字段见各 fetch 函数。
rank 和 industry 是"某日采集时刻的快照"，带采集日期 collect_date；
individual 是"个股×日"的时间序列，以 trade_date 为主键。

存储布局：
    data/fund_flow/
        individual/{code}.parquet         # 每只股票一个文件，支持增量
        rank/YYYY-MM-DD.parquet          # 每日一个快照文件
        industry/YYYY-MM-DD.parquet      # 每日一个快照文件

用法:
    python scripts/fetch_fund_flow.py                      # 只抓今日的三类数据
    python scripts/fetch_fund_flow.py --backfill 30        # 回补最近30个自然日
    python scripts/fetch_fund_flow.py --only individual     # 只抓个股资金流历史
    python scripts/fetch_fund_flow.py --stock 600094        # 只处理指定股票
    python scripts/fetch_fund_flow.py --source em           # 排行改走东方财富
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "fund_flow"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("fetch_fund_flow")

# 网络/数据源访问参数
MAX_RETRIES = 5          # 每个请求最大重试次数
RETRY_BACKOFF = 2.0      # 重试基础等待秒数(指数退避)
REQUEST_TIMEOUT = 20     # 单次请求超时

# 排名快照支持的同花顺 symbol（默认源）；东方财富作备用
THS_RANK_SYMBOLS = ["即时", "3日排行", "5日排行", "10日排行", "20日排行"]
RANK_EM_INDICATORS = ["今日", "3日", "5日", "10日"]

# 行业快照支持的 symbol
INDUSTRY_SYMBOLS = ["即时", "3日排行", "5日排行", "10日排行", "20日排行"]


# ---------------------------------------------------------------------------
# 数据源：个股资金流向历史日线（东方财富）stock_individual_fund_flow
# ---------------------------------------------------------------------------
INDIVIDUAL_SCHEMA = [
    "code", "market", "name", "date",
    "close", "pct_change",
    "main_net_inflow", "main_net_inflow_pct",       # 主力净值/净占比
    "xlarge_net_inflow", "xlarge_net_inflow_pct",   # 超大单
    "large_net_inflow", "large_net_inflow_pct",     # 大单
    "medium_net_inflow", "medium_net_inflow_pct",   # 中单
    "small_net_inflow", "small_net_inflow_pct",     # 小单
]

# 市代码 -> 市场名；北交所代码以 4/8 开头（secid 前缀同深市=0）
_MARKET_OF_CODE = {"6": "sh", "5": "sh", "9": "sh", "0": "sz", "3": "sz", "4": "bj", "8": "bj"}


def detect_market(code: str) -> str:
    """根据股票代码首字符推断所属市场（供 stock_individual_fund_flow 的 market 参数）。"""
    return _MARKET_OF_CODE.get(code[0], "sz")


def _market_secid(code: str) -> int:
    """股票代码 -> 东财 secid 前缀（沪=1，深/北=0）。"""
    return 1 if code[0] in ("6", "5", "9") else 0


_EASTMONEY_HIST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
}

_EASTMONEY_HIST_KLINES_COLS = [
    "date",
    "main_net_inflow",
    "small_net_inflow",
    "medium_net_inflow",
    "large_net_inflow",
    "xlarge_net_inflow",
    "main_net_inflow_pct",
    "small_net_inflow_pct",
    "medium_net_inflow_pct",
    "large_net_inflow_pct",
    "xlarge_net_inflow_pct",
    "close",
    "pct_change",
]


def _fetch_individual_raw(code: str) -> pd.DataFrame:
    """直接调东方财富日线资金流接口，返回含股票名的明细 DataFrame。

    相比 akshare 的 stock_individual_fund_flow，此处额外带回股票名称，
    且避开 akshare 自带 UA 容易被断连的问题。
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "lmt": "0",
        "klt": "101",
        "secid": f"{_market_secid(code)}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(time.time() * 1000),
    }
    r = requests.get(url, params=params, headers=_EASTMONEY_HIST_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or {}
    name = data.get("name")
    klines = data.get("klines") or []
    if not klines:
        return pd.DataFrame()

    rows = [row.split(",") for row in klines]
    df = pd.DataFrame(
        [row[: len(_EASTMONEY_HIST_KLINES_COLS)] for row in rows],
        columns=_EASTMONEY_HIST_KLINES_COLS,
    )
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df.insert(0, "name", name)
    return df


def fetch_individual(code: str) -> pd.DataFrame:
    """拉取单只股票的个股资金流向历史日线，标准化为 INDIVIDUAL_SCHEMA。"""
    raw = _call_with_retry(_fetch_individual_raw, code=code)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=INDIVIDUAL_SCHEMA)

    df = pd.DataFrame(index=raw.index)
    df["code"] = code
    df["market"] = detect_market(code)
    df["name"] = raw["name"]
    for col in [
        "date", "close", "pct_change",
        "main_net_inflow", "main_net_inflow_pct",
        "xlarge_net_inflow", "xlarge_net_inflow_pct",
        "large_net_inflow", "large_net_inflow_pct",
        "medium_net_inflow", "medium_net_inflow_pct",
        "small_net_inflow", "small_net_inflow_pct",
    ]:
        df[col] = raw[col] if col in raw.columns else None
    return df[INDIVIDUAL_SCHEMA].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 数据源：个股资金流向排行快照
#   source=ths: 同花顺 stock_fund_flow_individual（默认，名单：全市场个股）
#   source=em : 东方财富 stock_individual_fund_flow_rank（东财反爬时不可用）
# ---------------------------------------------------------------------------
RANK_SCHEMA = [
    "collect_date", "indicator", "source",
    "code", "name", "latest_price",
    "pct_change", "turnover_rate",          # 即时
    "inflow", "outflow", "net_inflow", "amount",   # 即时（元）
    "stage_pct_change", "stage_turnover", "stage_net_inflow",  # N日排行（元）
]

# 不含 collect_date 的行字段顺序
RANK_FIELDS = [
    "indicator", "source",
    "code", "name", "latest_price",
    "pct_change", "turnover_rate",
    "inflow", "outflow", "net_inflow", "amount",
    "stage_pct_change", "stage_turnover", "stage_net_inflow",
]

_THS_RANK_DAILY = {
    "3日排行": "3日",
    "5日排行": "5日",
    "10日排行": "10日",
    "20日排行": "20日",
}
_AMOUNT_UNIT = {"亿": 1e8, "万": 1e4, "元": 1.0}


def _parse_amount_series(series) -> pd.Series:
    """解析同花顺金额文本如 '5.73亿'/'7664.47万'/'-2.59亿' 为数值(元)。"""
    s = series.astype(str).str.strip()
    num = s.str.replace(r"[^0-9.\-]", "", regex=True)
    has_yi = s.str.contains("亿")
    has_wan = s.str.contains("万")
    vals = pd.to_numeric(num, errors="coerce")
    return vals.where(~has_yi, vals * 1e8).where(~has_wan, vals * 1e4)


def _parse_pct_series(series) -> pd.Series:
    """解析同花顺百分比文本如 '17.50%' 为数值(%)。"""
    return pd.to_numeric(series.astype(str).str.replace("%", "").str.strip(), errors="coerce")


def fetch_rank_ths(symbol: str) -> pd.DataFrame:
    """同花顺个股资金流快照 → 标准 RANK_SCHEMA 行（不含 collect_date）。"""
    raw = _call_with_retry(ak.stock_fund_flow_individual, symbol=symbol)
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = pd.DataFrame()
    df["code"] = raw["股票代码"].astype(str).str.zfill(6)
    df["name"] = raw["股票简称"]
    df["latest_price"] = pd.to_numeric(raw["最新价"], errors="coerce")

    if symbol == "即时":
        df["indicator"] = "今日"
        df["pct_change"] = _parse_pct_series(raw["涨跌幅"])
        df["turnover_rate"] = _parse_pct_series(raw["换手率"])
        df["inflow"] = _parse_amount_series(raw["流入资金"])
        df["outflow"] = _parse_amount_series(raw["流出资金"])
        df["net_inflow"] = _parse_amount_series(raw["净额"])
        df["amount"] = _parse_amount_series(raw["成交额"])
        for c in ("stage_pct_change", "stage_turnover", "stage_net_inflow"):
            df[c] = None
    else:
        df["indicator"] = _THS_RANK_DAILY[symbol]
        df["stage_pct_change"] = _parse_pct_series(raw["阶段涨跌幅"])
        df["stage_turnover"] = _parse_pct_series(raw["连续换手率"])
        df["stage_net_inflow"] = _parse_amount_series(raw["资金流入净额"])
        for c in ("pct_change", "turnover_rate", "inflow", "outflow", "net_inflow", "amount"):
            df[c] = None
    df["source"] = "ths"
    return df[RANK_FIELDS].reset_index(drop=True)


def fetch_rank_em(indicator: str) -> pd.DataFrame:
    """东方财富个股资金流向排行快照 → 标准 RANK_SCHEMA 行。

    东财接口 push2.eastmoney.com 常被反爬屏蔽，网络恢复时仍可用。
    """
    per = indicator.replace("日", "")  # 今日/3/5/10
    raw = _call_with_retry(ak.stock_individual_fund_flow_rank, indicator=indicator)
    if raw is None or raw.empty:
        return pd.DataFrame()

    ren = {
        "代码": "code",
        "名称": "name",
        "最新价": "latest_price",
        f"{per}涨跌幅": "pct_change",
        f"{indicator}主力净流入-净额": "net_inflow",
    }
    df = raw.rename(columns=ren)[[c for c in ren.values() if c in raw.rename(columns=ren).columns]].copy()
    for col in df.columns:
        if col not in ("code", "name"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["indicator"] = indicator
    df["source"] = "em"
    for c in ("turnover_rate", "inflow", "outflow", "amount",
              "stage_pct_change", "stage_turnover", "stage_net_inflow"):
        df[c] = None
    return df[RANK_FIELDS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 数据源：行业资金流向快照（同花顺）stock_fund_flow_industry
# ---------------------------------------------------------------------------
# 同花顺各 symbol 返回列略有差异：即时版带领涨股字段；日排行版为 阶段涨跌幅。
# 这里统一成同一组列名，缺失列自动置空。
_INDUSTRY_RENAME = {
    "序号": "rank",
    "行业": "industry",
    "行业指数": "industry_index",
    "行业-涨跌幅": "pct_change",
    "阶段涨跌幅": "pct_change",
    "流入资金": "inflow",
    "流出资金": "outflow",
    "净额": "net_inflow",
    "公司家数": "company_count",
    "领涨股": "leader_stock",
    "领涨股-涨跌幅": "leader_pct_change",
    "当前价": "leader_price",
}

INDUSTRY_SCHEMA = [
    "rank", "industry", "industry_index", "pct_change",
    "inflow", "outflow", "net_inflow", "company_count",
    "leader_stock", "leader_pct_change", "leader_price",
]


def fetch_industry(symbol: str) -> pd.DataFrame:
    """拉取行业资金流向快照并标准化。

    返回列: rank, industry, industry_index, pct_change, inflow, outflow,
            net_inflow, company_count, leader_stock, leader_pct_change, leader_price
    金额字段单位为 亿元。
    """
    raw = _call_with_retry(ak.stock_fund_flow_industry, symbol=symbol)
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.rename(columns=_INDUSTRY_RENAME)
    present = [c for c in INDUSTRY_SCHEMA if c in df.columns]
    df = df[present].copy()
    for col in df.columns:
        if col not in ("industry", "leader_stock"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in set(INDUSTRY_SCHEMA) - set(df.columns):
        df[col] = None
    df["collect_date"] = pd.Timestamp.today().normalize()
    return df[INDUSTRY_SCHEMA].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 通用：带重试的调用、parquet 读写
# ---------------------------------------------------------------------------
def _call_with_retry(func, **kwargs):
    """调用 akshare 函数，失败时指数退避重试。仍失败返回 None。"""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(**kwargs)
        except Exception as exc:  # noqa: BLE001 网络/反爬异常重试
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "[retry %d/%d] %s(%s) 失败: %s, %.0fs 后重试",
                    attempt, MAX_RETRIES, func.__name__, kwargs, exc, wait,
                )
                time.sleep(wait)
    logger.error("[FAIL] %s(%s) 最终失败: %s", func.__name__, kwargs, last_exc)
    return None


def load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def write_df(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 行)", path, len(df))


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    fh = logging.FileHandler(LOG_DIR / f"fund_flow_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


# ---------------------------------------------------------------------------
# 各数据集的写盘逻辑
# ---------------------------------------------------------------------------
def run_individual(stock_codes: list[str]) -> None:
    """个股资金流向历史日线：每只股票一个 parquet，按 date 增量合并。"""
    out_dir = OUTPUT_DIR / "individual"
    out_dir.mkdir(parents=True, exist_ok=True)

    for code in stock_codes:
        path = out_dir / f"{code}.parquet"
        df = fetch_individual(code)
        if df.empty:
            logger.info("[individual] %s 无数据，跳过", code)
            continue

        existing = load_existing(path)
        if not existing.empty:
            df = pd.concat([existing, df], ignore_index=True)
        df["date"] = pd.to_datetime(df["date"])
        df = (df.drop_duplicates(subset=["code", "date"], keep="last")
                .sort_values("date")
                .reset_index(drop=True))
        write_df(path, df)
        time.sleep(0.5)  # 控制请求频率，避免反爬


def run_rank(collect_date: str, source: str = "ths") -> None:
    """个股资金流向排行快照：每日一个文件。

    source=ths 走同花顺（5 个 symbol，覆盖今日/3/5/10/20日）；
    source=em  走东方财富（今日/3/5/10日），东财反爬时大概率抓不到。
    """
    out_dir = OUTPUT_DIR / "rank"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    if source == "em":
        for indicator in RANK_EM_INDICATORS:
            df = fetch_rank_em(indicator)
            if df.empty:
                logger.warning("[rank/em] indicator=%s 无数据", indicator)
            else:
                frames.append(df)
            time.sleep(1.0)
    else:
        for symbol in THS_RANK_SYMBOLS:
            df = fetch_rank_ths(symbol)
            if df.empty:
                logger.warning("[rank/ths] symbol=%s 无数据", symbol)
            else:
                frames.append(df)
            time.sleep(1.0)

    if not frames:
        logger.error("[rank] 全部源均无数据")
        return
    df = (pd.concat(frames, ignore_index=True)
            .sort_values(["code", "indicator"])
            .reset_index(drop=True))
    df.insert(0, "collect_date", pd.Timestamp(collect_date).normalize())
    df = df[RANK_SCHEMA].copy()
    write_df(out_dir / f"{collect_date}.parquet", df)


def run_industry(collect_date: str) -> None:
    """行业资金流向：每日一个快照文件（含即时/3日/5日/10日/20日五个 symbol）。"""
    out_dir = OUTPUT_DIR / "industry"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for symbol in INDUSTRY_SYMBOLS:
        df = fetch_industry(symbol)
        if df.empty:
            logger.warning("[industry] symbol=%s 无数据", symbol)
            continue
        frames.append(df)
        time.sleep(1.0)

    if not frames:
        logger.error("[industry] 全部 symbol 均无数据")
        return
    df = pd.concat(frames, ignore_index=True).reset_index(drop=True)
    df.insert(0, "collect_date", pd.Timestamp(collect_date).normalize())
    write_df(out_dir / f"{collect_date}.parquet", df)


# ---------------------------------------------------------------------------
def load_stock_codes() -> list[str]:
    """默认遍历的股票池：优先从个股排行快照推导，否则提示手动指定。"""
    avail = sorted((OUTPUT_DIR / "rank").glob("*.parquet")) if (OUTPUT_DIR / "rank").exists() else []
    if avail:
        df = pd.read_parquet(avail[-1])
        return sorted(df["code"].astype(str).str.zfill(6).unique().tolist())
    logger.warning("暂无股票池数据，请用 --stock 明确指定股票代码")
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取资金流数据并写入 parquet")
    parser.add_argument("--only", choices=["individual", "rank", "industry"], default=None,
                        help="只抓某类数据；缺省抓全部三类")
    parser.add_argument("--backfill", type=int, default=0,
                        help="回补前 N 个自然日的快照(rank/industry)，0=只抓今天")
    parser.add_argument("--stock", type=str, default=None, action="append",
                        help="个股资金流指定股票代码（可多次）；缺省用昨日排行快照推导股票池")
    parser.add_argument("--source", choices=["ths", "em"], default="ths",
                        help="排行快照数据源；ths=同花顺(默认，稳定)，em=东方财富(常被反爬)")
    args = parser.parse_args()

    setup_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    today = pd.Timestamp.today().strftime("%Y-%m-%d")

    # 快照类数据：今天 + 回补
    dates = [today] if args.backfill <= 0 else [
        (pd.Timestamp.today() - pd.Timedelta(days=d)).strftime("%Y-%m-%d")
        for d in range(args.backfill, -1, -1)
    ]

    if args.only in (None, "rank"):
        for d in dates:
            run_rank(d, source=args.source)

    if args.only in (None, "industry"):
        for d in dates:
            run_industry(d)

    if args.only in (None, "individual"):
        codes = args.stock or load_stock_codes()
        if not codes:
            logger.error("未指定股票的代码，且无股票池可推导，已跳过个股资金流")
        else:
            run_individual(codes)


if __name__ == "__main__":
    main()
