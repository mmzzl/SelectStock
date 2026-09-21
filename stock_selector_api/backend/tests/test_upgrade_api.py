"""B17：升级包管理 API（超管 CRUD + 文件上传）。"""
import hashlib


async def test_upgrade_admin_crud(client, admin_headers):
    payload = {"platform": "android", "package_type": "apk", "version_code": 201,
               "version_name": "2.0.1", "url": "/uploads/upgrade/a.apk", "md5": "x",
               "release_notes": "修复", "forced": True, "is_published": True}
    r = await client.post("/api/v1/upgrade/packages", headers=admin_headers, json=payload)
    assert r.status_code == 200
    pid = r.json()["data"]["id"]
    assert r.json()["data"]["publish_at"]  # 发布即记发布时间

    r2 = await client.get("/api/v1/upgrade/packages?platform=android", headers=admin_headers)
    assert r2.status_code == 200 and r2.json()["data"]["total"] >= 1

    r3 = await client.put(f"/api/v1/upgrade/packages/{pid}", headers=admin_headers,
                          json={"forced": False})
    assert r3.status_code == 200 and r3.json()["data"]["forced"] is False

    r4 = await client.delete(f"/api/v1/upgrade/packages/{pid}", headers=admin_headers)
    assert r4.status_code == 200
    r5 = await client.put(f"/api/v1/upgrade/packages/{pid}", headers=admin_headers,
                          json={"forced": True})
    assert r5.status_code == 404


async def test_upload_saves_file(client, admin_headers):
    body = b"fake-apk-bytes"
    r = await client.post("/api/v1/upgrade/packages/upload",
                          headers=admin_headers,
                          files=[("file", ("demo.apk", body, "application/octet-stream"))])
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["md5"] == hashlib.md5(body).hexdigest()
    assert data["size_bytes"] == len(body)
    assert data["url"].startswith("/uploads/upgrade/")

    from pathlib import Path

    from config import settings

    saved = list(Path(settings.upgrade_upload_dir).glob("*.apk"))
    assert saved and all(s.read_bytes() == body for s in saved)


async def test_upload_rejects_empty(client, admin_headers):
    r = await client.post("/api/v1/upgrade/packages/upload", headers=admin_headers,
                          files=[("file", ("empty.apk", b"", "application/octet-stream"))])
    assert r.status_code == 400


async def test_upgrade_requires_auth(client):
    r = await client.get("/api/v1/upgrade/packages")
    assert r.status_code in (401, 403)
