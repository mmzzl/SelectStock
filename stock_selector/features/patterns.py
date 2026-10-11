"""K线形态 + 量能的向量化派生列。输入是单只标的按 date 升序的日线，只依赖
open/high/low/close/volume（`pos60` 若已在构建链里算好会直接复用）。

设计约束（改这里之前先读）：

- 列只能是 **float64 或 bool**：这些列要进 parquet 并被 `backtest/rule_eval.py` 求值，
  而 RuleEvaluator 对每个变量做 float()，字符串标签（"锤子线"之类）会让规则静默失效。
- **as-of 安全**：所有窗口只回看当日及以前。局部高低点（枢轴）要等它右侧 PIVOT_SPAN
  根走完才算确认，未确认的不参与判据，绝不把未来信息写进当天。
- 位置语境统一走 `pos60`（现价在 60 日高低区间的分位）：低位 <= POS_LOW，高位 >= POS_HIGH。
  形态函数自身不判位置，位置编码进列名后缀（`_low` / `_high`），避免同一形态两套口径。
- 中长期形态只输出「突破/跌破当日」这一个事件点（反复置位会让触发率虚高、回测里同段
  行情被买多次）；颈线与箱体上下沿另存 float 列，供「跌破颈线离场」「回踩支撑低吸」引用。
- 圆弧底/V型反转/旗形/三角形是教科书形态的量化近似（趋势段+收窄+缩量+突破四要素），
  不是画线识别。形态可以骗线、量能可以对倒，命中只当概率参考。
"""

import numpy as np
import pandas as pd

# 位置分位：pos60 低于 0.35 算相对低位，高于 0.65 算相对高位
POS_LOW = 0.35
POS_HIGH = 0.65

# 单根解剖：实体/振幅占比低于该值算十字星；影线达到实体该倍数算长影线
DOJI_BODY_RATIO = 0.10
SHADOW_BODY_MULT = 2.0

# 量能：相对 vol_ma5 判放大/萎缩，相对 20 日均量的 z 分数判爆量，地量看 60 日最低
VOL_EXPAND = 1.3
VOL_SHRINK = 0.75
VOL_BLAST_Z = 2.5
VOL_GROUND_LOOKBACK = 60

# 双底(顶)/头肩：枢轴半径、两峰间隔、等高容差、突破确认幅度、突破后跟踪与颈线有效期
PIVOT_SPAN = 5
PEAK_MIN_GAP = 8
PEAK_MAX_GAP = 60
PEAK_DEPTH_TOL = 0.05
NECK_CONFIRM = 0.005
NECK_TRACK_BARS = 30
NECK_KEEP_BARS = 60

# 星形第三根需收进第一根实体中点；孕线是小实体 + 缩量的Inside Bar
STAR_BODY_RATIO = 0.35
HARAMI_BODY_RATIO = 0.5
HARAMI_VOL_RATIO = 0.8

# 区间类形态参数。旗形的「旗面」按 3~8 根枚举：突破判据必须比旗面上沿，
# 而不是比 12 根窗口最高价——后者要求回撤 2% 后一根 K 线内立刻创新高，实测永不相交。
FLAG_LEG_BARS = 12
FLAG_LEG_MOVE = 0.18
FLAG_RETRACE = 0.20
FLAG_MIN_RETRACE = 0.02
FLAG_MIN_BARS = 3
FLAG_MAX_BARS = 8
FLAG_BODY_MAX = 0.50                           # 旗面内平均实体/振幅上限，比孕线松一档
BOX_BARS = 30
BOX_HEIGHT = 0.18
TRI_BARS = 20
TRI_SQUEEZE = 0.85
ARC_BARS = 40
ARC_RISE_MIN = 0.03
ARC_RISE_MAX = 0.20
ARC_DEPTH = 0.10
ARC_REFIRE_BARS = 40
V_FALL = -0.10
V_REBOUND = 0.08


def _b(s: pd.Series) -> pd.Series:
    """布尔列收口：窗口起点的 NaN 参与 & 运算会退化成 object dtype，parquet 存不下。"""
    if s.dtype == bool:
        return s
    return s.astype("boolean").fillna(False).astype(bool)


def _lag(s: pd.Series, n: int) -> pd.Series:
    """布尔状态整体后移 n 根（"昨日仍在整理、今日才算突破"），空档按 False。"""
    return _b(s.shift(n))


def _parts(df: pd.DataFrame) -> dict:
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    return {
        "o": o, "h": h, "l": l, "c": c, "v": v, "rng": rng, "body": body,
        "upper": h - pd.concat([o, c], axis=1).max(axis=1),
        "lower": pd.concat([o, c], axis=1).min(axis=1) - l,
    }


def _pos60(df: pd.DataFrame, p: dict) -> pd.Series:
    if "pos60" in df.columns:
        return df["pos60"].astype(float)
    c = p["c"]
    win = c.rolling(60)
    return (c - win.min()) / (win.max() - win.min() + 1e-9)


def _volume_context(p: dict) -> pd.DataFrame:
    """量能语境：均量、量能趋势、z 分数 + 放量/缩量/地量/爆量四个标记。"""
    v = p["v"]
    ma5, ma20 = v.rolling(5).mean(), v.rolling(20).mean()
    ratio5 = v / ma5.replace(0, np.nan)
    out = pd.DataFrame(index=v.index)
    out["vol_ma20"] = ma20
    out["vol_trend"] = ma5 / ma20.replace(0, np.nan)         # >1 量能抬升，<1 萎缩
    out["vol_z"] = (v - ma20) / v.rolling(20).std().replace(0, np.nan)
    out["vol_expanding"] = _b(ratio5 >= VOL_EXPAND)
    out["vol_contracting"] = _b(ratio5 <= VOL_SHRINK)
    out["vol_ground"] = _b(v <= v.rolling(VOL_GROUND_LOOKBACK).min())
    out["vol_blast"] = _b(out["vol_z"] >= VOL_BLAST_Z)
    return out


def _single_bar(p: dict, vol: pd.DataFrame, pos: pd.Series) -> pd.DataFrame:
    """长下影/长上影按位置拆成止跌与见顶两种读法；十字星再按量能区分僵持与分歧。"""
    body, rng = p["body"], p["rng"]
    low_pos, high_pos = pos <= POS_LOW, pos >= POS_HIGH
    long_lower = (p["lower"] >= SHADOW_BODY_MULT * body) & (p["upper"] <= body) & (body > 0)
    long_upper = (p["upper"] >= SHADOW_BODY_MULT * body) & (p["lower"] <= body) & (body > 0)
    doji = body / rng <= DOJI_BODY_RATIO

    out = pd.DataFrame(index=body.index)
    out["hammer_low"] = _b(long_lower & low_pos)
    out["hanging_man_high"] = _b(long_lower & high_pos)
    out["shooting_star_high"] = _b(long_upper & high_pos)
    out["doji_low"] = _b(doji & low_pos)
    out["doji_high"] = _b(doji & high_pos)
    out["doji_high_blast"] = _b(doji & high_pos & vol["vol_blast"])   # 高位十字星 + 巨量 = 分歧巨大
    return out


def _combos(p: dict) -> pd.DataFrame:
    """2~3 根反转组合。吞没/刺透/乌云/星形都要求关键那根放量；孕线反过来要求缩量。"""
    o, c, h, l, v = p["o"], p["c"], p["h"], p["l"], p["v"]
    body = p["body"]
    po, pc = o.shift(1), c.shift(1)
    p2o, p2c = o.shift(2), c.shift(2)
    p1_body = (pc - po).abs()
    p2_body = (p2c - p2o).abs()
    p1_mid, p2_mid = (po + pc) / 2, (p2o + p2c) / 2
    ma5 = v.rolling(5).mean()
    up = v >= VOL_EXPAND * ma5.shift(1).replace(0, np.nan)

    bear, bull = c < o, c > o
    p_bear, p_bull = pc < po, pc > po
    p2_bear, p2_bull = p2c < p2o, p2c > p2o
    p1_high = pd.concat([po, pc], axis=1).max(axis=1)
    p1_low = pd.concat([po, pc], axis=1).min(axis=1)

    out = pd.DataFrame(index=c.index)
    out["bullish_engulf"] = _b(p_bear & bull & (c >= po) & (o <= pc) & up)
    out["bearish_engulf"] = _b(p_bull & bear & (o >= pc) & (c <= po) & up)
    star = p1_body <= STAR_BODY_RATIO * p2_body.replace(0, np.nan)
    out["morning_star"] = _b(p2_bear & star & bull & (c >= p2_mid) & up)
    out["evening_star"] = _b(p2_bull & star & bear & (c <= p2_mid) & up)
    # 刺透/乌云：第二根深入前一根实体过半但没全盖住，全盖住就归入吞没
    out["piercing_line"] = _b(p_bear & bull & (o < pc) & (c > p1_mid) & (c <= po) & up)
    out["dark_cloud"] = _b(p_bull & bear & (o > pc) & (c < p1_mid) & (c >= po) & up)
    inside = (h <= p1_high) & (l >= p1_low)
    out["harami"] = _b((p1_body > 0) & (body <= HARAMI_BODY_RATIO * p1_body) & inside
                       & (v <= HARAMI_VOL_RATIO * ma5.shift(1)))
    # 红三兵/黑三鸦：三根同向、开盘与收盘都逐级抬(压)低，且量能逐日温和递增
    rising_opens = (o > po) & (po > p2o)
    vol_step = (v > v.shift(1)) & (v.shift(1) > v.shift(2))
    out["three_soldiers"] = _b(bull & (pc > po) & (p2c > p2o)
                               & (c > pc) & (pc > p2c) & rising_opens & vol_step)
    out["three_crows"] = _b(bear & (pc < po) & (p2c < p2o)
                            & (c < pc) & (pc < p2c)
                            & (o < po) & (po < p2o) & vol_step)
    return out


def _pivot_mask(arr: np.ndarray, kind: str, span: int = PIVOT_SPAN) -> np.ndarray:
    """已确认的局部极值：右侧 span 根走完且窗口数据齐备；未确认一律 False。"""
    agg = pd.Series(arr).rolling(2 * span + 1, center=True, min_periods=span + 1)
    cen = (agg.min() if kind == "min" else agg.max()).to_numpy()
    with np.errstate(invalid="ignore"):
        mask = arr == cen
    mask[np.isnan(cen)] = False
    mask[-span:] = False
    return mask


def _first_break(after: int, cn: np.ndarray, expand: np.ndarray, level: float,
                 side: str) -> int | None:
    """第二个转折点后第一次「放量越线」的日子；越线要超出 NECK_CONFIRM 才算数。"""
    if not np.isfinite(level) or level <= 0:
        return None
    stop = min(len(cn), after + 1 + NECK_TRACK_BARS)
    if stop <= after + 1:
        return None
    seg, exp = cn[after + 1:stop], expand[after + 1:stop]
    with np.errstate(invalid="ignore"):
        hit = ((seg > level * (1 + NECK_CONFIRM)) if side == "above"
               else (seg < level * (1 - NECK_CONFIRM))) & exp
    idx = np.where(hit)[0]
    return int(after + 1 + idx[0]) if idx.size else None


def _double_scans(peaks: np.ndarray, cn: np.ndarray, ln: np.ndarray, hn: np.ndarray,
                  expand: np.ndarray, side: str) -> list[tuple[int, float]]:
    """双底 / 双顶：相邻两个同向枢轴大致等高，中间反向极值当颈线，放量突破记一次事件。"""
    records: list[tuple[int, float]] = []
    for a, b in zip(peaks[:-1], peaks[1:]):
        if not (PEAK_MIN_GAP <= b - a <= PEAK_MAX_GAP):
            continue
        if side == "above":
            deep = min(ln[a], ln[b])
            if max(ln[a], ln[b]) > deep * (1 + PEAK_DEPTH_TOL):
                continue                       # 两底高度差过大，不算双底
            level = hn[a:b + 1].max()
            if level <= deep * 1.03:
                continue                       # 中间反弹太弱，形态没撑开
        else:
            top = max(hn[a], hn[b])
            if min(hn[a], hn[b]) < top * (1 - PEAK_DEPTH_TOL):
                continue
            level = ln[a:b + 1].min()
            if level >= top * 0.97:
                continue
        hit = _first_break(b, cn, expand, level, side)
        if hit is not None:
            records.append((hit, float(level)))
    return records


def _head_scans(peaks: np.ndarray, cn: np.ndarray, ln: np.ndarray, hn: np.ndarray,
               expand: np.ndarray, side: str) -> list[tuple[int, float]]:
    """头肩底 / 头肩顶：连续三个同向枢轴，中间那个是明显更深的头，两肩大致齐平。

    颈线取两个中间极值里更严格的一侧（底取较高、顶取较低），宁可漏判不误判突破。
    """
    records: list[tuple[int, float]] = []
    for a, b, c3 in zip(peaks[:-2], peaks[1:-1], peaks[2:]):
        if not (PEAK_MIN_GAP <= c3 - a <= PEAK_MAX_GAP) or b - a < 3 or c3 - b < 3:
            continue
        if side == "above":
            if not (ln[b] < ln[a] and ln[b] < ln[c3]):
                continue
            if ln[c3] > ln[a] * 1.10:
                continue                       # 右肩可以略高，不能高太多
            level = max(hn[a:b + 1].max(), hn[b:c3 + 1].max())
        else:
            if not (hn[b] > hn[a] and hn[b] > hn[c3]):
                continue
            if hn[c3] < hn[a] * 0.90:
                continue
            level = min(ln[a:b + 1].min(), ln[b:c3 + 1].min())
        hit = _first_break(c3, cn, expand, level, side)
        if hit is not None:
            records.append((hit, float(level)))
    return records


def _event_column(records: list[tuple[int, float]], n: int) -> np.ndarray:
    arr = np.zeros(n, dtype=bool)
    for hit, _ in records:
        arr[hit] = True
    return arr


def _neck_column(records: list[tuple[int, float]], n: int) -> np.ndarray:
    """颈线值只在突破后 NECK_KEEP_BARS 根内可见，太老的支撑/压力位留着只会误导止损。"""
    col = np.full(n, np.nan)
    for hit, level in sorted(records):         # 后发生的覆盖先发生的
        col[hit:min(n, hit + NECK_KEEP_BARS)] = level
    return col


def _formations(p: dict, vol: pd.DataFrame) -> pd.DataFrame:
    cn = p["c"].to_numpy(float)
    ln = p["l"].to_numpy(float)
    hn = p["h"].to_numpy(float)
    n = len(cn)
    expand = vol["vol_expanding"].to_numpy(bool)
    trend = vol["vol_trend"]

    lows = np.where(_pivot_mask(ln, "min"))[0]
    highs = np.where(_pivot_mask(hn, "max"))[0]
    w = _double_scans(lows, cn, ln, hn, expand, "above")
    m = _double_scans(highs, cn, ln, hn, expand, "below")
    hs_bottom = _head_scans(lows, cn, ln, hn, expand, "above")
    hs_top = _head_scans(highs, cn, ln, hn, expand, "below")

    out = pd.DataFrame(index=p["c"].index)
    out["w_bottom_break"] = _event_column(w, n)
    out["m_top_break"] = _event_column(m, n)
    out["hs_bottom_break"] = _event_column(hs_bottom, n)
    out["hs_top_break"] = _event_column(hs_top, n)
    out["neckline_sup"] = _neck_column(sorted(w + hs_bottom), n)
    out["neckline_res"] = _neck_column(sorted(m + hs_top), n)

    idx = p["c"].index
    with np.errstate(invalid="ignore", divide="ignore"):
        fall = cn / p["c"].shift(FLAG_LEG_BARS).to_numpy(float) - 1
        rebound = cn / pd.Series(ln, index=idx).rolling(6).min().to_numpy(float) - 1
        out["v_reversal"] = _b(pd.Series((fall <= V_FALL) & (rebound >= V_REBOUND),
                                         index=p["c"].index))

        # 圆弧底要的是「碗」：有深度、碗底在左侧（近 10 根不再创新低）、已经弯上来一点、
        # 回升段温和放量且越涨越快（一阶差分加速，才是弧而不是 V 的右腿）。
        # 状态会因 vol_trend 反复穿越 1.0 而断续成立，所以要求前 40 根内没成立过才算一次，
        # 否则整段底部区域会以 3% 的频率天天 True。
        high_s, low_s = pd.Series(hn, index=idx), pd.Series(ln, index=idx)
        close_s = pd.Series(cn, index=idx)
        bowl = low_s.rolling(ARC_BARS).min()
        m10 = close_s.rolling(10).mean()
        arc_state = _b(
            (close_s >= bowl * (1 + ARC_RISE_MIN))
            & (close_s <= bowl * (1 + ARC_RISE_MAX))
            & (low_s.rolling(10).min() >= bowl * (1 + ARC_RISE_MIN))
            & (bowl <= high_s.rolling(ARC_BARS).max() * (1 - ARC_DEPTH))
            & (trend >= 1.0) & (trend <= 1.6)
            & ((m10 - m10.shift(5)) > (m10.shift(5) - m10.shift(10)))
        )
        fresh = _b(arc_state.astype(float).rolling(ARC_REFIRE_BARS).max().shift(1) < 1)
        out["arc_bottom"] = arc_state & fresh
    return out


def _flag_break(p: dict, trend: pd.Series, expand: pd.Series, up: bool) -> pd.Series:
    """旗形突破：旗杆（近 12 根内 ≥18% 的单边行情）→ 3~8 根缩量小实体整理 → 放量突破旗面沿。

    两条实现约束：
    1. 突破线取「旗面自己的高低点」，不是 12 根窗口的最高价——后者要求同一根 K 线既贴在
       窗口高点下方又创窗口高点上方，判据互斥，实测整表接近恒 False。
    2. 旗杆只要求「近 12 根内出现过」，不要求正好在整理前一根结束；按整理根数逐档枚举时
       后者会让 5314 个整理候选里只剩 223 个能当根突破，形态等于不存在。
    """
    c, h, l = p["c"], p["h"], p["l"]
    move = c / c.shift(FLAG_LEG_BARS) - 1
    quiet = trend <= 0.9                        # 整理期缩量：笔记里的「回调需要缩量」
    if up:
        pole = _b(move.rolling(FLAG_LEG_BARS).max() >= FLAG_LEG_MOVE)
        ref = h.rolling(FLAG_LEG_BARS).max()
        band = _b((c <= ref * (1 - FLAG_MIN_RETRACE)) & (c >= ref * (1 - FLAG_RETRACE)))
    else:
        pole = _b(move.rolling(FLAG_LEG_BARS).min() <= -FLAG_LEG_MOVE)
        ref = l.rolling(FLAG_LEG_BARS).min()
        band = _b((c >= ref * (1 + FLAG_MIN_RETRACE)) & (c <= ref * (1 + FLAG_RETRACE)))
    state = pole & band & _b(quiet)
    small = p["body"].div(p["rng"])
    hits = pd.Series(False, index=c.index)
    for n in range(FLAG_MIN_BARS, FLAG_MAX_BARS + 1):
        held = _b(state.astype(float).rolling(n).min().shift(1) == 1)
        tight = _b(small.rolling(n).mean().shift(1) <= FLAG_BODY_MAX)
        brk = _b(c > h.rolling(n).max().shift(1)) if up else _b(c < l.rolling(n).min().shift(1))
        hits = hits | (held & tight & brk & _b(expand))
    return hits


def _range_formations(p: dict, vol: pd.DataFrame) -> pd.DataFrame:
    """箱体/对称三角形/旗形/20 日支撑压力/回踩企稳/量价背离。"""
    o, c, h, l, v = p["o"], p["c"], p["h"], p["l"], p["v"]
    expand, trend = vol["vol_expanding"], vol["vol_trend"]
    ratio5 = v / v.rolling(5).mean().replace(0, np.nan)

    ma20 = c.rolling(20).mean()
    hi_box = h.rolling(BOX_BARS).max().shift(1)      # 昨日为止的区间上沿，突破判据不能用当日自身
    lo_box = l.rolling(BOX_BARS).min().shift(1)
    box = _b((hi_box - lo_box <= BOX_HEIGHT * ma20.shift(1)) & c.between(lo_box, hi_box))

    hi_tri, lo_tri = h.rolling(TRI_BARS).max(), l.rolling(TRI_BARS).min()
    width = hi_tri - lo_tri
    squeeze = _b((width.shift(1) <= width.shift(6) * TRI_SQUEEZE) & (trend.shift(1) <= 0.9))

    sup20 = l.rolling(20).min().shift(1)
    res20 = h.rolling(20).max().shift(1)

    out = pd.DataFrame(index=c.index)
    out["box_break_up"] = _lag(box, 1) & _b(c > hi_box) & expand
    out["box_break_dn"] = _lag(box, 1) & _b(c < lo_box) & expand
    out["box_upper"] = hi_box
    out["box_lower"] = lo_box
    out["tri_break_up"] = _lag(squeeze, 1) & _b(c > hi_tri.shift(1)) & expand
    out["tri_break_dn"] = _lag(squeeze, 1) & _b(c < lo_tri.shift(1)) & expand
    out["flag_up_break"] = _flag_break(p, trend, expand, up=True)
    out["flag_dn_break"] = _flag_break(p, trend, expand, up=False)
    out["sup20"] = sup20
    out["res20"] = res20
    # 回踩 20 日支撑后缩量收阳：笔记里的「低吸买点」
    out["pullback_stabilize"] = _b((l <= sup20 * 1.01) & (c > o) & (ratio5 <= VOL_SHRINK))
    # 价创新高但量能跟不上（量价背离）：分批止盈的参考
    out["vol_diverge"] = _b((c > res20) & (v < vol["vol_ma20"]) & (trend < 1.0))
    return out


PATTERN_FLOAT_COLUMNS = [
    "body_ratio", "upper_ratio", "lower_ratio", "gap_pct",
    "vol_ma20", "vol_trend", "vol_z",
    "neckline_sup", "neckline_res", "box_upper", "box_lower", "sup20", "res20",
]

PATTERN_BOOL_COLUMNS = [
    "vol_expanding", "vol_contracting", "vol_ground", "vol_blast",
    "hammer_low", "hanging_man_high", "shooting_star_high",
    "doji_low", "doji_high", "doji_high_blast",
    "bullish_engulf", "bearish_engulf", "morning_star", "evening_star",
    "piercing_line", "dark_cloud", "harami", "three_soldiers", "three_crows",
    "w_bottom_break", "m_top_break", "hs_bottom_break", "hs_top_break",
    "v_reversal", "arc_bottom",
    "box_break_up", "box_break_dn", "tri_break_up", "tri_break_dn",
    "flag_up_break", "flag_dn_break", "pullback_stabilize", "vol_diverge",
]

PATTERN_COLUMNS = PATTERN_FLOAT_COLUMNS + PATTERN_BOOL_COLUMNS


def add_pattern_columns(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上原位新增形态/量能列并返回；缺 OHLCV 或空表时原样返回。"""
    need = {"open", "high", "low", "close", "volume"}
    if df.empty or not need.issubset(df.columns):
        return df
    p = _parts(df)
    vol = _volume_context(p)

    out = pd.DataFrame(index=df.index)
    out["body_ratio"] = p["body"] / p["rng"]
    out["upper_ratio"] = p["upper"] / p["rng"]
    out["lower_ratio"] = p["lower"] / p["rng"]
    out["gap_pct"] = (p["o"] / p["c"].shift(1) - 1) * 100
    for col in vol.columns:
        out[col] = vol[col]

    parts = [_single_bar(p, vol, _pos60(df, p)), _combos(p), _formations(p, vol),
             _range_formations(p, vol)]
    for chunk in parts:
        for col in chunk.columns:
            out[col] = chunk[col]

    missing = [c for c in PATTERN_COLUMNS if c not in out.columns]
    extra = [c for c in out.columns if c not in PATTERN_COLUMNS]
    if missing or extra:
        raise RuntimeError(f"形态列与 PATTERN_COLUMNS 不一致 missing={missing} extra={extra}")

    for col in PATTERN_BOOL_COLUMNS:
        df[col] = _b(out[col])
    for col in PATTERN_FLOAT_COLUMNS:
        df[col] = out[col].astype(float)
    return df
