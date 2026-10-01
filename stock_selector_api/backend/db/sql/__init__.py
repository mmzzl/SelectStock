"""SQL 模型汇总：import 全部模型以注册进 Base.metadata。"""

from db.sql.base import Base
from db.sql.user import User
from db.sql.rbac import Role, Permission, Menu, Session, user_roles, role_permissions
from db.sql.device import Device
from db.sql.captcha import Captcha
from db.sql.login_lock import LoginLock
from db.sql.password_history import PasswordHistory
from db.sql.portfolio import PortfolioDiagResult, PortfolioDiagRun
from db.sql.holdings import Holding
from db.sql.trades import TradeRecord
from db.sql.inbox import InboxMessage
from db.sql.selection import SelectJob, SelectResult
from db.sql.backtest import BacktestJob
from db.sql.upgrade import UpgradePackage

__all__ = [
    "Base",
    "User",
    "Role",
    "Permission",
    "Menu",
    "Session",
    "Device",
    "Captcha",
    "LoginLock",
    "PasswordHistory",
    "PortfolioDiagRun",
    "PortfolioDiagResult",
    "Holding",
    "TradeRecord",
    "InboxMessage",
    "SelectJob",
    "SelectResult",
    "BacktestJob",
    "UpgradePackage",
    "user_roles",
    "role_permissions",
]
