"""交割单解析：默认券商中文表头→内部规范名；CSV 纯 stdlib，Excel 可选 openpyxl。"""
from __future__ import annotations

import csv
import io
import re

from config import settings

_CODE_RE = re.compile(r"^\d{6}$")
_DIR_ALIAS = {"买": "buy", "买入": "buy", "b": "buy", "buy": "buy",
              "卖": "sell", "卖出": "sell", "s": "sell", "sel": "sell", "sell": "sell"}


def _build_header_idx(headers: list[str]) -> dict[str, int]:
    aliases = settings.trades_columns
    idx: dict[str, int] = {}
    for internal, names in aliases.items():
        for col in headers:
            if col in names:
                idx[internal] = headers.index(col)
                break
    return idx


def _norm(x: str) -> str:
    return (x or "").strip()


def _as_float(x: str) -> float | None:
    try:
        return round(float(_norm(x)), 2)
    except (TypeError, ValueError):
        return None


def _as_int(x: str) -> int | None:
    try:
        return int(float(_norm(x)))
    except (TypeError, ValueError):
        return None


def _parse_row(header_idx: dict, cells: list[str], lineno: int, errors: list[dict]) -> dict | None:
    def get(k: str) -> str:
        i = header_idx.get(k)
        return _norm(cells[i]) if i is not None and i < len(cells) else ""

    code = get("stock_code")
    if not _CODE_RE.match(code):
        errors.append({"line": lineno, "reason": "证券代码需为6位数字"})
        return None
    direction = _DIR_ALIAS.get(get("direction").lower())
    if not direction:
        errors.append({"line": lineno, "reason": "买卖标志无法识别"})
        return None
    quantity = _as_int(get("quantity"))
    if not quantity or quantity <= 0:
        errors.append({"line": lineno, "reason": "成交数量需为正整数"})
        return None
    price = _as_float(get("price"))
    if price is None or price <= 0:
        errors.append({"line": lineno, "reason": "成交价格非法"})
        return None
    trade_date = get("trade_date")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
        errors.append({"line": lineno, "reason": "成交日期需为 YYYY-MM-DD"})
        return None
    return {
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": get("stock_name")[:64],
        "direction": direction,
        "price": price,
        "quantity": int(quantity),
        "amount": _as_float(get("amount")) or 0.0,
        "commission": _as_float(get("commission")) or 0.0,
        "stamp_tax": _as_float(get("stamp_tax")) or 0.0,
        "transfer_fee": _as_float(get("transfer_fee")) or 0.0,
        "settled_amount": _as_float(get("settled_amount")) or 0.0,
    }


def parse_deal_table(headers: list[str], data_rows: list[list[str]]) -> dict:
    errors: list[dict] = []
    items: list[dict] = []
    header_idx = _build_header_idx([_norm(h) for h in headers])
    missing = {
        k for k in ("trade_date", "stock_code", "direction", "quantity", "price")
        if k not in header_idx
    }
    if missing:
        return {"total": 0, "valid": 0, "errors": [
            {"line": 1, "reason": f"缺少必需列: {sorted(missing)}"}], "items": []}
    for lineno, cells in enumerate(data_rows, start=2):
        if not any(_norm(c) for c in cells):
            continue
        row = _parse_row(header_idx, cells, lineno, errors)
        if row is not None:
            items.append(row)
    return {"total": len(items) + len(errors), "valid": len(items), "errors": errors, "items": items}


def parse_deal_text(text: str) -> dict:
    text = (text or "").removeprefix("\uFEFF")
    reader = csv.reader(io.StringIO(text.strip()))
    rows = [r for r in reader if any(_norm(x) for x in r)]
    if not rows:
        return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": "文件为空"}], "items": []}
    return parse_deal_table(rows[0], rows[1:])


def parse_deal_bytes(raw: bytes, filename: str) -> dict:
    if filename.lower().endswith(".csv"):
        try:
            return parse_deal_text(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return parse_deal_text(raw.decode("gbk", errors="replace"))
    if filename.lower().endswith(".xlsx"):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return {"total": 0, "valid": 0,
                    "errors": [{"line": 1, "reason": "解析 .xlsx 需要安装 openpyxl"}], "items": []}
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception as exc:  # 损坏/非法 zip：优雅降级，不抛到调用方
            return {"total": 0, "valid": 0,
                    "errors": [{"line": 1, "reason": f"Excel 解析失败: {exc.__class__.__name__}"}],
                    "items": []}
        ws = wb.active
        raw_rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(max_row=2000)]
        if not raw_rows:
            return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": "Excel 为空"}], "items": []}
        return parse_deal_table(raw_rows[0], raw_rows[1:])
    return {"total": 0, "valid": 0,
            "errors": [{"line": 1, "reason": "仅支持 .csv / .xlsx 交割单"}], "items": []}


def trade_row_to_document(item: dict) -> dict:
    """规整为 TradeCreate 少 user_id/batch 的数据（供 commit 落库前补属主）。"""
    return {
        "trade_date": item["trade_date"],
        "stock_code": item["stock_code"],
        "stock_name": item.get("stock_name", ""),
        "direction": item["direction"],
        "price": float(item["price"]),
        "quantity": int(item["quantity"]),
        "amount": float(item.get("amount", 0.0)),
        "commission": float(item.get("commission", 0.0)),
        "stamp_tax": float(item.get("stamp_tax", 0.0)),
        "transfer_fee": float(item.get("transfer_fee", 0.0)),
        "settled_amount": float(item.get("settled_amount", 0.0)),
    }
