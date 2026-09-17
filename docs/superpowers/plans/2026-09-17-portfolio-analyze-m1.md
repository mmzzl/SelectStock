# 持仓诊断 M1：analyze 代理契约 + DB 双表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `stock_selector_api/` 实现持仓诊断 M1——后端新增 `POST /api/portfolio/analyze`（代理插件、幂等重放、判定/收益零后端计算）+ `GET /api/portfolio/runs`（按 user_id 隔离列表），并落 `portfolio_diag_runs` / `portfolio_diag_results` 双表，使昨日遗留的 5 个契约用例全绿。

**Architecture:** 按既有三层（api → services → repositories）+ SQLAlchemy 双表。analyze 为同步代理：`(user_id, natural_day, holdings_md5)` 幂等键命中→复用既有 run_id；未命中→建 queued run → 调插件(mock 客户端，M1 默认 `plugin_mode=mock`) → 插件 results/summary **原样透传落库**（R1-2 零算术）→ 置 completed → 返回 `{run_id, status, results, summary}`。外部契约路由挂 `/api`（方案 §2/§3 明确 `POST /api/portfolio/analyze`、`GET /api/portfolio/diag/{run_id}`，**不走 `/api/v1`**），不挂密码到期网关（外部系统入口，鉴权留 M3）。

**Tech Stack:** FastAPI · SQLAlchemy 2.0 (async) · SQLite(aiosqlite) · Pydantic v2 · httpx · pytest(asyncio_mode=auto)

**事实源：** `docs/agent-rules/3-overall-design/output/2026-09-15-select-uniapp/3-方案与实施计划.md`（§2 代理契约、§3 DB 双表）、《2-红线契约清单.md》（R1/R2/R3/R5）。

**Conventions（learned from codebase，`stock_selector_api/` 内）：**
- 分层：`api/v1/*.py`（路由，`core.response.ok/paged` 统一返回）→ `services/*.py`（业务）→ `repositories/*.py`（`ReposBase = pick()` + 通用 base helper `_find_one/_find_all/_delete_from`）→ `db/sql/*.py`（ORM，`Base/IdMixin/TimestampMixin/new_uuid`）。
- 测试：pytest `asyncio_mode=auto`，conftest 提供 session 级内存 SQLite（`db.base._engine`）+ seed；`client` fixture 为真 app；**测试命令一律在 `stock_selector_api/backend` 目录运行**。
- 新表注册进 `db/sql/__init__.py` 的 import + `__all__`（否则 conftest `create_all` 不建表）。
- 提交前跑 `code-compliance-check`。

---

## 文件映射

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `stock_selector_api/backend/db/sql/portfolio.py` | 新增 | ORM：`PortfolioDiagRun`（`portfolio_diag_runs`）+ `PortfolioDiagResult`（`portfolio_diag_results`） |
| `stock_selector_api/backend/db/sql/__init__.py` | 修改 | 注册两个新模型 |
| `stock_selector_api/backend/schemas/portfolio.py` | 新增 | `HoldingItem/AnalyzeBody`（入参）+ `PortfolioDiagRunCreate/PortfolioDiagResultCreate`（写白名单）+ Out schema |
| `stock_selector_api/backend/repositories/portfolio.py` | 新增 | run 仓 `find_by_key/list_by_user`；results 仓 `list_by_run` |
| `stock_selector_api/backend/repositories/__init__.py` | 修改 | 导出 `portfolio_runs` / `portfolio_results` 单例 |
| `stock_selector_api/backend/services/portfolio_plugin.py` | 新增 | 插件客户端：`call_plugin_analyze`（mock 默认 / http 待 M2） |
| `stock_selector_api/backend/services/portfolio_service.py` | 新增 | `analyze`（幂等+代理+双表落库）+ `list_runs` |
| `stock_selector_api/backend/config.py` | 修改 | 新增 `plugin_mode` / `plugin_base_url` |
| `stock_selector_api/backend/api/v1/portfolio.py` | 重写 | `POST /portfolio/analyze` + `GET /portfolio/runs` |
| `stock_selector_api/backend/api/v1/router.py` | 修改 | 移除 portfolio include（改由 main.py 挂 `/api`） |
| `stock_selector_api/backend/main.py` | 修改 | `app.include_router(portfolio_router, prefix="/api")` |
| `stock_selector_api/backend/tests/conftest.py` | 修改 | `_test_env` 显式 `settings.plugin_mode = "mock"` |
| `stock_selector_api/backend/tests/test_portfolio_db.py` | 新增 | 双表注册/约束测试 |
| `stock_selector_api/backend/tests/test_portfolio_repo.py` | 新增 | 幂等键查找/明细关联 |
| `stock_selector_api/backend/tests/test_portfolio_plugin.py` | 新增 | mock 插件结构 + 确定性 |
| `stock_selector_api/backend/tests/test_portfolio_service.py` | 新增 | 服务级幂等/隔离/透传 |
| `stock_selector_api/backend/tests/test_portfolio.py` | 重写 | 用 conftest `client` fixture，修复断掉的 `app_fx` |
| `stock_selector_api/backend/tests/test_portfolio_analyze.py` | 修改 | status 断言 `{queued,running}` → `"completed"`（对齐方案出参） |

---

### Task 1: DB 双表模型 + 注册

**Files:**
- Create: `stock_selector_api/backend/db/sql/portfolio.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`
- Test: `stock_selector_api/backend/tests/test_portfolio_db.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_portfolio_db.py`**

```python
"""持仓诊断双表契约：表名 / 列 / 唯一约束（方案 §3）。"""

from sqlalchemy import UniqueConstraint

from db.sql import Base
from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun


def test_tables_registered():
    tables = Base.metadata.tables
    assert "portfolio_diag_runs" in tables
    assert "portfolio_diag_results" in tables


def test_run_columns_and_unique_key():
    tbl = Base.metadata.tables["portfolio_diag_runs"]
    for col in ("run_id", "batch_id", "user_id", "natural_day", "status",
                "held_code_count", "total_market_value"):
        assert col in tbl.columns
    uniques = [c for c in tbl.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(uc.columns.keys()) == {"user_id", "natural_day", "batch_id"} for uc in uniques)


def test_result_columns_and_unique():
    tbl = Base.metadata.tables["portfolio_diag_results"]
    for col in ("run_id", "code", "name", "action", "latest_price",
                "quantity", "holding_pnl", "pnl_pct"):
        assert col in tbl.columns
    uniques = [c for c in tbl.constraints if isinstance(c, UniqueConstraint)]
    assert any(set(uc.columns.keys()) == {"run_id", "code"} for uc in uniques)


def test_models_tablenames():
    assert PortfolioDiagRun.__tablename__ == "portfolio_diag_runs"
    assert PortfolioDiagResult.__tablename__ == "portfolio_diag_results"
```

- [ ] **Step 2: 运行确认失败**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_db.py -q`
Expected: `ModuleNotFoundError: No module named 'db.sql.portfolio'`

- [ ] **Step 3: 实现模型 `backend/db/sql/portfolio.py`**

```python
"""持仓诊断双表：run 头 + 明细。收益/市值来自插件透传（R1-2 零算术），后端仅落库。"""

from sqlalchemy import Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin, new_uuid


class PortfolioDiagRun(Base, TimestampMixin):
    __tablename__ = "portfolio_diag_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "natural_day", "batch_id", name="uq_portfolio_diag_runs_key"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)  # holdings_md5
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    natural_day: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/completed/failed
    held_code_count: Mapped[int] = mapped_column(Integer, default=0)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    total_market_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    plugin_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(512), default="")


class PortfolioDiagResult(Base, IdMixin):
    __tablename__ = "portfolio_diag_results"
    __table_args__ = (
        UniqueConstraint("run_id", "code", name="uq_portfolio_diag_result_code"),
    )

    run_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # sell/hold/add
    action_reason: Mapped[str] = mapped_column(String(512), default="")
    latest_price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    holding_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    suggestion: Mapped[str] = mapped_column(String(512), default="")
```

- [ ] **Step 4: 注册到 `backend/db/sql/__init__.py`**

在 `from db.sql.password_history import PasswordHistory` 后追加，并把两个新类加进 `__all__`：

```python
from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun
```

`__all__` 追加 `"PortfolioDiagRun"`, `"PortfolioDiagResult"`。

- [ ] **Step 5: 运行确认通过**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_db.py -q`
Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add stock_selector_api/backend/db/sql/portfolio.py stock_selector_api/backend/db/sql/__init__.py stock_selector_api/backend/tests/test_portfolio_db.py
git commit -m "feat(portfolio): 持仓诊断双表模型 portfolio_diag_runs/results（幂等键+明细唯一约束）"
```

> 提交前调用 `code-compliance-check`。

---

### Task 2: schema + repository（幂等键查找 / 明细关联）

**Files:**
- Create: `stock_selector_api/backend/schemas/portfolio.py`
- Create: `stock_selector_api/backend/repositories/portfolio.py`
- Modify: `stock_selector_api/backend/repositories/__init__.py`
- Test: `stock_selector_api/backend/tests/test_portfolio_repo.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_portfolio_repo.py`**

```python
"""持仓诊断 Repository：幂等键查找 + 明细按 run 关联。"""

from repositories import portfolio_results, portfolio_runs


async def _clean() -> None:
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await portfolio_runs._delete_from("portfolio_diag_runs", {})


async def test_run_create_and_find_by_key():
    await _clean()
    run = await portfolio_runs.create({
        "batch_id": "md5-k1", "user_id": "repo-u1", "natural_day": "2026-09-16",
        "status": "queued", "held_code_count": 1,
    })
    assert run["run_id"]
    found = await portfolio_runs.find_by_key("repo-u1", "2026-09-16", "md5-k1")
    assert found and found["run_id"] == run["run_id"]
    assert await portfolio_runs.find_by_key("repo-u1", "2026-09-16", "md5-other") is None
    await _clean()


async def test_result_write_and_list_by_run():
    await _clean()
    run = await portfolio_runs.create({
        "batch_id": "md5-k2", "user_id": "repo-u2", "natural_day": "2026-09-16",
        "status": "completed", "held_code_count": 2,
    })
    await portfolio_results.create({
        "run_id": run["run_id"], "code": "600519", "action": "hold",
        "latest_price": 1700.0, "quantity": 100, "holding_pnl": 200.0, "pnl_pct": 1.2,
    })
    rows = await portfolio_results.list_by_run(run["run_id"])
    assert len(rows) == 1 and rows[0]["code"] == "600519"
    assert rows[0]["action"] == "hold"
    await _clean()
```

- [ ] **Step 2: 运行确认失败**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_repo.py -q`
Expected: `ModuleNotFoundError: No module named 'repositories.portfolio'`

- [ ] **Step 3: 实现 `backend/schemas/portfolio.py`**

```python
"""持仓诊断 schema：analyze 入参 + 落库写白名单 + 出参。"""

from pydantic import BaseModel, Field

from schemas.common import ORMModel


class HoldingItem(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    quantity: int = Field(ge=0)
    cost_price: float = Field(ge=0)


class AnalyzeBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    holdings: list[HoldingItem] = Field(min_length=1)
    # 兼容红线锚定测试传入的快照 md5 字段；服务端始终以实际 holdings 计算批次键
    user_holdings_md5: str | None = None
    portfolio_snapshot_md5: str | None = None


class PortfolioDiagRunCreate(BaseModel):
    batch_id: str
    user_id: str
    natural_day: str
    status: str = "queued"
    held_code_count: int = 0
    action_count: int = 0
    total_market_value: float = 0.0
    total_pnl: float = 0.0
    plugin_time_ms: int = 0
    error: str = ""


class PortfolioDiagResultCreate(BaseModel):
    run_id: str
    code: str
    name: str = ""
    action: str
    action_reason: str = ""
    latest_price: float = 0.0
    quantity: int = 0
    holding_pnl: float = 0.0
    pnl_pct: float = 0.0
    suggestion: str = ""


class PortfolioRunOut(ORMModel):
    run_id: str
    batch_id: str
    user_id: str
    natural_day: str
    status: str
    held_code_count: int = 0
    action_count: int = 0
    total_market_value: float = 0.0
    total_pnl: float = 0.0
    plugin_time_ms: int = 0
    error: str = ""


class PortfolioResultOut(ORMModel):
    run_id: str
    code: str
    name: str = ""
    action: str
    action_reason: str = ""
    latest_price: float = 0.0
    quantity: int = 0
    holding_pnl: float = 0.0
    pnl_pct: float = 0.0
    suggestion: str = ""
```

- [ ] **Step 4: 实现 `backend/repositories/portfolio.py`**

```python
"""持仓诊断 Repository：run 头 + 明细，按 (user_id, natural_day, batch_id) 幂等。"""

from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun
from repositories._choose import pick
from schemas.portfolio import PortfolioDiagResultCreate, PortfolioDiagRunCreate

RepoBase = pick()


class PortfolioDiagRunRepo(RepoBase):
    model = PortfolioDiagRun
    table = "portfolio_diag_runs"
    schema = PortfolioDiagRunCreate

    async def find_by_key(self, user_id: str, natural_day: str, batch_id: str) -> dict | None:
        return await self._find_one(
            {"user_id": user_id, "natural_day": natural_day, "batch_id": batch_id}
        )

    async def list_by_user(self, user_id: str, page: int = 1, size: int = 20):
        return await self.list(filters={"user_id": user_id}, page=page, size=size)


class PortfolioDiagResultRepo(RepoBase):
    model = PortfolioDiagResult
    table = "portfolio_diag_results"
    schema = PortfolioDiagResultCreate

    async def list_by_run(self, run_id: str) -> list[dict]:
        return await self._find_all(filters={"run_id": run_id})
```

- [ ] **Step 5: 导出单例 `backend/repositories/__init__.py`**

追加 import（`from repositories.password_history import PasswordHistoryRepo` 之后）：

```python
from repositories.portfolio import PortfolioDiagResultRepo, PortfolioDiagRunRepo
```

单例块追加：

```python
portfolio_runs = PortfolioDiagRunRepo()
portfolio_results = PortfolioDiagResultRepo()
```

`__all__` 追加 `"PortfolioDiagRunRepo"`, `"PortfolioDiagResultRepo"`, `"portfolio_runs"`, `"portfolio_results"`。

- [ ] **Step 6: 运行确认通过**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_repo.py -q`
Expected: `2 passed`

- [ ] **Step 7: Commit**

```bash
git add stock_selector_api/backend/schemas/portfolio.py stock_selector_api/backend/repositories/portfolio.py stock_selector_api/backend/repositories/__init__.py stock_selector_api/backend/tests/test_portfolio_repo.py
git commit -m "feat(portfolio): analyze schema + run/results 仓储（幂等键查找/明细关联）"
```

> 提交前调用 `code-compliance-check`。

---

### Task 3: 插件客户端（mock 占位）+ config

**Files:**
- Modify: `stock_selector_api/backend/config.py`
- Create: `stock_selector_api/backend/services/portfolio_plugin.py`
- Modify: `stock_selector_api/backend/tests/conftest.py`
- Test: `stock_selector_api/backend/tests/test_portfolio_plugin.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_portfolio_plugin.py`**

```python
"""插件客户端（mock 模式）：判定+收益快照结构、确定性。M2 前仅占位。"""

from services import portfolio_plugin

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    {"code": "300750", "name": "宁德时代", "quantity": 200, "cost_price": 180.0},
]


async def test_mock_plugin_returns_results_and_summary():
    out = await portfolio_plugin.call_plugin_analyze({"user_id": "u1", "holdings": HOLDINGS})
    assert len(out["results"]) == 3
    for r, h in zip(out["results"], HOLDINGS):
        assert r["code"] == h["code"]
        assert r["action"] in portfolio_plugin.ACTIONS
        assert r["latest_price"] > 0
        assert "holding_pnl" in r and "pnl_pct" in r
    s = out["summary"]
    assert s["holding_count"] == 3
    assert sum(s["action_counts"].values()) == 3


async def test_mock_plugin_replay_deterministic():
    a = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    b = await portfolio_plugin.call_plugin_analyze({"holdings": HOLDINGS[:2]})
    assert a == b
```

- [ ] **Step 2: 运行确认失败**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_plugin.py -q`
Expected: `ModuleNotFoundError: No module named 'services.portfolio_plugin'`

- [ ] **Step 3: config 增加插件配置（`config.py`，放在「定时任务」块后）**

```python
    # ---------- 持仓诊断插件（M1 默认 mock；M2 接入真实插件端口后置 env）----------
    plugin_mode: str = "mock"             # mock | http
    plugin_base_url: str = "http://localhost:8001"
```

- [ ] **Step 4: 实现 `backend/services/portfolio_plugin.py`**

```python
"""持仓诊断·插件客户端（R1-1 判定/收益 100% 收敛插件）。

M1（M2 未实现）默认 plugin_mode=mock：返回确定性 sell/hold/add 判定 + 收益快照，
仅供契约测试与本地自验。plugin_mode=http 时走 httpx 代理到插件地址（M2 接通后启用）。
"""

import logging

import httpx

from config import settings

logger = logging.getLogger("portfolio_plugin")

ACTIONS = ("sell", "hold", "add")


def _mock_analyze(holdings: list[dict]) -> dict:
    """模拟插件判定（sell/hold/add 轮换）+ 收益快照。仅测试占位，不接真实行情。"""
    results = []
    total_mv = 0.0
    total_pnl = 0.0
    for i, h in enumerate(holdings):
        action = ACTIONS[i % len(ACTIONS)]
        latest = round(h["cost_price"] * (1 + 0.005 * (i + 1)), 2)
        pnl = round((latest - h["cost_price"]) * h["quantity"], 2)
        results.append({
            "code": h["code"],
            "name": h.get("name", ""),
            "action": action,
            "action_reason": f"mock plugin: {action}",
            "latest_price": latest,
            "quantity": h["quantity"],
            "holding_pnl": pnl,
            "pnl_pct": round((latest / h["cost_price"]) * 100 - 100, 2),
            "suggestion": "",
        })
        total_mv += latest * h["quantity"]
        total_pnl += pnl
    summary = {
        "action_counts": {a: sum(1 for r in results if r["action"] == a) for a in ACTIONS},
        "total_market_value": round(total_mv, 2),
        "total_pnl": round(total_pnl, 2),
        "holding_count": len(results),
    }
    return {"results": results, "summary": summary}


async def _http_analyze(payload: dict) -> dict:
    url = settings.plugin_base_url.rstrip("/") + "/api/portfolio/analyze"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    return {"results": body["results"], "summary": body.get("summary", {})}


async def call_plugin_analyze(payload: dict) -> dict:
    """请求插件判定 + 收益快照；返回 {results, summary}。失败抛异常（R2-2 兜底由上层处理）。"""
    if settings.plugin_mode == "http":
        return await _http_analyze(payload)
    logger.info("plugin_mode=mock，返回 mock 判定（holdings=%d）", len(payload.get("holdings", [])))
    return _mock_analyze(payload["holdings"])
```

- [ ] **Step 5: conftest 显式 mock（`backend/tests/conftest.py` 的 `_test_env` 块内追加）**

在 `settings.schedule_enabled = False` 后追加：

```python
    settings.plugin_mode = "mock"
```

- [ ] **Step 6: 运行确认通过**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_plugin.py -q`
Expected: `2 passed`

- [ ] **Step 7: Commit**

```bash
git add stock_selector_api/backend/config.py stock_selector_api/backend/services/portfolio_plugin.py stock_selector_api/backend/tests/conftest.py stock_selector_api/backend/tests/test_portfolio_plugin.py
git commit -m "feat(portfolio): 插件客户端（mock 占位 + http 代理骨架）"
```

> 提交前调用 `code-compliance-check`。

---

### Task 4: portfolio 服务（analyze 幂等代理 + 双表落库 + runs 列表）

**Files:**
- Create: `stock_selector_api/backend/services/portfolio_service.py`
- Test: `stock_selector_api/backend/tests/test_portfolio_service.py`

- [ ] **Step 1: 写失败测试 `backend/tests/test_portfolio_service.py`**

```python
"""持仓诊断服务：幂等重放 / 用户隔离 / 判定收敛透传（零后端算术）。"""

from repositories import portfolio_results, portfolio_runs
from services import portfolio_service

HOLDINGS = [
    {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
    {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
]


async def _clean():
    await portfolio_results._delete_from("portfolio_diag_results", {})
    await portfolio_runs._delete_from("portfolio_diag_runs", {})


async def test_analyze_create_and_return_results():
    await _clean()
    out = await portfolio_service.analyze("svc-u1", "2026-09-16", HOLDINGS)
    assert out["run_id"]
    assert out["status"] == "completed"
    assert len(out["results"]) == 2
    for r in out["results"]:
        assert r["action"] in {"sell", "hold", "add"}
        assert "latest_price" in r and "holding_pnl" in r
    await _clean()


async def test_analyze_idempotent_replay():
    await _clean()
    a = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u2", "2026-09-16", HOLDINGS)
    assert a["run_id"] == b["run_id"]
    runs = await portfolio_runs._find_all({"user_id": "svc-u2"})
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    await _clean()


async def test_analyze_user_isolation_key():
    await _clean()
    a = await portfolio_service.analyze("svc-u3", "2026-09-16", HOLDINGS)
    b = await portfolio_service.analyze("svc-u3b", "2026-09-16", HOLDINGS)
    assert a["run_id"] != b["run_id"]
    await _clean()


async def test_list_runs_user_isolation():
    await _clean()
    await portfolio_service.analyze("svc-u4", "2026-09-16", HOLDINGS)
    items, total = await portfolio_service.list_runs("svc-u4")
    assert total >= 1 and all(x["user_id"] == "svc-u4" for x in items)
    items, total = await portfolio_service.list_runs("svc-other")
    assert total == 0
    await _clean()
```

- [ ] **Step 2: 运行确认失败**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_service.py -q`
Expected: `ModuleNotFoundError: No module named 'services.portfolio_service'`

- [ ] **Step 3: 实现 `backend/services/portfolio_service.py`**

```python
"""持仓诊断服务（M1）：代理插件 + 双表落库 + 幂等重放 + 按用户隔离列表。

红线：
- R1-1/R1-2 判定与收益全部透传插件返回，后端不做任何判定/收益公式；
- R2-3 同 (user_id, natural_day, holdings_md5) 幂等复用 run_id；
- R3-1 一律按 user_id 过滤（隔离）。
"""

import hashlib
import json
import logging
import time

from core.exceptions import BizError
from repositories import portfolio_results, portfolio_runs
from services import portfolio_plugin

logger = logging.getLogger("portfolio_service")

_RESULT_FIELDS = (
    "code", "name", "action", "action_reason", "latest_price",
    "quantity", "holding_pnl", "pnl_pct", "suggestion",
)


def holdings_md5(holdings: list[dict]) -> str:
    """规范化持仓快照（排序 + 固定字段）算 md5，作为批次幂等键（batch_id）。"""
    canon = json.dumps(
        [
            {
                "code": h["code"],
                "name": h.get("name", ""),
                "quantity": int(h["quantity"]),
                "cost_price": float(h["cost_price"]),
            }
            for h in holdings
        ],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.md5(canon.encode("utf-8")).hexdigest()


def _result_out(r: dict) -> dict:
    return {k: r.get(k) for k in _RESULT_FIELDS}


def _summary_from_plugin(plugin_out: dict, holding_count: int) -> dict:
    summary = dict(plugin_out.get("summary", {}))
    summary.setdefault("action_counts", {})
    summary.setdefault("total_market_value", 0.0)
    summary.setdefault("total_pnl", 0.0)
    summary.setdefault("holding_count", holding_count)
    return summary


def _summary_from_run(run: dict, results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {
        "action_counts": counts,
        "total_market_value": run.get("total_market_value", 0.0),
        "total_pnl": run.get("total_pnl", 0.0),
        "holding_count": run.get("held_code_count", 0),
    }


async def analyze(user_id: str, natural_day: str, holdings: list[dict]) -> dict:
    """代理插件执行一次诊断：幂等重放 / 建 run / 落库。返回 {run_id, status, results, summary}。"""
    batch_id = holdings_md5(holdings)

    existing = await portfolio_runs.find_by_key(user_id, natural_day, batch_id)
    if existing:
        results = await portfolio_results.list_by_run(existing["run_id"])
        return {
            "run_id": existing["run_id"],
            "status": existing["status"],
            "results": [_result_out(r) for r in results],
            "summary": _summary_from_run(existing, results),
        }

    run = await portfolio_runs.create({
        "batch_id": batch_id, "user_id": user_id, "natural_day": natural_day,
        "status": "queued", "held_code_count": len(holdings),
    })
    run_id = run["run_id"]
    try:
        t0 = time.time()
        plugin_out = await portfolio_plugin.call_plugin_analyze(
            {"user_id": user_id, "natural_day": natural_day, "holdings": holdings}
        )
        plugin_ms = int((time.time() - t0) * 1000)
        results = plugin_out["results"]
        for r in results:
            await portfolio_results.create({"run_id": run_id, **r})
        summary = plugin_out.get("summary", {})
        await portfolio_runs.update(run_id, {
            "status": "completed",
            "action_count": len(results),
            "total_market_value": float(summary.get("total_market_value", 0.0)),
            "total_pnl": float(summary.get("total_pnl", 0.0)),
            "plugin_time_ms": plugin_ms,
        })
        return {
            "run_id": run_id,
            "status": "completed",
            "results": [_result_out(r) for r in results],
            "summary": _summary_from_plugin(plugin_out, len(results)),
        }
    except Exception as exc:
        logger.exception("持仓诊断插件调用失败: %s", run_id)
        await portfolio_runs.update(run_id, {"status": "failed", "error": str(exc)[:512]})
        raise BizError("持仓诊断失败：插件不可用", code=502, status_code=502) from exc


async def list_runs(user_id: str, page: int = 1, size: int = 20):
    return await portfolio_runs.list_by_user(user_id, page, size)
```

- [ ] **Step 4: 运行确认通过**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/test_portfolio_service.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/portfolio_service.py stock_selector_api/backend/tests/test_portfolio_service.py
git commit -m "feat(portfolio): analyze 幂等代理服务 + 双表落库 + runs 列表"
```

> 提交前调用 `code-compliance-check`。

---

### Task 5: API 层 + 挂载 `/api` + 修复昨日两个契约测试

**Files:**
- Rewrite: `stock_selector_api/backend/api/v1/portfolio.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`（移出 portfolio）
- Modify: `stock_selector_api/backend/main.py`（挂 `/api`）
- Rewrite: `stock_selector_api/backend/tests/test_portfolio.py`
- Modify: `stock_selector_api/backend/tests/test_portfolio_analyze.py`

- [ ] **Step 1: 重写 `backend/api/v1/portfolio.py`**

```python
"""持仓诊断契约路由（M1）：POST /api/portfolio/analyze + GET /api/portfolio/runs。

- R1-1/R1-2 判定与收益全部收敛插件，后端仅代理/落库/展示（零算术）；
- R2-3 幂等重放：同 (user_id, natural_day, holdings_md5) 复用 run_id；
- R3-1 列表按 user_id 隔离（M1 由入参/查询参数承载，鉴权收敛由 M3 补强）。
"""

from fastapi import APIRouter, Query

from core.response import ok, paged
from schemas.portfolio import AnalyzeBody
from services import portfolio_service

router = APIRouter(prefix="/portfolio", tags=["持仓诊断"])


@router.post("/analyze", summary="持仓诊断分析（代理插件，幂等重放）")
async def analyze(body: AnalyzeBody) -> dict:
    data = await portfolio_service.analyze(
        body.user_id, body.natural_day, [h.model_dump() for h in body.holdings]
    )
    return ok(data)


@router.get("/runs", summary="诊断 run 列表（按 user_id 隔离）")
async def list_runs(
    user_id: str = Query(description="属主 user_id"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=500),
) -> dict:
    items, total = await portfolio_service.list_runs(user_id, page, size)
    return ok(paged(items, total, page, size))
```

- [ ] **Step 2: 从 `backend/api/v1/router.py` 移出 portfolio**

删除 `    portfolio,\n`（import 组）与下面两行：

```python
# —— 示例业务模块 ——
api_router.include_router(devices.router, dependencies=_password_gate)
api_router.include_router(portfolio.router, dependencies=_password_gate)
```

改为：

```python
# —— 示例业务模块 ——
api_router.include_router(devices.router, dependencies=_password_gate)
```

> portfolio 改由 main.py 以 `/api` 前缀挂载（方案 §2/§3 的外部契约路由），不挂密码到期网关。

- [ ] **Step 3: main.py 挂载（`app.include_router(api_router, ...)` 之后、静态挂载之前）**

先加 import：`from api.v1.portfolio import router as portfolio_router`，再插入：

```python
    app.include_router(api_router, prefix=settings.api_prefix)
    # 持仓诊断外部契约路由（方案 §2/§3）：POST /api/portfolio/analyze · GET /api/portfolio/runs
    app.include_router(portfolio_router, prefix="/api")
```

- [ ] **Step 4: 重写 `backend/tests/test_portfolio.py` 为 conftest `client` fixture**

```python
"""M1 红线契约测试：持仓诊断 analyze 契约（幂等重放 / 用户隔离 / 判定收敛透传）。

重构：删除自建 app_fx（settings.database_url 在 conftest 中被置 None 会崩），
改用 conftest 的 client fixture（复用 session 级内存 SQLite，见 backend/tests/conftest.py）。
每个用例后清空诊断双表，保证计数断言不受跨用例污染。
"""

import pytest
from sqlalchemy import text

import db.base as dbb

ANALYZE_BODY = {
    "user_id": "user_p0_aho9",
    "natural_day": "2026-09-15",
    "portfolio_snapshot_md5": "md5_abc123",
    "holdings": [
        {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
        {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    ],
}


@pytest.fixture(autouse=True)
async def _clean_diag():
    yield
    async with dbb._engine.begin() as conn:
        await conn.execute(text("DELETE FROM portfolio_diag_results"))
        await conn.execute(text("DELETE FROM portfolio_diag_runs"))


async def _db_runs():
    async with dbb._engine.begin() as conn:
        rows = await conn.execute(text(
            "SELECT run_id, user_id, natural_day, status FROM portfolio_diag_runs"
        ))
        return rows.fetchall()


class TestAnalyzeContract:
    async def test_r2i_idempotent_replay_reuses_run_id(self, client):
        # 同 user_id+natural_day+快照 → 两次调用返回同一 run_id，且只建 1 条 run
        r1 = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r1.status_code == 200, r1.text
        run1 = r1.json()["data"]["run_id"]

        r2 = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r2.status_code == 200, r2.text
        run2 = r2.json()["data"]["run_id"]

        assert run1 == run2, "幂等重放必须复用既有 run_id"
        runs = await _db_runs()
        assert len(runs) == 1, f"幂等重放不得重复建 run，实际 {len(runs)} 条"
        assert runs[0].status == "completed"

    async def test_user_id_isolation_no_cross_user(self, client):
        # 用户 A 的 run 对用户 B 不可见（list 接口按 user_id 隔离）
        r = await client.post(
            "/api/portfolio/analyze",
            json={**ANALYZE_BODY, "user_id": "user_p0_aho9_owner"},
        )
        assert r.status_code == 200, r.text

        lr = await client.get("/api/portfolio/runs", params={"user_id": ANALYZE_BODY["user_id"]})
        assert lr.status_code == 200, lr.text
        data = lr.json()["data"]
        assert all(x["user_id"] == ANALYZE_BODY["user_id"] for x in data.get("items", [])), data

    async def test_backend_never_computes_judgment(self, client):
        # 判定零后端计算：结果每只都带插件 action 字段（sell/hold/add）与收益快照
        r = await client.post("/api/portfolio/analyze", json=ANALYZE_BODY)
        assert r.status_code == 200, r.text
        body = r.json()["data"]
        results = body.get("results", [])
        assert len(results) == 2, "每只持仓都要有判定结果"
        for item in results:
            assert item["action"] in {"sell", "hold", "add"}, item
            assert "latest_price" in item and "holding_pnl" in item
```

- [ ] **Step 5: 修正 `backend/tests/test_portfolio_analyze.py` 的 status 断言**

对齐方案 §2（analyze 为同步代理，成功后 status=completed）：

- 文档字符串第 5 行「出参仅 {run_id, status∈{queued,running}}」→「出参 {run_id, status, results, summary}；M1 同步代理成功后 status=completed」
- 第 35 行 `assert s1 == 200, f"RED 应 404：got {s1}: {j1}"` → `assert s1 == 200, f"analyze 应 200：got {s1}: {j1}"`
- 第 38 行 `assert data1["status"] in {"queued", "running"}` → `assert data1["status"] == "completed"`

- [ ] **Step 6: 运行两个契约测试文件确认通过**

Run (workdir `stock_selector_api/backend`):
`python -m pytest tests/test_portfolio.py tests/test_portfolio_analyze.py -q`
Expected: `5 passed`（3+2）

- [ ] **Step 7: Commit**

```bash
git add stock_selector_api/backend/api/v1/portfolio.py stock_selector_api/backend/api/v1/router.py stock_selector_api/backend/main.py stock_selector_api/backend/tests/test_portfolio.py stock_selector_api/backend/tests/test_portfolio_analyze.py
git commit -m "feat(portfolio): analyze/runs API 挂载 /api + 修复 M1 契约测试（5 用例绿）"
```

> 提交前调用 `code-compliance-check`。

---

### Task 6: 全量回归 + 自检

**Files:**
- 无源文件改动（除非回归发现 bug）

- [ ] **Step 1: 全量回归**

Run (workdir `stock_selector_api/backend`): `python -m pytest tests/ -q`
Expected: 全部通过（既有 backtest/notify/auth/devices 测试 + 本计划新增 ~15 用例）。若有回归，用 `systematic-debugging` 定位修复后再跑。

- [ ] **Step 2: lint 自检（若 ruff 已安装；未安装则跳过）**

Run (workdir `stock_selector_api/backend`): `python -m ruff check .`
Expected: `All checks passed!`（无告警；若有则修复）

- [ ] **Step 3: 启动冒烟（可选，验证真实 app 能加载新路由）**

Run (workdir `stock_selector_api/backend`):
`python -c "from main import create_app; app=create_app(); print([r.path for r in app.routes if 'portfolio' in getattr(r,'path','')])"`
Expected: 打印 `['/api/portfolio/analyze', '/api/portfolio/runs']`

- [ ] **Step 4: Commit（若 Step 1/2 有修复）**

```bash
git add -A stock_selector_api/
git commit -m "chore(portfolio): M1 全量回归修复"
```

---

## 验收标准汇总

1. `backend/tests/test_portfolio_db.py` 4 用例绿（双表 + 唯一约束）
2. `backend/tests/test_portfolio_repo.py` 2 用例绿（幂等键查找 / 明细关联）
3. `backend/tests/test_portfolio_plugin.py` 2 用例绿（mock 判定 + 确定性）
4. `backend/tests/test_portfolio_service.py` 4 用例绿（幂等 / 隔离 / 透传）
5. `backend/tests/test_portfolio.py` + `backend/tests/test_portfolio_analyze.py` 5 用例绿（API 级契约）
6. `backend/tests/ -q` 全量回归通过
7. `import` 冒烟打印 `/api/portfolio/analyze` / `/api/portfolio/runs` 两条路由

## 风险与注意

- **幂等键由服务端算**：`holdings_md5` 以实际 holdings 规范化计算，客户端传入的 `user_holdings_md5/portfolio_snapshot_md5` 仅兼容放行、不参与键构造。
- **M1 同步代理**：未做异步任务/重试 3 次退避/失败双路通知（M3/M4 范畴，方案 §5 阶段划分）。
- **`plugin_mode` 默认 mock**：真实插件未实现前本地/测试都走 mock；M2 接通后 .env 置 `PLUGIN_MODE=http`。
- **路由前缀**：方案契约明确 `/api/portfolio/*`，不走 `/api/v1`；analyze 为系统入口，不挂密码到期网关。
- **测试须在 `stock_selector_api/backend` 目录运行**（conftest 依赖 backend/ 在 sys.path）。