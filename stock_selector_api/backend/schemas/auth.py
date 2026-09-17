"""Auth 相关 schema：登录、令牌对、用户信息、菜单。"""

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    # 验证码（开启 captcha_enabled 时必传）
    captcha_id: str | None = Field(default=None, max_length=64)
    captcha_code: str | None = Field(default=None, max_length=16)


class TokenPair(BaseModel):
    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class UpdateProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)


class UserInfo(BaseModel):
    id: str
    username: str
    display_name: str = ""
    email: str = ""
    is_superuser: bool = False
    roles: list[str] = []           # 角色 code 列表
    permissions: list[str] = []     # 权限码列表


class MenuNode(BaseModel):
    id: str
    name: str
    menu_type: str = "menu"
    path: str = ""
    icon: str = ""
    parent_id: str = ""
    sort_order: int = 0
    permission_code: str = ""
    children: list["MenuNode"] = []
