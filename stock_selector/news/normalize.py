"""东方财富 stockList 的 secid 归一化。"""

from __future__ import annotations


def normalize_secid(secid: str) -> dict:
    """把 secid 归一化为 {"code", "name", "market"}。

    - 90.BK0464 / 1007.BK000199 -> {"code": "BK0464", "name": "", "market": "板块"}
    - 0.159945 / 1.688256 / 150.014238 -> {"code": "159945", "name": "", "market": "A股"}
    - 105.GOOG 等 -> {"code": "", "name": "GOOG", "market": "美股"}
    - 201.HK49 -> {"code": "", "name": "HK49", "market": "港股"}
    - 116.06030 / 999.356570 等未知前缀 -> {"code": "", "name": "", "market": "未知"}
    """
    if not secid:
        return {"code": "", "name": "", "market": "未知"}
    s = str(secid).strip()
    prefix, _, suffix = s.partition(".")
    if not prefix or not suffix:
        return {"code": "", "name": "", "market": "未知"}
    if prefix in ("90", "1007") and suffix[:2] == "BK":
        return {"code": suffix, "name": "", "market": "板块"}
    if prefix in ("0", "1", "150") and suffix.isdigit() and len(suffix) == 6:
        return {"code": suffix, "name": "", "market": "A股"}
    if prefix in ("105", "106", "107", "153"):
        return {"code": "", "name": suffix, "market": "美股"}
    if prefix == "201":
        return {"code": "", "name": suffix, "market": "港股"}
    return {"code": "", "name": "", "market": "未知"}
