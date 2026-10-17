"""worker 侧回测任务：直接调用仓库内核 scripts/backtest_main.py。

后端进程按 settings.backtest_task_name 投递、只读 Celery 状态；计算与结果摘要
都在这里完成，摘要精简后再回传，避免把整条净值曲线塞进作业表。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

from celery_app import celery_app
from config import settings
from core.backtest_params import merge as merge_params

# 进度里保留的最近成交笔数；全量成交由内核自己落 MongoDB
RECENT_TRADES = 30
_TRADE_TEXT_KEYS = ("code", "name", "side", "date")


def _import_run_backtest():
    root = Path(settings.backtest_root).resolve()
    for entry in (str(root), str(root / "scripts")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from backtest_main import run_backtest

    return run_backtest


def _num(value):
    """numpy/Decimal 标量收敛成 JSON 可序列化的数值（json serializer 不认 numpy.int64）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _downsample_equity(equity: list[dict]) -> list[dict]:
    total = len(equity)
    limit = max(2, settings.backtest_summary_points)
    step = max(1, math.ceil(total / limit))
    points = equity[::step]
    if points and points[-1] is not equity[-1]:
        points.append(equity[-1])
    return [{"date": str(p.get("date", "")), "total_value": _num(p.get("total_value"))}
            for p in points]


def _trade_row(trade: dict) -> dict:
    return {
        key: (str(trade.get(key, "")) if key in _TRADE_TEXT_KEYS else _num(trade.get(key)))
        for key in ("code", "name", "side", "date", "price", "shares", "amount", "fee", "pnl")
    }


def _summary(result, params: dict) -> dict:
    trades = result.trades or []
    return {
        "task_id": str(result.task_id),
        "strategy": str(result.strategy),
        "start_date": str(result.start_date),
        "end_date": str(result.end_date),
        "params": params,
        "metrics": {key: _num(value) for key, value in (result.metrics or {}).items()},
        # 拦下来多少单不写出来，指标就分不清是行情给的还是「假定都能成交」给的
        "executability": dict(result.executability or {}),
        "equity": _downsample_equity(result.equity or []),
        "trade_count": len(trades),
        "recent_trades": [_trade_row(t) for t in trades[-RECENT_TRADES:]],
    }


@celery_app.task(bind=True, name=settings.backtest_task_name)
def run_backtest(self, params: dict | None = None) -> dict:
    run_backtest_kernel = _import_run_backtest()
    merged = merge_params(params)

    def on_progress(idx: int, total: int, date: str) -> None:
        # 留 100% 给 SUCCESS，进度条不会停在「已完成」却仍在收尾
        percent = min(99, int(idx * 100 / total)) if total else 0
        self.update_state(state="PROGRESS", meta={
            "progress": percent,
            "stage": f"逐日撮合 {idx}/{total}",
            "current_date": str(date),
        })

    try:
        result = run_backtest_kernel(SimpleNamespace(**merged), progress_cb=on_progress)
    except (Exception, SystemExit) as exc:
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
    return _summary(result, merged)
