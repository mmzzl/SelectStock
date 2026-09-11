"""从 MongoDB 代码→名称映射回填 hint 的 name（bk_stocks / sector_stocks）。

- 板块名: bk_stocks.bk_code -> bk_name（BK 代码命名空间）
- 个股名: bk_stocks.stock_code -> stock_name（裸 6 位代码），
           缺失时 sector_stocks.stock_code（sh./sz. 前缀）兜底
"""

from __future__ import annotations


def load_name_maps(db):
    """构建 (board_map, stock_map)。

    board_map: {BK代码: 板块名}
    stock_map: {6位股票代码: 股票名}
    """
    board = {}
    stock = {}
    for x in db["bk_stocks"].find({}, {"bk_code": 1, "bk_name": 1}):
        if x.get("bk_code"):
            board.setdefault(str(x["bk_code"]), x.get("bk_name") or "")
    for x in db["bk_stocks"].find({}, {"stock_code": 1, "stock_name": 1}):
        if x.get("stock_code"):
            stock.setdefault(str(x["stock_code"]), x.get("stock_name") or "")
    for x in db["sector_stocks"].find({}, {"stock_code": 1, "stock_name": 1}):
        code = str(x.get("stock_code") or "").rsplit(".", 1)[-1]
        if code:
            stock.setdefault(code, x.get("stock_name") or "")
    return board, stock


def enrich_hints(hints, board_map=None, stock_map=None):
    """给归一化 hint 回填 name；返回新列表，不改入参。"""
    board_map = board_map or {}
    stock_map = stock_map or {}
    out = []
    for h in hints or []:
        h = dict(h)
        if h.get("market") == "板块" and h.get("code"):
            h["name"] = board_map.get(h["code"], h.get("name", ""))
        elif h.get("market") == "A股" and h.get("code"):
            h["name"] = stock_map.get(h["code"], h.get("name", ""))
        out.append(h)
    return out
