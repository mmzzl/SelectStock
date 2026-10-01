"""系统资源采样：psutil 只读指标。
阈值判断与告警不在这里做，接口只给使用率与容量原值，前端负责呈现。"""
from __future__ import annotations

import os
import socket
import time

import psutil

# 伪文件系统分区（tmpfs/overlay/proc…）不是用户关心的容量，列出来只会淹没真实磁盘
_PSEUDO_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay",
    "squashfs", "autofs", "mqueue", "debugfs", "tracefs", "securityfs", "pstore",
    "bpf", "hugetlbfs", "fusectl", "configfs", "ramfs", "binfmt_misc", "efivarfs",
    "nsfs", "rpc_pipefs", "fuse.gvfsd-fuse", "fuse.portal",
}


def _load_avg() -> list[float]:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (AttributeError, OSError):     # Windows 无 load average
        return []


def cpu() -> dict:
    # interval=None 首次读数恒为 0，采样窗口给出真实占用
    return {
        "percent": psutil.cpu_percent(interval=0.2),
        "cores_logical": psutil.cpu_count(logical=True),
        "cores_physical": psutil.cpu_count(logical=False),
        "load_avg": _load_avg(),
    }


def memory() -> dict:
    m = psutil.virtual_memory()
    s = psutil.swap_memory()
    return {
        "total": m.total,
        "used": m.used,
        "available": m.available,
        "percent": m.percent,
        "swap_total": s.total,
        "swap_used": s.used,
        "swap_percent": s.percent,
    }


def disks() -> list[dict]:
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in _PSEUDO_FSTYPES or part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append({
            "path": part.mountpoint,
            "device": part.device,
            "fstype": part.fstype,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        })
    return out


def process() -> dict:
    """当前后端进程自身，用来判断是不是 API 进程在吃资源。"""
    proc = psutil.Process()
    info = proc.memory_info()
    return {
        "pid": proc.pid,
        "rss": info.rss,
        "cpu_percent": proc.cpu_percent(interval=None),
        "threads": proc.num_threads(),
        "create_time": int(proc.create_time()),
    }


def collect() -> dict:
    boot = psutil.boot_time()
    return {
        "hostname": socket.gethostname(),
        "platform": f"{os.uname().sysname} {os.uname().release}" if hasattr(os, "uname") else "",
        "boot_time": int(boot),
        "uptime_seconds": int(time.time() - boot),
        "sampled_at": int(time.time()),
        "cpu": cpu(),
        "memory": memory(),
        "disks": disks(),
        "process": process(),
    }
