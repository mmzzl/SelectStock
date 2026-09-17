"""通知组件事件目录：订阅策略按事件类型订阅（对齐 secvisual 订阅策略语义）。

新增业务事件只需在 EVENT_DEFS 加一项，并在业务代码里调 notify_service.emit(事件码, ...)。
"""

EVENT_DEFS: list[dict] = [
    {"code": "account_locked", "label": "账号/IP 被锁定", "description": "登录失败或验证码失败超限触发锁定"},
    {"code": "login_failure", "label": "登录失败", "description": "密码/验证码校验失败（建议配合发送间隔节流）"},
    {"code": "task_failed", "label": "定时任务执行失败", "description": "inputs.conf 定时任务抛异常"},
    {"code": "custom", "label": "自定义事件", "description": "业务模块调用 emit 手动触发"},
]

EVENT_CODES: set[str] = {e["code"] for e in EVENT_DEFS}


def event_label(code: str) -> str:
    for e in EVENT_DEFS:
        if e["code"] == code:
            return e["label"]
    return code
