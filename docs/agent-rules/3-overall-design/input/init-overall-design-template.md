# 总体设计阶段 · 背景情报简报

> **AI 使用说明**
>
> 本文档由 AI 在开始生成总体设计前发送给用户。
> 目的是收集 AI **无法仅从系统需求文档和代码库中自行推断**的背景情报、架构约束、团队决策，以及技术需求设计所需的前置输入。
>
> **填写规则：自由格式即可。**
>
> ---
>
> **本份简报已由 AI 依据 TR2《系统需求》+ 现有代码 + 产品知识库预填。**
> **请用户重点核对：§0.1 设计类型、末尾"待确认问题"4 项，其余为既有事实，仅作备查。**

---

## §0 技术需求设计前置信息

### §0.1 本次设计类型

- **特性技术需求设计（推荐）**：在既有 插件化选股系统 与 FastAPI 后端模板之上新增一组业务特性——选股执行代理与快照、实时推送（WebSocket）、站内信、版本管理、UniApp 客户端。**不改变既有后端分层架构**，仅新增业务模块与接入形态，需要对若干关键技术方案选型（任务执行模型、推送实现、渠道隔离、快照存储）。
- 不选"架构技术需求设计"的原因：无新产品/新架构/重大重构；后端框架（FastAPI 模板）、选股插件五层架构、通知组件全部延续，仅在其边界外新增特性。

### §0.2 本次技术需求设计范围

**关键业务场景与用户：**
- 用户(个人)在手机/PC 触发一次选股（手动 channel=manual 或外部调度 system），后端代理调用选股插件 → 拿到结构化结果 → 快照落库 → 立即推送 WebSocket 即时消息 + 写入站内信。
- 用户未在线或网络抖动 → 打开 App 后从站内信补齐（since=latest_id）。
- 用户配置订阅策略：哪些事件（选股命中/系统通知）通过什么渠道（企微/钉钉/飞书/邮箱）推给自己，同一事件并发去重、节流。
- 用户完善自己的通知渠道（各端唯一性校验、密钥 AES-GCM 加密落盘）。
- 用户检查到新版本（Android 整包 APK 下载 + 安装引导）。

**待选型的关键技术问题：**
1. 后端代理选股的执行模型：进程内 asyncio 后台任务（单飞+超时+失败重试） vs 引入外部任务队列（Celery/Redis Queue）。
2. 实时推送实现：FastAPI 原生 WebSocket（单进程内存连接表 + 按 user_id 路由） vs 引入消息中间件分发。
3. 选股快照/站内信存储：沿用既有 SQLite（app.db，新表 select_*） vs 切 Mongo。
4. 通知渠道多用户隔离：扩展既有 notify_channels.json 增加 user_id 维度 vs 新增 DB 表承载用户渠道。
5. Android APK 版本文件托管：后端静态文件目录 vs 外部对象存储。

**选型评估的约束目标：**
- 个人单用户工具，按需触发，规模极小（并发连接 ≤10，选股结果单日 ≤ 数百条）。
- 首要约束：① 不新增常驻外部服务（不引入 Redis/Celery/RabbitMQ）；② 复用既有组件边界最小化改动；③ 部署形态保持单机（Windows 本机或 Linux 服务器）；④ 可恢复性优先（掉线必补、失败可重试、数据不丢）。

**架构领先性/竞品对标（架构型 TRD 填写）：** 不涉及（个人工具，无竞品对标诉求）。

**TRD 的分析边界（以下已定，不再重新分析）：**
- 定时任务不进 FastAPI（外部调度 → 插件 → `POST /select/import` 幂等）【已定】
- 实时通道选定 WebSocket（非 MQTT/轮询）【已定】
- 不引入 UniPush/厂商推送【已定】
- 通知发送复用 notify_service（企微/钉钉/飞书/邮箱 webhook + AES-GCM）【已定】
- 数据库引擎基本盘沿用 SQLite 默认配置，不做引擎对选；仅评估"新业务数据"是进 SQLite 还是 Mongo

### §0.3 已有技术需求设计材料、预研与选型结论

- 系统需求（TR2 已批准，含 DFX 量化指标）：`docs/agent-rules/2-system-requirements/output/2026-09-15-select-uniapp/`
- AI 需求（TR1，A/96.8）：`docs/agent-rules/1-ai-requirements/output/2026-09-15-select-uniapp-requirements.md`
- 既有架构文档：`docs/spec/插件化量化选股系统 - 需求与架构设计文档.md`
- 产品知识库（4 条归档）：`doc/kb/plugin-query-snapshot-pattern.md`、`dual-notify-offline-compensation.md`、`run-ownership-visibility-isolation.md`、`external-scheduler-decoupled-import.md`

### §0.4 已确定的技术选型和约束

- **DB 基本盘**：后端默认 `db_backend=sqlite`（`sqlite+aiosqlite`，`data/app.db`）；Mongo 仅在全局开关切换下生效，本期不引入 MongoDB。
- **认证**：必须复用后端 `sys_users`/`core`（jwt|session + `ensure_password_current` 密码到期网关 + `require_permission`），不能绕过。
- **实时通道**：WebSocket，`/ws/notify`，鉴权复用统一认证，按 user_id 路由；断线由站内信持久化补齐。
- **版本管理**：`GET /api/v1/app/version`，`force=0/1`；本期只做 Android 整包 APK（uni.downloadFile + 进度 + MD5 + 安装引导）；不做 iOS 分发与 wgt 热更新。
- **任务治理**：可选日志、日志查询分页、复用 `APP_DEFAULTS` 快照查询模式；`POST /select/import` 幂等创建运行。
- **写操作审计**：`audit_logs` 表 + `security.py` 审计写。
- **多用户语义**：渠道/站内信/manual 运行按用户隔离；订阅策略全局共享；system 运行全员可见。
- **双方端口**：选股插件默认 8000 与后端冲突 → **待协调**（见待确认 Q3）。

---

## §1 现有架构与系统资料 ⭐强烈建议提供

**服务清单及职责：**

| 服务/子系统 | 职责 | 代码位置 |
|---|---|---|
| 插件化量化选股系统 | 五层架构：入口(命令行/模块/HTTP) → 核心调度 run_select() → 策略插件 → 数据源(aKShare) → 模型工具；统一输出 code/name/signal/reason | `D:\select_stock\stock_selector\` |
| 后端（安全管控平台模板） | FastAPI 模板：auth（jwt/session+验证码+锁定）、sys_users/roles/menus/permissions RBAC、通知组件、设备示例模块 | `D:\select_stock\stock_selector_api\backend\` |
| 通知组件 | 渠道配置(wecom/dingtalk/feishu/email JSON+密钥AES-GCM) + 订阅策略(policy JSON) + 事件节流 + 发送器 | `backend/services/notify\*`、`notify_service.py` |
| 外部调度 | 非 FastAPI 内，独立进程/脚本触发插件执行 | `D:\select_stock\scheduler\`、`scripts\` |
| UniApp 客户端（本期新增） | 4 Tab 响应式：自选股票/选股计划/消息中心/我的；Android 打包 | 新建 `D:\select_stock\uniapp\`（待定） |

**调用关系：**
- 客户端 → 后端 `/api/v1/*`（HTTP，jwt 头）+ `/ws/notify`（WebSocket，token 参数）
- 后端 → 选股插件 `/api/strategy/list`、`/api/selector/run`（HTTP，同步阻塞，需超时保护）、`/api/health`
- 外部调度 → 选股插件 `/api/selector/run`（system 运行）
- 后端 → 企微/钉钉/飞书 webhook、SMTP（notify_service）

**部署形态：** 单机。后端 `uvicorn` 8000（待协调），插件 uvicorn 8000（待协调）。数据全部落 `stock_selector_api/data/`。

---

## §2 依赖文档 （选填）

- TR2 系统需求：`docs/agent-rules/2-system-requirements/output/2026-09-15-select-uniapp/index.md`（+ ch01~ch09）
- TR1 需求（Epic/Feature/Story）：`docs/agent-rules/1-ai-requirements/output/2026-09-15-select-uniapp-requirements.md`
- 既有架构：`docs/spec/插件化量化选股系统 - 需求与架构设计文档.md`
- 知识库索引：`doc/kb/INDEX.md`
- OpenAPI 契约模板：`templates/openapi-template.yaml`

---

## §3 必须复用或不能改动的现有服务 （选填）

- **认证/RBAC**：复用 `sys_users`、`core/dependencies.require_permission`、`ensure_password_current`；用户表结构不加字段。
- **通知发送链路**：复用 `notify_service`（senders/channels/events/节流）；渠道存储可**扩展 user_id 维度**，但渠道类型元数据、发送器接口不变。
- **渠道密钥加密**：复用 `core/security` 的 AES-GCM 加解密。
- **选股插件**：内核 `run_select()` 与插件契约 `BaseSelectStrategy.run(df)` 不改；`api.py` 三个端点 shape（list/run/health)保持兼容。
- **调度**：外部调度不进 FastAPI（`external-scheduler-decoupled-import`）。
- **数据库引擎**：沿用既有 `db_backend` 开关，本期以 SQLite 为基线，不引入新数据库服务。
- **4 Tab 响应式骨架**：手机底部 Tab / PC≥768px 侧栏，作为 uni-app 新建工程骨架。

---

## §4 与外部系统的集成关系 （选填）

- 选股插件：`GET /api/strategy/list`、`POST /api/selector/run`、`GET /api/health`（需解决端口冲突 + 同步阻塞超时）
- 通知渠道：企微群机器人、钉钉自定义机器人、飞书自定义机器人（webhook）、SMTP 邮箱
- K线数据源：aKShare（插件内，后端不感知）

---

## §5 已知的性能和规模约束 （选填）

- 单用户个人工具，无多租户并发诉求。
- 并发 WebSocket 连接 ≤10；同时进行的选股代理任务 ≤1（单飞）。
- 选股结果单次 ≤ 数百条；站内信留存 ≥180 天（日志 ≥90 天）。
- 登录成功响应 ≤5s（PERF-05）；选股快照落库 ≤3s（PERF-03）；实时消息延迟 ≤5s（PERF-04）。
- 建议硬件：单核≥2G 或更高，无特殊要求。

---

## §6 硬件方案输入（如涉及硬件则填写）

纯软件产品，不涉及硬件方案。

---

## §7 其他设计约束或背景 （选填）

- **端口冲突遗留**：选股插件默认 `:8000` 与后端 uvicorn 默认 `:8000` 冲突，必须在本设计中给出协调结论（见待确认 Q3）。
- 部署目标：Windows 本机开发 + Linux 服务器可迁移；无 Docker 强制要求。
- APK 产物放置：后端 `data/` 或项目 `dist/` 目录（静态文件服务承载）。
- 代码注释规范：后端采用中英混合，本设计产出代码需遵守团队编码规范（`rules/coding-standards/`）。

---

## §8 涉及服务的本地代码路径 ⭐强烈建议提供

| 服务/子系统名 | 本地代码根目录路径 | 说明 |
|---|---|---|
| 插件化量化选股系统 | `D:\select_stock\stock_selector\` | 选股内核+HTTP 入口 api.py（8000，待协调） |
| 后端模板 | `D:\select_stock\stock_selector_api\backend\` | FastAPI；新模块挂 `api/v1/router.py` |
| 通知组件 | `backend\services\notify*` | channels/events/senders/notify_service（复用） |
| 外部调度 | `D:\select_stock\scheduler\`、`D:\select_stock\scripts\` | system 触发入口 |
| UniApp 客户端 | 新建 `D:\select_stock\uniapp\`（待定） | 4 Tab 响应式客户端 |

---

## 已确认的架构决策（用户 2026-09-16 确认，含 9 项修复）

- **Q1 · TRD 类型 = 特性技术需求设计**（既有架构不变，新增特性+选型）。
- **Q2 · 执行模型 = 进程内 asyncio 后台任务**，不引入 Celery/Redis。**修复升级：**
  - 单飞锁用 **DB 原子更新**（`UPDATE select_runs SET status=running WHERE id=? AND status IN (pending,interrupted)`，影响行数判断），不依赖进程内存。
  - 部署**硬约束单 worker**（`--workers 1`），避免 WS 连接表与单飞锁在进程间失效。
  - 服务启动时把遗留 `running` 自愈为 `interrupted`（可重试）。
- **Q3 · 端口协调 = 后端保持 8000，选股插件改 8001**（`plugin_base_url` 配置化）；WS `/ws/notify` **首帧鉴权**（连接建立后第一条鉴权消息，60s 未鉴权断开），token 不进 URL/日志（对齐 ST-07）。
- **Q4 · 存储 = 沿用 SQLite 新建表**（`select_runs`/`select_results`/`inbox_msgs`/`user_channels`/`app_versions`），不引入 Mongo。**修复升级：**
  - SQLite **WAL 模式 + busy_timeout**，写走单仓储串行化（aiosqlite 单连接队列）。
  - 消息**先落库后推送**；客户端每次重连+收到 WS 消息携带 `latest_inbox_id` 增量对齐。

**多用户/并发相关（2026-09-16 补充确认）：**
- 选股执行全局 DB 单飞（同一时刻仅 1 个任务），多用户并发时后续请求返回 `in_progress`/`queued` 排队语义 + 完成通知，不允许无反馈的沉默等待。
- `POST /select/import`（system 入口）同样参与单飞判定，覆盖 manual 与 system 互斥；插件侧建议进程内任务锁作最后防线。
- 通知节流 state 增加 `user_id` 维度并加写锁（避免多用户同写 `notify_send_state.json` 竞争）。
- 用户渠道进 DB 后，`send_event` 的 channel_key 解析改为"全局 JSON + 用户 DB"双源；既有通知 API 全量回归。

**其余已定修复项（纳入 §8 风险分析与设计）：**
- 代理侧超时 60~300s 可配 + 失败重试 1 次；插件超时/5xx 映射为 run 失败状态（可重试）。
- APK 强制升级 `force=1` 需防死循环：客户端 MD5 校验失败提供"暂仍使用旧版"出口；版本记录 `enabled` 启停开关。