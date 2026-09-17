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
| 通知组件 | 企微/钉钉/飞书 机器人 webhook + 邮箱 SMTP 四类渠道，AES-GCM 加密落盘；订阅策略按事件+渠道+节流分发，登录失败/锁定/任务失败自动触发通知 |
| 无构建前端 | 原生 JS + hash 路由，登录页 / 动态侧栏 / 用户/角色/菜单/示例设备管理页全内置 |
| 示例模块 | 设备管理完整 CRUD，作为「新增一个功能」的标准模板 |
| 部署就绪 | systemd unit + 安装/卸载脚本，适配 CentOS 物理机 |

## 快速开始（开发）

```bash
cd fastapi_template
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # 按需修改
./start.sh --dev            # 热重载启动 http://localhost:8000
```

浏览器打开 http://localhost:8000/ ，默认账号 `admin / admin123`（超管）。

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

安全告警发通知的**开箱组件**：先配好渠道，再按事件写订阅策略，事件触发即自动发送。

- **四类渠道**：企业微信（群机器人 webhook）、钉钉（加签机器人）、飞书（机器人）、邮箱（SMTP）。渠道以 JSON 文件落盘（`NOTIFY_CHANNELS_FILE`），**敏感字段 AES-GCM 加密**（SMTP 密码、钉钉/飞书 secret，密钥派生自 JWT_SECRET），对外只暴露"是否已配置"
- **订阅策略**（`NOTIFY_POLICY_FILE`）结构对齐 secvisual 订阅策略：总开关 `is_on` + 事件列表 + 渠道列表，每个事件可配**节流间隔**（`interval`，避免告警风暴）、自定义 `subject/template`；每渠道可配 `channel_key` 与开关
- **内置事件**：`account_locked`（账号/IP 被锁定）、`login_failure`（登录失败，含错误数）、`task_failed`（定时任务抛异常）、`custom`（业务 `emit` 任意事件）
- **接口**（`sys:notify:*` 权限）：
  - `GET/POST /api/v1/notify/channels`、`PUT/DELETE /api/v1/notify/channels/{key}`：渠道 CRUD
  - `POST /api/v1/notify/channels/{key}/test`：发一条测试消息
  - `GET/PUT /api/v1/notify/policy`：订阅策略读写
  - `GET /api/v1/notify/events`：内置事件元数据（配置页渲染用）
- **三处自动触发**：登录失败、账号锁定（`services/account_lock_service` 内 `notify_service.emit`）、定时任务异常（`scheduler/scheduler_.py` 的 `task_failed`）
- **业务接入**：任意代码 `await notify_service.emit("custom", ...)` 即按当前策略分发，`settings.notify_enabled` 为总闸
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

## 生产部署（CentOS 物理机）

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
