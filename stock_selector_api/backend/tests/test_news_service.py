"""tests/test_news_service.py — 消息面：薄代理 + 持仓相关标记 + 每日摘要推送。

验收重点是「别再静默」：没渠道要出声、全失败要出声、页面拿不到数据要出声。
"""
import pytest

from config import settings
from services import news_service

_SAMPLE = {
    "day": "2026-10-13", "stale_days": 0,
    "counts": {"bullish": 2, "bearish": 2, "risk": 1, "watch": 1},
    "health": {"analyzed": 167, "failed": 0, "fail_rate": 0.0},
    "items": {
        "bullish": [{"code": "", "name": "元件", "category": "板块", "direction": "bullish",
                     "score": 7, "priority": "", "reason": "PCB 涨价", "news_count": 2}],
        "bearish": [
            {"code": "000869", "name": "张裕A", "category": "个股", "direction": "bearish",
             "score": -8, "priority": "", "reason": "控股股东减持 3%", "news_count": 1},
            {"code": "", "name": "电池", "category": "板块", "direction": "bearish",
             "score": -6, "priority": "", "reason": "增值税出口退税率下调", "news_count": 3}],
        "risk": [{"code": "", "name": "新能源调整", "category": "风险", "direction": "risk",
                  "score": -5, "priority": "", "reason": "板块持续调整", "news_count": 1}],
        "watch": [{"code": "002855", "name": "捷荣技术", "category": "个股", "direction": "watch",
                   "score": 0, "priority": "高", "reason": "等回踩", "news_count": 1}],
    },
}


@pytest.fixture
def _patch_fetch(monkeypatch):
    async def fake(day=None):
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in _SAMPLE.items()}
    monkeypatch.setattr(news_service, "_fetch", fake)


async def test_daily_in_mock_mode_returns_empty_shell(client, admin_headers):
    """选股插件没起（mode=mock）时给空壳，前端据此出空态而不是报错。"""
    r = await client.get("/api/v1/news/daily", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["day"] is None and data["mine"] == [] and data["holding_count"] == 0


async def test_daily_requires_login(client):
    assert (await client.get("/api/v1/news/daily")).status_code in (401, 403)


@pytest.mark.parametrize("url", ["/api/v1/news/daily?day=2026-13-99-1",
                                 "/api/v1/news/daily?day=01234567890"])
async def test_daily_validates_day_param(client, admin_headers, url):
    assert (await client.get(url, headers=admin_headers)).status_code == 422


async def test_marking_matches_code_or_name(_patch_fetch, monkeypatch):
    """LLM 给的 code 十有八九是板块码，所以名称也要匹；持仓表里的名字带空格/全角，要先归一。"""
    async def fake_enabled(user_id):
        return [{"code": "000869", "name": "张 裕Ａ"}, {"code": "002855", "name": "捷荣技术"}]
    monkeypatch.setattr(news_service.holdings_repo, "list_enabled", fake_enabled)

    out = await news_service.daily_for_user("u1")
    by_name = {i["name"]: i["holding"] for row in out["items"].values() for i in row}
    assert by_name["张裕A"] is True          # 代码命中
    assert by_name["捷荣技术"] is True        # watch 项名称命中
    assert by_name["元件"] is False and by_name["电池"] is False
    # mine 按分数升序：最负的排前面，且持仓相关优先于全市场
    assert [i["name"] for i in out["mine"]][0] == "张裕A"
    assert out["holding_count"] == 2


async def test_marking_is_empty_when_user_has_no_holdings(_patch_fetch, monkeypatch):
    async def fake_enabled(user_id):
        return []
    monkeypatch.setattr(news_service.holdings_repo, "list_enabled", fake_enabled)
    out = await news_service.daily_for_user("u1")
    assert out["mine"] == [] and out["holding_count"] == 0


def test_digest_puts_my_holdings_first_and_flags_failures():
    data = {**_SAMPLE, "mine": [_SAMPLE["items"]["bearish"][0]], "holding_count": 2}
    title, text = news_service.build_digest(data)
    assert title == "消息面 · 2026-10-13"
    assert "利空 2" in text and "风险 1" in text
    assert "与我的 2 只持仓有关" in text
    assert text.index("减持") < text.index("全市场"), "持仓相关必须排在全市场之前"
    assert "000869" in text


def test_digest_warns_when_analysis_failed():
    data = {**_SAMPLE, "mine": [], "holding_count": 0,
            "health": {"analyzed": 167, "failed": 120, "fail_rate": 0.72}}
    _, text = news_service.build_digest(data)
    assert "分析失败 120/167" in text


def test_digest_risk_line_has_no_empty_reason():
    """风险行在读取层已整成「标题即全文」，正文为空时不能再拖一个空的 ｜。"""
    risk = {"code": "", "name": "IPO欺诈调查可能导致茶百道上市失败或严重推迟", "category": "",
            "direction": "risk", "score": -8, "priority": "", "reason": "", "news_count": 1}
    data = {**_SAMPLE, "mine": [], "holding_count": 0,
            "items": {**_SAMPLE["items"], "risk": [risk]}}
    _, text = news_service.build_digest(data)
    assert "· [风险] IPO欺诈调查可能导致茶百道上市失败或严重推迟 -8\n" in text


def test_digest_says_so_when_there_is_no_batch():
    """整批缺失（比如 LLM 挂了两周）不能显示成「今天没有利空」——那是把故障说成平安。"""
    title, text = news_service.build_digest(dict(news_service._EMPTY))
    assert title == "消息面 · 无数据" and "LLM" in text


def test_digest_marks_stale_batch():
    data = {**_SAMPLE, "stale_days": 2, "mine": [], "holding_count": 0}
    _, text = news_service.build_digest(data)
    assert "2 天前的批次" in text


async def test_push_digest_uses_channels_not_policies(monkeypatch):
    """推送不依赖「订阅策略」：策略表是空的，依赖它就等于永远不发。"""
    sent, inbox = [], []

    class _Channels:
        def list_all(self, ctype):
            if ctype != "dingtalk":
                return []
            # 页面列表是脱敏视图，发送必须走 get_by_key 的解密视图
            return [{"key": "ding_ops", "name": "值班群", "enabled": True, "webhook_url": True}]

        def get_by_key(self, key, ctype=None):
            return {"key": key, "enabled": True, "webhook_url": "https://x/secret-hook"}

    async def fake_send(ctype, channel, subject, content, to=None):
        assert channel["webhook_url"] == "https://x/secret-hook"
        sent.append((ctype, channel["key"], subject, content))

    async def fake_notify(user_id, event_code, title, content, related_type="", related_id=""):
        inbox.append((user_id, event_code, title))

    async def fake_fetch(day=None):
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in _SAMPLE.items()}

    async def fake_enabled(user_id):
        return [{"code": "000869", "name": "张裕A"}]

    class _Users:
        async def list(self, *, filters=None, page=1, size=20):
            return ([{"id": "u1", "username": "tester", "is_active": True}], 1)

    from services import inbox_notify
    from services.notify import senders

    monkeypatch.setattr(news_service, "_fetch", fake_fetch)
    monkeypatch.setattr(news_service.holdings_repo, "list_enabled", fake_enabled)
    monkeypatch.setattr(news_service, "channel_repo", _Channels())
    monkeypatch.setattr(senders, "send", fake_send)
    monkeypatch.setattr(inbox_notify, "notify_user", fake_notify)
    monkeypatch.setattr(news_service, "users_repo", _Users())

    out = await news_service.push_digest()
    assert out["robots"] == ["dingtalk:ding_ops"]
    assert out["users"] == ["tester"]
    assert sent[0][2] == "消息面 · 2026-10-13"
    assert sent[0][3].startswith("消息面 · 2026-10-13")
    assert "与我的 1 只持仓有关" in sent[0][3]
    assert inbox == [("u1", "news_digest", "消息面 · 2026-10-13")]


async def test_push_digest_without_channel_still_notifies_users(monkeypatch):
    """一个可用机器人渠道都没有时，站内信照发，并把「没有渠道」当错误说出来。"""
    inbox = []

    class _NoChannels:
        def list_all(self, ctype):
            return []

    async def fake_notify(user_id, event_code, title, content, related_type="", related_id=""):
        inbox.append(user_id)

    async def fake_fetch(day=None):
        return dict(_SAMPLE)

    async def fake_enabled(user_id):
        return []

    class _Users:
        async def list(self, *, filters=None, page=1, size=20):
            return ([{"id": "u1", "username": "tester", "is_active": True}], 1)

    from services import inbox_notify

    monkeypatch.setattr(news_service, "_fetch", fake_fetch)
    monkeypatch.setattr(news_service.holdings_repo, "list_enabled", fake_enabled)
    monkeypatch.setattr(news_service, "channel_repo", _NoChannels())
    monkeypatch.setattr(news_service, "users_repo", _Users())
    monkeypatch.setattr(inbox_notify, "notify_user", fake_notify)

    out = await news_service.push_digest()
    assert out["robots"] == [] and inbox == ["u1"]


async def test_push_digest_fetches_the_plugin_once(monkeypatch):
    """一天一批：不管几个用户，插件只取一次（逐用户重取是 N 倍无谓请求）。"""
    calls = []

    async def fake_fetch(day=None):
        calls.append(day)
        return dict(_SAMPLE)

    async def fake_enabled(user_id):
        return []

    class _Users:
        async def list(self, *, filters=None, page=1, size=20):
            return ([{"id": f"u{i}", "username": f"u{i}", "is_active": True} for i in range(4)], 4)

    class _NoChannels:
        def list_all(self, ctype):
            return []

    from services import inbox_notify

    async def fake_notify(*a, **k):
        return {}

    monkeypatch.setattr(news_service, "_fetch", fake_fetch)
    monkeypatch.setattr(news_service.holdings_repo, "list_enabled", fake_enabled)
    monkeypatch.setattr(news_service, "channel_repo", _NoChannels())
    monkeypatch.setattr(news_service, "users_repo", _Users())
    monkeypatch.setattr(inbox_notify, "notify_user", fake_notify)

    out = await news_service.push_digest()
    assert len(calls) == 1 and len(out["users"]) == 4


def test_settings_mode_default_is_mock_in_tests():
    assert settings.selection_plugin_mode in ("mock", "http")
