"""设备（示例模块）schema。"""

import ipaddress
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from schemas.common import ORMModel

_STATUS_PATTERN = r"^(online|offline|maintenance)$"


def _validate_ip(v: str | None) -> str | None:
    """允许为空或合法 IPv4/IPv6；不合法抛 ValueError。"""
    if v is None or v == "":
        return v
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise ValueError(f"不是合法的 IP 地址: {v}")
    return v


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    ip: str = ""
    vendor: str = ""
    model: str = ""
    location: str = ""
    status: str = Field(default="online", pattern=_STATUS_PATTERN)
    owner: str = ""
    remark: str = ""

    _ip = field_validator("ip")(_validate_ip)


class DeviceUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    vendor: str | None = None
    model: str | None = None
    location: str | None = None
    status: str | None = Field(default=None, pattern=_STATUS_PATTERN)
    owner: str | None = None
    remark: str | None = None

    @field_validator("ip")
    @classmethod
    def _ip(cls, v):
        return _validate_ip(v)


class DeviceOut(ORMModel):
    id: str
    name: str
    ip: str = ""
    vendor: str = ""
    model: str = ""
    location: str = ""
    status: str = "online"
    owner: str = ""
    remark: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
