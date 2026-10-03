"""api/v1/_multipart.py — 从原始请求体里取交割单文件。

App 端（uni.uploadFile + SAF 选出来的文件）的 part 常常没有 filename 甚至没有
Content-Disposition，FastAPI 的 File() 拿不到就会退回去读已被消费掉的 body
（RuntimeError: Stream consumed → 500）。这里自己按 boundary 切，拿到的是原始字节，
二进制 PDF 不会被表单解析当文本解码掉。

另外留一条 base64 通道：App 读 SAF 句柄时 uploadFile 会静默丢文件段，
plus.io / ContentResolver 直接把整份读成 base64 后用 JSON 提交，完全不碰 multipart。
"""
from __future__ import annotations

import base64
import json
import re

from core.exceptions import BizError

_MAX_BODY = 20 * 1024 * 1024


def _split_parts(raw: bytes, boundary: bytes) -> list[tuple[dict[str, str], bytes]]:
    parts: list[tuple[dict[str, str], bytes]] = []
    for seg in raw.split(b"--" + boundary)[1:-1]:
        seg = seg[2:] if seg[:2] == b"\r\n" else seg.lstrip(b"\r\n")
        head, sep, body = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.split(b"\r\n"):
            k, _, v = line.partition(b":")
            if v:
                headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
        fields = {}
        for value in re.findall(r'(\w+)="([^"]*)"', headers.get("content-disposition", "")):
            fields[value[0]] = value[1]
        info = {"name": fields.get("name", ""), "filename": fields.get("filename", ""),
                "content-type": headers.get("content-type", "")}
        parts.append((info, body[:-2] if body[-2:] == b"\r\n" else body))
    return parts


def _boundary(content_type: str) -> bytes | None:
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type or "")
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip().encode("latin-1")


def extract_file(raw: bytes, content_type: str) -> tuple[bytes, str]:
    """返回 (文件字节, 文件名)。文件名可能为空——调用方要按魔数认格式。"""
    if not raw:
        raise BizError("请求体是空的", code=400, status_code=400)
    if len(raw) > _MAX_BODY:
        raise BizError("交割单文件过大（上限 20MB）", code=413, status_code=413)
    boundary = _boundary(content_type)
    if boundary is None:
        return raw, ""
    parts = _split_parts(raw, boundary)
    if not parts:
        raise BizError("没解析出任何上传段，请改用「粘贴文本」导入", code=400, status_code=400)
    file_part = next(((i, b) for i, b in parts if i["name"] == "file" and b), None) \
        or next(((i, b) for i, b in parts if i["filename"] and b), None)
    if file_part is None:
        # 以前会退化成「最大的一段」，结果把 password 字段当交割单解析，报一堆缺列错误
        probe = next((b.decode("utf-8", errors="replace")
                      for i, b in parts if i["name"] == "probe" and b), "")
        raise BizError("没收到文件内容：App 端没能读出所选文件"
                       + (f"（诊断: {probe}）" if probe else "")
                       + "。请把对账单复制到「下载」目录后重选，或改用「粘贴文本」导入",
                       code=400, status_code=400)
    info, body = file_part
    return body, info["filename"]


def extract_field(raw: bytes, content_type: str, field_name: str) -> str:
    boundary = _boundary(content_type)
    if boundary is None:
        return ""
    for info, body in _split_parts(raw, boundary):
        if info["name"] == field_name:
            return body.decode("utf-8", errors="replace").strip()
    return ""


def part_summary(raw: bytes, content_type: str) -> str:
    """真机上传出问题时唯一能看到的证据：每段的名字/文件名/字节数/头 4 字节。

    短文本段直接带上原文——App 端把「卡在哪一步」的诊断串放在 probe 字段里传上来，
    日志里必须能读出来（password 例外，绝不落日志）。
    """
    boundary = _boundary(content_type)
    if boundary is None:
        return f"no-boundary ctype={content_type}"
    out = []
    for info, body in _split_parts(raw, boundary):
        line = (f"name={info['name'] or '-'} filename={info['filename'] or '-'} "
                f"bytes={len(body)} head={body[:4].hex()}")
        if info["name"] not in ("password", "file") and not info["filename"] and len(body) <= 400:
            line += f" value={body.decode('utf-8', errors='replace')}"
        out.append(line)
    return " | ".join(out) or "no-parts"


def has_base64_upload(raw: bytes) -> bool:
    return b"file_b64" in raw[:4096]


def extract_base64_upload(raw: bytes) -> tuple[bytes, str, str]:
    """JSON {file_b64, filename, password} → (文件字节, 文件名, 密码)。"""
    if len(raw) > _MAX_BODY * 2:          # base64 比原字节大约 4/3
        raise BizError("交割单文件过大（上限 20MB）", code=413, status_code=413)
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise BizError("上传体不是合法 JSON", code=400, status_code=400)
    if not isinstance(body, dict):
        raise BizError("上传体不是 JSON 对象", code=400, status_code=400)
    b64 = str(body.get("file_b64") or "").strip()
    if not b64:
        raise BizError("没收到文件内容（base64 为空）", code=400, status_code=400)
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as e:
        raise BizError(f"文件内容解码失败: {e}", code=400, status_code=400)
    if not data:
        raise BizError("上传的文件是空的", code=400, status_code=400)
    if len(data) > _MAX_BODY:
        raise BizError("交割单文件过大（上限 20MB）", code=413, status_code=413)
    return data, str(body.get("filename") or ""), str(body.get("password") or "")


def base64_summary(raw: bytes) -> str:
    """日志用：只打长度和头部，绝不把 base64 正文或密码写进日志。"""
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return f"bad-json bytes={len(raw)}"
    b64 = str(body.get("file_b64") or "")
    head = ""
    try:
        head = base64.b64decode(b64[:8] + "==", validate=False)[:4].hex()
    except Exception:
        pass
    return (f"bytes={len(raw)} b64_chars={len(b64)} head={head} "
            f"name={body.get('filename') or '-'} "
            f"probe={str(body.get('probe') or '-')[:300]}")

