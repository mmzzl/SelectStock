import importlib.util

SCRIPT = "scripts/kline_spider.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("kline_spider", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stocks_still_in_universe():
    mod = _load_script()
    assert mod.in_universe("sh", "600519")
    assert mod.in_universe("sz", "000001")
    assert mod.in_universe("sz", "300750")
    assert mod.in_universe("bj", "832000")


def test_funds_now_included():
    mod = _load_script()
    for code in ("510300", "512880", "588000", "513310", "520990"):
        assert mod.in_universe("sh", code), code
    for code in ("159599", "161725", "184801"):
        assert mod.in_universe("sz", code), code


def test_indices_and_bonds_still_excluded():
    mod = _load_script()
    assert not mod.in_universe("sh", "000001")   # 上证综合指数
    assert not mod.in_universe("sh", "113050")   # 沪市可转债
    assert not mod.in_universe("sz", "123456")   # 深市可转债
    assert not mod.in_universe("sh", "013959")   # 回购
    assert not mod.in_universe("sh", "")
    assert not mod.in_universe("xx", "600519")   # 未知交易所不给放行


def test_sz_index_leak_is_preexisting():
    """sz.399xxx 指数首位 3 与创业板号段同前缀，老过滤器就放进来了；本次不动它，避免误伤已依赖 399 数据的入口。"""
    mod = _load_script()
    assert mod.in_universe("sz", "399001")


def test_sina_volume_is_converted_to_lots(monkeypatch):
    """sina 的 volume 是「股/份」，入库前必须折算成手，否则主备源交错会让量能指标凭空差 100 倍。"""
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"day": "2026-09-30", "open": "10", "high": "11", "low": "9",
                     "close": "10.5", "volume": "2501689"}]

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    rec = mod._sina_kline("600519")[0]
    assert rec["volume"] == 25017
    assert rec["vol_unit"] == "lot"

    # 主源本来就给手，只能原样入库并打上单位标记，供补数脚本识别「已折算」
    bar = mod._parse_api_response("600519", [["2026-09-30", "10", "10.5", "11", "9", "28218"]])[0]
    assert bar["volume"] == 28218
    assert bar["vol_unit"] == "lot"
