"""worker 侧规则集回放任务：跑 scripts/ruleset_posterior.py 的全市场回放。

后端只投递与读状态（与回测任务同一条红线）。这里唯一的「计算」是把内核脚本当模块调：
分块扫 parquet、按交易日累加，产物写到 data/rules/active_posterior.json 供 8002 插件读。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from celery_app import celery_app
from config import settings

# 分块扫描阶段只给到 95%，剩下 5% 留给产物落盘，进度条不会「100% 了却还没完」
_PROGRESS_CAP = 95


def _import_module():
    root = Path(settings.backtest_root).resolve()
    for entry in (str(root), str(root / "scripts")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import ruleset_posterior

    return ruleset_posterior


def _write_json(path: Path, payload: dict) -> None:
    """原子替换：插件按 mtime 热读，读到半截 JSON 会静默退回默认规则。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@celery_app.task(bind=True, name=settings.ruleset_task_name)
def replay_ruleset(self, params: dict | None = None) -> dict:
    mod = _import_module()
    rules_file = Path(params.get("rules_file") or settings.active_rules_file)
    out_file = Path(params.get("out_file") or settings.ruleset_posterior_file)
    if not rules_file.is_file():
        raise RuntimeError(f"规则集文件不存在：{rules_file}（先在页面「应用」这条规则集）")

    mod.BLOCK = max(50, int(params.get("block") or settings.ruleset_replay_block))
    mod.setup_logging(verbose=False)
    rs = mod.load_rule_set(rules_file)
    files = sorted(Path(mod.KLINE_DIR).glob("*.parquet"))
    if not files:
        raise RuntimeError(f"{mod.KLINE_DIR} 下没有 parquet，日线没重建？")

    self.update_state(state="PROGRESS", meta={"progress": 1, "stage": f"扫描日期轴（{len(files)} 票）"})
    date_list, _ = mod.scan_universe(files, rs["fields"])
    if len(date_list) < 60:
        raise RuntimeError("日期轴不足 60 个交易日，无从回放")

    def on_progress(done: int, total: int) -> None:
        self.update_state(state="PROGRESS", meta={
            "progress": min(_PROGRESS_CAP, int(done * _PROGRESS_CAP / total)),
            "stage": f"回放 {done}/{total} 块",
        })

    t0 = time.time()
    try:
        states = mod.replay(files, rs, date_list, 0, progress_cb=on_progress)
    except (Exception, SystemExit) as exc:
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
    payload = mod.build_payload(states, rs, date_list, len(files))
    _write_json(out_file, payload)
    payload["elapsed_seconds"] = round(time.time() - t0, 1)
    return payload
