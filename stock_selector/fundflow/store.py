"""日分区资金流存储：落盘、合并、标准化、质量标记、覆盖度自检。纯函数层，不碰网络。

为什么要这一层：采集脚本原本把每只票的 124 天历史各存一个文件（`individual/{code}.parquet`），
「今天到底采到了没有、缺哪几天」只能扫 5,215 个文件才知道，而长期攒样本的第一需求恰恰是
**断档必须当天可见**。改成按交易日期分区后，缺一天 = 少一个文件，自检就是一次目录列举。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

DAILY_SUBDIR = "daily"
STATUS_FILE = "status.json"

# 交易日的行情快照里，主力/超大单/大单/中单/小单五档净流入 + 成交额是全部要留的原始量。
# 占比一律自己算（`*_ratio` = 净额 ÷ 成交额），不存接口给的两位小数版本：同一个定义，
# 精度差两个数量级，留两份只会让人猜该用哪一份。
AMOUNT_COLUMN = "amount"
TIERS = ("main", "xlarge", "large", "medium", "small")

DAILY_COLUMNS = [
    "trade_date", "code", "name", "close", "pct_change", AMOUNT_COLUMN,
    *[f"{t}_net_inflow" for t in TIERS],
    *[f"{t}_ratio" for t in TIERS],
    "valid", "exclude_reason", "data_ts", "collected_at",
]

# 停牌/无成交：成交额为 0。此时所有净流入都无意义（接口会给 "-"→NaN）
REASON_ZERO_AMOUNT = "zero_amount"
# 成交额未知：日线里那天的 amount 还没并回来（历史迁移会撞上），占比只能留 NaN。
# 与「成交额为 0」分开记：前者是缺数、后者是事实，混成一个标记以后就没法判断覆盖率。
REASON_NO_AMOUNT = "no_amount"
# 异常标的：名称带 ST/*ST 或「退」，长期实验的截面口径要把它们剔掉，但行要留着（剔与不剔是读时选择）
REASON_ABNORMAL_NAME = "abnormal_name"

# 命中任一子串即打 abnormal_name：ST（含 \*ST）与退市整理期（名称带「退」）。
_ABNORMAL_NAME_MARKERS = ("ST", "退")

ANCHOR_CODES = ("600000", "600036", "600519", "601398", "000001")
"""交易日历的代理：这几只巨型票基本不停牌，它们的**交集**日期就是交易所开市日。

用交集不用并集：某只偶然停牌一天时，交集只是少认一个交易日（自检少查一天，无害）；
并集则会多认一个交易日（把没采到的那天报成断档，天天假警报）。
"""


def partition_path(daily_dir: Path, trade_date: str) -> Path:
    return Path(daily_dir) / f"{trade_date}.parquet"


def read_partition(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=DAILY_COLUMNS)
    return pd.read_parquet(path)


def partition_rows(path: Path) -> int:
    """只读 parquet footer 的行数：自检要数 1,200 个分区，不能把数据全读进内存。"""
    path = Path(path)
    if not path.exists():
        return 0
    return int(pq.ParquetFile(path).metadata.num_rows)


def stored_dates(daily_dir: Path) -> dict[str, int]:
    """已落盘的交易日 -> 行数。"""
    daily_dir = Path(daily_dir)
    out: dict[str, int] = {}
    for path in sorted(daily_dir.glob("*.parquet")):
        day = path.stem
        if len(day) == 10 and day[4] == "-" and day[7] == "-":
            out[day] = partition_rows(path)
    return out


def normalize_and_flag(rows: pd.DataFrame, *, data_ts: str, collected_at: str) -> pd.DataFrame:
    """补上 trade_date / 五档占比 / 质量标记，产出严格 `DAILY_COLUMNS` 顺序的表。

    `amount` 是标准化唯一分母：缺失或 <=0 一律判 `zero_amount`（停牌或无成交），
    占比留 NaN——**不填 0**，因为「没有成交」和「净流入恰好为零」在因子里是两件事。
    """
    df = rows.copy()
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    date = pd.to_datetime(df["date"])
    df["trade_date"] = date.dt.strftime("%Y-%m-%d")
    df["data_ts"] = str(data_ts)
    df["collected_at"] = str(collected_at)

    amount = pd.to_numeric(df.get(AMOUNT_COLUMN), errors="coerce")
    df[AMOUNT_COLUMN] = amount
    for tier in TIERS:
        net = pd.to_numeric(df.get(f"{tier}_net_inflow"), errors="coerce")
        df[f"{tier}_net_inflow"] = net
        df[f"{tier}_ratio"] = net.where(amount > 0).div(amount.where(amount > 0))

    name = df["name"].fillna("").astype(str)
    abnormal = name.str.contains("|".join(_ABNORMAL_NAME_MARKERS), regex=True)
    no_amount = amount.isna()
    zero_amount = amount == 0
    reasons = pd.Series(None, index=df.index, dtype="object")
    reasons[zero_amount] = REASON_ZERO_AMOUNT
    reasons[no_amount] = REASON_NO_AMOUNT
    reasons[abnormal & reasons.isna()] = REASON_ABNORMAL_NAME
    df["exclude_reason"] = reasons
    df["valid"] = reasons.isna()
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[DAILY_COLUMNS].sort_values("code").reset_index(drop=True)


def merge_partition(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """同一天分区合并：`(code)` 唯一，新值非空才覆盖，旧值只在新值为空时保留。

    选这个方向是因为同一天可能被采多次（当日批量成功、次日逐只修复又带来一份），
    任何一次成功都不该被另一次失败抹掉。`combine_first` 正好是这个语义。
    """
    if existing is None or existing.empty:
        return (new if new is not None else pd.DataFrame()).drop_duplicates(
            "code", keep="last").reset_index(drop=True)
    if new is None or new.empty:
        return existing.reset_index(drop=True)
    merged = new.set_index("code").combine_first(existing.set_index("code"))
    return merged.reset_index()[DAILY_COLUMNS]


def write_partitions(daily_dir: Path, rows: pd.DataFrame) -> dict[str, int]:
    """按 `trade_date` 摊到各日分区并落盘，返回 {交易日: 本次合并后的总行数}。"""
    daily_dir = Path(daily_dir)
    daily_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    if rows is None or rows.empty:
        return written
    for trade_date, block in rows.groupby("trade_date", sort=True):
        path = partition_path(daily_dir, str(trade_date))
        merged = merge_partition(read_partition(path), block[DAILY_COLUMNS])
        merged.to_parquet(path, index=False, compression="zstd")
        written[str(trade_date)] = len(merged)
    return written


def expected_trading_days(kline_dir: Path, count: int, *,
                          anchors: tuple[str, ...] = ANCHOR_CODES) -> list[str]:
    """最近 `count` 个应有交易日（巨型票日线日期的交集，倒推）。"""
    kline_dir = Path(kline_dir)
    sets: list[set[str]] = []
    for code in anchors:
        path = kline_dir / f"{code}.parquet"
        if not path.exists():
            continue
        days = pd.read_parquet(path, columns=["date"])["date"]
        days = pd.to_datetime(days).dt.strftime("%Y-%m-%d")
        sets.append(set(days.iloc[-count:].tolist()))
    if not sets:
        return []
    common = sets[0]
    for other in sets[1:]:
        common &= other
    return sorted(common)[-count:]


def gap_report(daily_dir: Path, expected: list[str], *, known: dict[str, int] | None = None,
               thin_ratio: float = 0.8) -> dict:
    """断档与「薄日」清单。

    `missing` = 应采而根本没有文件的日子；`thin` = 有文件但行数明显低于近期水位
    （批量分页大面积失败会留下这种「看起来成功、其实缺一半」的日子，只数文件会漏掉它）。
    水位取已存分区行数的中位数：全市场票池会变（新股上市/退市），所以不写死阈值，
    且样本不足 5 个分区时不做薄日判定——冷启动时那只会误报。
    """
    known = stored_dates(daily_dir) if known is None else known
    missing = [d for d in expected if d not in known]
    thin: dict[str, int] = {}
    rows_series = sorted(known.values())
    if len(rows_series) >= 5:
        median = rows_series[len(rows_series) // 2]
        floor = median * thin_ratio
        thin = {d: n for d, n in known.items() if d in set(expected) and n < floor}
    return {"missing": missing, "thin": thin, "stored": len(known)}
