import importlib.util

SCRIPT = "scripts/backfill_kline_amount.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_kline_amount", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(day="2026-06-01", close=10.0, volume=1000.0, amount=None):
    return {"date": f"{day} 15:00", "close": close, "volume": volume, "amount": amount}


def test_symbol_prefixes():
    mod = _load_script()
    assert mod.symbol("600000") == "sh600000"
    assert mod.symbol("510300") == "sh510300"     # 5 字头是沪市场内基金
    assert mod.symbol("000002") == "sz000002"
    assert mod.symbol("159915") == "sz159915"
    assert mod.symbol("920001") == "bj920001"


def test_row_count_covers_the_window():
    """接口按 count 从 end 往前给，算少了会漏掉窗口开头的交易日。"""
    mod = _load_script()
    # 2026-05-22~2026-10-11 = 142 天 ≈ 94 个交易日
    assert mod.row_count("20260522", "20261011") > 94


def test_build_ops_writes_only_plausible_amounts():
    mod = _load_script()
    ratios: list[float] = []
    ops, stat = mod.build_ops("600000", {
        "2026-06-01": 1002000.0,        # 正常：额/(量×价)=1.002
        "2026-06-02": 0.0,              # 停牌
        "2026-06-03": 100.2,            # 万元没换算成元：量纲差 1e4，拦下
    }, [
        _doc(),
        _doc(day="2026-06-02"),
        _doc(day="2026-06-03"),
        _doc(day="2026-06-04", amount=987654321.0),   # 已有精确值，不能被粗值盖掉
        _doc(day="2026-06-05"),                       # 库里这天有行、腾讯没给
    ], ratios)
    assert stat == {"ok": 1, "zero": 1, "bad": 1, "has": 1, "miss": 1, "shares": 0}
    assert len(ops) == 1
    op = ops[0]
    assert op._filter == {"code": "600000", "date": "2026-06-01 15:00", "frequency": 9}
    assert op._doc == {"$set": {"amount": 1002000.0}}
    assert op._upsert is False                   # 库里没这根 K 线就跳过，不造行
    assert ratios and 0.99 < ratios[0] < 1.01


def test_star_rows_amount_still_written():
    """科创板那批行的 volume 在库里存成了「股」，比值贴着 0.01；额是交易所原值，照写，只单独计数。"""
    mod = _load_script()
    ops, stat = mod.build_ops("688001", {"2026-06-01": 670514700.0},
                              [_doc(close=55.2, volume=12388324.0)], [])
    assert stat["shares"] == 1 and stat["ok"] == 1 and stat["bad"] == 0
    assert ops[0]._doc == {"$set": {"amount": 670514700.0}}

    # 689 是 CDR（九号公司），腾讯同样给「股」，别只认 688 字头
    ops, stat = mod.build_ops("689009", {"2026-06-01": 253265912.0},
                              [_doc(close=37.01, volume=6806587.0)], [])
    assert stat == {"ok": 1, "zero": 0, "bad": 0, "has": 0, "miss": 0, "shares": 1}

    # 同一个比值出现在非科创板代码上就是真异常（比如额没换算），不能放行
    ops, stat = mod.build_ops("600001", {"2026-06-01": 670514700.0},
                              [_doc(close=55.2, volume=12388324.0)], [])
    assert stat["bad"] == 1 and stat["ok"] == 0 and ops == []


def test_fetch_converts_wan_yuan(monkeypatch):
    """腾讯第 8 列是「万元」，入库要换成元；日期做键，后面按库里的行对齐。"""
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"sz000002": {"qfqday": [
                ["2026-10-09", "10", "11", "12", "9", "5897549.00", {}, "2.63", "235533.48", ""],
                ["2026-10-10", "10", "11", "12", "9", "1"],               # 列不全，跳过
            ]}}}

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    out = mod.fetch("000002", "20260522", "20261011", mod.RateLimiter(1000.0))
    assert out == {"2026-10-09": 2355334800.0}
