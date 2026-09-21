"""B18：公开版本检查端点。

platform 使用独立值（t-h5/t-android），避免与 test_upgrade_* 的
android/h5/ios 已发布数据互相串扰（pytest 按文件名序收集，本文件在前）。
"""
from repositories import upgrade_repo


async def _seed() -> None:
    await upgrade_repo.create({"platform": "t-h5", "package_type": "h5", "version_code": 100,
                               "version_name": "1.0.0", "url": "", "md5": "", "size_bytes": 0,
                               "release_notes": "初版", "forced": False, "is_published": True})
    await upgrade_repo.create({"platform": "t-android", "package_type": "apk", "version_code": 101,
                               "version_name": "1.0.1", "url": "/uploads/upgrade/a.apk", "md5": "m",
                               "size_bytes": 10, "release_notes": "修复", "forced": True,
                               "is_published": True})
    await upgrade_repo.create({"platform": "t-android", "package_type": "apk", "version_code": 90,
                               "version_name": "0.9.0", "url": "/u/old.apk", "md5": "o",
                               "size_bytes": 1, "release_notes": "旧版", "forced": False,
                               "is_published": True})


async def test_latest_no_update_when_current_equals_latest(client):
    await _seed()
    r = await client.get("/api/app/latest?platform=t-h5&version_code=100")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["has_update"] is False
    assert data["latest"]["version_code"] == 100


async def test_latest_android_forced(client):
    await _seed()
    r = await client.get("/api/app/latest?platform=t-android&version_code=100")
    data = r.json()["data"]
    # 取 version_code 最大者（90 那条不得胜出）
    assert data["has_update"] is True and data["latest"]["forced"] is True
    assert data["latest"]["version_code"] == 101


async def test_latest_unknown_platform_no_update(client):
    r = await client.get("/api/app/latest?platform=no-such&version_code=1")
    data = r.json()["data"]
    assert data["has_update"] is False and data["latest"] is None


async def test_version_endpoint(client):
    r = await client.get("/api/app/version")
    assert r.status_code == 200 and r.json()["data"]["version"]
