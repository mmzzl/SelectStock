"""ruleset_posterior.py — 方案 B 落地：给一套自定义规则集算「持有可信度」和各卖出规则的事后表现。

口径（和用户的定义对齐，但把话说全）：
- 每条卖出规则 R：以 R 触发的时点为样本，观测 T+10 / T+20 交易日，给
  `hit_rate`（兑现率：20 日收盘低于触发价，卖对了）、`fly_rate`（卖飞率：20 日收盘涨过 +3%）、
  `median_ret`、`n`（触发次数，做置信过滤）。
- **持有可信度不是对单条规则，是对「全部卖出规则同时都不触发」这个联合状态**：
  历史上处于该状态的时点，往后 20 个交易日内**收盘价没有跌破 −3%** 的占比。
  这是状态频率，不是个股预测，所以必须连着 `base_pct` 和 `lift` 一起给：
  2026-10-16 抽样实测「什么都没响」这个状态相对全体的尾部 lift 只有 0.96、20 日超额 −0.02pp，
  也就是它的绝对数和「随便挑一只票」几乎没差别。只印一个好看的百分比是在骗人，
  所以基准/lift/t 是这份产物的必填字段，插件文案必须一起说出去。

计算方式：全市场分块流式回放，每个状态只留**按交易日的累加器**（不留 12M 行明细），
所以内存随分块大小走、与股票总数无关——这台机器上还跑着线上服务，可用内存只有 2.7GB。
比较一律用**同日配对**：点级均值会被「判据扎堆的日子」主导，2026-10-13 那次 sell 格
点级 +0.87%、日等权同日差分 −0.47pp，符号直接反过来。

判定文法与线上同一个真源：`stock_selector/backtest/rule_expr.py`（结构化条件 ⇄ 表达式 ⇄ 向量化），
所以这里算出来的东西和 `judge_holding` 不会分家。

用法:
    python scripts/ruleset_posterior.py --dry-run --step 8      # 抽样自检，只打印不落盘
    python scripts/ruleset_posterior.py                          # 全市场 + 写 data/rules/active_posterior.json
    python scripts/ruleset_posterior.py --rules x.json --out y.json --from 2019-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.backtest import rule_expr  # noqa: E402
from stock_selector.conviction import bucket  # noqa: E402

KLINE_DIR = ROOT / "data" / "stock_kline"
ACTIVE_FILE = ROOT / "data" / "rules" / "active.json"
OUT_FILE = ROOT / "data" / "rules" / "active_posterior.json"
LOG_DIR = ROOT / "logs"

FWD_SHORT = 10
FWD_LONG = 20
FLAT_GAIN = -0.03             # 「未出现显著下跌」= 未来 20 根收盘最低点相对今天 > −3%
FLY_GAIN = 0.03               # 卖飞 = 触发后 20 日收盘涨过 +3%
TAIL8, TAIL12 = -0.08, -0.12  # 灾难口径：未来 20 根内最低价相对今天的回撤
MIN_STATE_N = 200             # 用户定的置信下限：联合状态样本不足就不给数字
MIN_STATE_DAYS = 30           # 交易日太少时，lift 是几天行情撑起来的，也不算数
HIST_LO_PP, HIST_STEP = -30.0, 0.5   # 20 日收益的粗直方图，只为算中位数
HIST_BINS = 121
BLOCK = 250                   # 每块多少只票：5000 天 × 250 票 × ~12 列 float64 ≈ 120MB（机器上还有线上服务）

logger = logging.getLogger("ruleset_posterior")


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)


# ------------------------------------------------------------------ 规则集

def load_rule_set(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    sells = [(str(s.get("label") or f"规则{i + 1}"), str(s["expr"]).strip())
             for i, s in enumerate(data.get("sell_rules") or [])
             if isinstance(s, dict) and str(s.get("expr") or "").strip()]
    adds = [(str(a.get("label") or f"规则{i + 1}"), str(a["expr"]).strip())
            for i, a in enumerate(data.get("add_rules") or [])
            if isinstance(a, dict) and str(a.get("expr") or "").strip()]
    if not sells:
        raise SystemExit(f"{path} 里没有卖出规则，「全部不触发」这个状态无从定义")
    fields = sorted({f for _, e in sells + adds for f in rule_expr.validate(e)})
    return {"rule_set_id": data.get("rule_set_id"), "name": str(data.get("name") or ""),
            "sell": sells, "add": adds,
            "fields": sorted(set(fields) | {"close", "low"})}


# ------------------------------------------------------------------ 累加器

class States:
    """每个状态一组按日的计数/求和数组：内存 = 状态数 × 交易日数，与股票数无关。"""

    KEYS = ("cnt", "s10", "s20", "flat", "fly", "tail8", "tail12", "flip")

    def __init__(self, n_days: int):
        self.n_days = n_days
        self._d: dict[str, dict[str, np.ndarray]] = {}
        self._hist: dict[str, np.ndarray] = {}

    def add(self, name: str, di: np.ndarray, f10: np.ndarray, f20: np.ndarray,
            flat: np.ndarray, fly: np.ndarray, t8: np.ndarray, t12: np.ndarray,
            flip: np.ndarray, hist: np.ndarray) -> None:
        slot = self._d.setdefault(name, {k: np.zeros(self.n_days) for k in self.KEYS})
        # out=slot[...]：np.add(a, b) 只是返回新数组，跨块累加必须原地写回
        np.add(slot["cnt"], np.bincount(di, minlength=self.n_days), out=slot["cnt"])
        for key, w in (("s10", f10), ("s20", f20), ("flat", flat), ("fly", fly),
                       ("tail8", t8), ("tail12", t12), ("flip", flip)):
            np.add(slot[key], np.bincount(di, weights=w, minlength=self.n_days),
                   out=slot[key])
        self._hist[name] = self._hist.get(name, np.zeros(HIST_BINS)) + hist

    def get(self, name: str) -> dict[str, np.ndarray] | None:
        return self._d.get(name)

    def hist(self, name: str) -> np.ndarray:
        return self._hist.get(name, np.zeros(HIST_BINS))


def day_rate(slot: dict[str, np.ndarray], key: str) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.divide(slot[key], slot["cnt"], out=np.zeros_like(slot[key]),
                         where=slot["cnt"] > 0)


def summarize(st: States, name: str, base: str, date_list: list[str]) -> dict:
    slot = st.get(name)
    if slot is None or not slot["cnt"].any():
        return {"n": 0, "days": 0, "enough": False, "flat_pct": None}
    live = slot["cnt"] > 0
    n = int(slot["cnt"].sum())
    out = {
        "n": n,
        "days": int(live.sum()),
        "flat_pct": round(float(day_rate(slot, "flat")[live].mean()) * 100, 2),
        "flat_point_pct": round(float(slot["flat"].sum() / n) * 100, 2),
        "fly_pct": round(float(day_rate(slot, "fly")[live].mean()) * 100, 2),
        "tail8_pct": round(float(day_rate(slot, "tail8")[live].mean()) * 100, 2),
        "tail12_pct": round(float(day_rate(slot, "tail12")[live].mean()) * 100, 2),
        "flip_sell_20_pct": round(float(day_rate(slot, "flip")[live].mean()) * 100, 2),
        "fwd10_daymean_pct": round(float((slot["s10"][live] / slot["cnt"][live]).mean()) * 100, 3),
        "fwd20_daymean_pct": round(float((slot["s20"][live] / slot["cnt"][live]).mean()) * 100, 3),
        "median_ret_pct": _median_pct(st.hist(name)),
    }
    bslot = st.get(base)
    if name != base and bslot is not None:
        pair = live & (bslot["cnt"] > 0)
        if int(pair.sum()) >= MIN_STATE_DAYS:
            bflat = day_rate(bslot, "flat")
            diff = day_rate(slot, "flat")[pair] - bflat[pair]
            se = float(diff.std(ddof=1) / max(len(diff), 1) ** 0.5)
            base_pct = float(bflat[pair].mean()) * 100
            out.update({
                "base_pct": round(base_pct, 2),
                "lift": round(out["flat_pct"] / max(base_pct, 1e-9), 3),
                "vs_base_pp": round(float(diff.mean()) * 100, 3),
                "vs_base_t": round(float(diff.mean() / se), 2) if se > 0 else None,
                "base_tail8_pct": round(float(day_rate(bslot, "tail8")[pair].mean()) * 100, 2),
                "tail8_lift": round(out["tail8_pct"] / max(
                    float(day_rate(bslot, "tail8")[pair].mean()) * 100, 1e-9), 3),
                "exc20_pp": round(float((slot["s20"][pair] / slot["cnt"][pair]
                                         - bslot["s20"][pair] / bslot["cnt"][pair]).mean()) * 100, 3),
                "exc10_pp": round(float((slot["s10"][pair] / slot["cnt"][pair]
                                         - bslot["s10"][pair] / bslot["cnt"][pair]).mean()) * 100, 3),
                "era_flat_diff_pp": _era_diffs(slot, bslot, pair, len(date_list)),
            })
    out["enough"] = bool(n >= MIN_STATE_N and out["days"] >= MIN_STATE_DAYS)
    return out


def _median_pct(hist: np.ndarray) -> float | None:
    total = int(hist.sum())
    if not total:
        return None
    i = int(np.searchsorted(np.cumsum(hist), total / 2.0))
    return round(HIST_LO_PP + i * HIST_STEP + HIST_STEP / 2, 2)


def _era_diffs(slot: dict, bslot: dict, pair: np.ndarray, n_days: int) -> str:
    """三段时代各自的「未跌超3% − 全体」日等权差；跨代不同号就说明这个状态靠时代红利。"""
    day = np.arange(n_days)
    valid = np.flatnonzero(pair)
    if len(valid) < 3:
        return "nan,nan,nan"
    c1, c2 = np.quantile(valid, [1 / 3, 2 / 3])
    parts = []
    for lo, hi in ((-1, c1), (c1, c2), (c2, n_days + 1)):
        sel = pair & (day >= lo) & (day < hi)
        if int(slot["cnt"][sel].sum()) < MIN_STATE_N or int(bslot["cnt"][sel].sum()) < MIN_STATE_N:
            parts.append("nan")
            continue
        d = day_rate(slot, "flat")[sel] - day_rate(bslot, "flat")[sel]
        parts.append(f"{float(d.mean()) * 100:+.2f}")
    return ",".join(parts)


# ------------------------------------------------------------------ 回放

def scan_universe(files: list[Path], fields: list[str]) -> tuple[list[str], dict[str, Path]]:
    """第一遍只收日期轴和「这票有哪些列」，第二遍分块填矩阵。"""
    seen: set[str] = set()
    universe: dict[str, Path] = {}
    for path in files:
        try:
            dates = pd.read_parquet(path, columns=["date"])["date"]
        except Exception as exc:
            logger.debug("跳过 %s：%s", path.name, exc)
            continue
        if len(dates) < 60:
            continue
        seen.update(pd.to_datetime(dates).dt.strftime("%Y-%m-%d").tolist())
        universe[path.stem] = path
    logger.info("日期轴 %d 天 × 候选 %d 票", len(seen), len(universe))
    return sorted(seen), universe


def shift_fwd(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(a.shape, np.nan, dtype=a.dtype)
    if a.shape[0] > k:
        out[: a.shape[0] - k] = a[k:]
    return out


def roll_min_incl(a: np.ndarray, win: int) -> np.ndarray:
    """逐列的 min(a[i .. i+win-1])（含当天），不足窗口的尾部留 NaN。"""
    out = np.full(a.shape, np.nan, dtype=a.dtype)
    if a.shape[0] >= win:
        # DataFrame.rolling 按列走 axis=0；Series 只吃一维，而这里的行=交易日、列=股票
        r = pd.DataFrame(a[::-1]).rolling(win, min_periods=win).min().to_numpy()
        out[: len(r)] = r[::-1]
    return out


def roll_min_next(a: np.ndarray, win: int) -> np.ndarray:
    """min(a[i+1 .. i+win])：未来 win 根，不含当天。"""
    return shift_fwd(roll_min_incl(a, win), 1)


def flip_soon(mask: np.ndarray, win: int) -> np.ndarray:
    """未来 win 根（不含当天）内该状态是否出现过——列向 cumsum 差分。"""
    n, m = mask.shape
    pad = np.zeros((n + win, m), dtype=np.int32)
    pad[:n] = mask.astype(np.int32)
    cs = np.zeros((n + win + 1, m), dtype=np.int32)
    np.cumsum(pad, axis=0, out=cs[1:])
    return (cs[win + 1: win + 1 + n] - cs[1: 1 + n]) > 0


def replay(files: list[Path], rs: dict, date_list: list[str], day_from: int) -> States:
    axis = np.array(date_list, dtype="datetime64[ns]")
    st = States(len(date_list))
    names = [f"rule{i}" for i in range(len(rs["sell"]))] \
        + [f"add{i}" for i in range(len(rs["add"]))] + ["any_sell", "joint_hold", "all"]
    t0 = time.time()
    blocks = [files[i:i + BLOCK] for i in range(0, len(files), BLOCK)]
    for bi, block in enumerate(blocks):
        cols = _load_block(block, rs["fields"], axis)
        if cols is not None:
            _accumulate(st, cols, rs, names, day_from)
        del cols
        logger.info("块 %d/%d（%d 票）完成，累计 %.0fs", bi + 1, len(blocks), len(block),
                    time.time() - t0)
    return st


def _load_block(files: list[Path], fields: list[str], axis: np.ndarray) -> dict | None:
    n_days = len(axis)
    mat = {f: np.full((n_days, len(files)), np.nan, dtype=np.float64) for f in fields}
    kept = 0
    for path in files:
        try:
            df = pd.read_parquet(path, columns=["name", "date", *fields])
        except Exception as exc:
            logger.debug("跳过 %s：%s", path.name, exc)
            continue
        if len(df) < 60 or bucket.is_st_name(str(df["name"].iloc[-1] or "")):
            continue
        rows = np.searchsorted(axis, pd.to_datetime(df["date"]).to_numpy())
        for f in fields:
            mat[f][rows, kept] = df[f].to_numpy(dtype=np.float32)
        kept += 1
        del df
    if not kept:
        return None
    return {f: mat[f][:, :kept] for f in fields}


def _accumulate(st: States, cols: dict, rs: dict, names: list[str], day_from: int) -> None:
    close = cols["close"]
    low = cols["low"]
    with np.errstate(divide="ignore", invalid="ignore"):
        f10 = shift_fwd(close, FWD_SHORT) / close - 1.0
        f20 = shift_fwd(close, FWD_LONG) / close - 1.0
        mc20 = roll_min_next(close, FWD_LONG) / close - 1.0
        ml20 = roll_min_next(low, FWD_LONG) / close - 1.0
    judged = (np.isfinite(close) & (close > 0) & np.isfinite(f20)
              & np.isfinite(mc20) & np.isfinite(ml20))
    if day_from:
        judged[:day_from] = False
    flat = mc20 > FLAT_GAIN
    fly = f20 > FLY_GAIN
    t8 = ml20 <= TAIL8
    t12 = ml20 <= TAIL12

    sells = [rule_expr.eval_matrix(e, cols) & judged for _, e in rs["sell"]]
    adds = [rule_expr.eval_matrix(e, cols) & judged for _, e in rs["add"]]
    any_sell = np.any(sells, axis=0) if sells else np.zeros(close.shape, dtype=bool)
    flip = flip_soon(any_sell, FWD_LONG)
    states = {"all": judged, "any_sell": judged & any_sell, "joint_hold": judged & ~any_sell}
    for i, m in enumerate(sells):
        states[f"rule{i}"] = m
    for i, m in enumerate(adds):
        states[f"add{i}"] = judged & ~any_sell & m     # 与 judge_holding 的 sell 优先一致

    day_axis = np.arange(close.shape[0], dtype=np.int64)
    hist_bin = np.nan_to_num((f20 * 100 - HIST_LO_PP) / HIST_STEP, nan=0.0)
    hist_idx = np.clip(hist_bin, 0, HIST_BINS - 1).astype(np.int64)
    for name in names:
        mask = states[name]
        rows, colsi = np.nonzero(mask)
        if not len(rows):
            continue
        di = day_axis[rows]
        st.add(name, di, f10[rows, colsi], f20[rows, colsi], flat[rows, colsi],
               fly[rows, colsi], t8[rows, colsi], t12[rows, colsi], flip[rows, colsi],
               np.bincount(hist_idx[rows, colsi], minlength=HIST_BINS))


# ------------------------------------------------------------------ 产物

def build_payload(st: States, rs: dict, date_list: list[str], n_files: int) -> dict:
    rules = [{"label": lab, "expr": e, **summarize(st, f"rule{i}", "all", date_list)}
             for i, (lab, e) in enumerate(rs["sell"])]
    adds = [{"label": lab, "expr": e, **summarize(st, f"add{i}", "joint_hold", date_list)}
            for i, (lab, e) in enumerate(rs["add"])]
    base = summarize(st, "all", "all", date_list)
    joint = summarize(st, "joint_hold", "all", date_list)
    any_sell = summarize(st, "any_sell", "all", date_list)
    payload = {
        "version": 1,
        "rule_set_id": rs["rule_set_id"],
        "name": rs["name"],
        "computed_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contract": {
            "horizon_days": FWD_LONG,
            "flat_threshold_pct": round(abs(FLAT_GAIN) * 100, 2),
            "fly_threshold_pct": round(FLY_GAIN * 100, 2),
            "tail_threshold_pct": round(abs(TAIL8) * 100, 2),
            "min_samples": MIN_STATE_N, "min_days": MIN_STATE_DAYS,
            "meaning": ("hold_confidence_pct = 「全部卖出规则都不触发」的历史时点里，"
                        "后 20 个交易日收盘价未跌破 −3% 的占比（日等权）。"
                        "这是状态频率，不是个股预测；lift≈1 就等于说「和随机挑一只没差别」。"),
        },
        "universe": {"days": len(date_list), "files": n_files, "st_excluded": True,
                     "range": [date_list[0], date_list[-1]]},
        "base": base, "any_sell": any_sell, "joint_hold": joint,
        "sell_rules": rules, "add_rules": adds,
        "hold_confidence_pct": joint["flat_pct"] if joint.get("enough") else None,
    }
    if not joint.get("enough"):
        payload["hold_confidence_note"] = (
            f"「全部未触发」状态样本不足（N={joint.get('n', 0)}，覆盖 {joint.get('days', 0)} 个交易日，"
            f"门槛 {MIN_STATE_N}/{MIN_STATE_DAYS}），不给数字")
    return payload


def render(p: dict) -> str:
    c = p["contract"]
    lines = [f"规则集「{p['name']}」回放：{p['universe']['range'][0]}→{p['universe']['range'][1]}，"
             f"{p['universe']['days']} 个交易日，剔 ST；观测窗 T+{c['horizon_days']} 交易日，不含成本",
             _line("全体基准", p["base"]), _line("任一卖出触发", p["any_sell"])]
    lines += [_line(f"卖出·{r['label'][:12]}", r) for r in p["sell_rules"]]
    lines += [_line(f"加仓·{r['label'][:12]}", r, base=p["joint_hold"]) for r in p["add_rules"]]
    lines.append(_line("★ 持有可信度(全部未触发)", p["joint_hold"], base=p["base"]))
    if p["hold_confidence_pct"] is None:
        lines.append(f"  ⚠️ {p['hold_confidence_note']}")
    else:
        j = p["joint_hold"]
        lines.append(f"  解读：未跌超{abs(c['flat_threshold_pct']):.0f}% 的占比 {j['flat_pct']:.2f}%，"
                     f"全体基准 {j['base_pct']:.2f}%，lift {j['lift']:.3f}，"
                     f"配对 t={j['vs_base_t']}，三段时代差 {j['era_flat_diff_pp']}pp"
                     + ("（lift≈1：这个状态和随机挑票没差别，别当买入依据）"
                        if abs(j["lift"] - 1.0) < 0.05 else ""))
    return "\n".join(lines)


def _line(name: str, r: dict, base: dict | None = None) -> str:
    if not r.get("n"):
        return f"  {name:24s} n=0 —— 从未触发"
    tail = ""
    if "lift" in r:
        tail = (f"  基准{r['base_pct']:.2f}% lift {r['lift']:.3f} t={r['vs_base_t']} "
                f"超额20 {r['exc20_pp']:+.2f}pp 尾8 lift {r['tail8_lift']:.2f}")
    med = r.get("median_ret_pct")
    return (f"  {name:24s} n={r['n']:>11,}/{r['days']:>5,}日  "
            f"未跌超3% {r['flat_pct']:6.2f}%  卖飞 {r['fly_pct']:5.2f}%  "
            f"≤−8% {r['tail8_pct']:5.2f}%  转卖 {r['flip_sell_20_pct']:5.1f}%  "
            f"20日中位 {med:+.2f}%" + tail)


def main() -> None:
    global BLOCK

    ap = argparse.ArgumentParser(description="规则集全市场回放：持有可信度 + 各卖出规则事后表现")
    ap.add_argument("--rules", default=str(ACTIVE_FILE))
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--out", default=str(OUT_FILE))
    ap.add_argument("--step", type=int, default=1, help="每 N 个文件取 1 个（自检用）")
    ap.add_argument("--block", type=int, default=BLOCK)
    ap.add_argument("--from", dest="date_from", default="", help="只统计这天及以后的时点")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    rules_path = Path(args.rules)
    if not rules_path.is_file():
        logger.error("规则集不存在：%s（先在管理页面「应用」一条）", rules_path)
        raise SystemExit(2)
    rs = load_rule_set(rules_path)
    logger.info("规则集 %s：卖出 %d 条、加仓 %d 条，用到列 %s",
                rs["name"] or rules_path.name, len(rs["sell"]), len(rs["add"]), rs["fields"])

    BLOCK = max(50, args.block)
    files = sorted(Path(args.kline_dir).glob("*.parquet"))[:: max(1, args.step)]
    date_list, _ = scan_universe(files, rs["fields"])
    day_from = int(np.searchsorted(np.array(date_list, dtype=object), args.date_from)) \
        if args.date_from else 0
    if args.date_from:
        logger.info("--from %s → 从第 %d/%d 个交易日开始", args.date_from, day_from, len(date_list))
    st = replay(files, rs, date_list, day_from)
    payload = build_payload(st, rs, date_list, len(files))
    print(render(payload))
    if not args.dry_run:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已写 %s", out)


if __name__ == "__main__":
    main()
