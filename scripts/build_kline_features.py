"""从 MongoDB stock_kline 读取日线，用 talib 预计算指标，按股票分文件写入 parquet，支持增量更新。

代码清单默认是「bk_stocks 里的股票 + stock_kline 里有日线的场内基金(ETF/LOF)」，
因为 ETF 不在 bk_stocks 里，只按股票清单建就永远产不出 159599.parquet 这类文件。

用法:
    python scripts/build_kline_features.py --full          # 首次全量
    python scripts/build_kline_features.py                 # 增量更新
    python scripts/build_kline_features.py --no-etf        # 只要股票，不建 ETF/LOF
    python scripts/build_kline_features.py --code 600519   # 单只调试
    python scripts/build_kline_features.py --limit 100     # 调试时限制股票数
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import requests
import talib
import yaml
from pymongo import MongoClient
from tqdm import tqdm

from stock_selector.features.base_columns import add_base_columns

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "stock_kline"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("build_kline_features")

KLINE_COLLECTION = "stock_kline"
KLINE_FILTER = {"frequency": 9, "adjust": "qfq"}

# 场内基金：15/16/18 是深市，50/51/52/53/55/56/58 是沪市（ETF、LOF、REITs、债券 ETF）
FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "53", "55", "56", "58")


def is_fund_code(code: str) -> bool:
    return str(code).startswith(FUND_PREFIXES)


INDICATOR_FUNCS = {
    "ma5": lambda df: talib.SMA(df["close"], timeperiod=5),
    "ma10": lambda df: talib.SMA(df["close"], timeperiod=10),
    "ma20": lambda df: talib.SMA(df["close"], timeperiod=20),
    "ma60": lambda df: talib.SMA(df["close"], timeperiod=60),
    "ema12": lambda df: talib.EMA(df["close"], timeperiod=12),
    "ema26": lambda df: talib.EMA(df["close"], timeperiod=26),
    "macd_dif": lambda df: talib.MACD(df["close"], fastperiod=12, slowperiod=26, signalperiod=9)[0],
    "macd_dea": lambda df: talib.MACD(df["close"], fastperiod=12, slowperiod=26, signalperiod=9)[1],
    "macd_hist": lambda df: talib.MACD(df["close"], fastperiod=12, slowperiod=26, signalperiod=9)[2],
    "rsi14": lambda df: talib.RSI(df["close"], timeperiod=14),
    "kdj_k": lambda df: talib.STOCH(
        df["high"], df["low"], df["close"], fastk_period=9, slowk_period=3, slowd_period=3
    )[0],
    "kdj_d": lambda df: talib.STOCH(
        df["high"], df["low"], df["close"], fastk_period=9, slowk_period=3, slowd_period=3
    )[1],
    "wr14": lambda df: -talib.WILLR(df["high"], df["low"], df["close"], timeperiod=14),
    "atr14": lambda df: talib.ATR(df["high"], df["low"], df["close"], timeperiod=14),
    "boll_upper": lambda df: talib.BBANDS(df["close"], timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)[0],
    "boll_mid": lambda df: talib.BBANDS(df["close"], timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)[1],
    "boll_lower": lambda df: talib.BBANDS(df["close"], timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)[2],
    "obv": lambda df: talib.OBV(df["close"], df["volume"]),
    "roc10": lambda df: talib.ROC(df["close"], timeperiod=10),
    "adx14": lambda df: talib.ADX(df["high"], df["low"], df["close"], timeperiod=14),
}

BASE_COLUMNS = ["code", "name", "date", "open", "high", "low", "close", "volume", "amount"]
INDICATOR_COLUMNS = list(INDICATOR_FUNCS.keys()) + ["kdj_j", "vol_ma5", "last_close"]
DERIVED_COLUMNS = ["pct", "lu", "vol_ratio", "pos60"]
OUTPUT_COLUMNS = BASE_COLUMNS + INDICATOR_COLUMNS + DERIVED_COLUMNS


def load_config() -> dict:
    cfg = ROOT / "stock_selector" / "config" / "config.yaml"
    if cfg.exists():
        return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    return {"mongodb": {"host": "127.0.0.1", "port": 27017, "database": "eastmoney_news"}}


def get_client() -> MongoClient:
    mongo = load_config().get("mongodb") or {}
    return MongoClient(
        host=mongo.get("host", "127.0.0.1"),
        port=int(mongo.get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )


def collect_stock_names() -> dict:
    client = get_client()
    try:
        col = client[load_config()["mongodb"]["database"]]["bk_stocks"]
        names = {}
        for doc in col.find({}, {"stock_code": 1, "stock_name": 1}):
            code = str(doc.get("stock_code", "")).split(".")[-1].zfill(6)
            name = doc.get("stock_name")
            if code and name:
                names[code] = name
        return names
    finally:
        client.close()


def collect_fund_codes(client, database: str) -> list[str]:
    """stock_kline 里真有日线且看着像场内基金的代码（爬虫已经在抓 ETF，只是没人把它们列进清单）。"""
    codes = set()
    for doc in client[database][KLINE_COLLECTION].aggregate(
        [{"$match": KLINE_FILTER}, {"$group": {"_id": "$code"}}], allowDiskUse=True
    ):
        code = str(doc.get("_id") or "")
        if len(code) == 6 and code.isdigit() and is_fund_code(code):
            codes.add(code)
    return sorted(codes)


def collect_fund_names(codes: list[str]) -> dict[str, str]:
    """场内基金的名字：bk_stocks/all_stock.csv 都只有股票，只能问腾讯行情；拿不到就退回代码。"""
    names: dict[str, str] = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        symbols = ",".join(("sh" if c.startswith("5") else "sz") + c for c in batch)
        try:
            resp = requests.get(f"https://qt.gtimg.cn/q={symbols}", timeout=10)
            for line in resp.content.decode("gbk", "ignore").split(";"):
                f = line.split("~")
                if len(f) > 3 and len(f[2]) >= 6 and f[2].isdigit():
                    names[f[2].zfill(6)] = f[1]
        except Exception as exc:            # 名字只是展示用，行情源抖动不该让整轮构建失败
            logger.warning("取场内基金名字失败(第 %d 只起): %s", i, exc)
            break
    return names


def load_kline_df(client, database: str, code: str, after: str | None = None) -> pd.DataFrame:
    col = client[database][KLINE_COLLECTION]
    query = dict(KLINE_FILTER)
    query["code"] = code
    if after:
        query["date"] = {"$gt": f"{after} 23:59"}
    docs = list(
        col.find(query, {"date": 1, "open": 1, "high": 1, "low": 1, "close": 1,
                         "volume": 1, "amount": 1})
        .sort("date", 1)
    )
    if not docs:
        return pd.DataFrame()
    df = pd.DataFrame(docs).drop(columns=["_id"], errors="ignore")
    df["date"] = pd.to_datetime(df["date"].str.slice(0, 10))
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values("date").reset_index(drop=True)
    out = pd.DataFrame(index=df.index)
    out["code"] = df["code"]
    out["name"] = df["name"]
    out["date"] = df["date"]
    for base in ["open", "high", "low", "close", "volume", "amount"]:
        out[base] = df[base].astype(float)
    for col, func in INDICATOR_FUNCS.items():
        out[col] = func(df).astype(float)
    out["kdj_j"] = out["kdj_k"] * 3 - out["kdj_d"] * 2
    out["vol_ma5"] = df["volume"].astype(float).rolling(5).mean()
    out["last_close"] = df["close"].astype(float).shift(1)
    out = add_base_columns(out)
    return out[OUTPUT_COLUMNS]


def load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def write_df(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df.sort_values("date").reset_index(drop=True)
    df.to_parquet(path, index=False, compression="zstd")


def process_code(client, database: str, code: str, name: str, full: bool) -> bool:
    path = OUTPUT_DIR / f"{code}.parquet"
    existing = load_existing(path) if not full else pd.DataFrame()

    if not full and not existing.empty:
        last_date = existing["date"].max().strftime("%Y-%m-%d")
        new_df = load_kline_df(client, database, code, after=last_date)
        if new_df.empty:
            return False
        new_df["code"] = code
        new_df["name"] = name
        df = pd.concat([existing, new_df], ignore_index=True)
    else:
        df = load_kline_df(client, database, code)
        if df.empty:
            return False
        df["code"] = code
        df["name"] = name

    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    df = compute_indicators(df)
    if is_fund_code(code):
        # ETF 没有「板块涨停」概念（跨境 ETF 甚至不限幅），涨停线判据套在基金上只会造出假信号
        df["lu"] = False
    write_df(path, df)
    return True


def setup_logging() -> None:
    """配置 logging：控制台 + 按日滚动写入文件。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    fh = logging.FileHandler(
        LOG_DIR / f"kline_features_{time.strftime('%Y%m%d')}.log",
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 K线+指标 parquet，支持增量更新")
    parser.add_argument("--full", action="store_true", help="全量重建")
    parser.add_argument("--code", type=str, default=None, help="只处理指定股票代码")
    parser.add_argument("--limit", type=int, default=0, help="最多处理股票数（0=不限制）")
    parser.add_argument("--no-etf", action="store_true", help="不建场内基金（ETF/LOF），只建股票")
    args = parser.parse_args()

    setup_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    names = collect_stock_names()

    client = get_client()
    database = load_config()["mongodb"]["database"]
    try:
        if args.code:
            codes = [args.code]
            if is_fund_code(args.code) and not names.get(args.code):
                names.update(collect_fund_names([args.code]))
        else:
            codes = list(names.keys())
            if not args.no_etf:
                stock_codes = set(codes)
                fund_codes = [c for c in collect_fund_codes(client, database) if c not in stock_codes]
                named = collect_fund_names(fund_codes)
                names.update(named)
                codes += fund_codes
                logger.info("场内基金 %d 只入清单，拿到名字 %d/%d",
                            len(fund_codes), len(named), len(fund_codes))
        if args.limit:
            codes = codes[: args.limit]

        total = len(codes)
        logger.info("开始构建，mode=%s total=%d", "full" if args.full else "incremental", total)
        t0 = time.time()
        processed = 0
        skipped = 0
        failed = 0
        for code in tqdm(codes, desc="处理代码", unit="只", ncols=80):
            name = names.get(code, code)
            try:
                changed = process_code(client, database, code, name, args.full)
                if changed:
                    processed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                logger.warning("[FAIL] %s %s: %s", code, name, exc)
        elapsed = time.time() - t0
        logger.info(
            "完成。processed=%d skipped=%d failed=%d total=%d 耗时=%.1fs",
            processed, skipped, failed, total, elapsed,
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()