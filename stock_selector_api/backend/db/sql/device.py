"""设备示例模型（演示如何新增一个业务实体）。"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class Device(Base, IdMixin, TimestampMixin):
    __tablename__ = "devices"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    vendor: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    # 资产状态：online/offline/maintenance
    status: Mapped[str] = mapped_column(String(16), default="online")
    owner: Mapped[str] = mapped_column(String(64), default="")
    remark: Mapped[str] = mapped_column(String(255), default="")
