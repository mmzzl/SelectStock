# 本地知识库索引

> 本地模式知识存储（daedalus-knowledge local fallback）。检索时优先在此 Grep。

- [plugin-query-snapshot-pattern](plugin-query-snapshot-pattern.md) — 插件查询 + 运行快照落库（头表+明细双重快照，业务不内嵌计算） | trigger: 插件查询, 快照落库, 运行快照, 历史复盘
- [dual-notify-offline-compensation](dual-notify-offline-compensation.md) — 双路通知（外部渠道+站内信必写）+ 断线按 since 补齐 | trigger: 双路通知, 断线补偿, 站内信兜底, 消息不丢
- [run-ownership-visibility-isolation](run-ownership-visibility-isolation.md) — 运行归属双态隔离（manual 归属用户 / system 全员可见）+ 配置全局共享 | trigger: 数据隔离, 运行归属, 系统级运行, 多用户可见性
- [external-scheduler-decoupled-import](external-scheduler-decoupled-import.md) — 定时任务由外部调度驱动，业务只留幂等导入接口，不在后台托管定时器 | trigger: 定时任务解耦, 外部调度, 幂等导入, 不进后台