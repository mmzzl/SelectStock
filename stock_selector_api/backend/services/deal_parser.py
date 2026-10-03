"""交割单解析：默认券商中文表头→内部规范名；CSV 纯 stdlib，Excel 可选 openpyxl，PDF 可选 pdfplumber。"""
from __future__ import annotations

import csv
import io
import re

from config import settings

_CODE_RE = re.compile(r"^\d{6}$")
_DIR_ALIAS = {"买": "buy", "买入": "buy", "b": "buy", "buy": "buy",
              "卖": "sell", "卖出": "sell", "s": "sell", "sel": "sell", "sell": "sell"}
# 券商交割单里的方向写法比 CSV 花：证券买入/担保品买入/核中和卖出…
_DIR_HINTS = (("买", "buy"), ("sell", "sell"), ("卖", "sell"), ("b", "buy"))


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


def _as_float(x: str, nd: int = 2) -> float | None:
    s = _norm(x).replace(",", "").replace("，", "")
    if s.startswith("(") and s.endswith(")"):     # 部分券商用括号表示负数
        s = "-" + s[1:-1]
    try:
        return round(float(s), nd)
    except (TypeError, ValueError):
        return None


def _as_int(x: str) -> int | None:
    try:
        return int(float(_norm(x).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _norm_date(x: str) -> str:
    """券商导出/打印件里日期写法不一：2026/01/05、2026.1.5、20260105 都归一到 ISO。"""
    s = _norm(x).split(" ")[0]
    m = re.match(r"^(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?$", s)
    if not m:
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _direction(raw: str) -> str | None:
    s = _norm(raw).lower()
    d = _DIR_ALIAS.get(s)
    if d:
        return d
    for hint, val in _DIR_HINTS:
        if hint in s:
            return val
    return None


def _parse_row(header_idx: dict, cells: list[str], lineno: int, errors: list[dict],
               skip_non_trade: bool = False) -> dict | None:
    """skip_non_trade：对账单里代码留空或买卖类别不含「买/卖」的行（银证转账、利息归本、
    红利入账、股息红利差异扣税…）不是交易，跳过而不是刷错误。调用方用 skipped 计数兜透明。"""
    def get(k: str) -> str:
        i = header_idx.get(k)
        return _norm(cells[i]) if i is not None and i < len(cells) else ""

    code = get("stock_code")
    if not _CODE_RE.match(code):
        if skip_non_trade:
            return None
        errors.append({"line": lineno, "reason": "证券代码需为6位数字"})
        return None
    direction = _direction(get("direction"))
    if not direction:
        if skip_non_trade:
            return None
        errors.append({"line": lineno, "reason": "买卖标志无法识别"})
        return None
    quantity = _as_int(get("quantity"))
    if not quantity or quantity <= 0:
        errors.append({"line": lineno, "reason": "成交数量需为正整数"})
        return None
    price = _as_float(get("price"), 4)   # ETF/基金能到 4 位小数，按 2 位截会算错成本
    if price is None or price <= 0:
        errors.append({"line": lineno, "reason": "成交价格非法"})
        return None
    trade_date = _norm_date(get("trade_date"))
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


def _text_rows(text: str) -> list[list[str]]:
    """按内容认分隔符：制表符 → 逗号 → 空白对齐。

    从 PDF 阅读器里全选复制出来的交割单是空格对齐的，一行一个字段都没有逗号；
    只按逗号切的话整行会变成一个字段，报「缺少必需列」。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sample = "".join(lines[:4])
    if sample.count("\t") >= sample.count(",") and "\t" in sample:
        rows = list(csv.reader(io.StringIO(text.strip()), delimiter="\t"))
    elif "," in sample:
        rows = list(csv.reader(io.StringIO(text.strip())))
    else:
        rows = [ln.split() for ln in lines]        # \s+ 顺带吃掉全角空格与 NBSP
    return [r for r in rows if any(_norm(x) for x in r)]


def parse_deal_text(text: str) -> dict:
    text = (text or "").removeprefix("\uFEFF")
    rows = _text_rows(text)
    if not rows:
        return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": "文件为空"}], "items": []}
    strict = parse_deal_table(rows[0], rows[1:])
    if strict["errors"]:
        # 粘贴版常带页码/标题行，且多页反复出现表头——首行不是表头时按「就近继承表头」再试一次
        tolerant = parse_deal_pdf_tables([rows])
        if (tolerant["valid"], -len(tolerant["errors"])) > (strict["valid"], -len(strict["errors"])):
            return tolerant
    return strict


# PDF 表头至少要命中这几列才认它是表头，否则可能是账号信息/标题行
_MIN_HEADER_HITS = 3
_PDF_MAX_PAGES = 60


def _header_of(cells: list[str]) -> dict[str, int] | None:
    idx = _build_header_idx(cells)
    return idx if len(idx) >= _MIN_HEADER_HITS else None


def parse_deal_pdf_tables(tables: list[list[list[str]]]) -> dict:
    """PDF 抽出来的表格 → 复用同一套列映射与行级校验。

    表头按「就近继承」处理：多页交割单每页可能重复表头，也可能只有首页有，
    所以遇到新表头就切换，表头之前的标题/账号行直接丢弃而不是报一堆错。
    """
    errors: list[dict] = []
    items: list[dict] = []
    skipped = 0
    header_idx: dict[str, int] | None = None
    seen_header = False
    lineno = 1
    for table in tables:
        for row in table:
            cells = [_norm(c) for c in (row or [])]
            if not any(cells):
                continue
            hit = _header_of(cells)
            if hit:
                header_idx = hit
                seen_header = True
                continue
            if header_idx is None:
                continue
            if len([c for c in cells if c]) < 2:
                continue
            lineno += 1
            before = len(errors)
            row_parsed = _parse_row(header_idx, cells, lineno, errors, skip_non_trade=True)
            if row_parsed is not None:
                items.append(row_parsed)
            elif len(errors) == before:
                skipped += 1
    if not seen_header:
        return {"total": 0, "valid": 0, "errors": [
            {"line": 1, "reason": "PDF 里没找到交割单表头（需含 成交日期/证券代码/买卖标志/成交数量/成交价格）。"
                                   "若是扫描件或图片型 PDF，请改用 CSV 或让券商导出对账单文件"}],
            "items": [], "skipped": 0}
    return {"total": len(items) + len(errors) + skipped, "valid": len(items),
            "errors": errors, "items": items, "skipped": skipped}


def _is_encrypted(raw: bytes) -> bool:
    """加密 PDF 的 trailer 里必有 /Encrypt；用来区分「没给密码」和「文件本身坏了」。"""
    return b"/Encrypt" in raw


def _pdf_tables(raw: bytes, password: str = "") -> list[list[list[str]]]:
    import pdfplumber

    out: list[list[list[str]]] = []
    with pdfplumber.open(io.BytesIO(raw), password=password or None) as pdf:
        for page in pdf.pages[:_PDF_MAX_PAGES]:
            tables = [t for t in page.extract_tables() if t and len(t) > 1]
            if not tables:
                # 没有表格线（多数券商对账单是纯文字对齐）：按坐标切列
                tables = [t for t in page.extract_tables(
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}) if t and len(t) > 1]
            out.extend(tables)
    return out


def _err(reason: str) -> dict:
    return {"total": 0, "valid": 0, "errors": [{"line": 1, "reason": reason}], "items": []}


def _parse_pdf(raw: bytes, password: str) -> dict:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return _err("解析 .pdf 需要安装 pdfplumber（pip install pdfplumber）")
    if _is_encrypted(raw) and not password:
        return _err("该 PDF 已加密，请在下方「打开密码」里填写后重新预览"
                    "（券商对账单密码一般是身份证后 6 位或资金账号后 6 位）")
    try:
        tables = _pdf_tables(raw, password)
    except Exception as exc:   # 损坏/密码错：优雅降级，不抛给调用方
        return _err("PDF 打开密码不正确" if _is_encrypted(raw)
                    else f"PDF 解析失败: {exc.__class__.__name__}")
    if not tables:
        return _err("PDF 里没抽出任何表格，可能是扫描件/图片型 PDF")
    return parse_deal_pdf_tables(tables)


def _parse_xlsx(raw: bytes) -> dict:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return _err("解析 .xlsx 需要安装 openpyxl")
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # 损坏/非法 zip：优雅降级，不抛到调用方
        return _err(f"Excel 解析失败: {exc.__class__.__name__}")
    ws = wb.active
    raw_rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(max_row=2000)]
    if not raw_rows:
        return _err("Excel 为空")
    return parse_deal_table(raw_rows[0], raw_rows[1:])


def _parse_deal_text_bytes(raw: bytes) -> dict:
    try:
        return parse_deal_text(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return parse_deal_text(raw.decode("gbk", errors="replace"))


def parse_deal_bytes(raw: bytes, filename: str, password: str = "") -> dict:
    """魔数优先、扩展名其次：App 端 SAF 给的路径末段是 document:141169 这种句柄，根本没有文件名。"""
    name = (filename or "").lower()
    if raw[:4] == b"%PDF" or name.endswith(".pdf"):
        return _parse_pdf(raw, password)
    if raw[:4] == b"PK\x03\x04" or name.endswith(".xlsx"):
        return _parse_xlsx(raw)
    if not raw:
        return _err("上传的文件是空的")
    if b"\x00" in raw[:2048]:   # 不是 zip/pdf 的二进制：.xls 老格式之类，别当文本硬解
        return _err("仅支持 .csv / .xlsx / .pdf 交割单（.xls 请先在 Excel 里另存为 .xlsx）")
    return _parse_deal_text_bytes(raw)


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
