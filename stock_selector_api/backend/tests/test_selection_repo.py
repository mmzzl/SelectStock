from repositories import select_jobs_repo, select_results_repo

async def test_job_lifecycle():
    # strategies 列存 JSON 数组字符串（服务层负责 dumps/loads）
    job = await select_jobs_repo.create({"user_id": "s1", "strategies": '["trend_rebound"]',
                                         "status": "queued", "source": "manual"})
    assert job["job_id"]
    await select_jobs_repo.update(job["job_id"], {"status": "running"})
    await select_results_repo.create({"job_id": job["job_id"], "code": "600519",
                                      "name": "茅台", "signal": "趋势回踩", "reason": "MA20 上穿"})
    rows = await select_results_repo.list_by_job(job["job_id"])
    assert len(rows) == 1 and rows[0]["signal"] == "趋势回踩"
    await select_jobs_repo.update(job["job_id"], {"status": "done"})
    got = await select_jobs_repo.get(job["job_id"])
    assert got["status"] == "done"


async def test_enriched_fields_roundtrip():
    """选股插件 P1 的 strategy/asof/price/score/rank 必须能落库并回读（rank 列名为 rank_no）。"""
    job = await select_jobs_repo.create({"user_id": "s1e", "strategies": "[]",
                                         "status": "queued", "source": "manual"})
    await select_results_repo.create({
        "job_id": job["job_id"], "code": "000001", "name": "平安银行",
        "signal": "trend_rebound", "reason": "回踩 MA20",
        "strategy": "trend_rebound", "asof": "2026-09-24",
        "price": 11.2, "score": 0.62, "rank_no": 1})
    row = (await select_results_repo.list_by_job(job["job_id"]))[0]
    assert (row["strategy"], row["asof"], row["rank_no"]) == ("trend_rebound", "2026-09-24", 1)
    assert row["price"] == 11.2 and row["score"] == 0.62


async def test_find_running_and_list():
    await select_jobs_repo.create({"user_id": "s2", "strategies": "[]", "status": "queued", "source": "manual"})
    r = await select_jobs_repo.find_running("s2")
    assert r is not None
    # 终态不算运行中
    await select_jobs_repo.update(r["job_id"], {"status": "done"})
    assert await select_jobs_repo.find_running("s2") is None
    items, total = await select_jobs_repo.list_by_user("s2", 1, 20)
    assert total >= 1 and items[0]["job_id"] == r["job_id"]


def test_ensure_select_result_columns_upgrades_old_table():
    """老库（无富化列）启动时必须被幂等补齐，否则写入会静默失败。"""
    from sqlalchemy import create_engine, inspect, text

    from db import _ensure_select_result_columns

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE select_results (job_id VARCHAR(36), code VARCHAR(16),"
                          " name VARCHAR(64), signal VARCHAR(64), reason VARCHAR(1024))"))
        _ensure_select_result_columns(conn)
        _ensure_select_result_columns(conn)          # 幂等：跑两次不报错
        cols = {c["name"] for c in inspect(conn).get_columns("select_results")}
        conn.execute(text("INSERT INTO select_results (job_id, code, strategy, asof, price, score,"
                          " rank_no) VALUES ('j', '000001', 'trend_rebound', '2026-09-24', 11.2, 0.62, 1)"))
        assert conn.execute(text("SELECT rank_no FROM select_results")).scalar() == 1
    assert {"strategy", "asof", "price", "score", "rank_no"} <= cols
