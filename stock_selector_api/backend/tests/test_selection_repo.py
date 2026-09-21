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


async def test_find_running_and_list():
    await select_jobs_repo.create({"user_id": "s2", "strategies": "[]", "status": "queued", "source": "manual"})
    r = await select_jobs_repo.find_running("s2")
    assert r is not None
    # 终态不算运行中
    await select_jobs_repo.update(r["job_id"], {"status": "done"})
    assert await select_jobs_repo.find_running("s2") is None
    items, total = await select_jobs_repo.list_by_user("s2", 1, 20)
    assert total >= 1 and items[0]["job_id"] == r["job_id"]
