"""h5榜 vs h10榜 同窗配对：同一 300 池、同一 72 个信号日（07-01→10-16）、
同一「T+1 收盘买 → T+H 收盘卖」口径（entry=next，两臂同式），只比头部 30 名的
真实实现收益（不用各自标签）。t 按 §11 口径：H 日重叠 ⇒ 除 √H。"""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, qlib
from qlib.data import D

qlib.init(provider_uri="/data/work/qlib_data/cn", verbose=False)

def top30_arm(path):
    d = pd.read_parquet(path)[["datetime","instrument","score"]]
    d = d.sort_values(["datetime","score"], ascending=[True, False])
    g = d.groupby("datetime", sort=False).head(30)
    return {(r.datetime, r.instrument): i for i, r in enumerate(g.itertuples())}, g

_, top5 = top30_arm("data/qlib/pred_batchP_h5.parquet")
_, top10 = top30_arm("data/qlib/pred_batchP_h10.parquet")
inst_all = sorted(set(top5["instrument"].tolist()))
cal = D.calendar(freq="day")
idx = {pd.Timestamp(c): i for i, c in enumerate(cal)}
beg = min(idx[pd.Timestamp(x)] for x in top5["datetime"].unique())
end = min(beg + 95, len(cal) - 1)
close = None
calf = [pd.Timestamp(c).date() for c in cal[beg:end+1]]
look = {c: i for i, c in enumerate(calf)}
b_s, e_s = str(calf[0]), str(calf[-1])
pool_close = {}
for i0 in range(0, len(inst_all), 60):          # 300 只一把抓会掉日期，分片取（qlib 内存缓存怪癖）
    ser = D.features(inst_all[i0:i0+60], ["$close"], start_time=b_s, end_time=e_s)["$close"]
    for (inst, dt), v in ser.items():
        pool_close[(inst, pd.Timestamp(dt))] = v

def arm_ret(day, k, arm):
    rows = arm[arm.datetime == day]
    out = []
    for r in rows.itertuples():
        i = look.get(pd.Timestamp(day).date())
        if i is None or i + k + 1 >= len(calf):
            continue
        t1, tk = pd.Timestamp(calf[i+1]), pd.Timestamp(calf[i+k+1])
        c1, ck = pool_close.get((r.instrument, t1)), pool_close.get((r.instrument, tk))
        if c1 and c1 > 0 and ck and ck > 0:
            out.append(ck / c1 - 1)
    return out

def paired(k):
    days = sorted(set(top5["datetime"].unique()))
    d5, d10, dd = [], [], []
    for day in days:
        a5, a10 = arm_ret(day, k, top5), arm_ret(day, k, top10)
        if len(a5) >= 25 and len(a10) >= 25:
            d5.append(np.mean(a5)); d10.append(np.mean(a10)); dd.append(np.mean(a5) - np.mean(a10))
    dd = np.array(dd); n = len(dd)
    return n, (np.mean(d5)*100), (np.mean(d10)*100), np.mean(dd)*100, \
           np.mean(dd)/(dd.std(ddof=1)/np.sqrt(n))*np.sqrt(k), np.mean(dd)/(dd.std(ddof=1)/np.sqrt(n))*np.sqrt(np.sqrt(k))

for k in (5, 10):
    n, r5, r10, diff, t_adj, t_real = paired(k)
    print(f"H={k} 配对天 {n}  h5榜均值 {r5:+.3f}%/笔  h10榜均值 {r10:+.3f}%/笔  "
          f"差 {diff:+.3f}pp  t(÷√{k}) {t_adj:+.2f}")
