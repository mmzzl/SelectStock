# FastAPI 开箱即用模板（安全管控平台）

面向安全产品研发的 FastAPI 后端模板：**权限体系、认证、动态菜单、数据库**等基础工程一次做扎实，开新功能直接往里填业务模块，不重复造轮子。

## 特性一览

| 能力 | 说明 |
|---|---|
| 认证双模式 | `AUTH_MODE=jwt\|session` 配置即切换，JWT 支持 access+refresh 刷新续期 |
| 三档角色 RBAC | 超管 / 普通管理员 / 只读三档开箱即用，角色、权限、菜单全部入库可扩展 |
| 动态菜单 | 菜单按角色权限动态下发树形结构，前端无构建直接渲染 |
| 数据库可配置 | `DB_BACKEND=sqlite\|mongodb` 一键切换，业务代码不感知底层 |
| 定时任务 | 参考产品 `inputs.conf` 风格配置文件声明定时任务，APScheduler 执行，改配置即可加任务 |
| 统一日志 | loguru 控制台 + 滚动文件 |
| 登录验证码+账户锁定 | Pillow 图片验证码绑定匿名会话防伪造；登录/验证码失败超限按用户名+IP 双维度锁定，时长可配置、到期自动解锁 |
| 密码治理+角色权限分配 | 密码长度/复杂度可配置校验、历史防重用、定期强制改密（到期业务接口 428）；权限树展示与角色分配（模块→功能→动作） |
| 通知组件 | 企微/钉钉/飞书 机器人 webhook + 邮箱 SMTP 四类渠道，AES-GCM 加密落盘；多条订阅策略按事件+渠道+节流分发，登录失败/锁定/任务失败自动触发通知 |
| 无构建前端 | 原生 JS + hash 路由，登录页 / 动态侧栏 / 用户/角色/菜单/示例设备管理页全内置 |
| 示例模块 | 设备管理完整 CRUD，作为「新增一个功能」的标准模板 |
| 持仓诊断 | 外部契约接口 `POST /api/portfolio/analyze`（代理插件、幂等重放、零后端判定/收益）+ `GET /api/portfolio/runs`（按 user_id 隔离），双表落库 |
| 回测（Celery） | 「回测」页发起任务：参数白名单校验 + 单飞 → `send_task` 投递，worker 跑内核，逐日进度回显；后端零计算，结果摘要落 `backtest_jobs`、明细进 Mongo |
| 系统监控 | `GET /api/v1/system/metrics`（psutil 只读采样 CPU/内存/Swap/磁盘/后端进程 RSS），前端 5 秒自动刷新 |
| 部署就绪 | 本机 Ubuntu 走 supervisor 五进程 + nginx 8080 反代（配置副本在仓库 `deploy/`）；`deploy/` 内另留 CentOS systemd 单元与安装脚本 |

## 快速开始（开发）

```bash
cd fastapi_template
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # 按需修改
./start.sh --dev            # 热重载启动 http://localhost:8000
```

浏览器打开 http://localhost:8000/ ，默认账号 `admin / admin123`（超管）。

> 本机正式实例不跑 8000（该端口被 `lex_math_new` 占用）：后端由 supervisor 托管在 **8010**，
> 对外经 nginx **8080**。本地临时起 dev 前先 `ss -ltn | grep 80` 确认端口没撞，别和常驻实例并跑。

### 跑测试

```bash
cd backend
../.venv/bin/python -m pytest tests/ -q
```

## 架构总览

```
backend/                      # FastAPI 应用
├── main.py                   # app 工厂 + lifespan(建表/seed/调度器)
├── config.py                 # pydantic-settings 全量配置
├── core/                     # 基础能力: security(双模式令牌)/dependencies(权限)/response/exceptions/logger
├── db/                       # DB 一切: sql(模型) + mongo(indexes) + seed(初始化数据) 统一入口
├── schemas/                  # Pydantic DTO（字段校验唯一真相源）
├── repositories/             # 数据访问: sql_impl + mongo_impl 双实现 + 各实体 Repo + rbac 聚合
├── services/                 # 业务: auth_service / rbac_service / device_service / captcha_service / notify_service + notify/
├── api/v1/                   # 路由: auth(含验证码) / sys_users / sys_roles / sys_menus / sys_permissions / devices / notify
├── scheduler/                # 定时任务: loader(解析 inputs.conf) + scheduler_(APScheduler) + tasks/
├── tests/                    # pytest 冒烟（登录→菜单→CRUD→权限隔离→刷新）+ 锁定/密码/RBAC/通知专项
└── conf/inputs.conf          # 定时任务声明文件（参考产品约定）
frontend/static/              # 无构建前端（由 FastAPI 直接托管）
deploy/                       # systemd unit + install/uninstall 脚本
```

### 分层依赖铁律

- 只有 `db/` 与 `repositories/` 能碰 SQLAlchemy/Motor；`schemas/services/api` 一律不得 import DB 技术栈
- `api → services → repositories → db`，禁止反向或跨层
- 所有 ID 统一 UUID 字符串，时间戳统一 `datetime`（跨 DB 一致的关键约定）

## 为核心能力设计（对应你关心的点）

### 1. 权限体系一句话

```python
from core.dependencies import require_permission

@router.delete("/{id}", dependencies=[Depends(require_permission("device:delete"))])
async def delete_device(id: str): ...
```

业务开发者加一行依赖即可完成鉴权；超管/普通/只读由 seed 初始化，角色、权限码、菜单在管理界面配置后立即生效。权限码约定 `<模块>:<动作>`（如 `device:create`），与菜单的 `permission_code` 一一对应。

### 2. 认证双模式

`AUTH_MODE=jwt` 用无状态 JWT（access 30 分钟 + refresh 7 天）；`AUTH_MODE=session` 用服务端 sessions 表 + HttpOnly Cookie。业务代码不感知——都走 `get_current_user` 依赖。

### 2.5 登录验证码（防爆破，参考产品 vericode 语义）

- `GET /api/v1/auth/captcha` 生成 PNG 图片验证码（Pillow 绘制：字符 + 噪点 + 干扰线），返回 `{captcha_id, image_base64}`，并签发 HttpOnly 匿名会话 cookie `anon_sid`
- 登录时提交 `captcha_id + captcha_code`；校验要求 **code 匹配 + 会话 cookie 匹配 + 未过期 + 未使用** 四重条件，用后即焚（一次使用即失效，攻击者即使截获验证码也无法复用）
- **防伪造核心**：验证码记录绑定 `anon_sid` cookie，跨客户端/会话无法复用；错误或伪造即焚码，阻断撞库猜测
- 字符集剔除易混淆字符（0/O/1/I/l），校验大小写不敏感
- 开关：`CAPTCHA_ENABLED`（生产建议 true）、`CAPTCHA_LENGTH`、`CAPTCHA_EXPIRE_SECONDS`

### 2.6 账户锁定（暴力破解防护）

登录失败与验证码失败共享一套计数器，命中各自阈值即锁定，防止撞库与爆破：

- **双维度**：`user:{用户名}` 与 `ip:{客户端IP}` 两条 key 独立计数、独立锁定，任一维度命中即拒绝；换用户名也逃不掉 IP 锁
- **共用计数**：验证码输错与密码输错累加在同一个 key 上；`CAPTCHA_MAX_FAILURES` 控制验证码阈值、`LOGIN_MAX_FAILURES` 控制登录阈值，两者分开配
- **可配置锁定时长**：命中阈值锁定 `LOCK_SECONDS` 秒（默认 300 = 5 分钟，配 0 即不锁定），锁定期内即使凭证正确也返回 429
- **到时自动解锁**：锁定期满后首次请求懒清除（`check_locked` 发现过期即解锁），无需人工干预
- **登录成功清零**：成功后清除该用户名与 IP 的历史失败计数
- **代理场景**：`LOCK_TRUST_PROXY=true` 时取 `X-Forwarded-For` 首 IP 计算 IP 维度（Nginx 后置部署建议开启）
- 配置项：`LOGIN_MAX_FAILURES`、`CAPTCHA_MAX_FAILURES`、`LOCK_SECONDS`、`LOCK_TRUST_PROXY`（均见 `.env.example`）

### 2.7 密码策略（长度/复杂度/历史防重用/定期强制改密）

一套可配置的密码治理，建号、自助改密、管理员重置三处统一生效：

- **长度与复杂度**：`PASSWORD_MIN_LENGTH`（默认 8）+ `PASSWORD_REQUIRE_UPPER/LOWER/DIGIT/SPECIAL` 四开关（前三默认必含，特殊符号默认关）。不满足返回可读提示，接口 400
- **历史防重用**：`PASSWORD_HISTORY_COUNT`（默认 5）记录每人最近 N 个旧密码哈希，改密不得与最近 N 个相同，防"循环改回旧密码"；0 关闭
- **定期修改密码**：`PASSWORD_EXPIRE_DAYS`（默认 90）为密码有效期，**jwt/session 两种认证模式通用**（令牌有效期本身另有 JWT_*/SESSION_* 配置）。到期后：
  - 登录仍成功，但响应带 `password_expired: true`
  - 除改密/登出/me/menus/刷新外的业务接口统一返回 **428**（由 `ensure_password_current` 网关，router 级挂载）
  - 到期前 `PASSWORD_EXPIRE_WARNING_DAYS` 天登录响应带 `password_expire_warning: true` + `password_expire_in_days` 提醒
  - 改密后 `password_changed_at` 刷新，业务自动恢复；(session 模式)改密同时吊销旧会话
- **接口**：
  - `POST /api/v1/auth/change-password`：自助改密，**必须验证旧密码**
  - `POST /api/v1/sys/users/{id}/password`：管理员重置他人密码（`sys:user:update` 权限，免旧密码）
  - `PUT /api/v1/auth/me`：自助修改个人资料（显示名/邮箱）
- 前端：登录响应 `password_expired`/warning 自动引导；业务接口 428 自动跳强制改密页

### 2.8 角色权限分配（权限树）

- `GET /api/v1/sys/permissions/tree`：**权限树（模块 → 功能 → 动作）**，结构参考产品 trees.xml / normal_tree.json 的树状分组，供角色分配页逐模块/逐功能勾选
- `GET /api/v1/sys/roles/{id}`：角色详情（含已分配 permission_ids/permission_codes），回显勾选状态
- `POST /api/v1/sys/roles/{id}/permissions`：分配权限（`sys:role:update`），改后动态菜单随之变化——不同角色看到不同页面、拥有不同权限（readonly 角色天然无管理权限）

### 2.9 通知组件（渠道 + 订阅策略，参考 secvisual / 订阅策略语义）

安全告警发通知的**开箱组件**：先配好渠道，再新增订阅策略（可多条），事件触发即自动发送。

- **四类渠道**：企业微信（群机器人 webhook）、钉钉（加签机器人）、飞书（机器人）、邮箱（SMTP）。渠道以 JSON 文件落盘（`NOTIFY_CHANNELS_FILE`），**敏感字段 AES-GCM 加密**（SMTP 密码、钉钉/飞书 secret，密钥派生自 JWT_SECRET），对外只暴露"是否已配置"
- **订阅策略**（`NOTIFY_POLICIES_FILE`，多条）：每条 = 名称 + 自身开关 + 事件订阅 + 渠道绑定（`channel_key`、邮箱 `to`、可选 `template`）。每个事件可配**节流间隔**（`interval`，避免告警风暴）与自定义 `subject`；节流按 `(策略, 事件)` 记账，所以两条策略订阅同一事件各发各的、互不压制（典型用法：运维群收全量、业务群只收任务失败）
- **内置事件**：`account_locked`（账号/IP 被锁定）、`login_failure`（登录失败，含错误数）、`task_failed`（定时任务抛异常）、`custom`（业务 `emit` 任意事件）
- **接口**（`sys:notify:*` 权限）：
  - `GET/POST /api/v1/notify/channels`、`PUT/DELETE /api/v1/notify/channels/{key}`：渠道 CRUD
  - `POST /api/v1/notify/channels/{key}/test`：发一条测试消息
  - `GET /api/v1/notify/policies`、`POST /api/v1/notify/policies`、`GET/PUT/DELETE /api/v1/notify/policies/{id}`：订阅策略 CRUD（未知事件码/渠道类型一律 400 拒收）
  - `GET /api/v1/notify/events`：内置事件与渠道类型元数据（配置页渲染用）
- **三处自动触发**：登录失败、账号锁定（`services/account_lock_service` 内 `notify_service.emit`）、定时任务异常（`scheduler/scheduler_.py` 的 `task_failed`）
- **业务接入**：任意代码 `await notify_service.emit("custom", ...)` 即遍历启用中的策略分发，`settings.notify_enabled` 为进程级总闸
- **自测**：执行 `POST /channels/{key}/test` 前建议先用 `./scripts/` 或直接 curl 验证 webhook；测试套件用 recorder 替换发送器，离线跑通不发真实请求

### 3. 数据库可配置切换

`DB_BACKEND=sqlite`（默认，零依赖）或 `DB_BACKEND=mongodb`（需 Mongo 服务）。`repositories/` 下每实体一个 Repo，同时声明 `model`(SQL) 与 `table`(Mongo)，通用助手方法（`_find_one`/`_find_all`/`_list_from` 等）双端语义一致，业务层零分支。

### 4. 动态菜单链路

seed 预置菜单树（含目录/菜单二级）→ 每菜单关联权限码 → 登录后 `GET /api/v1/auth/menus` 按当前用户权限过滤返回树 → 前端 `js/app.js` 渲染侧栏。菜单管理界面可增删改，改动即时影响所有用户的侧栏。

### 5. 定时任务（inputs.conf 风格）

`backend/conf/inputs.conf`：

```ini
[task://device_health:device_health_check]
enable = true
interval = 300s
# 或 cron = hour=2,minute=30,second=0
```

启动时解析并注册到 APScheduler；`enable=false` 即停用，无需改代码。新增任务三步：写 `scheduler/tasks/xxx.py` 异步函数 → 在 inputs.conf 加一节 → 重启。

### 6. 持仓诊断（portfolio 契约接口）

面向外部系统的**持仓诊断代理接口**：判定（sell/hold/add）与收益/市值 100% 收敛到插件（`services/portfolio_plugin.py`），后端只做**代理 + 落库 + 展示**，不做任何判定/收益公式（红线 R1）。

- 路由挂载：外部契约前缀 `/api`（不走 `/api/v1`，不挂密码到期网关）。由 `backend/main.py` 的 `app.include_router(portfolio_router, prefix="/api")` 挂载。
- 插件模式：`config.plugin_mode`，默认 `mock`（确定性判定 + 收益快照，离线可跑契约测试）；真实插件接通后 `.env` 置 `PLUGIN_MODE=http`（HTTP 代理到 `plugin_base_url`，M2 范畴）。
- 幂等重放：**幂等键由服务端计算** —— 对 holdings 规范化（排序 + code/name/quantity/cost_price）取 `holdings_md5` 作 `batch_id`；同 `(user_id, natural_day, batch_id)` 重复调用复用既有 `run_id`，不重复建 run/明细。客户端传的 `user_holdings_md5`/`portfolio_snapshot_md5` 仅兼容放行、不参与键构造。
- M1 为**同步代理**：成功后 `status=completed`；插件失败则 run 置 `failed` 并返回 502。
- 数据表：`portfolio_diag_runs`（run 头，唯一键 `(user_id, natural_day, batch_id)`）+ `portfolio_diag_results`（明细，唯一键 `(run_id, code)`）。

**`POST /api/portfolio/analyze`** — 提交持仓获取诊断判定 + 收益快照：

```bash
curl -s http://localhost:8000/api/portfolio/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "u_1001",
    "natural_day": "2026-09-17",
    "holdings": [
      {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
      {"code": "000858", "name": "五粮液",    "quantity": 50,  "cost_price": 142.0}
    ]
  }'
```

响应（`data` 结构）：

```json
{
  "success": true, "code": 0, "message": "成功",
  "data": {
    "run_id": "xxx",
    "status": "completed",
    "results": [
      {"code": "600519", "name": "贵州茅台", "action": "sell", "action_reason": "...",
       "latest_price": 1696.44, "quantity": 100, "holding_pnl": 844.0, "pnl_pct": 0.5, "suggestion": ""}
    ],
    "summary": {
      "action_counts": {"sell": 1, "hold": 1, "add": 0},
      "total_market_value": 176815.0, "total_pnl": 915.0, "holding_count": 2
    }
  }
}
```

同一 `user_id + natural_day + holdings` 重复调用 → 返回相同 `run_id`（幂等重放）。

**取价口径（`plugin/portfolio/pricing.py`）**：判定的基准日是**今天**时先问腾讯批量行情（一次请求覆盖整批代码，30 秒进程内缓存），问不到才退回日线 parquet 那一根；基准日是**历史日期**时只用那一天的收盘，绝不问行情——否则同一份数据两次诊断出两个盈亏。`latest_price` 因此可能是实时价，而买卖判定始终来自指标行。

**`POST /api/portfolio/quotes`** — 持仓页每次打开都调它取「现在这一眼」的价：不落 run、不做判定，只回 `latest_price`/`holding_pnl`/`pnl_pct`/`market_value` 加 `price_time`、`price_source`（`live` 时是 `20261012150000` 这样的行情时刻，`kline`/`snapshot` 时是日期）。后端 `GET /api/v1/holdings` 会把它盖在诊断快照之上，插件不可达则保留快照并标 `price_source=snapshot`，App 持仓页把来源与时刻显示在现价后面。

**`GET /api/portfolio/runs`** — 按 user_id 隔离的诊断历史列表：

```bash
curl -s "http://localhost:8000/api/portfolio/runs?user_id=u_1001&page=1&size=20"
```

响应 `data` 为分页对象：`{"items": [...], "total": N, "page": 1, "size": 20}`，`items` 仅含该 `user_id` 的 run（越权不可见他人，鉴权收敛由 M3 补强）。

相关文件：`backend/db/sql/portfolio.py`、`backend/schemas/portfolio.py`、`backend/repositories/portfolio.py`、`backend/services/portfolio_service.py`、`backend/services/portfolio_plugin.py`、`backend/api/v1/portfolio.py`。

#### 形态买卖规则的停用与恢复（2026-10-17）

线上现在**只有成本灾难线（−8%）在判 sell**，形态类的卖出/加仓规则全部关着。理由：两条占位规则
（`sell = price <= ma10*0.95`、`add = close >= ma5*1.03 and rsi14 <= 65`）零标定、常数抄自测试夹具，
没有任何回放证据支撑，先停用再说。

生效的那份是 `data/rules/active.json`（**gitignore，只存在于本机**，不在仓库里）。三种形状的含义必须分清：

| JSON 里的值 | 含义 |
|---|---|
| 键缺失 / `null` | 沿用 `portfolio/rules.py` 的默认常量（等于没关） |
| `[]`（空列表） | **这一侧规则被关闭**，判定只剩灾难线 |
| 非空列表 | 用这批规则判 |

恢复办法，二选一，**都不用重启进程**（`load_rules()` 每次判定都重读文件，没有进程内缓存）：

```bash
# A) 正式恢复：在后台管理页「规则集」里建一条规则集并点「应用」
#    —— 点应用会直接覆盖 data/rules/active.json，手写那份就没了
# B) 应急回退：删掉这份文件 → 退回 rules.py 默认常量（占位规则又会活过来）
rm /data/work/SelectStock/data/rules/active.json
```

改完抽查一条判定，确认文案里没有基于形态卖点的台账数（关着时不该出现「20 日内 X% 自行转卖」）。
直连插件 8001 只算不落库，用「成本=现价」造一个零盈亏的例子最稳：

```bash
curl -s http://localhost:8001/api/portfolio/analyze -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"_manual_check\",\"natural_day\":\"$(date +%F)\",\"holdings\":[{\"code\":\"600519\",\"name\":\"贵州茅台\",\"quantity\":100,\"cost_price\":1400.0}]}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["results"][0]; print(d["action"], d.get("sell_trigger_price"), d["suggestion"])'
```

（把 `cost_price` 换成 600519 的现价附近，让它落在 hold；浮亏超过 8% 就会由灾难线判成 sell。）

注意管理页在 `rule_set_id: null` 时显示「未应用任何规则集」，而线上其实在用这份手写文件——页面读数
不等于运行时状态。台账 `scripts/build_judgement_ledger.py` 每晚 19:00 重建后，规则关着会让 hold 那一格
退化成全市场基线、转卖列为 NaN，这是预期结果，不是故障。

#### 合取判据批次 B：预注册清单与扫描入口（2026-10-17）

单条件那一层已经扫到头（62,208 格只剩 1 条及格、买入腿 0/952 独立成立），下一批改成
**两根轴的合取**。清单由生成器产出，不手抄、也不从 Mongo 取：

```bash
# 1) 生成/重生成预注册批次（只写文件，不读行情、不烧试次）
python3 scripts/conjunction_candidates.py --dry-run     # 先看
python3 scripts/conjunction_candidates.py               # 写 config/rule_candidates_conj.json
# 已有文件要重写必须加 --force：预注册批次一旦定稿就不该跟着读数改

# 2) 扫描（会记试次；批次的 60 条必须整批跑）
python3 scripts/profit_scan.py --inventory-file config/rule_candidates_conj.json \
  --min-tenure 250 --run-id 20261017-conj-v1 \
  --export-csv logs/profit_scan/rows_20261017-conj-v1.csv
```

四条口径要记住：

- **票池必须与 v1 那一轮完全一致**（`--min-tenure 250`、`--min-amount 0`、全市场 step=1）。
  加流动性门槛会让「合取过了」分不清是判据的作用还是换池子的作用——池子也是一个自由度。

- **阈值一个都没新增**：60 条 = C(6,2)=15 个轴对 × 4 象限，全部由 v1 已注册的 12 条尾档
  （pos60 / vol_ratio / vol_z / rsi14 / adx14 / 乖离 的两端）两两 AND 而成，组合不引入新自由度；
- **试次是累计的**：这一批 60 × 64 = 3,840 格，从 `logs/profit_scan/trials.jsonl` 的累计数里加，
  及格线（`rule_gates.t_required`）按累计试次抬，`--trial-budget`（默认 30 万）用满就拒绝再扫；
- **批次不寄存在 Mongo**：`profit_scan_base_conditions` 每次 `base_conditions.py` 入库前先
  `delete_many({})`，所以 `--inventory-file` 走本地 JSON；对它用 `--limit-conditions` 会直接报错，
  从固定条数里挑几条看等于事后挑数据。

**批次 B 已跑完（2026-10-17 16:49，run_id `20261017-conj-v1`，3,840 行在
`profit_scan_rows` + `logs/profit_scan/rows_20261017-conj-v1.csv`，累计试次 129,856）：**
判定分布 **过 0 / 不过 3,840**。同一份票池（`cells_ok` 3,578,690 与 v1 那一轮逐格相同）下：

- 卡点全在按交易日聚类的显著性——**3,840 格无一例外都带 `sig`**；981 格其余四道内容门槛
  （笔数≥300、胜率>50%、盈亏比>1、扣后每笔>0）全过，但其中只有 271 格按日均值还是正的，
  最高 `t_day=4.29`（及格线 4.94）。
- 目标 4 的硬读数：笔数≥300 的 3,328 格里**盈亏比最高 1.90、中位 1.11，没有一格 ≥2.0**。
  所以「盈亏比 2.0」和「笔数够多」在日线两轴合取这个空间里不能同时成立。
- 最像样的三族（缩量超买 / 高位缩量 / 放量强趋势）按日 t 只有 1.8~2.8，且每笔只比同日大盘基准
  多 0.2~0.5pp——那点利润大部分是「当天买什么都涨」的 β。
- **这批 37 个"差一口气"的格子不许事后挑出来当规则**；要验它们只能另开一个预注册批次，
  并走三轴（时代 / 子时期 / 股票抽样）。买入腿这条路（单条件 → 两轴合取）到此判完。

#### 持仓动作模型批次 C：口径在看任何读数之前先写死（2026-10-17）

买入腿判完之后，C 问的是另一件事：**手里已经有仓位，今天该卖 / 该加 / 该提前止盈吗**。
这件事以前"连输入都凑不齐"，缺的是持仓状态（第几天、相对成本浮亏多少）；episode 由我们自己造，
入场价和持有期天然已知，所以状态齐了。

先记一条化简，否则会白训两个模型：**卖出和加仓是同一个标签的两个阈值**。
两条分支各自只多/少一次卖出的费用差额（加仓那份才是一整轮换手），于是两个动作都是
`r_H = 再拿 H 天的前向收益` 的单调函数 ⇒ 只训一个回归 `r_H` 的模型，卖侧取预测值最低的分位、
加侧取最高的分位。「提前止盈」也不用单独建模，它就是卖侧在 `pnl_pp > 0` 那一刀上的切片。

- **决策只看未来**：入场成本已经沉没 ⇒ `pnl_pp`（相对成本的浮动盈亏）**进特征、绝不进标签**。
  但它是特征里最重要的一列——要学的正是「在浮亏 X% 的状态下，继续拿是不是更好」。
- **窗口与票池与 v1 / B 逐项一致**：2015-06-01→2021-12-31；`bucket.is_stock_code`（剔场内基金与北交所）、
  本库 ≥250 根日线、当前简称非 ST、不设成交额门槛。换池子就是换自由度，这条不能动。
- **Episode**：入场 = 当日**收盘买得进**（`executability.buy_blocked` 用当日 `pct`，贴着涨停线或一字板的
  入场日剔掉），入场价 `close(e)`；每只票每 **12** 个交易日取一个入场日（> 最大 H=10 ⇒ episode 之间
  不共用前向 bar，样本量不会被滑动重叠灌水）。
- **决策行**：`h = 1..10`（A 股 T+1 ⇒ 没有 h=0）。`pa = close(j)`——当天必须卖得掉，一字板/停牌当天不判；
  `pb` = 从 `j+H` 起最多顺移 `REPAIR_MAX_DAYS`(10) 行，取第一个卖得掉的收盘价；顺移不到 ⇒ 该格判不了，
  计入 `r_unfilled`，不当 0。
- **断档防护（#144 那个坑）**：episode 用到的相邻行日期差必须 ≤ **15 个自然日**，否则整个 episode 丢弃并计数。
  301212 / 301353 那种「往后数 10 行」实际跨了 100 个交易日的票，不防就是把「三个月后」当成「十天后」来学。
- **成本**：卖侧 `−r_H × (1 − SELL_FEE_RATE)`（费率真源在 `backtest/broker.py`，两条分支各只有一次卖出，
  只扣差额；腾出现金能不能再买回来不在这条口径里）；加侧 `r_H − ROUND_TRIP_PP`（加仓是真多一轮换手，
  用 `rule_gates` 那个 0.35pp，含过户与预估滑点）。
- **特征契约**（只允许 ≤t 的因果列，线上 provider 同样取得到）：`hold_days, pnl_pp, pnl_atr, bias20, pos60,
  vol_ratio, vol_z, rsi14, adx14, atr_n, lu, pct, dd_hi20, board`。
- **训练**：LightGBM 回归 `r_H`（H=5、10 各一版），净化式 walk-forward + 25 自然日 embargo；
  **AUC / RMSE 只当参考**，能不能判过分只看下面的门槛。
- **验收 = 12 格，跑之前定死**：2 个 H × 2 个方向 × 3 个分位阈值（卖侧预测 `r` 最低 10% / 20% / 30%，
  加侧最高 10% / 20% / 30%；**按名次取恰好那 pct% 的行**，不按 `p ≤ np.quantile(p, pct)` 取值——
  模型没有信号时预测会被早停压成一条常数，那种写法会把全部行都判成触发，三格变成同一格却自称「最挑的 10%」）。
  每格按「同一个 episode 只算第一次触发」收成逐笔（与 `profit_scan.cooldown`
  同口径——真实仓位卖一次就没了）；判据用 `rule_gates.evaluate_trade` 五道（≥300 笔、胜率>50%、
  盈亏比>1、扣后每笔>0、按交易日聚类的 t ≥ `t_required(累计试次)`），三时代只展示不判。
  **这 12 格要烧进 `logs/profit_scan/trials.jsonl`**（129,856 → 129,868，及格线 ≈4.94）。
- **两条参考线同屏**：全样本平均（「见了就动」到底值多少 pp）与同日均值（选出来的子集比「当天所有持仓」
  好多少 = α）。只有前者为正、后者为零，那就是择时挑了个本身在涨的日子，不是动作有依据。

**批次 C 已跑完（2026-10-17 17:39，run_id `20261017-action-v1`，12 格在
`logs/actions/cells_20261017-action-v1.csv`，累计试次 129,868，及格线 t≥4.94）：**
样本 `data/actions/decision_samples.parquet` = 3,714 只票 / 292,035 个 episode / 2,914,407 条决策行
（2015-06-02→2021-12-03；丢弃：断档 episode 2,862、尾部不足 8,947、决策日卖不掉 5,943 行、
顺移后仍取不到卖价 45 个标签）。判定分布 **过 0 / 不过 12**：

- **12 格全部只卡 `sig`**（按交易日聚类的 t 不够），其余四道内容门槛都在不同格过了——
  也就是说「扣完费每笔为正、胜率过半、盈亏比>1」是能凑出来的，凑不出来的是「不是运气」。
  卖侧 6 格 t_day 0.3~3.69，加侧 6 格 t_day **全为负**（−1.62~−0.39），且 6 格里 5 格胜率不足 50%。
- 最好的一格：H=5 卖侧最挑的 10% —— 82,515 笔（episode 去重后）、胜率 56.2%、盈亏比 1.24、
  每笔扣费 **+0.58pp**（见了就动的口径）、日均 +0.50pp、同日全部持仓基准 +0.20pp ⇒ **超额 +0.39pp**（α 口径）、
  t_day 3.69。三时代 `+1.19, −0.47, +0.32`，**中间那格是负的**，不稳。
- 两条参考线一对就露馅：加侧每笔看着赚（H=10 10% 那格 +0.64pp），但日均是 −0.08pp、同日基准 +0.25pp
  ⇒ 那点钱来自「这些天持仓本身在涨」，不来自加仓这个动作。
- 模型本身没有信号，这是根因：样本外（2018–2021 四折、2,455,341 行）与 `r` 的相关只有
  **0.0553（H=5）/ 0.0469（H=10）**，符号命中 51.2%；2019 那折整体退化（`best_iter=1`、相关 0.0006 / −0.0170）。
  重要度前五名是 `bias20 / atr_n / pos60 / vol_ratio / pct`——全是波动率几何与位置，不是新信息；
  `pnl_pp`（沉没成本）只排第 9，说明「浮亏多少」并没有可学的后续走势差异。
- 与买入腿合起来读：**卖侧超额 +0.39pp 明显大于批次 B 买入腿最好那格的 +0.05pp（纯 β）**，
  所以「什么时候该卖」比「什么时候该买」离答案更近一点，但仍在防自欺门槛之下。
- **这 12 格里最接近的 H=5 卖侧 10% 不许事后提拔成线上规则**（挑最接近的那格 = 又把 12 次比较当成 1 次）。
  要继续验它，只能另开一个预注册批次：改动量化成一条判据、烧进台账抬及格线、走三轴（时代/子时期/股票抽样），
  2022+ 留出段仍然只允许显式读一次。动作腿到此判完。

#### 持仓动作批次 D：换输入（2026-10-17 预注册，任何读数之前先写死）

批 C 的 0/12 根因是**没有信息**（样本外相关 0.05），不是没调好。D 只动两件有证据支持、且不需要新数据源的事：
① 把 β 从**训练目标**里剥掉（别让树把容量花在大盘涨跌上）；② 加横截面与市场状态列（树自己算不出
「当天这只票在全市场/同板块排第几」，这是真新增维度）。

**先划掉的路（每条都已有一条实测反证，别再当"待试"提）**：改二分类/换损失——可信度模型那轮 LightGBM 二分类
样本外 AUC **0.5374**（≈抛硬币，见 §可信度重训）；拆「卖出 + 加仓」两个模型——两个动作是同一个 `r_H` 的
单调函数，不存在目标打架，拆了只把格子翻倍；再加 K 线衍生变种（更多周期 ATR/RSI/KDJ）——与现有列同源；
资金流/北向/基本面——盘上 `data/fund_flow/individual/` 每只票只有 **124 行（2026-04-09→10-13）**，
东财那个接口只给滚动窗口、回溯不到 2015–2021，基本面在本仓库没有任何数据，那不是"加一列"而是攒一年数据。

**两臂一次跑完、都记账，为的是能归因（不是多试几次挑一个）**：
- **D1 = 只换标签**：训练目标 `y = r_H − med_H(day)`，`med_H(day)` = 同一交易日、同一 H 的**全部**可算决策行
  `r_H` 的中位数（在场口径，与批 C 的 `day_benchmark` 同一把尺子；含自身，日内数千行、影响可忽略）。特征集与批 C 逐项相同。
- **D2 = 换标签 + 新特征（主判）**：D1 的标签 + 下面 9 列。
- **判定端两臂照旧用未截断的真实 `r_H`** 换算扣费优势（卖 `−r_H×(1−SELL_FEE_RATE)`、加 `r_H−0.35pp`），
  五道门槛与三时代展示全部不变 ⇒ 换标签只改变模型学什么，**不改变"赚不赚钱"那把尺子**。
  标签不做 1%/99 Winsorize：卖出动作赚的正是左尾，截断后再算每笔就是自欺；批 C 的退化来自无信号，不是尾部主导。
- 诚实说明一条：新**特征**只用 `t` 日收盘已知量；被减掉的 `med_H(day)` 确实要用到 `t+H` 的全市场——那是**标签定义**
  （与 `r_H` 本身同一段前向），不是特征侧泄露。
- **新增 9 列**（`scripts/build_cross_section.py`，票池与批 C 逐项一致：`is_stock_code` + 本库 ≥250 根日线 +
  非 ST + `--to 2021-12-31`）：`xs_bias20 / xs_pos60 / xs_vol_ratio / xs_vol_z`（当日全市场分位）、
  `xs_board_bias20 / xs_board_pos60`（同板块内当日分位，板块真源 `bucket.classify_board`）、
  `mkt_up_ratio`（当日上涨家数占比）、`mkt_med_pct`（全市场当日涨跌幅中位）、`mkt_vol20`（`mkt_med_pct` 序列近 20 日标准差）。
  分位一律 `rank/(n+1)` ∈ (0,1)，缺值行不参与排。决策行按 `(date, code)` 左连接，**连不上就留 NaN 交给 LightGBM，
  不许丢行**——丢行等于用"特征可用性"重新定义样本。
- **验收 = 24 格，跑之前定死**：两臂各按批 C 那 12 格（2 个 H × 2 个方向 × 3 个分位、按名次取恰好那 pct%）原样跑，
  格子定义一个都不改。台账 129,868 → **129,892**（Bonferroni 是对数尺子，24 格几乎不抬线：4.943 → 4.944）。
- **归因规则先看**：D2 过而 D1 同格不过 ⇒ 增量来自新列；两臂同格一起过 ⇒ 增量来自剥 β；两臂全不过 ⇒ 触发止损线。
- **止损线（同样先写死）**：若两臂样本外与真实 `r` 的相关都 **< 0.10**（对照批 C 的 0.0553 / 0.0469），
  或 24 格 0 过 ⇒ **单票短期动作预测这条线正式关掉**，不在同一批日线列上开第三个动作批次。
  下一步只剩两条都需要新输入的路：① 组合层现金/仓位（把 平均成本/单票占比/可用现金 喂进
  `backtest/engine.py`+`broker.py`，去验批 B 抓到的 A 类暴跌族「有钱没日子」那个形状）；② 从今天起按日攒资金流，
  一年后才谈得上可训窗。
- **红线不变**：预注册之后不许按读数增删格子或特征列；2022+ 留出段这批仍然不碰；报数必须同屏两套口径。

**批次 D 已跑完（2026-10-17 18:13，run_id `20261017-action-d-v1`，24 格在
`logs/actions/cells_20261017-action-d-v1.csv`，meta `data/actions/action_model_meta_20261017-action-d-v1.json`，
台账 129,868 → **129,892**，及格线 t≥4.94）：判定分布 过 **0** / 不过 **24**，两臂各 0/12。**
横截面列 100% 贴上（2,914,403 决策行无一丢行，`mkt_vol20` 也 100%，说明池子与批 C 逐项对上了）。

- **最好的一格换人了，也更接近了**：D2 的 H=10 卖侧最挑 10% —— 65,391 笔、胜率 **58.2%**、盈亏比 1.34、
  每笔扣费 **+1.18pp**、日均 +0.89pp、同日基准 +0.27pp ⇒ **超额 +0.91pp**、**t_day 4.43**（批 C 最好那格是 3.69 / 超额 +0.39pp）。
  14/24 格**只卡 `sig`** 一道，另 10 格卡 `win+sig`（都在加侧）。三时代 `+1.92, −0.01, +0.78`——中间那段（2019–2020）是平的。
- **卖侧三批以来第一次整侧过内容门槛**：12 个卖侧格子**全部**「扣后每笔为正 + 胜率过半（52.3%~58.2%）+ 盈亏比>1 + 笔数≥300」，
  且 24 格的 `excess_pp` 全为正（剥掉同日基准后仍有增量）。批 C 的加侧 6 格 t 全负、5 格胜率不足 50%，
  这次加侧 t 转正了（最高 2.23）——但**加侧 12 格里 10 格胜率仍不足 50%**（48.0%~49.6%，只有 D2 的 H=10 两格 50.2%/50.5% 过线），
  靠均值撑着的含义是「赢的比输的大，但常输」。
- **归因按预注册那三条走，一条都不许美化**：没有任何一格“过”，所以“新列有效”这个结论**拿不到**；
  能说的只有方向：同格 D2 比 D1 的 t_day 高（卖侧 H=10 4.43 vs 2.60、H=5 3.86 vs 3.46），
  与真实 `r` 的样本外相关也从 0.0574/0.0646 抬到 **0.0678/0.0759**——**两臂都还是 < 0.10 ⇒ 预注册的止损线成立**。
- **新列确实被模型用了，但用的不是个股信息**：D2 重要度第一名是 `mkt_vol20`（H=10 gain 590.9，几乎是第二名 `atr_n` 的两倍），
  之后才是 `atr_n / xs_board_bias20 / pos60 / xs_bias20`。⇒ 那 9 列带来的增量主要是**市场波动体制**（现在是什么时候），
  不是“这只票在全市场排第几比别的票更该卖”。体制类信息天然扎堆在少数日子，正是按日聚类的 t 要拦的东西。
- **信号在衰减，不在积累**：D2 分折相关 `corr_r` 2018 折 0.1245(H=5)/0.1140(H=10) → 2019 0.0749/0.0908 → 2020 0.0176/0.0178 → 2021 0.0228/0.0163。
  后期两折基本没有——读数为“卖侧看着赚钱”主要靠 2015–2018 那批行情（停牌多、涨跌停板制度刚立、波动大）。
- **剥 β 这件事对判定没有影响，也不该有**：判定端两臂都用未截断的真实 `r_H`，同预测同读数逐字段一致（`tests/test_action_eval.py` 钉住了）。
  被减掉的当期中位数：H=5 日均 −0.401pp（跨日标准差 3.125pp）、H=10 −0.594pp（4.327pp）——**持仓普遍每天亏这么多**，
  这解释了为什么加侧的“每笔为正、日均为负/基准为负”那种幻觉在批 C 会出现。
- **止损线已触发 ⇒ 单票短期动作预测这条线到此关掉**：不在同一批日线列上开第三个动作批次（E），
  也不把 D2 那格 4.43 当“差一口气”提拔（挑它 = 又把 24 次比较当成 1 次）。剩下两条都需要**新输入**的路：
  ① 组合层现金/仓位（把 平均成本/单票占比/可用现金 喂进 `backtest/engine.py`+`broker.py`，验批 B 抓到的 A 类暴跌族“有钱没日子”）；
  ② 从今天起按日攒资金流，一年后才谈得上可训窗。2022+ 留出段三批（B/C/D）**仍未碰**。

### 7. 回测（Celery 投递，后端零计算）

红线与前几章一致：**后端不做任何回测算术**。`services/backtest_service.py` 只做参数校验、
单飞判定、`send_task` 投递和 `AsyncResult` 读状态；撮合、指标、落库全在 worker 进程里调
仓库根的 `scripts/backtest_main.py`。

| 接口 | 权限 | 说明 |
|---|---|---|
| `GET /api/v1/backtest/options` | `backtest:list` | 表单规格（默认值 + 数值上下界）与可选策略来源 |
| `POST /api/v1/backtest/run` | `backtest:run` | 投递一次回测，返回 `job_id`；同一用户已有未结束作业时返回 400 |
| `GET /api/v1/backtest/runs` | `backtest:list` | 分页作业列表（摘要，不含净值曲线） |
| `GET /api/v1/backtest/runs/{job_id}` | `backtest:list` | 作业详情，含 120 点净值与最近 30 笔成交；非本人作业 403 |
| `GET /api/v1/backtest/workers` | `backtest:list` | worker 在线探测（页面顶部 chip 用） |

参数白名单与校验在 `core/backtest_params.py`：未知键直接拒；日期必须 `YYYY-MM-DD` 且
`start < end`；`target_amount` 不得撞上 `capital × position_cap_pct` 的单票上限（否则一建仓就
`ValueError`，宁可发起前报错）。`strategy` 留空即规则模式（买卖条件读 Mongo `trading_rules`）。

进度状态机：worker 每推进若干交易日 `update_state(PROGRESS, {progress, stage})`，后端把
celery 状态归并进作业表 —— `PROGRESS→running`（写百分比与阶段文案）、`SUCCESS→done`（存精简
摘要）、`FAILURE→failed`（存错误摘要）、`PENDING` 且超过 3 小时判 `failed`，避免 worker 掉线时
把用户的单飞位永久占住。结果分两层：作业表只留 120 点净值 + 30 笔成交，全量明细由内核写
MongoDB `backtest_results / backtest_trades / backtest_equity`。

撮合可执行性（2026-10-17 起）：引擎不再假定「看到收盘价就能成交」。判据真源在
`stock_selector/backtest/executability.py`，与逐笔扫描 `scripts/profit_scan.py` 同一把尺子
（`LIMIT_TOL_PP=0.05`）：贴着涨停线买不进、`high == low` 的一字板当天没有对手盘卖不掉、
当日无行情行按停牌处理（买入直接放弃这笔，卖出押着等下一个有行情的日子，不设顺移上限——
真实账户不会因为卖不掉就把仓位删掉）。每次运行把拦了多少单写进 `result.executability`
（CLI `--no-save` 也会打印），指标就分得清是行情给的还是「假定都能成交」给的。

一个已知副作用：组合层遇到**日线断档**（不是真停牌）时会把仓位押到数据重现那天，成交价用那天。
2026-10-17 实跑 `trend_rebound`／抽 300 只／2025-01~2026-06 复核，`sell_blocked=199` 全部来自
`301212`、`301353` 两只（parquet 从 2025-10-22 直接断到 2026-03-25，各押 99/100 个交易日才出掉），
买入侧 0 次拦截（该策略买点最高只到 +5.78%，够不到涨停线）。断档回补（886 只可从 Mongo 拼）
没做之前，跨这段区间的组合级收益/回撤要按「含断档」读。

相关文件：`backend/core/backtest_params.py`、`backend/celery_app.py`、`backend/tasks/backtest_tasks.py`、
`backend/services/backtest_service.py`、`backend/db/sql/backtest.py`、`backend/repositories/backtest.py`、
`backend/api/v1/backtest.py`、`frontend/static/js/pages/backtest.js`。

### 8. 系统监控（psutil 只读采样）

`GET /api/v1/system/metrics`（权限 `sys:monitor:list`）返回 hostname/uptime、CPU（含核数与
load average）、内存与 Swap、各挂载分区使用率、后端自身进程 RSS 与线程数。采样是阻塞调用，
路由里用 `asyncio.to_thread` 包一层，避免卡住事件循环；伪文件系统（proc/overlay 等）会被过滤，
同一挂载点只保留一条。页面 `#/system` 每 5 秒轮询一次，离开页面即停。

只读、不触发任何操作，用途是判断回测 worker 有没有把机器吃满。实现见
`backend/services/system_service.py`、`backend/api/v1/system.py`、`frontend/static/js/pages/system.js`。

## 生产部署

本机（Ubuntu + supervisor + nginx）的完整进程拓扑、端口由来、部署步骤与故障排查，
统一写在仓库根的 [README「后台部署与运维」一节](../README.md#后台部署与运维supervisor--nginx)。
配置源副本在仓库根 `deploy/`（`selectstock-api.conf`、`selectstock-celery-worker.conf`、
`selectstock-plugin-*.conf`、`selectstock-scheduler.conf`、`nginx/selectstock.conf`）。

### 备选：CentOS 物理机 systemd

```bash
cd deploy
sudo bash install.sh /opt/fastapi-template   # 一键装依赖+注册 systemd+启动
# 记得修改 /opt/fastapi-template/.env 里的 JWT_SECRET（默认值可被伪造令牌），改后重启
systemctl status fastapi-template
journalctl -u fastapi-template -f
```

> 注意：systemd 服务默认单 worker（`--workers 1`）。SQLite 场景不要开多 worker——会重复执行定时任务并引发 SQLite 写锁竞争。若切换到 MongoDB 且希望多 worker，请另行评估定时任务的幂等性。

卸载：`sudo bash deploy/uninstall.sh`

## 文档

- 新增功能模块指南：[docs/新增功能模块指南.md](docs/新增功能模块指南.md)
