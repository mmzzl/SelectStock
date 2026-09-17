---
type: knowledge
scope: domain
trigger: ["双路通知", "断线补偿", "站内信兜底", "消息不丢", "实时推送补拉"]
created: 2026-09-15
updated: 2026-09-15
confidence: 0.9
---

## [业务规则] 双路通知 + 断线补偿

通知采用"外部渠道 + 站内信"双路并写：外部 webhook（企微/钉钉/飞书/邮箱）尽力送达，站内信必写为持久兜底；实时通道（WebSocket）断线期间的消息由站内信持久化，客户端重连后按 `since=<本地 latest_id>` 补齐，不丢不漏。

## [Solution]
- 事件模型：`select_completed` / `select_failed` 等内置事件，触发后按订阅策略分发。
- 双路规则：外部渠道失败不阻塞业务，站内信无论如何必写。
- 实时通道：WebSocket 按 user_id 路由推送在线客户端；断线消息不直接补发，依赖站内信作为可靠同步源。
- 客户端可靠补偿：本地记录 latest_id，重连后请求 `GET /api/v1/messages?since=latest_id` 补齐缺口。
- 消息体含运行日期、命中股票数、前 N 条命中摘要（N 可配置）。

## [Constraints]
- 前置：实时通道仅覆盖在线/后台场景；App 被杀后不承诺离线送达（厂商推送为另一范畴）。
- 副作用：外部渠道送达率不可控，接受"双路"而非"单路可靠"。
- 禁止：把站内信做成仅在线缓存，必须持久化留痕。

## [Verification]
- 断网关停 WebSocket → 产生新事件 → 重连后拉取 since → 消息补齐且与站内信一致。

## [Environment]
- 语言/框架：FastAPI + notify_service；客户端 UniApp（uni.connectSocket）。
- 依赖：持久化消息表、事件分发链路、WebSocket 注册表（user_id→connection）。

## [Related]
- stock_selector_api notify 模块；TR1 需求 F-2.3/F-2.4。