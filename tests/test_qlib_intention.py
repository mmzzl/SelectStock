"""批次 S：QuantPosition 四层「主力意图」判据移植的单元测试（docs §29）。

移植要求是**忠实照搬**原阈值（`/data/work/QuantPosition/apps/api/services/review_service.py`），
所以每条测试都对着原作者手调的那个数写：涨跌量比 1.3、缩量比 0.7、量堆 ≥2 日、
涨停 = 当日涨幅 ≥9.5%、均价强势 = ≥65% 的 bar 收在 VWAP 上方且均价上斜……
分时时段按真实数据固定为 48 根：早盘 09:35→10:00（6 根）、午盘 10:05→14:00（30 根）、
尾盘 14:05→15:00（12 根）。
"""
from __future__ import annotations

from datetime import date, timedelta

from stock_selector_qlib import intention as it

AM = [f"09:{m:02d}" for m in range(35, 60, 5)] + [f"10:{m:02d}" for m in range(0, 60, 5)] \
     + [f"11:{m:02d}" for m in range(0, 31, 5)]
PM = [f"13:{m:02d}" for m in range(5, 60, 5)] + [f"14:{m:02d}" for m in range(0, 60, 5)] + ["15:00"]
TIMES = (AM + PM)[:48]
assert len(TIMES) == 48 and TIMES[0] == "09:35" and TIMES[-1] == "15:00"


def bar(i: int, *, close=10.0, vol=1000, open=None, high=None, low=None) -> dict:
    """第 i 根分时 bar；high/low 默认贴着收盘价 ±0.02，让 (高+低+收)/3 == 收。"""
    o = close if open is None else open
    return {"date": f"2026-09-29 {TIMES[i]}", "open": o,
            "high": close + 0.02 if high is None else high,
            "low": close - 0.02 if low is None else low,
            "close": close, "volume": vol}


def bars(n=48, **kw) -> list[dict]:
    return [bar(i, **kw) for i in range(n)]


def patch(rows: list[dict], a: int, b: int, **kw) -> None:
    """改 [a,b) 这一段：值给列表逐根取，给标量整段同值。

    改了 close 而没同时给 high/low 时顺带重推上下影，免得造出 low>close 的假 bar。
    """
    for i in range(a, min(b, len(rows))):
        j = i - a
        got = {}
        for key, val in kw.items():
            if isinstance(val, (list, tuple)):
                val = val[j] if j < len(val) else rows[i].get({"vol": "volume"}.get(key, key))
            got[key] = val
        derived = {}
        if "close" in got:
            if "high" not in got:
                derived["high"] = got["close"] + 0.02
            if "low" not in got:
                derived["low"] = got["close"] - 0.02
        for key, val in {**got, **derived}.items():
            rows[i][{"vol": "volume"}.get(key, key)] = val


def daily(closes: list[float], *, opens=None, vols=None, highs=None, lows=None) -> list[dict]:
    """日线行；不给 open 就用昨收（于是「收≥开」正好等于当日不跌），不给量就 1000。"""
    out = []
    for i, c in enumerate(closes):
        o = (closes[i - 1] if i else c) if opens is None else \
            (opens[i] if i < len(opens) else opens[-1])
        v = 1000 if vols is None else (vols[i] if i < len(vols) else vols[-1])
        h = max(o, c) + 0.05 if highs is None else (highs[i] if i < len(highs) else highs[-1])
        lo = min(o, c) - 0.05 if lows is None else (lows[i] if i < len(lows) else lows[-1])
        out.append({"date": (date(2026, 6, 1) + timedelta(days=i)).isoformat(),
                    "open": o, "high": h, "low": lo, "close": c, "volume": v})
    return out


# 一个「低位 + 上涨放量下跌缩量 + 两天量堆」的日线样本，后面多处当吸筹素材用
ACCUM = daily([10 + i * 0.1 for i in range(12)] + [11.2 - i * 0.1 for i in range(8)],
              vols=[1000] * 10 + [5000] * 2 + [300] * 8)


# ── 第 1 层：位置定大方向 ────────────────────────────────────────────

def test_位置按阶段涨幅分三档():
    assert it.position_stage(daily([10.0, 10.2, 10.3, 10.4, 16.0])) == "高位"    # +60% > 50%
    assert it.position_stage(daily([10.0, 10.2, 10.3, 10.4, 12.0])) == "中段"    # +20%
    assert it.position_stage(daily([10.0, 10.2, 10.3, 10.4, 11.0])) == "低位"    # +10%


def test_日线不足5根时位置退成中段():
    assert it.position_stage(daily([10.0, 20.0])) == "中段"


# ── 第 2 层：日线量能辨真假 ──────────────────────────────────────────

def test_只有一天放量不算持续量堆于是降级成洗盘量():
    vols = [1000] * 11 + [5000] + [300] * 8
    res = it.daily_volume_trend(daily([10 + i * 0.1 for i in range(12)]
                                      + [11.2 - i * 0.1 for i in range(8)], vols=vols))
    assert res["has_volume_pile"] is False
    assert res["pattern"] == "洗盘量"


def test_量堆满两天才判吸筹量():
    res = it.daily_volume_trend(ACCUM)
    assert res["has_volume_pile"] is True
    assert res["pattern"] == "吸筹量"


def test_下跌放量判出货量():
    vols = [3000] * 14 + [500] * 6
    res = it.daily_volume_trend(daily([12 - i * 0.1 for i in range(14)]
                                      + [10.6 + i * 0.1 for i in range(6)], vols=vols))
    assert res["pattern"] == "出货量"


def test_日线不足10根时量能判数据不足():
    assert it.daily_volume_trend(daily([10.0, 10.1, 10.2]))["pattern"] == "数据不足"


# ── 第 3 层：日线序列与形态 ──────────────────────────────────────────

def test_涨停后缩量():
    rows = daily([10, 11, 11, 11, 11, 11], vols=[2000, 2000, 500, 500, 500, 500])
    assert it.daily_sequences(rows) == ["涨停后缩量"]


def test_涨停后放量滞涨():
    rows = daily([10, 11, 11, 11, 11, 11], vols=[1000, 1000, 3000, 3000, 3000, 3000])
    assert it.daily_sequences(rows) == ["涨停后放量滞涨"]


def test_涨停后继续上攻():
    assert it.daily_sequences(daily([10, 11, 11.4, 11.9, 12.5, 13.1])) == ["涨停后继续上攻"]


def test_量价齐升要连阳且量递增():
    rows = daily([10, 10.2, 10.4, 10.6, 10.8, 11.0],
                 vols=[1000, 1000, 1000, 1200, 1500, 2000])
    assert it.daily_sequences(rows) == ["量价齐升"]


def test_连阴缩量():
    rows = daily([11, 10.8, 10.6, 10.4, 10.2, 10.0],
                 vols=[2000, 1800, 1600, 1400, 1200, 1000])
    assert it.daily_sequences(rows) == ["连阴缩量"]


def test_涨停线定在9_5():
    below = daily([10, 10.94, 10.94, 10.94, 10.94, 10.94],
                  vols=[2000, 2000, 500, 500, 500, 500])
    assert it.daily_sequences(below) == []                 # +9.4% 不算涨停
    above = daily([10, 10.96, 10.96, 10.96, 10.96, 10.96],
                  vols=[2000, 2000, 500, 500, 500, 500])
    assert it.daily_sequences(above) == ["涨停后缩量"]      # +9.6% 算


def test_长下影():
    rows = daily([10.0] * 14 + [10.1], opens=[10.0] * 14 + [10.05],
                 lows=[9.95] * 14 + [9.6], highs=[10.05] * 14 + [10.12])
    assert "长下影" in it.daily_patterns(rows)


def test_避雷针():
    rows = daily([10.0] * 14 + [10.05], opens=[10.0] * 15,
                 lows=[9.95] * 15, highs=[10.05] * 14 + [10.5])
    assert "避雷针" in it.daily_patterns(rows)


def test_W底要两边都探到同一低点区():
    lows = [10.6] * 5 + [9.2, 10.5, 10.6, 9.0, 10.4, 10.2, 9.1, 10.7, 10.9]
    rows = daily([11.0] * 15, lows=lows, highs=[11.1] * 15)
    assert "W底" in it.daily_patterns(rows)


def test_假破位():
    assert "假破位" in it.daily_patterns(daily([10.0] * 12 + [10.0, 9.8, 10.0]))


# ── 第 4 层：当日分时盘口 ────────────────────────────────────────────

def test_均价强势要多数在均价上方且均价上斜():
    bs = bars()
    patch(bs, 0, 8, close=9.0, vol=500)        # 早盘放量砸低，均价被拖下来
    patch(bs, 8, 48, close=10.0, vol=50)
    status, vwap = it.vwap_state(bs)
    assert status == "强势"
    assert 9.3 < vwap < 9.4


def test_均价弱势要多数在均价下方且均价下斜():
    bs = bars()
    patch(bs, 0, 8, close=10.0, vol=500)
    patch(bs, 8, 48, close=9.0, vol=50)
    assert it.vwap_state(bs)[0] == "弱势"


def test_均价不动时判震荡():
    assert it.vwap_state(bars())[0] == "震荡"


def test_均价强势的占比线在65():
    bs = bars()
    patch(bs, 0, 19, close=10.0)
    patch(bs, 19, 48, close=11.0)          # 29/48 = 60% 在均价上方，不够 65%
    assert it.vwap_state(bs)[0] == "震荡"
    patch(bs, 18, 19, close=11.0)          # 再加一根到 30/48 = 62.5%……还不算
    assert it.vwap_state(bs)[0] == "震荡"
    patch(bs, 17, 18, close=11.0)          # 31/48 = 64.6%，差一点
    assert it.vwap_state(bs)[0] == "震荡"
    patch(bs, 16, 17, close=11.0)          # 32/48 = 66.7% ⇒ 达标
    assert it.vwap_state(bs)[0] == "强势"


def test_早盘放量急拉后缩量回落判出货():
    bs = bars()
    patch(bs, 0, 3, vol=10000, close=[10.0, 10.2, 10.4])
    signal, detail = it.intraday_volume_state(bs)
    assert signal == "出货"
    assert "急拉" in detail


def test_下跌放量加尾盘放量跳水判出货():
    bs = bars()
    patch(bs, 36, 48, vol=3000)
    patch(bs, 0, 48, open=10.02, close=10.0)
    patch(bs, 0, 4, open=9.98, close=10.0)     # 只有 4 根收在开盘上方
    signal, detail = it.intraday_volume_state(bs)
    assert signal == "出货"
    assert "跳水" in detail


def test_上涨放量加尾盘拉升判洗盘():
    bs = bars()
    patch(bs, 36, 48, vol=3000)
    patch(bs, 0, 48, open=9.98, close=10.0)
    patch(bs, 0, 4, open=10.02, close=10.0)
    assert it.intraday_volume_state(bs)[0] == "洗盘"


def test_盘中突然大单拉升判试盘():
    bs = bars()
    patch(bs, 10, 12, vol=40000, close=11.0)
    assert it.intraday_volume_state(bs)[0] == "试盘"


def test_量价没有背离时判震荡():
    assert it.intraday_volume_state(bars()) == ("震荡", "无明显量价背离")


def test_分时不足10根时量能判数据不足():
    assert it.intraday_volume_state(bars(5)) == ("震荡", "数据不足")


def test_尾盘放量拉升识别为尾盘抢筹型():
    bs = bars()
    patch(bs, 42, 48, vol=5000, close=[10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    assert it.intraday_pattern(bs, "震荡", "震荡") == "尾盘抢筹型"


def test_弱势加出货加双头识别为M头分时():
    bs = bars()
    patch(bs, 1, 2, high=10.6)
    patch(bs, 5, 6, high=10.6)
    assert it.intraday_pattern(bs, "弱势", "出货") == "M头分时"


def test_弱势加出货加一路阴跌识别为高开低走():
    bs = bars()
    patch(bs, 0, 48, close=[10.6] + [10.6 - i * 0.02 for i in range(1, 48)])
    patch(bs, 0, 1, high=11.0)                 # 早盘那一冲高，把双头判据的两段拉开
    assert it.intraday_pattern(bs, "弱势", "出货") == "高开低走阴跌型"


def test_后半日抬高识别为U型洗盘():
    bs = bars()
    patch(bs, 0, 24, close=10.0)
    patch(bs, 24, 48, close=10.5)
    assert it.intraday_pattern(bs, "震荡", "洗盘") == "U型洗盘分时"


def test_没有分时形态时是震荡平衡形态():
    bs = bars()
    patch(bs, 0, 1, high=11.0)                 # 没有第二个头，就不算 M 头
    assert it.intraday_pattern(bs, "震荡", "震荡") == "震荡平衡形态"


def test_尾盘信号三档():
    up = bars()
    patch(up, 42, 48, vol=5000, close=[10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    down = bars()
    patch(down, 42, 48, vol=5000, close=[10.5, 10.4, 10.3, 10.2, 10.1, 10.0])
    assert it.tail_state(up) == "抢筹"
    assert it.tail_state(down) == "放量跳水"
    assert it.tail_state(bars()) == "无量横盘"


# ── 四层合成：主力意图决策树 ─────────────────────────────────────────

def test_低位加吸筹量加量堆判吸筹且置信度高():
    res = it.main_force_intention("低位", ACCUM, "震荡", "震荡", "震荡平衡形态", "无量横盘")
    assert res["intention"] == "吸筹"
    assert res["intention_confidence"] == "高"
    assert "持续量堆" in res["intention_detail"]


def test_低位加当日洗盘强势也能判吸筹():
    rows = daily([10 + i * 0.1 for i in range(12)] + [11.2 - i * 0.1 for i in range(8)],
                 vols=[1000] * 11 + [5000] + [300] * 8)      # 只有 1 根量堆 ⇒ 洗盘量
    res = it.main_force_intention("低位", rows, "强势", "洗盘", "U型洗盘分时", "抢筹")
    assert res["intention"] == "吸筹"
    assert res["intention_confidence"] == "中"


def test_中段加出货信号但昨大跌今企稳判假出货诱空():
    rows = daily([10.0] * 10 + [11.0, 10.5, 10.8])
    res = it.main_force_intention("中段", rows, "震荡", "出货", "M头分时", "无量横盘")
    assert res["intention"] == "假出货诱空"
    assert res["intention_confidence"] == "高"
    assert "昨大跌今企稳" in res["intention_detail"]


def test_中段加缩量回调加均价强势判洗盘():
    res = it.main_force_intention("中段", ACCUM, "强势", "洗盘", "U型洗盘分时", "抢筹")
    assert res["intention"] == "洗盘"
    assert res["intention_confidence"] == "高"
    assert "均价支撑" in res["intention_detail"]


def test_高位只要出现派发信号就判真出货():
    flat = daily([10.0] * 12)
    assert it.main_force_intention("高位", flat, "震荡", "出货", "M头分时", "无量横盘")[
        "intention"] == "真出货"
    assert it.main_force_intention("高位", flat, "震荡", "震荡", "震荡平衡形态", "放量跳水")[
        "intention"] == "真出货"


def test_高位量价尚可判高位震荡且置信度低():
    res = it.main_force_intention("高位", daily([10.0] * 12), "震荡", "震荡",
                                  "震荡平衡形态", "无量横盘")
    assert res["intention"] == "高位震荡"
    assert res["intention_confidence"] == "低"


def test_位置不明时量价齐升兜底判吸筹():
    rows = daily([10.0] * 14 + [10.2, 10.4, 10.6, 10.8, 11.0],
                 vols=[1000] * 14 + [1000, 1200, 1500, 2000, 2500])
    res = it.main_force_intention("中段", rows, "震荡", "震荡", "震荡平衡形态", "无量横盘")
    assert res["intention"] == "吸筹"
    assert res["intention_detail"].endswith("量价齐升")


# ── 汇总入口：judge ─────────────────────────────────────────────────

def test_没有当日分时就不给判定():
    assert it.judge(ACCUM, []) is None


def test_judge把四层结果一起交出来():
    bs = bars()
    patch(bs, 0, 8, close=9.0, vol=500)
    patch(bs, 8, 48, close=10.0, vol=50)
    res = it.judge(ACCUM, bs)
    assert set(res) >= {"position", "vwap_status", "volume_signal", "pattern", "tail_signal",
                        "daily_vol_pattern", "daily_patterns", "sequences",
                        "intention", "intention_detail", "intention_confidence"}
    assert res["position"] == "低位"
    assert res["vwap_status"] == "强势"
    assert res["intention"] == "吸筹"
