"""批次 D 新输入（横截面分位 + 市场状态）的口径测试。

这批列是「换输入」，不是「调参」，所以钉的全是定义性的事：
1. **分位分母是组内有效值个数 + 1**，NaN 既不占位也不进分母，否则缺数据的票会把全天的分位压低；
2. **板块分位只在同板块内排**（板块真源 `bucket.classify_board`），跨板块排就等于没排；
3. **市场状态列不许有未来函数**：第 t 天的 `mkt_vol20` 只能由 ≤t 的中位序列算出来；
4. **票池与批 C 逐项一致**：同一个 `_read`、同一个 ≥250 根门槛、同一句 ST 判定、同一个 `--to`。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cs():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load("build_cross_section", "scripts/build_cross_section.py")


def day_frame():
    """三天四票的小长表：第 1 天有一票 pos60 缺值，第 2 天同板块两只打平（测平均名次与分母）。"""
    rows = []
    for day in (0, 1, 2):
        for code, board, pos in [("000001", 0, 0.1), ("000002", 0, 0.5),
                                 ("300001", 1, 0.9), ("300002", 1, 0.7)]:
            if day == 1 and code == "300002":
                pos = np.nan
            if day == 2 and code in ("300001", "300002"):
                pos = 0.9                      # 打平：名次取平均 3.5
            rows.append({"day": day, "code": code, "board": board, "bias20": pos,
                         "pos60": pos, "vol_ratio": 1.0, "vol_z": 0.0,
                         "pct": 1.0 if code in ("000001", "300001") else -1.0})
    return pd.DataFrame(rows)


def test_pct_rank_uses_valid_count_plus_one(cs):
    df = day_frame()
    r = cs.pct_rank(df["pos60"], df["day"])
    d0 = df["day"].to_numpy() == 0
    assert sorted(r[d0]) == pytest.approx([0.2, 0.4, 0.6, 0.8])       # 4 个有效值 ⇒ 分母 5
    d1 = df["day"].to_numpy() == 1
    # 第 1 天有一个 NaN ⇒ 分母跟着降到 4，且 NaN 那行留 NaN（不是当 0 排进去）
    assert np.isnan(r[d1]).sum() == 1
    assert sorted(r[d1][~np.isnan(r[d1])]) == pytest.approx([0.25, 0.5, 0.75])


def test_pct_rank_ties_take_average(cs):
    df = day_frame()
    r = cs.pct_rank(df["pos60"], df["day"])
    d2 = df["day"].to_numpy() == 2
    # 第 2 天两只 0.9 打平（名次 3.5/3.5），4 个有效 ⇒ 3.5/5
    assert r[d2].tolist() == pytest.approx([0.2, 0.4, 0.7, 0.7])


def test_board_rank_only_inside_board(cs):
    df = day_frame()
    board_key = df["board"].astype(str) + "_" + df["day"].astype(str)
    r = cs.pct_rank(df["bias20"], board_key)
    d0 = (df["day"].to_numpy() == 0) & (df["board"].to_numpy() == 0)
    # 主板两只内部排 ⇒ 分母 3
    assert sorted(r[d0]) == pytest.approx([1 / 3, 2 / 3])


def test_market_vol20_is_causal(cs):
    df = day_frame()
    out = cs.market_daily(df)
    assert len(out) == 3
    assert out["mkt_vol20"].isna().all()          # 不足 20 行按口径留空
    # 未来行的涨跌幅改了，第 0/1 天的读数不许动（把第 2 天灌满再验一次）
    big = pd.concat([df] + [df.assign(day=df["day"] + 3 * i) for i in range(1, 8)],
                    ignore_index=True)
    base = cs.market_daily(big)
    poison = big.copy()
    last = poison["day"] == poison["day"].max()
    poison.loc[last, "pct"] = 99.0
    again = cs.market_daily(poison)
    early = again["day"] < again["day"].max()
    assert np.allclose(base.loc[early, "mkt_vol20"].to_numpy(dtype=float),
                       again.loc[early, "mkt_vol20"].to_numpy(dtype=float), equal_nan=True)


def test_market_daily_breadth(cs):
    out = cs.market_daily(day_frame())
    assert out["mkt_up_ratio"].tolist() == pytest.approx([0.5, 0.5, 0.5])
    assert out["mkt_med_pct"].tolist() == pytest.approx([0.0, 0.0, 0.0])
    assert out["mkt_n"].tolist() == [4, 4, 4]        # 分位分母会变，家数计数不会


def _write_kline(dir_path: Path, code: str, d: pd.DataFrame) -> None:
    pq.write_table(pa.Table.from_pandas(d.assign(code=code), preserve_index=False),
                   dir_path / f"{code}.parquet")


def _mk_stock(n, start="2015-06-01", **over):
    date = pd.date_range(start, periods=n, freq="D")
    i = np.arange(n, dtype=float)
    d = {"date": date.to_numpy(), "name": "测试票", "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100 + i, "volume": np.full(n, 1e6),
         "amount": np.full(n, 5e7), "pct": np.full(n, 1.0), "last_close": 99 + i,
         "ma20": 100 + i, "pos60": (i % 100) / 100.0, "vol_ratio": np.full(n, 1.0),
         "vol_z": np.zeros(n), "rsi14": np.full(n, 50.0), "adx14": np.full(n, 20.0),
         "atr14": np.full(n, 2.0), "lu": np.zeros(n, dtype=bool)}
    d.update(over)
    return pd.DataFrame(d)


def _day_dates(long: pd.DataFrame) -> pd.Series:
    """day 列是 epoch-day 序号，测试里换回日期才看得出砍在哪一行。"""
    return pd.to_datetime(long["day"], unit="D")


def test_collect_matches_batch_c_pool(cs, tmp_path):
    # 一只不足 250 根、一只刚好够、一只 ST；ST 判定看的是文件最后一行的现用简称
    _write_kline(tmp_path, "000001", _mk_stock(240))
    full = _mk_stock(300)
    _write_kline(tmp_path, "000002", full)
    _write_kline(tmp_path, "000003", full.assign(name="ST Removed"))
    long = cs.collect(sorted(tmp_path.glob("*.parquet")), min_tenure=250,
                      to_ns=np.datetime64("2017-01-01"))
    assert "000003" not in set(long["code"])                 # ST 整只跳过
    got = long.groupby("code")["day"].count()
    assert "000001" not in got.index                        # 240 根连门槛都不过
    assert got["000002"] == 51                               # 第 250 根起才进池，前面不是补 0 而是没这行
    assert _day_dates(long).min() == pd.Timestamp(full["date"].to_numpy()[249])


def test_collect_cuts_at_to(cs, tmp_path):
    _write_kline(tmp_path, "000001", _mk_stock(400))
    long = cs.collect(sorted(tmp_path.glob("*.parquet")), min_tenure=1,
                      to_ns=np.datetime64("2015-12-31"))
    assert _day_dates(long).max() == pd.Timestamp("2015-12-31")   # --to 之后的 bar 一栏都不碰


def test_build_outputs_schema_and_range(cs):
    xs, mkt = cs.build(day_frame())
    assert list(xs.columns) == ["date", "code", *cs.XS_COLUMNS]
    assert list(mkt.columns) == ["date", *cs.MKT_COLUMNS]
    assert len(xs) == 12 and len(mkt) == 3
    vals = xs[cs.XS_COLUMNS].to_numpy(dtype=float)
    ok = ~np.isnan(vals)
    assert (vals[ok] > 0).all() and (vals[ok] < 1).all()     # rank/(n+1) 严格落在开区间
    assert xs["date"].dt.strftime("%Y-%m-%d").nunique() == 3
    # 每票每天只有一行，join 才不会把决策行放大
    assert not xs.duplicated(["date", "code"]).any()
