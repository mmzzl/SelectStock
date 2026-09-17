# IPD Story 索引 — 智能选股助手 v1.0

> 生成时间: 2026-09-15
> 来源: docs/agent-rules/2-system-requirements/output/2026-09-15-select-uniapp/
> 范围: 全量 17 项 REQ（P0×15 / P1×2），全部 [新增]

## 结构概览

| 层级 | 数量 |
|------|------|
| Epic | 3 |
| Feature | 7 |
| Story | 17 |
| Tech-系统级 | 17 |
| Tech-服务级 | 28 |

## REQ → Story 映射速查

| REQ | 优先级 | Story | 交付标记 |
|---|---|---|---|
| REQ-001 | P0 | Story1.1.1-选股策略清单获取与展示 | 🔴 |
| REQ-002 | P0 | Story1.1.2-立即选股提交与执行状态 | 🔴 |
| REQ-016 | P1 | Story1.1.3-任务执行治理 | 🟡 |
| REQ-005 | P0 | Story1.2.1-外部调度导入 | 🔴 |
| REQ-008 | P0 | Story1.2.2-选股事件多通道推送 | 🔴 |
| REQ-009 | P0 | Story1.2.3-站内信 | 🔴 |
| REQ-010 | P0 | Story1.2.4-WebSocket实时消息与断线补齐 | 🔴 |
| REQ-003 | P0 | Story1.3.1-选股结果快照落库 | 🔴 |
| REQ-004 | P0 | Story1.3.2-选股历史按自然日分组查询 | 🔴 |
| REQ-006 | P0 | Story2.1.1-通知渠道管理 | 🔴 |
| REQ-007 | P0 | Story2.1.2-订阅策略配置 | 🔴 |
| REQ-017 | P1 | Story2.1.3-写操作审计 | 🟡 |
| REQ-012 | P0 | Story3.1.1-响应式导航外壳 | 🔴 |
| REQ-011 | P0 | Story3.2.1-多账号登录认证 | 🔴 |
| REQ-013 | P0 | Story3.2.2-账号与设置 | 🔴 |
| REQ-015 | P0 | Story3.2.3-多用户数据隔离与越权防护 | 🔴 |
| REQ-014 | P0 | Story3.3.1-App版本管理 | 🔴 |

## 完整映射

- **Epic1-智能选股闭环** → [README.md](Epic1-智能选股闭环/README.md)
  - **Feature1.1-手动选股与复盘** → [README.md](Epic1-智能选股闭环/Feature1.1-手动选股与复盘/README.md)
    - **Story1.1.1-选股策略清单获取与展示** → [README.md](Epic1-智能选股闭环/Feature1.1-手动选股与复盘/Story1.1.1-选股策略清单获取与展示/README.md)
      - Tech-系统级-插件策略代理链路 → [README.md](.../Tech-系统级-插件策略代理链路/README.md)
        - Tech-服务级-策略代理接口实现.md
        - Tech-服务级-客户端选股策略列表.md
    - **Story1.1.2-立即选股提交与执行状态** → [README.md](.../Story1.1.2-立即选股提交与执行状态/README.md)
      - Tech-系统级-异步选股执行链路 → [README.md](.../Tech-系统级-异步选股执行链路/README.md)
        - Tech-服务级-选股提交与状态机接口.md
        - Tech-服务级-客户端立即选股交互.md
    - **Story1.1.3-任务执行治理** → [README.md](.../Story1.1.3-任务执行治理/README.md)
      - Tech-系统级-任务执行治理 → [README.md](.../Tech-系统级-任务执行治理/README.md)
        - Tech-服务级-单并发与超时兜底.md
        - Tech-服务级-指数退避重试.md
  - **Feature1.2-系统级自动选股与通知** → [README.md](Epic1-智能选股闭环/Feature1.2-系统级自动选股与通知/README.md)
    - **Story1.2.1-外部调度导入** → [README.md](.../Story1.2.1-外部调度导入/README.md)
      - Tech-系统级-外部导入幂等管道 → [README.md](.../Tech-系统级-外部导入幂等管道/README.md)
        - Tech-服务级-导入接口与幂等实现.md
    - **Story1.2.2-选股事件多通道推送** → [README.md](.../Story1.2.2-选股事件多通道推送/README.md)
      - Tech-系统级-选股事件多通道分发 → [README.md](.../Tech-系统级-选股事件多通道分发/README.md)
        - Tech-服务级-事件触发与接收范围.md
        - Tech-服务级-外部渠道发送与兜底.md
    - **Story1.2.3-站内信** → [README.md](.../Story1.2.3-站内信/README.md)
      - Tech-系统级-站内信服务 → [README.md](.../Tech-系统级-站内信服务/README.md)
        - Tech-服务级-站内信模型与列表接口.md
        - Tech-服务级-已读与未读数接口.md
    - **Story1.2.4-WebSocket实时消息与断线补齐** → [README.md](.../Story1.2.4-WebSocket实时消息与断线补齐/README.md)
      - Tech-系统级-WebSocket推送与补齐 → [README.md](.../Tech-系统级-WebSocket推送与补齐/README.md)
        - Tech-服务级-WS端点与用户路由.md
        - Tech-服务级-客户端连接与断线补齐.md
  - **Feature1.3-选股结果快照与历史查询** → [README.md](Epic1-智能选股闭环/Feature1.3-选股结果快照与历史查询/README.md)
    - **Story1.3.1-选股结果快照落库** → [README.md](.../Story1.3.1-选股结果快照落库/README.md)
      - Tech-系统级-快照落库通道 → [README.md](.../Tech-系统级-快照落库通道/README.md)
        - Tech-服务级-运行头表与明细表写入.md
    - **Story1.3.2-选股历史按自然日分组查询** → [README.md](.../Story1.3.2-选股历史按自然日分组查询/README.md)
      - Tech-系统级-历史查询链路 → [README.md](.../Tech-系统级-历史查询链路/README.md)
        - Tech-服务级-历史分组查询接口.md
        - Tech-服务级-运行明细查询接口.md
- **Epic2-通知配置与审计** → [README.md](Epic2-通知配置与审计/README.md)
  - **Feature2.1-通知渠道与订阅策略** → [README.md](Epic2-通知配置与审计/Feature2.1-通知渠道与订阅策略/README.md)
    - **Story2.1.1-通知渠道管理** → [README.md](.../Story2.1.1-通知渠道管理/README.md)
      - Tech-系统级-渠道管理与安全存储 → [README.md](.../Tech-系统级-渠道管理与安全存储/README.md)
        - Tech-服务级-渠道CRUD接口.md
        - Tech-服务级-测试发送服务.md
    - **Story2.1.2-订阅策略配置** → [README.md](.../Story2.1.2-订阅策略配置/README.md)
      - Tech-系统级-订阅策略与节流 → [README.md](.../Tech-系统级-订阅策略与节流/README.md)
        - Tech-服务级-全局策略接口.md
        - Tech-服务级-节流时间窗实现.md
    - **Story2.1.3-写操作审计** → [README.md](.../Story2.1.3-写操作审计/README.md)
      - Tech-系统级-审计记录通道 → [README.md](.../Tech-系统级-审计记录通道/README.md)
        - Tech-服务级-审计模型与埋点.md
- **Epic3-客户端账号与安全基座** → [README.md](Epic3-客户端账号与安全基座/README.md)
  - **Feature3.1-响应式导航外壳** → [README.md](Epic3-客户端账号与安全基座/Feature3.1-响应式导航外壳/README.md)
    - **Story3.1.1-响应式导航外壳** → [README.md](.../Story3.1.1-响应式导航外壳/README.md)
      - Tech-系统级-客户端响应式架构 → [README.md](.../Tech-系统级-客户端响应式架构/README.md)
        - Tech-服务级-路由守卫与断点布局.md
  - **Feature3.2-多账号隔离与会话** → [README.md](Epic3-客户端账号与安全基座/Feature3.2-多账号隔离与会话/README.md)
    - **Story3.2.1-多账号登录认证** → [README.md](.../Story3.2.1-多账号登录认证/README.md)
      - Tech-系统级-统一认证与会话 → [README.md](.../Tech-系统级-统一认证与会话/README.md)
        - Tech-服务级-登录认证对接.md
        - Tech-服务级-token刷新与请求拦截.md
    - **Story3.2.2-账号与设置** → [README.md](.../Story3.2.2-账号与设置/README.md)
      - Tech-系统级-账号与改密链路 → [README.md](.../Tech-系统级-账号与改密链路/README.md)
        - Tech-服务级-me与改密接口对接.md
    - **Story3.2.3-多用户数据隔离与越权防护** → [README.md](.../Story3.2.3-多用户数据隔离与越权防护/README.md)
      - Tech-系统级-用户数据隔离基线 → [README.md](.../Tech-系统级-用户数据隔离基线/README.md)
        - Tech-服务级-按用户作用域过滤实现.md
  - **Feature3.3-App版本管理** → [README.md](Epic3-客户端账号与安全基座/Feature3.3-App版本管理/README.md)
    - **Story3.3.1-App版本管理** → [README.md](.../Story3.3.1-App版本管理/README.md)
      - Tech-系统级-版本托管与升级链路 → [README.md](.../Tech-系统级-版本托管与升级链路/README.md)
        - Tech-服务级-版本接口与托管.md
        - Tech-服务级-客户端下载校验安装.md