"""qlib 路线初版：Alpha158 特征 → LightGBM 训练 → IC/分层验收 → TopkDropout 回测 → 最新一天选股名单。

数据来自 `scripts/qlib_export_kline.py`（Mongo 两批互补日线）+ qlib 自己的 `dump_bin.py`，
本脚本不碰本地 parquet，也不碰旧的回测引擎——按「从数据到回测全部走 qlib」的要求实现。

四条写死在代码里的口径（改任何一条，历史读数就不可比）：
1. **标签 `Ref($close,-5)/Ref($close,-1)-1`**：T 日收盘算特征 → T+1 收盘买 → T+5 收盘卖。
   特征绝不包含 T+1 的任何信息，符合本项目「T 日信号只能 T+1 用」的防前瞻红线。
2. **回测不做额外平移**：qlib 的 TopkDropout 在交易日 T 决策时读的是 **T−1 日**的信号
   （`TradeCalendarManager.get_step_time(trade_step, shift=1)`，源码写明 shift>0 取更早一根），
   成交价是 T 日收盘。于是「特征日 F 的分数 → F+1 收盘成交」天然成立，
   而 F+1 收盘正是标签的买入价。再手动平移一次就变成 F+2 成交、拿 F+1 的标签算收益，
   两侧口径错位——所以这里刻意**不**平移。
3. **票池只按训练段的流动性选**：`--universe` 取 train_beg→train_end 日均成交额最大的一批**股票**
   （剔场内基金/指数/B股/北交所）。用训练段是因为「谁有流动性」这件事本身是历史事实，
   拿测试段的排名当票池就是把未来信息塞进组合，和用今天的沪深300成分股回测2016年一样假。
   代价也要说：票池冻结到 2021 年底，2022 年后新崛起的票永远进不来。
4. **成本与不可成交**：买入 0.0005、卖出 0.0015（含千1印花税）、最低 5 元；
   `limit_threshold=0.095` 让涨跌停当日买不进/卖不掉。这条对 30x/68x（20% 涨跌幅）偏严，
   也就是宁可少赚也不给自己造假成交。
5. **滚动重训必须冻结票池**（`--pool-file`）：票池按训练段流动性重挑一次，`train_end` 一挪就换血
   （批次 I 实测 59/300），于是「第 8 名」「跌出前 100 才卖」在不同臂之间比的不是同一批票。
   冻结池之后，滚动只推进训练窗，横截面的定义不变，两臂的逐日 IC 才能按日配对。

产物除了聚合读数，还落两份**逐日**序列（批次 L 的仪器，缺了就每次滚动重训只能报点估计）：
`data/qlib/ic_daily_<run_id>.parquet`（逐日秩相关/Pearson/横截面只数/Q5−Q1 日差）与
`data/qlib/pred_<run_id>.parquet`（测试段逐日逐票打分）；两臂配对检验用
`scripts/qlib_ic_pair.py`，5 日重叠标签 ⇒ 逐日 t 要 ÷√5 才是接近真实的读数。

内存约束：实测每行样本约 3.65 KB（冒烟：120 只 × 2019-2021 = 87,600 行 → 峰值 670 MB，
扣掉 Python+qlib+pandas 的 ~350 MB 底板）。本机只剩 ~5.5G，800 只 × 10 年 ≈ 210 万行 ≈ 8G
装不下，所以 `--universe` 默认 300（≈79 万行 / 3.2G）；要抬高之前先按这个单价重算一遍。

用法:
    python3 scripts/qlib_run.py --smoke                    # 小池短窗，几分钟，验管道
    python3 scripts/qlib_run.py --universe 300 --backtest   # 初版全跑
    python3 scripts/qlib_run.py --to-mongo                  # 额外把名单写进 qlib_top_stocks

滚动重训（批次 L 起的标准跑法：池冻结、票池文件不覆盖线上那份、给种子让两臂可比）:
    python3 scripts/qlib_run.py --pool-file data/qlib/model/pool.txt \\
        --inst-name roll_2026_06_30 --seed 7 --train-end 2026-06-30 \\
        --valid-beg 2026-07-01 --valid-end 2026-07-31 \\
        --test-beg 2026-08-01 --test-end 2026-10-16 \\
        --export-model data/qlib/model_roll_2026H2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path("/data/work/qlib_data/cn")
CSV_DIR = Path("/data/work/qlib_data/csv")
REPORT_DIR = Path("/data/work/SelectStock/data/qlib")
LABEL = "Ref($close, -5) / Ref($close, -1) - 1"
# 号段白名单：沪主板的 60/科创板 68、深主板 00、创业板 30；其余（15/16/18/5x 基金、
# 399 指数、2xx B股、4/8 北交所）一律不进股票池
STOCK_PREFIX = ("60", "68", "00", "30")

logger = logging.getLogger("qlib_run")


def stock_symbols() -> list[str]:
    """从导出的 CSV 文件名拿全部 symbol，按号段只留股票。"""
    return sorted(f.stem for f in CSV_DIR.glob("*.csv") if f.stem[2:] and f.stem[2:4] in STOCK_PREFIX)


def liquid_universe(symbols: list[str], beg: str, end: str, top: int) -> list[str]:
    """按 [beg,end] 的日均成交额排出前 top 只——只用训练段，不碰测试段。"""
    frames = []
    for i, sym in enumerate(symbols, 1):
        code = sym[2:]
        path = CSV_DIR / f"{sym}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, usecols=["date", "amount"])
        except pd.errors.EmptyDataError:
            continue
        df = df[(df["date"] >= beg) & (df["date"] <= end)]
        if len(df) < 250:
            continue
        frames.append((sym, float(df["amount"].mean())))
        if i % 1000 == 0:
            logger.info("流动性统计 %d/%d", i, len(symbols))
    ranked = pd.DataFrame(frames, columns=["symbol", "avg_amount"]).sort_values(
        "avg_amount", ascending=False)
    return ranked.head(top)["symbol"].tolist()


def read_pool_file(path: Path) -> list[str]:
    """冻结票池：一行一个 symbol，也接受 qlib instruments 的 `SYMBOL\\t起\\t止` 三列格式。

    为什么要有这一条：`--universe` 每次按训练段重挑，`train_end` 一挪池就换血，
    两臂的「第几名」比的不是同一批票，逐日 IC 也就没法按日配对。
    顺序保留（不按字典序排）是为了让「同一个池文件 = 同一批票」这件事肉眼可查。
    """
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        sym = line.split("\t")[0].strip().upper()
        if not sym or sym.startswith("#"):
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    if len(out) < 30:
        raise ValueError(f"冻结票池 {path} 只有 {len(out)} 只：少于 30 只算不出横截面 IC")
    bad = [s for s in out if not (s[:2] in ("SH", "SZ", "BJ") and s[2:4] in STOCK_PREFIX)]
    if bad:
        raise ValueError(f"冻结票池 {path} 里有非 A 股代码（{bad[:5]}），池文件被写坏了")
    return out


def write_instruments(qlib_dir: Path, name: str, symbols: list[str]) -> Path:
    """写 instruments/<name>.txt，起止日沿用 all.txt 里每只票自己的区间。

    文件名一律转小写：qlib 的 Instrument 在查表前会把名字 `lower()`，
    写成 `batchL_ctrl.txt` 就会报 `instrument not exists: .../batchl_ctrl.txt`
    ——文件明明在盘上，却永远读不到（批次 L 第一次起跑就是这么死的）。
    """
    spans = {}
    for line in (qlib_dir / "instruments" / "all.txt").read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            spans[parts[0].strip().upper()] = (parts[1].strip(), parts[2].strip())
    path = qlib_dir / "instruments" / f"{name.lower()}.txt"
    lines = [f"{s}\t{spans[s][0]}\t{spans[s][1]}" for s in symbols if s in spans]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_dataset(qlib, instruments: str, fit_end: str, segments: dict):
    """Alpha158 因子 + 训练段拟合的稳健标准化。fit 窗口只到 fit_end，绝不越过训练段末尾。"""
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.processor import CSRankNorm, DropnaLabel, Fillna, RobustZScoreNorm

    handler = Alpha158(
        instruments=instruments,
        start_time=segments["train"][0],
        end_time=segments["test"][1],
        fit_start_time=segments["train"][0],
        fit_end_time=fit_end,
        label=[LABEL],
        infer_processors=[
            RobustZScoreNorm(fit_start_time=segments["train"][0], fit_end_time=fit_end,
                             fields_group="feature", clip_outlier=True),
            Fillna(fields_group="feature", fill_value=0),
        ],
        # DropnaLabel.is_for_infer() 是 False，所以它只作用于 learn 流；infer 流保留全部行
        learn_processors=[DropnaLabel(), CSRankNorm(fields_group="label")],
    )
    return DatasetH(handler, segments=segments)


def dt_inst(series: pd.Series) -> pd.Series:
    """统一成 (datetime, instrument) 两层索引——qlib 不同入口的层序不保证一致，
    层序不同直接 concat 会对不上键、悄悄得到一张全空表。"""
    if set(series.index.names) != {"datetime", "instrument"}:
        raise ValueError(f"意料之外的索引层：{series.index.names}")
    return series.reorder_levels(["datetime", "instrument"]).sort_index()


def as_series(frame) -> pd.Series:
    """col_set='label' 返回的是单列 DataFrame，取第一列成 Series。"""
    return dt_inst(frame.iloc[:, 0] if isinstance(frame, pd.DataFrame) else frame)


def daily_ic_frame(pred: pd.Series, label: pd.Series, min_names: int = 30) -> pd.DataFrame:
    """逐日横截面相关**序列**（以前只回聚合值，配对检验就没有输入可算）。

    一天不足 `min_names` 只就不进序列：十几只票的秩相关噪声极大，留着等于让那几天
    以同等权重拉动整段均值。列名/索引名是落盘格式的一部分，改动会让历史 parquet 读不了。
    """
    frame = pd.concat([dt_inst(pred).rename("score"), dt_inst(label).rename("label")],
                      axis=1).dropna()
    rows = []
    for day, grp in frame.groupby(level="datetime"):
        if len(grp) < min_names:
            continue
        rows.append((day, len(grp),
                     grp["score"].corr(grp["label"], method="pearson"),
                     grp["score"].corr(grp["label"], method="spearman")))
    return pd.DataFrame(rows, columns=["day", "n", "pearson", "spearman"]).set_index("day")


def ic_summary(ic: pd.DataFrame) -> dict:
    """聚合读数。**键名与历史 `run_*.json` 完全一致**，否则新旧批次没法放进一张表比。"""
    if ic.empty:
        return {}
    s = ic["spearman"]
    return {
        "days": int(len(ic)),
        "median_cross_section": float(ic["n"].median()),
        "rank_ic_mean": float(s.mean()),
        "rank_ic_std": float(s.std()),
        "rank_icir_daily": float(s.mean() / s.std()) if s.std() else float("nan"),
        "rank_ic_tstat": float(s.mean() / (s.std() / np.sqrt(len(s)))) if s.std() else float("nan"),
        "pearson_ic_mean": float(ic["pearson"].mean()),
        "share_days_positive": float((s > 0).mean()),
    }


def daily_ic(pred: pd.Series, label: pd.Series) -> dict:
    """逐日横截面相关的聚合值（秩相关是主读数，Pearson 一起给）。"""
    return ic_summary(daily_ic_frame(pred, label))


def dump_ic_daily(dest: Path, run_id: str, ic_valid: pd.DataFrame, ic_test: pd.DataFrame,
                  spread: pd.Series) -> Path:
    """逐日读数落盘成一张宽表：配对检验拿它当输入，不需要重新训练任何一方。"""
    frame = pd.DataFrame(index=sorted(set(ic_valid.index) | set(ic_test.index)))
    for seg, ic in (("valid", ic_valid), ("test", ic_test)):
        frame[f"spearman_{seg}"] = ic["spearman"]
        frame[f"pearson_{seg}"] = ic["pearson"]
        frame[f"n_{seg}"] = ic["n"]
    frame["ls_spread"] = spread if len(spread) else np.nan
    frame.index.name = "day"
    frame.to_parquet(dest / f"ic_daily_{run_id}.parquet")
    return dest / f"ic_daily_{run_id}.parquet"


def dump_predictions(dest: Path, run_id: str, pred: pd.Series) -> Path:
    """测试段逐日逐票的原始打分。有了它，任何新窗口/新分档口径都能离线重算，不必再花 5 分钟
    和 2.9G 内存重训一遍——这是滚动臂攒样本的前提。"""
    frame = dt_inst(pred).rename("score").reset_index()
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
    frame.to_parquet(dest / f"pred_{run_id}.parquet", index=False)
    return dest / f"pred_{run_id}.parquet"


def quintile_table(pred: pd.Series, fwd_ret: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """按当天预测分五档，看真实 5 日收益是否单调——比一个平均 IC 更能说明「能不能用」。

    返回 (分档聚合表, 逐日「最好档 − 最差档」价差序列)。价差序列单独给 t：
    同一天内部相减已经把大盘波动抵消了，用两档各自的标准差合并会**低估**显著性。
    """
    frame = pd.concat([dt_inst(pred).rename("score"), dt_inst(fwd_ret).rename("ret")],
                      axis=1).dropna()
    out = []
    for day, grp in frame.groupby(level="datetime"):
        if len(grp) < 50:
            continue
        q = pd.qcut(grp["score"].rank(method="first"), 5, labels=False)
        means = grp.groupby(q)["ret"].mean()
        for k in range(5):
            if k in means.index:
                out.append((day, k, float(means[k])))
    table = pd.DataFrame(out, columns=["day", "bucket", "ret"])
    if table.empty:
        return table, pd.Series(dtype=float)
    agg = table.groupby("bucket")["ret"].agg(["mean", "std", "count"])
    agg["t"] = agg["mean"] / (agg["std"] / np.sqrt(agg["count"]))
    wide = table.pivot(index="day", columns="bucket", values="ret")
    spread = (wide[4] - wide[0]).dropna()
    return agg, spread


def t_stat(series: pd.Series) -> float:
    """均值对 0 的 t 值；序列长度就是样本量。"""
    if len(series) < 2:
        return float("nan")
    sd = float(series.std())
    return float(series.mean() / (sd / np.sqrt(len(series)))) if sd else float("nan")


def forward_returns(qlib, instruments: list[str], beg: str, end: str) -> pd.Series:
    """直接从 bin 里取收盘价算 T+1→T+5 的真实收益，用于分层表（绕开 CSRankNorm 后的标签）。"""
    close = dt_inst(qlib.data.D.features(instruments, ["$close"], start_time=beg,
                                        end_time=end)["$close"])
    grp = close.groupby(level="instrument", group_keys=False)
    fwd = grp.shift(-5) / grp.shift(-1) - 1.0
    return fwd.dropna().rename("ret")


def run_backtest(qlib, signal: pd.Series, beg: str, end: str, topk: int, n_drop: int,
                 benchmark: str) -> dict:
    from qlib.contrib.evaluate import backtest_daily
    from qlib.contrib.strategy import TopkDropoutStrategy

    # qlib 每个决策日要取「下一根 K 线」当区间右端点（backtest/utils.py:131
    # `self._calendar[calendar_index + 1]`），结束日撞上日历最后一天就 IndexError。
    # 所以最后一个交易日不参与撮合（它的分数只进选股名单），读数里如实记下实际结束日。
    days = [str(d)[:10] for d in qlib.data.D.calendar(start_time=beg, end_time=end)]
    if len(days) < 2:
        return {"insufficient_calendar": len(days)}
    end = days[-2]

    strategy = TopkDropoutStrategy(topk=topk, n_drop=n_drop, signal=signal, only_tradable=False)
    exchange_kwargs = {
        "freq": "day",
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5.0,
        # 9.5% 当作涨跌停：主板 10% 的票一字板买不进/卖不掉；30x/68x 的 20% 票也被一并挡住，
        # 宁可少成交也不给自己造假成交
        "limit_threshold": 0.095,
        "deal_price": "close",
    }
    capital = 1e8
    report, _positions = backtest_daily(start_time=beg, end_time=end, strategy=strategy,
                                        account=capital, benchmark=benchmark,
                                        exchange_kwargs=exchange_kwargs)
    if report is None or report.empty:
        return {"empty_report": True}
    # 净值必须走 report['account']（现金+持仓市值，手续费已扣）。
    # report['return'] 是**扣成本前**的毛收益：qlib 源码 backtest/account.py:283 写的是
    # `return_rate = (earning + cost) / last_account_value`，把当天手续费又加了回去。
    # 本策略日换手约 30%，一年手续费 ~12 个百分点——毛/净差掉的就是整年赚头。
    if "account" not in report.columns:
        return {"missing_account_column": list(report.columns)}
    values = report["account"].astype(float).dropna().sort_index()
    if values.empty:
        return {"empty_account": True}
    nav = values / capital
    daily = nav.pct_change().fillna(0.0)
    gross = report["return"].dropna() if "return" in report.columns else pd.Series(dtype=float)
    gross_nav = (1.0 + gross).cumprod()
    years = len(daily) / 243.0
    ann = float(nav.iloc[-1] ** (1 / years) - 1) if years > 0 else float("nan")
    dd = float((nav / nav.cummax() - 1).min())
    ann_gross = float(gross_nav.iloc[-1] ** (1 / years) - 1) if years > 0 and len(gross_nav) else None
    dd_gross = float((gross_nav / gross_nav.cummax() - 1).min()) if len(gross_nav) else None
    excess_ann = None
    bench = report["bench"].dropna() if "bench" in report.columns else None
    if bench is not None and len(bench):
        bnav = (1 + bench).cumprod()
        bann = float(bnav.iloc[-1] ** (1 / (len(bench) / 243.0)) - 1)
        excess_ann = ann - bann
    turnover = report["turnover"] if "turnover" in report.columns else None
    cost = report["cost"] if "cost" in report.columns else None
    # 逐年拆开看：门槛 B 预注册要求「留出段各年度中位数 > 0」，只给整体值就是没验收
    bench_all = report["bench"] if "bench" in report.columns else None
    stamp = pd.DatetimeIndex(pd.to_datetime(daily.index))
    yearly = {}
    for year, grp in daily.groupby(stamp.year):
        strat = float((1 + grp).prod() - 1)
        bench = None
        if bench_all is not None:
            b = bench_all.reindex(grp.index).dropna()
            if len(b):
                bench = float((1 + b).prod() - 1)
        yearly[str(int(year))] = {
            "days": int(len(grp)),
            "strategy_return": round(strat, 4),  # 净值口径（已扣成本）
            "benchmark_return": round(bench, 4) if bench is not None else None,
            "excess": round(strat - bench, 4) if bench is not None else None,
        }
    excesses = [v["excess"] for v in yearly.values() if v["excess"] is not None]
    return {
        "report_columns": list(report.columns),
        "window": [beg, end],
        "days": int(len(daily)),
        "annual_return": round(ann, 4),
        "max_drawdown": round(dd, 4),
        "annual_return_gross_of_cost": None if ann_gross is None else round(ann_gross, 4),
        "max_drawdown_gross_of_cost": None if dd_gross is None else round(dd_gross, 4),
        "final_account_value": round(float(values.iloc[-1]), 2),
        "benchmark": benchmark,
        "yearly": yearly,
        "yearly_excess_median": round(float(np.median(excesses)), 4) if excesses else None,
        "annual_excess_vs_benchmark": round(excess_ann, 4) if isinstance(excess_ann, float) else None,
        "daily_win_rate": round(float((daily > 0).mean()), 4),
        "total_return": round(float(nav.iloc[-1] - 1), 6),
        "total_return_gross_of_cost": (None if len(gross_nav) == 0
                                       else round(float(gross_nav.iloc[-1] - 1), 6)),
        "mean_daily_cost": round(float(cost.mean()), 6) if cost is not None else None,
        "mean_daily_turnover": round(float(turnover.mean()), 4) if turnover is not None else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qlib-dir", default=str(QLIB_DIR))
    ap.add_argument("--universe", type=int, default=300, help="训练段流动性取前 N 只股票")
    ap.add_argument("--pool-file", default="",
                    help="冻结票池：一行一个 symbol（或 instruments 的三列格式）。给了它就不重挑池，"
                         "滚动重训之间才可比；池只 300 只是内存上限，见模块头说明")
    ap.add_argument("--inst-name", default="",
                    help="写 instruments/<name>.txt 的名字，缺省 liq<池大小>；跑研究臂时务必另起名，"
                         "别覆盖线上实盘那份")
    ap.add_argument("--seed", type=int, default=None,
                    help="LightGBM 种子。滚动臂之间要配对就得消除抽样噪声，缺省不设=与历史批次同口径")
    ap.add_argument("--train-beg", default="2016-01-01")
    ap.add_argument("--train-end", default="2021-12-31")
    ap.add_argument("--valid-beg", default="2022-01-01")
    ap.add_argument("--valid-end", default="2022-12-31")
    ap.add_argument("--test-beg", default="2023-01-01")
    ap.add_argument("--test-end", default="2026-10-16")
    ap.add_argument("--smoke", action="store_true", help="200 只 + 短窗，只验管道")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--n-drop", type=int, default=5)
    ap.add_argument("--benchmark", default="SH510300",
                    help="基准：面板里唯一贯穿 2015-12→2026-10 的大盘代理。"
                         "399xxx 指数只有 2025-01 之后的 419 行，2021/2023 窗口直接无数据")
    ap.add_argument("--to-mongo", action="store_true", help="把最新选股名单 upsert 进 qlib_top_stocks")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--export-model", metavar="DIR", nargs="?", const="", default=None,
                    help="训练完把 booster+稳健标准化统计+票池落盘，给 qlib 插件（8003）只读；"
                         "不带 DIR 时写默认目录 data/qlib/model")
    ap.add_argument("--write-live-model", action="store_true",
                    help="允许把产物写进插件正在读的 data/qlib/model（等于当场换掉线上模型）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.smoke:
        args.universe, args.train_beg, args.train_end = 120, "2019-01-01", "2020-12-31"
        args.valid_beg, args.valid_end, args.test_beg, args.test_end = "2021-01-01", "2021-06-30", "2021-07-01", "2021-12-31"
    run_id = args.run_id or pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stock_selector_qlib import artifacts

    export_dest: Path | None = None
    if args.export_model is not None:
        export_dest = Path(args.export_model) if args.export_model else artifacts.MODEL_DIR
        # 8003 每次调用都重读 data/qlib/model ⇒ 写进去就等于当场换掉线上模型，连 refresh 都不用。
        # 研究臂必须写到别的目录，所以这条要在训练**之前**拦，别花完 5 分钟才拒绝。
        if export_dest.resolve() == artifacts.MODEL_DIR.resolve() and not args.write_live_model:
            raise SystemExit(
                f"--export-model 指向实盘模型目录 {artifacts.MODEL_DIR}：研究臂请另给一个 DIR，"
                "确实要把线上模型换掉才加 --write-live-model")

    # qlib 的 LGBModel.fit 会走 R.log_metrics → mlflow 实验；新版 mlflow 默认拒绝
    # 文件型 backend（"maintenance mode"），不设这个开关就直接抛异常、根本训不完。
    # 实验目录固定到 data/qlib/mlruns，免得在工作目录里长出 mlruns。
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=args.qlib_dir, region=REG_CN, kernels=1, expression_cache=None,
              dataset_cache=None, uri=str(REPORT_DIR / "mlruns"))
    calendar = [str(d)[:10] for d in qlib.data.D.calendar()]
    print(f"qlib 日历 {len(calendar)} 天：{calendar[0]} → {calendar[-1]}")

    liquid_end = min(args.train_end, calendar[-1])
    if args.pool_file:
        pool = read_pool_file(Path(args.pool_file))
        print(f"票池：{len(pool)} 只（冻结自 {args.pool_file}，本次不重挑）")
    else:
        symbols = stock_symbols()
        print(f"面板里的股票（剔基金/指数/B股/北交所）：{len(symbols)} 只")
        pool = liquid_universe(symbols, args.train_beg, liquid_end, args.universe)
        print(f"票池：{len(pool)} 只（{args.train_beg}→{liquid_end} 日均成交额前 {args.universe}）")
    inst_file = write_instruments(Path(args.qlib_dir), args.inst_name or f"liq{len(pool)}", pool)
    print(f"票池 → {inst_file}")

    segments = {
        "train": (args.train_beg, args.train_end),
        "valid": (args.valid_beg, args.valid_end),
        "test": (args.test_beg, args.test_end),
    }
    # 用落盘文件的实际名（已小写），不要用传进来的 --inst-name 原样
    dataset = build_dataset(qlib, inst_file.stem, liquid_end, segments)

    from qlib.contrib.model.gbdt import LGBModel

    gbdt_kwargs = {} if args.seed is None else {"seed": args.seed}
    model = LGBModel(loss="mse", learning_rate=0.04, num_leaves=128, max_depth=-1,
                     colsample_bytree=0.6, subsample=0.7, reg_alpha=10.0, reg_lambda=10.0,
                     n_estimators=600, num_thread=4, verbose=-1, **gbdt_kwargs)
    print(f"训练 {segments['train']} / 验证 {segments['valid']} / 测试 {segments['test']}")
    model.fit(dataset)

    if export_dest is not None:
        # 插件进程只加载最近 130 天，归一化统计必须从这次训练里带出来：
        # RobustZScoreNorm 是在已加载的那段面板上算 median/MAD 的，短窗重算的不是同一个模型
        from stock_selector_qlib.export import export_from_dataset

        dest = export_from_dataset(model, dataset, pool, {
            "run_id": run_id,
            "train_end": args.train_end,
            "segments": {k: [str(x) for x in v] for k, v in segments.items()},
            "label": LABEL,
            "universe": args.universe,
            "pool_size": len(pool),
            "pool_file": args.pool_file,
            "seed": args.seed,
            "qlib_version": getattr(qlib, "__version__", ""),
            "source": "scripts/qlib_run.py",
        }, dest=export_dest)
        print(f"模型产物落盘：{dest}（{len(pool)} 只票池，插件读这一份）")

    from qlib.data.dataset.handler import DataHandlerLP

    # DK_L = 经过 learn_processors（含 DropnaLabel）的那份，标签才是 CSRankNorm 之后的排名值；
    # 秩相关对单调变换不敏感，所以拿它算 IC 是安全的，但跨段比大小要记得这不是原始收益
    label_test = as_series(dataset.prepare("test", col_set="label", data_key=DataHandlerLP.DK_L))
    label_valid = as_series(dataset.prepare("valid", col_set="label", data_key=DataHandlerLP.DK_L))
    pred_test = dt_inst(model.predict(dataset, segment="test"))
    pred_valid = dt_inst(model.predict(dataset, segment="valid"))
    ic_valid = daily_ic_frame(pred_valid, label_valid)
    ic_test = daily_ic_frame(pred_test, label_test)
    report = {
        "run_id": run_id,
        "config": {k: v for k, v in vars(args).items() if k not in ("run_id",)},
        "pool_size": len(pool),
        "pool_file": args.pool_file,
        "label_formula": LABEL,
        "ic_valid": ic_summary(ic_valid),
        "ic_test": ic_summary(ic_test),
    }
    print(json.dumps(report["ic_valid"], indent=2, ensure_ascii=False))
    print(json.dumps(report["ic_test"], indent=2, ensure_ascii=False))

    fwd = forward_returns(qlib, pool, args.test_beg, args.test_end)
    table, spread = quintile_table(pred_test, fwd)
    report["ic_daily_file"] = str(dump_ic_daily(REPORT_DIR, run_id, ic_valid, ic_test, spread))
    report["pred_file"] = str(dump_predictions(REPORT_DIR, run_id, pred_test))
    print(f"逐日序列落盘：{report['ic_daily_file']} / {report['pred_file']}")
    if not table.empty:
        report["quintile_test"] = table.round(5).to_dict("index")
        report["quintile_long_short_pooled_t"] = float(
            (table.loc[4, "mean"] - table.loc[0, "mean"]) /
            ((table.loc[4, "std"] / np.sqrt(table.loc[4, "count"])) +
             (table.loc[0, "std"] / np.sqrt(table.loc[0, "count"]))))
        report["quintile_long_short_daily_mean_pct"] = round(float(spread.mean() * 100), 4)
        report["quintile_long_short_paired_t"] = t_stat(spread)
        print("五分档真实 5 日收益（档位 0=预测最差 → 4=预测最好）：")
        print(table)
        print(f"最好档 − 最差档：合并 t {report['quintile_long_short_pooled_t']:.2f} / "
              f"同日配对 t {report['quintile_long_short_paired_t']:.2f}"
              f"（逐日均值 {report['quintile_long_short_daily_mean_pct']:.3f}%，"
              f"{len(spread)} 天）")

    if args.backtest:
        # 回测是最容易整段崩掉的一步，先把已达门槛 A 的读数落一次盘，别一起丢
        (REPORT_DIR / f"run_{run_id}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
        report["backtest"] = run_backtest(qlib, pred_test, args.test_beg, args.test_end,
                                         args.topk, args.n_drop, args.benchmark)
        print("回测（TopkDropout：qlib 在 T 日读 T−1 日信号、按 T 日收盘成交，含成本与涨跌停）：")
        print(json.dumps(report["backtest"], indent=2, ensure_ascii=False))

    # 最新一个交易日的选股名单：用训练段之外的模型给分，特征只到当日收盘
    latest = calendar[-1]
    day_ts = pd.Timestamp(latest)
    # xs 剥掉 datetime 层，留下 instrument 单层索引；如果直接用布尔掩码筛，
    # .items() 会吐出 (datetime, instrument) 元组，下游 parquet 写入当场炸类型
    if (pred_test.index.get_level_values("datetime") == day_ts).any():
        tail = pred_test.xs(day_ts, level="datetime")
    else:
        # --smoke 的测试段止于 2021-12-31，日历最后一天根本没有分数
        tail = pd.Series(dtype=float)
    if len(tail):
        top = tail.sort_values(ascending=False).head(args.topk)
        selections = [{"code": str(inst)[2:], "qlib_symbol": str(inst),
                       "score": round(float(score), 6)} for inst, score in top.items()]
        report["selection_date"] = latest
        report["selection"] = selections
        pd.DataFrame(selections).to_parquet(REPORT_DIR / f"selection_{latest}.parquet",
                                            index=False)
        print(f"{latest} 选股前 10：{[s['code'] for s in selections[:10]]}")
        if args.to_mongo:
            import pymongo

            db = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=8000)["eastmoney_news"]
            db["qlib_top_stocks"].update_one(
                {"date": latest, "model_id": run_id},
                {"$set": {"date": latest, "model_id": run_id, "model_type": "lgbm",
                          "factor": "alpha158", "stocks": json.dumps(selections),
                          "created_at": pd.Timestamp.now().to_pydatetime()}},
                upsert=True)
            print(f"已写 Mongo qlib_top_stocks（date={latest}, model_id={run_id}）")

    (REPORT_DIR / f"run_{run_id}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    print(f"读数落档：{REPORT_DIR / f'run_{run_id}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
