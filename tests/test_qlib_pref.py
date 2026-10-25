"""热门偏好名单（pref.py）四条判据的口径测试。

用户明确要的名单形状：热门板块 + 新闻利好 + 排除小市值 + 趋势向上。
这里只测**纯函数**（给定输入表 → 给定输出），不碰 qlib、不碰磁盘、不碰 HTTP，
所以每一条都能在毫秒级跑完，也每一条都能独立失败。

测的东西按他提的顺序排列，另外钉三件容易被糊过去的事：
1. 合成分必须单调（哪一路强，分就高），而且是 0~1（落库和 App 都按 0~1 走，百分制是展示层的事）；
2. 市值/趋势是**硬闸**，不是加分项——分数再高，闸不过就不进名单；
3. 趋势判据只能用当日及以前的 K 线（剪掉未来不改变判定），这条和护栏那条自求是同一个规矩。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_selector_qlib import pref


def _board():
    """四个板块：热度差异全靠 ret/ratio/涨停家数拉开。"""
    return pd.DataFrame([
        # kind, board_code, board_name, stock_count, ret_pct, turnover_ratio, limit_up_count
        ("concept", "BK1", "机器人", 20, 8.0, 2.0, 5),
        ("concept", "BK2", "光伏", 20, 3.0, 1.2, 1),
        ("concept", "BK3", "冷门", 20, -2.0, 0.8, 0),
        ("concept", "BK4", "三票小板块", 3, 99.0, 9.0, 3),   # 成员太少，噪声板块
    ], columns=["kind", "board_code", "board_name", "stock_count",
                "ret_pct", "turnover_ratio", "limit_up_count"])


def _member():
    return pd.DataFrame([
        ("BK1", "机器人", "600001"),
        ("BK2", "光伏", "600001"),    # 同一只票挂两个板块，取最热的那个
        ("BK1", "机器人", "600002"),
        ("BK3", "冷门", "600003"),
    ], columns=["board_code", "board_name", "stock_code"])


# --------------------------------------------------------------------------- 板块热度

def test_hot_boards_按热度排序并丢掉成员太少的板块():
    hot = pref.hot_boards(_board(), min_stocks=10, top=30)
    assert list(hot["board_code"]) == ["BK1", "BK2", "BK3"], "小样本噪声板块不该进来"
    assert hot["heat"].iloc[0] > hot["heat"].iloc[-1]
    assert hot["heat"].between(0.0, 1.0).all()
    assert list(hot["heat_rank"]) == [1, 2, 3]


def test_hot_boards_的top参数真的截断():
    assert len(pref.hot_boards(_board(), min_stocks=10, top=2)) == 2


def test_best_board_per_stock_每只票只留最热的那个板块():
    hot = pref.hot_boards(_board(), min_stocks=10, top=30)
    best = pref.best_board_per_stock(_member(), hot)
    assert best["600001"]["board_name"] == "机器人"
    assert best["600001"]["heat"] == pytest.approx(hot["heat"].iloc[0])
    assert set(best) == {"600001", "600002", "600003"}


# --------------------------------------------------------------------------- 新闻利好

def test_news_positive_只取正分且板块和个股分开():
    rows = [
        {"category": "板块", "code": "BK0475", "name": "银行", "score": 3},
        {"category": "板块", "code": "", "name": "BK0481", "score": 5},
        {"category": "个股", "code": "600999", "name": "某公司", "score": 4},
        {"category": "板块", "code": "BK0500", "name": "煤炭", "score": -6},   # 利空混进来要忽略
        {"category": "板块", "code": "BK0600", "name": "空分", "score": 0},
        {"category": "个股", "code": "", "name": "通用汽车", "score": 7},       # 美股名字，没有 A 股代码
    ]
    out = pref.news_positive(rows)
    assert out["boards"] == {"BK0475": 3, "BK0481": 5}, "code 空但 name 写的是 BK 码也算板块"
    assert out["stocks"] == {"600999": 4}
    assert out["names"] == {"通用汽车": 7}, "没代码的个股名要能按名字匹"
    assert "BK0500" not in out["boards"] and "BK0600" not in out["boards"]


def test_news_positive_板块名没带码时单独一格():
    rows = [{"category": "板块", "code": "", "name": "汽车零部件", "score": 4}]
    out = pref.news_positive(rows)
    assert out["boards"] == {}
    assert out["board_names"] == {"汽车零部件": 4}, "中文名不能混进代码表，也不能当没看见"


def test_news_positive_同日同板块多条累加():
    rows = [{"category": "板块", "code": "BK1", "name": "机器人", "score": 3},
            {"category": "板块", "code": "BK1", "name": "机器人", "score": 2}]
    assert pref.news_positive(rows)["boards"]["BK1"] == 5


# --------------------------------------------------------------------------- 趋势向上

def _kline(closes, ma20, date0="2026-10-01"):
    df = pd.DataFrame({"date": pd.date_range(date0, periods=len(closes)), "close": closes,
                       "ma20": ma20})
    df["ma5"] = df["close"].rolling(5, min_periods=1).mean()
    df["ma10"] = df["close"].rolling(10, min_periods=1).mean()
    return df


def test_trend_state_站上ma20且ma20上行才算向上():
    up = _kline([10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5],
                [9.0, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7])
    st = pref.trend_state(up)
    assert st["up"] is True
    # 超出 TREND_STRENGTH_SPAN 的涨幅封顶在 1.0：这一路只认「站上均线」，不奖励追高。
    assert st["strength"] == pytest.approx(1.0)

    down = _kline([12, 11.5, 11, 10.5, 10, 9.5, 9, 8.5],
                  [10.0, 10.05, 10.1, 10.15, 10.2, 10.25, 10.3, 10.35])
    assert pref.trend_state(down)["up"] is False


def test_trend_state_ma20走平不算向上():
    flat = _kline([10, 10.2, 10.1, 10.3, 10.2, 10.4, 10.3, 10.5], [10.4] * 8)
    assert pref.trend_state(flat)["up"] is False, "均线不抬头就不是「趋势向上」"


def test_trend_state_均线没算出来时判不了返回none():
    st = pref.trend_state(_kline([10, 11, 12, 13, 14, 15, 16, 17], [np.nan] * 8))
    assert st is None, "判不了必须是 None，不能当成「不向上」也不能当成「向上」"


def test_trend_state_只看当日及以前():
    """给定 day 时只能看到那天及以前的 K 线——这条和护栏那条自求是同一个规矩。"""
    full = _kline([10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 20.0, 21.0],
                  [9.0, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9])
    day = str(full["date"].iloc[7].date())
    assert pref.trend_state(full, day=day) == pref.trend_state(full.iloc[:8])


def test_trend_state_站上ma20但不强时分数小():
    st = pref.trend_state(_kline([10.00, 10.01, 10.02, 10.03, 10.04, 10.05],
                                 [9.95, 9.96, 9.97, 9.98, 9.99, 10.00]))
    assert st["up"] is True
    assert st["strength"] == pytest.approx(0.05)


# --------------------------------------------------------------------------- 合成分

def test_compose_score_三路单调():
    base = {"heat": 0.5, "news": 0.0, "trend": 0.5}
    s0 = pref.compose_score(**base)
    assert pref.compose_score(**{**base, "heat": 0.9}) > s0
    assert pref.compose_score(**{**base, "news": 0.8}) > s0
    assert pref.compose_score(**{**base, "trend": 0.9}) > s0
    assert pref.compose_score(heat=0.0, news=0.0, trend=0.0) == pytest.approx(0.0)
    assert pref.compose_score(heat=1.0, news=1.0, trend=1.0) == pytest.approx(1.0)


def test_compose_score_权重是用户点名的顺序():
    """他要的是「热门板块 + 新闻利好」优先，趋势是闸；三路权重之和必须是 1。"""
    assert pref.WEIGHTS["heat"] >= pref.WEIGHTS["news"]
    assert sum(pref.WEIGHTS.values()) == pytest.approx(1.0)


# --------------------------------------------------------------------------- 名单组装

def _rows():
    """模型榜的当日全池行（pref 只在已进榜的票里挑，不重新打分）。"""
    return [{"code": c, "qlib_symbol": f"SH{c}", "date": "2026-10-22", "score_raw": 1.0 - i * .1,
             "score": 0.9 - i * .1, "rank": i + 1, "universe": 5}
            for i, c in enumerate(["600001", "600002", "600003", "600004", "600005"])]


def _res(**over) -> pref.PrefResult:
    kw = dict(
        rows=_rows(),
        best={"600001": {"board_code": "BK1", "board_name": "机器人", "heat": 1.0},
              "600002": {"board_code": "BK1", "board_name": "机器人", "heat": 1.0},
              "600003": {"board_code": "BK2", "board_name": "光伏", "heat": 0.5},
              "600004": {"board_code": "BK2", "board_name": "光伏", "heat": 0.5}},
        # 600005 不在任何热门板块里
        boards={"BK1": 5, "BK2": 0},
        names={},
        mv={"600001": 300.0, "600002": 20.0, "600003": 80.0, "600004": 60.0, "600005": 900.0},
        trends={"600001": {"up": True, "strength": 0.6, "alignment": True},
                "600002": {"up": True, "strength": 0.6, "alignment": True},
                "600003": {"up": True, "strength": 0.2, "alignment": False},
                "600004": {"up": False, "strength": 0.0, "alignment": False},
                "600005": {"up": True, "strength": 0.9, "alignment": True}},
        min_float_mv=50.0,
        top=30,
    )
    kw.update(over)
    return pref.build_pref_list(**kw)


def _build(**over) -> list[dict]:
    return _res(**over).rows


# 上面 fixture 的默认口径：五只票里 600002 被市值闸掉、600004 被趋势闸掉、
# 600005 不在热门板块，所以名单应当是 600001（热度 1.0 + 板块利好 5）和 600003。

def test_名单里小市值再热也要被闸掉():
    assert "600002" not in {r["code"] for r in _build()}, "20 亿流通市值 < 50 亿闸门，必须出局"


def test_名单里趋势向下的再热也要被闸掉():
    assert "600004" not in {r["code"] for r in _build()}


def test_不在热门板块里的票不进名单():
    assert "600005" not in {r["code"] for r in _build()}


def test_名单按合成分降序并且rank从1开始():
    out = _build()
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True)
    assert [r["rank"] for r in out] == list(range(1, len(out) + 1))
    assert all(0.0 <= s <= 1.0 for s in scores), "分数必须是 0~1，百分制是展示层的事"


def test_每条都带着四项拆解的理由():
    r = _build()[0]
    assert r["code"] == "600001"
    for part in ("机器人", "利好", "市值", "趋势"):
        assert part in r["reason"], f"理由里必须看得见 {part} 这一路"
    assert r["extra"]["board_name"] == "机器人"
    assert r["extra"]["float_mv"] == pytest.approx(300.0)
    assert r["extra"]["model_rank"] == 1


def test_top参数截断():
    assert len(_build(top=1)) == 1


def test_判不了的票不进名单但不报错():
    """趋势算不出来（新股/停牌/均线 NaN）就不排进来；整张名单可以为空，但不能抛。"""
    out = _build(trends={"600001": None, "600003": None,
                         "600004": {"up": False, "strength": 0.0, "alignment": False}})
    assert [r["code"] for r in out] == []


def test_闸掉的数量要能被统计出来():
    """静默筛数是缺陷：每一路各挡了多少只必须能查得到。"""
    res = _res()
    assert res.stats["in"] == 5
    assert res.stats["no_hot_board"] == 1     # 600005
    assert res.stats["small_cap"] == 1        # 600002
    assert res.stats["trend_down"] == 1       # 600004
    assert res.stats["out"] == len(res.rows) == 2


def test_市值缺数的票单独计数不当成小市值():
    res = _res(mv={"600001": 300.0, "600002": 20.0, "600003": 80.0,
                   "600004": 60.0, "600005": 900.0, "600006": None})
    assert res.stats["mv_missing"] == 0
    res2 = _res(mv={"600001": None, "600002": 20.0, "600003": 80.0, "600004": 60.0})
    assert res2.stats["mv_missing"] == 1
    assert "600001" not in {r["code"] for r in res2.rows}


def test_装载器挑最近一天时不早于给定日():
    days = ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert pref.latest_day_not_after(days, "2026-10-22") == "2026-10-22"
    assert pref.latest_day_not_after(days, "2026-10-23") == "2026-10-22"
    assert pref.latest_day_not_after(days, "2026-10-19") is None


def test_新闻滞后天数要如实报出():
    assert pref.days_between("2026-10-21", "2026-10-23") == 2
    assert pref.days_between("2026-10-23", "2026-10-23") == 0


# --------------------------------------------------------------------------- 装载（读文件）

def test_装载器认得带日期的parquet文件名(tmp_path, monkeypatch):
    """文件名口径：只有 `YYYY-MM-DD.parquet` 才算一天的数据。

    这条是踩过才知道要写的：按「名字够长」筛会把 2026-10-22.parquet（18 个字符）判成
    「不是日期文件」，于是整条偏好榜的市值与新闻静默变成空——名单出 0 只，看起来像「今天没有票」。
    """
    for sub in ("market_snapshot", "news_analysis"):
        d = tmp_path / sub
        d.mkdir()
        for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
            (d / f"{day}.parquet").write_bytes(b"")
        (d / "notes.txt").write_bytes(b"")
    monkeypatch.setattr(pref, "DATA", tmp_path)
    assert pref._files(tmp_path / "market_snapshot") == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert pref.latest_day_not_after(pref._files(tmp_path / "news_analysis"), "2026-10-23") == "2026-10-22"


def test_市值快照读出来的单位是亿元(tmp_path, monkeypatch):
    """快照里 float_mv 是「亿元」，这里不换算，但必须按 asset=stock 取，且缺数给 None。"""
    d = tmp_path / "market_snapshot"
    d.mkdir()
    pd.DataFrame([
        ("2026-10-22", "600000", "浦发银行", "stock", 1200.5),
        ("2026-10-22", "510300", "沪深300ETF", "fund", 9999.0),
        ("2026-10-22", "000001", "平安银行", "stock", None),
    ], columns=["date", "code", "name", "asset", "float_mv"]).to_parquet(d / "2026-10-22.parquet")
    monkeypatch.setattr(pref, "DATA", tmp_path)
    mv, display, day = pref.load_snapshot("2026-10-22")
    assert day == "2026-10-22"
    assert mv["600000"] == pytest.approx(1200.5)
    assert "510300" not in mv, "场内基金不是股票，不该进市值表"
    assert mv["000001"] is None
    assert display["600000"] == "浦发银行"


def test_板块热度只取一个窗口否则同一板块被数三遍(tmp_path, monkeypatch):
    """口径坑：`board.parquet` 同一个板块存 3/7/30 日三行，不筛窗口等于把板块数三遍。

    这里让 BK0001「近 30 日很热、近 3 日很冷」，BK0002 反过来。真实意图是看当日热度，
    所以必须按 3 日窗排名——不筛的话 BK0001 会靠 30 日那行挤进热门，票的理由写错板块涨幅。
    """
    d = tmp_path / "sector_heat"
    d.mkdir()
    pd.DataFrame([
        ("BK0001", "机器学习", 15, 1.0, 0.9, 1, 3, "2026-10-22"),
        ("BK0001", "机器学习", 15, 20.0, 3.0, 9, 30, "2026-10-22"),
        ("BK0002", "工业机器人", 20, 15.0, 2.5, 8, 3, "2026-10-22"),
        ("BK0002", "工业机器人", 20, 2.0, 0.8, 0, 30, "2026-10-22"),
    ], columns=["board_code", "board_name", "stock_count", "ret_pct",
                "turnover_ratio", "limit_up_count", "window", "asof"]).to_parquet(d / "board.parquet")
    pd.DataFrame([
        ("BK0001", "机器学习", "600001", 3), ("BK0001", "机器学习", "600001", 30),
        ("BK0002", "工业机器人", "600002", 3), ("BK0002", "工业机器人", "600002", 30),
    ], columns=["board_code", "board_name", "stock_code", "window"]).to_parquet(d / "member.parquet")
    monkeypatch.setattr(pref, "DATA", tmp_path)

    hot, best, asof = pref.load_boards("2026-10-22")

    assert asof == "2026-10-22"
    assert len(hot) == hot["board_code"].nunique() == 2, "每个板块只能有一行热度"
    assert list(hot.sort_values("heat_rank")["board_code"]) == ["BK0002", "BK0001"]
    assert best["600002"]["board_ret_pct"] == pytest.approx(15.0), "取的是 3 日窗的涨幅"


def test_新闻装载吃得下numpy数组和空值(tmp_path, monkeypatch):
    """`bullish` 列存的是 ndarray（不是 list），`x or []` 会直接抛 ValueError。

    真实数据里还有 NaN（那条新闻没解析出利好），逐条读时要跳过而不是崩。
    """
    import numpy as np
    d = tmp_path / "news_analysis"
    d.mkdir()
    pd.DataFrame({
        "bullish": [
            np.array([{"category": "板块", "name": "BK0475", "code": "BK0475", "score": 3},
                      {"category": "板块", "name": "半导体", "code": "", "score": 2}], dtype=object),
            np.array([], dtype=object),
            None,
            np.nan,
            np.array([{"category": "个股", "name": "浦发银行", "code": "600000", "score": 2}], dtype=object),
        ]
    }).to_parquet(d / "2026-10-22.parquet")
    monkeypatch.setattr(pref, "DATA", tmp_path)

    news, day = pref.load_news("2026-10-22")

    assert day == "2026-10-22"
    assert news["boards"] == {"BK0475": 3}
    assert news["board_names"] == {"半导体": 2}, "没给 BK 码的板块名进中文名那格"
    assert news["stocks"] == {"600000": 2}
