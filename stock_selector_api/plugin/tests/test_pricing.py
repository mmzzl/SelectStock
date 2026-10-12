"""test_pricing.py"""
import pandas as pd
import pytest
from time import monotonic

from portfolio import pricing
from portfolio.pricing import PricingError, latest_price_from_df, latest_price


def _df():
    return pd.DataFrame({
        "date": ["2026-09-15", "2026-09-16", "2026-09-17"],
        "close": [10.0, 10.5, 11.0],
        "open": [9.9, 10.1, 10.4],
    })


def test_latest_price_from_df_uses_row_le_day():
    assert latest_price_from_df(_df(), "2026-09-16") == 10.5


def test_latest_price_from_df_future_day_uses_last():
    assert latest_price_from_df(_df(), "2026-09-20") == 11.0


def test_latest_price_from_df_empty_raises():
    with pytest.raises(PricingError):
        latest_price_from_df(pd.DataFrame(columns=["date", "close"]), "2026-09-16")


def test_latest_price_falls_back_to_ak(monkeypatch):
    calls = []

    class FakeAKSource:
        def get_kline(self, code):
            calls.append(code)
            return pd.DataFrame({"date": ["2026-09-16"], "close": [42.5]})

    monkeypatch.setattr("portfolio.pricing.AKShareSource", FakeAKSource)
    assert latest_price("600000", "2026-09-17") == 42.5
    assert calls == ["600000"]


class _FakeAkModule:
    """假 akshare：记录 fund_etf_hist_em 的入参，返回带中文列名的历史表。"""

    def __init__(self, frame=None, error=None):
        self.kwargs = None
        self._frame = frame
        self._error = error

    def fund_etf_hist_em(self, **kw):
        self.kwargs = kw
        if self._error:
            raise self._error
        return self._frame

    def stock_zh_a_hist(self, **kw):        # 股票分支不该被 ETF 代码走到
        raise AssertionError("ETF 不该走 stock_zh_a_hist")


def _fund_frame():
    return pd.DataFrame({"日期": ["2026-09-29", "2026-09-30"], "收盘": [2.60, 2.556]})


def test_etf_code_uses_fund_interface_with_bounded_window(monkeypatch):
    import sys

    fake = _FakeAkModule(frame=_fund_frame())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr("portfolio.pricing.AKShareSource",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走股票源")))
    assert latest_price("159599", "2026-10-03") == 2.556
    assert fake.kwargs["symbol"] == "159599"
    assert len(fake.kwargs["start_date"]) == 8        # 不限窗口会被东财掐连接，必须带 start_date


def test_etf_interface_error_falls_back_to_stock_source(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "akshare", _FakeAkModule(error=OSError("限流")))
    calls = []

    class FakeAKSource:
        def get_kline(self, code):
            calls.append(code)
            return None

    monkeypatch.setattr("portfolio.pricing.AKShareSource", FakeAKSource)
    monkeypatch.setattr("stock_selector.quote.tencent._request", _raise)
    with pytest.raises(PricingError):
        latest_price("512880", "2026-10-03")
    assert calls == ["512880"]


def _raise(syms, timeout):
    raise OSError("断网")


def _record(sym: str, code: str, price: float) -> str:
    """拼一条 88 段的腾讯快照报文（字段下标见 stock_selector/quote/tencent.py）。"""
    f = ["1", "某ETF", code, str(price), "2.6", "2.6"] + ["0"] * 82
    f[35] = f"{price}/1000/2600000"
    f[37] = "260"
    return f'v_{sym}="' + "~".join(f) + '";'


def _tencent_price(price, code="512880"):
    seen = {}

    def fake_request(syms, timeout):
        seen["syms"] = list(syms)
        return _record(syms[0], code, price)

    fake_request.seen = seen
    return fake_request


def test_realtime_quote_rescues_throttled_akshare(monkeypatch):
    """东财掐连接（本机实测会连续几十分钟 RemoteDisconnected）时，实时价必须把利润分析接住。"""
    import sys

    monkeypatch.setitem(sys.modules, "akshare", _FakeAkModule(error=OSError("限流")))
    monkeypatch.setattr("portfolio.pricing.AKShareSource",
                        lambda *a, **k: type("S", (), {"get_kline": lambda self, c: None})())
    fake = _tencent_price(2.556, "159599")
    monkeypatch.setattr("stock_selector.quote.tencent._request", fake)
    assert latest_price("159599", "2026-10-03") == 2.556
    assert fake.seen["syms"] == ["sz159599"]     # 15 字头在深交所


def test_sh_fund_code_maps_to_sh_market(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "akshare", _FakeAkModule(error=OSError("限流")))
    monkeypatch.setattr("portfolio.pricing.AKShareSource",
                        lambda *a, **k: type("S", (), {"get_kline": lambda self, c: None})())
    fake = _tencent_price(1.02)
    monkeypatch.setattr("stock_selector.quote.tencent._request", fake)
    assert latest_price("512880", "2026-10-03") == 1.02
    assert fake.seen["syms"] == ["sh512880"]     # 5 字头是沪市，不能按 _market_prefix 的 bj 兜底走


# ---------------------------------------------------------------- 实时优先取价（持仓价）
class _FakeTencent:
    """按代码返回快照，记录每次批量请求：用来证明「一批一次请求」和「历史日期根本不问行情」。"""

    def __init__(self, quotes: dict, error: Exception | None = None):
        self.quotes = quotes
        self.error = error
        self.calls: list[list[str]] = []

    def fetch(self, codes, timeout=6.0, retries=2):
        self.calls.append([str(c) for c in codes])
        if self.error:
            raise self.error
        return {str(c): self.quotes[str(c)] for c in codes if str(c) in self.quotes}


def _write_kline(code: str, rows: list[tuple[str, float]]) -> None:
    import os
    from pathlib import Path

    d = Path(os.environ["PLUGIN_KLINE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": [r[0] for r in rows], "close": [r[1] for r in rows]}).to_parquet(
        d / f"{code}.parquet")


def test_today_takes_live_price_even_with_local_kline(monkeypatch):
    """本地 parquet 每天 18:00 才重建，收盘前读它就是昨天的价——今天必须先问行情。"""
    from datetime import date

    today = date.today().isoformat()
    monkeypatch.setattr(pricing, "tencent_quote",
                        _FakeTencent({"001266": {"price": 36.94, "quote_time": "20261012150000"}}))
    _write_kline("001266", [(today, 40.64)])
    out = pricing.resolve(["001266"], today)
    assert out["001266"] == {"price": 36.94, "price_time": "20261012150000", "price_source": "live"}


def test_historical_day_never_asks_the_market(monkeypatch):
    """补跑上周三的持仓必须用上周三那根收盘，否则同一份数据两次诊断出两个盈亏。"""
    fake = _FakeTencent({"001266": {"price": 99.0, "quote_time": "20261012150000"}})
    monkeypatch.setattr(pricing, "tencent_quote", fake)
    _write_kline("001266", [("2026-10-09", 38.0), ("2026-10-10", 39.5)])
    out = pricing.resolve(["001266"], "2026-10-09")
    assert out["001266"]["price"] == 38.0
    assert out["001266"]["price_source"] == "kline"
    assert out["001266"]["price_time"] == "2026-10-09"     # 旧价要旧得看得见
    assert fake.calls == []


def test_live_outage_degrades_to_that_bar_with_its_date(monkeypatch):
    """问不到行情不是失败而是降级：退回日线那一根，并把日期标出来，页面才显示得出「这是收盘」。"""
    from datetime import date

    today = date.today().isoformat()
    monkeypatch.setattr(pricing, "tencent_quote", _FakeTencent({}, error=OSError("断网")))
    _write_kline("001266", [("2026-10-09", 38.0), ("2026-10-10", 39.5)])
    out = pricing.resolve(["001266"], today)
    assert out["001266"] == {"price": 39.5, "price_time": "2026-10-10", "price_source": "kline"}


def test_live_quotes_batch_once_and_reuse_within_ttl(monkeypatch):
    """持仓列表/利润分析/诊断三条路径同一次加载里问的是同一批代码：只发一个 HTTP，不是三个。"""
    from datetime import date

    today = date.today().isoformat()
    fake = _FakeTencent({"600000": {"price": 9.67, "quote_time": "20261012150000"},
                         "001266": {"price": 36.94, "quote_time": "20261012150000"},
                         "300750": {"price": 369.99, "quote_time": "20261012150000"}})
    monkeypatch.setattr(pricing, "tencent_quote", fake)
    got = pricing.live_quotes(["600000", "001266", "300750"])
    assert got["001266"] == (36.94, "20261012150000")
    assert fake.calls == [["600000", "001266", "300750"]]
    assert pricing.live_quotes(["600000"]) == {"600000": (9.67, "20261012150000")}
    assert fake.calls == [["600000", "001266", "300750"]]      # 缓存命中，不再问
    pricing._live_cache["600000"] = (9.0, "", monotonic() - pricing.LIVE_CACHE_TTL - 1)
    assert pricing.live_quotes(["600000"])["600000"] == (9.67, "20261012150000")
    assert fake.calls[-1] == ["600000"]                         # 过期后重问，旧价不留


def test_prewarm_skips_historical_day(monkeypatch):
    fake = _FakeTencent({"600000": {"price": 9.67, "quote_time": "20261012150000"}})
    monkeypatch.setattr(pricing, "tencent_quote", fake)
    pricing.prewarm(["600000"], "2026-10-09")
    assert fake.calls == []
    pricing.prewarm(["600000"], "2099-01-01")
    assert fake.calls == [["600000"]]


def test_wants_live_only_from_today_onwards():
    from datetime import date, timedelta

    today = date.today()
    assert pricing.wants_live(today.isoformat())
    assert pricing.wants_live((today + timedelta(days=1)).isoformat())
    assert not pricing.wants_live((today - timedelta(days=1)).isoformat())
    assert not pricing.wants_live("2026-09-17 15:00:00")
