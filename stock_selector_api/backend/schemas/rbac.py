"""角色/权限/菜单相关 schema。"""

from datetime import datetime

from pydantic import BaseModel, Field

from schemas.common import ORMModel


# ---------------- 角色 ----------------

class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class RoleOut(ORMModel):
    id: str
    name: str
    code: str
    description: str = ""
    permission_codes: list[str] = []
    created_at: datetime | None = None


# ---------------- 权限 ----------------

class PermissionOut(ORMModel):
    id: str
    code: str
    name: str
    module: str = ""


# 角色分配权限请求
class RoleAssignPermissions(BaseModel):
    permission_ids: list[str] = []


# ---------------- 菜单 ----------------

class MenuCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    menu_type: str = Field(default="menu", pattern=r"^(dir|menu|button)$")
    path: str = ""
    icon: str = ""
    parent_id: str = ""
    sort_order: int = 0
    permission_code: str = ""
    is_visible: bool = True


class MenuUpdate(BaseModel):
    name: str | None = None
    menu_type: str | None = None
    path: str | None = None
    icon: str | None = None
    parent_id: str | None = None
    sort_order: int | None = None
    permission_code: str | None = None
    is_visible: bool | None = None


class MenuOut(ORMModel):
    id: str
    name: str
    menu_type: str = "menu"
    path: str = ""
    icon: str = ""
    parent_id: str = ""
    sort_order: int = 0
    permission_code: str = ""
    is_visible: bool = True
    children: list["MenuOut"] = []
