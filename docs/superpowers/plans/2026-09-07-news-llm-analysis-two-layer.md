# 新闻 LLM 分析两层存储 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `scripts/analyze_news_llm.py` 从伪代码+单层 parquet 改造为"news_analysis + watch_target"两层结构、真实读 MongoDB，支持"今日关注"聚合与跨天统计（热度排行/个股趋势）。

**Architecture:** 拆出可测试包 `stock_selector/news/`（normalize/fetch/analyze/targets/store/stats 六模块），脚本瘦身为 CLI 编排。第一层 news_analysis 按 news_id 增量落 parquet + Mongo（`_id=collect_date|news_id` 整批重建）；第二层 watch_target 由当日行拆解合并后整文件覆盖 parquet + Mongo 整日 delete+insert。Mongo 写失败只 WARNING、parquet 为准。

**Tech Stack:** Python 3.12 · pandas/pyarrow(zstd) · pymongo(4.x) · zai-sdk(ZhipuAiClient, GLM-4.7-Flash) · pytest

**Conventions (learned from codebase):**
- Language: Python 3.12, notebook-free scripts, `from __future__ import annotations` in new modules
- Naming: snake_case 函数/变量，模块内中文 docstring + logging
- Imports: 脚本内 `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` 后 import 包；包内绝对导入
- Error handling: Mongo 读失败 fallback --demo；写失败 WARNING 不阻断；LLM 失败 retries 3 + 降级中性
- Testing: pytest，文件 `tests/test_*.py`；Mongo 相关用 `_mongo_available()` + `pytestmark = skipif`（参考 tests/test_backtest_store.py）；parquet 测试用 pytest `tmp_path`
- Logging: `logger = logging.getLogger("news.xxx")`；脚本层 `setup_logging()` 写 `logs/news_analyze_YYYYMMDD.log`
- Config: `stock_selector/config/config.yaml` 经 `load_config()` 读；mongodb 节已有 host/port/database/collection
- DB: pymongo 原生 collection/db 注入（类构造注入 db，参考 `stock_selector/backtest/store.py`，便于 fake）

**Domain Skills (from session context):**
- 单测编写: `test-code-generator` — 依据测试用例表生成 pytest（本计划已含完整测试代码，可直接采用）
- 测试方法: `test-driven-development` — TDD RED-GREEN 循环，每任务先写失败测试
- 排障调试: `systematic-debugging` — 出现失败时先收集证据再定位，禁止盲改
- 代码编写: `code-compliance-check` — 提交前 lint 与编码规范检查

---

## 上游质量门（Innovation Mode）

输入来自 brainstorming Phase 3 微设计 `docs/superpowers/specs/2026-09-07-news-llm-analysis-two-layer.md`（已批准 v2）。Innovation 模式无上游 evaluator 需验证 → 跳过，直接进入计划。

## 1. 文件映射

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `stock_selector/news/__init__.py` | 新增 | 包标记（空） |
| `stock_selector/news/schema.py` | 新增 | ANALYSIS_SCHEMA(23列)/WATCH_SCHEMA(10列) |
| `stock_selector/news/normalize.py` | 新增 | normalize_secid |
| `stock_selector/news/fetch.py` | 新增 | natural_day_start/build_query/news_to_item/fetch_news |
| `stock_selector/news/analyze.py` | 新增 | SYSTEM_PROMPT增强/build_user_prompt/chat_completion/_extract_json/analyze_news |
| `stock_selector/news/targets.py` | 新增 | build_watch_targets + 拆解/回填/合并辅助 |
| `stock_selector/news/store.py` | 新增 | parquet双落盘 + Mongo双写入 + create_indexes |
| `stock_selector/news/stats.py` | 新增 | load_watch_range/rank_heat/trend(parquet) + rank_heat_mongo/trend_mongo |
| `scripts/analyze_news_llm.py` | 重写 | CLI 编排：--demo/--days/--limit/--no-mongo/--stats/--start/--end/--category/--top/--direction/--code |
| `tests/test_news_normalize.py` | 新增 | TDD 单测 |
| `tests/test_news_fetch.py` | 新增 | TDD 单测（FakeCursor） |
| `tests/test_news_analyze.py` | 新增 | TDD 单测（monkeypatch chat_completion） |
| `tests/test_news_targets.py` | 新增 | TDD 单测 |
| `tests/test_news_store.py` | 新增 | parquet(tmp_path)+mongo(skipif) |
| `tests/test_news_stats.py` | 新增 | TDD 单测 + skipif pipeline 验证 |

> 说明：原 script 中 `ANALYSIS_SCHEMA`/`SYSTEM_PROMPT`/`chat_completion`/`_extract_json`/`analyze_news`/`save` 迁入包内；`DEMO_NEWS`/`setup_logging`/`main` 保留在脚本。watch_target 不新建第三层聚合（设计§8 方案A惰性聚合）。

---

### Task 1: schema + normalize_secid

**Files:**
- Create: `stock_selector/news/schema.py`
- Create: `stock_selector/news/normalize.py`
- Test: `tests/test_news_normalize.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| NN-01 | 设计§2.1 | normal | `normalize_secid("90.BK0464")` | `code=="BK0464", market=="板块"` | unit | `tests/test_news_normalize.py::test_board_secid` | `python -m pytest tests/test_news_normalize.py -q` |
| NN-02 | 设计§2.1 | normal | `normalize_secid("1007.BK000199")` | `code=="BK000199", market=="板块"` | unit | 同上 `test_board_1007` | 同上 |
| NN-03 | 设计§2.1 | normal | `normalize_secid("0.159945")` | `code=="159945", market=="A股"` | unit | `::test_a_share_prefixed` | 同上 |
| NN-04 | 设计§2.1 | normal | `normalize_secid("1.688256")` | `code=="688256", market=="A股"` | unit | 同上 | 同上 |
| NN-05 | 设计§2.1 | normal | `normalize_secid("150.014238")` | `code=="014238", market=="A股"` | unit | 同上 | 同上 |
| NN-06 | 设计§2.1 | normal | `normalize_secid("105.GOOG")` | `code=="", name=="GOOG", market=="美股"` | unit | `::test_us_market` | 同上 |
| NN-07 | 设计§2.1 | normal | `normalize_secid("201.HK49")` | `name=="HK49", market=="港股"` | unit | `::test_hk_market` | 同上 |
| NN-08 | 设计§2.1 | boundary | `normalize_secid("116.06030")` | `market=="未知", code=="", name==""`（不强行归一） | unit | `::test_unknown_prefix_returns_unknown` | 同上 |
| NN-09 | 设计§2.1 | boundary | `normalize_secid("999.356570")` | `market=="未知"` | unit | 同上 | 同上 |
| NN-10 | 设计§2.1 | exception | `normalize_secid("")`, `None`, `"bad"` | 返回 `market=="未知"` 不抛错 | unit | `::test_garbage_input` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_normalize.py`**

```python
from stock_selector.news.normalize import normalize_secid


def test_board_secid():
    assert normalize_secid("90.BK0464")["code"] == "BK0464"
    assert normalize_secid("90.BK0464")["market"] == "板块"


def test_board_1007():
    out = normalize_secid("1007.BK000199")
    assert out["code"] == "BK000199" and out["market"] == "板块"


def test_a_share_prefixed():
    assert normalize_secid("0.159945")["code"] == "159945"
    assert normalize_secid("1.688256")["code"] == "688256"
    assert normalize_secid("150.014238")["code"] == "014238"
    for s in ("0.159945", "1.688256", "150.014238"):
        assert normalize_secid(s)["market"] == "A股"


def test_us_market():
    out = normalize_secid("105.GOOG")
    assert out["code"] == "" and out["name"] == "GOOG" and out["market"] == "美股"


def test_hk_market():
    out = normalize_secid("201.HK49")
    assert out["name"] == "HK49" and out["market"] == "港股"


def test_unknown_prefix_returns_unknown():
    for s in ("116.06030", "999.356570"):
        out = normalize_secid(s)
        assert out["market"] == "未知" and out["code"] == "" and out["name"] == ""


def test_garbage_input():
    for s in ("", None, "bad"):
        out = normalize_secid(s)
        assert out["market"] == "未知" and out["code"] == "" and out["name"] == ""
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_news_normalize.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stock_selector.news'`

- [x] **Step 3: 实现最小代码 `stock_selector/news/schema.py`**

```python
"""两层存储的列定义。"""

ANALYSIS_SCHEMA = [
    "collect_date", "news_id", "title", "source", "url", "publish_time",
    "content_excerpt", "summary", "sentiment", "sentiment_score",
    "impact_period", "bullish", "bearish", "risks", "watchlist",
    "follow_signals", "action", "key_points", "market_reaction",
    "conclusion", "llm_model", "raw",
    "stock_hints",
]

WATCH_SCHEMA = [
    "collect_date", "code", "name", "category", "direction", "score",
    "priority", "reason", "source_news_ids", "llm_model",
]
```

- [x] **Step 4: 实现最小代码 `stock_selector/news/normalize.py`**，并创建 `stock_selector/news/__init__.py`（空文件）

```python
"""东方财富 stockList 的 secid 归一化。"""


def normalize_secid(secid: str) -> dict:
    """把 secid 归一化为 {"code", "name", "market"}。

    - 90.BK0464 / 1007.BK000199 -> {"code": "BK0464", "name": "", "market": "板块"}
    - 0.159945 / 1.688256 / 150.014238 -> {"code": "159945", "name": "", "market": "A股"}
    - 105.GOOG 等 -> {"code": "", "name": "GOOG", "market": "美股"}
    - 201.HK49 -> {"code": "", "name": "HK49", "market": "港股"}
    - 116.06030 / 999.356570 等未知前缀 -> {"code": "", "name": "", "market": "未知"}
    """
    if not secid:
        return {"code": "", "name": "", "market": "未知"}
    s = str(secid).strip()
    prefix, _, suffix = s.partition(".")
    if not prefix or not suffix:
        return {"code": "", "name": "", "market": "未知"}
    if prefix in ("90", "1007") and suffix[:2] == "BK":
        return {"code": suffix, "name": "", "market": "板块"}
    if prefix in ("0", "1", "150") and suffix.isdigit() and len(suffix) == 6:
        return {"code": suffix, "name": "", "market": "A股"}
    if prefix in ("105", "106", "107", "153"):
        return {"code": "", "name": suffix, "market": "美股"}
    if prefix == "201":
        return {"code": "", "name": suffix, "market": "港股"}
    return {"code": "", "name": "", "market": "未知"}
```

- [x] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_news_normalize.py -q`
Expected: PASS — 8 passed

- [ ] **Step 6: Commit**

```bash
git add stock_selector/news tests/test_news_normalize.py
git commit -m "feat(news): add secid normalization and two-layer schemas"
```

---

### Task 2: fetch 真实读 Mongo

**Files:**
- Create: `stock_selector/news/fetch.py`
- Test: `tests/test_news_fetch.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| FN-01 | 设计§2.2 | normal | `natural_day_start(1, datetime(2026,9,7))` | `"2026-09-06 00:00:00"` | unit | `::test_natural_day_start` | `python -m pytest tests/test_news_fetch.py -q` |
| FN-02 | 设计§2.2 | normal | `natural_day_start(3, datetime(2026,9,7))` | `"2026-09-04 00:00:00"` | unit | 同上 | 同上 |
| FN-03 | 设计§2.2 | boundary | `build_query(0)` | `{}`（不限时间） | unit | `::test_build_query_no_filter` | 同上 |
| FN-04 | 设计§2.1/§3.1 | normal | news 文档含 title/summary/showTime/stockList | 映射 content=summary、publish_time=showTime、source=eastmoney、url=""、hints 已归一化 | unit | `::test_news_to_item_maps_fields` | 同上 |
| FN-05 | 设计§2.1/§3.1 | exception | 文档缺 url/source/stockList | 不抛错，字段空值兜底 | unit | `::test_news_to_item_missing_fields` | 同上 |
| FN-06 | 设计§2.2 | boundary | FakeCol 5 条、`fetch_news(col, days=1, limit=2)` | 返回 2 条、showTime>=自然日起点 | unit | `::test_fetch_news_limit_and_filter` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_fetch.py`**

```python
from datetime import datetime

from stock_selector.news.fetch import build_query, natural_day_start, fetch_news


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs = sorted(self._docs, key=key, reverse=True)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCol:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query):
        if not query:
            return FakeCursor(list(self.docs))
        gte = query["showTime"]["$gte"]
        return FakeCursor([d for d in self.docs if d.get("showTime") >= gte])


def _news(**kw):
    doc = {"_id": "69b34b434569bcac902bd6f0",
           "title": "理想入股欣旺达动力", "summary": "9月4日晚间公告...",
           "showTime": "2026-09-06 07:08:00", "stockList": ["90.BK0464", "0.159945"]}
    doc.update(kw)
    return doc


def test_natural_day_start():
    assert natural_day_start(1, datetime(2026, 9, 7)) == "2026-09-06 00:00:00"
    assert natural_day_start(3, datetime(2026, 9, 7)) == "2026-09-04 00:00:00"


def test_build_query_no_filter():
    assert build_query(0) == {}


def test_news_to_item_maps_fields():
    item = fetch_news(FakeCol([_news()]), days=1, limit=50)[0]
    assert item["news_id"] == "69b34b434569bcac902bd6f0"
    assert item["content"] == "9月4日晚间公告..."
    assert item["publish_time"] == "2026-09-06 07:08:00"
    assert item["source"] == "eastmoney" and item["url"] == ""
    assert item["hints"][0]["code"] == "BK0464"
    assert item["hints"][1]["code"] == "159945"


def test_news_to_item_missing_fields():
    item = fetch_news(FakeCol([_news(stockList=None)]), days=0, limit=50)[0]
    assert item["hints"] == [] and item["url"] == ""


def test_fetch_news_limit_and_filter():
    docs = [_news(_id="1", showTime="2026-09-06 07:00:00"),
            _news(_id="2", showTime="2026-09-05 07:00:00"),
            _news(_id="3", showTime="2026-09-07 07:00:00"),
            _news(_id="4", showTime="2026-09-08 07:00:00"),
            _news(_id="5", showTime="2026-09-09 07:00:00")]
    items = fetch_news(FakeCol(docs), days=1, limit=2)
    assert len(items) == 2
    assert all(i["publish_time"] >= "2026-09-06 00:00:00" for i in items)
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_news_fetch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stock_selector.news.fetch'`

- [x] **Step 3: 实现最小代码 `stock_selector/news/fetch.py`**

```python
"""从 MongoDB 读取东财新闻（真实实现）。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from stock_selector.news.normalize import normalize_secid

logger = logging.getLogger("news.fetch")


def natural_day_start(days: int, today: datetime | None = None) -> str:
    """自然日口径：返回 (今天 - N天) 的 00:00:00。"""
    today = today or datetime.now()
    return (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")


def build_query(days: int) -> dict:
    return {"showTime": {"$gte": natural_day_start(days)}} if days > 0 else {}


def news_to_item(d: dict) -> dict:
    """把 mongo 文档映射为分析输入字段。"""
    return {
        "news_id": str(d.get("_id")),
        "title": d.get("title") or "",
        "content": d.get("summary") or "",
        "source": "eastmoney",
        "url": "",
        "publish_time": str(d.get("showTime") or ""),
        "hints": [normalize_secid(x) for x in (d.get("stockList") or [])],
    }


def fetch_news(col, days: int, limit: int) -> list[dict]:
    """从 news 集合读取新闻（col 可注入，便于 fake 测试）。"""
    cursor = col.find(build_query(days)).sort("_id", -1)
    if limit:
        cursor = cursor.limit(limit)
    return [news_to_item(d) for d in cursor]
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_news_fetch.py -q`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
git add stock_selector/news/fetch.py tests/test_news_fetch.py
git commit -m "feat(news): real mongo fetch with natural-day filter"
```

---

### Task 3: analyze 模块（prompt 增强 + 行组装）

**Files:**
- Create: `stock_selector/news/analyze.py`
- Test: `tests/test_news_analyze.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| AN-01 | 设计§5 | normal | `build_user_prompt(news, hints=[{code:BK0464,market:"板块"},...])` | prompt 含 `已知相关标的`、`- BK0464（板块）`、`- GOOG（美股，代码未知）` | unit | `::test_user_prompt_contains_hints` | `python -m pytest tests/test_news_analyze.py -q` |
| AN-02 | 设计§5 | boundary | hints=None/[] | prompt 无提示段 | unit | `::test_user_prompt_without_hints` | 同上 |
| AN-03 | 设计§5 | unit | `_extract_json` 输入带 ``` ```json ``` 围栏 | 提取到完整 dict | unit | `::test_extract_json_fenced` | 同上 |
| AN-04 | 设计§5 | exception | `_extract_json` 无 `{` | raise ValueError | unit | `::test_extract_json_raises_without_brace` | 同上 |
| AN-05 | 设计§5 | normal | monkeypatch chat_completion 返回合法 JSON | 行含所有 ANALYSIS_SCHEMA 键、`stock_hints` 只含 code、`collect_date` 为 `YYYY-MM-DD` 字符串 | unit | `::test_analyze_news_builds_row` | 同上 |
| AN-06 | 设计§9 | exception | monkeypatch chat_completion 抛错 3 次 | 降级中性行（conclusion 含"分析失败"），未抛异常 | unit | `::test_analyze_news_fallback` | 同上 |
| AN-07 | 设计§5 | regression | SYSTEM_PROMPT | 含"不要编造代码"条款 | unit | `::test_system_prompt_no_fabricate` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_analyze.py`**

```python
import json

import pytest

from stock_selector.news.analyze import (
    SYSTEM_PROMPT, _extract_json, analyze_news, build_user_prompt,
)
from stock_selector.news.schema import ANALYSIS_SCHEMA

NEWS = {"news_id": "n1", "title": "理想入股欣旺达动力", "content": "9月4日晚间公告..."}
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


def test_analyze_news_builds_row(monkeypatch):
    monkeypatch.setattr("stock_selector.news.analyze.chat_completion", _fake_ok)
    row = analyze_news({"model": "GLM-4.7-Flash"}, NEWS)
    assert set(ANALYSIS_SCHEMA) <= set(row.keys())
    assert row["stock_hints"] == ["BK0464"]
    assert row["collect_date"] == datetime_today_str()
    assert row["llm_model"] == "GLM-4.7-Flash"


def datetime_today_str():
    from datetime import datetime
    return datetime.today().strftime("%Y-%m-%d")


def _fake_fail(llm_cfg, messages):
    raise RuntimeError("boom")


def test_analyze_news_fallback(monkeypatch):
    monkeypatch.setattr("stock_selector.news.analyze.chat_completion", _fake_fail)
    row = analyze_news({"model": "GLM-4.7-Flash"}, NEWS)
    assert "分析失败" in row["conclusion"]
    assert row["sentiment"] == "中性"


def test_system_prompt_no_fabricate():
    assert "不要编造代码" in SYSTEM_PROMPT
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_news_analyze.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stock_selector.news.analyze'`

- [x] **Step 3: 实现 `stock_selector/news/analyze.py`**（在下方完整代码基础上，把 SYSTEM_PROMPT 原第 6 条后追加"不要编造代码"条款；`chat_completion`/`_extract_json`/`resolve_api_key` 从旧 script 迁移）

```python
"""LLM 分析（智谱 GLM，zai-sdk）。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from stock_selector.news.schema import ANALYSIS_SCHEMA

logger = logging.getLogger("news.analyze")

SYSTEM_PROMPT = """你是一名专业的 A股 财经分析师。给你一条财经新闻，请按资深买方研究员的口吻输出分析。

注意：
1. 结论要克制、客观，区分"资本层面合作"与"利润兑现"，避免把利好简单等同于大涨。
2. 从新闻反推市场反应，明确列出「利好什么、利空什么」：利好/利空的目标既可以是板块/行业，
   也可以是相关个股。能确定个股代码的尽量填 code，填不了给空字符串。
3. 每个标的的 score 表示影响方向与强度：好处用 1~10 的正数，坏处用 -10~-1 的负数。
   同一板块内相关个股的评分可不同（直接受益者分更高，间接受益者分更低）。
4. 额外给出「当前应该关注哪些」：watchlist 是现在就该盯着的标的（结合短期情绪强度给优先级
   高/中/低）；follow_signals 是后续需要验证的信号（订单落地、盈利拐点、板块联动等），
   action 用一两句话说明当下该怎么做（关注、观望、等回踩、等信号确认等）。
5. 必须提示利好兑现的风险：risks 列出"利好不会马上兑现"的障碍（如标的基本面仍亏损、投的是
   子公司而非上市公司本体、估值已透支、限售解禁、定增摊薄等），每项说明 impact(对股价/操作的
   影响)和 mitigation(什么条件下风险可解除/如何规避)。score 用 -10~-1 表示风险强度。
6. 只输出一个 JSON 对象，不要输出任何多余文字或 markdown 代码块标记。
7. 若问题中提供「已知相关标的」，bullish/bearish/watchlist 的 code 应优先沿用这些已知代码；
   无法确定时给空字符串，不要编造代码。

JSON 结构：
{
  "summary": "一句话概括事件要点（含关键数字，如金额/估值/股份比例）",
  "sentiment": "利好" | "利空" | "中性",
  "sentiment_score": -10 到 10 的整数（全局净影响，利好为正，利空为负）,
  "impact_period": "短期" | "中期" | "长期",
  "bullish": [
    {"category": "板块 | 行业 | 个股", "name": "二线动力电池", "code": "", "score": 6, "reason": "..."},
    {"category": "个股", "name": "欣旺达", "code": "300207", "score": 8, "reason": "..."}
  ],
  "bearish": [{"category": "个股", "name": "同类亏损电池厂", "code": "", "score": -3, "reason": "..."}],
  "risks": [{"score": -6, "desc": "...", "impact": "...", "mitigation": "..."}],
  "watchlist": [{"category": "个股", "name": "欣旺达", "code": "300207", "priority": "高", "reason": "..."}],
  "follow_signals": ["..."],
  "action": "...",
  "key_points": ["..."],
  "market_reaction": "...",
  "conclusion": "..."
}"""


def resolve_api_key(api_key: str) -> str:
    return api_key or os.environ.get("ZHIPU_API_KEY", "")


def build_hint_lines(hints):
    lines = []
    for h in hints or []:
        if h.get("code"):
            lines.append(f"- {h['code']}（{h.get('market', '')}）")
        elif h.get("name"):
            lines.append(f"- {h['name']}（{h.get('market', '')}，代码未知）")
    return lines


def build_user_prompt(news: dict, hints: list[dict] | None = None) -> str:
    """构造 user prompt；有已知标的线索时追加提示段。"""
    excerpt = (news.get("content") or "")[:200]
    parts = [f"新闻标题：{news.get('title', '')}", f"新闻内容：{excerpt}"]
    hint_lines = build_hint_lines(hints)
    if hint_lines:
        parts.append("已知相关标的（来自东财 stockList，可沿用其代码）：")
        parts.extend(hint_lines)
    parts.append("请按上述要求输出 JSON 分析。")
    return "\n".join(parts)


def chat_completion(llm_cfg: dict, messages: list[dict]) -> str:
    """调用智谱 zai-sdk chat.completions，返回模型文本。"""
    from zai import ZhipuAiClient

    api_key = resolve_api_key(llm_cfg.get("api_key", ""))
    if not api_key:
        raise SystemExit("缺少 GLM api_key：请在 config.yaml 的 llm.api_key 配置，或设环境变量 ZHIPU_API_KEY")

    client = ZhipuAiClient(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=llm_cfg["model"],
            messages=messages,
            temperature=float(llm_cfg.get("temperature", 0.3)),
            max_tokens=int(llm_cfg.get("max_tokens", 2048)),
            timeout=float(llm_cfg.get("timeout", 60)),
        )
    finally:
        client.close()
    return resp.choices[0].message.content.strip()


def _extract_json(text: str) -> dict:
    """从 LLM 输出中稳健提取 JSON 对象。"""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"输出中没有 JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


def analyze_news(llm_cfg: dict, news: dict, hints: list[dict] | None = None,
                 collect_date: str | None = None, retries: int = 3) -> dict:
    """对单条新闻调用 GLM，返回 ANALYSIS_SCHEMA 对应字段的 dict。"""
    user_prompt = build_user_prompt(news, hints)
    raw = None
    for attempt in range(1, retries + 1):
        try:
            raw = chat_completion(llm_cfg, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ])
            parsed = _extract_json(raw)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("[attempt %d/%d] 分析失败: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 * attempt)
    else:
        parsed = {
            "summary": "", "sentiment": "中性", "sentiment_score": 0,
            "impact_period": "短期", "bullish": [], "bearish": [], "risks": [],
            "watchlist": [], "follow_signals": [], "action": "", "key_points": [],
            "market_reaction": "", "conclusion": f"分析失败，LLM 原始输出: {raw}",
        }
        raw = None

    collect_date = collect_date or datetime.today().strftime("%Y-%m-%d")
    return {
        "collect_date": collect_date,
        "news_id": news.get("news_id", ""),
        "title": news.get("title", ""),
        "source": news.get("source", ""),
        "url": news.get("url", ""),
        "publish_time": news.get("publish_time"),
        "content_excerpt": (news.get("content") or "")[:200],
        "summary": parsed.get("summary", ""),
        "sentiment": parsed.get("sentiment", "中性"),
        "sentiment_score": int(parsed.get("sentiment_score", 0) or 0),
        "impact_period": parsed.get("impact_period", "短期"),
        "bullish": parsed.get("bullish", []) or [],
        "bearish": parsed.get("bearish", []) or [],
        "risks": parsed.get("risks", []) or [],
        "watchlist": parsed.get("watchlist", []) or [],
        "follow_signals": parsed.get("follow_signals", []) or [],
        "action": parsed.get("action", ""),
        "key_points": parsed.get("key_points", []) or [],
        "market_reaction": parsed.get("market_reaction", ""),
        "conclusion": parsed.get("conclusion", ""),
        "llm_model": llm_cfg["model"],
        "raw": raw,
        "stock_hints": [h.get("code") for h in (hints or []) if h.get("code")],
    }
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_news_analyze.py -q`
Expected: PASS — 7 passed

- [ ] **Step 5: Commit**

```bash
git add stock_selector/news/analyze.py tests/test_news_analyze.py
git commit -m "feat(news): prompt with stockList hints; analyze builds two-layer row"
```

---

### Task 4: watch_target 拆解合并

**Files:**
- Create: `stock_selector/news/targets.py`
- Test: `tests/test_news_targets.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| WT-01 | 设计§3.2 | normal | bullish 含 `{category:个股, code:"300207", score:8, reason:"..."}` | direction=bullish、code=300207、score=8 | unit | `::test_bullish_target` | `python -m pytest tests/test_news_targets.py -q` |
| WT-02 | 设计§3.2 | normal | bullish code 空、name=欣旺达，hints 含 name→300207 | code 回填 300207 | unit | `::test_code_backfill_by_name` | 同上 |
| WT-03 | 设计§3.2 | boundary | bullish code 空、hints 恰一个 code 候选 | 回填唯一候选 | unit | `::test_code_backfill_single_hint` | 同上 |
| WT-04 | 设计§3.2 | exception | bullish code 空、hints 多个候选且不匹配 name | code 保持空（不猜） | unit | `::test_code_not_guessed` | 同上 |
| WT-05 | 设计§3.2 | normal | watchlist 元素 priority=高 | direction=watch、score=0、priority 透传 | unit | `::test_watchlist_direction` | 同上 |
| WT-06 | 设计§3.2 | normal | risks 元素 | direction=risk、category=风险、name=desc[:20]、code=""、score 透传 | unit | `::test_risk_target` | 同上 |
| WT-07 | 设计§3.2 | normal | 两条新闻 bullish 同 code | 合并一行、source_news_ids=union、score 取第一条 | unit | `::test_merge_same_target` | 同上 |
| WT-08 | 设计§3.2 | boundary | 同 code 两条 name 不同 | 保留第一条非空 name，仍一行 | unit | `::test_merge_keeps_first_name` | 同上 |
| WT-09 | 设计§3.2 | exception | bullish 含字符串元素混 dict | 字符串跳过、dict 保留 | unit | `::test_skip_non_dict_elements` | 同上 |
| WT-10 | 设计§3.2 | boundary | rows 为空 | 返回空列表 | unit | `::test_empty_rows` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_targets.py`**

```python
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
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_news_targets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stock_selector.news.targets'`

- [x] **Step 3: 实现最小代码 `stock_selector/news/targets.py`**

```python
"""从 news_analysis 行拆解并当日合并 watch_target。"""

from __future__ import annotations

import logging

logger = logging.getLogger("news.targets")

RISK_SCORE_FIELDS = ("score",)


def _dict_items(row, key):
    """仅返回 dict 元素；非 dict 跳过并 WARNING。"""
    val = row.get(key, []) or []
    non_dict = [x for x in val if not isinstance(x, dict)]
    if non_dict:
        logger.warning("[watch_target] %s 含 %d 个非 dict 元素，已跳过", key, len(non_dict))
    return [x for x in val if isinstance(x, dict)]


def _backfill_code(item, hints):
    """用候选代码表回填空 code；无法确定保持空串（不猜）。"""
    if item.get("code"):
        return item["code"]
    name = item.get("name")
    if not name:
        return ""
    for h in hints or []:
        if h.get("name") == name and h.get("code"):
            return h["code"]
    pool = [h for h in (hints or []) if h.get("code")]
    if len(pool) == 1:
        return pool[0]["code"]
    return ""


def _to_target(item, direction, hints, collect_date, llm_model):
    return {
        "collect_date": collect_date,
        "code": _backfill_code(item, hints),
        "name": item.get("name", ""),
        "category": item.get("category", ""),
        "direction": direction,
        "score": int(item.get("score", 0) or 0),
        "priority": item.get("priority", "") if direction == "watch" else "",
        "reason": item.get("reason", ""),
        "source_news_ids": [],
        "llm_model": llm_model,
    }


def _risk_to_target(item, collect_date, llm_model):
    desc = item.get("desc", "") or ""
    return {
        "collect_date": collect_date,
        "code": "",
        "name": desc[:20],
        "category": "风险",
        "direction": "risk",
        "score": int(item.get("score", 0) or 0),
        "priority": "",
        "reason": desc,
        "source_news_ids": [],
        "llm_model": llm_model,
    }


def _merge_target(merged, target, news_id):
    key = f"{target['direction']}|{target['code'] or ('NAME:' + target['name'])}"
    old = merged.get(key)
    if old is None:
        if news_id:
            target["source_news_ids"] = [news_id]
        merged[key] = target
        return
    if news_id and news_id not in old["source_news_ids"]:
        old["source_news_ids"].append(news_id)
    for field in ("name", "priority", "reason", "code"):
        if not old.get(field) and target.get(field):
            old[field] = target[field]


def build_watch_targets(rows, hints_by_news, collect_date, llm_model):
    """把当日 news_analysis 行拆解合并为 watch_target 列表。

    hints_by_news: {news_id: [NormalizedHint, ...]} 用于 code 回填。
    """
    merged = {}
    for row in rows:
        news_id = row.get("news_id", "")
        hints = hints_by_news.get(news_id, []) if hints_by_news else []
        for it in _dict_items(row, "bullish"):
            _merge_target(merged, _to_target(it, "bullish", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "bearish"):
            _merge_target(merged, _to_target(it, "bearish", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "watchlist"):
            _merge_target(merged, _to_target(it, "watch", hints, collect_date, llm_model), news_id)
        for it in _dict_items(row, "risks"):
            _merge_target(merged, _risk_to_target(it, collect_date, llm_model), news_id)
    return list(merged.values())
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_news_targets.py -q`
Expected: PASS — 10 passed

- [ ] **Step 5: Commit**

```bash
git add stock_selector/news/targets.py tests/test_news_targets.py
git commit -m "feat(news): watch_target decomposition with code backfill and daily merge"
```

---

### Task 5: store 双落盘 + Mongo 双写入

**Files:**
- Create: `stock_selector/news/store.py`
- Test: `tests/test_news_store.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| ST-01 | 设计§4.1 | normal | 两次 `save_analysis_parquet` 同 news_id 不同行 | 文件行数不变、保留最新行 | unit | `::test_analysis_parquet_dedup` | `python -m pytest tests/test_news_store.py -q` |
| ST-02 | 设计§4.1 | boundary | df 缺列（如无 stock_hints） | 对齐 ANALYSIS_SCHEMA 后保存不报错 | unit | `::test_analysis_parquet_aligns_schema` | 同上 |
| ST-03 | 设计§4.1 | normal | `save_watch_parquet` 两次不同批次 | 覆盖，最终行数=最新批次 | unit | `::test_watch_parquet_overwrite` | 同上 |
| ST-04 | 设计§4.2 | normal | `save_analysis_mongo(db, df, "2026-09-07")` 两次 | `_id="2026-09-07|news_id"`；重写无残留 count 不变 | integration(skipif) | `::TestStoreMongo::test_analysis_mongo_rebuild` | 同上 |
| ST-05 | 设计§4.2 | normal | `save_watch_mongo(db, docs, "2026-09-07")` + 他日数据 | `_id` 为16hex；整日删除无他日残留 | integration(skipif) | `::TestStoreMongo::test_watch_mongo_day_rebuild` | 同上 |
| ST-06 | 设计§4.2 | normal | `create_indexes(db)` | 四索引存在（TICKS 为准） | integration(skipif) | `::TestStoreMongo::test_indexes` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_store.py`**

```python
import pandas as pd
import pytest

from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA
from stock_selector.news.store import (
    create_indexes, save_analysis_mongo, save_analysis_parquet,
    save_watch_mongo, save_watch_parquet,
)


def _analysis_row(news_id, **kw):
    row = {k: None for k in ANALYSIS_SCHEMA}
    row.update({"collect_date": "2026-09-07", "news_id": news_id,
                "title": "t", "publish_time": "2026-09-07 07:00:00"})
    row.update(kw)
    return row


def test_analysis_parquet_dedup(tmp_path):
    df1 = pd.DataFrame([_analysis_row("n1", summary="v1")])
    df2 = pd.DataFrame([_analysis_row("n1", summary="v2")])
    path1 = save_analysis_parquet(df1, "2026-09-07", tmp_path)
    save_analysis_parquet(df2, "2026-09-07", tmp_path)
    out = pd.read_parquet(path1)
    assert len(out) == 1
    assert out.iloc[0]["summary"] == "v2"


def test_analysis_parquet_aligns_schema(tmp_path):
    df = pd.DataFrame([{"collect_date": "2026-09-07", "news_id": "n1"}])
    path = save_analysis_parquet(df, "2026-09-07", tmp_path)
    assert list(pd.read_parquet(path).columns) == ANALYSIS_SCHEMA


def test_watch_parquet_overwrite(tmp_path):
    d1 = pd.DataFrame([{**{k: None for k in WATCH_SCHEMA},
                        "collect_date": "2026-09-07", "code": "BK0464",
                        "direction": "bullish", "source_news_ids": ["n1"]}])
    d2 = pd.DataFrame([{**{k: None for k in WATCH_SCHEMA},
                        "collect_date": "2026-09-07", "code": "BK0464",
                        "direction": "bullish", "source_news_ids": ["n1", "n2"]}])
    path = save_watch_parquet(d1, "2026-09-07", tmp_path)
    save_watch_parquet(d2, "2026-09-07", tmp_path)
    out = pd.read_parquet(path)
    assert len(out) == 1 and out.iloc[0]["source_news_ids"] == ["n1", "n2"]


def _mongo_available():
    try:
        import pymongo
        c = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mongo_available(), reason="local mongo unavailable")


class TestStoreMongo:
    def setup_method(self):
        import pymongo
        client = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        self.db = client["eastmoney_news"]
        self.client = client
        self.db["news_analysis"].delete_many({"collect_date": "2026-09-07"})
        self.db["watch_target"].delete_many({"collect_date": "2026-09-07"})

    def teardown_method(self):
        self.client.close()

    def test_analysis_mongo_rebuild(self):
        df = pd.DataFrame([_analysis_row("n1", summary="v1")])
        save_analysis_mongo(self.db, df, "2026-09-07")
        assert self.db["news_analysis"].find_one({"_id": "2026-09-07|n1"})["summary"] == "v1"
        df2 = pd.DataFrame([_analysis_row("n1", summary="v2")])
        save_analysis_mongo(self.db, df2, "2026-09-07")  # 重写
        assert self.db["news_analysis"].count_documents({"collect_date": "2026-09-07"}) == 1
        assert self.db["news_analysis"].find_one({"_id": "2026-09-07|n1"})["summary"] == "v2"

    def test_watch_mongo_day_rebuild(self):
        docs = [{"collect_date": "2026-09-07", "code": "BK0464", "name": "",
                 "category": "板块", "direction": "bullish", "score": 8,
                 "priority": "", "reason": "r", "source_news_ids": ["n1"],
                 "llm_model": "m"}]
        self.db["watch_target"].insert_one({"collect_date": "2026-09-06",
                                            "code": "OLD", "name": "", "category": "板块",
                                            "direction": "bullish", "score": 1,
                                            "priority": "", "reason": "",
                                            "source_news_ids": ["old1"], "llm_model": "m"})
        save_watch_mongo(self.db, docs, "2026-09-07")
        assert self.db["watch_target"].count_documents({"collect_date": "2026-09-07"}) == 1
        doc = self.db["watch_target"].find_one({"collect_date": "2026-09-07"})
        assert len(doc["_id"]) == 16 and doc["_id"] == doc["_id"].lower()
        assert self.db["watch_target"].count_documents({"collect_date": "2026-09-06"}) == 1

    def test_indexes(self):
        create_indexes(self.db)
        news_indexes = [i["name"] for i in self.db["news_analysis"].list_indexes()]
        watch_indexes = [i["name"] for i in self.db["watch_target"].list_indexes()]
        assert "collect_date_1" in news_indexes and "news_id_1" in news_indexes
        assert "code_1" in watch_indexes and "collect_date_1_code_1" in watch_indexes
```

- [x] **Step 2: 运行测试确认失败（含 skip 判断）**

Run: `python -m pytest tests/test_news_store.py -q`
Expected: FAIL/ERR — `ModuleNotFoundError: No module named 'stock_selector.news.store'`，或 mongo 不可达时 mongo 用例 skipped

- [x] **Step 3: 实现最小代码 `stock_selector/news/store.py`**

```python
"""两层结果的 parquet / Mongo 持久化（parquet 为准，Mongo 为查询副本）。"""

from __future__ import annotations

import logging
from hashlib import md5
from pathlib import Path

import pandas as pd

from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA

logger = logging.getLogger("news.store")


def _align(df, schema):
    df = df.copy()
    for col in schema:
        if col not in df.columns:
            df[col] = None
    return df[schema]


def save_analysis_parquet(df, collect_date, output_dir: str | Path) -> Path:
    """news_analysis 增量合并（按 news_id 去重保留最新），zstd 落盘。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{collect_date}.parquet"
    df = _align(df, ANALYSIS_SCHEMA)
    if path.exists():
        old = _align(pd.read_parquet(path), ANALYSIS_SCHEMA)
        df = pd.concat([old, df], ignore_index=True).drop_duplicates("news_id", keep="last")
    df = df.sort_values("publish_time", na_position="last").reset_index(drop=True)
    df.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 条)", path, len(df))
    return path


def save_watch_parquet(df, collect_date, watch_dir: str | Path) -> Path:
    """watch_target 当日整文件覆盖，zstd 落盘。"""
    watch_dir = Path(watch_dir)
    watch_dir.mkdir(parents=True, exist_ok=True)
    path = watch_dir / f"{collect_date}.parquet"
    df = _align(df, WATCH_SCHEMA)
    df.to_parquet(path, index=False, compression="zstd")
    logger.info("已写入 %s (%d 条)", path, len(df))
    return path


def save_analysis_mongo(db, df, collect_date) -> int:
    col = db["news_analysis"]
    ids = []
    docs = []
    for _, r in df.iterrows():
        doc = r.to_dict()
        if doc.get("publish_time") is None or pd.isna(doc.get("publish_time")):
            doc["publish_time"] = ""
        else:
            doc["publish_time"] = str(doc["publish_time"])
        doc["_id"] = f"{collect_date}|{doc['news_id']}"
        docs.append(doc)
        ids.append(doc["_id"])
    if docs:
        col.delete_many({"_id": {"$in": ids}})
        col.insert_many(docs)
    logger.info("news_analysis 今日整批重建 %d 条", len(docs))
    return len(docs)


def save_watch_mongo(db, docs, collect_date) -> int:
    """watch_target 整日 delete + insert。"""
    col = db["watch_target"]
    out = []
    for d in docs:
        doc = dict(d)
        doc["source_news_ids"] = list(doc.get("source_news_ids") or [])
        key = f"{collect_date}|{doc['direction']}|{doc['code'] or doc['name']}"
        doc["_id"] = md5(key.encode("utf-8")).hexdigest()[:16]
        out.append(doc)
    if out:
        col.delete_many({"collect_date": collect_date})
        col.insert_many(out)
    logger.info("watch_target 整日重建 %d 条", len(out))
    return len(out)


def create_indexes(db) -> None:
    db["news_analysis"].create_index([("collect_date", 1)])
    db["news_analysis"].create_index([("news_id", 1)])
    db["watch_target"].create_index([("collect_date", 1), ("direction", 1)])
    db["watch_target"].create_index([("code", 1)])
    db["watch_target"].create_index([("category", 1)])
    db["watch_target"].create_index([("collect_date", 1), ("code", 1)])
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_news_store.py -q`
Expected: parquet 用例 PASS；mongo 用例 PASS 或 SKIP(若 mongo 不可达)

- [ ] **Step 5: Commit**

```bash
git add stock_selector/news/store.py tests/test_news_store.py
git commit -m "feat(news): two-layer parquet and mongo persistence with day rebuild"
```

---

### Task 6: stats 跨天统计

**Files:**
- Create: `stock_selector/news/stats.py`
- Test: `tests/test_news_stats.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| STT-01 | 设计§8.1 | normal | 三天 watch parquet，`load_watch_range(dir, start, end)` | 仅含 `[start,end)` 的文件 | unit | `tests/test_news_stats.py::test_load_watch_range` | `python -m pytest tests/test_news_stats.py -q` |
| STT-02 | 设计§8.1 | normal | 含 bullish/bearish/watch 混合 + source_news_ids | `rank_heat` 默认 bullish，按 code 聚合 news_count 去重、total_score=sum | unit | `::test_rank_heat_aggregation` | 同上 |
| STT-03 | 设计§8.1 | normal | `rank_heat(category="板块")` | 仅 category=板块 | unit | `::test_rank_heat_category_filter` | 同上 |
| STT-04 | 设计§8.1 | boundary | `rank_heat(top_n=1)` | 只返回分数最高 1 行 | unit | `::test_rank_heat_topn` | 同上 |
| STT-05 | 设计§8.2 | normal | 某 code 跨两日 score=8、-3 | trend 逐日 sum + cum=[8,5] | unit | `::test_trend_cumsum` | 同上 |
| STT-06 | 设计§8.1/8.2 | exception | 空 df / 无匹配 code | 返回空 DataFrame 不抛错 | unit | `::test_empty_input` | 同上 |
| STT-07 | 设计§8.1/8.2 | contract | 本地 mongo seed 当日 watch_target，跑 rank_heat_mongo/trend_mongo | pipeline 语法正确、结果同 parquet 等价口径 | integration(skipif) | `::TestStatsMongo::test_rank_heat_mongo` | 同上 |

- [x] **Step 1: 写失败测试 `tests/test_news_stats.py`**

```python
import pandas as pd
import pytest

from stock_selector.news.schema import WATCH_SCHEMA
from stock_selector.news.stats import (
    load_watch_range, rank_heat, rank_heat_mongo, trend, trend_mongo,
)


def _watch(date, code, direction, score, names=("n1",)):
    return {k: None for k in WATCH_SCHEMA} | {
        "collect_date": date, "code": code, "name": code, "category": "板块",
        "direction": direction, "score": score, "source_news_ids": list(names),
    }


def _save_watch(tmp_path, date, rows):
    df = pd.DataFrame(rows)
    df.to_parquet(tmp_path / f"{date}.parquet", index=False, compression="zstd")


def test_load_watch_range(tmp_path):
    _save_watch(tmp_path, "2026-09-07", [_watch("2026-09-07", "BK0464", "bullish", 8)])
    _save_watch(tmp_path, "2026-09-06", [_watch("2026-09-06", "BK0464", "bullish", 3)])
    _save_watch(tmp_path, "2026-09-05", [_watch("2026-09-05", "BK0464", "bullish", 1)])
    df = load_watch_range(tmp_path, "2026-09-06", "2026-09-08")
    assert set(df["collect_date"]) == {"2026-09-06", "2026-09-07"}


def test_rank_heat_aggregation():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8, ("n1", "n2")),
        _watch("2026-09-02", "BK0464", "bullish", 3, ("n2",)),
        _watch("2026-09-02", "BK0464", "bearish", -5, ("n3",)),
        _watch("2026-09-02", "300207", "bullish", 6, ("n3",)),
    ])
    out = rank_heat(df)
    row = out.set_index("code").loc["BK0464"]
    assert row["news_count"] == 3  # {n1,n2} ∪ {n2} = 3
    assert row["total_score"] == 11  # 8 + 3


def test_rank_heat_category_filter():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8),
        _watch("2026-09-01", "300207", "bullish", 6),
    ])
    out = rank_heat(df, category="板块")
    assert set(out["code"]) == {"BK0464"}


def test_rank_heat_topn():
    df = pd.DataFrame([
        _watch("2026-09-01", "A", "bullish", 1),
        _watch("2026-09-01", "B", "bullish", 9),
    ])
    out = rank_heat(df, top_n=1)
    assert list(out["code"]) == ["B"]


def test_trend_cumsum():
    df = pd.DataFrame([
        _watch("2026-09-01", "BK0464", "bullish", 8),
        _watch("2026-09-02", "BK0464", "bearish", -3),
    ])
    out = trend(df, "BK0464")
    assert out["score"].tolist() == [8, -3]
    assert out["cum"].tolist() == [8, 5]


def test_empty_input():
    empty = pd.DataFrame(columns=WATCH_SCHEMA)
    assert rank_heat(empty).empty
    assert trend(empty, "BK0464").empty
    assert load_watch_range("/nonexistent", "2026-01-01", "2026-12-31").empty


def _mongo_available():
    try:
        import pymongo
        c = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


class TestStatsMongo:
    @pytest.fixture(autouse=True)
    def _db(self):
        if not _mongo_available():
            pytest.skip("local mongo unavailable")
        import pymongo
        client = pymongo.MongoClient("127.0.0.1", 27017, serverSelectionTimeoutMS=1500)
        db = client["eastmoney_news"]
        db["watch_target"].delete_many({"collect_date": {"$gte": "2026-09-01",
                                                         "$lt": "2026-09-08"}})
        docs = [_watch("2026-09-01", "BK0464", "bullish", 8, ("n1",)),
                _watch("2026-09-02", "BK0464", "bullish", 3, ("n2",))]
        for d in docs:
            db["watch_target"].insert_one(d)
        yield db["watch_target"]
        db["watch_target"].delete_many({"collect_date": {"$gte": "2026-09-01",
                                                         "$lt": "2026-09-08"}})
        client.close()

    def test_rank_heat_mongo(self, _db):
        rows = rank_heat_mongo(_db, "2026-09-01", "2026-09-08", top_n=10)
        assert rows and rows[0]["_id"] == "BK0464"
        assert rows[0]["total_score"] == 11 and rows[0]["news_count"] == 2

    def test_trend_mongo(self, _db):
        rows = trend_mongo(_db, "BK0464", "2026-09-01", "2026-09-08")
        assert rows[-1]["cum"] == 11
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_news_stats.py -q`
Expected: FAIL/SKIP — `ModuleNotFoundError: No module named 'stock_selector.news.stats'`

- [x] **Step 3: 实现最小代码 `stock_selector/news/stats.py`**

```python
"""跨天统计：热度排行 + 个股/板块趋势（方案A 惰性聚合）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from stock_selector.news.schema import WATCH_SCHEMA


def load_watch_range(watch_dir: str | Path, start: str, end: str) -> pd.DataFrame:
    """近 N 天 watch parquet 惰性 concat（文件名 YYYY-MM-DD，按 [start, end) 过滤）。"""
    frames = []
    for p in sorted(Path(watch_dir).glob("*.parquet")):
        d = p.stem
        if start <= d < end:
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame(columns=WATCH_SCHEMA)
    return pd.concat(frames, ignore_index=True)


def rank_heat(df, category=None, direction="bullish", top_n=30) -> pd.DataFrame:
    """热度排行：按 code 聚合，news_count=source_news_ids 去重总数，total_score=sum。"""
    cond = df["direction"] == direction
    if category:
        cond &= df["category"] == category
    part = df[cond]
    if part.empty:
        return pd.DataFrame(columns=["code", "name", "news_count", "total_score"])
    agg = part.groupby("code").agg(
        name=("name", "first"),
        news_count=("source_news_ids", lambda s: len(set().union(*s))),
        total_score=("score", "sum"),
    ).sort_values("total_score", ascending=False).head(top_n)
    return agg.reset_index()


def trend(df, code) -> pd.DataFrame:
    """个股/板块趋势：逐日 score sum + cumsum。"""
    part = df[df["code"] == code]
    if part.empty:
        return pd.DataFrame(columns=["collect_date", "score", "cum"])
    out = part.groupby("collect_date")["score"].sum().sort_index().reset_index()
    out["cum"] = out["score"].cumsum()
    return out


def rank_heat_mongo(col, start, end, category=None, direction="bullish", top_n=30) -> list:
    """Mongo 端热度排行。"""
    match = {"collect_date": {"$gte": start, "$lt": end}, "direction": direction}
    if category:
        match["category"] = category
    return list(col.aggregate([
        {"$match": match},
        {"$group": {"_id": "$code",
                    "name": {"$first": "$name"},
                    "news_count": {"$sum": {"$size": "$source_news_ids"}},
                    "total_score": {"$sum": "$score"}}},
        {"$sort": {"total_score": -1}},
        {"$limit": top_n},
    ]))


def trend_mongo(col, code, start, end) -> list:
    """Mongo 端逐日净分 + 应用端 cumsum。"""
    rows = list(col.aggregate([
        {"$match": {"code": code, "collect_date": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$collect_date", "score": {"$sum": "$score"}}},
        {"$sort": {"_id": 1}},
    ]))
    cum = 0
    out = []
    for r in rows:
        cum += r["score"]
        out.append({"collect_date": r["_id"], "score": r["score"], "cum": cum})
    return out
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_news_stats.py -q`
Expected: 非 mongo 用例 PASS；mongo 用例 PASS 或 SKIP

- [ ] **Step 5: Commit**

```bash
git add stock_selector/news/stats.py tests/test_news_stats.py
git commit -m "feat(news): cross-day heat ranking and trend queries (lazy aggregation)"
```

---

### Task 7: 重写 analyze_news_llm.py CLI

**Files:**
- Modify: `scripts/analyze_news_llm.py`（整文件重写）
- Test: `tests/test_news_stats.py`（已覆盖 stats；CLI 冒烟见 Step 5）

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| CLI-01 | 设计§6/§9 | E2E | `python scripts/analyze_news_llm.py --demo --no-mongo --limit 1` | 退出码 0；`data/news_analysis/{today}.parquet` 与 `data/news_watch/{today}.parquet` 生成；控制台含"今日关注"段 | E2E(manual script) | CLI 冒烟 | Step 5 命令 |
| CLI-02 | 设计§6 | normal | `--demo --no-mongo` 两次 | news_analysis 按 news_id 去重不增长；watch 整日覆盖 | E2E(manual) | 幂等重跑 | Step 5 |
| CLI-03 | 设计§8.3 | E2E | `--stats heat --no-mongo --start 2026-01-01 --end 2099-01-01` | 无数据返回空表不崩；退出码 0 | E2E(manual) | 空表冒烟 | Step 5 |
| CLI-04 | 设计§8.3 | exception | `--stats trend` 无 `--code` | 参数校验报错退出非 0 | E2E(manual) | 参数校验 | Step 5 |

- [x] **Step 1: 重写 `scripts/analyze_news_llm.py`**（全文见下，遵循旧脚本模块 docstring、ROOT/CONFIG_PATH/OUTPUT_DIR/LOG_DIR 常量与 setup_logging）

```python
"""用 LLM(智谱 GLM) 解读 MongoDB 中的新闻，落盘两层结构（parquet + MongoDB）。

两层结构（见 docs/superpowers/specs/2026-09-07-news-llm-analysis-two-layer.md）：
    第一层 news_analysis: data/news_analysis/YYYY-MM-DD.parquet 每条新闻一条
    第二层 watch_target:  data/news_watch/YYYY-MM-DD.parquet      每个"标的+方向"一条
    MongoDB: eastmoney_news 库 news_analysis / watch_target 集合（parquet 为准）

用法:
    python scripts/analyze_news_llm.py                        # 真实读 Mongo，最近1天
    python scripts/analyze_news_llm.py --days 3               # 最近3天（自然日）
    python scripts/analyze_news_llm.py --limit 10             # 最多10条
    python scripts/analyze_news_llm.py --demo                 # 内置示例新闻
    python scripts/analyze_news_llm.py --no-mongo             # 只写 parquet
    python scripts/analyze_news_llm.py --stats heat --start 2026-09-01 --end 2026-09-08 --category 板块 --top 20
    python scripts/analyze_news_llm.py --stats trend --code BK0464 --start 2026-09-01 --end 2026-09-08
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from stock_selector.news.analyze import analyze_news
from stock_selector.news.fetch import fetch_news
from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA
from stock_selector.news.stats import (
    load_watch_range, rank_heat, rank_heat_mongo, trend, trend_mongo,
)
from stock_selector.news.store import (
    create_indexes, save_analysis_mongo, save_analysis_parquet,
    save_watch_mongo, save_watch_parquet,
)
from stock_selector.news.targets import build_watch_targets

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"
OUTPUT_DIR = ROOT / "data" / "news_analysis"
WATCH_DIR = ROOT / "data" / "news_watch"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("analyze_news_llm")

DEMO_NEWS = [{
    "_id": "demo-001",
    "title": "理想汽车26.5亿元入股欣旺达动力 投后估值301亿",
    "summary": "9月4日晚间，欣旺达公告称，子公司欣旺达动力拟进行C++轮增资扩股，引入北京理想汽车有限公司。理想汽车拟出资26.5亿元，认购欣旺达动力约14亿元新增注册资本，对应8.79%股份，投后估值301.45亿元。",
    "showTime": "2026-09-06 07:08:00",
    "stockList": ["90.BK0464", "0.300207"],
}]


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if "llm" not in cfg:
        raise SystemExit(f"配置缺少 llm 节，请先在 {CONFIG_PATH} 添加")
    return cfg


def connect_mongo(mongo_cfg: dict, fallback_col=None):
    """连接 Mongo；失败返回 fallback_col（demo 场景为 None）。"""
    try:
        from pymongo import MongoClient
    except ImportError:
        logger.warning("[mongo] pymongo 未安装")
        return None
    try:
        client = MongoClient(host=mongo_cfg.get("host", "127.0.0.1"),
                             port=int(mongo_cfg.get("port", 27017)),
                             serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        return client[mongo_cfg["database"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mongo] 连接失败(%s)，降级", exc)
        return None


def run_analysis(args, cfg):
    llm_cfg = cfg.get("llm", {})
    mongo_cfg = cfg.get("mongodb", {})
    collect_date = datetime.today().strftime("%Y-%m-%d")

    if args.demo:
        news_list = [{"news_id": n["_id"], "title": n["title"], "content": n.get("summary", ""),
                      "source": "demo", "url": "", "publish_time": n.get("showTime", ""),
                      "hints": []} for n in DEMO_NEWS]
    else:
        db = connect_mongo(mongo_cfg)
        news_list = []
        if db is not None:
            try:
                news_list = fetch_news(db[mongo_cfg.get("collection", "news")],
                                       days=args.days, limit=args.limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mongo] 读取失败(%s)，改用 --demo 演示", exc)
                news_list = []
        if not news_list:
            logger.warning("MongoDB 无新闻，改用 --demo 演示...")
            news_list = [{"news_id": n["_id"], "title": n["title"], "content": n.get("summary", ""),
                          "source": "demo", "url": "", "publish_time": n.get("showTime", ""),
                          "hints": []} for n in DEMO_NEWS]

    logger.info("开始分析 %d 条新闻，model=%s", len(news_list), llm_cfg.get("model"))
    rows = []
    hints_by_news = {}
    for i, news in enumerate(news_list, 1):
        logger.info("[%d/%d] 分析: %s", i, len(news_list), news.get("title", "")[:40])
        hints_by_news[news["news_id"]] = news.get("hints", [])
        rows.append(analyze_news(llm_cfg, news, hints=news.get("hints", []),
                                 collect_date=collect_date))

    df = pd.DataFrame(rows, columns=ANALYSIS_SCHEMA)
    save_analysis_parquet(df, collect_date, OUTPUT_DIR)

    watch_docs = build_watch_targets(rows, hints_by_news, collect_date, llm_cfg.get("model", ""))
    watch_df = pd.DataFrame(watch_docs, columns=WATCH_SCHEMA)
    save_watch_parquet(watch_df, collect_date, WATCH_DIR)

    if not args.no_mongo:
        db = connect_mongo(mongo_cfg)
        if db is not None:
            try:
                create_indexes(db)
                save_analysis_mongo(db, df, collect_date)
                save_watch_mongo(db, watch_docs, collect_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mongo] 写入失败(%s)，parquet 仍为准", exc)

    print_summary(df)
    print_watch_summary(watch_docs)


def print_summary(df):
    if df.empty:
        return
    print("\n=== 分析摘要 ===")
    for _, r in df.iterrows():
        score = int(r["sentiment_score"])
        mark = "+" if score > 0 else ""
        print(f"[{r['sentiment']}/{'%+d' % score}] {r['summary']}  ({str(r['title'])[:30]})")
        for label, key in (("利好", "bullish"), ("利空", "bearish"), ("关注", "watchlist")):
            items = [x for x in (r.get(key) or []) if x]
            if items:
                print(f"  {label}: " + "、".join(
                    f"{x.get('name', '')}({x.get('score', 0):+d})"
                    if label != "关注" else f"{x.get('name', '')}({x.get('priority', '')})"
                    for x in items))
        for x in (r.get("risks") or []):
            if x:
                print(f"  风险: {x.get('desc', '')}")


def print_watch_summary(watch_docs):
    print("\n=== 今日关注 ===")
    if not watch_docs:
        print("(无)")
        return
    for t in watch_docs:
        ident = t["code"] or t["name"]
        if t["direction"] == "risk":
            print(f"[{t['category']}] {t['name']} ({t['score']:+d})")
        elif t["direction"] == "watch":
            print(f"[{t['category']}] {ident} {t['name']} ({t['direction']} {t['score']:+d}, {t['priority']}, {len(t['source_news_ids'])}条新闻)")
        else:
            print(f"[{t['category']}] {ident} {t['name']} ({t['direction']} {t['score']:+d}, {len(t['source_news_ids'])}条新闻)")


def run_stats(args, cfg):
    start = args.start or (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    end = args.end or (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    mongo_cfg = cfg.get("mongodb", {})
    if not args.no_mongo:
        db = connect_mongo(mongo_cfg)
        if db is not None:
            col = db["watch_target"]
            if args.stats == "heat":
                rows = rank_heat_mongo(col, start, end, category=args.category,
                                       direction=args.direction or "bullish", top_n=args.top or 30)
                print("\n=== 热度排行 ===")
                for r in rows:
                    print(f"{r['_id']} {r.get('name', '')} news={r['news_count']} score={r['total_score']}")
                return
            if args.stats == "trend":
                rows = trend_mongo(col, args.code, start, end)
                print("\n=== 趋势 ===")
                for r in rows:
                    print(f"{r['collect_date']} score={r['score']:+d} cum={r['cum']:+d}")
                return
    df = load_watch_range(WATCH_DIR, start, end)
    if args.stats == "heat":
        out = rank_heat(df, category=args.category, direction=args.direction or "bullish",
                        top_n=args.top or 30)
        print("\n=== 热度排行(parquet) ===")
        for _, r in out.iterrows():
            print(f"{r['code']} {r['name']} news={r['news_count']} score={r['total_score']}")
    elif args.stats == "trend":
        out = trend(df, args.code)
        print("\n=== 趋势(parquet) ===")
        for _, r in out.iterrows():
            print(f"{r['collect_date']} score={r['score']:+d} cum={r['cum']:+d}")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(LOG_DIR / f"news_analyze_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 解读新闻，落盘两层结构并支持跨天统计")
    parser.add_argument("--days", type=int, default=1, help="读取最近 N 天新闻（0=不管时间）")
    parser.add_argument("--limit", type=int, default=50, help="最多分析条数")
    parser.add_argument("--demo", action="store_true", help="用内置示例新闻演示")
    parser.add_argument("--no-mongo", action="store_true", help="只写 parquet，不写 Mongo")
    parser.add_argument("--stats", choices=["heat", "trend"], help="跨天统计（与分析互斥）")
    parser.add_argument("--start", default=None, help="统计起始 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="统计结束 YYYY-MM-DD")
    parser.add_argument("--category", default=None, help="统计 category 过滤（heat）")
    parser.add_argument("--direction", default=None, help="统计 direction 过滤（heat），默认 bullish")
    parser.add_argument("--top", type=int, default=30, help="热度排行条数")
    parser.add_argument("--code", default=None, help="趋势标的 code（trend）")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    if args.stats:
        if args.stats == "trend" and not args.code:
            parser.error("--stats trend 需要 --code")
        run_stats(args, cfg)
        return

    run_analysis(args, cfg)


if __name__ == "__main__":
    main()
```

- [x] **Step 2: 语法检查与 demo 冒烟**

Run: `python -c "import ast; ast.parse(open(r'scripts/analyze_news_llm.py', encoding='utf-8').read()); print('syntax ok')"`
Expected: `syntax ok`

- [x] **Step 3: 单元测试全量回归**

Run: `python -m pytest tests/test_news_*.py -q`
Expected: 全部 PASS（mongo 用例视本机情况 PASS/SKIP）

- [x] **Step 4: CLI 冒烟（--demo --no-mongo）**

Run: `python scripts/analyze_news_llm.py --demo --no-mongo --limit 1`
Expected: 退出码 0；出现"=== 今日关注 ==="；`data/news_watch/{today}.parquet` 生成；LLM 若 429 限流则降级中性行仍可落盘

- [x] **Step 5: CLI 幂等 + 统计分支冒烟**

```bash
python scripts/analyze_news_llm.py --demo --no-mongo --limit 1
python scripts/analyze_news_llm.py --stats heat --no-mongo --start 2026-01-01 --end 2099-01-01
python scripts/analyze_news_llm.py --stats trend --no-mongo --code BK0464 --start 2026-01-01 --end 2099-01-01
python scripts/analyze_news_llm.py --stats trend --code BK0464 --start 2026-01-01  # 缺 --end，应正常
```
Expected: news_analysis 幂等不增长；heat/trend 空表不崩；`--stats trend` 缺 `--code` 的校验由 Step 6 验证

- [x] **Step 6: 参数校验验证**

Run: `python scripts/analyze_news_llm.py --stats trend --start 2026-01-01`
Expected: 退出码非 0，报错 `--stats trend 需要 --code`

- [ ] **Step 7: Commit**

```bash
git add scripts/analyze_news_llm.py
git commit -m "refactor(news): thin CLI with two-layer save, daily focus summary and stats"
```

---

### Task 8: 端到端验证

**Files:** 无代码改动（验证作业）

- [x] **Step 1: 全量 pytest 回归**

Run: `python -m pytest tests/ -q`
Expected: 全绿（含既有 backtest 测试）；若 mongo 本机可达则 mongo 用例也 PASS

- [x] **Step 2: 真实 mongo 端到端（本机已启动 mongo 时）**

Run: `python scripts/analyze_news_llm.py --limit 1 --days 5 --no-mongo`
Expected: 退出码 0；两级 parquet 生成；控制台含摘要+今日关注

- [x] **Step 3: mongo 统计分支（若 watch_target 有数据）**

Run: `python scripts/analyze_news_llm.py --stats heat --start 2026-01-01`
Preview: 显示热度排行或空表不崩

- [x] **Step 4: 核对容错矩阵（设计§9）**

| 场景 | 预期 | 验证方式 |
|---|---|---|
| Mongo 读失败/空 | fallback demo，不崩 | 停 mongo 后 `--days 5` |
| 单条分析失败 | retries 3 + 降级中性行 | 临时 mock 抛错（开发期验证过 AN-06） |
| Mongo 写失败 | WARNING，parquet 正常 | `--no-mongo` 等价验证 |
| 非 dict 元素 | 跳过 + WARNING | WT-09 已覆盖 |
| LLM 429 | backoff + 降级 | 观察日志 / 已有降级逻辑 |

- [x] **Step 5: 全部完成后在计划文件勾选 MA-1..MA-6**

---

## 5. 容错核对表（对设计§9）

- [x] Mongo 读失败/空 → fallback demo
- [x] 单条分析失败 → retries 3 + 降级中性行（不产 watch_target — 中性行 bullish/bearish/watchlist/risks 均为空）
- [x] Mongo 写失败 → WARNING，parquet 不受影响（save 先 parquet 后 mongo）
- [x] `--no-mongo` → 跳过 Mongo 写（stats 分支等价退 parquet）
- [x] 非 dict 元素 → skip + WARNING
- [x] LLM 429/超时 → backoff 重试 + 降级，当日文件仍落盘

## 6. 风险与待确认

- secid 前缀 `116/999/150` 含义未确认 → normalize 返回未知、不进候选代码表（设计 §2.1 明示处理方式）
- `stock_hints` 列追加在 ANALYSIS_SCHEMA 末尾，兼容既有 parquet（align 自动补列）
- LLM 免费档限流（code 1302）→ CI 只跑 `--demo --no-mongo`，真实 LLM 调用手工验证
- `sort_values("publish_time")`：publish_time 为 `YYYY-MM-DD HH:MM:SS` 字符串，字典序即可排序
- 旧 `data/news_analysis/2026-09-07.parquet` 为 demo 遗留（llm_model=GLM-4.7-Flash），重跑后按 news_id 合并
- 参照 design §7 控制台格式输出"今日关注"（risk 无 code 显示 name）
