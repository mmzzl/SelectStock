---
name: stock-api-conventions
description: stock_selector_api 后端分层/路由挂载/模型注册/测试运行约定
type: project
---

## 事实记录

### 2026-09-17: 外部契约路由与内部业务路由分流

**触发场景**: 新增对外系统契约接口（非内部管理页面业务）
**结论**: 内部业务路由进 `api/v1/router.py`（前缀 `/api/v1` + `_password_gate` 密码到期网关）；对外契约路由在 `main.py` 直接 `app.include_router(router, prefix="/api")`，不挂网关。例：持仓诊断 `/api/portfolio/*`。
**原因**: 方案文档 §2/§3 明确外部入口独立前缀。

### 2026-09-17: 新 ORM 表必须注册进 db/sql/__init__.py

**触发场景**: 在 db/sql/ 下新增模型后测试建表失败/表不存在
**结论**: `db/sql/__init__.py` 除了 import 模型，还要把模型类加进 `__all__`，否则 conftest 的 `create_all` 不建表。
**原因**: conftest 依赖 `from db.sql import Base` 导入链注册 metadata。

### 2026-09-17: 测试 fixture 约束与运行目录

**触发场景**: 写 stock_selector_api/backend/tests 用例
**结论**:
- 测试在 `stock_selector_api/backend` 目录运行（conftest 依赖 backend/ 在 sys.path）。
- conftest `_test_env` 置 `settings.database_url = None`（内存 SQLite 用 dbb._engine 覆盖）。**测试内勿自建依赖 settings.database_url 的 app_fx**（会崩），用 conftest 的 `client` fixture（真 app + session 级内存库）。
- 仓库分层：api/v1 → services → repositories（`pick()` 选 sql/mongo 基类，通用助手 `_find_one/_find_all/_delete_from/list/update`）→ db/sql（Base/IdMixin/TimestampMixin/new_uuid）。

### 2026-09-17: 提交规范

**触发场景**: 提交代码
**结论**: conventional commit 中文消息 + scope，如 `feat(portfolio): ...`、`fix(window): ...`、`perf(features): ...`、`chore(...): ...`。
**原因**: 仓库 git log 既有风格。