"""账户锁定（暴力破解防护）专项测试。

设计：用户名+IP 双维度共用计数，验证码失败/登录失败分别有阈值，
任一维度超限即锁；锁定期到自动解锁；登录成功清计数。
"""

import pytest

from config import settings


async def _issue_for(client):
    """取一张验证码并读库拿明文，返回 (cid, code)。"""
    r = await client.get("/api/v1/auth/captcha")
    assert r.status_code == 200
    cid = r.json()["data"]["captcha_id"]

    from repositories import captchas

    rec = await captchas.get_by_id(cid)
    return cid, rec["code"]


async def _login_raw(client, username, password, cid=None, code=None):
    """直接登录（指定验证码，不走读库明文）。"""
    return await client.post("/api/v1/auth/login", json={
        "username": username, "password": password,
        "captcha_id": cid, "captcha_code": code,
    })


# ---------------------------------------------------------------
# 验证码失败超限 → 锁定
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_captcha_failures_lock(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_max_failures", 2)
    monkeypatch.setattr(settings, "login_max_failures", 10)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    # 第 1 次：只计数，不锁
    cid, _ = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, "WRONG")
    assert r.status_code == 400, r.text

    # 第 2 次：触发锁定 → 429
    cid, _ = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, "WRONG")
    assert r.status_code == 429, r.text
    assert "锁定" in r.json()["message"]

    # 锁定期内即使验证码/密码正确也拒绝
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, code)
    assert r.status_code == 429


# ---------------------------------------------------------------
# 登录失败超限 → 锁定（验证码正确但密码错）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_failures_lock(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_max_failures", 10)
    monkeypatch.setattr(settings, "login_max_failures", 2)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    # 第 1 次密码错：401（凭据错误），只计数不锁
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "wrongpass", cid, code)
    assert r.status_code == 401, r.text

    # 第 2 次密码错：触发锁定 → 429
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "wrongpass", cid, code)
    assert r.status_code == 429, r.text
    assert "锁定" in r.json()["message"]

    # 锁定期内密码正确也拒
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, code)
    assert r.status_code == 429


# ---------------------------------------------------------------
# IP 维度锁定：换用户名逃不掉（双维度）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_ip_dimension_lock(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_max_failures", 10)
    monkeypatch.setattr(settings, "login_max_failures", 2)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    # 用不存在用户连续失败，触发 ip 维度锁定（不存在用户→401 凭据错误）
    for _ in range(2):
        cid, code = await _issue_for(client)
        r = await _login_raw(client, "ghost_user", "x", cid, code)
        assert r.status_code in (401, 429), r.text

    # 同一 IP 换其他用户名仍被锁（ip 维度）
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, code)
    assert r.status_code == 429, r.text


# ---------------------------------------------------------------
# 到时自动解锁
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_expires_auto_unlock(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_max_failures", 2)
    monkeypatch.setattr(settings, "login_max_failures", 10)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    # 触发锁定
    for _ in range(2):
        cid, _ = await _issue_for(client)
        await _login_raw(client, "admin", "admin123", cid, "WRONG")
    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, code)
    assert r.status_code == 429

    # 手动把所有锁定记录（user + ip 维度）的锁定时间拨回过去，模拟到期自动解锁
    from datetime import datetime, timedelta, timezone

    from repositories import login_locks

    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    rows = await login_locks._find_all()
    for r in rows:
        await login_locks.set_locked(r["lock_key"], expired, 2)

    cid, code = await _issue_for(client)
    r = await _login_raw(client, "admin", "admin123", cid, code)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------
# 并发爆破：原子计数不丢、不出现 500、命中阈值后锁定（堵并发绕过）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_failures_no_bypass(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_enabled", False)
    monkeypatch.setattr(settings, "login_max_failures", 5)
    monkeypatch.setattr(settings, "captcha_max_failures", 100)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    import asyncio

    async def _fail():
        return await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrongpass",
                  "captcha_id": None, "captcha_code": None},
        )

    resps = await asyncio.gather(*[_fail() for _ in range(8)], return_exceptions=True)
    for r in resps:
        assert not isinstance(r, Exception), r
        # 并发下绝不能因唯一约束冲突返回 500（曾导致计数丢失、锁定被绕过）
        assert r.status_code in (401, 429), r.text

    # 命中阈值后锁定仍生效：正确密码也拒
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123",
              "captcha_id": None, "captcha_code": None},
    )
    assert r.status_code == 429, r.text


# ---------------------------------------------------------------
# 阈值=1 边界：首错即锁（曾因 set_locked 作用于不存在的行而静默失效）
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_threshold_one_locks_first_failure(client, monkeypatch):
    monkeypatch.setattr(settings, "captcha_enabled", False)
    monkeypatch.setattr(settings, "login_max_failures", 1)
    monkeypatch.setattr(settings, "captcha_max_failures", 100)
    monkeypatch.setattr(settings, "lock_seconds", 300)

    r = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrongpass",
              "captcha_id": None, "captcha_code": None},
    )
    assert r.status_code in (401, 429), r.text

    r = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123",
              "captcha_id": None, "captcha_code": None},
    )
    assert r.status_code == 429, r.text
