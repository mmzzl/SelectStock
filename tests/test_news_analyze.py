import json
from datetime import datetime

import pytest

from stock_selector.news.analyze import (
    SYSTEM_PROMPT, _extract_json, analyze_news, build_user_prompt,
)
from stock_selector.news.schema import ANALYSIS_SCHEMA

NEWS = {"news_id": "n1", "title": "理想入股欣旺达动力", "summary": "9月4日晚间公告..."}
HINTS = [{"code": "BK0464", "name": "", "market": "板块"},
         {"code": "", "name": "GOOG", "market": "美股"}]


def test_user_prompt_contains_hints():
    prompt = build_user_prompt(NEWS, HINTS)
    assert "已知相关标的" in prompt
    assert "- BK0464（板块）" in prompt
    assert "- GOOG（美股，代码未知）" in prompt


def test_user_prompt_without_hints():
    for hints in (None, []):
        prompt = build_user_prompt(NEWS, hints)
        assert "已知相关标的" not in prompt


def test_extract_json_fenced():
    text = "```json\n{\"a\": 1}\n```"
    assert _extract_json(text) == {"a": 1}


def test_extract_json_raises_without_brace():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def _fake_ok(llm_cfg, messages):
    return json.dumps({
        "summary": "理想26.5亿入股欣旺达动力",
        "sentiment": "利好", "sentiment_score": 8, "impact_period": "中期",
        "bullish": [{"category": "板块", "name": "二线动力电池", "code": "BK0464",
                     "score": 6, "reason": "资本背书"}],
        "bearish": [], "risks": [], "watchlist": [],
        "follow_signals": [], "action": "关注", "key_points": [], "market_reaction": "情绪升温",
        "conclusion": "利好但有风险",
    })


def _datetime_today_str():
    return datetime.today().strftime("%Y-%m-%d")


def test_analyze_news_builds_row(monkeypatch):
    monkeypatch.setattr("stock_selector.news.analyze.chat_completion", _fake_ok)
    row = analyze_news({"model": "GLM-4.7-Flash"}, NEWS, hints=HINTS)
    assert set(ANALYSIS_SCHEMA) <= set(row.keys())
    assert row["stock_hints"] == ["BK0464"]
    assert row["collect_date"] == _datetime_today_str()
    assert row["llm_model"] == "GLM-4.7-Flash"


def _fake_fail(llm_cfg, messages):
    raise RuntimeError("boom")


def test_analyze_news_fallback(monkeypatch):
    monkeypatch.setattr("stock_selector.news.analyze.chat_completion", _fake_fail)
    row = analyze_news({"model": "GLM-4.7-Flash"}, NEWS)
    assert "分析失败" in row["conclusion"]
    assert row["sentiment"] == "中性"


def test_system_prompt_no_fabricate():
    assert "不要编造代码" in SYSTEM_PROMPT
