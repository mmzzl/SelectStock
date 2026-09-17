"""通知发送器：企业微信/钉钉/飞书 机器人 webhook + 邮箱 SMTP。

- 机器人渠道：标准 webhook JSON（钉钉/飞书写入时加时间戳+SC 签名）
- 邮箱：smtplib，走 asyncio.to_thread 避免阻塞事件循环
- 传输层可注入（_http_post/_smtp_send），测试时替换为记录假发送
"""

import asyncio
import base64
import hashlib
import hmac
import smtplib
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx

from core.exceptions import BizError


class _Http:
    _client: httpx.AsyncClient | None = None

    @classmethod
    def get(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(timeout=10)
        return cls._client

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None


async def _http_post(url: str, payload: dict) -> None:
    resp = await _Http.get().post(url, json=payload)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return
    # 钉钉/飞书/企微在 HTTP 200 下仍可能返回业务错误码
    errcode = data.get("errcode") or data.get("code")
    if errcode and int(errcode) != 0:
        raise BizError(f"渠道返回错误: {errcode} {data.get('errmsg') or data.get('msg') or ''}".strip())


async def _smtp_send(channel: dict, subject: str, content: str) -> None:
    """SMTP 发送（阻塞，调用方用 asyncio.to_thread）。"""
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    from_name = channel.get("from_name") or channel.get("from_email") or ""
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), channel.get("from_email") or channel.get("username") or ""))
    to_list = channel.get("to") or []
    msg["To"] = ", ".join(to_list)

    host = channel.get("host")
    port = int(channel.get("port") or (465 if channel.get("use_ssl") else 25))
    use_ssl = bool(channel.get("use_ssl"))
    username = channel.get("username", "")
    password = channel.get("password", "")

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    try:
        if not use_ssl and port != 25:
            server.starttls()
        if username:
            server.login(username, password)
        server.sendmail(channel.get("from_email") or username, to_list, msg.as_string())
        server.quit()
    except Exception as e:
        try:
            server.quit()
        except Exception:
            pass
        raise BizError(f"SMTP 发送失败: {e}")


# 钉钉/飞书 加签：timestamp+"\n"+secret → HMAC-SHA256 → base64 → urlenamencode
def _robot_sign(secret: str, timestamp: str) -> str:
    if not secret:
        return ""
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def send_url(channel: dict) -> str:
    """加入时间戳+签名的最终请求 URL（无 secret 则原样）。"""
    url = channel.get("webhook_url", "")
    secret = channel.get("secret", "")
    if not secret:
        return url
    ts = str(int(time.time() * 1000))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={_robot_sign(secret, ts)}"


async def send(channel_type: str, channel: dict, subject: str, content: str, to: list[str] | None = None) -> None:
    """按渠道类型分派发送。channel 需为已解密的 resolved 视图；邮件收件人由调用方传入。"""
    if not channel.get("enabled", True):
        return
    if channel_type == "wecom":
        await _http_post(channel["webhook_url"], {"msgtype": "text", "text": {"content": content}})
    elif channel_type == "dingtalk":
        await _http_post(send_url(channel), {"msgtype": "text", "text": {"content": content}})
    elif channel_type == "feishu":
        await _http_post(send_url(channel), {"msg_type": "text", "content": {"text": content}})
    elif channel_type == "email":
        ch = dict(channel)
        ch["to"] = [x.strip() for x in (to or []) if x and x.strip()]
        await asyncio.to_thread(_smtp_send, ch, subject, content)
    else:
        raise BizError(f"未知渠道类型: {channel_type}")
