"""test_pricing.py"""
import pandas as pd
import pytest

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
    monkeypatch.setattr("requests.get", _raise)
    with pytest.raises(PricingError):
        latest_price("512880", "2026-10-03")
    assert calls == ["512880"]


def _raise(*a, **k):
    raise OSError("断网")


class _FakeResp:
    def __init__(self, payload: str):
        self.content = payload.encode("gbk")


def _tencent_price(price):
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen["url"] = url
        return _FakeResp(f'v_x="51~某ETF~512880~{price}~2.6~')

    fake_get.seen = seen
    return fake_get


def test_realtime_quote_rescues_throttled_akshare(monkeypatch):
    """东财掐连接（本机实测会连续几十分钟 RemoteDisconnected）时，实时价必须把利润分析接住。"""
    import sys

    monkeypatch.setitem(sys.modules, "akshare", _FakeAkModule(error=OSError("限流")))
    monkeypatch.setattr("portfolio.pricing.AKShareSource",
                        lambda *a, **k: type("S", (), {"get_kline": lambda self, c: None})())
    fake = _tencent_price(2.556)
    monkeypatch.setattr("requests.get", fake)
    assert latest_price("159599", "2026-10-03") == 2.556
    assert fake.seen["url"].endswith("q=sz159599")     # 15 字头在深交所


def test_sh_fund_code_maps_to_sh_market(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "akshare", _FakeAkModule(error=OSError("限流")))
    monkeypatch.setattr("portfolio.pricing.AKShareSource",
                        lambda *a, **k: type("S", (), {"get_kline": lambda self, c: None})())
    fake = _tencent_price(1.02)
    monkeypatch.setattr("requests.get", fake)
    assert latest_price("512880", "2026-10-03") == 1.02
    assert fake.seen["url"].endswith("q=sh512880")     # 5 字头是沪市，不能按 _market_prefix 的 bj 兜底走
