"""统一响应结构约定：{success, code, message, data}。"""

from typing import Any

from fastapi.responses import JSONResponse

OK_CODE = 0


def ok(data: Any = None, message: str = "成功", code: int = OK_CODE) -> dict:
    return {"success": True, "code": code, "message": message, "data": data}


def fail(message: str, code: int = 1, data: Any = None) -> dict:
    return {"success": False, "code": code, "message": message, "data": data}


def ok_response(data: Any = None, message: str = "成功") -> JSONResponse:
    return JSONResponse(content=ok(data, message))


def fail_response(message: str, code: int = 1, status_code: int = 400) -> JSONResponse:
    return JSONResponse(content=fail(message, code), status_code=status_code)


# 分页数据结构
def paged(items: list, total: int, page: int, size: int) -> dict:
    return {"items": items, "total": total, "page": page, "size": size}
