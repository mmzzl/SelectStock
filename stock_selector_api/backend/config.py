"""应用全局配置：pydantic-settings，环境变量 + .env 双层覆盖。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 包所在目录（config.py 所在处）
BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # 容忍 .env 里多余的键
    )

    # 应用基础
    app_name: str = "安全管控平台模板"
    api_prefix: str = "/api/v1"
    debug: bool = True

    # 认证方式：jwt | session
    auth_mode: str = "jwt"

    # JWT 认证
    jwt_secret: str = "change-me-jwt-secret-at-least-32-chars"
    jwt_algorithm: str = "HS256"
    jwt_access_expire_minutes: int = 30
    jwt_refresh_expire_days: int = 7

    # Session 认证
    session_expire_hours: int = 8
    session_cookie_name: str = "session_id"
    session_cookie_secure: bool = False
    session_cookie_httponly: bool = True

    # 数据库
    db_backend: str = "sqlite"  # sqlite | mongodb
    database_url: str = f"sqlite+aiosqlite:///{BACKEND_DIR.parent / 'data' / 'app.db'}"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "app_template"

    # 验证码（登录防爆破）
    captcha_enabled: bool = True           # 是否启用验证码
    captcha_length: int = 4                # 字符个数
    captcha_expire_seconds: int = 300      # 有效期（秒）
    captcha_cookie_name: str = "anon_sid"  # 匿名会话 cookie（绑定验证码防伪造）
    captcha_cookie_httponly: bool = True

    # 账户锁定（暴力破解防护，参考产品语义）
    login_max_failures: int = 5            # 登录失败次数上限（含验证码错误，共用计数）
    captcha_max_failures: int = 3          # 验证码失败次数上限（与登录共用计数）
    lock_seconds: int = 300                # 命中上限后的锁定时间（秒），0 表示不锁定
    lock_trust_proxy: bool = True          # 是否信任 X-Forwarded-For 等代理头取真实 IP

    # 密码策略（长度/复杂度；登录、自助改密、管理员重置统一校验）
    password_min_length: int = 8                # 最小长度
    password_require_upper: bool = True         # 必须含大写字母
    password_require_lower: bool = True         # 必须含小写字母
    password_require_digit: bool = True         # 必须含数字
    password_require_special: bool = False      # 必须含特殊符号
    # 定期修改密码（jwt/session 两种认证模式通用）
    password_expire_days: int = 90              # 密码有效期（天），0 = 不启用定期改密
    password_expire_warning_days: int = 7       # 到期前 N 天登录响应带 warning 提醒
    password_history_count: int = 5             # 记录最近 N 个历史密码并拒绝重用，0 = 不记录

    # 日志
    log_level: str = "INFO"
    log_dir: str = str(BACKEND_DIR.parent / "data" / "logs")

    # 定时任务（inputs.conf 风格配置文件，相对 backend/）
    schedule_conf: str = str(BACKEND_DIR / "conf" / "inputs.conf")

    # ---------- 通知组件（渠道配置 / 订阅策略，对齐 secvisual alarm_policy.json 风格）----------
    notify_enabled: bool = True
    notify_channels_file: str = str(BACKEND_DIR / "conf" / "notify_channels.json")
    notify_policy_file: str = str(BACKEND_DIR / "conf" / "notify_policy.json")
    notify_state_file: str = str(BACKEND_DIR.parent / "data" / "notify_send_state.json")
    schedule_enabled: bool = True

    # ---------- 持仓诊断插件（M2 起默认接真实插件；测试 conftest 强制 mock）----------
    plugin_mode: str = "http"              # http（本机插件，默认 8001）| mock（测试确定性）
    plugin_base_url: str = "http://localhost:8001"

    # ---------- 选股插件（薄代理，端口 8002）----------
    selection_plugin_mode: str = "http"        # http（本机选股插件 8002）| mock（测试确定性）
    selection_plugin_base_url: str = "http://localhost:8002"

    # ---------- 交割单解析（券商列别名；键为内部规范名）----------
    trades_columns: dict = {
        "trade_date": ["成交日期", "成交时间"],
        "stock_code": ["证券代码", "股票代码", "代码"],
        "stock_name": ["证券名称", "股票名称", "名称"],
        "direction": ["买卖标志", "买卖方向", "成交类别"],
        "quantity": ["成交数量", "数量"],
        "price": ["成交价格", "价格"],
        "amount": ["成交金额", "金额"],
        "commission": ["手续费", "佣金"],
        "stamp_tax": ["印花税"],
        "transfer_fee": ["过户费"],
        "settled_amount": ["结算金额", "发生金额"],
    }

    # ---------- 回测（Celery worker 跑内核，后端只投递任务与查状态）----------
    # 独立 redis db（db 3），与旧 stock_select 项目用的库隔离
    celery_broker_url: str = "redis://127.0.0.1:6379/3"
    celery_result_backend: str = "redis://127.0.0.1:6379/3"
    celery_result_ttl: int = 7 * 24 * 3600     # 回测结果在 redis 里的保留时长
    backtest_task_name: str = "backtest.run"   # worker 侧 task 名，后端按名 send_task
    # 回测内核所在仓库根（含 scripts/backtest_main.py 与 data/stock_kline）
    backtest_root: str = str(BACKEND_DIR.parent.parent)
    backtest_summary_points: int = 120         # 净值曲线降采样点数，避免作业表塞整条曲线

    # ---------- 版本升级中心（自托管）----------
    # 用 as_posix() 归一为 / 分隔：Windows 下仍满足「路径含 /upgrade」契约，且 pathlib 兼容
    upgrade_upload_dir: str = (BACKEND_DIR.parent / "data" / "uploads" / "upgrade").as_posix()

    # ---------- 定时任务（盘后）----------
    daily_diag_time: str = "18:00"
    daily_selection_time: str = "17:30"

    # 部署
    service_user: str = "admin"
    service_group: str = "admin"

    def is_sqlite(self) -> bool:
        return self.db_backend == "sqlite"

    def is_mongodb(self) -> bool:
        return self.db_backend == "mongodb"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
