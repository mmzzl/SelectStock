---
type: knowledge
scope: module
trigger: ["插件查询", "快照落库", "运行快照", "外部引擎", "历史复盘", "选股落库"]
created: 2026-09-15
updated: 2026-09-15
confidence: 0.9
---

## [业务模式] 插件查询 + 运行快照落库

外部选股引擎只提供信号（code/name/signal/reason），业务系统不内嵌任何计算，仅代理查询并以"运行头表 + 结果明细"双重快照留存。

## [Solution]
- 业务层 **不内嵌、不重写引擎计算**，通过 HTTP 代理查询独立插件（地址环境变量可配）。
- 每次触发生成运行快照：`select_runs`（头表：owner_type/owner_id/trigger_type/strategies/status/result_count/started_at/finished_at/duration_ms/error）+ `select_results`（明细：run_id/code/name/signal/reason/extra JSON）。
- 明细唯一约束 `(run_id, code, signal)`，防止同一运行内重复信号。
- 历史浏览以自然日为分组维度展示运行与命中明细。

## [Constraints]
- 前置：插件与业务服务解耦，插件对业务无侵入。
- 副作用：结果时效由插件执行时机决定，业务侧只做异步任务状态管理。
- 禁止：业务侧重实现引擎算法或缓存在本地重算。

## [Verification]
- 手动触发 → 异步返回 run_id → 完成后历史可分日查明细；两次同参不产生重复明细。

## [Environment]
- 语言/框架：FastAPI（Python）+ 独立 Python 选股插件服务，HTTP 对接。
- 依赖：HTTP 客户端、SQLite/Mongo 持久化。

## [Related]
- stock_selector_api select 模块（设计阶段）；TR1 需求 F-1.3 落库模型。