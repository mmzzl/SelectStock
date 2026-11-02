import importlib.util

import pytest

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


def test_tencent_star_volume_is_converted_to_lots():
    """腾讯给科创板（688/689）的成交量是「股」，给其他板块是「手」——不折算同一根 K 线换个源就差 100 倍。"""
    mod = _load_script()
    star = mod._parse_api_response("688001", [["2026-09-30", "10", "10.5", "11", "9", "12388324"]])[0]
    assert star["volume"] == 123883
    assert star["vol_unit"] == "lot"
    cdr = mod._parse_api_response("689009", [["2026-09-30", "10", "10.5", "11", "9", "6806587"]])[0]
    assert cdr["volume"] == 68066        # 689 是 CDR，腾讯同样给股
    assert mod._parse_api_response("300750", [["2026-09-30", "10", "10.5", "11", "9", "334591"]])[0]["volume"] == 334591


def test_both_sources_leave_amount_unknown(monkeypatch):
    """腾讯/新浪的日线接口都不给成交额，必须留 null。

    写成 0 会被下游当成「真·零成交」：`risk/context.py` 的 `amount.where(amount > 0, 估算)`
    就此失效，流动性判据会跟着错。精确值由 merge_snapshot_amount / backfill_kline_amount 回写。
    """
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"day": "2026-09-30", "open": "10", "high": "11", "low": "9",
                     "close": "10.5", "volume": "2501689"}]

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._sina_kline("600519")[0]["amount"] is None

    bar = mod._parse_api_response("600519", [["2026-09-30", "10", "10.5", "11", "9", "28218"]])[0]
    assert bar["amount"] is None


def test_tencent_source_asks_for_bare_prices_and_labels_raw(monkeypatch):
    """库里 1,227 万行日线标着 `adjust:"qfq"`，实测价却是裸价（§48.1）——爬虫得老实标。

    判据不是猜的：把 2025 年以来 2,405 个除权日（东财分红送配事件按交易所公式推的台阶）对上库里那天的价，
    残差中位 +0.17%~+0.25%，而当天派息/送转的隐含跌幅中位是 0.87%~1.08%；真前复价的话残差会等于那个隐含
    跌幅（假跌被复权吃掉、残差归零的是「复权后涨幅」而不是「裸价跌幅」）。所以库里存的就是裸价。
    主源只准请求 `day`，并且只认 `day`——静默回落 `qfqday` 会让同一天的两根 K 线来自两个基准。
    """
    mod = _load_script()
    sent = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"sh600519": {
                "qfqday": [["2026-09-30", "8.0", "8.4", "8.6", "7.9", "30000"]],
                "day": [["2026-09-30", "10.0", "10.5", "11.0", "9.0", "30000"]]}}}

    def _get(url, params=None, timeout=None, **kw):
        sent["param"] = params["param"]
        return _Resp()

    monkeypatch.setattr(mod.requests, "get", _get)
    rec = mod._tencent_kline("600519")[0]
    assert "qfq" not in sent["param"]              # 请求参数就要裸价
    assert rec["close"] == 10.5                     # 取 day，不是 qfqday
    assert rec["adjust"] == "raw"


def test_tencent_source_without_bare_series_falls_back_to_sina(monkeypatch):
    """只给 qfqday 的响应＝口径不对，宁可用备源，也不能悄悄写进复权价。"""
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"sh600519": {"qfqday": [["2026-09-30", "8.0", "8.4", "8.6", "7.9", "3"]]}}}

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._tencent_kline("600519") is None


def test_tencent_empty_payload_is_not_a_successful_fetch(monkeypatch):
    """2026-11-02 实测：腾讯回 HTTP 200，但 body 里只有 `{"version": ...}`、没有 `day` 数组。

    以前这种响应返回 `[]`，`_multi_source_kline` 只认 `None` 才回落备源 ⇒ 6922 只里
    6304 只静默记成「skipped（没有新柱子）」，当天全场只落库 595 只，qlib 榜只剩 2 只。
    「源没给数据」必须是失败，不能让空响应长得像「今天这根确实没有」。
    """
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"sh600000": {"version": "231202020"}}}

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._tencent_kline("600000") is None


def test_multi_source_uses_sina_when_tencent_returns_empty_payload(monkeypatch):
    """空 payload 要真的把备源叫上来：2026-11-02 22:12 实测 sina 有当天的柱子。"""
    mod = _load_script()
    asked: list[str] = []

    class _TencentResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"sh600000": {"version": "231202020"}}}

    class _SinaResp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"day": "2026-11-02", "open": "10", "high": "11", "low": "9",
                     "close": "10.5", "volume": "2501689"}]

    def _get(url, params=None, timeout=None, **kw):
        asked.append(url)
        return _SinaResp() if "sina" in url else _TencentResp()

    monkeypatch.setattr(mod.requests, "get", _get)
    recs = mod._multi_source_kline("600000")
    assert [u for u in asked if "sina" in u], f"没回落备源：{asked}"
    assert recs[0]["date"] == "2026-11-02 15:00"
    assert recs[0]["source"] == "sina"


def test_sina_source_labels_raw(monkeypatch):
    """sina 的日线接口从来不复权，标 qfq 是跟着主源一起错的。"""
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"day": "2026-09-30", "open": "10", "high": "11", "low": "9",
                     "close": "10.5", "volume": "2501689"}]

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._sina_kline("600519")[0]["adjust"] == "raw"


# ------------------------------------------------ 当天落库只数的自检（2026-11-02 那轮的洞）
def test_day_bar_problem_fires_on_the_2026_11_02_shape():
    """2026-11-02 实测：应抓 6,922 只，当天真落库 595 只（8.6%），qlib 榜只剩 2 只。

    这一轮跑完只留了一行 `success=596, skipped=6304`，没有任何告警；面板那层的覆盖率
    告警要等到 17:16 才响。采集这层自己得会喊。
    """
    mod = _load_script()
    p = mod.day_bar_problem("2026-11-02", fetched=6922, with_bar=595)
    assert p is not None
    assert (p["with_bar"], p["fetched"]) == (595, 6922)
    assert p["ratio"] == pytest.approx(0.086, abs=0.002)


def test_full_day_is_not_a_problem():
    mod = _load_script()
    assert mod.day_bar_problem("2026-10-29", fetched=6922, with_bar=6890) is None


def test_a_non_trading_day_stays_quiet():
    """周末/休市：那天全场都不该有柱子，`with_bar=0` 不能当成缺数天天刷屏。"""
    mod = _load_script()
    assert mod.day_bar_problem("2026-11-01", fetched=6922, with_bar=0) is None


def test_the_boundary_is_the_min_ratio():
    mod = _load_script()
    assert mod.day_bar_problem("2026-11-02", fetched=1000, with_bar=900) is None
    assert mod.day_bar_problem("2026-11-02", fetched=1000, with_bar=899) is not None


def test_codes_with_bar_counted_from_the_db_not_from_the_loop():
    """判据要读库，不能拿循环里的 `success` 凑：`success` 数的是「这次有新柱子的票」，
    而补跑/重跑时全场都是 0——库里有当天柱子就是有的，那才是榜单真正依赖的那一列。"""
    mod = _load_script()

    class _FakeCol:
        def aggregate(self, pipeline, **kw):
            assert pipeline[0]["$match"]["date"] == {"$gte": "2026-11-02",
                                                     "$lt": "2026-11-03"}
            return [{"_id": None, "n": 595}]

    assert mod.codes_with_bar(_FakeCol(), "2026-11-02") == 595


# ------------------------------------------------ 腾讯参数格式（2026-11-02 "bad params" 回归）
def test_tencent_params_need_the_trailing_field(monkeypatch):
    """腾讯 fqkline 现在必须在第 6 个字段留个空位：`sh600519,day,,,320` 回 `bad params`。

    2026-11-02 22:4x 实测（同一个 code，只差一个尾逗号）：
      `sh600519,day,,,320`  → HTTP 200, 66 字节, {"code":1,"msg":"bad params"}，只有 version
      `sh600519,day,,,320,` → HTTP 200, 24,541 字节, code=0，`day` 数组 320 根，末根是当天
    所以这不是「源今天没给数据」，是请求本身不合规：一个尾逗号 = 全市场 0 根。
    """
    mod = _load_script()
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"sh600519": {"day": [
                ["2026-11-02", "1410.000", "1439.100", "1447.000", "1395.020", "82691.000"]]}}}

    def _get(url, params=None, timeout=None, **kw):
        seen.update(params or {})
        return _Resp()

    monkeypatch.setattr(mod.requests, "get", _get)
    mod._tencent_kline("600519")
    assert seen["param"].endswith(","), f"param 少第 6 个字段，腾讯会回 bad params：{seen['param']}"
    assert seen["param"] == "sh600519,day,,,320,"


def test_tencent_bad_params_response_is_a_source_failure(monkeypatch):
    """`{"code":1,"msg":"bad params"}` 必须当成源失败（None），不能当成「今天没有新柱子」的 []。"""
    mod = _load_script()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 1, "msg": "bad params", "data": {"sh600519": {"version": "16"}}}

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._tencent_kline("600519") is None


# ------------------------------------------------ 腾讯 WAF 501（2026-11-02 22:4x 实测）
def test_waf_501_page_is_a_source_failure_not_a_parse_error(monkeypatch):
    """`ifzq.gtimg.cn` 整域被腾讯 WAF 挡了：HTTP 501 + `Server: stgw` + 一段跳
    `waf.tencent.com/501page.html` 的 HTML。

    2026-11-02 22:4x 实测：603/688/605/601 号段 72 只全 501，连 22:36 还成功过的
    600519 也 501。这是「这个域名不给你了」，不是「这只票没柱子」——必须返回 None
    让备源上场，而不是抛 JSON 解析异常、也不是静默记 skipped。
    """
    mod = _load_script()

    class _Resp:
        status_code = 501

        def raise_for_status(self):
            raise AssertionError("WAF 页不该走 raise_for_status 才算失败判据")

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        @property
        def text(self):
            return ('<!DOCTYPE html><html><head><script>var i=location.href;'
                    'window.location.href="https://waf.tencent.com/501page.html?u="+location.origin;'
                    '</script></head></html>')

    monkeypatch.setattr(mod.requests, "get", lambda url, params=None, timeout=None, **kw: _Resp())
    assert mod._tencent_kline("603000") is None


def test_tencent_falls_to_proxy_endpoint_when_primary_is_walled(monkeypatch):
    """主域被 WAF 挡时，要真的打到 proxy.finance.qq.com 那个镜像拿回数据。

    backfill_kline_amount.py 里已核实：proxy 域名的 `newfqkline/get` 给 10 列，
    下标 0-5 与主源同构（日期/开/收/高/低/量），下标 8 是成交额（万元）。
    """
    mod = _load_script()
    hit = []

    class _Walled:
        status_code = 501
        text = "<html>waf.tencent.com/501page.html</html>"

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    class _Proxy:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"sh603000": {"day": [
                ["2026-11-02", "15.01", "16.21", "16.29", "14.92", "297602.00",
                 {}, "2.69", "46810.99", "0.00", "0.00"]]}}}

    def _get(url, params=None, timeout=None, **kw):
        hit.append(url)
        return _Proxy() if "proxy.finance.qq.com" in url else _Walled()

    monkeypatch.setattr(mod.requests, "get", _get)
    recs = mod._tencent_kline("603000")
    assert recs, f"主域被挡时没去 proxy 镜像：{hit}"
    assert any("proxy.finance.qq.com" in u for u in hit)
    assert recs[0]["date"] == "2026-11-02 15:00"
    assert recs[0]["close"] == 16.21


# ------------------------------------------------ 第 6 字段：qfq vs 裸价（两项目口径不同）
def test_qfq_mode_asks_for_qfq_field_and_reads_qfqday(monkeypatch):
    """QuantPosition 那份版本用的是 `...,320,qfq`，读 `qfqday`。

    SelectStock 这版必须守裸价（§48.1：静默回落 qfqday 会让同一天两根 K 线来自两个
    复权基准）。但「第 6 个字段」的写法要能被两种口径复用，所以抽成 _tencent_token。
    """
    mod = _load_script()
    assert mod._tencent_token("sh", "600519", 320, adjust="qfq") == "sh600519,day,,,320,qfq"
    assert mod._tencent_token("sh", "600519", 320) == "sh600519,day,,,320,"
    assert mod._tencent_token("sz", "000001", 320) == "sz000001,day,,,320,"


def test_qfq_mode_is_not_the_default():
    """默认必须还是裸价：改成 qfq 会污染 §48 那套复权链。"""
    mod = _load_script()
    assert mod.TENCENT_ADJUST == "raw"
    assert mod._tencent_token("sh", "600519", 320).split(",")[-1] == ""
