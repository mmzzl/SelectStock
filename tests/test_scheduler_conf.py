"""`config/inputs.conf` 的调度表：段名唯一性与资金流的两跑（16:30 采集 + 20:30 当晚重取）。

为什么要钉住：`systems/conf.py:_load_raw` 用 `except Exception → logging.error` 把解析异常吞掉，
而 configparser 是 `strict=True` —— **两段重名会让整份 conf 解析成 0 个任务**（在 /tmp 实测过），
所有定时任务静默停摆，只在 `logs/scheduler.err.log` 留一行。这种失败必须有一条测试挡在前面。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from systems.conf import Conf                                        # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "scheduler_scheduler", ROOT / "scheduler" / "scheduler.py")
scheduler = importlib.util.module_from_spec(_SPEC)
sys.modules["scheduler_scheduler"] = scheduler
_SPEC.loader.exec_module(scheduler)


@pytest.fixture(scope="module")
def tasks():
    return scheduler.tasks_load_from_inputs(Conf(str(ROOT), "config/inputs.conf"))


def test_conf_loads_every_script_section(tasks):
    """conf 能解析（重名会让这里直接变成 0 个任务）。`enable = false` 的段调度器会跳过。"""
    conf = Conf(str(ROOT), "config/inputs.conf")
    sections = [s for s, opts in conf.config.items()
                if s.startswith("script://") and opts.get("enable", "true") != "false"]
    assert len(tasks) == len(sections) > 0


def test_script_sections_have_unique_commands(tasks):
    """段名必须互不相同：`#`/`:` 之后的部分会被正则截掉，所以靠后缀区分是有效的。"""
    ids = [(t.script, str(getattr(t, "trigger", None))) for t in tasks]
    assert len(ids) == len(set(ids))


def test_fund_flow_runs_daily_and_once_more_in_the_evening(tasks):
    """16:30 采集 + 20:30 当晚重取（批量接口只给当下，过午夜这一天永久欠着）。"""
    ff = [t for t in tasks if "fetch_fund_flow.py" in t.script]
    assert len(ff) == 2, [t.script for t in ff]
    triggers = sorted(str(t.trigger) for t in ff)
    assert triggers == ["cron[hour='16', minute='30']", "cron[hour='20', minute='30']"]
    evening = next(t for t in ff if "20" in str(t.trigger))
    assert "--only daily" in evening.script        # 只重取截面，不重复跑 rank/industry


def test_nextday_shadow_runs_once_a_day_with_its_own_ledger(tasks):
    """W3 影子账每晚 17:45 一跑（docs §37.14.1 开关① / §38）。

    钉住三件事： Flags 三个全在（漏 --settle 会让 E2 永远等在缺腿里）；out-dir 与模型目录
    一一对应（`qlib_nextday_shadow.py` 的护账闸是按目录 run_id 对账的，跑错臂会直接拒写）；
    时间在 qlib_update_bin（17:15）之后 —— 面板没更新就出榜，榜是昨天的。"""
    task = next(t for t in tasks if "qlib_nextday_shadow.py" in t.script)
    assert str(task.trigger) == "cron[hour='17', minute='45']"
    for flag in ("--list", "--settle", "--report",
                 "--out-dir data/qlib/nextday_shadow_w3",
                 "--model-dir data/qlib/model_batchW_c3"):
        assert flag in task.script, flag
