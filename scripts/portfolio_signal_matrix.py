"""批次 E 的工程前置：把「哪天哪只票会出信号」和撮合要用的行情列预计算成矩阵。

为什么要矩阵：`KernelStrategy` 每判一个 (交易日, 代码) 都要 `provider.window()` 切一次片，
全池 3,714 只 × 1,217 个交易日就是 450 万次；而 `window()` 背后是把整只票的 parquet 常驻缓存——
实测整池日线深拷贝 4.6G，本机可用只有 5G 上下，基准加六格共七趟会在中途把机器压穿。
这里逐只票流式读盘（算完即释放），一遍算完落成日期×代码矩阵，之后每趟回测只查表。

查表与实时必须是同一段判据：本脚本调的就是插件的 `signal_at`（与实盘选股、与 `KernelStrategy`
同源），`--verify` 再随机抽点用实时 `ParquetProvider` 走 `KernelStrategy.signal_on()` 逐项对一遍，
对不上就停下修，不许带差开跑（README §6 批次 E 的 E7）。

行情一律 float64：矩阵里存 float32 会让收盘价差出 1e-6 级，而股数是 `金额/价` 取整到 100 股，
边界上就能多买/少买一手——那是取数误差，不是策略差异，七趟对照全废。
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.backtest.kernel_strategy import KernelStrategy      # noqa: E402
from stock_selector.backtest.provider import ParquetProvider            # noqa: E402
from stock_selector.core.selector import create_strategy                # noqa: E402
from scripts.profit_scan import check_mem_headroom                      # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
POOL_FILE = ROOT / "data" / "actions" / "decision_samples.parquet"
OUT_DIR = ROOT / "data" / "portfolio"

# 批次 E 预注册钉死的三件东西：策略、窗、行情列（改任何一件 = 新批次，得重新预注册）
STRATEGY_NAME = "trend_rebound"
DATE_FROM = "2017-01-01"
DATE_TO = "2021-12-31"
PRICE_COLUMNS = ("high", "low", "close", "pct")
MATRIX_FILE = "signal_matrix.npz"
HITS_FILE = "signal_hits.parquet"
VERIFY_POINTS = 200
SEED = 20261017          # 抽样种子先写死，不许抽到不匹配再换种子

logger = logging.getLogger("signal_matrix")

# worker 侧只读上下文（initializer 建一次，别每只票重建策略对象）
_CTX: dict = {}


def _init_worker(kline_dir: str, calendar: np.ndarray, required_rows: int, strategy_name: str) -> None:
    _CTX["kline_dir"] = Path(kline_dir)
    _CTX["calendar"] = calendar
    _CTX["required_rows"] = required_rows
    _CTX["strategy"] = create_strategy(strategy_name)


def _scan_one(code: str) -> dict | None:
    """单只票：只在自己有行情的日子里问判据，其余日子天生是「无行情行」＝无信号。

    切片口径与 `ParquetProvider.window(code, date, required_rows)` 逐项一致：
    按日期升序取 ≤ date 的最后 `required_rows` 行。基准日没行的日子这里根本不会遍历，
    与 `signal_on()` 那条「最后一行早于基准日就返回 None」同效。

    窗前那些行**必须留在表里**（窗口头 30 个交易日的回看要用它们，删了就与实时口径不一致），
    但只有日期正好落在日历上的行才允许写命中与价格：`searchsorted` 会把早于日历首日的行都返回
    下标 0，不核对等值就会把整段窗前的命中与收盘价一股脑塞进日历第一天（历史段实测首日因此
    多出 8.4 万条命中，留出段多 60 万条，并把那天本该停牌的价格填成窗前最后一天的价）。
    """
    path = _CTX["kline_dir"] / f"{code}.parquet"
    if not path.exists():
        return None
    strategy = _CTX["strategy"]
    rows = _CTX["required_rows"]
    calendar = _CTX["calendar"]
    df = pd.read_parquet(path)
    if df.empty:
        return None
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.assign(_d=dates).sort_values("_d").reset_index(drop=True)
    arr = df["_d"].to_numpy()
    pos = np.searchsorted(calendar, arr)
    in_cal = pos < len(calendar)
    in_cal[in_cal] &= calendar[pos[in_cal]] == arr[in_cal]
    idx = np.flatnonzero(in_cal)
    out = {"code": code, "pos": pos[idx], "hit_pos": np.array([], dtype=np.int32),
           "hit_score": np.array([], dtype=np.float64), "hit_name": [],
           **{col: df[col].astype(float).to_numpy()[idx] for col in PRICE_COLUMNS}}
    if len(df) < 2:
        return out

    values = df.drop(columns=["_d"])
    hits: list[int] = []
    scores: list[float] = []
    names: list[str] = []
    for i in idx:
        hit = strategy.signal_at(values.iloc[max(0, i - rows + 1):i + 1].reset_index(drop=True))
        if hit is None:
            continue
        hits.append(int(pos[i]))
        scores.append(float(hit.score) if hit.score is not None else float("nan"))
        # KernelStrategy 只在命中体没名字时回填当日名称，这里按它的口径存
        names.append(hit.name if hit.name else str(df["name"].iloc[i]))
    out["hit_pos"] = np.array(hits, dtype=np.int32)
    out["hit_score"] = np.array(scores, dtype=np.float64)
    out["hit_name"] = names
    return out


def pool_codes(kline_dir: Path, limit: int = 0) -> list[str]:
    """票池 = 批 C/D 那 3,714 只（同一把 is_stock_code + ≥250 根日线 + 非 ST 筛出来的）。"""
    codes = sorted(pd.read_parquet(POOL_FILE, columns=["code"])["code"].unique().tolist())
    codes = [c for c in codes if (kline_dir / f"{c}.parquet").exists()]
    return codes[:limit] if limit else codes


def trading_calendar(kline_dir: Path, provider: ParquetProvider, codes: list[str],
                     strict: bool = True, date_from: str = DATE_FROM,
                     date_to: str = DATE_TO) -> np.ndarray:
    """日历 = `ParquetProvider.dates()`（引擎并集）∩ 窗，与基准那一趟完全同源。

    `strict` 时核一遍票池并集与之相等：票池少日子＝矩阵日历比引擎窄，七趟的交易日数就都不对。
    冒烟（只取前 N 只票）时关掉——几十只票当然凑不出完整交易日历，那是抽样不是口径问题。

    `date_from/date_to` 默认就是批次 E/E2 钉死的那个窗；只有留出段（E3）才显式传别的窗，
    传了也只影响这一个产物，不会把已经落档的 E2 矩阵口径改掉。
    """
    window = [d for d in provider.dates() if date_from <= d <= date_to]
    pool: set[str] = set()
    for code in codes:
        col = pd.read_parquet(kline_dir / f"{code}.parquet", columns=["date"])
        pool.update(pd.to_datetime(col["date"]).dt.strftime("%Y-%m-%d").tolist())
    pool_window = {d for d in pool if date_from <= d <= date_to}
    missing = [d for d in window if d not in pool_window]
    extra = sorted(d for d in pool_window if d not in set(window))
    if strict and (missing or extra):
        raise SystemExit(f"票池日历与引擎日历不一致：票池缺 {missing[:5]} 多 {extra[:5]}")
    if not strict:
        window = [d for d in window if d in pool_window]
    return np.array(window, dtype="<U10")


def _densify(results: list[dict], codes: list[str], calendar: np.ndarray) -> dict:
    """稀疏（每只票只有自己有行的日子）摊成稠密矩阵：缺行情＝NaN，无信号＝hit False。"""
    shape = (len(calendar), len(codes))
    idx = {c: j for j, c in enumerate(codes)}
    price = {col: np.full(shape, np.nan, dtype=np.float64) for col in PRICE_COLUMNS}
    hit = np.zeros(shape, dtype=bool)
    score = np.full(shape, np.nan, dtype=np.float64)
    hits: list[tuple[str, str, float, str]] = []
    for res in results:
        if res is None:
            continue
        j = idx[res["code"]]
        for col in PRICE_COLUMNS:
            price[col][res["pos"], j] = res[col]
        if len(res["hit_pos"]):
            hit[res["hit_pos"], j] = True
            score[res["hit_pos"], j] = res["hit_score"]
            hits.extend((str(calendar[p]), res["code"], s, n)
                        for p, s, n in zip(res["hit_pos"], res["hit_score"], res["hit_name"]))
    return {"price": price, "hit": hit, "score": score, "hits": hits}


def build(kline_dir: Path, out_dir: Path, workers: int, limit: int,
          date_from: str = DATE_FROM, date_to: str = DATE_TO) -> dict:
    codes = pool_codes(kline_dir, limit)
    logger.info("算日历：引擎并集 ∩ 票池并集（%d 只票）", len(codes))
    calendar = trading_calendar(kline_dir, ParquetProvider(kline_dir), codes,
                                strict=not limit, date_from=date_from, date_to=date_to)
    logger.info("池 %d 只 × 日历 %d 天（%s→%s）", len(codes), len(calendar), calendar[0], calendar[-1])

    required_rows = int(create_strategy(STRATEGY_NAME).required_rows())
    results: list[dict] = []
    with mp.Pool(workers, initializer=_init_worker,
                 initargs=(str(kline_dir), calendar, required_rows, STRATEGY_NAME)) as pool:
        done = 0
        hit_sofar = 0
        for res in pool.imap_unordered(_scan_one, codes, chunksize=8):
            done += 1
            if res:
                hit_sofar += len(res["hit_pos"])
            if done % 250 == 0 or done == len(codes):
                logger.info("已扫 %d/%d 只，累计命中 %d 个 (日,票)", done, len(codes), hit_sofar)
            results.append(res)

    dense = _densify(results, codes, calendar)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez(out_dir / MATRIX_FILE, calendar=calendar, codes=np.array(codes, dtype="<U10"),
             hit=dense["hit"], score=dense["score"], **dense["price"])
    pd.DataFrame(dense["hits"], columns=["date", "code", "score", "name"]).to_parquet(
        out_dir / HITS_FILE, index=False)
    meta = {
        "strategy": STRATEGY_NAME, "date_from": date_from, "date_to": date_to,
        "required_rows": required_rows, "pool_codes": len(codes),
        "calendar_days": len(calendar),
        "price_rows": int(np.isfinite(dense["price"]["close"]).sum()),
        "signal_hits": len(dense["hits"]),
        "hit_rate_pct": round(100.0 * len(dense["hits"]) / (len(calendar) * len(codes)), 4),
        "seed": SEED, "verify_points": VERIFY_POINTS,
        "npz": str(out_dir / MATRIX_FILE), "hits": str(out_dir / HITS_FILE),
    }
    (out_dir / "signal_matrix_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("落盘：%s", json.dumps(meta, ensure_ascii=False))
    return meta


class MatrixProvider:
    """矩阵版数据面：只实现引擎真正会调的那几个口，方法名与 `ParquetProvider` 对齐。

    口径逐条对齐：当日无行 ⇒ `row()/price()` 返回 None（引擎据此判停牌），
    不拿前一天的价冒充当天。
    """

    def __init__(self, npz_path: Path):
        data = np.load(npz_path)
        self._calendar = data["calendar"].astype(str).tolist()
        self._codes = data["codes"].astype(str).tolist()
        self._col = {c: j for j, c in enumerate(self._codes)}
        self._row = {d: i for i, d in enumerate(self._calendar)}
        self._price = {col: data[col] for col in PRICE_COLUMNS}
        self._hit = data["hit"]
        self._score = data["score"]

    def dates(self) -> list[str]:
        return self._calendar

    def codes(self) -> list[str]:
        return self._codes

    def _cell(self, date: str, code: str) -> tuple[int, int] | None:
        i = self._row.get(date)
        j = self._col.get(code)
        return None if i is None or j is None else (i, j)

    def row(self, date: str, code: str) -> dict | None:
        cell = self._cell(date, code)
        if cell is None:
            return None
        rec = {col: float(self._price[col][cell[0], cell[1]]) for col in PRICE_COLUMNS}
        return None if not np.isfinite(rec["close"]) else rec

    def price(self, date: str, code: str) -> float | None:
        rec = self.row(date, code)
        return None if rec is None else rec["close"]

    def signal(self, date: str, code: str) -> tuple[bool, float]:
        cell = self._cell(date, code)
        if cell is None:
            return False, float("nan")
        return bool(self._hit[cell[0], cell[1]]), float(self._score[cell[0], cell[1]])


class CachedKernelStrategy(KernelStrategy):
    """只把「判据怎么算」换成查表，排序、闸门、三条出场全部原样继承。

    继承而不是重写：出场规则与 `propose_buys` 的排序必须和基准那一趟一模一样，
    否则批次 E 比的就不止是现金这一件事。
    """

    def __init__(self, strategy, provider, hit_names: dict, **kwargs) -> None:
        super().__init__(strategy, provider, **kwargs)
        self.hit_names = hit_names

    def signal_on(self, date: str, code: str):
        hit, score = self.provider.signal(date, code)
        if not hit:
            return None
        return _CachedHit(code=code, name=self.hit_names.get((date, code), ""), score=score)


class _CachedHit:
    """查表版命中体：只带 `KernelStrategy` 实际消费的 name/score 两个字段。"""

    def __init__(self, code: str, name: str, score: float) -> None:
        self.code = code
        self.name = name
        self.score = None if np.isnan(score) else score


def load_hit_names(out_dir: Path) -> dict:
    table = pd.read_parquet(out_dir / HITS_FILE)
    return {(r.date, r.code): r.name for r in table.itertuples(index=False)}


def verify(kline_dir: Path, out_dir: Path, points: int, seed: int) -> int:
    """抽 (交易日, 代码) 点，实时 `KernelStrategy.signal_on` 与查表逐项对。

    随机点覆盖「命中」与「有行情无信号」，再专门补一批「当日无行」的点压停牌口径
    ——那条最容易在预计算里被悄悄丢掉（把上一天的行当今天用）。
    """
    provider = MatrixProvider(out_dir / MATRIX_FILE)
    live = KernelStrategy(strategy=create_strategy(STRATEGY_NAME),
                          provider=ParquetProvider(kline_dir))
    cached = CachedKernelStrategy(strategy=create_strategy(STRATEGY_NAME), provider=provider,
                                  hit_names=load_hit_names(out_dir))
    dates, codes = provider.dates(), provider.codes()
    rng = random.Random(seed)
    picks = [(rng.choice(dates), rng.choice(codes)) for _ in range(points)]
    # 随机点几乎踩不到窗两端，而恰恰是这两头最容易漏口径（窗前历史行被当成首日、
    # 首日停牌被填成前一天的价），所以边界日按代码定点各抽一批压一遍
    for edge in (dates[0], dates[-1]):
        picks.extend((edge, c) for c in rng.sample(codes, min(200, len(codes))))
    suspended = 0
    for code in rng.sample(codes, min(60, len(codes))):
        have = set(pd.read_parquet(kline_dir / f"{code}.parquet", columns=["date"])
                   .pipe(lambda d: pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")))
        gap = [d for d in dates if d not in have]
        if gap:
            picks.append((rng.choice(gap), code))
            suspended += 1

    mismatch: list[str] = []
    hits = 0
    for date, code in picks:
        a, b = live.signal_on(date, code), cached.signal_on(date, code)
        if a is None and b is None:
            continue
        if a is None or b is None:
            mismatch.append(f"{date} {code}: 一边没信号（实时={a is not None} 查表={b is not None}）")
            continue
        hits += 1
        if float(a.score or 0.0) != float(b.score or 0.0):
            mismatch.append(f"{date} {code}: score 实时={a.score!r} 查表={b.score!r}")
        if (a.name or "") != (b.name or ""):
            mismatch.append(f"{date} {code}: name 实时={a.name!r} 查表={b.name!r}")
    logger.info("等价校验：%d 个点（含 %d 个停牌日），命中 %d 个，不一致 %d 个",
                len(picks), suspended, hits, len(mismatch))
    for line in mismatch[:10]:
        logger.warning("  %s", line)
    if mismatch:
        raise SystemExit(f"矩阵与实时判据不一致（{len(mismatch)} 个点），批次 E 不许开跑")
    logger.info("矩阵与实时 signal_on 全等，批次 E 可以开跑")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="批次 E：逐日全池判据预计算 + 等价校验")
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--workers", type=int, default=min(6, mp.cpu_count() or 4))
    ap.add_argument("--limit", type=int, default=0, help="只算前 N 只票（冒烟用）")
    # 窗默认是批次 E/E2 钉死的那个；留出段（E3）才显式传，且只影响 --out-dir 指过去的那个产物
    ap.add_argument("--date-from", default=DATE_FROM)
    ap.add_argument("--date-to", default=DATE_TO)
    ap.add_argument("--verify", action="store_true", help="抽点比对矩阵与实时 signal_on")
    ap.add_argument("--points", type=int, default=VERIFY_POINTS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--skip-mem-guard", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kline_dir, out_dir = Path(args.kline_dir), Path(args.out_dir)
    if args.verify:
        return verify(kline_dir, out_dir, args.points, args.seed)
    check_mem_headroom(skip=args.skip_mem_guard)
    build(kline_dir, out_dir, args.workers, args.limit, args.date_from, args.date_to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
