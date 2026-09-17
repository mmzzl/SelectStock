"""用户相关 schema。"""

from datetime import datetime

from pydantic import BaseModel, Field

from schemas.common import ORMModel


class UserCreate(ORMModel):
    # 密码真实验证（长度/复杂度）交给 services/password_policy（配置驱动）
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = ""
    email: str = ""
    role_ids: list[str] = []


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1)
    role_ids: list[str] | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


class UserOut(ORMModel):
    id: str
    username: str
    display_name: str = ""
    email: str = ""
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[str] = []
    role_names: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
