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


MAPS = ({"BK0459": "电池"}, {"002466": "天齐锂业", "000830": "鲁西化工", "000869": "张 裕Ａ"})


def _bear(name, code):
    return [_row("n1", bearish=[{"category": "个股", "name": name, "code": code,
                                 "score": -7, "reason": "r"}])]


def test_foreign_code_dropped_but_name_kept():
    # 000830 在韩国是 SK海力士、在 A 股是鲁西化工：代码留着就会跳错股票
    docs = build_watch_targets(_bear("SK海力士", "000830"), {}, "2026-10-13", "m", MAPS)
    assert docs[0]["code"] == "" and docs[0]["name"] == "SK海力士"


def test_unknown_code_dropped():
    docs = build_watch_targets(_bear("华虹宏力", "1347"), {}, "2026-10-13", "m", MAPS)
    assert docs[0]["code"] == ""


def test_matching_code_kept():
    docs = build_watch_targets(_bear("天齐锂业", "002466"), {}, "2026-10-13", "m", MAPS)
    assert docs[0]["code"] == "002466"


def test_name_compare_ignores_spacing_and_fullwidth():
    docs = build_watch_targets(_bear("张裕A", "000869"), {}, "2026-10-13", "m", MAPS)
    assert docs[0]["code"] == "000869"


def test_board_code_kept_without_name_match():
    rows = [_row("n1", bearish=[{"category": "板块", "name": "新能源电池", "code": "BK0459",
                                 "score": -6, "reason": "r"}])]
    assert build_watch_targets(rows, {}, "2026-10-13", "m", MAPS)[0]["code"] == "BK0459"


def test_no_maps_skips_verification():
    docs = build_watch_targets(_bear("SK海力士", "000830"), {}, "2026-10-13", "m")
    assert docs[0]["code"] == "000830"


def test_risk_rows_ignore_maps():
    rows = [_row("n1", risks=[{"score": -6, "desc": "估值已透支", "impact": "", "mitigation": ""}])]
    docs = build_watch_targets(rows, {}, "2026-10-13", "m", MAPS)
    assert docs[0]["direction"] == "risk" and docs[0]["code"] == ""


def test_unlisted_board_code_kept():
    # BK 是东财板块命名空间，本表没收录也不会跟 A 股代码撞，留着还能做跨天统计
    rows = [_row("n1", bearish=[{"category": "板块", "name": "锂电材料", "code": "BK000148",
                                 "score": -7, "reason": "r"}])]
    assert build_watch_targets(rows, {}, "2026-10-13", "m", MAPS)[0]["code"] == "BK000148"


def test_code_copied_as_name_is_replaced_by_official_name():
    rows = [_row("n1", bullish=[{"category": "板块", "name": "BK0459", "code": "BK0459",
                                 "score": 6, "reason": "r"}])]
    doc = build_watch_targets(rows, {}, "2026-10-13", "m", MAPS)[0]
    assert doc["name"] == "电池" and doc["code"] == "BK0459"
