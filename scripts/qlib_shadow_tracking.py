"""qlib 影子盘：每日纸面组合追踪（线上臂 vs 纸面反面尺）。

预注册依据：《qlib风格与标签周期验证_20261025.md》换线门槛条文第 2 条 + 2026-10-26 拍板记录。
- 臂（写死，改动 = 重新预注册）：online_p10_r015 = batchP_h10@r0.15（10-26 拍板起的线上，
  对照基准）、paper_q10_r035（旧线上 Q-lab10@r0.35 下线上纸面，反面尺）、
  p10_r010（深回撤偏好档留观）。
- 影子起点 2026-10-23（首轮 10-22 的分数直接取自各臂全市场复检面板做种子）。
- 每晚 17:35（qlib_update_bin 17:15 之后）：补新交易日分数 → 重建护栏信号 →
  从影子起点**全窗重放**回测（确定性，无外部状态）；分数缓存只追加。
- 特征每个新交易日只取一次，各模型各自 normalize+predict（共享 raw 面板）；
  行过滤按各臂 artifact 自身列数算 NaN（base 三臂 = 158 列，与种子面板产出口径一致）。
- 读数输出 data/qlib/shadow_20261025/：shadow_<日期>.json + 各臂 <arm>.parquet/<arm>.json。
- 判定（10-26 拍板时写死）：影子期（≥3 个月）纸面 Q@r0.35 若对新线上同时
  「区间年化超额领先 ≥3pp 且最大回撤浅 ≥2pp」→ 回滚 Q-lab10；否则持有新配置。

用法：python3 -u scripts/qlib_shadow_tracking.py [--arms online_p10_r015,paper_q10_r035,…]
      [--limit-symbols N --start 2026-10-23]   # 冒烟小票池限时窗
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import qlib_fullmarket_validation as fv  # noqa: E402
from qlib_daily_universe import panel_calendar, panel_stocks  # noqa: E402
from stock_selector_qlib import artifacts, serving  # noqa: E402
import stock_selector_qlib.backtest as bt  # noqa: E402
from stock_selector_qlib.backtest import run_qlib_backtest  # noqa: E402

SHADOW_DIR = REPO / "data" / "qlib" / "shadow_20261025"
SHADOW_BEG = "2026-10-23"
SHADOW_BEG_PREV = "2026-10-22"          # 首个交易日排名用的 T−1 分数日
TOPK, ENTRY, EXITR = 20, 50, 300
CAPITAL = 100_000_000.0
BENCH = "SH510300"

# 各臂（2026-10-26 拍板换线后反转，改动 = 重新预注册）：
#   线上 = batchP_h10（10-26 07:20 拷入，§同日拍板记录）@ r0.15（复检甜点档，后端缺省同步）；
#   旧线上 batchQ_lab10（10-19→10-26）下线进纸面 @ 原 r0.35 档，作影子反面尺；
#   p10_r010 = 深回撤偏好档留观。seed = 各模型自己全市场复检面板的首日 T−1 分数。
#   两只模型缓存（scores_batchP_h10 / scores_batchQ_lab10）换线前已各暖到 2026-10-23。
ARMS: dict[str, dict] = {
    "online_p10_r015": {"dir": artifacts.MODEL_DIR, "risk": 0.15,
                        "seed": REPO / "data/qlib/fullmarket_validation_h10_20261025_scores.parquet"},
    "paper_q10_r035": {"dir": REPO / "data/qlib/model_batchQ_lab10", "risk": 0.35,
                       "seed": REPO / "data/qlib/fullmarket_validation_20261025_scores.parquet"},
    "p10_r010": {"dir": REPO / "data/qlib/model_batchP_h10", "risk": 0.10,
                 "seed": REPO / "data/qlib/fullmarket_validation_h10_20261025_scores.parquet"},
}


def _need_list(days: list[str]) -> list[str]:
    return [d for d in days if SHADOW_BEG_PREV <= d]


def load_art(dir_):
    return artifacts.load(dir_)


def run_id_of(art) -> str:
    rid = getattr(art, "run_id", None) or str(art.meta.get("run_id"))
    return rid or "unknown"


def ensure_scores(art, union: list[str], days: list[str], seed_path: Path | None,
                  upto_idx: dict[str, int], limit: int) -> pd.Series:
    """某模型的影子期分数缓存：缺哪天补哪天；特征按日取一次，Q 后各臂各自归一化打分。"""
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    cache = SHADOW_DIR / (f"scores_{run_id_of(art)}.parquet" if not limit
                          else f"scores_{run_id_of(art)}_smoke.parquet")
    if cache.exists():
        f = pd.read_parquet(cache)
        s = f.iloc[:, 0]
    elif seed_path is not None and seed_path.exists():
        f = pd.read_parquet(seed_path)
        s = f[f.columns[0]]
        s = s[s.index.get_level_values(0) >= SHADOW_BEG_PREV]
        print(f"  种子 {len(s):,} 行", flush=True)
    else:
        s = pd.Series(dtype="float64", name="score",
                      index=pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"]))
    have = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in s.index.get_level_values(0).unique()}
    need = [d for d in _need_list(days) if d not in have]
    limit_u = union if not limit else union[:limit]
    arts_cols = list(art.columns)
    for d in need:
        i = upto_idx[d]
        warm = days[max(0, i - serving.WARMUP_DAYS + 1)]
        parts: list[pd.Series] = []
        t0, thin = time.time(), 0
        if fv.free_gb() < 2.0:
            raise RuntimeError(f"影子打分前可用内存 {fv.free_gb():.1f} GB < 2 GB，中止")
        for k in range(0, len(limit_u), 300):
            frame = serving.raw_features(limit_u[k:k + 300], warm, d)
            keep = frame[arts_cols].isna().sum(axis=1) <= serving.MAX_NAN_FACTORS
            thin += int((~keep).sum())
            kept = frame[keep]
            if len(kept):
                part = pd.Series(art.booster.predict(serving.normalize(kept, art)), index=kept.index)
                parts.append(part)
            del frame
            gc.collect()
        if not parts:
            raise RuntimeError(f"影子 {d} 无一行分数；查面板/模型")
        # 只留当天行：预热窗的旧行不入缓存（种子已覆盖同日，拼一起会重复标签）
        d0 = pd.Timestamp(d)
        day_pred = (pd.concat(parts).reorder_levels(["datetime", "instrument"]).sort_index())
        day_pred = day_pred[day_pred.index.get_level_values(0) ==
                            d0].xs(d0, level="datetime", drop_level=False)
        s = pd.concat([s, day_pred]).sort_index()
        have |= {d}
        print(f"  {run_id_of(art)} 补 {d}：{len(day_pred):,} 行（薄流动 {thin}），{time.time()-t0:.0f}s", flush=True)
    tmp = cache.with_suffix(".tmp")
    s.name = "score"
    s.to_frame().to_parquet(tmp)
    tmp.replace(cache)
    # 归一日期层级为 Timestamp（种子/新补混合后保险），并给层级命名
    s = s.sort_index()
    s.index = s.index.set_levels([pd.to_datetime(s.index.levels[0]), s.index.levels[1]])
    s.index = s.index.set_names(["datetime", "instrument"])
    return s


def simulate(label: str, sig: pd.Series, union: list[str], today: str, risk: float) -> dict:
    result = fv.run_arm(sig, union, SHADOW_BEG, today, CAPITAL, BENCH, TOPK, ENTRY, EXITR,
                        dict(bt.EXCHANGE_KWARGS), label, 0.0, 0.0)
    m = {k: (round(v, 6) if isinstance(v, (int, float)) else v)
         for k, v in result.metrics.items()}
    eq = fv.curve(result)
    eq.to_frame("equity").to_parquet(SHADOW_DIR / f"{label}.parquet")
    out = {"arm": label, "as_of": today, "params": {"topk": TOPK, "entry_rank": ENTRY,
           "exit_rank": EXITR, "risk_degree": risk, "hold_rule": "band"},
           "metrics": m, "picks_latest": [], "picks_count": 0}
    last_days = sorted({str(d) for d in sig.index.get_level_values(0)})
    if last_days:
        last = last_days[-1]
        day_scores = sig.xs(last, level="datetime").sort_values(ascending=False)
        out["picks_latest"] = day_scores.head(TOPK).index.tolist()
        out["as_of_last_signal"] = last
        out["picks_count"] = len(day_scores)
    (SHADOW_DIR / f"{label}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    print(f"{label:<16} 区间 {m['total_return']:+.2%}  超额 {m.get('annual_excess') or 0:+.2%}  "
          f"回撤 {m['max_drawdown']:+.2%}  换手 {(m.get('turnover_daily') or 0):.2%}/日  "
          f"最新首选 {out['picks_latest'][:3]}", flush=True)
    return out


def main() -> int:
    global BASE_COST
    ap = argparse.ArgumentParser(description="qlib 影子盘 nightly 重放")
    ap.add_argument("--arms", default=",".join(ARMS), help="只跑这些臂（默认全部）")
    ap.add_argument("--limit-symbols", type=int, default=0, help="冒烟：限制票池前 N 只")
    ap.add_argument("--start", default=None, help="冒烟：重放起点（默认 SHADOW_BEG）")
    ap.add_argument("--warm-only", action="store_true", help="只补分数缓存，不重放（首个交易日前的预热）")
    args = ap.parse_args()
    global SHADOW_BEG
    if args.start:
        SHADOW_BEG = args.start
    arms = {k: v for k, v in ARMS.items() if k in set(args.arms.split(","))}
    if not arms:
        raise SystemExit(f"--arms 没匹配到任何臂：{list(ARMS)}")

    serving.init_qlib()
    days = panel_calendar()
    today = days[-1]           # 补分记到最新交易日
    sim_end = days[-2]         # 撮合窗右端必须留 ≥1 天余量（_snap_window 的规矩）
    if sim_end < SHADOW_BEG and not args.warm_only:
        raise SystemExit(f"面板最新日 {today} 还没到影子起点 {SHADOW_BEG}+1，先跑不了")
    upto_idx = {d: i for i, d in enumerate(days)}

    symbols = panel_stocks()
    st_codes = fv.load_st_codes(fv.ST_FILE)
    close, amount = fv.load_panel(symbols, days)
    _, st_u, _ = fv.build_universes(close, amount, SHADOW_BEG_PREV, today, st_codes,
                                    130, 1e8)
    union = sorted(set(st_u.columns[st_u.any()]))
    close_w = close.loc[days[max(0, upto_idx[SHADOW_BEG_PREV]) - serving.WARMUP_DAYS + 1]:today]
    amount_w = amount.loc[close_w.index]
    del close, amount
    gc.collect()
    print(f"影子 {SHADOW_BEG}→{today}，st 票池并集 {len(union)} 只，可用内存 {fv.free_gb():.1f}G", flush=True)

    # 每个模型补一次分数（多臂共用同一模型缓存）
    by_model: dict[str, tuple] = {}
    for label, cfg in arms.items():
        art = load_art(Path(cfg["dir"]))
        rid = run_id_of(art)
        by_model.setdefault(rid, (art, cfg.get("seed")))
    sig_by_model: dict[str, pd.Series] = {}
    for rid, (art, seed) in by_model.items():
        s = ensure_scores(art, union, days, seed, upto_idx, args.limit_symbols)
        if args.warm_only:
            sig, _ = fv.build_signal(s, st_u, close_w, amount_w, rid)
            days_seen = sorted({str(x) for x in sig.index.get_level_values(0)})
            out = {"run_id": rid, "top": TOPK, "entry_rank": ENTRY, "picks": {}}
            for d in days_seen[-2:]:
                sc = sig.xs(d, level="datetime").sort_values(ascending=False)
                out["picks"][d] = {"tradeable": len(sc), "top20": sc.head(TOPK).index.tolist()}
            (SHADOW_DIR / f"{rid}_day0.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"warm-only：{rid} 缓存补到 {today}，不重放；首建名单 → {rid}_day0.json", flush=True)
            for d, v in out["picks"].items():
                print(f"  {d} 可买 {v['tradeable']}，前20 {v['top20'][:5]}…", flush=True)
            continue
        s = s.loc[SHADOW_BEG_PREV:today]
        sig_by_model[rid] = s
        print(f"分数 {rid}：{len(s):,} 行（{SHADOW_BEG_PREV}→{today}）", flush=True)
    if args.warm_only:
        return 0

    summary = {"as_of": today, "shadow_beg": SHADOW_BEG, "arms": {}}
    for label, cfg in arms.items():
        art = load_art(Path(cfg["dir"]))
        s = sig_by_model[run_id_of(art)]
        if s.empty:
            print(f"{label}: 无分数，跳过", flush=True)
            continue
        sig, _ = fv.build_signal(s, st_u, close_w, amount_w, label)
        summary["arms"][label] = simulate(label, sig, union, sim_end, cfg["risk"])

    out = SHADOW_DIR / f"shadow_{today}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"读数落档：{out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
