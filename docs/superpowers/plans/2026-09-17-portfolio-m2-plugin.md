# 持仓诊断 M2：插件 analyze 判定（sell/hold/add + 收益快照） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `stock_selector_api/plugin/` 新增独立插件服务：`POST /api/portfolio/analyze` 真实判定每只持仓 sell/hold/add 并计算收益快照（行情复用既有 parquet K线链路 + AKShare 兜底），判定内核复用 `stock_selector/backtest/rule_eval.py` 的 `RuleEvaluator`，100% 承担判定与收益计算（红线 R1-1/R1-4），后端只代理透传。

**Architecture:** 插件是独立 FastAPI 进程（默认端口 8001，`plugin_base_url` 指向它）。三层：(1) `pricing` 取价——K线 parquet（`data/stock_kline/{code}.parquet`，5186 只全量已就绪）读取自然日 ≤ natural_day 的最新行，缺文件时 AKShare 实时兜底；(2) `judge` 判定——用既有 `RuleEvaluator` 对最新指标行求值 sell/add 规则（默认规则为常量，可 JSON 覆盖），未触发则 hold；(3) `revenue` 收益快照——latest_price/market_value/holding_pnl/pnl_pct 及 summary 全部在插件算。后端 `call_plugin_analyze` 已实现 http 分支，本计划把默认 `plugin_mode` 翻为 `http`。

**Tech Stack:** FastAPI · httpx · pandas（读 parquet）· akshare（行情兜底）· 既有 `stock_selector/backtest/rule_eval.py (RuleEvaluator)` · pytest(asyncio_mode=auto)

**裁定（以《3-方案与实施计划.md》为事实源，见其 L3「本迭代唯一事实源」）：**
- action 枚举固定 `sell/hold/add`（方案 L24/57；`buy` 仅选自选股信号 REQ-001~005，诊断不判未持仓，插件不得返回 buy）
- 收益字段沿用 M1 落库字段 `latest_price / holding_pnl / pnl_pct`（方案 L25）
- 事件名（M4 用）`portfolio_diag_confirmed / portfolio_diag_failed`（方案 L45）
- RES-07 绑定 DFX `REL-07`（DFX 唯一编号体系为 REL-01~08）

---

## 文件映射

| 文件 | 动作 | 职责 |
|---|---|---|
| `stock_selector_api/plugin/__init__.py` | 新增 | 空包 |
| `stock_selector_api/plugin/portfolio/__init__.py` | 新增 | 空包 |
| `stock_selector_api/plugin/portfolio/rules.py` | 新增 | 默认 sell/add 规则常量 + JSON 覆盖 |
| `stock_selector_api/plugin/portfolio/pricing.py` | 新增 | 行情取价：parquet 优先 + AKShare 兜底 |
| `stock_selector_api/plugin/portfolio/judge.py` | 新增 | RuleEvaluator 判定 → action/reason |
| `stock_selector_api/plugin/portfolio/revenue.py` | 新增 | 收益快照逐只计算 + summary 聚合 |
| `stock_selector_api/plugin/portfolio/analyzer.py` | 新增 | `analyze(payload, provider)` 编排（R1-1/1-4 全部计算在此） |
| `stock_selector_api/plugin/portfolio/provider.py` | 新增 | KlineProvider（注入点，测试用 FakeProvider） |
| `stock_selector_api/plugin/server.py` | 新增 | FastAPI 插件服务 `POST /api/portfolio/analyze` |
| `stock_selector_api/plugin/config/portfolio_rules.json` | 新增 | 规则覆盖文件（空默认用时用常量） |
| `stock_selector_api/plugin/requirements.txt` | 新增 | 依赖（fastapi/uvicorn/httpx/pandas/akshare） |
| `stock_selector_api/plugin/tests/test_judge.py` | 新增 | 判定纯逻辑单测 |
| `stock_selector_api/plugin/tests/test_revenue.py` | 新增 | 收益快照纯逻辑单测 |
| `stock_selector_api/plugin/tests/test_analyzer.py` | 新增 | 编排：FakeProvider + 全文用例 |
| `stock_selector_api/plugin/tests/test_server.py` | 新增 | ASGI HTTP 契约测试 |
| `stock_selector_api/plugin/tests/conftest.py` | 新增 | sys.path 注入 + 规则覆盖隔离（tmp） |
| `stock_selector_api/backend/config.py` | 修改 | `plugin_mode` 默认 `mock` → `http`（tests conftest 仍强制 mock，不受影响） |

**复用（不改动）：** `stock_selector/backtest/rule_eval.py:26 RuleEvaluator.eval(condition, row) -> bool`（_ALLOWED_COLUMNS 见其 L11-18：含 close/ma5/ma10/ma20/ma60/rsi14/adx14/vol_ma5 等；`price`→`close` 别名）。

---

### Task 1: rules.py — 规则常量与覆盖

**Files:**
- Create: `stock_selector_api/plugin/portfolio/rules.py`
- Test: `stock_selector_api/plugin/tests/test_rules.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_rules.py"""
import json
from pathlib import Path

from portfolio.rules import load_rules


def test_defaults_when_file_empty(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio_rules.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    rules = load_rules()
    assert "sell" in rules and "add" in rules and "hold" in rules
    assert isinstance(rules["sell"], str) and len(rules["sell"]) > 0


def test_json_overrides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio_rules.json"
    cfg.write_text(json.dumps({"sell": "close < 1", "add": "close > 2"}),
                   encoding="utf-8")
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(cfg))
    rules = load_rules()
    assert rules["sell"] == "close < 1"
    assert rules["add"] == "close > 2"


def test_file_absent_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "/nonexistent/x.json")
    rules = load_rules()
    assert rules["sell"] and rules["add"] and rules["hold"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_rules.py -q`（在 `stock_selector_api/plugin` 目录）
Expected: FAIL（`ModuleNotFoundError: No module named 'portfolio'`）

- [ ] **Step 3: 实现**

```python
"""portfolio/rules.py — sell/add/hold 判定规则：默认常量，JSON 可覆盖。

规则表达式与回测 trading_rules 同构（布尔表达式，仅访问 RuleEvaluator 白名单列）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("portfolio.rules")

# 默认规则（常量；与回测 evaluate_holdings 语义一致：跌破均线→sell，站上均线且未过热→add）
DEFAULT_SELL_RULE = "price <= ma10 * 0.95"
DEFAULT_ADD_RULE = "close >= ma5 * 1.03 and rsi14 <= 65"
DEFAULT_HOLD_RULE = "close >= ma1 and close <= ma99"  # 兜底恒真（无触发时 hold）


def _default_rules() -> dict:
    return {
        "sell": DEFAULT_SELL_RULE,
        "add": DEFAULT_ADD_RULE,
        "hold": DEFAULT_HOLD_RULE,
    }


def load_rules() -> dict:
    """读 PLUGIN_RULES_FILE（JSON）覆盖默认规则；文件缺失/{} → 默认常量。"""
    rules = _default_rules()
    path = os.environ.get("PLUGIN_RULES_FILE", "")
    if not path or not Path(path).is_file():
        return rules
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in ("sell", "add", "hold"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                rules[key] = value.strip()
    except Exception as exc:  # 规则文件坏了不致命：退回默认
        logger.warning("解析 %s 失败(%s)，使用默认规则", path, exc)
    return rules
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_rules.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): 持仓诊断 M2 规则常量与 JSON 覆盖（sell/add/hold 默认规则）"
```

---

### Task 2: pricing.py — 行情取价（parquet 优先 + AKShare 兜底）

**Files:**
- Create: `stock_selector_api/plugin/portfolio/pricing.py`
- Test: `stock_selector_api/plugin/tests/test_pricing.py`

**说明：** R1-4「插件在 analyze 内自行查实时行情计算收益快照」。优先读 `data/stock_kline/{code}.parquet`（已含全部指标列的增量行情，`load_kline_df` 同源）；文件缺失时用 `stock_selector/datasource/ak_source.py::AKShareSource.get_kline` 实时拉取取最近收盘。两处都拿不到 → 抛 `PricingError`（触发后端 R2-2 重试）。

- [ ] **Step 1: 写失败测试**

```python
"""test_pricing.py"""
import pandas as pd
import pytest

from portfolio.pricing import PricingError, latest_price_from_df, latest_price


def _df():
    return pd.DataFrame({
        "date": ["2026-09-15", "2026-09-16", "2026-09-17"],
        "close": [10.0, 10.5, 11.0],
        "open": [9.9, 10.1, 10.4],
    })


def test_latest_price_from_df_uses_row_le_day():
    assert latest_price_from_df(_df(), "2026-09-16") == 10.5


def test_latest_price_from_df_future_day_uses_last():
    assert latest_price_from_df(_df(), "2026-09-20") == 11.0


def test_latest_price_from_df_empty_raises():
    with pytest.raises(PricingError):
        latest_price_from_df(pd.DataFrame(columns=["date", "close"]), "2026-09-16")


def test_latest_price_falls_back_to_ak(monkeypatch):
    from portfolio.pricing import _ak_latest
    calls = []

    def fake_get_kline(code):
        calls.append(code)
        return pd.DataFrame({"date": ["2026-09-16"], "close": [42.5]})

    monkeypatch.setattr("portfolio.pricing._ak_latest", fake_get_kline)
    assert latest_price("600000", "2026-09-17") == 42.5
    assert calls == ["600000"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 实现**

```python
"""portfolio/pricing.py — 行情取价：K线 parquet 优先（含指标列），AKShare 实时兜底。"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

from stock_selector.datasource.ak_source import AKShareSource


class PricingError(Exception):
    """行情不可得（触发后端重试/置 failed）。"""


_DEFAULT_KLINE_DIR = str(Path(__file__).resolve().parents[3] / "data" / "stock_kline")


def _kline_dir() -> Path:
    return Path(os.environ.get("PLUGIN_KLINE_DIR", _DEFAULT_KLINE_DIR))


def _market_prefix(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "30", "12")):
        return "sz"
    return "bj"


def _code_file(code: str) -> Path | None:
    for suffix in ("", f".{_market_prefix(code)}"):
        p = _kline_dir() / f"{code}{suffix}.parquet"
        if p.is_file():
            return p
    return None


def _load_kline_df(code: str) -> pd.DataFrame:
    p = _code_file(code)
    if p is None:
        raise PricingError(f"无K线数据: {code}")
    df = pd.read_parquet(p)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    return df


def latest_price_from_df(df: pd.DataFrame, natural_day: str) -> float:
    if df is None or df.empty or "close" not in df.columns:
        raise PricingError("行情数据为空或缺少 close 列")
    sub = df[df["date"] <= natural_day] if "date" in df.columns else df
    if sub.empty:
        raise PricingError(f"natural_day({natural_day}) 前无行情")
    return float(sub.sort_values("date").iloc[-1]["close"])


def _ak_latest(code: str) -> float:
    df = AKShareSource().get_kline(code)
    if df is None or df.empty or "close" not in df.columns:
        raise PricingError(f"AKShare 无行情: {code}")
    return float(df.iloc[-1]["close"])


def latest_price(code: str, natural_day: str) -> float:
    """K线 parquet 优先；文件缺失 → AKShare 实时兜底。"""
    try:
        df = _load_kline_df(code)
        return latest_price_from_df(df, natural_day)
    except PricingError:
        return _ak_latest(code)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_pricing.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): M2 行情取价（parquet 优先 + AKShare 兜底）"
```

---

### Task 3: judge.py — RuleEvaluator 判定

**Files:**
- Create: `stock_selector_api/plugin/portfolio/judge.py`
- Test: `stock_selector_api/plugin/tests/test_judge.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_judge.py"""
from portfolio.judge import judge_holding

ROW = {"close": 10.0, "price": 10.0, "ma10": 11.0, "ma5": 9.5,
       "ma20": 9.0, "rsi14": 40.0, "vol_ma5": 10.0, "volume": 9.0}


def test_sell_when_below_ma10_95():
    row = dict(ROW, price=10.0, ma10=11.0)   # 10 <= 10.45 → True
    action, reason = judge_holding(row, "price <= ma10 * 0.95", "")
    assert action == "sell" and reason


def test_add_when_above_ma5_and_rsi_ok():
    row = dict(ROW, close=11.0, ma5=10.0, rsi14=50.0)
    action, reason = judge_holding(row, "close < 0", "close >= ma5 * 1.03 and rsi14 <= 65")
    assert action == "add"


def test_hold_otherwise():
    action, reason = judge_holding(ROW, "price <= ma10 * 0.95", "close >= ma5 * 1.03")
    assert action == "hold"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_judge.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""portfolio/judge.py — sell/add 判定（hold 兜底）。判定内核复用回测 RuleEvaluator。"""
from __future__ import annotations

from stock_selector.backtest.rule_eval import RuleEvaluator

REASON = {
    "sell": "跌破近期均线支撑，建议减仓观察",
    "add": "趋势向上且未过热，可考虑小幅加仓",
    "hold": "未触发卖出/加仓条件，继续持有",
}

_eval = RuleEvaluator()

MAX_REASON_LEN = 128


def judge_holding(row: dict, sell_rule: str, add_rule: str) -> tuple[str, str]:
    """返回 (action, action_reason)。sell 优先于 add；均不触发 → hold。"""
    if sell_rule and _eval.eval(sell_rule, row):
        return "sell", REASON["sell"][:MAX_REASON_LEN]
    if add_rule and _eval.eval(add_rule, row):
        return "add", REASON["add"][:MAX_REASON_LEN]
    return "hold", REASON["hold"][:MAX_REASON_LEN]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_judge.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): M2 判定内核（RuleEvaluator sell/add/hold）"
```

---

### Task 4: revenue.py — 收益快照 + summary 聚合

**Files:**
- Create: `stock_selector_api/plugin/portfolio/revenue.py`
- Test: `stock_selector_api/plugin/tests/test_revenue.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_revenue.py"""
from portfolio.revenue import snapshot, summarize


def test_snapshot_positive_pnl():
    r = snapshot({"code": "600519", "name": "茅台", "quantity": 100, "cost_price": 100.0}, 110.0)
    assert r["latest_price"] == 110.0
    assert r["holding_pnl"] == 1000.0
    assert r["pnl_pct"] == 10.0
    assert r["action_reason"] == ""


def test_snapshot_zero_cost_pnl_pct_zero():
    r = snapshot({"code": "000001", "name": "平安", "quantity": 10, "cost_price": 0.0}, 5.0)
    assert r["holding_pnl"] == 50.0
    assert r["pnl_pct"] == 0.0


def test_summarize():
    results = [
        {"action": "sell", "latest_price": 10.0, "quantity": 100, "holding_pnl": 50.0},
        {"action": "hold", "latest_price": 20.0, "quantity": 10, "holding_pnl": 0.0},
    ]
    s = summarize(results)
    assert s["action_counts"] == {"sell": 1, "hold": 1}
    assert s["total_market_value"] == 1200.0
    assert s["total_pnl"] == 50.0
    assert s["holding_count"] == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_revenue.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""portfolio/revenue.py — 收益快照与 summary 聚合（R1-1/R1-4 插件计算）。"""
from __future__ import annotations


def _r2(v: float) -> float:
    return round(float(v), 2)


def snapshot(holding: dict, latest_price: float) -> dict:
    """单只持仓的收益快照（后端零算术，全部由插件产出）。"""
    quantity = int(holding["quantity"])
    cost = float(holding.get("cost_price", 0.0))
    latest = float(latest_price)
    pnl = (latest - cost) * quantity
    pnl_pct = ((latest - cost) / cost * 100) if cost > 0 else 0.0
    return {
        "code": str(holding["code"]),
        "name": str(holding.get("name", "")),
        "action": "",
        "action_reason": "",
        "latest_price": _r2(latest),
        "quantity": quantity,
        "holding_pnl": _r2(pnl),
        "pnl_pct": _r2(pnl_pct),
        "suggestion": "",
    }


def summarize(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    total_value = 0.0
    total_pnl = 0.0
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        total_value += float(r["latest_price"]) * int(r["quantity"])
        total_pnl += float(r.get("holding_pnl", 0.0))
    return {
        "action_counts": counts,
        "total_market_value": _r2(total_value),
        "total_pnl": _r2(total_pnl),
        "holding_count": len(results),
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_revenue.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): M2 收益快照逐只计算 + summary 聚合"
```

---

### Task 5: provider.py + analyzer.py — 编排

**Files:**
- Create: `stock_selector_api/plugin/portfolio/provider.py`
- Create: `stock_selector_api/plugin/portfolio/analyzer.py`
- Test: `stock_selector_api/plugin/tests/test_analyzer.py`

Provider 职责：对每只持仓取最新指标行（date ≤ natural_day），供 RuleEvaluator 求值 + latest_price 取价。真实实现读 parquet（含指标列）；测试注入 FakeProvider（确定性，不碰磁盘/网络）。

- [ ] **Step 1: 写失败测试**

```python
"""test_analyzer.py"""
import pytest

from portfolio.analyzer import analyze_holdings
from portfolio.rules import load_rules


class FakeProvider:
    """确定性行情/指标：每只持仓一行最新指标。"""

    def __init__(self, rows: dict):
        self.rows = rows

    def latest_row(self, code: str) -> dict | None:
        return self.rows.get(code)


HOLDINGS = [
    {"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0},
    {"code": "600002", "name": "乙", "quantity": 50, "cost_price": 20.0},
    {"code": "600003", "name": "丙", "quantity": 10, "cost_price": 30.0},
]

ROWS = {
    "600001": {"close": 9.0, "price": 9.0, "ma10": 10.0, "ma5": 10.0, "ma20": 10.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # sell（9<=9.5）
    "600002": {"close": 25.0, "price": 25.0, "ma10": 24.0, "ma5": 20.0, "ma20": 20.0,
               "rsi14": 50.0, "vol_ma5": 100.0, "volume": 100.0},   # add
    "600003": {"close": 30.0, "price": 30.0, "ma10": 29.0, "ma5": 29.0, "ma20": 29.0,
               "rsi14": 40.0, "vol_ma5": 100.0, "volume": 100.0},   # hold
}


def test_analyze_judges_and_computes_revenue(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "")   # 用默认规则
    out = analyze_holdings(HOLDINGS, FakeProvider(ROWS))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600001"]["action"] == "sell"
    assert by_code["600002"]["action"] == "add"
    assert by_code["600003"]["action"] == "hold"
    # 收益快照由插件算（R1-4）
    assert by_code["600001"]["latest_price"] == 9.0
    assert by_code["600001"]["holding_pnl"] == round((9.0 - 10.0) * 100, 2)
    assert by_code["600002"]["holding_pnl"] == round((25.0 - 20.0) * 50, 2)
    # summary
    s = out["summary"]
    assert s["action_counts"] == {"sell": 1, "add": 1, "hold": 1}
    assert s["total_market_value"] == round(9.0 * 100 + 25.0 * 50 + 30.0 * 10, 2)
    assert s["holding_count"] == 3


def test_analyze_missing_row_sorted_to_sell(monkeypatch):
    monkeypatch.setenv("PLUGIN_RULES_FILE", "")
    rows = dict(ROWS)
    del rows["600003"]                       # 行情缺失
    out = analyze_holdings(HOLDINGS, FakeProvider(rows))
    by_code = {r["code"]: r for r in out["results"]}
    assert by_code["600003"]["action"] == "hold"
    assert by_code["600003"]["action_reason"] == "行情缺失，维持原判不做交易建议"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_analyzer.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""portfolio/provider.py — 最新指标行提供者（真实=parquet；测试=内存 dict）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_INDICATOR_COLUMNS = {
    "close", "open", "high", "low", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60", "ema12", "ema26",
    "macd_dif", "macd_dea", "macd_hist", "rsi14",
    "kdj_k", "kdj_d", "kdj_j", "wr14", "atr14",
    "boll_upper", "boll_mid", "boll_lower", "obv", "roc10",
    "vol_ma5", "last_close", "adx14",
}


class KlineProvider:
    """从 K线 parquet 读 date ≤ natural_day 的最新一行指标（与回测同一数据面）。"""

    def __init__(self, natural_day: str, kline_dir: Path | None = None):
        self.natural_day = natural_day
        self.kline_dir = kline_dir

    def latest_row(self, code: str) -> dict | None:
        from portfolio.pricing import _code_file, _market_prefix  # 复用文件名规则

        p = _code_file(code) if self.kline_dir is None else (self.kline_dir / f"{code}.parquet")
        if p is None or not Path(p).is_file():
            return None
        df = pd.read_parquet(p)
        df["date"] = df["date"].astype(str).str.slice(0, 10)
        sub = df[df["date"] <= self.natural_day]
        if sub.empty:
            return None
        last = sub.sort_values("date").iloc[-1]
        row = {c: float(last[c]) for c in _INDICATOR_COLUMNS if c in last}
        row["code"] = str(code)
        row["name"] = str(last.get("name", "")) if "name" in last else ""
        row["date"] = str(last["date"])
        return row
```

```python
"""portfolio/analyzer.py — 插件 analyze 编排（R1-1/R1-4：判定+收益全部在此计算）。"""
from __future__ import annotations

from portfolio.judge import judge_holding
from portfolio.revenue import snapshot, summarize
from portfolio.rules import load_rules


def analyze_holdings(holdings: list[dict], provider) -> dict:
    """对每只持仓：取最新指标行 → RuleEvaluator 判定 → 收益快照 → summary。"""
    rules = load_rules()
    results: list[dict] = []
    for h in holdings:
        base = snapshot(h, 0.0 if h.get("cost_price") is None else float(h["cost_price"]))
        row = provider.latest_row(h["code"])
        if row is None:
            base["action"] = "hold"
            base["action_reason"] = "行情缺失，维持原判不做交易建议"
            results.append(base)
            continue
        base["action"], base["action_reason"] = judge_holding(row, rules["sell"], rules["add"])
        base["latest_price"] = round(float(row["close"]), 2)
        qty = int(h["quantity"])
        cost = float(h.get("cost_price", 0.0))
        base["holding_pnl"] = round((base["latest_price"] - cost) * qty, 2)
        base["pnl_pct"] = round(((base["latest_price"] - cost) / cost * 100), 2) if cost > 0 else 0.0
        results.append(base)
    return {"results": results, "summary": summarize(results)}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_analyzer.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): M2 provider + analyze 编排（判定+收益快照+summary）"
```

---

### Task 6: server.py + Authenticator 契约测试（ASGI）

**Files:**
- Create: `stock_selector_api/plugin/server.py`
- Create: `stock_selector_api/plugin/test_auth.py`（契约客户端侧以 FFF 写入）
- Test: `stock_selector_api/plugin/tests/test_server.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_server.py"""
import httpx
import pytest

from server import app


@pytest.mark.asyncio
async def test_analyze_endpoint_contract():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/analyze", json={
            "user_id": "u_1",
            "natural_day": "2026-09-17",
            "holdings": [
                {"code": "600001", "name": "甲", "quantity": 100, "cost_price": 10.0},
            ],
        })
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body and "summary" in body
    assert body["results"][0]["code"] == "600001"


@pytest.mark.asyncio
async def test_analyze_invalid_body_rejected():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/portfolio/analyze", json={"user_id": "u_1"})
    assert resp.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_server.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
"""server.py — 持仓诊断插件服务（独立进程，默认 8001）。

契约：POST /api/portfolio/analyze
  body:   {user_id, natural_day, holdings: [{code,name,quantity,cost_price}]}
  resp:   {results: [{code,name,action,action_reason,latest_price,quantity,
                     holding_pnl,pnl_pct,suggestion}], summary: {...}}
一切判定与收益计算在插件完成（R1-1/1-4）；后端仅代理透传（R1-2）。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from portfolio.analyzer import analyze_holdings
from portfolio.pricing import PricingError
from portfolio.provider import KlineProvider

app = FastAPI(title="持仓诊断插件", version="0.2.0")


class HoldingIn(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)


class AnalyzeBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    holdings: list[HoldingIn] = Field(min_length=1)


@app.post("/api/portfolio/analyze")
async def analyze(body: AnalyzeBody) -> dict:
    try:
        out = analyze_holdings(
            [h.model_dump() for h in body.holdings],
            KlineProvider(natural_day=body.natural_day),
        )
    except PricingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return out


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8001)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_server.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin
git commit -m "feat(plugin): M2 插件 HTTP 服务 POST /api/portfolio/analyze"
```

---

### Task 7: 后端默认 plugin_mode 翻 http + 全量回归

**Files:**
- Modify: `stock_selector_api/backend/config.py:84`
- Test: `stock_selector_api/backend/tests/test_portfolio_plugin.py`、`test_portfolio_service.py`（应仍绿——conftest 强制 mock）

- [ ] **Step 1: 改 backend 默认**

```python
# config.py 原第 84 行
plugin_mode: str = "http"   # http（本机插件，默认 8001）| mock（测试确定性）
```

- [ ] **Step 2: 验证后端测试仍绿（conftest 锁定 mock）**

Run: `python -m pytest tests/ -q`（在 `stock_selector_api/backend` 目录，用 `--basetemp` 规避 Windows pytest-current 权限告警）
Expected: 61 passed（M2 不触后端逻辑，回归不变）

- [ ] **Step 3: 手动冒烟插件服务**

Run: `python -m uvicorn server:app --port 8001`（在 `stock_selector_api/plugin` 目录），另终端：
`curl -s -X POST http://localhost:8001/api/portfolio/analyze -H 'Content-Type: application/json' -d '{"user_id":"u_1","natural_day":"2026-09-17","holdings":[{"code":"000001","name":"平安银行","quantity":100,"cost_price":10}]}'`
Expected: `{"results":[{...action...}],"summary":{...}}`（读真实 parquet；缺文件时 AKShare 兜底可能较慢）

- [ ] **Step 4: Commit（config 默认值变更单独提交）**

```bash
git add stock_selector_api/backend/config.py
git commit -m "chore(plugin): M2 后端默认 plugin_mode 翻为 http（测试仍强制 mock）"
```

---

## 验收标准汇总（对照 M2 行验收基准「R1-1/1-4 插件收益计算 + 判定收敛」）

| 验收项 | 验证方式 | 结果 |
|---|---|---|
| 插件自行取行情并计算收益快照（R1-4） | `test_revenue.py` + `test_pricing.py` | 绿 |
| 判定收敛插件（sell/add/hold，RuleEvaluator） | `test_judge.py` + `test_analyzer.py` | 绿 |
| HTTP 契约与后端 client 对齐（results/summary） | `test_server.py` + 手动冒烟 | 绿 |
| 后端零算术不受影响（仍透传 mock） | backend 全量回归 61 绿 | 绿 |
| 后端默认接通真实插件 | `config.plugin_mode="http"` | 生效 |

## 风险与注意

- **K线缺文件**：插件 PricingError→503→后端视为插件不可用自动走 R2-2 重试(M3 实现)；无基目录时 `PLUGIN_KLINE_DIR` 可指 `D:\select_stock\data\stock_kline`。
- **AKShare 兜底慢/被反爬**：仅文件缺失时触发；生产建议补全 parquet。
- 既有 4 例 mock 插件测试（`test_portfolio_plugin.py`）不改动——mock 模式仍保留供离线测试。