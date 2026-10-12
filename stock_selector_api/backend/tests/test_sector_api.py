"""板块热力图薄代理：后端只透传，字段形状必须与 8002 插件一致，且不编造行情。"""
import pytest

from config import settings
from services import sector_service


class _StubResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


def _client_for(payload):
    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _StubResponse(payload)

    return _StubClient


async def test_heat_in_mock_mode_returns_empty_shell(client, admin_headers):
    """mock 模式必须回空壳而不是假涨幅：前端据此出「数据未生成」空态。"""
    r = await client.get("/api/v1/sector/heat?window=3", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data == {"asof": None, "window": None, "windows": [], "kinds": [], "count": 0, "boards": []}


@pytest.mark.parametrize("url", [
    "/api/v1/sector/heat",
    "/api/v1/sector/board?board_code=A05",
    "/api/v1/sector/kline?code=000001",
])
async def test_sector_requires_login(client, url):
    assert (await client.get(url)).status_code in (401, 403)


@pytest.mark.parametrize("url", [
    "/api/v1/sector/heat?window=0",
    "/api/v1/sector/heat?kind=other",
    "/api/v1/sector/kline?code=123",
    "/api/v1/sector/kline?code=000001&days=5",
])
async def test_sector_validates_params(client, admin_headers, url):
    assert (await client.get(url, headers=admin_headers)).status_code == 422


async def test_heat_passes_plugin_envelope_through(monkeypatch):
    payload = {"code": 0, "data": {"asof": "2026-10-09", "window": 3, "windows": [3, 7, 30],
                                   "kinds": ["concept", "industry"], "count": 1,
                                   "boards": [{"board_code": "A05", "ret_pct": 1.5}]}}
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(sector_service.httpx, "AsyncClient", _client_for(payload))
    out = await sector_service.heat(3, "all")
    assert out["asof"] == "2026-10-09" and out["boards"][0]["board_code"] == "A05"


async def test_plugin_business_code_becomes_biz_error(monkeypatch):
    """脚本没跑过 → 插件回 code=1 + 人话，后端翻成 400 让前端提示，而不是空图骗人。"""
    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(sector_service.httpx, "AsyncClient",
                        _client_for({"code": 1, "message": "缺少 board.parquet", "data": None}))
    with pytest.raises(Exception) as exc:
        await sector_service.board_detail("A05", 3)
    assert "board.parquet" in str(exc.value)


async def test_plugin_unreachable_is_friendly_error(monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("connection refused")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(settings, "selection_plugin_mode", "http")
    monkeypatch.setattr(sector_service.httpx, "AsyncClient", _Boom)
    with pytest.raises(Exception) as exc:
        await sector_service.stock_kline("000001", 120)
    assert "暂不可用" in str(exc.value)
