"""盘后全市场快照：把交易所口径的涨跌停价/换手率/成交额/流通市值落成日频文件。

产出（每个交易日一个文件，重跑覆盖、幂等）：
    data/market_snapshot/{YYYY-MM-DD}.parquet

为什么要单独落这份数据：本地日线里**只有涨停标记 `lu`，没有跌停**，成交额自 2026-05-21
起爬虫不再回填（全是 0），涨跌停阈值还得区分主板/创业/科创/北交所/ST/ETF。自己按百分比
推一定会算错。腾讯快照字段 47/48 是交易所下发的涨停价/跌停价，连同换手率、量比、精确
成交额一起给到（解析与单位口径见 stock_selector/quote/tencent.py），风控的「涨跌停可成交性」
和「流动性」两条规则就以这份文件为准。

口径：
- 标的池 = data/stock_kline 下的全部代码（含场内基金），停牌/退市代码在快照里天然缺失；
- `at_limit_up/at_limit_down` 用「价格 ≤ 涨停价 - 0.005」判定，价格是 0.01 一跳，留半个 tick 容差；
- 基金不参与涨跌停成交性判断（口径与日线 `lu` 强制 False 一致），只打 `asset=fund` 标记；
- 不写五档盘口：那是盘中实时链路的职责（见 redis 最新行情），日频复盘用不上。

用法:
    python scripts/fetch_market_snapshot.py
    python scripts/fetch_market_snapshot.py --code 600000 300750     # 调试
    python scripts/fetch_market_snapshot.py --min-ratio 0.2 --out-dir /tmp/snap
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from tqdm import tqdm

from stock_selector.quote import tencent

ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = ROOT / "data" / "stock_kline"
OUTPUT_DIR = ROOT / "data" / "market_snapshot"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("fetch_market_snapshot")

# 半个 tick：价格最小变动 0.01，判定贴住涨跌停要留容差
TICK_EPS = 0.005
# 1/5 字头是场内基金（ETF/LOF/REITs），与 pricing.py 的基金口径一致
_FUND_FIRST = ("1", "5")

COLUMNS = ["date", "code", "name", "asset", "price", "pct", "prev_close", "open", "high",
           "low", "volume", "amount", "turnover_pct", "vol_ratio", "float_mv", "total_mv",
           "limit_up", "limit_down", "at_limit_up", "at_limit_down", "outer_volume",
           "inner_volume", "avg_price", "captured_at"]


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(
        LOG_DIR / f"market_snapshot_{time.strftime('%Y%m%d')}.log", encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def load_universe(codes: list[str] | None, limit: int | None) -> list[str]:
    """标的池：本地日线目录下的全部代码（文件名即六位代码）。"""
    if codes:
        universe = [c.strip().zfill(6) for c in codes if c.strip()]
    else:
        universe = sorted(p.stem for p in KLINE_DIR.glob("*.parquet")
                          if p.stem.isdigit() and len(p.stem) == 6)
        if not universe:
            raise SystemExit(f"{KLINE_DIR} 下没有日线文件，标的池为空")
    if limit:
        universe = universe[:limit]
    return universe


def _quote_date(quote_time: str) -> str:
    """快照自带日期（YYYYMMDDHHMMSS）；异常长度退回今天，别让一条脏记录打断整批。"""
    raw = str(quote_time or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return datetime.now().strftime("%Y-%m-%d")


def _at_limit(price: float | None, limit: float | None, up: bool) -> bool:
    if price is None or limit is None or limit <= 0:
        return False
    return price >= limit - TICK_EPS if up else price <= limit + TICK_EPS


def to_frame(records: dict[str, dict], captured_at: str) -> pd.DataFrame:
    rows = []
    for code, q in records.items():
        price = q["price"]
        rows.append({
            "date": _quote_date(q["quote_time"]),
            "code": code,
            "name": q["name"],
            "asset": "fund" if code[0] in _FUND_FIRST else "stock",
            "price": price,
            "pct": q["pct"],
            "prev_close": q["prev_close"],
            "open": q["open"],
            "high": q["high"],
            "low": q["low"],
            "volume": q["volume"],
            "amount": q["amount"],
            "turnover_pct": q["turnover_pct"],
            "vol_ratio": q["vol_ratio"],
            "float_mv": q["float_mv"],
            "total_mv": q["total_mv"],
            "limit_up": q["limit_up"],
            "limit_down": q["limit_down"],
            "at_limit_up": _at_limit(price, q["limit_up"], True),
            "at_limit_down": _at_limit(price, q["limit_down"], False),
            "outer_volume": q["outer_volume"],
            "inner_volume": q["inner_volume"],
            "avg_price": q["avg_price"],
            "captured_at": captured_at,
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch_universe(universe: list[str], batch: int, timeout: float) -> dict[str, dict]:
    """分片抓取（腾讯单次可带 ~200 只；分片失败只丢该片，命中率由调用方把关）。"""
    out: dict[str, dict] = {}
    for start in tqdm(range(0, len(universe), batch), desc="snapshot", mininterval=5):
        out.update(tencent.fetch(universe[start:start + batch], timeout=timeout))
    return out


def prune(out_dir: Path, keep_days: int) -> None:
    """只留最近 keep_days 个文件：全市场快照一天约 5000 行，留一年也才几十 MB，纯按份数裁。"""
    files = sorted(p for p in out_dir.glob("*.parquet") if p.stem.isdigit())
    if keep_days <= 0 or len(files) <= keep_days:
        return
    for p in files[:len(files) - keep_days]:
        p.unlink()
        logger.info("清理旧快照 %s（保留最近 %d 份）", p.name, keep_days)


def build(universe: list[str], batch: int, timeout: float, min_ratio: float,
          out_dir: Path, keep_days: int) -> dict:
    records = fetch_universe(universe, batch, timeout)
    ratio = len(records) / len(universe) if universe else 0.0
    if ratio < min_ratio:
        raise SystemExit(
            f"快照只命中 {len(records)}/{len(universe)} 只（{ratio:.1%} < 下限 {min_ratio:.1%}），"
            "通常是行情源限流或网络故障——拒绝写出残缺文件"
        )
    df = to_frame(records, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if df.empty:
        raise SystemExit("快照解析后为空，检查代码格式与行情源")

    dates = sorted(df["date"].unique())
    if len(dates) > 1:
        logger.warning("快照跨到多个交易日 %s，按最后一个交易日整批落盘", dates)
    day = dates[-1]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.parquet"
    df.sort_values("code").reset_index(drop=True).to_parquet(path, compression="zstd", index=False)
    logger.info("写出 %s：%d 行（股票 %d / 基金 %d）", path, len(df),
                int((df["asset"] == "stock").sum()), int((df["asset"] == "fund").sum()))

    # 有涨跌停价的股票里命中率极低是正常的（收盘时贴板的本来就少），但全零说明字段没拿到
    stocks = df[df["asset"] == "stock"]
    logger.info(
        "字段覆盖：涨停价 %d/%d 跌停价 %d/%d 成交额>0 %d 换手率>0 %d；贴涨停 %d 只 贴跌停 %d 只",
        int(stocks["limit_up"].notna().sum()), len(stocks),
        int(stocks["limit_down"].notna().sum()), len(stocks),
        int((stocks["amount"] > 0).sum()), int((stocks["turnover_pct"] > 0).sum()),
        int(stocks["at_limit_up"].sum()), int(stocks["at_limit_down"].sum()),
    )
    prune(out_dir, keep_days)
    return {"date": day, "rows": len(df), "ratio": ratio}


def main() -> None:
    parser = argparse.ArgumentParser(description="盘后全市场涨跌停/流动性快照")
    parser.add_argument("--code", nargs="*", default=None, dest="codes",
                        help="只抓这些代码（调试用，默认全市场）")
    parser.add_argument("--limit", type=int, default=None, help="只抓前 N 只（调试）")
    parser.add_argument("--batch", type=int, default=200, help="单次请求的代码数，默认 200")
    parser.add_argument("--timeout", type=float, default=8.0, help="单片超时（秒）")
    parser.add_argument("--min-ratio", type=float, default=0.6,
                        help="命中率下限，低于则判定为行情故障并不落盘")
    parser.add_argument("--keep-days", type=int, default=250, help="快照文件保留份数，0=不清理")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    setup_logging()
    started = time.time()
    universe = load_universe(args.codes, args.limit)
    logger.info("标的池 %d 只，分片 %d 只/请求", len(universe), args.batch)
    out = build(universe, args.batch, args.timeout, args.min_ratio, args.out_dir, args.keep_days)
    logger.info("完成：date=%s 行数=%d 命中率=%.1f%% 耗时 %.1fs",
                out["date"], out["rows"], 100 * out["ratio"], time.time() - started)


if __name__ == "__main__":
    main()
