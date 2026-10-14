"""盘中监控池：从 app.db 只读取「最近一次选股命中 + 当前持仓」。

用临时 sqlite 建最小表结构（holdings/select_jobs/select_results），不碰真库：这里要断言的是
「取哪一批、谁排在前面、持仓市值怎么算」，与 app 侧怎么写库无关。批次时间按 UTC naive 存
（与后端一致），所以用例里换算到本地再比。
"""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_selector.realtime.pool import PoolError, load     # noqa: E402

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def utc_text(moment: dt.datetime) -> str:
    """把本地时间写成库里那种 UTC naive 文本。"""
    return moment.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="milliseconds")


def build(tmp_path: Path, *, jobs=(), results=(), holdings=(), trades=()) -> Path:
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE select_jobs (job_id TEXT, status TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE select_results (job_id TEXT, code TEXT, rank_no INTEGER)")
    conn.execute("CREATE TABLE holdings (user_id TEXT, code TEXT, quantity INTEGER, "
                 "cost_price REAL, enabled INTEGER)")
    # T+1 只认成交日，所以这张表也得能建出来（列名与后端 db/sql/trades.py 一致）
    conn.execute("CREATE TABLE trade_records (user_id TEXT, trade_date TEXT, stock_code TEXT, "
                 "direction TEXT, price REAL, quantity INTEGER)")
    conn.executemany("INSERT INTO select_jobs VALUES (?, ?, ?)", jobs)
    conn.executemany("INSERT INTO select_results VALUES (?, ?, ?)", results)
    conn.executemany("INSERT INTO holdings VALUES (?, ?, ?, ?, ?)", holdings)
    conn.executemany("INSERT INTO trade_records VALUES (?, ?, ?, ?, ?, ?)", trades)
    conn.commit()
    conn.close()
    return path


NOW = dt.datetime(2026, 10, 13, 9, 30, tzinfo=LOCAL_TZ)          # 周二早盘前


@pytest.fixture
def db(tmp_path):
    """一批 3 只命中（j2 最新且 done）+ 2 只启用持仓（含 1 只重复、1 只已停用）。"""
    return build(tmp_path,
                 jobs=[("j1", "done", utc_text(NOW - dt.timedelta(days=3))),
                       ("j2", "done", utc_text(NOW - dt.timedelta(hours=16))),
                       ("j3", "failed", utc_text(NOW))],
                 results=[("j2", "600000", 1), ("j2", "300750", 2), ("j2", "000001", 3),
                          ("j1", "002594", 1), ("j3", "601318", 1)],
                 holdings=[("u1", "002855", 400, 12.75, 1), ("u1", "600000", 100, 10.0, 1),
                           ("u1", "000999", 100, 5.0, 0)])


def test_pool_is_latest_done_batch_plus_positions(db):
    plan = load(db, now=NOW)
    assert plan.codes == ["600000", "300750", "000001", "002855"]   # rank 在前，持仓补齐在后
    assert plan.job_id == "j2"          # 只认 done；failed 的 j3 即使最新也不取（那是残留）
    assert plan.held == {"002855": pytest.approx(5100.0), "600000": pytest.approx(1000.0)}


def test_limit_keeps_positions_and_only_truncates_hits(db):
    plan = load(db, limit=1, now=NOW)
    assert plan.codes == ["600000", "002855"]     # 截的是候选，不是要卖的持仓


def test_extra_codes_come_first(db):
    plan = load(db, extra=["601318", "600000"], now=NOW)
    assert plan.codes[:2] == ["601318", "600000"]     # 手工盯的票排最前，重复只留一次
    assert plan.codes.count("600000") == 1


def test_batch_age_converted_to_local(db):
    plan = load(db, now=NOW)
    assert plan.selected_at == (NOW - dt.timedelta(hours=16)).replace(tzinfo=None)
    assert plan.age_hours == pytest.approx(16.0, abs=0.1)
    assert plan.stale is False


def test_weekend_gap_is_not_stale(tmp_path):
    """周五收盘后的批次，周一早上读不算过期：阈值要跨过整个周末，否则每个周一都白报。"""
    friday = dt.datetime(2026, 10, 9, 17, 30, tzinfo=LOCAL_TZ)
    monday = dt.datetime(2026, 10, 12, 9, 25, tzinfo=LOCAL_TZ)
    db = build(tmp_path, jobs=[("j1", "done", utc_text(friday))],
               results=[("j1", "600000", 1)])
    assert load(db, now=monday).stale is False


def test_stale_batch_warns(tmp_path, caplog):
    db = build(tmp_path, jobs=[("j1", "done", utc_text(NOW - dt.timedelta(days=5)))],
               results=[("j1", "600000", 1)])
    with caplog.at_level("WARNING"):
        plan = load(db, now=NOW)
    assert plan.stale is True and "检查调度器" in caplog.text      # 只 WARN，不中断轮询


def test_missing_batch_still_monitors_positions(tmp_path):
    """选股还没跑过（首日/清库）时持仓照样得盯——卖出信号只跟持仓有关。"""
    db = build(tmp_path, holdings=[("u1", "002855", 400, 12.75, 1)])
    plan = load(db, now=NOW)
    assert plan.codes == ["002855"] and plan.selected_at is None and plan.stale is True


def test_same_code_held_by_two_users_sums_value(tmp_path):
    db = build(tmp_path,
               holdings=[("u1", "600000", 100, 10.0, 1), ("u2", "600000", 50, 10.0, 1)])
    assert load(db, now=NOW).held == {"600000": pytest.approx(1500.0)}


def test_missing_db_raises(tmp_path):
    with pytest.raises(PoolError, match="找不到 app.db"):
        load(tmp_path / "nope.db")


# ── 买入名额 / T+1 / 可用资金要的三个账户事实 ────────────────────────────────────

def test_ranks_follow_the_app_display_order(db):
    """名次就是「买入只看前几只」的判据，所以必须跟 App 展示序一致，不能按代码排序。"""
    assert load(db, now=NOW).ranks == {"600000": 0, "300750": 1, "000001": 2}


def test_ranks_cover_only_the_truncated_hits(db):
    """手工代码与纯持仓票没有名次：前者是用户自己点的，后者是加仓/卖出，都不占买入名额。"""
    plan = load(db, limit=2, extra=["601318"], now=NOW)
    assert plan.ranks == {"600000": 0, "300750": 1}
    assert "601318" not in plan.ranks and "002855" not in plan.ranks


def test_bought_today_reads_only_todays_buys(tmp_path):
    db = build(tmp_path, holdings=[("u1", "002855", 400, 12.75, 1)],
               trades=[("u1", "2026-10-13", "2855", "buy", 12.75, 400),
                       ("u1", "2026-10-13", "600000", "sell", 10.0, 100),
                       ("u1", "2026-10-09", "600111", "buy", 10.0, 100)])
    assert load(db, now=NOW).bought_today == frozenset({"002855"})   # 昨天买的和今天的卖出都不锁


def test_bought_today_survives_a_db_without_trades(tmp_path, caplog):
    """成交表缺失（老库）只让 T+1 判不了，不能让池拒绝开工。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE select_jobs (job_id TEXT, status TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE select_results (job_id TEXT, code TEXT, rank_no INTEGER)")
    conn.execute("CREATE TABLE holdings (user_id TEXT, code TEXT, quantity INTEGER, "
                 "cost_price REAL, enabled INTEGER)")
    conn.execute("INSERT INTO holdings VALUES ('u1', '002855', 400, 12.75, 1)")
    conn.commit()
    conn.close()
    with caplog.at_level("WARNING"):
        plan = load(path, now=NOW)
    assert plan.bought_today == frozenset() and plan.codes == ["002855"]
    assert "T+1 判定跳过" in caplog.text


def test_cash_left_uses_the_cost_basis_and_refuses_without_capital(db):
    """可用资金 = 总资金 − 持仓成本：没有总资金就没有口径，返回 None 而不是编一个 20 万。"""
    plan = load(db, now=NOW)
    assert plan.cash_left(200000) == pytest.approx(193900.0)
    assert plan.cash_left(0) is None


def test_cash_left_never_goes_negative(tmp_path):
    db = build(tmp_path, holdings=[("u1", "600000", 100000, 10.0, 1)])
    assert load(db, now=NOW).cash_left(200000) == 0.0      # 早已满仓：剩 0，不是 −80 万


def test_missing_table_raises(tmp_path):
    path = tmp_path / "app.db"
    sqlite3.connect(path).close()
    with pytest.raises(PoolError, match="select_jobs"):
        load(path)


def test_nothing_to_monitor_raises(tmp_path):
    db = build(tmp_path, jobs=[("j1", "failed", utc_text(NOW))])
    with pytest.raises(PoolError, match="既没有成功的选股批次"):
        load(db, now=NOW)
