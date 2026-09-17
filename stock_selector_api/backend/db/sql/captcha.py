"""验证码模型（用于登录防爆破，参考产品 vericode 语义）。

防伪造设计：验证码生成时绑定一个匿名会话键(anon_sid cookie)，
登录校验时必须同时满足：code 正确 + 未过期 + 未使用 + 会话匹配，用后即焚。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin


class Captcha(Base, IdMixin):
    __tablename__ = "captchas"

    code: Mapped[str] = mapped_column(String(8), nullable=False)  # 明文码，仅在校验期内存活
    session_key: Mapped[str] = mapped_column(String(64), default="", index=True)  # 绑定匿名会话 cookie
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

