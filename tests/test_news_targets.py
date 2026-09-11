from stock_selector.news.targets import build_watch_targets


def _row(news_id, **kw):
    row = {"news_id": news_id, "bullish": [], "bearish": [], "watchlist": [], "risks": []}
    row.update(kw)
    return row


def _pick(docs, **kw):
    for d in docs:
        if all(d.get(k) == v for k, v in kw.items()):
            return d
    raise AssertionError(f"not found: {kw}")


def test_bullish_target():
    rows = [_row("n1", bullish=[{"category": "个股", "name": "欣旺达", "code": "300207",
                                 "score": 8, "reason": "r"}])]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert len(docs) == 1
    t = docs[0]
    assert t["direction"] == "bullish" and t["code"] == "300207"
    assert t["score"] == 8 and t["category"] == "个股"


def test_code_backfill_by_name():
    rows = [_row("n1", bullish=[{"category": "个股", "name": "欣旺达", "code": "",
                                 "score": 5, "reason": "r"}])]
    hints = {"n1": [{"code": "300207", "name": "欣旺达", "market": "A股"}]}
    docs = build_watch_targets(rows, hints, "2026-09-07", "m")
    assert docs[0]["code"] == "300207"


def test_code_backfill_single_hint():
    rows = [_row("n1", bullish=[{"category": "板块", "name": "二线动力电池", "code": "",
                                 "score": 5, "reason": "r"}])]
    hints = {"n1": [{"code": "BK0464", "name": "", "market": "板块"}]}
    docs = build_watch_targets(rows, hints, "2026-09-07", "m")
    assert docs[0]["code"] == "BK0464"


def test_code_not_guessed():
    rows = [_row("n1", bullish=[{"category": "板块", "name": "三线电池", "code": "",
                                 "score": 5, "reason": "r"}])]
    hints = {"n1": [{"code": "BK0464", "name": "", "market": "板块"},
                    {"code": "BK0002", "name": "", "market": "板块"}]}
    docs = build_watch_targets(rows, hints, "2026-09-07", "m")
    assert docs[0]["code"] == ""


def test_watchlist_direction():
    rows = [_row("n1", watchlist=[{"category": "个股", "name": "欣旺达", "code": "300207",
                                   "priority": "高", "reason": "r"}])]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert docs[0]["direction"] == "watch" and docs[0]["score"] == 0
    assert docs[0]["priority"] == "高"


def test_risk_target():
    rows = [_row("n1", risks=[{"score": -6, "desc": "欣旺达动力仍在亏损（虽收窄但未盈利）",
                               "impact": "短线追高易被套", "mitigation": "等盈利拐点"}])]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert docs[0]["direction"] == "risk" and docs[0]["category"] == "风险"
    assert docs[0]["name"] == "欣旺达动力仍在亏损（虽收窄但未盈利）"[:20]
    assert docs[0]["code"] == "" and docs[0]["score"] == -6


def test_merge_same_target():
    rows = [
        _row("n1", bullish=[{"category": "个股", "name": "欣旺达", "code": "300207",
                             "score": 8, "reason": "r1"}]),
        _row("n2", bullish=[{"category": "个股", "name": "欣旺达", "code": "300207",
                             "score": 6, "reason": "r2"}]),
    ]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert len(docs) == 1
    assert docs[0]["source_news_ids"] == ["n1", "n2"]
    assert docs[0]["score"] == 8  # 第一条


def test_merge_keeps_first_name():
    rows = [
        _row("n1", bullish=[{"category": "个股", "name": "欣旺达", "code": "300207",
                             "score": 8, "reason": ""}]),
        _row("n2", bullish=[{"category": "个股", "name": "欣旺达动力", "code": "300207",
                             "score": 5, "reason": ""}]),
    ]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert len(docs) == 1 and docs[0]["name"] == "欣旺达"


def test_skip_non_dict_elements():
    rows = [_row("n1", bullish=[{"category": "个股", "name": "欣旺达", "code": "300207",
                                 "score": 8, "reason": ""}, "bad-string"])]
    docs = build_watch_targets(rows, {}, "2026-09-07", "m")
    assert len(docs) == 1 and docs[0]["code"] == "300207"


def test_empty_rows():
    assert build_watch_targets([], {}, "2026-09-07", "m") == []
