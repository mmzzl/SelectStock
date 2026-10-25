"""批次 S：QuantPosition「主力意图」四层判据的忠实移植（docs §29）。

只搬判据、不接线上。输入是**已经由调用方截到信号日 T** 的日线行 + T 当日 48 根分时 bar，
纯函数不碰数据源、不看未来。阈值原样照抄
`/data/work/QuantPosition/apps/api/services/review_service.py`，一个数都没改——
本批要测的是这套别人手调的阈值在我们的池子里到底还有没有信息，改阈值是下一批的事。

行的字段约定：`{date, open, high, low, close, volume}`；分时行的 `date` 是
`"YYYY-MM-DD HH:MM"`（时段划分靠这个字符串，和原作者一样）。
"""
from __future__ import annotations

Row = dict

# 原作者的分时三段划分（09:35→10:00 / 10:05→14:00 / 14:05→15:00）
_MORN_END = "10:00"
_AFTER_NOON_END = "14:00"
_TAIL_START = "14:05"
_LIMIT_UP = 0.095          # 他把「当日涨幅 ≥9.5%」当涨停（他的票池剔了 300/688）
_BEARISH = ("M头分时", "高开低走阴跌型", "早盘脉冲全天回落")


def _avg(rows: list[Row]) -> float:
    return sum(r["volume"] for r in rows) / max(len(rows), 1)


def _time(row: Row) -> str:
    return str(row["date"])[11:16]


# ── 第 1 层：位置定大方向 ────────────────────────────────────────────

def position_stage(daily_rows: list[Row]) -> str:
    """阶段涨幅 >50% 高位、>15% 中段，其余低位；日线不足 5 根时退回中段。"""
    if len(daily_rows) < 5:
        return "中段"
    closes = [r["close"] for r in daily_rows]
    gain = (closes[-1] - closes[0]) / closes[0]
    if gain > 0.50:
        return "高位"
    if gain > 0.15:
        return "中段"
    return "低位"


# ── 第 2 层：日线量能辨真假 ──────────────────────────────────────────

def daily_volume_trend(daily_rows: list[Row]) -> dict:
    """近 20 日的涨跌量比、下跌缩量比、量堆天数。"""
    if len(daily_rows) < 10:
        return {"pattern": "数据不足", "up_vol_ratio": 0, "down_vol_shrink": 0,
                "has_volume_pile": False}
    recent = daily_rows[-20:]
    up = [r for r in recent if r["close"] >= r["open"]]
    down = [r for r in recent if r["close"] < r["open"]]
    up_vol = sum(r["volume"] for r in up)
    down_vol = sum(r["volume"] for r in down)
    up_down_ratio = up_vol / max(down_vol, 1)
    avg_up_vol = up_vol / max(len(up), 1)
    avg_down_vol = down_vol / max(len(down), 1)
    down_shrink_ratio = avg_down_vol / max(avg_up_vol, 1)

    vols = [r["volume"] for r in recent]
    avg_vol = sum(vols) / max(len(vols), 1)
    std = (sum((v - avg_vol) ** 2 for v in vols) / max(len(vols), 1)) ** 0.5
    threshold = avg_vol + std * 1.5
    pile_days = sum(1 for v in vols if v > threshold and v > avg_vol * 1.8)
    has_pile = pile_days >= 2

    if up_down_ratio > 1.3 and down_shrink_ratio < 0.7 and has_pile:
        pattern = "吸筹量"
    elif down_shrink_ratio < 0.6:
        pattern = "洗盘量"
    elif up_down_ratio < 0.7:
        pattern = "出货量"
    else:
        pattern = "震荡量"
    return {"pattern": pattern, "up_vol_ratio": round(up_down_ratio, 2),
            "down_shrink_ratio": round(down_shrink_ratio, 2), "has_volume_pile": has_pile}


# ── 第 3 层：日线连续动作与 K 线形态 ─────────────────────────────────

def daily_sequences(daily_rows: list[Row]) -> list[str]:
    """近 6 日的连续动作：涨停后缩量/放量滞涨/继续上攻、量价齐升、连阴缩量、高位放量滞涨。"""
    seq: list[str] = []
    if len(daily_rows) < 6:
        return seq
    recent = daily_rows[-6:]
    closes = [r["close"] for r in recent]
    volumes = [r["volume"] for r in recent]
    avg_vol = sum(volumes) / max(len(volumes), 1)

    gains: list[float] = []
    limit_up = [False] * len(recent)
    for i in range(1, len(recent)):
        prev, cur = recent[i - 1], recent[i]
        gain = (cur["close"] - prev["close"]) / max(prev["close"], 0.01)
        gains.append(gain)
        limit_up[i] = gain >= _LIMIT_UP

    idxs = [i for i in range(1, len(recent)) if limit_up[i]]
    if idxs:
        idx = idxs[-1]
        days_after = len(recent) - 1 - idx
        vol_after = sum(volumes[idx + 1:]) / max(days_after, 1)
        vol_ratio = vol_after / max(avg_vol, 1)
        if days_after >= 1:
            after = gains[idx:]
            avg_after = sum(after) / max(len(after), 1)
            if avg_after < 0.005 and vol_ratio < 0.8:
                seq.append("涨停后缩量")
            elif avg_after < 0.005 and vol_ratio >= 1.2:
                seq.append("涨停后放量滞涨")
            elif avg_after > 0.02:
                seq.append("涨停后继续上攻")

    if sum(1 for g in gains[-3:] if g > 0) >= 2:
        if all(volumes[-3 + i] < volumes[-2 + i] for i in range(2)):
            seq.append("量价齐升")

    if sum(1 for g in gains[-3:] if g < 0) >= 2:
        if all(volumes[-3 + i] >= volumes[-2 + i] for i in range(2)):
            seq.append("连阴缩量")

    if len(gains) >= 3:
        if abs(sum(gains[-3:])) < 0.02 and max(volumes[-3:]) > avg_vol * 1.5:
            seq.append("高位放量滞涨")
    return seq


def daily_patterns(daily_rows: list[Row]) -> list[str]:
    """近 15 日的 K 线形态：长下影、避雷针、W 底、假破位。"""
    if len(daily_rows) < 10:
        return []
    recent = daily_rows[-15:]
    closes = [r["close"] for r in recent]
    highs = [r["high"] for r in recent]
    lows = [r["low"] for r in recent]
    out: list[str] = []

    for i in range(-5, 0):
        r = recent[i]
        body = abs(r["close"] - r["open"])
        lower = min(r["open"], r["close"]) - r["low"]
        upper = r["high"] - max(r["open"], r["close"])
        if body > 0 and lower > body * 2:
            out.append("长下影")
        if body > 0 and upper > body * 2:
            out.append("避雷针")

    low_10 = min(lows[-10:])
    low_idx = lows[-10:].index(low_10) if len(lows) >= 10 else 0
    if 2 < low_idx < 8:
        before = lows[-10:-10 + low_idx]
        after = lows[-10 + low_idx + 1:]
        threshold = low_10 * 1.03
        if sum(1 for x in before if x <= threshold) >= 1 \
                and sum(1 for x in after if x <= threshold) >= 1:
            out.append("W底")

    if len(closes) >= 3:
        prev, mid, last = closes[-3], closes[-2], closes[-1]
        if prev > mid and last > mid and abs(last - prev) / max(prev, 0.01) < 0.03:
            out.append("假破位")
    return list(set(out))


# ── 第 4 层：当日分时盘口 ────────────────────────────────────────────

def vwap_state(bars_5m: list[Row]) -> tuple[str, float | None]:
    """均价线关系：≥65% 的 bar 收在 VWAP 上方且后半日抬高 = 强势，反之弱势。"""
    if not bars_5m:
        return "震荡", None
    total_pv = sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in bars_5m)
    total_v = sum(b["volume"] for b in bars_5m)
    vwap = total_pv / total_v if total_v > 0 else bars_5m[-1]["close"]

    ratio = sum(1 for b in bars_5m if b["close"] >= vwap) / len(bars_5m)
    half = len(bars_5m) // 2
    first, second = bars_5m[:half], bars_5m[half:]
    first_mean = sum(b["close"] for b in first) / len(first) if first else vwap
    second_mean = sum(b["close"] for b in second) / len(second) if second else vwap
    slope = second_mean - first_mean

    if ratio >= 0.65 and slope > 0:
        return "强势", vwap
    if ratio <= 0.35 and slope < 0:
        return "弱势", vwap
    return "震荡", vwap


def intraday_volume_state(bars_5m: list[Row]) -> tuple[str, str]:
    """三段量能：出货 / 洗盘 / 试盘 / 震荡。"""
    if len(bars_5m) < 10:
        return "震荡", "数据不足"
    morning = [b for b in bars_5m if str(b["date"])[11:13] in ("09", "10")
               and _time(b) <= _MORN_END]
    afternoon = [b for b in bars_5m if "10:05" <= _time(b) <= _AFTER_NOON_END]
    tail = [b for b in bars_5m if _time(b) >= _TAIL_START]

    morning_vol = _avg(morning) if morning else 0.0
    afternoon_vol = _avg(afternoon) if afternoon else 0.0
    tail_vol = _avg(tail) if tail else 0.0

    total_vol = sum(b["volume"] for b in bars_5m)
    up_vol = sum(b["volume"] for b in bars_5m if b["close"] >= b["open"])
    down_vol = total_vol - up_vol
    up_down_ratio = up_vol / max(down_vol, 1)
    overall = total_vol / max(len(bars_5m), 1)
    up_close = bars_5m[-1]["close"] >= bars_5m[-1]["open"]

    morning_spike = morning_vol > afternoon_vol * 1.5 and afternoon_vol > 0
    early_highs = [b["close"] for b in bars_5m[:5]]
    early_rising = len(early_highs) >= 2 and max(early_highs) > early_highs[0]
    if morning_spike and early_rising:
        if _avg(bars_5m[3:8]) < _avg(bars_5m[:3]) * 0.4:
            return "出货", "早盘放量急拉后缩量回落，量价背离"

    if down_vol > up_vol * 1.5 and tail_vol > overall * 1.3 and not up_close:
        return "出货", "下跌放量，尾盘放量跳水"

    if up_down_ratio > 1.5 and tail_vol > overall * 1.2 and up_close:
        return "洗盘", "下跌缩量上涨放量，尾盘放量拉升"

    first_third = bars_5m[:len(bars_5m) // 3]
    max_first_vol = max((b["volume"] for b in first_third), default=0)
    if max_first_vol > overall * 2:
        peak_close = max(b["close"] for b in first_third)
        if peak_close > first_third[0]["close"] * 1.03:
            after = bars_5m[len(bars_5m) // 3:]
            if _avg(after) < max_first_vol * 0.3:
                return "试盘", "盘中突然大单拉升测试抛压"

    return "震荡", "无明显量价背离"


def intraday_pattern(bars_5m: list[Row], vwap_status: str, volume_signal: str) -> str:
    """分时形态：尾盘抢筹 / M头 / 高开低走 / 早盘脉冲回落 / U型洗盘 / 震荡平衡。"""
    if len(bars_5m) < 20:
        return "震荡"
    closes = [b["close"] for b in bars_5m]
    highs = [b["high"] for b in bars_5m]
    half = len(closes) // 2
    first_mean = sum(closes[:half]) / max(half, 1)
    second_mean = sum(closes[half:]) / max(len(closes) - half, 1)

    tail_bars = bars_5m[-6:]
    tail_vol = _avg(tail_bars)
    overall_vol = _avg(bars_5m)
    tail_rising = tail_bars[-1]["close"] > tail_bars[0]["close"]

    early_high = max(highs[:6]) if len(highs) >= 6 else 0
    second_peak = max(highs[3:9]) if len(highs) >= 9 else 0
    double_top = bool(early_high > 0 and second_peak > 0
                      and abs(early_high - second_peak) / max(early_high, 0.01) < 0.03)

    if tail_vol > overall_vol * 1.5 and tail_rising:
        return "尾盘抢筹型"

    if vwap_status == "弱势" and volume_signal == "出货":
        if double_top:
            return "M头分时"
        if closes[0] > closes[-1] and closes[0] > closes[len(closes) // 4] * 1.02:
            return "高开低走阴跌型"
        if (bars_5m[0]["close"] < bars_5m[min(2, len(bars_5m) - 1)]["close"]
                and closes[-1] < closes[0]):
            return "早盘脉冲全天回落"

    if volume_signal == "洗盘":
        if second_mean > first_mean and closes[-1] > closes[0]:
            return "U型洗盘分时"
        if second_mean >= first_mean * 1.01 and closes[-1] > closes[0]:
            return "单边震荡上行"

    if double_top:
        return "M头分时"
    return "震荡平衡形态"


def tail_state(bars_5m: list[Row]) -> str:
    """最后 6 根的量能+方向：抢筹 / 放量跳水 / 无量横盘。"""
    tail = bars_5m[-6:]
    tail_vol = sum(b["volume"] for b in tail)
    overall = sum(b["volume"] for b in bars_5m) / max(len(bars_5m), 1)
    if tail_vol > len(tail) * overall * 1.5:
        if tail[-1]["close"] > tail[0]["close"]:
            return "抢筹"
        if tail[-1]["close"] < tail[0]["close"]:
            return "放量跳水"
    return "无量横盘"


# ── 四层合成：主力意图决策树 ─────────────────────────────────────────

def main_force_intention(position: str, daily_rows: list[Row], vwap_status: str,
                         volume_signal: str, pattern: str, tail_signal: str) -> dict:
    """位置定方向 → 量能辨真假 → K 线看底仓 → 盘口拆演戏，合成意图 + 置信度。"""
    vol_trend = daily_volume_trend(daily_rows)
    shapes = daily_patterns(daily_rows)
    seqs = daily_sequences(daily_rows)

    intention, confidence = "震荡", "低"
    why: list[str] = []

    if position == "低位":
        accum_seq = "量价齐升" in seqs
        if vol_trend["pattern"] == "吸筹量" or accum_seq:
            intention, confidence = "吸筹", "中"
            why.append("低位+上涨放量下跌缩量")
            if vol_trend["has_volume_pile"]:
                why.append("持续量堆")
                confidence = "高"
            if accum_seq:
                why.append("量价齐升")
                confidence = "高"
            if "长下影" in shapes or "W底" in shapes:
                why.append("底部形态")
                confidence = "高"
        elif volume_signal == "洗盘" and vwap_status == "强势":
            intention, confidence = "吸筹", "中"
            why.append("低位+今日洗盘企稳")

    elif position == "中段":
        repaired = "假破位" in shapes or "长下影" in shapes

        yesterday_drop_recovery = False
        if len(daily_rows) >= 3:
            d2, d1, d0 = daily_rows[-3], daily_rows[-2], daily_rows[-1]
            fell = (d1["close"] - d2["close"]) / max(d2["close"], 0.01) < -0.02
            bounced = (d0["close"] - d1["close"]) / max(d1["close"], 0.01) > 0.01
            yesterday_drop_recovery = fell and bounced

        is_wash = (volume_signal in ("洗盘", "试盘") and vwap_status != "弱势"
                   and tail_signal != "放量跳水")
        seq_wash = "涨停后缩量" in seqs or "连阴缩量" in seqs
        seq_dist = "涨停后放量滞涨" in seqs or "高位放量滞涨" in seqs
        seq_strong = "涨停后继续上攻" in seqs

        if volume_signal == "出货":
            if repaired or yesterday_drop_recovery:
                intention = "假出货诱空"
                why.append("中段+出货假象")
                if yesterday_drop_recovery:
                    why.append("昨大跌今企稳")
                if repaired:
                    why.append("形态修复")
                confidence = "高" if (vwap_status != "弱势" and yesterday_drop_recovery) else "中"
                if "长下影" in shapes:
                    why.append("长下影确认支撑")
            elif seq_dist:
                intention, confidence = "出货风险", "中"
                why.append("中段+连续放量滞涨")
            elif pattern in _BEARISH or vwap_status == "弱势":
                intention = "出货风险" if vwap_status == "弱势" else "假出货诱空"
                confidence = "中"
                why.append("中段+价量背离")
            else:
                intention, confidence = "假出货诱空", "低"
                why.append("中段+出货信号不清")
        elif is_wash or seq_wash:
            intention, confidence = "洗盘", "中"
            why.append("中段+缩量回调")
            if seq_wash:
                why.append(seqs[0] if seqs else "连续缩量")
                confidence = "高"
            if vol_trend["pattern"] == "洗盘量" or vol_trend["down_shrink_ratio"] < 0.6:
                why.append("量能萎缩主力惜售")
                confidence = "高"
            if vwap_status == "强势":
                why.append("均价支撑")
                confidence = "高"
        elif seq_strong:
            intention, confidence = "吸筹", "高"
            why.append("中段+涨停后继续上攻")

    elif position == "高位":
        seq_high_dist = "高位放量滞涨" in seqs or "涨停后放量滞涨" in seqs
        real_dist = (volume_signal == "出货" or vwap_status == "弱势" or pattern in _BEARISH
                     or tail_signal == "放量跳水" or "避雷针" in shapes or seq_high_dist)
        if real_dist:
            intention, confidence = "真出货", "高"
            why.append("高位+主力派发信号")
            if vol_trend["pattern"] == "出货量":
                why.append("阴量放大")
            if tail_signal == "放量跳水":
                why.append("尾盘跳水无承接")
        else:
            intention, confidence = "高位震荡", "低"
            why.append("高位+量价尚可")

    if intention == "震荡":
        if "量价齐升" in seqs:
            intention, confidence = "吸筹", "中"
            why.append("量价齐升")
        elif "涨停后继续上攻" in seqs:
            intention, confidence = "吸筹", "高"
            why.append("涨停后继续上攻")
        elif "连阴缩量" in seqs and position in ("低位", "中段"):
            intention, confidence = "洗盘", "中"
            why.append("连阴缩量洗盘")
        elif position in ("低位", "中段") and vwap_status == "强势" and volume_signal != "出货":
            intention = "洗盘" if position == "中段" else "吸筹"
            confidence = "低"
            why.append(f"{position}+均线支撑")

    return {"intention": intention,
            "intention_detail": "，".join(why) if why else "信号不明确",
            "intention_confidence": confidence,
            "daily_vol_pattern": vol_trend["pattern"],
            "daily_patterns": shapes,
            "sequences": seqs}


# ── 汇总入口 ────────────────────────────────────────────────────────

def judge(daily_rows: list[Row], bars_5m: list[Row]) -> dict | None:
    """一只票在信号日的四层判定；没有当日分时就不给判定（原作者也是直接跳过）。"""
    if not bars_5m:
        return None
    position = position_stage(daily_rows)
    vwap_status, vwap = vwap_state(bars_5m)
    volume_signal, volume_detail = intraday_volume_state(bars_5m)
    pattern = intraday_pattern(bars_5m, vwap_status, volume_signal)
    tail_signal = tail_state(bars_5m)
    force = main_force_intention(position, daily_rows, vwap_status, volume_signal,
                                 pattern, tail_signal)
    return {"position": position, "vwap": vwap, "vwap_status": vwap_status,
            "volume_signal": volume_signal, "volume_detail": volume_detail,
            "pattern": pattern, "tail_signal": tail_signal, **force}
