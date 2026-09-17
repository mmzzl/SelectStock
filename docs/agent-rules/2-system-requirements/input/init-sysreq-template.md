# 系统需求阶段 · 背景情报简报

> **🤖 AI 使用说明**
>
> 本文档由 AI 启动系统需求分析前生成，已从 TR1 用户需求文档（`docs/agent-rules/1-ai-requirements/output/2026-09-15-select-uniapp-requirements.md`）预填已知情报。
> 请用户**核对并确认** §1/§2/§8/§9，无需重复编写，只需补充或修正；如无异议告知 AI 即可。

---

## §1 业务背景 ⭐必填

**精炼背景（预填自 TR1 §1.1.1，请确认或修改）：**

- **用户**：个人投资者 / 选股工具使用者本人（核心）；使用者在多账号场景下兼任平台管理员。
- **要解决的问题**：现状选股必须 SSH 登录服务器执行命令行脚本，结果不落库没法按日期复盘，选股完成无主动通知；需要一个**手机/PC 都能用**的客户端把它变成"随时查看 + 定时自动选股 + 自动通知"。
- **为什么是现在**：已有独立选股插件（`stock_selector`，只查行情给信号）与通用 FastAPI 后端模板（`stock_selector_api`，含认证/通知/健康检查），缺一层易用客户端与结果沉淀。
- **关键边界**：后端**不内嵌任何选股计算**，只代理查询插件并快照落库；定时任务由外部独立调度服务发起，**不进 FastAPI**；通知完全复用现有 `notify_service`，不引入 UniPush/厂商推送；不做 K 线/自选股/持仓/权限管理页。

## §2 关键业务场景 ⭐必填

**预填自 TR1 三级场景建模与 playbook（请确认或修改）：**

1. **每日定时自动选股**：收盘后外部调度服务调用选股插件 → 拿到命中信号 → 导入后端（`POST /select/import`，系统级运行）→ 落库 + 向全部用户推送（webhook + 站内信 + WebSocket 实时消息）。期望：不登录也能第一时间在手机收到"今日命中"通知，并按自然日回看。
2. **手动立即选股**：使用者打开 App 选股 Tab → 勾选策略（≤3 步）→ 立即选股 → 后端异步代理插件查询 → 完成后结果 Tab 按自然日分组展示（code/name/signal/reason）。期望：选股历史持续可回看，不依赖终端。
3. **结果通知与复盘**：收到买入/卖出信号通知 → 打开消息 Tab 看站内信/点开详情 → 结果 Tab 回看当日命中明细。期望：消息不丢（断线重连按 since 补齐），配置渠道/策略可视化且按用户隔离。

## §3 关联的历史版本文档 （选填）

- 无历史版本。上一阶段产物（内容输入，非历史版本对比）：`docs/agent-rules/1-ai-requirements/output/2026-09-15-select-uniapp-requirements.md`

## §4 涉及的关联服务和系统 （选填）

**预填自 TR1（请确认）：**

- 选股插件 `stock_selector`（独立 HTTP 服务：`GET /api/strategy/list`、`POST /api/selector/run`、`GET /api/health`，默认端口 8000，地址可配）
- 后端 `stock_selector_api`（FastAPI：认证 `sys_users`/JWT/验证码/锁定/密码到期、`notify_service` 通知、`GET /api/health`）
- 通知渠道服务：企业微信/钉钉/飞书自定义机器人、SMTP 邮箱（webhook）
- 外部调度服务（独立定时任务，调用插件 + `POST /select/import`）
- 移动端 UniApp 客户端（新开发，本阶段核心新增）
- **应用版本托管**：后端提供版本检查接口 + 安装包托管目录（使用者手动上传 APK 与版本记录）

## §5 现有架构图或系统全景图 （选填，强烈建议提供）

**预填自 TR1（文本描述，请确认；如有更明确图可补充路径）：**

```
[外部调度服务] ──查插件──▶ [stock_selector 选股插件] ──信号──▶ [stock_selector_api 后端]
                                                                  │ 落库(select_runs/results)
                                                                  ├─▶ notify_service(渠道webhook)
                                                                  ├─▶ 站内信(inbox_messages)
                                                                  └─▶ WebSocket(/ws/notify)
[UniApp 客户端(手机/PC)] ──REST/WS──▶ [stock_selector_api 后端]──(代理查插件)──▶ [stock_selector]
```

## §6 领域术语和业务规则 （选填）

**预填自 TR1 词汇表（请确认）：**

| 术语 | 定义 |
|---|---|
| 选股插件（Plugin） | 独立运行的选股引擎服务（`stock_selector`），只查行情给信号 code/name/signal/reason |
| 选股运行（SelectRun） | 一次选股执行快照（select_runs 头表），trigger_type=manual/scheduled，含状态与统计 |
| 命中信号（SelectResult） | 一条选股结果（select_results 明细），(run_id, code, signal) 唯一 |
| 通知渠道（Channel） | 企微/钉钉/飞书/邮箱四类 webhook 渠道配置，**按用户隔离** |
| 订阅策略（Policy） | 事件+渠道类型+节流间隔的通知分发规则，**全局共享一套** |
| 站内信（Inbox） | 系统事件（选股完成/失败等）在 App 内的消息记录，按用户隔离 |
| 外部调度服务 | 调用选股插件与 `POST /select/import` 的独立定时服务（不进 FastAPI） |

**业务规则（预填自 TR1，请确认）**：
- 选股运行归属：manual → 归属用户（可见范围=owner）；scheduled/系统级 → 对所有用户可见
- 通知范围：manual 完成 → 仅推送归属用户；scheduled 完成 → 推送全部用户
- 并发与超时：同一时刻仅 1 个运行中任务；长任务 30 分钟超时标记 failed；失败自动重试 3 次指数退避
- 实时消息：WebSocket `/ws/notify` 按 user_id 路由；断线消息由站内信持久化，重连按 `since=latest_id` 补齐

## §7 其他 AI 不知道的上下文 （选填）

- 个人自用工具：规模为单节点、低并发（个位数账号）、本地/家庭网络部署；无对外 SLA、无出海、非法务强约束场景
- 部署沿用 `stock_selector_api/deploy/` 一键脚本联邦：单节点 ≤30 分钟、初始化配置 ≤10 分钟（.env：JWT_SECRET/插件地址/DB 配置）

## §8 版本需求范围界定 （选填，强烈建议提供）

**本版本 v1.0 包含（预填自 TR1，请确认）：**
- 选股：策略清单展示、立即选股（异步代理插件）、结果快照落库 + 按自然日历史浏览、选股运行明细
- 通知：渠道管理（四类 webhook CRUD + 测试发送）、订阅策略配置（全局共享）、选股完成/失败推送（webhook + 站内信）、WebSocket 实时消息 + 安卓本地通知
- 客户端基座：多账号登录（验证码）、4 主 Tab 响应式（手机底部 Tab / PC 左侧栏）
- 版本管理：启动检查最新版本（`GET /api/v1/app/version`）、Android 整包 APK 下载 + 系统安装、可选/强制双更新策略、H5 提示刷新（不下载）

**本版本明确不包含（预填自 TR1，请确认）：**
- UniPush/个推厂商离线推送通道（App 被杀后不承诺离线送达）
- 个股 K 线图、自选股/关注列表/持仓管理
- UniApp 内用户/角色/权限/RBAC 管理页、注册与密码找回（账号由后端 sys_users 供给）
- iOS 原生分发（TestFlight/App Store）与 wgt 热更新（本期仅 Android 整包 + H5 提示）
- 出海/国际化、NFV/XAAS

**后续版本规划（可选）：**
- 待定（个人工具，按实际使用反馈迭代）

## §9 DFX 约束背景（AI 预填自 TR1 §2.2，请人工确认修改）

**性能基线（预填自 TR1 §2.2.1，请确认）：**
- 高频页面加载（选股/结果/消息 Tab）：p99 ≤ 3s，无 ≥7s 页面；低频页面流畅占比 ≥90%
- 业务查询接口：p95 ≤ 1s（策略列表/历史/站内信）
- 选股执行提交：≤ 2s 返回 run_id（异步长任务不计入 1s）
- 选股任务 30 分钟内完成并落库（带超时兜底）

**可靠性基线（预填自 TR1 §2.2.2，请确认）：**
- 无对外 SLA；单节点部署，无集群/RTO/RPO/过载熔断（裁剪说明已记录）
- 手动选股失败自动重试 3 次指数退避；长任务 30 分钟超时标记 failed；select_failed 通知 + 站内信
- 破坏性操作二次确认（删除渠道/清空历史）

**可监控性约束（预填自 TR1 §2.2.2/§2.2.8，请确认）：**
- 沿用 loguru 控制台 + 滚动文件日志；配置项环境变量化（SELECT_PLUGIN_URL/并发/超时/节流）
- 健康检查 `GET /api/health` 供外部调度/监控探测；select_runs 作为操作留痕

**安全约束（预填自 TR1 §2.1/决策，请确认）：**
- 登录复用既有认证：JWT + 验证码 + 失败锁定 + 密码到期网关；多账号数据按用户隔离（越权访问返回无权限）
- 渠道密钥 AES-GCM 落盘（沿用既有 notify_service）；JWT_SECRET 环境变量配置
- 资源访问：仅本人可查/改本人渠道、站内信、手动选股运行
- 版本安装包完整性：下载后校验 MD5，防止包损坏/篡改；安装需系统"安装未知应用"授权（引导页）

**可扩展性约束（预填自 TR1，请确认）：**
- 个人工具规模（个位数账号低并发），无水平扩展需求；后端/插件均为无状态可按需重启

**兼容性约束（预填自 TR1 §2.2.6，请确认）：**
- 后端 Linux（沿用 systemd 部署）；客户端无硬件绑定
- H5 形态：Chrome 80+ / Edge 79+ / Safari 13.1+ / Firefox 72+
- 移动端：iOS / Android（UniApp 打包）+ 底部 Tab 触控适配；Android 版本升级依赖"安装未知应用"授权（引导页处理）
- 新产品首版，无历史升级矩阵；降级=git 回滚+数据库快照

**可测试性约束（请确认）：**
- 后端 OpenAPI 契约（FastAPI 既有）供自动化接口测试；插件地址可配便于替换测试桩
- 站内信/渠道/策略均可通过接口构造测试数据；WebSocket 消息可连接后可观测
- 版本接口可构造测试数据（上传测试 APK + 强制/可选版本记录），下载/安装可在模拟器验证

**开放性约束（预填自 TR1 §2.2.5，请确认）：**
- 后端全部接口 OpenAPI 文档化；`POST /select/import` 单一对外接口（外部调度），鉴权复用统一认证
- 插件对接经 HTTP（地址环境变量可配）；通知渠道为 webhook 标准
- 版本托管：`GET /api/v1/app/version` 开放给客户端匿名/登录态检查；APK 静态目录服务

**日志与可观测性约束（预填自 TR1，请确认）：**
- loguru 滚动文件日志；选股运行/站内信落库可追溯；健康检查接口；见"可监控性约束"

## §10 结构化补充信息 ⭐强烈建议提供

### §10.1 数据模型字段需求

**预填自 TR1 F-1.3/F-2.4（请确认）：**
- `select_runs`：id、owner_type(user|system)、owner_id、trigger_type(manual|scheduled)、strategies、status(running|success|failed)、result_count、started_at、finished_at、duration_ms、error
- `select_results`：id、run_id、code、name、signal、reason、extra(JSON)；唯一约束(run_id, code, signal)
- `inbox_messages`：id、user_id、type、run_id?、title、content、read_status、created_at
- 通知渠道（沿用 notify_service）：渠道类型(企微/钉钉/飞书/邮箱)、webhook 配置、密钥字段(AES-GCM)、状态、**user_id（按用户隔离）**
- 订阅策略（沿用 notify_service + 全局共享）：事件类型、渠道类型列表、节流间隔；**无 user_id（全局）**
- `app_versions`（版本发布）：version_code、version_name、platform(android/h5)、force(0|1)、url、md5、size_bytes、release_notes、released_at、status(enabled/disabled)

### §10.2 API 接口推导

**预填自 TR1（请确认）：**
- 认证（复用既有）：登录、验证码、刷新 token、改密
- 选股：`GET /api/strategies`（代理插件策略清单）、`POST /api/select/run`（提交立即选股→run_id）、`GET /api/select/history`（按自然日分组）、`GET /api/select/run/{run_id}`（运行明细）、`POST /api/select/import`（外部调度导入，系统级）
- 通知：渠道 CRUD + 测试发送（按用户）、订阅策略 GET/PUT（全局）、`GET /api/v1/messages`（站内信+未读数）、`PUT /api/v1/messages/{id}/read`
- 实时：`/ws/notify`（鉴权，按 user_id 路由推送）
- 版本管理：`GET /api/v1/app/version`（最新版本记录，含 md5/url/force/release_notes）

### §10.3 枚举值与状态机

**预填自 TR1（请确认）：**
- trigger_type：manual / scheduled
- select_runs.status：running → success | failed（终态），失败可重试
- 渠道类型：wecom / dingtalk / feishu / email
- 站内信 read_status：unread / read
- 事件类型：select_completed / select_failed（可扩展）
- app_versions：platform（android / h5）、force（0 可选 / 1 强制）、status（enabled 发布中 / disabled 停用）

### §10.4 业务规则

**预填自 TR1（请确认）：**
- 越权校验：仅本人可查/改本人渠道、站内信、手动选股运行；系统级运行全员可见
- 转发规则：manual→归属用户；scheduled→全员
- 幂等：外部导入接口幂等（重复导入不产生重复运行）
- 并发/超时：同一时刻仅 1 个运行中任务；30 分钟超时→failed；重试 3 次指数退避
- 消息不丢：WebSocket 断线由站内信持久化补齐（since=latest_id）
- 版本规则：版本比较用 version_code 数值；force=true 不可跳过；下载后 MD5 不一致不得安装；status=disabled 不再提示