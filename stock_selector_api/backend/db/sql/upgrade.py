# db/sql/upgrade.py
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin


class UpgradePackage(IdMixin, TimestampMixin, Base):
    __tablename__ = "upgrade_packages"

    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # h5|android|ios
    package_type: Mapped[str] = mapped_column(String(8), default="")   # wgt|apk|ipa|h5
    version_code: Mapped[int] = mapped_column(Integer, nullable=False)
    version_name: Mapped[str] = mapped_column(String(32), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    md5: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    release_notes: Mapped[str] = mapped_column(String(1024), default="")
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_at: Mapped[str] = mapped_column(String(32), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
