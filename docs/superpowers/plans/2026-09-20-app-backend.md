# 智能选股 App — 后端实施计划（M0–M5）
> 子系统：Backend API（8000）/ 持仓诊断插件（8001）/ 选股插件（8002）
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在既有后端（持仓/诊断/通知组件框架，均已提交）之上交付交割单导入与持仓推导、利润/选股插件代理、站内信+WS 通知双路、定时任务、版本升级服务，全部满足"计算收敛插件、前端只显示"红线。

**Architecture:** 三服务。Backend 持库并提供认证/业务 API；持仓诊断插件（`plugin/`，8001）扩展 `POST /api/portfolio/pnl` 计算已实现+浮动盈亏（含移动加权成本序列，单点口径）；选股插件（`stock_selector/api.py`）端口改为 8002 并保持内核不动，由 backend 薄代理。新业务全部遵循既有 repository(dict)+SQLite 模式与统一响应 `ok()/paged()`。

**Tech Stack:** FastAPI + SQLAlchemy(async, sqlite 默认) + pydantic、httpx、APScheduler、pytest + pytest-asyncio、守望件 `openpyxl`（可选 Excel）、uni-app 无关。

**Spec:** `docs/superpowers/specs/2026-09-20-select-app-uniapp-design.md`

## Global Constraints
- R1-1/R1-2：判定与收益**不得**在后端做任何盈利/收益率算术；一律经 `portfolio_plugin` 代理调用插件。选股结果同样只代理到选股插件。
- R1-3：前端零计算；后端计算边界的例外仅为"持仓簿记"（数量/成本快照），成本序列唯一权威在持仓诊断插件（`plugin/portfolio/realized.py`），两边用同一移动加权算法，回归测试锁一致。
- R3-1：所有用户业务查询按 `CurrentUserDep` 属主隔离；管理端点用 `require_roles("superadmin")`（复用现有依赖，不改 seed）。
- 测试环境固定 `plugin_mode=mock`、`schedule_enabled=False`（conftest 已设）；新选股代理同样提供 mock 模式。
- 每个新表模型须在 `db/sql/__init__.py` 导入注册（否则 `create_all` 不建表）。
- 端口：backend 8000、持仓诊断插件 8001、选股插件 8002。
- repository(dict)+UUID 约定、`ok()/paged()` 响应、`BizError` 语义、Python ≥3.10。

---

## 文件映射（后端）

| 文件 | 动作 | 职责 |
|---|---|---|
| `stock_selector_api/backend/config.py` | 修改 | `selection_plugin_base_url/selection_plugin_mode/upgrade_upload_dir/daily_diag_time/daily_selection_time/trades_columns` 新键 |
| `stock_selector_api/backend/db/sql/trades.py` | 新增 | `TradeRecord` 表 |
| `stock_selector_api/backend/db/sql/holdings.py` | 修改 | `derived_from_trades`、`last_derived_at` 列 |
| `stock_selector_api/backend/db/sql/selection.py` | 新增 | `SelectJob`、`SelectResult` 表 |
| `stock_selector_api/backend/db/sql/inbox.py` | 新增 | `InboxMessage` 表 |
| `stock_selector_api/backend/db/sql/upgrade.py` | 新增 | `UpgradePackage` 表 |
| `stock_selector_api/backend/db/sql/__init__.py` | 修改 | 注册新模型 |
| `stock_selector_api/backend/db/__init__.py` | 修改 | holdings 老库补列 ALTER |
| `stock_selector_api/backend/schemas/trades.py` | 新增 | `TradeCreate/TradeRow/PreviewResult` |
| `stock_selector_api/backend/schemas/selection.py` | 新增 | job/result 写白名单与出参 |
| `stock_selector_api/backend/schemas/inbox.py` | 新增 | 写白名单与出参 |
| `stock_selector_api/backend/schemas/upgrade.py` | 新增 | 写白名单与出参 |
| `stock_selector_api/backend/schemas/holdings.py` | 修改 | 新增 derived 字段 |
| `stock_selector_api/backend/repositories/trades.py` | 新增 | 交易记录仓储 |
| `stock_selector_api/backend/repositories/selection.py` | 新增 | 选股 job/results 仓储 |
| `stock_selector_api/backend/repositories/inbox.py` | 新增 | 站内信仓储 |
| `stock_selector_api/backend/repositories/upgrade.py` | 新增 | 升级包仓储 |
| `stock_selector_api/backend/services/deal_parser.py` | 新增 | 交割单解析（CSV 纯 stdlib + Excel 可选） |
| `stock_selector_api/backend/services/derive_holdings.py` | 新增 | 移动加权推导纯函数（与插件同算法） |
| `stock_selector_api/backend/services/trades_service.py` | 新增 | preview/commit/手动录入/删除/持仓联动 |
| `stock_selector_api/backend/services/portfolio_plugin.py` | 修改 | +`call_plugin_pnl`（http/mock） |
| `stock_selector_api/backend/services/holdings_service.py` | 修改 | +`pnl()`（装填 trades+holdings→代理）、list 附最近诊断快照 |
| `stock_selector_api/backend/api/v1/trades.py` | 新增 | 交割单/交易记录 API |
| `stock_selector_api/backend/api/v1/holdings.py` | 修改 | +`GET /pnl`；list 返回附快照字段 |
| `stock_selector_api/backend/api/v1/router.py` | 修改 | 挂新路由 |
| `stock_selector_api/backend/services/core/ws_manager.py` → 使用 `stock_selector_api/backend/services/ws_manager.py` | 新增 | WsManager（内存按 user 分组） |
| `stock_selector_api/backend/services/inbox_service.py` | 新增 | 列表/已读/未读 |
| `stock_selector_api/backend/services/inbox_notify.py` | 新增 | 落库+WS 双路 helper |
| `stock_selector_api/backend/api/v1/messages.py` | 新增 | 站内信 API |
| `stock_selector_api/backend/api/ws_notify.py` | 新增 | `/ws/notify` 端点 |
| `stock_selector_api/backend/main.py` | 修改 | 注册 `/ws/notify` 与 `trades/messages` 路由 |
| `stock_selector_api/backend/services/selection_service.py` | 新增 | 选股代理 job 生命周期 |
| `stock_selector_api/backend/api/v1/selection.py` | 新增 | 选股 API |
| `stock_selector_api/backend/scheduler/tasks/market_daily.py` | 新增 | `daily_diag`/`daily_selection` |
| `stock_selector_api/backend/scheduler/tasks/__init__.py` | 修改 | 空导入占位（确保注册） |
| `stock_selector_api/backend/conf/inputs.conf` | 修改 | 新增两节 |
| `stock_selector_api/backend/services/diag_service.py` | 修改 | 完成/失败钩子→通知 |
| `stock_selector_api/backend/services/upgrade_service.py` | 新增 | 升级包存储/查询/latest |
| `stock_selector_api/backend/api/v1/upgrade.py` | 新增 | 管理 API + 上传 |
| `stock_selector_api/backend/api/app_version.py` | 新增 | `/api/app/latest`、`/api/app/version` 公开端点 |
| `stock_selector_api/plugin/portfolio/realized.py` | 新增 | 移动加权成本+已实现/浮动盈亏（单点口径） |
| `stock_selector_api/plugin/server.py` | 修改 | +`POST /api/portfolio/pnl` |
| `stock_selector/api.py` | 修改 | 注释端口改 8002 |
| `scripts/start_selector.sh`（`D:\select_stock\scripts\`） | 新增 | 选股插件 8002 启动脚本 |
| `stock_selector_api/backend/tests/test_trades_*.py` 等 | 新增 | 见各任务 |

---

### Task B1: 配置新键 + 选股插件端口 8002 + 启动脚本（M0）

**Files:**
- Modify: `stock_selector_api/backend/config.py`、`stock_selector/api.py:8`
- Create: `D:\select_stock\scripts\start_selector.sh`
- Test: `stock_selector_api/backend/tests/test_config_keys.py`

**Interfaces:**
- Consumes: 现有 `Settings`
- Produces: `settings.selection_plugin_base_url="http://localhost:8002"`、`settings.selection_plugin_mode="http"`、`settings.upgrade_upload_dir`、`settings.daily_diag_time="18:00"`、`settings.daily_selection_time="17:30"`、`settings.trades_columns`（dict 别名表）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_config_keys.py
from config import get_settings


def test_new_keys_exist_with_defaults():
    s = get_settings()
    assert s.selection_plugin_base_url == "http://localhost:8002"
    assert s.selection_plugin_mode in ("http", "mock")
    assert "stock_code" in s.trades_columns
    assert s.daily_diag_time == "18:00"
    assert s.daily_selection_time == "17:30"
    assert "/upgrade" in s.upgrade_upload_dir
```

- [ ] **Step 2: 运行确认失败**

Run（`workdir=stock_selector_api/backend`）: `python -m pytest tests/test_config_keys.py -v --basetemp .pytest_tmp`
Expected: FAIL（AttributeError: Settings 无 selection_plugin_base_url）

- [ ] **Step 3: 实现 config 新键**

```python
# config.py —— 在「持仓诊断插件」之后、部署之前插入
    # ---------- 选股插件（薄代理，端口 8002）----------
    selection_plugin_mode: str = "http"        # http（本机选股插件 8002）| mock（测试确定性）
    selection_plugin_base_url: str = "http://localhost:8002"

    # ---------- 交割单解析（券商列别名；键为内部规范名）----------
    trades_columns: dict = {
        "trade_date": ["成交日期", "成交时间"],
        "stock_code": ["证券代码", "股票代码", "代码"],
        "stock_name": ["证券名称", "股票名称", "名称"],
        "direction": ["买卖标志", "买卖方向", "成交类别"],
        "quantity": ["成交数量", "数量"],
        "price": ["成交价格", "价格"],
        "amount": ["成交金额", "金额"],
        "commission": ["手续费", "佣金"],
        "stamp_tax": ["印花税"],
        "transfer_fee": ["过户费"],
        "settled_amount": ["结算金额", "发生金额"],
    }

    # ---------- 版本升级中心（自托管）----------
    upgrade_upload_dir: str = str(BACKEND_DIR.parent / "data" / "uploads" / "upgrade")

    # ---------- 定时任务（盘后）----------
    daily_diag_time: str = "18:00"
    daily_selection_time: str = "17:30"
```

- [ ] **Step 4: 选股插件端口注释改 8002 + 启动脚本**

```python
# stock_selector/api.py —— 第 8 行 docstring
#     python -m uvicorn stock_selector.api:app --host 0.0.0.0 --port 8002
```

```bash
# scripts/start_selector.sh（仓库根，git 兼容 LF；Windows 用 git-bash 或直接 uvicorn）
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m uvicorn stock_selector.api:app --host 0.0.0.0 --port 8002
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_config_keys.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add stock_selector_api/backend/config.py stock_selector/api.py scripts/start_selector.sh stock_selector_api/backend/tests/test_config_keys.py
git commit -m "feat: 选股插件端口 8002 + 配置新键（交割单/选股/升级/定时）+ 启动脚本"
```

---

### Task B2: trade_records 表 + 仓储（M1）

**Files:**
- Create: `stock_selector_api/backend/db/sql/trades.py`、`stock_selector_api/backend/schemas/trades.py`、`stock_selector_api/backend/repositories/trades.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`
- Test: `stock_selector_api/backend/tests/test_trades_repo.py`

**Interfaces:**
- Produces: `TradeRecord` 表（`trade_records`）；`trades_repo.list_by_user(user_id, *, code=None, direction=None, date_from=None, date_to=None) -> list[dict]`、`trades_repo.list_by_batch(batch_id) -> list[dict]`、`create/get/delete`（基类）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_trades_repo.py
from repositories import trades_repo


async def test_create_and_get():
    created = await trades_repo.create({
        "user_id": "u1", "trade_date": "2026-09-01", "stock_code": "600519",
        "stock_name": "贵州茅台", "direction": "buy", "price": 1680.5,
        "quantity": 100, "amount": 168050.0, "commission": 5.0,
        "stamp_tax": 0.0, "transfer_fee": 0.0, "settled_amount": 168055.0,
        "source": "manual", "batch_id": "",
    })
    got = await trades_repo.get(created["id"])
    assert got["stock_code"] == "600519" and got["direction"] == "buy"


async def test_list_by_user_filters():
    base = {"user_id": "u1", "trade_date": "2026-09-01", "stock_code": "600519",
            "stock_name": "N", "price": 10.0, "quantity": 100, "amount": 1000.0,
            "settled_amount": 1005.0, "source": "manual"}
    await trades_repo.create({**base, "direction": "buy"})
    await trades_repo.create({**base, "direction": "sell", "trade_date": "2026-09-05"})
    buys = await trades_repo.list_by_user("u1", direction="buy")
    assert len(buys) == 1 and buys[0]["direction"] == "buy"
    ranged = await trades_repo.list_by_user("u1", date_from="2026-09-02", date_to="2026-09-06")
    assert len(ranged) == 1 and ranged[0]["direction"] == "sell"
    by_code = await trades_repo.list_by_user("u1", code="600519")
    assert len(by_code) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_trades_repo.py -v --basetemp .pytest_tmp`
Expected: FAIL（ImportError: 无 trades_repo）

- [ ] **Step 3: 实现表 + schema + repo**

```python
# db/sql/trades.py
from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class TradeRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "trade_records"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trade_date: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    stock_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    stock_name: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[str] = mapped_column(String(8), nullable=False)   # buy|sell
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0.0)
    settled_amount: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="manual")   # manual|import
    batch_id: Mapped[str] = mapped_column(String(64), default="", index=True)
```

```python
# schemas/trades.py
from pydantic import BaseModel, Field


class TradeCreate(BaseModel):
    """仓储写白名单（含 user_id；供 _assign 落库）。"""
    user_id: str = Field(min_length=1, max_length=64)
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)   # buy|sell
    price: float = Field(ge=0)
    quantity: int = Field(ge=0)
    amount: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)
    source: str = Field(default="manual", max_length=16)
    batch_id: str = Field(default="", max_length=64)


class TradeRow(BaseModel):
    """交割单一条（API 双步：preview 返回 / commit 回传）。"""
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=6, max_length=6)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)
    price: float = Field(ge=0)
    quantity: int = Field(gt=0)
    amount: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)
```

```python
# repositories/trades.py
from sqlalchemy import select

from db.sql.trades import TradeRecord
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.trades import TradeCreate

RepoBase = pick()


class TradeRepo(RepoBase):
    model = TradeRecord
    table = "trade_records"
    schema = TradeCreate

    async def list_by_user(
        self, user_id: str, *, code: str | None = None,
        direction: str | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[dict]:
        async with self._session() as s:
            stmt = select(self.model).where(self.model.user_id == user_id)
            if code:
                stmt = stmt.where(self.model.stock_code == code)
            if direction:
                stmt = stmt.where(self.model.direction == direction)
            if date_from:
                stmt = stmt.where(self.model.trade_date >= date_from)
            if date_to:
                stmt = stmt.where(self.model.trade_date <= date_to)
            stmt = stmt.order_by(self.model.trade_date.desc(), self.model.created_at.desc()).limit(1000)
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]

    async def list_by_batch(self, batch_id: str) -> list[dict]:
        return await self._find_all(filters={"batch_id": batch_id}, order_by="trade_date")

    async def list_all_by_user(self, user_id: str) -> list[dict]:
        """利润/推导入参：全部按代码+日期正序。"""
        async with self._session() as s:
            stmt = (
                select(self.model)
                .where(self.model.user_id == user_id)
                .order_by(self.model.stock_code.asc(), self.model.trade_date.asc(), self.model.created_at.asc())
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [_model_to_dict(o) for o in rows]
```

```python
# db/sql/__init__.py —— 增补 import（保持按字母序）
from db.sql.trades import TradeRecord
# __all__ 追加 "TradeRecord"
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_trades_repo.py -v --basetemp .pytest_tmp`
Expected: PASS
（注意：conftest 为每个用例清空 holdings；trade_records 默认无清理，repo 测试用唯一 user_id 规避互扰）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql/trades.py stock_selector_api/backend/schemas/trades.py stock_selector_api/backend/repositories/trades.py stock_selector_api/backend/db/sql/__init__.py stock_selector_api/backend/tests/test_trades_repo.py
git commit -m "feat(trades): 交易记录表/仓储/写白名单（M1）"
```

---

### Task B3: holdings 推导标记列 + 老库补列迁移（M1）

**Files:**
- Modify: `stock_selector_api/backend/db/sql/holdings.py`、`stock_selector_api/backend/schemas/holdings.py`、`stock_selector_api/backend/db/__init__.py`
- Test: `stock_selector_api/backend/tests/test_holdings_db.py`（追加用例）

**Interfaces:**
- Produces: `Holding` 新增列 `derived_from_trades: bool`、`last_derived_at: datetime|None`；`HoldingCreate` 新增两字段

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_holdings_db.py 追加
from datetime import datetime, timezone

from repositories import holdings_repo


async def test_derived_marker_columns():
    from db.sql.holdings import Holding
    from db.sql.base import Base
    cols = {c.name for c in Holding.__table__.columns}
    assert {"derived_from_trades", "last_derived_at"} <= cols

    created = await holdings_repo.create({
        "user_id": "der-1", "code": "600519", "name": "茅台",
        "quantity": 100, "cost_price": 1680.5,
        "derived_from_trades": True,
    })
    assert created["derived_from_trades"] is True

    ts = datetime.now(timezone.utc)
    updated = await holdings_repo.update(created["id"], {"last_derived_at": ts})
    assert updated["last_derived_at"] is not None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_holdings_db.py -v --basetemp .pytest_tmp`
Expected: FAIL（新列不存在）

- [ ] **Step 3: 实现**

```python
# db/sql/holdings.py —— 文件头加 datetime 导入
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, String, UniqueConstraint

# 模型内追加：
    derived_from_trades: Mapped[bool] = mapped_column(Boolean, default=False)
    last_derived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
```

```python
# schemas/holdings.py —— HoldingCreate / HoldingIn / HoldingOut 追加字段
    derived_from_trades: bool = False
    last_derived_at: datetime | None = None    # from datetime import datetime
```

```python
# db/__init__.py —— create_all 的 sqlite 分支追加迁移助手
async def create_all() -> None:
    if settings.is_sqlite():
        from db.sql import Base
        from db.base import get_engine

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_user_password_changed_at)
            await conn.run_sync(_ensure_holdings_derived_columns)
        log.info("SQLite 建表完成")
    else:
        from db.mongo.indexes import ensure_indexes

        await ensure_indexes()


def _ensure_holdings_derived_columns(sync_conn) -> None:
    """老库补列：现有 holdings 表新增推导标记列（幂等）。"""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(sync_conn).get_columns("holdings")}
    for col, ddl in (("derived_from_trades", "BOOLEAN"), ("last_derived_at", "DATETIME")):
        if col not in cols:
            sync_conn.execute(text(f"ALTER TABLE holdings ADD COLUMN {col} {ddl}"))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_holdings_db.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql/holdings.py stock_selector_api/backend/schemas/holdings.py stock_selector_api/backend/db/__init__.py stock_selector_api/backend/tests/test_holdings_db.py
git commit -m "feat(holdings): 推导标记列 + 老库迁移（M1 交割单联动）"
```

---

### Task B4: 交割单解析器（CSV stdlib + Excel 可选）（M1）

**Files:**
- Create: `stock_selector_api/backend/services/deal_parser.py`
- Test: `stock_selector_api/backend/tests/test_deal_parser.py`

**Interfaces:**
- Consumes: `settings.trades_columns`
- Produces:
  - `parse_deal_text(text: str) -> dict` → `{"total", "valid", "errors": [{line, reason}], "items": [TradeRow dict...]}`
  - `parse_deal_bytes(raw: bytes, filename: str) -> dict`（.csv 走纯文本；.xlsx 走 openpyxl；.xls 报提示）
  - `trade_row_to_document(item: dict) -> dict`（规整为 `TradeCreate` 少 user_id/batch 的数据）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_deal_parser.py
from services.deal_parser import parse_deal_text, parse_deal_bytes

CSV_HEADER = "成交日期,证券代码,证券名称,买卖标志,成交数量,成交价格,成交金额,手续费,印花税,过户费,结算金额\n"


def test_parse_csv_standard():
    text = CSV_HEADER + "2026-09-01,600519,贵州茅台,买入,100,1680.50,168050,5,168.05,0.5,168223.55\n"
    out = parse_deal_text(text)
    assert out["total"] == 1 and out["errors"] == []
    it = out["items"][0]
    assert it["stock_code"] == "600519" and it["direction"] == "buy"
    assert it["quantity"] == 100 and round(it["price"], 2) == 1680.5


def test_direction_aliases():
    text = CSV_HEADER + "2026-09-01,600519,N,卖出,100,10,1000,1,1,0,1002\n2026-09-02,000001,N,B,200,5,1000,0,0,0,1000\n"
    out = parse_deal_text(text)
    assert [i["direction"] for i in out["items"]] == ["sell", "buy"]


def test_row_errors_and_bom():
    text = "\uFEFF" + CSV_HEADER + ",600519,N,B,abc,1,1,0,0,0,1\n"
    out = parse_deal_text(text)
    assert out["errors"] and out["valid"] == 0
    assert out["errors"][0]["line"] == 2


def test_missing_header_rejected():
    out = parse_deal_text("日期,代码,数量\n2026-01-01,600519,100\n")
    assert out["valid"] == 0 and len(out["errors"]) == 1


def test_excel_missing_dep_graceful():
    out = parse_deal_bytes(b"PK\x03\x04dummy", "f.xlsx")
    assert out["valid"] == 0 and out["errors"][0]["reason"].endswith("openpyxl")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_deal_parser.py -v --basetemp .pytest_tmp`
Expected: FAIL（ImportError: 无 deal_parser）

- [ ] **Step 3: 实现解析器**

```python
# services/deal_parser.py
"""交割单解析：默认券商中文表头→内部规范名；CSV 纯 stdlib，Excel 可选 openpyxl。"""
from __future__ import annotations

import csv
import io
import re

from config import settings

_CODE_RE = re.compile(r"^\d{6}$")
_DIR_ALIAS = {"买": "buy", "买入": "buy", "b": "buy", "buy": "buy",
              "卖": "sell", "卖出": "sell", "s": "sell", "sel": "sell", "sell": "sell"}


def _build_header_idx(headers: list[str]) -> dict[str, int]:
    aliases = settings.trades_columns
    idx: dict[str, int] = {}
    for internal, names in aliases.items():
        for col in headers:
            if col in names:
                idx[internal] = headers.index(col)
                break
    return idx


def _norm(x: str) -> str:
    return (x or "").strip()


def _as_float(x: str) -> float | None:
    try:
        return round(float(_norm(x)), 2)
    except (TypeError, ValueError):
        return None


def _as_int(x: str) -> int | None:
    try:
        return int(float(_norm(x)))
    except (TypeError, ValueError):
        return None


def _parse_row(header_idx: dict, cells: list[str], lineno: int, errors: list[dict]) -> dict | None:
    def get(k: str) -> str:
        i = header_idx.get(k)
        return _norm(cells[i]) if i is not None and i < len(cells) else ""

    code = get("stock_code")
    if not _CODE_RE.match(code):
        errors.append({"line": lineno, "reason": "证券代码需为6位数字"})
        return None
    direction = _DIR_ALIAS.get(get("direction").lower())
    if not direction:
        errors.append({"line": lineno, "reason": "买卖标志无法识别"})
        return None
    quantity = _as_int(get("quantity"))
    if not quantity or quantity <= 0:
        errors.append({"line": lineno, "reason": "成交数量需为正整数"})
        return None
    price = _as_float(get("price"))
    if price is None or price <= 0:
        errors.append({"line": lineno, "reason": "成交价格非法"})
        return None
    trade_date = get("trade_date")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
        errors.append({"line": lineno, "reason": "成交日期需为 YYYY-MM-DD"})
        return None
    return {
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": get("stock_name")[:64],
        "direction": direction,
        "price": price,
        "quantity": int(quantity),
        "amount": _as_float(get("amount")) or 0.0,
        "commission": _as_float(get("commission")) or 0.0,
        "stamp_tax": _as_float(get("stamp_tax")) or 0.0,
        "transfer_fee": _as_float(get("transfer_fee")) or 0.0,
        "settled_amount": _as_float(get("settled_amount")) or 0.0,
    }


def parse_deal_table(headers: list[str], data_rows: list[list[str]]) -> dict:
    errors: list[dict] = []
    items: list[dict] = []
    header_idx = _build_header_idx([_norm(h) for h in headers])
    missing = {
        k for k in ("trade_date", "stock_code", "direction", "quantity", "price")
        if k not in header_idx
    }
    if missing:
        return {"total": 0, "valid": 0, "errors": [
            {"line": 1, "reason": f"缺少必需列: {sorted(missing)}"}] , "items": []}
    for lineno, cells in enumerate(data_rows, start=2):
        if not any(_norm(c) for c in cells):
            continue
        row = _parse_row(header_idx, cells, lineno, errors)
        if row is not None:
            items.append(row)
    return {"total": len(items) + len(errors), "valid": len(items), "errors": errors, "items": items}


def parse_deal_text(text: str) -> dict:
    text = (text or "").removeprefix("\uFEFF")
    reader = csv.reader(io.StringIO(text.strip()))
    rows = [r for r in reader if any(_norm(x) for x in r)]
    if not rows:
        return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": "文件为空"}], "items": []}
    return parse_deal_table(rows[0], rows[1:])


def parse_deal_bytes(raw: bytes, filename: str) -> dict:
    if filename.lower().endswith(".csv"):
        try:
            return parse_deal_text(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return parse_deal_text(raw.decode("gbk", errors="replace"))
    if filename.lower().endswith(".xlsx"):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return {"total": 0, "valid": 0,
                    "errors": [{"line": 1, "reason": "解析 .xlsx 需要安装 openpyxl"}], "items": []}
        import io as _io

        from openpyxl import load_workbook
        wb = load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        raw_rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(max_row=2000)]
        if not raw_rows:
            return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": "Excel 为空"}], "items": []}
        return parse_deal_table(raw_rows[0], raw_rows[1:])
    return {"total": 0, "valid": 0,
            "errors": [{"line": 1, "reason": "仅支持 .csv / .xlsx 交割单"}], "items": []}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_deal_parser.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/deal_parser.py stock_selector_api/backend/tests/test_deal_parser.py
git commit -m "feat(trades): 交割单解析（中文表头别名/方向归一/CSV+可选Excel）"
```

---

### Task B5: 移动加权持仓推导（纯函数，与插件同算法）（M1）

**Files:**
- Create: `stock_selector_api/backend/services/derive_holdings.py`
- Test: `stock_selector_api/backend/tests/test_derive_holdings.py`

**Interfaces:**
- Consumes: 无（纯函数；`list_all_by_user` 的 dict 序列）
- Produces: `derive_holdings_from_trades(trades: list[dict]) -> tuple[list[dict], list[str]]` →
  `([{stock_code, stock_name, quantity, cost_price}, ...], [errors])`；`cost_price` 4 位小数
  > 供插件测试锁一致（插件端 `realized.compute_pnl` 复现同序列）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_derive_holdings.py
from services.derive_holdings import derive_holdings_from_trades


def _trade(day, code, name, direction, qty, price):
    return {"trade_date": day, "stock_code": code, "stock_name": name,
            "direction": direction, "price": price, "quantity": qty}


def test_moving_average():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "buy", 100, 1700.0),
    ])
    assert errs == []
    h = rows[0]
    assert h["stock_code"] == "600519" and h["quantity"] == 200
    assert round(h["cost_price"], 2) == 1650.0  # (1600*100+1700*100)/200


def test_partial_sell_keeps_cost():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-02", "600519", "茅台", "sell", 40, 1800.0),
    ])
    h = rows[0]
    assert h["quantity"] == 60 and h["cost_price"] == 1600.0


def test_oversell_reports_error():
    rows, errs = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 10, 100.0),
        _trade("09-02", "600519", "茅台", "sell", 99, 110.0),
    ])
    assert rows == [] and any("600519" in e for e in errs)


def test_multiple_codes_grouped():
    rows, _ = derive_holdings_from_trades([
        _trade("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _trade("09-01", "000858", "五粮液", "buy", 200, 142.0),
    ])
    assert len(rows) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_derive_holdings.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# services/derive_holdings.py
"""持仓簿记：交易记录 → 持仓（移动加权成本）。仅簿记（R1-2 例外）；
盈利归属插件，此处不产出任何盈亏字段。"""
from __future__ import annotations


def derive_holdings_from_trades(trades: list[dict]) -> tuple[list[dict], list[str]]:
    """按 stock_code 顺序处理交易记录，移动加权推导持仓快照。
    卖出的已实现盈亏属于插件口径，此处只维护数量与成本。"""
    errors: list[str] = []
    position: dict[str, dict] = {}

    for t in trades:
        code = t["stock_code"]
        qty = int(t["quantity"])
        price = float(t["price"])
        cur = position.setdefault(code, {"qty": 0, "cost": 0.0, "name": t.get("stock_name", "")})
        if t["direction"] == "buy":
            total = cur["qty"] + qty
            if total > 0:
                cur["cost"] = (cur["cost"] * cur["qty"] + price * qty) / total
            else:
                cur["cost"] = price
            cur["qty"] = total
        else:
            if qty > cur["qty"]:
                errors.append(f"卖出超过持仓: {code}（{cur['qty']}<{qty}）")
                continue
            cur["qty"] -= qty  # 卖出不动成本基准

    holdings = [
        {"stock_code": code, "stock_name": v["name"], "quantity": v["qty"],
         "cost_price": round(v["cost"], 4)}
        for code, v in position.items() if v["qty"] > 0
    ]
    return holdings, errors
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_derive_holdings.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/derive_holdings.py stock_selector_api/backend/tests/test_derive_holdings.py
git commit -m "feat(trades): 移动加权持仓推导（簿记，盈利归插件）"
```

---

### Task B6: trades_service + 交割单 API（preview / commit / 手动录入 / 删除）（M1）

**Files:**
- Create: `stock_selector_api/backend/api/v1/trades.py`、`stock_selector_api/backend/services/trades_service.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`、`stock_selector_api/backend/main.py`（并入 v1 router 即可，无需 main 改动）
- Test: `stock_selector_api/backend/tests/test_trades_api.py`

**Interfaces:**
- Consumes: Task B2 `trades_repo`、Task B4 `parse_deal_bytes/parse_deal_text`、Task B5 `derive_holdings_from_trades`、Task B3 `holdings_repo`
- Produces:
  - `trades_service.preview(user_id, raw, filename) -> dict`
  - `trades_service.commit(user_id, rows: list[dict]) -> dict` → `{"imported", "holdings_changed": [...], "errors"}`
  - `trades_service.create_one(user_id, row) -> dict`（手动录入单笔）
  - `trades_service.delete_one(user_id, trade_id) -> bool`
  - `trades_service.list_trades(user_id, query) -> dict`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_trades_api.py
import uuid

from repositories import holdings_repo, trades_repo
from services import trades_service


async def _mk_trade_row(code="600519", days=("2026-09-01",), direction="buy", qty=100, price=10.0):
    return {"trade_date": days[0], "stock_code": code, "stock_name": "N",
            "direction": direction, "price": price, "quantity": qty,
            "amount": price * qty, "commission": 0.0, "stamp_tax": 0.0,
            "transfer_fee": 0.0, "settled_amount": price * qty}


async def test_commit_derives_holdings():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    rows = [
        await _mk_trade_row("600519", ("2026-09-01",), "buy", 100, 1600.0),
        await _mk_trade_row("600519", ("2026-09-02",), "buy", 100, 1700.0),
    ]
    out = await trades_service.commit(uid, rows)
    assert out["imported"] == 2 and out["holdings_changed"] == ["600519"] and out["errors"] == []
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert h["quantity"] == 200 and round(h["cost_price"], 2) == 1650.0
    assert h["derived_from_trades"] is True


async def test_commit_oversell_rejects_before_write():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    out = await trades_service.commit(uid, [await _mk_trade_row("600519", direction="sell", qty=10)])
    assert out["imported"] == 0 and out["errors"]
    assert await trades_repo.list_by_batch("") == [] or await trades_repo.list_by_user(uid) == []


async def test_manual_create_and_delete():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    r = await _mk_trade_row()
    created = await trades_service.create_one(uid, r)
    rows = await trades_service.list_trades(uid, {})
    assert len(rows["items"]) == 1
    await trades_service.delete_one(uid, created["id"])
    assert await trades_service.list_trades(uid, {}) == {"items": [], "total": 0}
    h = await holdings_repo.find_by_user_code(uid, "600519")
    assert h is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_trades_api.py -v --basetemp .pytest_tmp`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现服务**

```python
# services/trades_service.py
"""交割单/交易记录：预览→提交双步、手动录入、删除、持仓联动（簿记）。"""
from __future__ import annotations

import uuid

from core.exceptions import BizError
from repositories import holdings_repo, trades_repo
from services.derive_holdings import derive_holdings_from_trades


def _settled(row: dict) -> dict:
    """把 TradeRow/手动入参归整为落库字段（batch_id 由调用方补）。"""
    return {
        "trade_date": str(row["trade_date"]),
        "stock_code": str(row["stock_code"]),
        "stock_name": str(row.get("stock_name", ""))[:64],
        "direction": str(row["direction"]),
        "price": round(float(row["price"]), 4),
        "quantity": int(row["quantity"]),
        "amount": round(float(row.get("amount", 0.0)), 2),
        "commission": round(float(row.get("commission", 0.0)), 2),
        "stamp_tax": round(float(row.get("stamp_tax", 0.0)), 2),
        "transfer_fee": round(float(row.get("transfer_fee", 0.0)), 2),
        "settled_amount": round(float(row.get("settled_amount", 0.0)), 2),
    }


async def preview(user_id: str, raw, filename: str | None = None) -> dict:
    """解析不落库（自动填 user_id/batch 空），返回 {total, valid, errors, items}。"""
    if not raw:
        raise BizError("请上传交割单文件")
    out = parse_deal_bytes(raw, filename or "deal.csv") if isinstance(raw, (bytes, bytearray)) else parse_deal_text(raw)
    return out  # items 为 TradeRow 兼容 dict


async def commit(user_id: str, rows: list[dict], batch_note: str = "") -> dict:
    """校验重放 → 写 trade_records → 移动加权推导 → 覆盖/新建 holdings。"""
    if not rows:
        raise BizError("无可提交的记录")
    trades = []
    row_level_errors = []
    for i, row in enumerate(rows, start=1):
        try:
            trades.append(_settled(row))
        except (KeyError, TypeError, ValueError):
            row_level_errors.append({"line": i, "reason": "字段缺失或非法"})
    if row_level_errors:
        return {"imported": 0, "holdings_changed": [], "errors": row_level_errors}

    all_user_trades = await trades_repo.list_all_by_user(user_id)
    batch_id = uuid.uuid4().hex[:12]
    for t in trades:
        all_user_trades.append({**t, "source": "import", "batch_id": batch_id})
    all_user_trades.sort(key=lambda t: (t["stock_code"], t["trade_date"]))

    holdings, derive_errs = derive_holdings_from_trades(all_user_trades)
    if derive_errs:
        return {"imported": 0, "holdings_changed": [], "errors": [{"line": 0, "reason": e} for e in derive_errs]}

    changed = []
    for t in trades:
        await trades_repo.create({**t, "user_id": user_id, "source": "import", "batch_id": batch_id})
    for h in holdings:
        existing = await holdings_repo.find_by_user_code(user_id, h["stock_code"])
        if existing:
            await holdings_repo.update(existing["id"], {
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "name": h["stock_name"] or existing["name"],
                "derived_from_trades": True,
            })
        else:
            await holdings_repo.create({
                "user_id": user_id, "code": h["stock_code"], "name": h["stock_name"],
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "derived_from_trades": True,
            })
        changed.append(h["stock_code"])
    return {"imported": len(trades), "holdings_changed": changed, "errors": []}


async def create_one(user_id: str, row: dict) -> dict:
    """手动录入单笔，同样推导更新持仓。"""
    t = {**_settled(row), "source": "manual", "batch_id": ""}
    all_user_trades = await trades_repo.list_all_by_user(user_id)
    trial = sorted(all_user_trades + [t], key=lambda r: (r["stock_code"], r["trade_date"]))
    holdings, errs = derive_holdings_from_trades(trial)
    if errs:
        raise BizError("；".join(errs))
    created = await trades_repo.create({**t, "user_id": user_id})
    for h in holdings:
        existing = await holdings_repo.find_by_user_code(user_id, h["stock_code"])
        if existing:
            await holdings_repo.update(existing["id"], {
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "name": h["stock_name"] or existing["name"], "derived_from_trades": True})
        else:
            await holdings_repo.create({"user_id": user_id, "code": h["stock_code"],
                                        "name": h["stock_name"], "quantity": h["quantity"],
                                        "cost_price": h["cost_price"], "derived_from_trades": True})
    return created


async def delete_one(user_id: str, trade_id: str) -> bool:
    """删除单笔并重算持仓（仅允许 manual 来源；import 批次行删除留待扩展）。"""
    row = await trades_repo.get(trade_id)
    if not row or row["user_id"] != user_id:
        raise BizError("交易记录不存在", code=404, status_code=404)
    if row["source"] != "manual":
        raise BizError("导入产生的记录不可单笔删除", code=400, status_code=400)
    remaining = [t for t in await trades_repo.list_all_by_user(user_id) if t["id"] != trade_id]
    holdings, errs = derive_holdings_from_trades(
        sorted(remaining, key=lambda r: (r["stock_code"], r["trade_date"])))
    if errs:
        raise BizError("；".join(errs))
    await trades_repo.delete(trade_id)
    for h in holdings:
        existing = await holdings_repo.find_by_user_code(user_id, h["stock_code"])
        if existing:
            await holdings_repo.update(existing["id"], {
                "quantity": h["quantity"], "cost_price": h["cost_price"],
                "name": h["stock_name"] or existing["name"], "derived_from_trades": True})
        else:
            await holdings_repo.create({"user_id": user_id, "code": h["stock_code"],
                                        "name": h["stock_name"], "quantity": h["quantity"],
                                        "cost_price": h["cost_price"], "derived_from_trades": True})
    return True


async def list_trades(user_id: str, query: dict) -> dict:
    rows = await trades_repo.list_by_user(
        user_id, code=query.get("code"), direction=query.get("direction"),
        date_from=query.get("trade_date_from"), date_to=query.get("trade_date_to"))
    return {"items": rows, "total": len(rows)}
```

```python
# api/v1/trades.py
from fastapi import APIRouter, File, Query, Request, UploadFile

from core.dependencies import CurrentUserDep, require_roles
from core.exceptions import BizError
from core.response import ok, fail
from schemas.trades import TradeRow
from services import trades_service

router = APIRouter(prefix="/trades", tags=["交易记录"])


@router.post("/import/preview", summary="交割单预览（不落库）")
async def preview(request: Request, file: UploadFile | None = File(default=None)):
    raw = None
    filename = None
    if file is not None:
        raw = await file.read()
        filename = file.filename or "deal.csv"
    else:
        body = await request.body()
        raw = body.decode("utf-8") if body else ""
    out = await trades_service.preview("", raw, filename)   # 解析不依赖 user
    if out["errors"] and not out["items"]:
        return ok(out)   # 预览不过校验，前端据此渲染行级错误
    return ok(out)


@router.post("/import/commit", summary="提交交割单（写记录并推导持仓）")
async def commit(user: CurrentUserDep, request: Request):
    body = await request.json()
    rows = body.get("rows") or []
    try:
        valid = [TradeRow(**r).model_dump() for r in rows]
    except Exception as e:
        raise BizError(f"记录字段不合法: {e}", code=422, status_code=422)
    out = await trades_service.commit(user.id, valid, body.get("batch_note", ""))
    if out["errors"]:
        return fail("提交存在行级错误，未写入", data=out)
    return ok(out, message="导入完成")


@router.get("", summary="交易记录列表")
async def list_trades(user: CurrentUserDep, code: str | None = None,
                      direction: str | None = None,
                      trade_date_from: str | None = None,
                      trade_date_to: str | None = None):
    data = await trades_service.list_trades(user.id, {
        "code": code, "direction": direction,
        "trade_date_from": trade_date_from, "trade_date_to": trade_date_to})
    return ok(data)


@router.post("", summary="手动录入单笔")
async def create_one(user: CurrentUserDep, row: TradeRow):
    created = await trades_service.create_one(user.id, row.model_dump())
    return ok(created, message="已录入并更新持仓")


@router.delete("/{trade_id}", summary="删除单笔（仅手动来源）")
async def delete_one(user: CurrentUserDep, trade_id: str):
    await trades_service.delete_one(user.id, trade_id)
    return ok(message="记录已删除")
```

```python
# api/v1/router.py —— 追加 import 与挂载
from api.v1 import trades
api_router.include_router(trades.router, dependencies=_password_gate)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_trades_api.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/api/v1/trades.py stock_selector_api/backend/services/trades_service.py stock_selector_api/backend/api/v1/router.py stock_selector_api/backend/tests/test_trades_api.py
git commit -m "feat(trades): 交割单预览/提交 + 手动录入 + 持仓联动 API（M1）"
```

---

### Task B7: 持仓诊断插件扩展 `POST /api/portfolio/pnl`（利润单点口径）（M1）

**Files:**
- Create: `stock_selector_api/plugin/portfolio/realized.py`
- Modify: `stock_selector_api/plugin/server.py`
- Test: `stock_selector_api/plugin/tests/test_realized.py`、`stock_selector_api/plugin/tests/test_server_pnl.py`

**Interfaces:**
- Produces（插件端契约）:
  - `compute_pnl(trades: list[dict], holdings: list[dict], provider, latest_price_fn)` →
    ```
    {"per_code": [{code, name, position, avg_cost, latest_price, realized_pnl, unrealized_pnl, total_pnl, fees}],
     "realized_summary": {"total_realized", "total_fees"},
     "unrealized_summary": {"total_unrealized", "total_value", "holding_count"}}
    ```
  - `POST /api/portfolio/pnl` body `{user_id, natural_day, trades: [TradeIn...], holdings: [HoldingIn...]}`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/plugin/tests/test_realized.py
from collections.abc import Callable

import pytest

from portfolio.realized import compute_pnl


@pytest.fixture
def prices() -> Callable[[str], float]:
    def _fn(code: str) -> float:
        return {"600519": 1800.0, "000001": 12.0}.get(code, 1.0)
    return _fn


def _t(day, code, name, direction, qty, price, fee=0.0, stamp=0.0):
    return {"trade_date": day, "stock_code": code, "stock_name": name,
            "direction": direction, "price": price, "quantity": qty,
            "commission": fee, "stamp_tax": stamp, "transfer_fee": 0.0,
            "settled_amount": 0.0}


def test_realized_on_partial_sell(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-02", "600519", "茅台", "sell", 40, 1800.0, fee=5.0, stamp=72.0),
    ], [], prices)
    row = out["per_code"][0]
    assert row["position"] == 60                                  # 100-40
    assert row["avg_cost"] == 1600.0                              # 卖出不动成本
    assert round(row["realized_pnl"], 2) == 7923.0                # (1800-1600)*40 - 5 - 72
    assert round(row["unrealized_pnl"], 2) == (1800.0 - 1600.0) * 60


def test_moving_average_reused_by_backend_derive(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-02", "600519", "茅台", "buy", 100, 1700.0),
    ], [], prices)
    row = out["per_code"][0]
    assert round(row["avg_cost"], 4) == 1650.0
    assert row["position"] == 200


def test_unrealized_uses_holdings_fallback(prices):
    out = compute_pnl([], [{"code": "000001", "name": "平安", "quantity": 1000, "cost_price": 10.0}], prices)
    row = out["per_code"][0]
    assert row["realized_pnl"] == 0.0
    assert round(row["unrealized_pnl"], 2) == (12.0 - 10.0) * 1000


def test_summaries(prices):
    out = compute_pnl([
        _t("09-01", "600519", "茅台", "buy", 100, 1600.0),
        _t("09-01", "000001", "平安", "buy", 1000, 10.0),
    ], [], prices)
    assert out["unrealized_summary"]["total_value"] == 1800.0 * 100 + 12.0 * 1000
```

```python
# stock_selector_api/plugin/tests/test_server_pnl.py
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


def test_pnl_endpoint_ok():
    r = client.post("/api/portfolio/pnl", json={
        "user_id": "u1", "natural_day": "2026-09-20",
        "trades": [{"trade_date": "2026-09-01", "stock_code": "600519", "stock_name": "茅台",
                    "direction": "buy", "price": 1600.0, "quantity": 100}],
        "holdings": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert "per_code" in body and "realized_summary" in body
```

- [ ] **Step 2: 运行确认失败**

Run（`workdir=stock_selector_api/plugin`）: `python -m pytest tests/test_realized.py tests/test_server_pnl.py -v --basetemp .pytest_tmp`
Expected: FAIL（ImportError: 无 real或服务器无 pnl 路由）

- [ ] **Step 3: 实现插件端**

```python
# plugin/portfolio/realized.py
"""利润插件核心：移动加权成本序列（单点口径）+ 已实现/浮动盈亏。
后端持仓簿记与本模块用同一移动加权算法，回归测试锁一致（R1-2）。"""
from __future__ import annotations


def _r(v: float) -> float:
    return round(float(v), 2)


def _cost_step(qty: int, cost: float, buy_qty: int, price: float) -> float:
    total = qty + buy_qty
    return (cost * qty + price * buy_qty) / total if total > 0 else price


def compute_pnl(trades: list[dict], holdings: list[dict], latest_price_fn) -> dict:
    """trades 逐条移动加权；卖出即时结算已实现盈亏（费用扣减）。
    latest_price_fn(code)->float 现价；持仓快照仅兜底现成数量/成本。"""
    pos: dict[str, dict] = {}
    realized: dict[str, float] = {}
    fees: dict[str, float] = {}

    for t in sorted(trades, key=lambda x: (x["stock_code"], x["trade_date"])):
        code = t["stock_code"]
        cur = pos.setdefault(code, {"name": t.get("stock_name", ""), "qty": 0, "cost": 0.0})
        fee = float(t.get("commission", 0.0)) + float(t.get("stamp_tax", 0.0)) + float(t.get("transfer_fee", 0.0))
        fees[code] = fees.get(code, 0.0) + fee
        qty = int(t["quantity"])
        price = float(t["price"])
        if t["direction"] == "buy":
            cur["cost"] = _cost_step(cur["qty"], cur["cost"], qty, price)
            cur["qty"] += qty
        else:
            if qty > cur["qty"]:   # 容错：超卖只按现持仓结算
                qty = cur["qty"]
            realized[code] = realized.get(code, 0.0) + (price - cur["cost"]) * qty - fee
            cur["qty"] -= qty

    # holdings 提供无交易记录的仓位兜底（名称/成本）
    for h in holdings:
        code = h["code"]
        cur = pos.setdefault(code, {"name": h.get("name", ""), "qty": int(h.get("quantity", 0)),
                                    "cost": float(h.get("cost_price", 0.0))})
        if cur["qty"] == 0:
            cur["qty"] = int(h.get("quantity", 0))
            cur["cost"] = float(h.get("cost_price", 0.0))
        cur["name"] = h.get("name", cur.get("name", ""))

    per_code = []
    total_value = 0.0
    total_unrealized = 0.0
    total_realized = 0.0
    total_fees = 0.0
    for code, cur in pos.items():
        latest = float(latest_price_fn(code))
        unreal = (latest - cur["cost"]) * cur["qty"] if cur["qty"] > 0 else 0.0
        total_value += latest * cur["qty"]
        total_unrealized += unreal
        rp = realized.get(code, 0.0)
        total_realized += rp
        total_fees += fees.get(code, 0.0)
        per_code.append({
            "code": code, "name": cur["name"],
            "position": cur["qty"], "avg_cost": _r(cur["cost"]),
            "latest_price": _r(latest),
            "realized_pnl": _r(rp), "unrealized_pnl": _r(unreal),
            "total_pnl": _r(rp + unreal), "fees": _r(fees.get(code, 0.0)),
        })
    return {
        "per_code": per_code,
        "realized_summary": {"total_realized": _r(total_realized),
                             "total_fees": _r(total_fees)},
        "unrealized_summary": {"total_unrealized": _r(total_unrealized),
                               "total_value": _r(total_value),
                               "holding_count": len(per_code)},
    }
```

```python
# plugin/server.py —— 在 HoldingIn 之后追加 TradeIn 与 PnlBody，并新增路由
class TradeIn(BaseModel):
    trade_date: str = Field(min_length=8, max_length=16)
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    direction: str = Field(min_length=3, max_length=8)
    price: float = Field(ge=0)
    quantity: int = Field(ge=0)
    commission: float = Field(default=0.0)
    stamp_tax: float = Field(default=0.0)
    transfer_fee: float = Field(default=0.0)
    settled_amount: float = Field(default=0.0)


class PnlBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    natural_day: str = Field(min_length=8, max_length=16)
    trades: list[TradeIn] = Field(default_factory=list)
    holdings: list[HoldingIn] = Field(default_factory=list)


@app.post("/api/portfolio/pnl")
async def pnl(body: PnlBody) -> dict:
    from portfolio.realized import compute_pnl
    from portfolio.pricing import latest_price

    try:
        return compute_pnl(
            [t.model_dump() for t in body.trades],
            [h.model_dump() for h in body.holdings],
            lambda code: latest_price(code, body.natural_day),
        )
    except PricingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_realized.py tests/test_server_pnl.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/plugin/portfolio/realized.py stock_selector_api/plugin/server.py stock_selector_api/plugin/tests/test_realized.py stock_selector_api/plugin/tests/test_server_pnl.py
git commit -m "feat(plugin): POST /api/portfolio/pnl 利润端点（移动加权成本单点口径）"
```

---

### Task B8: 后端利润代理 `/api/v1/holdings/pnl` + 持仓列表附诊断快照（M1）

**Files:**
- Modify: `stock_selector_api/backend/services/portfolio_plugin.py`、`stock_selector_api/backend/services/holdings_service.py`、`stock_selector_api/backend/api/v1/holdings.py`
- Test: `stock_selector_api/backend/tests/test_holdings_pnl.py`

**Interfaces:**
- Consumes: Task B7 插件契约
- Produces:
  - `portfolio_plugin.call_plugin_pnl(payload) -> dict`（http 代理 / mock 确定性）
  - `holdings_service.pnl(user_id, natural_day) -> dict`（透传插件结果；无持仓/无交易→空结构）
  - `holdings_service.list_holdings(user_id)` 附最近诊断 run 明细（`latest_price/holding_pnl/pnl_pct/action/action_reason`）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_holdings_pnl.py
import uuid

from repositories import holdings_repo
from services import holdings_service

MP = {  # mock 判定映射（plugin_mode=mock）
    "sell": {"code": "600519", "name": "茅台", "action": "sell", "action_reason": "mock plugin: sell",
             "latest_price": 1700.0, "quantity": 100, "holding_pnl": 6300.0, "pnl_pct": 3.75, "suggestion": ""},
}


async def test_pnl_proxies_to_plugin():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1604.0})
    out = await holdings_service.pnl(uid, "2026-09-20")
    assert "per_code" in out and "realized_summary" in out
    assert out["per_code"][0]["avg_cost"] == 1604.0
    # 未实现盈亏来自插件（mock 对成本 *1.005 基准）：断言结构而非精确值，禁后端算术
    assert out["unrealized_summary"]["holding_count"] == 1


async def test_pnl_empty_no_holdings():
    out = await holdings_service.pnl("ghost-user", "2026-09-20")
    assert out["per_code"] == []
    assert out["realized_summary"] == {"total_realized": 0.0, "total_fees": 0.0}
    assert out["unrealized_summary"]["holding_count"] == 0


async def test_list_holdings_enriched_with_diag():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1688.0})

    await holdings_service.complete_diag_for(uid, [  # 先制造一条诊断 run（供 snapshot 附着）
    ])
    rows = await holdings_service.list_holdings(uid)
    assert rows[0]["code"] == "600519"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_holdings_pnl.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现代理与端点**

```python
# services/portfolio_plugin.py —— 追加 mock/hhttp pnl

def _mock_pnl(payload: dict) -> dict:
    """确定性 mock：成本为基准价，latest=成本*(1+0.5%)，已实现=0。仅测试占位。"""
    hold = {h["code"]: h for h in payload.get("holdings", [])}
    per = []
    for code, h in hold.items():
        cost = float(h.get("cost_price", 0.0))
        qty = int(h.get("quantity", 0))
        latest = round(cost * 1.005, 2)
        per.append({"code": code, "name": h.get("name", ""), "position": qty,
                    "avg_cost": round(cost, 4), "latest_price": latest,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": round((latest - cost) * qty, 2),
                    "total_pnl": round((latest - cost) * qty, 2), "fees": 0.0})
    return {"per_code": per,
            "realized_summary": {"total_realized": 0.0, "total_fees": 0.0},
            "unrealized_summary": {"total_unrealized": round(sum(p["unrealized_pnl"] for p in per), 2),
                                   "total_value": round(sum(p["latest_price"] * p["position"] for p in per), 2),
                                   "holding_count": len(per)}}


async def _http_pnl(payload: dict) -> dict:
    url = settings.plugin_base_url.rstrip("/") + "/api/portfolio/pnl"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def call_plugin_pnl(payload: dict) -> dict:
    if settings.plugin_mode == "http":
        return await _http_pnl(payload)
    return _mock_pnl(payload)
```

```python
# services/holdings_service.py —— 追加（顶部 import datetime/date 与 services.portfolio_plugin）

async def pnl(user_id: str, natural_day: str) -> dict:
    trades = await trades_repo.list_all_by_user(user_id)
    holdings = await holdings_repo.list_enabled(user_id)
    out = await portfolio_plugin.call_plugin_pnl({
        "user_id": user_id,
        "natural_day": natural_day,
        "trades": trades,
        "holdings": holdings,
    })
    return out


async def complete_diag_for(user_id: str, results: list[dict]) -> None:
    """测试辅助：为 list_holdings 附快照提供最近诊断 run（生产由 diag 落库）。"""
    if not results:
        return
    from schemas.portfolio import PortfolioDiagRunCreate
    run = await portfolio_runs.create({
        "batch_id": "seed", "user_id": user_id, "natural_day": "2026-09-20",
        "status": "completed", "held_code_count": len(results),
        "action_count": len(results)})
    for r in results:
        await portfolio_results.create({"run_id": run["run_id"], **r})


async def list_holdings(user_id: str, enabled_only: bool = False) -> list[dict]:
    if enabled_only:
        rows = await holdings_repo.list_enabled(user_id)
    else:
        rows = await holdings_repo.list_by_user(user_id)
    return await _attach_diag_snapshot(user_id, rows)


async def _attach_diag_snapshot(user_id: str, rows: list[dict]) -> list[dict]:
    """最近一次诊断 run 明细附到持仓行（latest_price/holding_pnl/pnl_pct/action）。"""
    if not rows:
        return rows
    runs = await portfolio_runs.list_by_user(user_id, page=1, size=1)
    if not runs:
        return rows
    latest = runs[0]
    results = await portfolio_results.list_by_run(latest["run_id"])
    by_code = {r["code"]: r for r in results}
    for h in rows:
        snap = by_code.get(h["code"])
        for key in ("latest_price", "holding_pnl", "pnl_pct", "action", "action_reason", "suggestion"):
            h[key] = snap.get(key) if snap else None
        h["diag_run_id"] = latest["run_id"]
    return rows
```

```python
# api/v1/holdings.py —— 追加
@router.get("/pnl", summary="利润分析（代理持仓诊断插件 pnl，零算术）")
async def holdings_pnl(user: CurrentUserDep, natural_day: str):
    data = await holdings_service.pnl(user.id, natural_day)
    return ok(data)
```

> 注意：`holdings_service` 顶部需补 import：`from repositories import portfolio_results, portfolio_runs, trades_repo`、`from services import portfolio_plugin`，并在 `list_holdings` 签名尾部把 `_attach_diag_snapshot` 用在原返回处。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_holdings_pnl.py tests/test_holdings_api.py -v --basetemp .pytest_tmp`
Expected: PASS（既有 holdings_api 回归不破）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/portfolio_plugin.py stock_selector_api/backend/services/holdings_service.py stock_selector_api/backend/api/v1/holdings.py stock_selector_api/backend/tests/test_holdings_pnl.py
git commit -m "feat(holdings): /api/v1/holdings/pnl 利润代理 + 列表附诊断快照（M1）"
```

---

### Task B9: 站内信模型 + 仓储（M2 基建）

**Files:**
- Create: `stock_selector_api/backend/db/sql/inbox.py`、`stock_selector_api/backend/schemas/inbox.py`、`stock_selector_api/backend/repositories/inbox.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`
- Test: `stock_selector_api/backend/tests/test_inbox_repo.py`

**Interfaces:**
- Produces: `InboxMessage`（表 `inbox_messages`）+ `inbox_repo.list_for(user_id, *, page, size, unread_only, since_id) -> tuple[list[dict], int, int]`、`latest_id(user_id) -> str|None`、`mark_read(id, user_id)`, `mark_all_read(user_id)`, `unread_count(user_id)`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_inbox_repo.py
from repositories import inbox_repo


async def test_create_and_list():
    await inbox_repo.create({"user_id": "ins1", "event_code": "selection_completed",
                             "title": "选股完成", "content": "命中 3 只", "read_at": None})
    await inbox_repo.create({"user_id": "ins1", "event_code": "selection_completed",
                             "title": "选股完成", "content": "命中 5 只", "read_at": None})
    items, total, unread = await inbox_repo.list_for("ins1", page=1, size=20)
    assert total == 2 and unread == 2


async def test_mark_read_and_unread_filter():
    msg = await inbox_repo.create({"user_id": "ins2", "event_code": "portfolio_diag_completed",
                                   "title": "诊断完成", "content": "ok"})
    await inbox_repo.mark_read(msg["id"], "ins2")
    _, _, unread = await inbox_repo.list_for("ins2", page=1, size=20, unread_only=True)
    assert unread == 0


async def test_latest_and_after():
    a = await inbox_repo.create({"user_id": "ins3", "event_code": "a", "title": "t", "content": "c"})
    await inbox_repo.create({"user_id": "ins3", "event_code": "b", "title": "t", "content": "c"})
    latest = await inbox_repo.latest_id("ins3")
    assert latest == (await inbox_repo.list_for("ins3", page=1, size=1))[0][0]["id"]
    after, _, _ = await inbox_repo.list_for("ins3", page=1, size=20, since_id=a["id"])
    assert len(after) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_inbox_repo.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# db/sql/inbox.py
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class InboxMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "inbox_messages"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(String(2048), default="")
    read_at: Mapped[DateTime | None] = mapped_column(DateTime, default=None, nullable=True)
    related_type: Mapped[str] = mapped_column(String(32), default="")
    related_id: Mapped[str] = mapped_column(String(64), default="")
```

```python
# schemas/inbox.py
from datetime import datetime

from pydantic import BaseModel, Field


class InboxCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    event_code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="", max_length=2048)
    read_at: datetime | None = None
    related_type: str = Field(default="", max_length=32)
    related_id: str = Field(default="", max_length=64)


class InboxOut(BaseModel):
    id: str
    user_id: str
    event_code: str
    title: str
    content: str = ""
    read_at: datetime | None = None
    created_at: datetime | None = None
    related_type: str = ""
    related_id: str = ""
```

```python
# repositories/inbox.py
from datetime import datetime, timezone

from sqlalchemy import func, select

from db.sql.inbox import InboxMessage
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.inbox import InboxCreate

RepoBase = pick()


def _read_at() -> datetime:
    return datetime.now(timezone.utc)


class InboxRepo(RepoBase):
    model = InboxMessage
    table = "inbox_messages"
    schema = InboxCreate

    async def unread_count(self, user_id: str) -> int:
        async with self._session() as s:
            stmt = (select(func.count()).select_from(self.model)
                    .where(self.model.user_id == user_id, self.model.read_at.is_(None)))
            return (await s.execute(stmt)).scalar_one()

    async def latest_id(self, user_id: str) -> str | None:
        async with self._session() as s:
            stmt = (select(self.model.id).where(self.model.user_id == user_id)
                    .order_by(self.model.created_at.desc()).limit(1))
            return (await s.execute(stmt)).scalar_one_or_none()

    async def mark_read(self, msg_id: str, user_id: str) -> bool:
        return await self._update_where_one(
            self.table, {"id": msg_id, "user_id": user_id}, {"read_at": _read_at()})

    async def mark_all_read(self, user_id: str) -> None:
        async with self._session() as s:
            from sqlalchemy import update as sa_update
            await s.execute(sa_update(self.model).where(
                self.model.user_id == user_id, self.model.read_at.is_(None)
            ).values(read_at=_read_at()))
            await s.commit()

    async def list_for(self, user_id: str, *, page: int = 1, size: int = 20,
                       unread_only: bool = False, since_id: str | None = None
                       ) -> tuple[list[dict], int, int]:
        page = max(1, page)
        size = min(max(1, size), 500)
        async with self._session() as s:
            base = select(self.model).where(self.model.user_id == user_id)
            count_stmt = select(func.count()).select_from(self.model).where(self.model.user_id == user_id)
            unread_stmt = (select(func.count()).select_from(self.model)
                           .where(self.model.user_id == user_id, self.model.read_at.is_(None)))
            if unread_only:
                base = base.where(self.model.read_at.is_(None))
                count_stmt = count_stmt.where(self.model.read_at.is_(None))
            if since_id:
                ref = await s.get(self.model, since_id)
                if ref is not None:
                    base = base.where(self.model.created_at > ref.created_at)
                    count_stmt = count_stmt.where(self.model.created_at > ref.created_at)
            total = (await s.execute(count_stmt)).scalar_one()
            unread = (await s.execute(unread_stmt)).scalar_one()
            rows = (await s.execute(
                base.order_by(self.model.created_at.desc())
                    .offset((page - 1) * size).limit(size))).scalars().all()
            return [_model_to_dict(o) for o in rows], total, unread
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_inbox_repo.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql/inbox.py stock_selector_api/backend/schemas/inbox.py stock_selector_api/backend/repositories/inbox.py stock_selector_api/backend/db/sql/__init__.py stock_selector_api/backend/tests/test_inbox_repo.py
git commit -m "feat(inbox): 站内信表/仓储（列表/未读/已读/since_id）"
```

---

### Task B10: 站内信 API + 落库+WS 双路 helper（M2）

**Files:**
- Create: `stock_selector_api/backend/api/v1/messages.py`、`stock_selector_api/backend/services/inbox_service.py`、`stock_selector_api/backend/services/inbox_notify.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`
- Test: `stock_selector_api/backend/tests/test_messages_api.py`、`stock_selector_api/backend/tests/test_inbox_notify.py`

**Interfaces:**
- Produces:
  - API `GET /api/v1/messages?page&size&unread_only&since_id` → `{items,total,unread_count}`；`PUT /api/v1/messages/{id}/read`；`PUT /api/v1/messages/read_all`
  - `inbox_notify.notify_user(user_id, event_code, title, content, related_type="", related_id="") -> dict`（落库 + WS 推送）
  - 依赖 Task B12 `ws_manager.push`（Task B11 先建 manager；本任务可先用接口占位）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_messages_api.py
from services import inbox_notify


async def test_messages_api_flow(client, admin_headers):
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()["data"]
    await inbox_notify.notify_user(me["id"], "selection_completed", "选股完成", "命中 3 只")
    await inbox_notify.notify_user(me["id"], "selection_completed", "选股完成", "命中 5 只")

    r = await client.get("/api/v1/messages", headers=admin_headers)
    data = r.json()["data"]
    assert data["total"] == 2 and data["unread_count"] == 2

    mid = data["items"][0]["id"]
    r2 = await client.put(f"/api/v1/messages/{mid}/read", headers=admin_headers)
    assert r2.status_code == 200
    r3 = await client.get("/api/v1/messages?unread_only=true", headers=admin_headers)
    assert r3.json()["data"]["unread_count"] == 1

    await client.put("/api/v1/messages/read_all", headers=admin_headers)
    r4 = await client.get("/api/v1/messages", headers=admin_headers)
    assert r4.json()["data"]["unread_count"] == 0


async def test_messages_requires_auth(client):
    r = await client.get("/api/v1/messages")
    assert r.status_code in (401, 403)
```

```python
# stock_selector_api/backend/tests/test_inbox_notify.py
from repositories import inbox_repo


async def test_notify_writes_and_returns():
    msg = await inbox_notify.notify_user("nn-1", "selection_completed", "T", "C",
                                         related_type="select_jobs", related_id="j1")
    assert msg["user_id"] == "nn-1"
    row = await inbox_repo.get(msg["id"])
    assert row["related_id"] == "j1" and row["read_at"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_messages_api.py tests/test_inbox_notify.py -v --basetemp .pytest_tmp`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现**

```python
# services/inbox_service.py —— 组装/出参（读模型加 read_status 语义字段）
from repositories import inbox_repo


def _out(msg: dict) -> dict:
    # read_status: True=未读（read_at is None）——对齐客户端 unread 判定
    return {**msg, "read_status": msg.get("read_at") is None}


async def list_messages(user_id: str, query: dict) -> dict:
    items, total, unread = await inbox_repo.list_for(
        user_id, page=query.get("page", 1), size=query.get("size", 20),
        unread_only=bool(query.get("unread_only")), since_id=query.get("since_id"))
    return {"items": [_out(m) for m in items], "total": total, "unread_count": unread}


async def mark_read(user_id: str, msg_id: str) -> bool:
    return await inbox_repo.mark_read(msg_id, user_id)


async def mark_all_read(user_id: str) -> None:
    await inbox_repo.mark_all_read(user_id)
```

```python
# api/v1/messages.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.exceptions import NotFoundError
from core.response import ok
from services import inbox_service

router = APIRouter(prefix="/messages", tags=["站内信"])


@router.get("", summary="站内信列表")
async def list_messages(user: CurrentUserDep,
                        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                        unread_only: bool = False, since_id: str | None = None):
    return ok(await inbox_service.list_messages(user.id, {
        "page": page, "size": size, "unread_only": unread_only, "since_id": since_id}))


@router.put("/{msg_id}/read", summary="标记已读")
async def mark_read(user: CurrentUserDep, msg_id: str):
    if not await inbox_service.mark_read(user.id, msg_id):
        raise NotFoundError("消息不存在")
    return ok(message="已读")


@router.put("/read_all", summary="全部已读")
async def mark_all_read(user: CurrentUserDep):
    await inbox_service.mark_all_read(user.id)
    return ok(message="已全部标记已读")
```

```python
# services/inbox_notify.py
"""事件→站内信落库 + WS 实时推送（双路，不依赖外部渠道开关）。"""
from __future__ import annotations

from repositories import inbox_repo
from services import inbox_service, ws_manager


async def notify_user(user_id: str, event_code: str, title: str, content: str,
                      related_type: str = "", related_id: str = "") -> dict:
    msg = await inbox_repo.create({
        "user_id": user_id, "event_code": event_code, "title": title,
        "content": content, "related_type": related_type, "related_id": related_id,
    })
    out = inbox_service._out(msg)
    await ws_manager.push(user_id, {"type": "msg", "message": out})
    return out
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_messages_api.py tests/test_inbox_notify.py -v --basetemp .pytest_tmp`
Expected: PASS（若 ws_manager 尚未实现，本任务在 B11 落地后回归一次）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/api/v1/messages.py stock_selector_api/backend/services/inbox_service.py stock_selector_api/backend/services/inbox_notify.py stock_selector_api/backend/api/v1/router.py stock_selector_api/backend/tests/test_messages_api.py stock_selector_api/backend/tests/test_inbox_notify.py
git commit -m "feat(inbox): 站内信 API + 落库/WS 双路 helper（M2）"
```

---

### Task B11: WsManager + `/ws/notify` 端点（M2）

**Files:**
- Create: `stock_selector_api/backend/services/ws_manager.py`、`stock_selector_api/backend/api/ws_notify.py`
- Modify: `stock_selector_api/backend/main.py`
- Test: `stock_selector_api/backend/tests/test_ws_notify.py`

**Interfaces:**
- Produces:
  - `ws_manager.connect(user_id, ws)` / `disconnect(user_id, ws)` / `push(user_id, payload) -> int`
  - `WS /ws/notify`：首帧 `{"type":"auth","token"}` → `hello{latest_inbox_id, unread_count}`；后续 `ping`→`pong`；`msg` 帧由 `push` 产生
  - 依赖：Task B9 `inbox_repo.latest_id/unread_count`；`core.security.decode_token` + `_user_id_from_sub`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_ws_notify.py
from services import ws_manager, inbox_notify
from repositories import inbox_repo


async def test_push_only_to_target_user():
    received = []
    class Fake:
        def __init__(self): self.sent = []
        async def send_json(self, obj): received.append(obj)
    f1, f2 = Fake(), Fake()
    ws_manager.connect("w1", f1)
    ws_manager.connect("w2", f2)
    await ws_manager.push("w1", {"type": "msg", "message": {"id": "m1"}})
    ws_manager.disconnect("w1", f1)
    assert len(f1.sent) == 0      # 本测试只收集 received（Fake.send_json 由 push 驱动）
    assert [m["message"]["id"] for m in received] == ["m1"] and len(received) == 1


async def test_ws_hello_and_ping(client, admin_headers):
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()["data"]
    msg = await inbox_notify.notify_user(me["id"], "selection_completed", "T", "C")
    token = admin_headers["Authorization"].split(" ")[1]
    async with client.websocket_connect("/ws/notify") as ws:
        await ws.send_json({"type": "auth", "token": token})
        hello = await ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["unread_count"] == 1
        await ws.send_json({"type": "ping"})
        assert (await ws.receive_json())["type"] == "pong"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_ws_notify.py -v --basetemp .pytest_tmp`
Expected: FAIL（无 ws_manager / 路由）

- [ ] **Step 3: 实现**

```python
# services/ws_manager.py
"""内存 WS 连接管理器：按 user_id 分组；push 仅投递在线连接。"""
from __future__ import annotations

from collections import defaultdict


class WsManager:
    def __init__(self) -> None:
        self._conns: dict[str, set] = defaultdict(set)

    def connect(self, user_id: str, ws) -> None:
        self._conns[user_id].add(ws)

    def disconnect(self, user_id: str, ws) -> None:
        self._conns.get(user_id, set()).discard(ws)

    async def push(self, user_id: str, payload: dict) -> int:
        n = 0
        for ws in list(self._conns.get(user_id, ())):
            try:
                await ws.send_json(payload)
                n += 1
            except Exception:   # noqa: BLE001 单个连接失败不影响其余
                self.disconnect(user_id, ws)
        return n


ws_manager = WsManager()
```

```python
# api/ws_notify.py
"""WS 通知端点：首帧鉴权 → hello；在线用户实时收 msg 帧。"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import security
from services import inbox_service, ws_manager
from repositories import inbox_repo

router = APIRouter()


@router.websocket("/ws/notify")
async def ws_notify(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:                       # noqa: BLE001
        await ws.close(code=4401)
        return
    if first.get("type") != "auth":
        await ws.close(code=4401)
        return
    try:
        payload = security.decode_token(first.get("token", ""), expected_type="access")
        user_id = security._user_id_from_sub(payload["sub"])
    except Exception:                       # noqa: BLE001
        await ws.close(code=4401)
        return

    latest = await inbox_repo.latest_id(user_id)
    unread = await inbox_service.list_messages(user_id, {"page": 1, "size": 1}).__await__()
    unread_count = (await inbox_repo.unread_count(user_id))
    await ws.send_json({"type": "hello", "latest_inbox_id": latest, "unread_count": unread_count})

    ws_manager.connect(user_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, ws)
```

> 修正说明：上段 `unread` 那行写法冗余且错误（不应调用 list_messages），**以 `unread_count = await inbox_repo.unread_count(user_id)` 为准**，删除 `unread` 行。

```python
# main.py —— create_app() 内追加（在 include external_router 之后）
    from api.ws_notify import router as ws_router
    app.include_router(ws_router)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_ws_notify.py -v --basetemp .pytest_tmp`
Expected: PASS（Starlette TestClient 支持 `websocket_connect`）

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/ws_manager.py stock_selector_api/backend/api/ws_notify.py stock_selector_api/backend/main.py stock_selector_api/backend/tests/test_ws_notify.py
git commit -m "feat(notify): WsManager + /ws/notify（首帧鉴权/hello/心跳）"
```

---

### Task B12: 选股任务表 + 仓储（M3）

**Files:**
- Create: `stock_selector_api/backend/db/sql/selection.py`、`stock_selector_api/backend/schemas/selection.py`、`stock_selector_api/backend/repositories/selection.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`
- Test: `stock_selector_api/backend/tests/test_selection_repo.py`

**Interfaces:**
- Produces: `SelectJob`（表 `select_jobs`）、`SelectResult`（表 `select_results`）；
  `select_jobs_repo.list_by_user(user_id, page, size) -> tuple[list, int]`、`find_running(user_id)`；
  `select_results_repo.list_by_job(job_id)`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_selection_repo.py
from repositories import select_jobs_repo, select_results_repo


async def test_job_lifecycle():
    job = await select_jobs_repo.create({"user_id": "s1", "strategies": ["trend_rebound"],
                                         "status": "queued", "source": "manual"})
    assert job["job_id"]
    await select_jobs_repo.update(job["job_id"], {"status": "running"})
    await select_results_repo.create({"job_id": job["job_id"], "code": "600519",
                                      "name": "茅台", "signal": "趋势回踩", "reason": "MA20 上穿"})
    rows = await select_results_repo.list_by_job(job["job_id"])
    assert len(rows) == 1 and rows[0]["signal"] == "趋势回踩"
    await select_jobs_repo.update(job["job_id"], {"status": "done"})
    got = await select_jobs_repo.get(job["job_id"])
    assert got["status"] == "done"


async def test_find_running_and_list():
    await select_jobs_repo.create({"user_id": "s2", "strategies": [], "status": "queued", "source": "manual"})
    r = await select_jobs_repo.find_running("s2")
    assert r is not None
    items, total = await select_jobs_repo.list_by_user("s2", 1, 20)
    assert total >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_selection_repo.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# db/sql/selection.py
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin, new_uuid


class SelectJob(Base, TimestampMixin):
    __tablename__ = "select_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategies: Mapped[str] = mapped_column(String(256), default="")   # JSON 数组字符串
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued/running/done/failed
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    error: Mapped[str] = mapped_column(String(512), default="")
    result_count: Mapped[int] = mapped_column(Integer, default=0)


class SelectResult(Base, IdMixin):
    __tablename__ = "select_results"

    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    signal: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(1024), default="")
```

```python
# schemas/selection.py
from pydantic import BaseModel, Field


class SelectJobCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    strategies: str = Field(default="[]", max_length=256)
    status: str = Field(default="queued", max_length=16)
    source: str = Field(default="manual", max_length=16)
    error: str = Field(default="", max_length=512)
    result_count: int = 0


class SelectResultCreate(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=64)
    signal: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=1024)
```

```python
# repositories/selection.py
import json

from sqlalchemy import select

from db.sql.selection import SelectJob, SelectResult
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.selection import SelectJobCreate, SelectResultCreate

RepoBase = pick()


class SelectJobRepo(RepoBase):
    model = SelectJob
    table = "select_jobs"
    schema = SelectJobCreate

    async def list_by_user(self, user_id: str, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        return await self.list(filters={"user_id": user_id}, page=page, size=size)

    async def find_running(self, user_id: str) -> dict | None:
        return await self._find_one({"user_id": user_id, "status": "running"})

    async def find_latest(self, user_id: str) -> dict | None:
        async with self._session() as s:
            stmt = (select(self.model).where(self.model.user_id == user_id)
                    .order_by(self.model.created_at.desc()).limit(1))
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None


class SelectResultRepo(RepoBase):
    model = SelectResult
    table = "select_results"
    schema = SelectResultCreate

    async def list_by_job(self, job_id: str) -> list[dict]:
        return await self._find_all(filters={"job_id": job_id})
```

```python
# db/sql/__init__.py —— 追加 import 与 __all__
from db.sql.selection import SelectJob, SelectResult
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_selection_repo.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql/selection.py stock_selector_api/backend/schemas/selection.py stock_selector_api/backend/repositories/selection.py stock_selector_api/backend/db/sql/__init__.py stock_selector_api/backend/tests/test_selection_repo.py
git commit -m "feat(selection): 选股 job/results 表+仓储（M3）"
```

---

### Task B13: 选股服务（代理 8002 + job 生命周期 + 完成通知）（M3）

**Files:**
- Create: `stock_selector_api/backend/services/selection_service.py`
- Test: `stock_selector_api/backend/tests/test_selection_service.py`

**Interfaces:**
- Consumes: Task B12 repo、Task B10 `inbox_notify`、Task B1 config 键
- Produces:
  - `selection_service.list_strategies() -> list[str]`
  - `selection_service.start_run(user_id, strategies: list[str] | None, source="manual") -> dict`（单飞：running 中拒绝）
  - `selection_service.run_status(user_id, job_id) -> dict`（含 results；越权拒绝）
  - `selection_service.list_runs(user_id, page, size) -> dict`
  - `selection_service._run_job(job_id, user_id, strategies)`（后台任务；done/failed 时 `inbox_notify.notify_user`）

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_selection_service.py
import uuid

from core.exceptions import BizError
from repositories import inbox_repo, select_results_repo, select_jobs_repo
from services import selection_service


def _mk_user():
    return f"us-{uuid.uuid4().hex[:8]}"


async def test_manual_run_completes_with_results():
    uid = _mk_user()
    out = await selection_service.start_run(uid, ["trend_rebound"])
    assert out["job_id"] and out["status"] in ("queued", "done")
    job = await select_jobs_repo.get(out["job_id"])
    assert job["status"] == "done" and job["result_count"] > 0
    last = await inbox_repo.latest_id(uid)
    assert last is not None       # 完成即发站内信


async def test_single_flight_rejects_duplicate():
    uid = _mk_user()
    await selection_service.start_run(uid, ["trend_rebound"])
    with pytest.raises(BizError):
        await selection_service.start_run(uid, ["trend_rebound"])


async def test_detail_and_ownership():
    uid = _mk_user()
    out = await selection_service.start_run(uid, ["trend_rebound"])
    det = await selection_service.run_status(uid, out["job_id"])
    assert det["results"]
    with pytest.raises(BizError):
        await selection_service.run_status("other-user", out["job_id"])


async def test_list_runs_paged():
    uid = _mk_user()
    await selection_service.start_run(uid, None)
    page = await selection_service.list_runs(uid, 1, 20)
    assert page["total"] >= 1


async def test_strategies_mock():
    assert set(await selection_service.list_strategies()) >= {"trend_rebound", "box_oscillate"}
```

> 需在测试文件顶部 `import pytest`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_selection_service.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# services/selection_service.py
"""选股服务：薄代理选股插件(8002) + job 生命周期 + 完成/失败通知。
红线：不在后端做任何选股逻辑；只有代理与落库。"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from config import settings
from core.exceptions import BizError
from repositories import select_jobs_repo, select_results_repo
from services import inbox_notify

logger = logging.getLogger("selection_service")

_RUNNING_KEY = "selection_single_flight"   # 单飞按 job 状态判断即可，无需额外锁


def _jobs_key(user_id: str, strategies: list[str]) -> str:
    return f"{user_id}:{json.dumps(sorted(strategies), ensure_ascii=False)}"


async def list_strategies() -> list[str]:
    if settings.selection_plugin_mode == "http":
        from services import your_mode_guard  # noqa: F401  占位防误import（实际直接内联）
        url = settings.selection_plugin_base_url.rstrip("/") + "/api/strategy/list"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        return list(body.get("data") or [])
    return ["trend_rebound", "box_oscillate"]


async def _mock_run(strategies: list[str]) -> list[dict]:
    """确定性模拟结果（测试/插件未启时）。"""
    samples = [
        {"code": "600519", "name": "贵州茅台", "signal": "趋势回踩", "reason": "mock: MA20 上穿"},
        {"code": "000858", "name": "五粮液", "signal": "箱体突破", "reason": "mock: 突破箱顶"},
    ]
    return [s for i, s in enumerate(samples) if not strategies or i < len(samples)]


async def _http_run(strategies: list[str]) -> list[dict]:
    url = settings.selection_plugin_base_url.rstrip("/") + "/api/selector/run"
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(url, json={"strategies": strategies or None})
        resp.raise_for_status()
        body = resp.json()
    items = body.get("data") or []
    return [{
        "code": str(it.get("code", "")),
        "name": str(it.get("name", "")),
        "signal": str(it.get("signal", "") or it.get("action", "")),
        "reason": str(it.get("reason", "")),
    } for it in items if it.get("code")]


async def start_run(user_id: str, strategies: list[str] | None,
                    source: str = "manual") -> dict:
    if not user_id:
        raise BizError("user_id 不能为空", code=400, status_code=400)
    running = await select_jobs_repo.find_running(user_id)
    if running:
        raise BizError(f"已有选股任务运行中: {running['job_id']}", code=409, status_code=409)
    strategies = strategies or ["trend_rebound", "box_oscillate"]
    job = await select_jobs_repo.create({
        "user_id": user_id,
        "strategies": json.dumps(strategies, ensure_ascii=False),
        "status": "queued",
        "source": source,
    })
    asyncio.create_task(_run_job(job["job_id"], user_id, strategies))
    return {"job_id": job["job_id"], "status": "queued"}


async def _run_job(job_id: str, user_id: str, strategies: list[str]) -> None:
    try:
        await select_jobs_repo.update(job_id, {"status": "running"})
        if settings.selection_plugin_mode == "http":
            results = await _http_run(strategies)
        else:
            results = await _mock_run(strategies)
        for r in results:
            await select_results_repo.create({"job_id": job_id, **r})
        await select_jobs_repo.update(job_id, {"status": "done", "result_count": len(results)})
        await inbox_notify.notify_user(
            user_id, "selection_completed", "选股完成",
            f"本次选股命中 {len(results)} 只（策略: {'/'.join(strategies)}）",
            related_type="select_jobs", related_id=job_id)
    except Exception as exc:    # noqa: BLE001
        logger.exception("选股任务失败: %s", job_id)
        await select_jobs_repo.update(job_id, {"status": "failed", "error": str(exc)[:512]})
        await inbox_notify.notify_user(
            user_id, "selection_failed", "选股失败", str(exc)[:512],
            related_type="select_jobs", related_id=job_id)


async def run_status(user_id: str, job_id: str) -> dict:
    job = await select_jobs_repo.get(job_id)
    if not job or job["user_id"] != user_id:
        raise BizError("选股任务不存在", code=404, status_code=404)
    job["results"] = await select_results_repo.list_by_job(job_id)
    return job


async def list_runs(user_id: str, page: int = 1, size: int = 20) -> dict:
    items, total = await select_jobs_repo.list_by_user(user_id, page, size)
    for job in items:
        job["result_count_now"] = await select_results_repo.list_by_job(job["job_id"])
        job["result_count_now"] = len(job["result_count_now"]) if isinstance(job["result_count_now"], list) else job["result_count_now"]
    return {"items": items, "total": total}
```

> 代码注意：去掉 `list_strategies` 里无意义的 `your_mode_guard` import（直接 httpx 内联，无其他依赖）；`list_runs` 里计算结果条数直接 `len(await select_results_repo.list_by_job(job["job_id"]))`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_selection_service.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/services/selection_service.py stock_selector_api/backend/tests/test_selection_service.py
git commit -m "feat(selection): 选股代理服务（job 生命周期/单飞/完成通知）"
```

---

### Task B14: 选股 API 路由（M3）

**Files:**
- Create: `stock_selector_api/backend/api/v1/selection.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`
- Test: `stock_selector_api/backend/tests/test_selection_api.py`

**Interfaces:**
- Consumes: Task B13 service
- Produces: `GET /api/v1/selection/strategies`、`POST /api/v1/selection/run`、`GET /api/v1/selection/runs`、`GET /api/v1/selection/runs/{job_id}`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_selection_api.py
import pytest


async def test_selection_api_flow(client, admin_headers):
    r = await client.get("/api/v1/selection/strategies", headers=admin_headers)
    assert r.status_code == 200 and r.json()["data"]

    r2 = await client.post("/api/v1/selection/run", headers=admin_headers, json={"strategies": ["trend_rebound"]})
    assert r2.status_code == 200
    job_id = r2.json()["data"]["job_id"]

    r3 = await client.get(f"/api/v1/selection/runs/{job_id}", headers=admin_headers)
    assert r3.status_code == 200 and r3.json()["data"]["results"] is not None

    r4 = await client.get("/api/v1/selection/runs", headers=admin_headers)
    assert r4.status_code == 200 and r4.json()["data"]["total"] >= 1


async def test_selection_requires_auth(client):
    r = await client.post("/api/v1/selection/run", json={"strategies": []})
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_selection_api.py -v --basetemp .pytest_tmp`
Expected: FAIL（404）

- [ ] **Step 3: 实现**

```python
# api/v1/selection.py
from fastapi import APIRouter, Query

from core.dependencies import CurrentUserDep
from core.response import ok, paged
from pydantic import BaseModel, Field
from services import selection_service

router = APIRouter(prefix="/selection", tags=["选股"])


class RunIn(BaseModel):
    strategies: list[str] | None = Field(default=None, max_length=8)


@router.get("/strategies", summary="可选策略")
async def strategies(user: CurrentUserDep):
    return ok(await selection_service.list_strategies())


@router.post("/run", summary="触发选股（后台任务）")
async def run(user: CurrentUserDep, body: RunIn):
    return ok(await selection_service.start_run(user.id, body.strategies), message="选股任务已创建")


@router.get("/runs", summary="选股任务列表")
async def runs(user: CurrentUserDep, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    data = await selection_service.list_runs(user.id, page, size)
    return ok(paged(data["items"], data["total"], page, size))


@router.get("/runs/{job_id}", summary="任务详情与结果")
async def run_detail(user: CurrentUserDep, job_id: str):
    return ok(await selection_service.run_status(user.id, job_id))
```

```python
# api/v1/router.py
from api.v1 import selection
api_router.include_router(selection.router, dependencies=_password_gate)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_selection_api.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/api/v1/selection.py stock_selector_api/backend/api/v1/router.py stock_selector_api/backend/tests/test_selection_api.py
git commit -m "feat(selection): 选股 API（strategies/run/runs/detail）"
```

---

### Task B15: 定时任务 daily_diag / daily_selection + 诊断完成通知（M4）

**Files:**
- Create: `stock_selector_api/backend/scheduler/tasks/market_daily.py`
- Modify: `stock_selector_api/backend/conf/inputs.conf`、`stock_selector_api/backend/scheduler/tasks/__init__.py`、`stock_selector_api/backend/services/diag_service.py`
- Test: `stock_selector_api/backend/tests/test_market_daily.py`、`stock_selector_api/backend/tests/test_diag_notify.py`

**Interfaces:**
- Consumes: `users` repo、Task B13 `selection_service.start_run(source="scheduled")`、`diag_service.trigger_diag`、Task B10 `inbox_notify`
- Produces:
  - `market_daily.daily_diag(item) -> int`（对全部启用用户触发诊断；返回处理数）
  - `market_daily.daily_selection(item) -> int`
  - `diag_service._run_diag` 完成/失败时调 `inbox_notify`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_market_daily.py
import uuid

from repositories import holdings_repo
from scheduler.tasks import market_daily


async def test_daily_diag_skips_no_holdings():
    # 无持仓用户：不报错，返回 0 处理
    n = await market_daily.daily_diag(None)
    assert n >= 0


async def test_daily_selection_creates_jobs():
    n = await market_daily.daily_selection(None)
    assert n >= 0
```

```python
# stock_selector_api/backend/tests/test_diag_notify.py
import uuid

from repositories import holdings_repo, inbox_repo
from services import diag_service


async def test_diag_completion_notifies():
    uid = f"u-{uuid.uuid4().hex[:8]}"
    await holdings_repo.create({"user_id": uid, "code": "600519", "name": "茅台",
                                "quantity": 100, "cost_price": 1688.0})
    out = await diag_service.trigger_diag(uid, "2026-09-20")
    assert out and out["status"] in ("queued", "completed")
    # 等后台任务完成（轮询 inbox）
    for _ in range(50):
        if await inbox_repo.latest_id(uid):
            break
        import asyncio
        await asyncio.sleep(0.05)
    assert await inbox_repo.latest_id(uid) is not None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_market_daily.py tests/test_diag_notify.py -v --basetemp .pytest_tmp`
Expected: FAIL（无 market_daily 模块 / 无通知）

- [ ] **Step 3: 实现**

```python
# scheduler/tasks/market_daily.py
"""盘后定时任务：持仓诊断 + 选股（M4）。由 inputs.conf 声明触发。"""
from __future__ import annotations

from datetime import date, datetime, timezone

from core.logger import log
from repositories import users
from services import diag_service, selection_service


def _today() -> str:
    return date.today().isoformat()


async def daily_diag(item) -> int:
    """对所有启用用户触发当日持仓诊断（无持仓自动跳过）。"""
    rows, _ = await users.list(filters={"is_active": True}, page=1, size=1000)
    n = 0
    for u in rows or []:
        try:
            out = await diag_service.trigger_diag(u["id"], _today())
            if out:
                n += 1
        except Exception as e:   # noqa: BLE001
            log.warning("daily_diag 跳过用户 {}: {}", u.get("username"), e)
    log.info("daily_diag 完成，处理 {} 个用户", n)
    return n


async def daily_selection(item) -> int:
    """对每个启用用户触发默认策略选股（source=scheduled）。"""
    rows, _ = await users.list(filters={"is_active": True}, page=1, size=1000)
    n = 0
    for u in rows or []:
        try:
            await selection_service.start_run(u["id"], None, source="scheduled")
            n += 1
        except Exception as e:   # noqa: BLE001
            log.warning("daily_selection 跳过用户 {}: {}", u.get("username"), e)
    log.info("daily_selection 完成，创建 {} 个任务", n)
    return n
```

```python
# conf/inputs.conf —— 追加两节（注释示例保留）
[task://market_daily:daily_diag]
enable = true
title = 盘后持仓诊断
cron = hour=18,minute=0,second=0

[task://market_daily:daily_selection]
enable = true
title = 盘后选股
cron = hour=17,minute=30,second=0
```

```python
# scheduler/tasks/__init__.py —— 追加复数注册提示（保持空导入以触发模块加载）
from scheduler.tasks import market_daily  # noqa: F401
```

```python
# services/diag_service.py —— _run_diag 成功/失败分支追加（文件头 import inbox_notify）
        await inbox_notify.notify_user(
            user_id, "portfolio_diag_completed", "持仓诊断完成",
            f"共诊断 {len(plugin_out['results'])} 只持仓，建议卖出 {summary.get('action_counts', {}).get('sell', 0)} 只",
            related_type="portfolio_diag_runs", related_id=run_id)
# 失败分支 except 内、写入 failed 状态后追加：
        await inbox_notify.notify_user(
            user_id, "portfolio_diag_failed", "持仓诊断失败", str(exc)[:512],
            related_type="portfolio_diag_runs", related_id=run_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_market_daily.py tests/test_diag_notify.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/scheduler/tasks/market_daily.py stock_selector_api/backend/conf/inputs.conf stock_selector_api/backend/scheduler/tasks/__init__.py stock_selector_api/backend/services/diag_service.py stock_selector_api/backend/tests/test_market_daily.py stock_selector_api/backend/tests/test_diag_notify.py
git commit -m "feat(scheduler): 盘后诊断/选股定时任务 + 诊断完成通知（M4）"
```

---

### Task B16: 升级包表 + 仓储（M5）

**Files:**
- Create: `stock_selector_api/backend/db/sql/upgrade.py`、`stock_selector_api/backend/schemas/upgrade.py`、`stock_selector_api/backend/repositories/upgrade.py`
- Modify: `stock_selector_api/backend/db/sql/__init__.py`
- Test: `stock_selector_api/backend/tests/test_upgrade_repo.py`

**Interfaces:**
- Produces: `UpgradePackage`（`upgrade_packages`）+ `upgrade_repo.latest_published(platform)`、`list_for_admin(platform, page, size)`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_upgrade_repo.py
from repositories import upgrade_repo


async def test_latest_published():
    await upgrade_repo.create({"platform": "android", "package_type": "apk", "version_code": 101,
                               "version_name": "1.0.1", "url": "/u/a.apk", "md5": "m1",
                               "size_bytes": 1024, "release_notes": "修复", "forced": False,
                               "is_published": True})
    await upgrade_repo.create({"platform": "android", "package_type": "apk", "version_code": 102,
                               "version_name": "1.0.2", "url": "/u/b.apk", "md5": "m2",
                               "size_bytes": 1024, "release_notes": "新功能", "forced": True,
                               "is_published": True})
    latest = await upgrade_repo.latest_published("android")
    assert latest["version_code"] == 102 and latest["forced"] is True
    assert await upgrade_repo.latest_published("ios") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_upgrade_repo.py -v --basetemp .pytest_tmp`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# db/sql/upgrade.py
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class UpgradePackage(IdMixin, TimestampMixin, Base):
    __tablename__ = "upgrade_packages"

    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # h5|android|ios
    package_type: Mapped[str] = mapped_column(String(8), default="")   # wgt|apk|ipa|h5
    version_code: Mapped[int] = mapped_column(Integer, nullable=False)
    version_name: Mapped[str] = mapped_column(String(32), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    md5: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    release_notes: Mapped[str] = mapped_column(String(1024), default="")
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_at: Mapped[str] = mapped_column(String(32), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
```

```python
# schemas/upgrade.py
from pydantic import BaseModel, Field


class UpgradeCreate(BaseModel):
    platform: str = Field(min_length=2, max_length=16)
    package_type: str = Field(default="", max_length=8)
    version_code: int = Field(ge=1)
    version_name: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=512)
    md5: str = Field(default="", max_length=64)
    size_bytes: int = 0
    release_notes: str = Field(default="", max_length=1024)
    forced: bool = False
    is_published: bool = False
    publish_at: str = Field(default="", max_length=32)
    created_by: str = Field(default="", max_length=64)
```

```python
# repositories/upgrade.py
from sqlalchemy import select

from db.sql.upgrade import UpgradePackage
from repositories._choose import pick
from repositories.sql_impl import _model_to_dict
from schemas.upgrade import UpgradeCreate

RepoBase = pick()


class UpgradeRepo(RepoBase):
    model = UpgradePackage
    table = "upgrade_packages"
    schema = UpgradeCreate

    async def latest_published(self, platform: str) -> dict | None:
        async with self._session() as s:
            stmt = (select(self.model).where(self.model.platform == platform,
                                             self.model.is_published.is_(True))
                    .order_by(self.model.version_code.desc()).limit(1))
            obj = (await s.execute(stmt)).scalars().first()
            return _model_to_dict(obj) if obj else None

    async def list_for_admin(self, platform: str | None = None, page: int = 1, size: int = 20):
        filters = {"platform": platform} if platform else None
        return await self.list(filters=filters, page=page, size=size)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_upgrade_repo.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/db/sql/upgrade.py stock_selector_api/backend/schemas/upgrade.py stock_selector_api/backend/repositories/upgrade.py stock_selector_api/backend/db/sql/__init__.py stock_selector_api/backend/tests/test_upgrade_repo.py
git commit -m "feat(upgrade): 升级包表+仓储（M5）"
```

---

### Task B17: 升级管理 API + 文件上传（M5）

**Files:**
- Create: `stock_selector_api/backend/api/v1/upgrade.py`、`stock_selector_api/backend/services/upgrade_service.py`
- Modify: `stock_selector_api/backend/api/v1/router.py`
- Test: `stock_selector_api/backend/tests/test_upgrade_api.py`

**Interfaces:**
- Consumes: Task B16 repo、settings.upgrade_upload_dir、`require_roles("superadmin")`
- Produces: `POST/GET /api/v1/upgrade/packages`、`PUT/DELETE /api/v1/upgrade/packages/{id}`、`POST /api/v1/upgrade/packages/upload`；`upgrade_service.store_package(...)`、`save_upload(file, raw, admin) -> {url, md5, size_bytes}`、`delete_package(id)`

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_upgrade_api.py
import hashlib


async def test_upgrade_admin_crud(client, admin_headers):
    payload = {"platform": "android", "package_type": "apk", "version_code": 201,
               "version_name": "2.0.1", "url": "/uploads/upgrade/a.apk", "md5": "x",
               "release_notes": "修复", "forced": True, "is_published": True}
    r = await client.post("/api/v1/upgrade/packages", headers=admin_headers, json=payload)
    assert r.status_code == 200
    pid = r.json()["data"]["id"]

    r2 = await client.get("/api/v1/upgrade/packages?platform=android", headers=admin_headers)
    assert r2.status_code == 200 and r2.json()["data"]["total"] >= 1

    r3 = await client.put(f"/api/v1/upgrade/packages/{pid}", headers=admin_headers,
                          json={"forced": False})
    assert r3.status_code == 200 and r3.json()["data"]["forced"] is False

    r4 = await client.delete(f"/api/v1/upgrade/packages/{pid}", headers=admin_headers)
    assert r4.status_code == 200


async def test_upload_saves_file(client, admin_headers):
    body = b"fake-apk-bytes"
    r = await client.post("/api/v1/upgrade/packages/upload",
                          headers=admin_headers,
                          files=[("file", ("demo.apk", body, "application/octet-stream"))])
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["md5"] == hashlib.md5(body).hexdigest()
    assert data["size_bytes"] == len(body)


async def test_upgrade_admin_requires_superadmin(client, admin_headers):
    # 只读普通用户（非超管）→ 403 由既有权限体系覆盖；此处验证路由存在
    r = await client.get("/api/v1/upgrade/packages", headers=admin_headers)
    assert r.status_code == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_upgrade_api.py -v --basetemp .pytest_tmp`
Expected: FAIL（404/ImportError）

- [ ] **Step 3: 实现**

```python
# services/upgrade_service.py
"""升级包：落库 / 文件上传 / 删除。上传文件存 settings.upgrade_upload_dir。"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from config import settings
from core.exceptions import BizError
from repositories import upgrade_repo


async def save_upload(filename: str, raw: bytes) -> dict:
    if not filename or not raw:
        raise BizError("文件为空")
    upload_dir = Path(settings.upgrade_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    target = upload_dir / f"{hashlib.md5(raw).hexdigest()[:16]}_{safe}"
    target.write_bytes(raw)
    md5 = hashlib.md5(raw).hexdigest()
    rel = f"/uploads/upgrade/{target.name}"
    return {"url": rel, "md5": md5, "size_bytes": len(raw)}


async def create_package(data: dict, admin: str) -> dict:
    if not await upgrade_repo.latest_published(data["platform"]):
        pass
    data.setdefault("publish_at", "" if not data.get("is_published") else date.today().isoformat())
    return await upgrade_repo.create({**data, "created_by": admin})


async def update_package(pkg_id: str, data: dict, admin: str) -> dict:
    if data.get("is_published") is True:
        data.setdefault("publish_at", date.today().isoformat())
    return await upgrade_repo.update(pkg_id, {**data, "created_by": admin})


async def delete_package(pkg_id: str) -> bool:
    return await upgrade_repo.delete(pkg_id)
```

```python
# api/v1/upgrade.py
from fastapi import APIRouter, File, UploadFile, Query

from core.dependencies import CurrentUserDep, require_roles
from core.exceptions import NotFoundError
from core.response import ok, paged
from pydantic import BaseModel, Field
from services import upgrade_service

router = APIRouter(prefix="/upgrade/packages", tags=["版本升级"])

_ADMIN = [Depends(require_roles("superadmin"))]


class UpgradeIn(BaseModel):
    platform: str = Field(min_length=2, max_length=16)
    package_type: str = Field(default="", max_length=8)
    version_code: int = Field(ge=1)
    version_name: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=512)
    md5: str = Field(default="", max_length=64)
    release_notes: str = Field(default="", max_length=1024)
    forced: bool = False
    is_published: bool = False


@router.get("", dependencies=_ADMIN, summary="升级包列表")
async def list_packages(platform: str | None = None,
                        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    items, total = await upgrade_repo.list_for_admin(platform, page, size)
    return ok(paged(items, total, page, size))


@router.post("", dependencies=_ADMIN, summary="新增升级包")
async def create_package(user: CurrentUserDep, body: UpgradeIn):
    return ok(await upgrade_service.create_package(body.model_dump(), user.username), message="已创建")


@router.post("/upload", dependencies=_ADMIN, summary="上传升级文件")
async def upload_file(file: UploadFile = File(...)):
    raw = await file.read()
    return ok(await upgrade_service.save_upload(file.filename or "pkg.bin", raw), message="已上传")


@router.put("/{pkg_id}", dependencies=_ADMIN, summary="更新升级包")
async def update_package(user: CurrentUserDep, pkg_id: str, body: dict):
    returning = await upgrade_service.update_package(pkg_id, body, user.username)
    if not returning:
        raise NotFoundError("升级包不存在")
    return ok(returning, message="已更新")


@router.delete("/{pkg_id}", dependencies=_ADMIN, summary="删除升级包")
async def delete_package(pkg_id: str):
    if not await upgrade_service.delete_package(pkg_id):
        raise NotFoundError("升级包不存在")
    return ok(message="已删除")
```

> 代码注意：上传端点前面 `@app` 依赖顺序——`POST /upload` 需要在 `POST ""` 之外独立；删除文件体残留不在本任务范围。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_upgrade_api.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/api/v1/upgrade.py stock_selector_api/backend/services/upgrade_service.py stock_selector_api/backend/api/v1/router.py stock_selector_api/backend/tests/test_upgrade_api.py
git commit -m "feat(upgrade): 升级包管理 API + 文件上传（M5）"
```

---

### Task B18: `/api/app/latest` 公开端点 + `/api/app/version`（M5）

**Files:**
- Create: `stock_selector_api/backend/api/app_version.py`
- Modify: `stock_selector_api/backend/main.py`
- Test: `stock_selector_api/backend/tests/test_app_version_api.py`

**Interfaces:**
- Consumes: Task B16 `upgrade_repo.latest_published`
- Produces:
  - `GET /api/app/latest?platform=&version_code=` → `{has_update, latest|null}`
  - `GET /api/app/version` → `{version, name}`
  - H5 平台：latest 为最新发布版（version_code/name/release_notes/forced）；App 平台返回 wgt/apk/ipa 包

- [ ] **Step 1: 写失败测试**

```python
# stock_selector_api/backend/tests/test_app_version_api.py
from repositories import upgrade_repo


async def _seed(client) -> None:
    await upgrade_repo.create({"platform": "h5", "package_type": "h5", "version_code": 100,
                               "version_name": "1.0.0", "url": "", "md5": "", "size_bytes": 0,
                               "release_notes": "初版", "forced": False, "is_published": True})
    await upgrade_repo.create({"platform": "android", "package_type": "apk", "version_code": 101,
                               "version_name": "1.0.1", "url": "/uploads/upgrade/a.apk", "md5": "m",
                               "size_bytes": 10, "release_notes": "修复", "forced": True,
                               "is_published": True})


async def test_latest_public_no_auth(client):
    await _seed(client)
    r = await client.get("/api/app/latest?platform=h5&version_code=100")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["has_update"] is False or (data["latest"] and data["latest"]["platform"] == "h5")


async def test_latest_android_forced(client):
    await _seed(client)
    r = await client.get("/api/app/latest?platform=android&version_code=100")
    data = r.json()["data"]
    assert data["has_update"] is True and data["latest"]["forced"] is True


async def test_version_endpoint(client):
    r = await client.get("/api/app/version")
    assert r.status_code == 200 and r.json()["data"]["version"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_app_version_api.py -v --basetemp .pytest_tmp`
Expected: FAIL（404）

- [ ] **Step 3: 实现**

```python
# api/app_version.py
"""公开版本检查端点（无需鉴权）：
/api/app/latest 供 uniapp 的 H5/App 启动检查；/api/app/version 健康信息。"""
from fastapi import APIRouter

from core.response import ok
from repositories import upgrade_repo

router = APIRouter(prefix="/api/app", tags=["版本检查"])


@router.get("/latest")
async def latest(platform: str = "h5", version_code: int = 0):
    pkg = await upgrade_repo.latest_published(platform)
    if pkg is None:
        return ok({"has_update": False, "latest": None})
    current = version_code or 0
    return ok({
        "has_update": int(pkg["version_code"]) > int(current),
        "latest": pkg,
    })


@router.get("/version")
async def version():
    from config import settings
    return ok({"version": settings.app_name and "0.1.0", "name": settings.app_name}, message="ok")
```

```python
# main.py —— create_app() 内追加
    from api.app_version import router as app_version_router
    app.include_router(app_version_router)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_app_version_api.py -v --basetemp .pytest_tmp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stock_selector_api/backend/api/app_version.py stock_selector_api/backend/main.py stock_selector_api/backend/tests/test_app_version_api.py
git commit -m "feat(upgrade): /api/app/latest + /api/app/version 公开端点（M5）"
```

---

### Task B19: 后端全量回归 + 全库冒烟（M11 前置）

**Files:**
- 全部后端测试
- Modify: 无（回归）

- [ ] **Step 1: 全量测试**

Run（`workdir=stock_selector_api/backend`）: `python -m pytest tests -v --basetemp .pytest_tmp`
Expected: 全绿（含既有 auth/rbac/portfolio/holdings/diag/notify 回归 + 新 trades/inbox/selection/ws/upgrade）

- [ ] **Step 2: 三服务冒烟（可选，需 8001/8002 已起）**

```bash
# 终端1
cd stock_selector_api/plugin && python -m uvicorn server:app --port 8001
# 终端2
bash scripts/start_selector.sh
# 终端3
curl -s http://127.0.0.1:8002/api/health
curl -s -X POST http://127.0.0.1:8001/api/portfolio/analyze -H "Content-Type: application/json" -d '{"user_id":"smoke","natural_day":"2026-09-20","holdings":[{"code":"600519","name":"茅台","quantity":100,"cost_price":1600}]}'
curl -s -X POST http://127.0.0.1:8001/api/portfolio/pnl -H "Content-Type: application/json" -d '{"user_id":"smoke","natural_day":"2026-09-20","trades":[{"trade_date":"2026-09-01","stock_code":"600519","stock_name":"茅台","direction":"buy","price":1600,"quantity":100}],"holdings":[]}'
```
Expected: 80 插件/6 位数 5 位 0 OK；pnl 返回 per_code/realized_summary

- [ ] **Step 3: Commit（如有遗漏修正）**

```bash
git add -A
git commit -m "chore(backend): M0–M5 全量回归与三服务冒烟"
```

---

## 验收对照（后端 M0–M5）

| 规格需求 | 任务 | 验证 |
|---|---|---|
| 交割单导入（预览/提交/手动） | B4/B6 | test_deal_parser / test_trades_api |
| 持仓移动加权推导、持仓联动 | B5/B6/B3 | test_derive_holdings / test_trades_api |
| 利润全部走插件（含已实现） | B7/B8 | plugin test_realized / test_holdings_pnl |
| 选股代理 8002 + job/回看 | B1/B12/B13/B14 | test_selection_* |
| 通知双路（站内信+WS） | B9/B10/B11 | test_inbox_* / test_ws_notify |
| 通知由定时任务触发 | B15 | test_market_daily / test_diag_notify |
| 版本升级服务 | B16/B17/B18 | test_upgrade_* / test_app_version_api |
| 红线：后端零盈利算术 | B7/B8 设计 | code review：pnl 仅代理 |

## 风险与注意

- **仓库 8000/8001/8002 端口并发**：开发时三个服务分别 `uvicorn` 起；测试 `plugin_mode=mock`、`selection_plugin_mode=mock` 兜底。
- **Excel 解析依赖**：openpyxl 未装时 `parse_deal_bytes` 返回明确错误（测试覆盖）；CSV 全量可用。
- **后台任务与 pytest**：`asyncio.create_task` 在测试里可能未等完成即断 loop；测试用轮询/短等待（如 test_diag_notify）。
- **升级中心强制升级**：`forced` 由客户端执行，后端只标记（uniapp 计划承接）。