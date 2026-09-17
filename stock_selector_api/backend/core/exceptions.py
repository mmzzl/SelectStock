"""业务异常与全局异常处理器：统一返回 {success, code, message}。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.logger import log
from core.response import fail


class BizError(Exception):
    """业务逻辑异常：对外返回可读 message。"""

    def __init__(self, message: str, code: int = 1, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class NotFoundError(BizError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, code=404, status_code=404)


class LockedError(BizError):
    """账户/验证码失败超限被锁定（HTTP 429）。"""

    def __init__(self, minutes: float | None = None, remaining: int | None = None):
        if minutes is not None:
            message = f"失败次数过多，账号已锁定，请 {minutes} 分钟后再试"
        else:
            message = "失败次数过多，账号已暂时锁定"
        super().__init__(message, code=429, status_code=429)
        self.remaining = remaining


class CaptchaError(BizError):
    """验证码输入有误（计入锁定计数的失败类型）。"""

    def __init__(self, message: str = "验证码错误，请重新输入"):
        super().__init__(message, code=430)


class PasswordExpiredError(BizError):
    """密码超过有效期（HTTP 428）：除改密/登出等豁免接口外，其余业务接口拒绝访问。"""

    def __init__(self, message: str = "密码已过期，请先修改密码"):
        super().__init__(message, code=428, status_code=428)


class PermissionDeniedError(BizError):
    def __init__(self, message: str = "无权限执行此操作"):
        super().__init__(message, code=403, status_code=403)


class AuthError(BizError):
    def __init__(self, message: str = "未登录或登录已过期"):
        super().__init__(message, code=401, status_code=401)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError):
        if exc.status_code >= 500:
            log.error("BizError: {}", exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(exc.message, exc.code),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        msg = first.get("msg", "参数校验失败")
        # 提取字段路径，让报错更可读
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        detail = f"{loc}: {msg}" if loc else str(msg)
        log.warning("参数校验失败: {} {}", request.url.path, detail)
        return JSONResponse(status_code=422, content=fail(f"参数错误: {detail}", 422))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("未捕获异常: {} {}", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content=fail("服务器内部错误", 500),
        )
