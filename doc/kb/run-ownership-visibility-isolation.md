---
type: knowledge
scope: module
trigger: ["数据隔离", "运行归属", "系统级运行", "多用户可见性", "manual"]
created: 2026-09-15
updated: 2026-09-15
confidence: 0.85
---

## [架构约束] 运行归属与可见性（manual / 系统级双态隔离）

多用户系统中将"任务运行"划分为两态：手动运行（manual）归属具体用户，仅本人可见；系统级运行（scheduled/调度导入）为全员可见共享数据。配置类数据（订阅策略）允许全局共享，仅用户私有数据（渠道/站内信/历史）按用户隔离。

## [Solution]
- `select_runs.owner_type(user|system)` + `owner_id`：user=归属该用户；system=前台全员可见。
- 通知转发规则：manual 完成 → 仅推送给归属用户；system 完成 → 推送全部用户。
- 全局共享项（订阅策略/通知规则）与按用户隔离项（渠道、站内信、历史、手动运行）**明确区分**。
- 越权校验：仅本人可查/改本人私有数据；系统级数据全员可见。

## [Constraints]
- 前置：认证体系已有多账号（sys_users）。
- 副作用：全局配置的改动影响所有用户，需在需求文档声明"全局共享"边界。
- 禁止：将全局配置误设为按用户复制，或把系统级运行数据私有化。

## [Verification]
- 用户 A 手动运行仅 A 可见；调度导入运行 A/B 均可见且均收到通知。

## [Environment]
- 语言/框架：FastAPI + SQLite/Mongo；多账号 JWT 认证。
- 依赖：owner 字段建模 + 按 user 过滤的查询接口。

## [Related]
- stock_selector_api select/notify 模块；TR1 需求多用户语义。