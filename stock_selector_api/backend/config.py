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

    # ---------- 持仓诊断插件（M1 默认 mock；M2 接入真实插件端口后置 env）----------
    plugin_mode: str = "mock"             # mock | http
    plugin_base_url: str = "http://localhost:8001"

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
