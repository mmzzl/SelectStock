"""App 端上传的 multipart 段可能没有 filename（SAF 句柄名），必须自己按原始字节切。"""
import base64
import json

import pytest

from api.v1._multipart import (base64_summary, extract_base64_upload, extract_field, extract_file,
                               has_base64_upload, part_summary)
from core.exceptions import BizError

BOUND = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
CTYPE = f"multipart/form-data; boundary={BOUND}"
PDF = b"%PDF-1.7\n\xff\xfe\x00\x01 binary-ish \x00\x00 tail"


def _mp(*parts: tuple[str, bytes, str | None]) -> bytes:
    out = b""
    for name, body, filename in parts:
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out += (f"--{BOUND}\r\n{disp}\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        out += body + b"\r\n"
    return out + f"--{BOUND}--\r\n".encode()


def test_file_part_without_filename_keeps_exact_bytes():
    raw = _mp(("file", PDF, None))
    body, filename = extract_file(raw, CTYPE)
    assert body == PDF and filename == ""


def test_saf_handle_style_filename_is_passed_through():
    raw = _mp(("file", PDF, "document:141169"))
    body, filename = extract_file(raw, CTYPE)
    assert body == PDF and filename == "document:141169"


def test_password_field_and_file_part_are_matched_by_name():
    raw = _mp(("password", b"138016", None), ("file", PDF, None))
    assert extract_field(raw, CTYPE, "password") == "138016"
    assert extract_file(raw, CTYPE)[0] == PDF          # 认 name=file，不是「最大段」
    assert extract_field(raw, CTYPE, "nope") == ""


def test_wrong_part_name_needs_a_filename():
    raw = _mp(("attachment", PDF, "a.pdf"), ("note", b"hi", None))
    assert extract_file(raw, CTYPE)[0] == PDF


def test_only_text_fields_is_not_mistaken_for_a_file():
    """App 读不到文件时只会发来 password：要报「没收到文件」，不能把密码当交割单解析。"""
    with pytest.raises(BizError) as e:
        extract_file(_mp(("password", b"138016", None)), CTYPE)
    assert "没收到文件" in str(e.value)


def test_empty_file_part_is_not_mistaken_for_a_file():
    with pytest.raises(BizError):
        extract_file(_mp(("file", b"", None), ("password", b"138016", None)), CTYPE)


def test_no_boundary_returns_whole_body():
    assert extract_file(b"abc", "application/octet-stream") == (b"abc", "")
    assert extract_field(b"abc", "text/plain", "file") == ""


def test_part_summary_shows_shapes_for_real_device_debugging():
    s = part_summary(_mp(("file", PDF, None)), CTYPE)
    assert "name=file" in s and f"bytes={len(PDF)}" in s and "filename=-" in s


def test_part_summary_reveals_probe_but_never_password():
    """真机诊断串要能在日志里读到；密码一个字都不能进日志。"""
    s = part_summary(_mp(("probe", "通道=multipart(resolved) path=_doc/x.pdf".encode(), None),
                         ("password", b"138016", None),
                         ("file", PDF, None)), CTYPE)
    assert "通道=multipart(resolved)" in s
    assert "138016" not in s and "head=31333830" in s


def test_missing_file_part_carries_device_diag():
    with pytest.raises(BizError) as e:
        extract_file(_mp(("password", b"138016", None),
                         ("probe", "resolve失败:14".encode(), None)), CTYPE)
    assert "没收到文件" in str(e.value) and "resolve失败:14" in str(e.value)


def test_base64_upload_roundtrip_and_oversize():
    raw = json.dumps({"file_b64": base64.b64encode(PDF).decode(),
                      "filename": "电子对账单.pdf", "password": "138016"}).encode()
    assert has_base64_upload(raw) is True
    data, name, pwd = extract_base64_upload(raw)
    assert (data, name, pwd) == (PDF, "电子对账单.pdf", "138016")
    assert has_base64_upload(b'{"a":1}') is False
    with pytest.raises(BizError):
        extract_base64_upload(json.dumps({"file_b64": ""}).encode())
    with pytest.raises(BizError):
        extract_base64_upload(b"not json at all file_b64")


def test_base64_summary_keeps_body_out_of_logs():
    s = base64_summary(json.dumps({"file_b64": base64.b64encode(PDF).decode(),
                                   "password": "138016", "probe": "p=1"}).encode())
    assert "head=25504446" in s and "p=1" in s
    assert "138016" not in s and base64.b64encode(PDF).decode() not in s


CSV = "成交日期,证券代码,证券名称,买卖标志,成交数量,成交价格\n2026-09-01,600519,贵州茅台,买入,100,1680.5\n".encode()


async def test_endpoint_accepts_nameless_file_part(client, admin_headers):
    """真机 SAF 上传没有 filename：端点要照样解析，不能再掉回 body 分支报 500。"""
    body = _mp(("file", CSV, None), ("password", b"", None))
    r = await client.post("/api/v1/trades/import/preview", content=body,
                          headers={"Content-Type": CTYPE, **admin_headers})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["valid"] == 1 and d["errors"] == []


async def test_endpoint_still_accepts_plain_text_body(client, admin_headers):
    r = await client.post("/api/v1/trades/import/preview", content=CSV,
                          headers={"Content-Type": "text/plain", **admin_headers})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["valid"] == 1


async def test_endpoint_accepts_base64_json_upload(client, admin_headers):
    """App 绕开 uni.uploadFile 的那条通道：SAF 句柄名 + base64，服务端照样认格式。"""
    body = json.dumps({"file_b64": base64.b64encode(CSV).decode(),
                       "filename": "document:141169", "password": "",
                       "probe": "通道=base64(native-read)"}).encode()
    r = await client.post("/api/v1/trades/import/preview", content=body,
                          headers={"Content-Type": "application/json", **admin_headers})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["valid"] == 1


async def test_endpoint_base64_empty_file_reports_actionable_error(client, admin_headers):
    body = json.dumps({"file_b64": "", "probe": "resolve失败:14"}).encode()
    r = await client.post("/api/v1/trades/import/preview", content=body,
                          headers={"Content-Type": "application/json", **admin_headers})
    assert r.status_code == 400 and "base64 为空" in r.json()["message"]
