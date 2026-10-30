"""资金榜 / 原始消息流的 8010 薄代理：只透传 8002 的信封，mock 模式回空壳不编数。

红线同板块热力图：窗口怎么切、消息怎么排都在插件侧算完；后端一处算术都不做，
唯一做的事是把 code!=0 翻成 BizError，让前端出「数据未生成 / 档位缺数」的提示。
"""
import pytest

from config import settings
from services import fund_service, news_service


class _StubResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


def _client_for(payload, calls):
    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            calls.append((url, params or {}))
            return _StubResponse(payload)

    return _StubClient


RANK_SHELL = {"asof": None, "window": None, "windows": [], "side": "in", "days": 0,
              "count": 0, "total": 0, "rows": [], "days_used": [], "thin_days": [],
              "notice": None}


# ---------------------------------------------------------------- 路由层

async def test_fund_rank_in_mock_mode_returns_empty_shell(client, admin_headers):
    r = await client.get("/api/v1/fund/rank?window=3&side=in", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["data"] == RANK_SHELL


@pytest.mark.parametrize("url", [
    "/api/v1/fund/rank",
    "/api/v1/news/feed",
    "/api/v1/news/item?id=6ae3ef969ca5d4a7d72421b8",
])
async def test_new_endpoints_require_login(client, url):
    assert (await client.get(url)).status_code in (401, 403)


@pytest.mark.parametrize("url", [
    "/api/v1/fund/rank?window=0",
    "/api/v1/fund/rank?window=999",
    "/api/v1/fund/rank?side=both",
    "/api/v1/news/feed?limit=0",
    "/api/v1/news/item?id=short",
])
async def test_new_endpoints_validate_params(client, admin_headers, url):
    assert (await client.get(url, headers=admin_headers)).status_code == 422


# ---------------------------------------------------------------- 服务层透传

async def test_fund_rank_passes_plugin_envelope_through(monkeypatch):
    calls = []
    payload = {"code": 0, "data": {**RANK_SHELL, "asof": "2026-10-29", "window": 3,
                                   "windows": [3, 7], "total": 5373, "count": 1,
                                   "rows": [{"code": "600000", "net_inflow": 3.0e8}]}}
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(fund_service.httpx, "AsyncClient", _client_for(payload, calls))

    out = await fund_service.rank(window=7, side="out", limit=50)
    assert out["asof"] == "2026-10-29" and out["total"] == 5373
    assert calls[0][0].endswith("/api/fund/rank")
    assert calls[0][1] == {"window": 7, "side": "out", "limit": 50}


async def test_fund_rank_missing_product_becomes_biz_error(monkeypatch):
    """脚本没跑过 → code=1 + 人话。翻成 BizError 让前端提示，不能回空榜骗人。"""
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(fund_service.httpx, "AsyncClient",
                        _client_for({"code": 1, "message": "缺少 rank.parquet，请先执行 python scripts/build_fund_rank.py",
                                     "data": []}, []))
    with pytest.raises(Exception) as exc:
        await fund_service.rank()
    assert "build_fund_rank.py" in str(exc.value)


async def test_news_feed_forwards_the_cursor(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(news_service.httpx, "AsyncClient",
                        _client_for({"code": 0, "data": {"rows": [], "next_cursor": None, "limit": 20}}, calls))
    out = await news_service.feed(limit=20, before="6ae3ef969ca5d4a7d72421b8")
    assert out["rows"] == []
    assert calls[0][1] == {"limit": 20, "before": "6ae3ef969ca5d4a7d72421b8"}


async def test_news_feed_without_cursor_sends_empty_before(monkeypatch):
    """8002 的 before 允许空串（表示从最新开始），代理不能自己发明一个游标。"""
    calls = []
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(news_service.httpx, "AsyncClient",
                        _client_for({"code": 0, "data": {"rows": [], "next_cursor": None}}, calls))
    await news_service.feed(limit=30, before=None)
    assert calls[0][1] == {"limit": 30, "before": ""}


async def test_news_item_passes_detail_through(monkeypatch):
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(news_service.httpx, "AsyncClient", _client_for(
        {"code": 0, "data": {"id": "6ae3ef969ca5d4a7d72421b8", "title": "人民锐评",
                             "summary": "正文", "showTime": "2026-10-30 09:13:28",
                             "stockList": ["90.BK0800"], "pinglun_Num": 2, "share": 3}}, []))
    out = await news_service.item("6ae3ef969ca5d4a7d72421b8")
    assert out["title"] == "人民锐评" and out["stockList"] == ["90.BK0800"]


async def test_news_item_unknown_id_becomes_biz_error(monkeypatch):
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(news_service.httpx, "AsyncClient", _client_for(
        {"code": 3, "message": "没有这条消息（可能已被源清理）", "data": None}, []))
    with pytest.raises(Exception) as exc:
        await news_service.item("6ae3ef969ca5d4a7d72421b8")
    assert "没有这条消息" in str(exc.value)
