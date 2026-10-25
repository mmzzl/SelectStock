"""板块过滤（2026-10-26 拍板）：科创板 688/689、创业板 300/301 不出榜——用户没有交易权限。"""
from stock_selector_qlib import artifacts, serving


def test_board_excluded_prefixes():
    assert serving.board_excluded("SH688498")
    assert serving.board_excluded("SH689009")
    assert serving.board_excluded("SZ300308")
    assert serving.board_excluded("SZ301165")
    assert serving.board_excluded("688001")       # 裸码也能判
    assert not serving.board_excluded("SH600396")
    assert not serving.board_excluded("SZ000960")
    assert not serving.board_excluded("SZ001258")
    assert not serving.board_excluded("SH603256")


def test_score_pool_filters(tmp_path, monkeypatch):
    f = tmp_path / "universe.txt"
    f.write_text("SH600396\nSH688498\nSZ301165\nSZ000960\n", encoding="utf-8")
    monkeypatch.setattr(serving, "POOL_SOURCE", "universe")
    monkeypatch.setattr(serving, "UNIVERSE_FILE", f)
    assert serving.score_pool(object()) == ["SH600396", "SZ000960"]


def test_score_pool_filters_model_mode(monkeypatch):
    art = type("Art", (), {"pool": ["SH600396", "SZ300308", "SH688498"]})()
    monkeypatch.setattr(serving, "POOL_SOURCE", "model")
    assert serving.score_pool(art) == ["SH600396"]


def test_ranks_by_code_says_why_for_excluded_boards(tmp_path, monkeypatch):
    """持仓页问到 300/688 时，原因要写「按交易权限剔出名单」，不能写成「模型没给这只打过分」。

    两种「没名次」的应对不一样：前者是规则（今晚起就不打分），后者像缺数（该去查行情）。
    """
    listing = tmp_path / "universe.txt"
    listing.write_text("SH600000\nSZ300313\n", encoding="utf-8")
    monkeypatch.setattr(serving, "UNIVERSE_FILE", listing)
    monkeypatch.setattr(serving, "POOL_SOURCE", "universe")
    monkeypatch.setattr(artifacts, "load", lambda *a, **k: type("Art", (), {"pool": []})())
    monkeypatch.setattr(serving, "guard_notes", lambda: {})
    monkeypatch.setattr(serving, "day_ranking", lambda refresh=False, wait=True: [
        {"code": "600000", "date": "2026-10-23", "score_raw": 1.2, "score": 1.0,
         "rank": 1, "universe": 1}])

    out = serving.ranks_by_code(["300313", "510300"])
    by = {it["code"]: it for it in out["items"]}
    assert by["300313"]["in_pool"] is False
    assert "交易权限" in by["300313"]["note"]
    assert "交易权限" not in by["510300"]["note"]      # 其它池外票保持原话
