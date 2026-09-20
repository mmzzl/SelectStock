from services.deal_parser import parse_deal_text, parse_deal_bytes

CSV_HEADER = "成交日期,证券代码,证券名称,买卖标志,成交数量,成交价格,成交金额,手续费,印花税,过户费,结算金额\n"


def test_parse_csv_standard():
    text = CSV_HEADER + "2026-09-01,600519,贵州茅台,买入,100,1680.50,168050,5,168.05,0.5,168223.55\n"
    out = parse_deal_text(text)
    assert out["total"] == 1 and out["errors"] == []
    it = out["items"][0]
    assert it["stock_code"] == "600519" and it["direction"] == "buy"
    assert it["quantity"] == 100 and round(it["price"], 2) == 1680.5


def test_direction_aliases():
    text = CSV_HEADER + "2026-09-01,600519,N,卖出,100,10,1000,1,1,0,1002\n2026-09-02,000001,N,B,200,5,1000,0,0,0,1000\n"
    out = parse_deal_text(text)
    assert [i["direction"] for i in out["items"]] == ["sell", "buy"]


def test_row_errors_and_bom():
    text = "\uFEFF" + CSV_HEADER + ",600519,N,B,abc,1,1,0,0,0,1\n"
    out = parse_deal_text(text)
    assert out["errors"] and out["valid"] == 0
    assert out["errors"][0]["line"] == 2


def test_missing_header_rejected():
    out = parse_deal_text("日期,代码,数量\n2026-01-01,600519,100\n")
    assert out["valid"] == 0 and len(out["errors"]) == 1


def test_excel_corrupt_graceful():
    # openpyxl 已安装时，非法 zip 字节应优雅降级为错误（不抛异常到调用方）
    out = parse_deal_bytes(b"PK\x03\x04dummy", "f.xlsx")
    assert out["valid"] == 0 and out["items"] == []
    assert out["errors"] and out["errors"][0]["line"] == 1


def test_unsupported_extension():
    out = parse_deal_bytes(b"whatever", "f.xls")
    assert out["valid"] == 0 and "仅支持" in out["errors"][0]["reason"]
