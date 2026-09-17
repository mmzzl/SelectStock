"""测试辅助函数：验证码链路登录。"""


async def login_with_captcha(client, username, password):
    """按验证码链路登录：取验证码 → 读库明文 → 带码登录（client 自动携带 anon_sid cookie）。"""
    r = await client.get("/api/v1/auth/captcha")
    assert r.status_code == 200, r.text
    captcha_id = r.json()["data"]["captcha_id"]

    from repositories import captchas

    record = await captchas.get_by_id(captcha_id)
    code = record["code"]

    r = await client.post("/api/v1/auth/login", json={
        "username": username, "password": password,
        "captcha_id": captcha_id, "captcha_code": code,
    })
    return r
