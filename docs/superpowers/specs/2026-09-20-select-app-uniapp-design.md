# 智能选股 App（uniapp）— 设计与实施规格

> 文档类型：设计规格（Design Spec）
> 日期：2026-09-20
> 仓库：`D:\select_stock`（分支 `master-portfolio-plans-docs`）
> 前置交付：M1 诊断引擎 / M2 持仓诊断插件 / M3 持仓 CRUD+CSV 导入 / M3 外部诊断批次（均已提交）

## 1. 目标

交付一套以 **uniapp 为前端（H5 + Android/iOS App 打包）** 的智能选股 App，覆盖：

1. uniapp 前端，支持手机和电脑；
2. 支持通知（站内信 + WebSocket 实时推送）；
3. uniapp 版本更新后自动更新（自托管 uni-upgrade-center 等价实现）；
4. 券商交割单导入 + 手动录入，实现持仓、持仓记录（交易记录）与持仓利润分析；
5. 选股（策略列表 / 手动触发 / 历史回看 / 定时任务自动选股）；
6. 选股结果**不**由后端/前端计算，调用选股插件 HTTP 服务获取；
7. 利润（浮动盈亏 + 已实现盈亏）**不**由后端/前端计算，调用持仓诊断插件 HTTP 服务获取；
8. 通知由定时任务触发，uniapp 通过站内信 + WS 接收。

## 2. 现状盘点（已交付、复用不改动）

| 资产 | 位置 | 说明 |
|---|---|---|
| 后端 API | `stock_selector_api/backend/`（FastAPI，端口 8000） | 认证/JWT/RBAC/验证码、用户、`/api/v1/holdings` CRUD+CSV 导入、`/api/portfolio/analyze` 代理、`/api/portfolio/runs`、`/api/external/portfolio/diag` 外部诊断批次（幂等/单飞/重试）、通知组件框架（渠道/策略/节流）、调度器框架（`scheduler/` + `conf/inputs.conf`） |
| 持仓诊断插件（利润插件） | `stock_selector_api/plugin/`（独立 FastAPI，端口 8001） | `POST /api/portfolio/analyze`：判定(sell/hold/add)+收益快照+summary；**利润计算以插件为唯一口径** |
| 选股插件（内核） | `stock_selector/`（`api.py` 为 HTTP 入口，默认端口需改 8002） | `GET /api/strategy/list`、`POST /api/selector/run`；策略 `trend_rebound` / `box_oscillate`；全市场选股较重 |
| 老前端 | `stock_selector_api/frontend/static/`（静态 HTML） | 被 uniapp 取代；本版本不再演进 |

**端口占用现状**：Backend=8000（已配）、持仓诊断插件=8001（已配）。选股插件 `stock_selector/api.py` 注释默认 8000，与本仓库 backend 冲突，**改为 8002**。

## 3. 范围边界（YAGNI）

- 不做 uniCloud 云函数版升级中心；升级中心用后端 FastAPI 自托管等价能力。
- 不做第三方 App 离线推送（个推/uni-push）；通知仅站内信 + WS（在线时接收）。
- 不重做 M2/M3 已交付能力；在其上叠加。
- 外部渠道通知（钉钉/企微/飞书/邮件，模板工程已有）沿用但**不在本版本扩展**；策略默认关闭。
- 选股不做回测、不做机器学习；仅执行既有 `run_select()` 内核语义。
- 交割单走「预览→确认→提交」双步；解析即服务端做（后端），前端只上传文件并展示校验结果。

## 4. 架构

```
┌─────────────────────────────── uniapp 前端（uniapp/，H5 + App 打包） ───────────────────────────────┐
│  页面：登录 / 持仓 / 交割单导入 / 交易记录 / 利润分析 / 选股 / 诊断回看 / 消息 / 我的 / 版本检查      │
│  纯逻辑（src/logic/*，node --test）：路由守卫 / WS 状态机 / 视图映射（零计算）                        │
│  网络：src/api/client.js 统一注入 token、401/428 处理；文件上传（交割单）走 client 上传 adapter      │
└───────────────┬───────────────────────────────────────────┬──────────────────────────────────────┘
                │ HTTP /api/**、/ws/notify                    │（App 热更检查 /api/app/latest 亦走 backend）
                ▼                                              ▼
┌────────────────────── Backend API（8000）──────────────────────┐
│ 认证/RBAC/用户（既有）                                          │
│ 持仓 CRUD（既有）+ 交易记录 trades + 交割单导入 + 持仓移动加权推导 │
│ 选股代理：/api/v1/selection/* → 选股插件(8002)                  │
│ 利润代理：/api/v1/holdings/pnl → 持仓诊断插件(8001)             │
│ 诊断 run 存储（既有）+ 站内信 inbox + WS /ws/notify             │
│ 版本升级服务 upgrade_packages + /api/app/latest                 │
│ 定时任务宿主：daily_diag / daily_selection（conf/inputs.conf）  │
└──────┬──────────────────────────┬───────────────────────────────┘
       │ HTTP                        │ HTTP
       ▼                             ▼
选股插件（8002）                   持仓诊断插件（8001）
stock_selector/api.py              plugin/server.py（既有 M2）
GET /api/strategy/list   ┌─────────┴─────────┐
POST /api/selector/run   │ POST /api/portfolio/analyze（既有）│
（改端口，不改内核）      │ + NEW POST /api/portfolio/pnl       │
                         └───────────────────┘
```

**计算边界（红线）**

| 计算类型 | 计算位置 | 前端行为 |
|---|---|---|
| 选股结果 | 选股插件（8002） | 只渲染 |
| 浮动盈亏 / 判定 | 持仓诊断插件（8001 analyze） | 只渲染 |
| 已实现盈亏（成本序列） | 持仓诊断插件（8001 pnl） | 只渲染 |
| 持仓数量/移动加权成本（持仓簿记，供诊断入参与展示） | 后端导入时落库 | 只渲染 |
| 站内信未读数等元信息 | 后端 | 只渲染 |

**约束**：后端 `services/holdings_service`、`services/diag_service` 等不得新增任何盈亏/收益率算术；一律走 `portfolio_plugin` 代理。移动加权成本序列算法**单点定义在持仓诊断插件内**（收到 trades 时复现），后端持仓表仅保存推导结果（数量+成本）作为参考与诊断入参，避免两处成本口径漂移。

## 5. 数据模型（后端，默认 SQLite，遵循既有 repository(dict)+UUID 模式）

### 5.1 `trade_records`（交易记录）
- `trade_id`(PK-UUID)、`user_id`、`trade_date`(YYYY-MM-DD)、`stock_code`、`stock_name`、`direction`(buy|sell)、`price`、`quantity`(>0)、`amount`、`commission`(佣金)、`stamp_tax`(印花税)、`transfer_fee`(过户费)、`settled_amount`(发生金额/结算金额)、`source`(import|manual)、`batch_id`(交割单导入批次，可空)、`created_at`
- 说明：一条交割单 = 一条买入或卖出记录；手动录入同等。

### 5.2 `holdings`（扩展既有表）
- 既有字段保留；新增 `derived_from_trades`(bool，导入批次推导)、`last_derived_at`
- 推导规则：按 `stock_code` 分组，买入增加持仓，卖出减少；移动加权成本 `avg_cost=(旧仓量*旧成本 + 买入量*买入价)/(总仓量)`（卖出不改成本基准）；费用计入对应方向已实现（由插件口径）。

### 5.3 `select_jobs` + `select_results`（选股任务）
- `select_jobs`：`job_id`、`user_id`、`strategies`(list)、`status`(queued|running|done|failed)、`source`(manual|scheduled)、`error`(可空)、`created_at`、`updated_at`
- `select_results`：`id`、`job_id`、`code`、`name`、`signal`、`reason`

### 5.4 `inbox_messages`（站内信）
- `id`、`user_id`、`event_code`、`title`、`content`、`read_at`(null=未读)、`related_type`(如 run_id/job_id，可空)、`created_at`

### 5.5 `upgrade_packages`（升级包）
- `id`、`platform`(h5|android|ios)、`package_type`(wgt|apk|ipa|h5)、`version_code`(int)、`version_name`、`url`、`md5`、`size_bytes`、`release_notes`、`forced`(bool)、`is_published`(bool)、`publish_at`、`created_at`、`created_by`

## 6. API 契约（新增）

### 6.1 交易记录与交割单
| 方法 路径 | 说明 |
|---|---|
| `POST /api/v1/trades/import/preview` | multipart 上传文件（.csv/.xls/.xlsx）或粘贴文本（`file`/`text`）；后端解析，返回 `{total, valid:[{...row}], errors:[{line,reason}]}`，**不落库** |
| `POST /api/v1/trades/import/commit` | body `{user_id(取自token), rows:[...交易字段], batch_note?}`；校验重放后写 `trade_records` + 推导 holdings，返回 `{imported, holdings_changed:[code...], errors}` |
| `GET /api/v1/trades?page&size&code&direction&trade_date_from&trade_date_to` | 交易记录列表（属主隔离） |
| `POST /api/v1/trades` | 手动录入单笔（含费用、direction），写记录 + 更新 holdings |
| `DELETE /api/v1/trades/{id}` | 删除单笔（回滚对应持仓推导；历史批次原则上不改，仅单笔来源可删） |
| `GET /api/v1/holdings` | 既有；列表附最近诊断快照字段（latest_price/holding_pnl/pnl_pct/action，来自最近 run 明细） |
| `GET /api/v1/holdings/pnl` | 利润分析：读 user trades+holdings → 代理 8001 `POST /api/portfolio/pnl` → 透传 `{per_code:[...], realized_summary, unrealized_summary}` |

**交割单列映射**：默认列「成交日期|证券代码|证券名称|买卖标志|成交数量|成交价格|成交金额|手续费|印花税|过户费|结算金额」；`config.py` 提供 `trades_mapping` 表头别名，解析器支持 BOM/CRLF、`买卖标志` 值为「买入/卖出|买/卖|B/S」归一化。

### 6.2 选股
| 方法 路径 | 说明 |
|---|---|
| `GET /api/v1/selection/strategies` | 代理 8002 `GET /api/strategy/list`（超时/不可用回 503，提示插件未启） |
| `POST /api/v1/selection/run` | body `{strategies?}`；建 job(queued)+后台调 8002 `POST /api/selector/run`→写 `select_results`→done→站内信+WS（`selection_completed`） |
| `GET /api/v1/selection/runs?page&size&status` | job 列表（含结果条数/状态/耗时） |
| `GET /api/v1/selection/runs/{job_id}` | job 详情 + `results[]`（App 轮询） |

### 6.3 站内信与 WS
| 方法 路径 | 说明 |
|---|---|
| `GET /api/v1/messages?page&size&since_id&unread_only` | `{items,total,unread_count}` |
| `PUT /api/v1/messages/{id}/read` | 标记已读 |
| `PUT /api/v1/messages/read_all` | 全部已读 |
| `WS /ws/notify` | 首帧 `{"type":"auth","token"}`→`hello{latest_inbox_id,unread_count}`；`msg` 帧推新站内信；`ping`→`pong`；断线重连按 `latest_inbox_id` 对齐（WS 客户端状态机在 uniapp，测试覆盖纯逻辑） |

事件集扩展（`services/notify/events.py`）：`portfolio_diag_completed`、`portfolio_diag_failed`、`selection_completed`、`selection_failed`。钩子统一「站内信必写 + WS 推送」双路（外部渠道沿用但默认关）。

### 6.4 版本升级
| 方法 路径 | 说明 |
|---|---|
| `GET /api/app/latest?platform=h5|android|ios&version_code=N` | `{has_update, latest:{version_code,version_name,url,md5,size_bytes,release_notes,forced,package_type}}`；`has_update=false` 时 `latest=null`；H5 返回最新发布版（供前端比对）；App 返回 wgt/apk/ipa 包 |
| `GET /api/app/version` | 后端当前版本（健康信息） |
| `POST/GET/PUT/DELETE /api/v1/upgrade/packages` | 管理 CRUD（RBAC `sys:upgrade:update`）；`POST /api/v1/upgrade/packages/upload` multipart 上传文件存 `data/uploads/upgrade/`，返回 url+md5+size |
| `GET /api/v1/upgrade/packages?platform&page&size` | 管理列表 |

### 6.5 定时任务
复用 `scheduler/` + `conf/inputs.conf`，新增：
- `daily_diag`：每日 `HH:MM` 对所有有启用持仓的用户调用 `diag_service.trigger_diag`（幂等）→ 完成/失败事件钩子通知；
- `daily_selection`：每日 `HH:MM` 创建选股 job（默认策略，`source=scheduled`）→ 完成事件钩子通知。
任务完成后通知由 6.3 钩子实现（即「通知由定时任务实现」）。

## 7. 关键流程

### 7.1 交割单导入（双步）
1. App 「导入」页选择/上传文件（或粘贴 CSV 文本）→ `trades/import/preview`；
2. 后端解析（表头别名归一、方向归一、数值校验、6 位代码校验），返回 valid 行 + errors；
3. App 展示校验结果（可编辑后重传）；用户确认 → `trades/import/commit`；
4. 后端事务写 `trade_records`（batch_id）→ 重算 holdings（移动加权）→ 返回导入数与持仓变更；
5. 全程前端零计算（预览的解析与口径全在后端）。

### 7.2 利润分析
1. App 「利润/持仓」页请求 `GET /api/v1/holdings/pnl`；
2. 后端读该用户 trades + holdings → 代理 8001 `POST /api/portfolio/pnl`；
3. 插件复现移动加权成本：逐代码计算 已实现盈亏（Σ卖出结算金额 − 成本基准×卖出量 − 费用）与 浮动盈亏（现价×持仓 − 成本市值，现价走 pricing parquet/AKShare）；
4. 后端透传 → App 渲染（不重算）。

### 7.3 选股（手动 + 定时）
- 手动：App 选策略 → `POST /api/v1/selection/run` → 返回 job_id → 轮询 `selection/runs/{id}` → 渲染结果；
- 定时：`daily_selection` 触发同一 job 链路，完成后推站内信+WS；
- 结果快照落库，`selection/runs` 历史回看。

### 7.4 通知送达
- 诊断/选股完成（含定时触发的）→ 写 `inbox_messages` → `WsManager.push(user_id, msg)` → 在线 uniapp 收到并刷新消息页/未读数；
- 离线未收到者下次打开消息页按 `since_id`/未读补齐。

### 7.5 自动更新
- H5：`onLaunch` 与「我的」页检查 `/api/app/latest?platform=h5`，比对本地版本号，有新版本弹「版本更新」→ 刷新加载（发布即生效，带缓存清理）；
- App（Android/iOS）：`App.vue` + 手动入口按 uni-upgrade-app 逻辑检查 `platform=android|ios` → 返回 `package_type∈{wgt,apk,ipa}` + url + md5 → `plus.downloader` 下载（带进度）→ `plus.runtime.install`（wgt 静默热更；apk 整包安装）→ `forced=true` 时不可关闭；iOS 仅发布 wgt。

## 8. uniapp 前端结构

```
uniapp/
├─ package.json / vite.config.js（uni 插件 + 代理 /api、/ws） / README.md
├─ src/
│  ├─ manifest.json / pages.json（5 Tab：持仓/选股/诊断/消息/我的）
│  ├─ main.js / App.vue（守卫接入 + 版本检查启动钩子）
│  ├─ api/  client.js(token/401/428/上传adapter) · holdings.js · trades.js · selection.js · messages.js · upgrade.js
│  ├─ logic/ guard.js · csv_deal.js(交割单文本预览，可选) · ws.js · view.js（零计算映射，node --test）
│  ├─ store/ session.js
│  ├─ composables/ useClient.js
│  └─ pages/
│     ├─ login/index.vue
│     ├─ holdings/index.vue · edit.vue
│     ├─ holdings/import.vue（双步：上传/粘贴→预览校验→提交）
│     ├─ trades/index.vue（交易记录）
│     ├─ profit/index.vue（利润分析：透传 /holdings/pnl 若并入持仓页则合并）
│     ├─ selection/index.vue（策略下拉+触发+轮询+结果）· selection/history.vue
│     ├─ diag/index.vue · diag/detail.vue
│     ├─ messages/index.vue（站内信+WS）
│     └─ mine/index.vue（退出/改密/版本检查）
└─ tests/  guard · ws · view · client（node --test，零额外依赖）
```

- PC/手机自适应：H5 下宽屏容器（uni 条件样式 / scss 断点），Tab 布局在 PC 顶部或侧边导航（实现期定稿，不阻塞后端）。
- App 打包：`build:app`（HBuilderX/脚手架打包），`manifest.json` 配置版本号与升级中心地址。

## 9. 配置与部署

- `config.py` 新增：`selection_plugin_base_url=http://localhost:8002`、`upgrade_upload_dir=${BACKEND_DIR.parent}/data/uploads/upgrade`、`trades_mapping`(交割单列别名)、`daily_diag_time`、`daily_selection_time`；
- 选股插件端口 8002：`stock_selector/api.py` 启动注释与 `start` 脚本（`uvicorn stock_selector.api:app --port 8002`），不改内核；
- 启动脚本：`start_backend.sh`、`start_plugin.sh`(8001)、`start_selector.sh`(8002)、`start_scheduler.sh`；
- 数据库默认 SQLite（`data/app.db`），Mongo 可选（repo 接口不变）。

## 10. 里程碑与任务（验收可交付）

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| M0 | 选股插件端口 8002 + config 新键 + 启动脚本 + 回归 | — |
| M1 | trades 模型/repo/schema/service + 交割单解析(CSV/Excel) + preview/commit API + 手动录入 API + holdings 移动加权推导 + `/holdings/pnl` 前端前置：代理 8001 `POST /api/portfolio/pnl`（含插件扩展该端点） | M0 |
| M2 | 通知双路基建：inbox 模型/repo/service/API + WsManager + `/ws/notify` + 消息未读计数（先无业务挂钩） | M1 |
| M3 | 选股代理：selection jobs/results 模型+ repo+service+API + 8002 代理 + 完成通知挂钩 | M2 |
| M4 | 定时任务：daily_diag / daily_selection + 事件钩子（诊断/选股完成→站内信+WS）| M3 |
| M5 | 版本升级服务：upgrade_packages + 管理 CRUD/上传 + `/api/app/latest` | M2 |
| M6 | uniapp 脚手架+基础壳（H5+App 工程、5 Tab、client、守卫 session） | M1..M5 契约定稿 |
| M7 | uniapp 持仓/交割单导入/交易记录/利润分析页 | M6 |
| M8 | uniapp 选股页（手动触发+轮询+历史） | M6 |
| M9 | uniapp 诊断回看 + 消息中心 + WS 实时 | M6 |
| M10 | uniapp 自动更新（H5 检查 + App uni-upgrade-app 逻辑） | M6 |
| M11 | 端到端联调：三服务联动 + 交割单全链路 + 定时通知 + 更新升级 + 部署文档 + H5/App 双端手工冒烟 | 全部 |

任务粒度：单任务 = 先失败测试 → 实现 → 回归 → 提交；后端 pytest（`--basetemp=...` 规避 Windows 权限问题），uniapp 纯逻辑 `node --test`。总任务量控制在 ≤ 32 个。

## 11. 风险与注意

- **选股较重**：全市场选股可能分钟级；手动触发用 job+轮询，避免同步阻塞；定时任务与手动并发需 job 级单飞（按 user+strategies 组合幂等）。
- **成本口径一致性**：移动加权成本序列单点定义在持仓诊断插件；后端推导仅存快照。插件 `pnl` 与后端推导、M2 `analyze` 的 `pnl_pct` 同源同口径，回归测试锁定。
- **插件 HTTP 故障**：8001/8002 不可达 → 后端透传 503，App 显示「插件不可用」；不允许后端降级自行计算（红线）。
- **交割单格式多样性**：解析器以默认列映射 + 表头别名为主，未知列/缺失关键列走行级错误；每周支持格式做成可配置，不承诺全券商。
- **App 打包/测试**：真机 UI 与 uni 运行时差异不纳入自动化；以 README 手工冒烟清单覆盖（继承 M5 计划做法）。
- **版本强制升级**：`forced` 包必安装，UI 不得提供跳过。
- **离线装包**：uniapp `npm install` 依赖外网；若离线，脚手架结构与全部纯逻辑测试仍可验证，UI 打包留待联网执行。

## 12. 验收标准汇总

| 需求 | 验收 |
|---|---|
| 交割单导入+持仓推导 | 预览(行级错误)→提交→持仓数量/成本与手工一致（样本校验）；手动录入等价 |
| 持仓记录 | `/api/v1/trades` 列表可筛；交易记录与持仓联动 |
| 持仓利润分析 | `/holdings/pnl` 返回已实现+浮动+汇总，全部来自插件；前端无计算代码 |
| 选股 | 手动触发 job+轮询出结果；定时任务自动出结果并通知 |
| 通知 | 定时任务完成后站内信可查、在线 WS 实时收到、未读数正确 |
| 自动更新 | H5 发布新版刷新可见；App wgt 热更/整包安装通过；强制升级不可跳过 |
| 双端 | H5（手机/PC）与 Android 打包可运行核心链路 |