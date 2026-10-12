"""tests/test_quote_tencent.py — 腾讯快照解析：单位、哨兵、缺字段、批量与重试。

全部用 2026-10-12 抓的真实报文做夹具，测试不打网络。
"""

import pytest

from stock_selector import quote
from stock_selector.quote import tencent

# 正常交易中的上海主板票（含五档、涨停/跌停价、换手、量比）
SH600000 = ('v_sh600000="1~浦发银行~600000~9.67~9.54~9.59~392018~208137~183881'
            '~9.67~5~9.66~2140~9.65~1219~9.64~1521~9.63~997'
            '~9.68~538~9.69~6346~9.70~15609~9.71~6859~9.72~7883'
            '~~20261012125824~0.13~1.36~9.70~9.55~9.67/392018/378644591'
            '~392018~37864~0.12~6.29~~9.70~9.55~1.57~3220.67~3220.67~0.43'
            '~10.49~8.59~0.70~-31353~9.66~5.20~6.44~~~0.00~37864.4591~0.0000~0'
            '~   A~GP-A~-19.55~5.57~4.34~6.14~0.50~13.11~8.07~6.62~2.55~11.92'
            '~33305838300~33305838300~-72.72~-13.04~33305838300~~~-15.32~0.00'
            '~CNY~0~___D__F__N~9.75~-12003~";')

# 退市整理期：涨停/跌停字段是 -1，五档全 0，成交额 0
SZ000004 = ('v_sz000004="51~国华退~000004~0.51~0.51~0.00~0~0~0'
            '~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0'
            '~~20261012090000~0.00~0.00~0.00~0.00~0.51/0/0~0~0~0.00~2.48~D'
            '~0.00~0.00~0.00~0.64~0.68~0.78~-1~-1~0.00~0~0.00~-1.60~10.41'
            '~~~~0.0000~0.0000~0~ ~GP-A~-95.40~0.00~0.00~35.82~12.00~13.98~0.25'
            '~0.00~0.00~10.87~126289068~132380282~~-95.10~126289068~~~-95.84~0.00'
            '~CNY~0~~0.00~0~";')


def test_parse_maps_price_and_limits():
    q = tencent.parse(SH600000)
    assert q["code"] == "600000" and q["name"] == "浦发银行"
    assert q["price"] == 9.67 and q["prev_close"] == 9.54
    # 交易所口径的涨跌停价，风控判「买不进/卖不出」全靠它
    assert q["limit_up"] == 10.49 and q["limit_down"] == 8.59
    assert q["pct"] == 1.36 and q["turnover_pct"] == 0.12 and q["vol_ratio"] == 0.70
    assert q["quote_time"] == "20261012125824"


def test_units_are_lots_and_yuan():
    """volume=手、amount=元 —— 和 data/stock_kline 的日线口径必须一致，否则风控算错量级。"""
    q = tencent.parse(SH600000)
    assert q["volume"] == 392018
    assert q["amount"] == pytest.approx(378644591.0)


def test_order_book_levels_and_inner_outer():
    q = tencent.parse(SH600000)
    assert q["bids"][0] == (9.67, 5)
    assert q["asks"][0] == (9.68, 538)
    assert len(q["bids"]) == 5 and len(q["asks"]) == 5
    # 外盘=主动买、内盘=主动卖：「打单流入」的可用近似
    assert q["outer_volume"] == 208137 and q["inner_volume"] == 183881


def test_delisted_sentinel_minus_one_becomes_none():
    """-1 是「无涨跌停数据」，当成价格会让所有买入信号被误判成可成交。"""
    q = tencent.parse(SZ000004)
    assert q["limit_up"] is None and q["limit_down"] is None
    assert q["bids"] == [] and q["asks"] == []
    assert q["amount"] == 0.0            # 停牌/退市零成交是真实值，不能变 None


def test_zero_price_record_is_dropped():
    assert tencent.parse('v_sh600000="1~~600000~0.00~9.5~"') is None


def test_short_record_returns_none_not_partial():
    """字段不够长（接口异常/被截断）时整条丢弃，不给半成品快照。"""
    assert tencent.parse('v_sh600000="1~浦发银行~600000~9.67~9.54~"') is None


def test_amount_falls_back_to_wan_field_when_triples_broken(monkeypatch):
    """字段 35 坏掉时退到字段 37（万元），单位换算不能漏。"""
    fields = SH600000.split("~")
    assert fields[tencent.F_AMOUNT_WAN].strip() == "37864"
    monkeypatch.setattr(tencent, "F_TRIPLES", -99)   # 让三段式取不到
    q = tencent.parse(SH600000)
    assert q["amount"] == pytest.approx(37864 * 1e4)


@pytest.mark.parametrize("code,want", [
    ("600000", "sh600000"), ("688981", "sh688981"), ("510300", "sh510300"),
    ("000001", "sz000001"), ("300750", "sz300750"), ("159915", "sz159915"),
    ("920002", "bj920002"), ("830799", "bj830799"), ("430047", "bj430047"),
    ("sh600000", "sh600000"),
])
def test_symbol_prefix(code, want):
    """920 是北交所新代码段，必须排在「9 开头归沪」前面判。"""
    assert tencent.symbol(code) == want


def test_fetch_batches_and_maps_by_code(monkeypatch):
    calls = []

    def fake_request(syms, timeout):
        calls.append(list(syms))
        return "\n".join(SH600000 if s.endswith("600000") else SZ000004 for s in syms)

    monkeypatch.setattr(tencent, "_request", fake_request)
    monkeypatch.setattr(tencent, "BATCH", 2)
    out = tencent.fetch(["600000", "000004", "600000"])
    assert set(out) == {"600000", "000004"}
    assert [len(c) for c in calls] == [2, 1]          # 60 只一片改成 2 后确实分片


def test_fetch_keeps_going_when_one_batch_fails(monkeypatch):
    """单片失败只影响该片：拿不到的代码不出现在结果里，调用方才能判「行情缺失」。"""
    def fake_request(syms, timeout):
        if syms[0].endswith("600000"):
            raise OSError("connection reset")
        return SZ000004

    monkeypatch.setattr(tencent, "_request", fake_request)
    monkeypatch.setattr(tencent, "BATCH", 1)
    out = tencent.fetch(["600000", "000004"], retries=1)
    assert set(out) == {"000004"}


def test_fetch_returns_empty_after_retries_exhausted(monkeypatch):
    def fake_request(syms, timeout):
        raise OSError("down")

    monkeypatch.setattr(tencent, "_request", fake_request)
    assert tencent.fetch(["600000"], timeout=1, retries=2) == {}


def test_module_reexported_from_package():
    assert quote.fetch is tencent.fetch
    assert quote.BATCH == tencent.BATCH
