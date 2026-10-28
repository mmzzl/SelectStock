"""全臂头 30 同尺读数（向量化）。每臂用自己可执行的 leg：
  next: T+1收盘买→T+H收盘卖 / close: T收盘买→T+H收盘卖 / open: T+1开盘买→T+1收盘卖
超额 = 头30均值 − 当日本池(pred 自带 300 支)等权；t=逐日差 t ÷√H（§11）；毛益=扣费前。
"""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, qlib
from qlib.data import D

qlib.init(provider_uri="/data/work/qlib_data/cn")

ARMS = {
    "P_h5(现线上)": ("pred_batchP_h5", "next", 5, ("2026-07-01", "2026-10-16")),
    "P_h10(备份)":  ("pred_batchP_h10", "next", 10, ("2026-07-01", "2026-10-16")),
    "U_c1": ("pred_batchU_c1", "close", 1, ("2026-07-01", "2026-10-16")),
    "U_c3": ("pred_batchU_c3", "close", 3, ("2026-07-01", "2026-10-16")),
    "W_c1": ("pred_batchW_c1", "close", 1, ("2024-01-02", "2026-10-23")),
    "Wx_c1": ("pred_batchWx_c1", "close", 1, ("2024-01-02", "2026-10-23")),
    "W_c3": ("pred_batchW_c3", "open", 1, ("2024-01-02", "2026-10-23")),
    "Y_c4(方向)": ("pred_batchY_c4", "open", 1, ("2024-01-02", "2026-10-23")),
    "W_h10": ("pred_batchW_h10", "next", 10, ("2024-01-02", "2026-10-23")),
}
WIN_S, WIN_E, WIN_COMMON = "2024-01-02", "2026-10-28", ("2026-07-01", "2026-10-16")

inst_all = sorted({i for pre, *_ in ARMS.values()
                   for i in pd.read_parquet(f"data/qlib/{pre}.parquet")["instrument"].unique()})
cal = D.calendar(freq="day")
cal_ts = [pd.Timestamp(c) for c in cal]
cidx = {c: i for i, c in enumerate(cal_ts)}
# 面板收拢成 (交易日 × 代码) 两张矩阵
cl_mat = np.full((len(cal), len(inst_all)), np.nan)
op_mat = np.full((len(cal), len(inst_all)), np.nan)
inst_i = {c: j for j, c in enumerate(inst_all)}
for i0 in range(0, len(inst_all), 60):          # qlib 300 只一把抓会掉日期，分片取
    df = D.features(inst_all[i0:i0+60], ["$close", "$open"], start_time=WIN_S, end_time=WIN_E)
    for (inst, dt), row in zip(df.index, df.values):
        cl_mat[cidx[pd.Timestamp(dt)], inst_i[inst]] = row[0]
        op_mat[cidx[pd.Timestamp(dt)], inst_i[inst]] = row[1]
print(f"[面板] {len(inst_all)} 支 / {int(np.isfinite(cl_mat).sum())} 个值", flush=True)

DIP_FILE = "data/qlib/pred_batchP_h5.parquet"   # 日期样板行没有意义，只是占位提醒

def load(pre):
    d = pd.read_parquet(f"data/qlib/{pre}.parquet", columns=["datetime", "instrument", "score"])
    d["dt"] = pd.to_datetime(d["datetime"])
    return d

def head_stats(pre, entry, k, win):
    d = load(pre)
    d = d[(d.dt >= pd.Timestamp(win[0])) & (d.dt <= pd.Timestamp(win[1]))]
    if not len(d):
        return None
    d = d.sort_values(["dt", "score"], ascending=[True, False])
    d["ri"] = d.dt.map(cidx)
    if entry == "close":
        d["bi"], d["si"] = d.ri, d.ri + k
    else:
        d["bi"], d["si"] = d.ri + 1, d.ri + 1 + k
    d = d[d.si < len(cal) - 1]
    inst_j = d.instrument.map(inst_i)
    def px(d_, mat, col):
        return np.array([mat[i, j] for i, j in zip(d_[col].to_numpy(), inst_j.to_numpy())])
    buy = px(d, cl_mat, "bi") if entry != "open" else px(d, op_mat, "bi")
    sell = px(d, cl_mat, "si")
    ok = (buy > 0) & (sell > 0)
    d = d[ok]
    r = sell[ok] / buy[ok] - 1.0
    d = d.assign(ret=r)
    # 当日全池（本臂 pred 全体 300 支）等权
    pg = d.groupby("dt")["ret"].mean().rename("pool_mean").to_frame()
    head = d.groupby("dt").head(30)
    per_day = head.groupby("dt")["ret"].agg(head_mean="mean", head_wr=lambda x: (x > 0).mean())
    m = pg.join(per_day, how="inner")
    ex = m.head_mean - m.pool_mean
    if len(ex) < 5:
        return None
    t_raw = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
    return dict(n=len(ex), gross=m.head_mean.mean() * 100, exc=ex.mean() * 100,
                t=t_raw / np.sqrt(k), wr=m.head_wr.mean() * 100)

print("臂            |    全窗（各臂自己的留出段）                          |    公共窗 2026-07-01→10-16", flush=True)
for name, (pre, entry, k, own) in ARMS.items():
    a, b = head_stats(pre, entry, k, own), head_stats(pre, entry, k, WIN_COMMON)
    def s(x):
        return f"n={x['n']:4d} 毛 {x['gross']:+.3f}%/笔 超 {x['exc']:+.3f}pp t(÷√{k}) {x['t']:+.2f} 红 {x['wr']:.1f}%" if x else "—"
    print(f"{name:14s} | {s(a):<52s} | {s(b)}", flush=True)
print("[注] 跨臂比同窗超额；leg 口径每臂不同（close=今天收盘买、open=明早开盘买、next=明天收盘买）。")
