from repositories import upgrade_repo


async def test_latest_published():
    await upgrade_repo.create({"platform": "android", "package_type": "apk", "version_code": 101,
                               "version_name": "1.0.1", "url": "/u/a.apk", "md5": "m1",
                               "size_bytes": 1024, "release_notes": "修复", "forced": False,
                               "is_published": True})
    await upgrade_repo.create({"platform": "android", "package_type": "apk", "version_code": 102,
                               "version_name": "1.0.2", "url": "/u/b.apk", "md5": "m2",
                               "size_bytes": 1024, "release_notes": "新功能", "forced": True,
                               "is_published": True})
    latest = await upgrade_repo.latest_published("android")
    assert latest["version_code"] == 102 and latest["forced"] is True
    assert await upgrade_repo.latest_published("ios") is None


async def test_unpublished_not_returned():
    await upgrade_repo.create({"platform": "h5", "package_type": "h5", "version_code": 999,
                               "version_name": "9.9.9", "is_published": False})
    assert await upgrade_repo.latest_published("h5") is None


async def test_list_for_admin_paged():
    items, total = await upgrade_repo.list_for_admin("android", 1, 20)
    assert total >= 2 and all(i["platform"] == "android" for i in items)
    _, total_all = await upgrade_repo.list_for_admin(None, 1, 20)
    assert total_all >= 3
