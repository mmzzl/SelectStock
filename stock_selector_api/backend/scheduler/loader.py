"""inputs.conf 风格定时任务声明解析器。

参考产品(SIP/STA)的 inputs.conf 约定，格式：
    [script://bin/xxx.py 参数]      # 或 [task://模块:函数]
    enable = true
    interval = 300s                 # 支持 300s / 10m / 2h
    cron = hour=23,minute=49,second=0   # 或 day=1,hour=0,minute=33

interval 与 cron 二选一：同时出现时 interval 优先。
enable = false 的任务会被跳过。
"""

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScheduleItem:
    section: str
    title: str
    target: str                      # task://module:func 或 script://path 参数
    kind: str                        # task | script | daemon
    enable: bool = True
    interval: int | None = None      # 秒
    cron_kwargs: dict = field(default_factory=dict)
    args: str = ""

_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])$")


def _parse_interval(value: str) -> int | None:
    m = _INTERVAL_RE.match(value.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n * unit


def _parse_cron(value: str) -> dict:
    """解析 `hour=23,minute=49` → APScheduler CronTrigger kwargs。"""
    kwargs = {}
    mapping = {
        "second": "second", "minute": "minute", "hour": "hour",
        "day": "day", "month": "month", "week": "week", "dow": "day_of_week",
    }
    for part in value.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in mapping and v:
            kwargs[mapping[k]] = int(v) if v.isdigit() else v
    return kwargs


def _resolve_path(path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        # 相对路径基于 backend/ 目录解析（config.py 所在处 / 子目录 2 级）
        from config import BACKEND_DIR

        return BACKEND_DIR / p
    return p


def parse_inputs_conf(path: str | Path) -> list[ScheduleItem]:
    """解析配置文件，返回可执行任务清单。"""
    path = _resolve_path(path)
    conf = configparser.ConfigParser(interpolation=None)
    # configparser 的 MAX_INTERPOLATION_DEPTH 对普通 ini 无影响，容错大小写
    conf.optionxform = str
    conf.read(path, encoding="utf-8")
    items: list[ScheduleItem] = []
    for section in conf.sections():
        if not (section.startswith("script://") or section.startswith("task://") or section.startswith("daemon://")):
            internal = conf.items(section)
            if not internal:
                continue
        body = dict(conf.items(section))
        enable = body.get("enable", "true").strip().lower() in ("true", "1", "yes")
        title = body.get("title", section)
        target = section
        kind = "task"
        if section.startswith("script://"):
            kind = "script"
            # 分离脚本路径与参数
            parts = section[len("script://"):].strip().split(None, 1)
            target = "script://" + parts[0]
            args = parts[1] if len(parts) > 1 else ""
        elif section.startswith("daemon://"):
            kind = "daemon"
        elif section.startswith("task://"):
            target = section[len("task://"):].strip()
            kind = "task"

        interval = None
        if body.get("interval"):
            interval = _parse_interval(body["interval"])
        cron_kwargs = {}
        if body.get("cron"):
            cron_kwargs = _parse_cron(body["cron"])

        items.append(ScheduleItem(
            section=section, title=title, target=target, kind=kind,
            enable=enable, interval=interval, cron_kwargs=cron_kwargs,
            args=body.get("args", ""),
        ))
    return items
