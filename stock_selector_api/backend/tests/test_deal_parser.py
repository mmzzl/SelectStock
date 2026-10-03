from services.deal_parser import (
    parse_deal_text, parse_deal_bytes, parse_deal_pdf_tables,
)

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


def test_tab_separated_paste():
    text = ("成交日期\t证券代码\t买卖标志\t成交数量\t成交价格\n"
            "2026-09-01\t600519\t买入\t100\t1680.50\n")
    out = parse_deal_text(text)
    assert out["valid"] == 1 and out["errors"] == []


def test_whitespace_aligned_paste_from_pdf():
    """从 PDF 阅读器全选复制出来的对账单是空格对齐的：整行没有半个逗号。"""
    text = ("东方财富证券股份有限公司交易总部股票明细对账单\n"
            "打印日期：2026-10-02 11:13:10\n"
            "发生日期 买卖类别 证券代码 证券名称 成交数量 成交价格 总发生金额 手续费\n"
            "20260929 证券买入 159599 芯片ETF东财 1100 2.6060 -2866.74 0.14\n"
            "20260929 证券卖出 002855 捷荣技术 700 12.450 8715.00 5.20\n")
    out = parse_deal_text(text)
    assert out["valid"] == 2 and out["errors"] == []
    buy, sell = out["items"]
    assert buy["trade_date"] == "2026-09-29" and buy["stock_code"] == "159599"
    assert buy["quantity"] == 1100 and round(buy["price"], 3) == 2.606
    assert sell["direction"] == "sell" and sell["stock_name"] == "捷荣技术"


def test_repeated_header_across_pages_is_not_a_trade_row():
    text = ("发生日期 买卖类别 证券代码 成交数量 成交价格\n"
            "20260929 买入 159599 1100 2.606\n"
            "第2页，共2页\n"
            "发生日期 买卖类别 证券代码 成交数量 成交价格\n"
            "20260930 卖出 159599 500 2.700\n")
    out = parse_deal_text(text)
    assert out["valid"] == 2 and out["errors"] == []
    assert [i["trade_date"] for i in out["items"]] == ["2026-09-29", "2026-09-30"]


def test_excel_corrupt_graceful():
    # openpyxl 已安装时，非法 zip 字节应优雅降级为错误（不抛异常到调用方）
    out = parse_deal_bytes(b"PK\x03\x04dummy", "f.xlsx")
    assert out["valid"] == 0 and out["items"] == []
    assert out["errors"] and out["errors"][0]["line"] == 1


def test_unsupported_binary_rejected():
    # .xls 是 OLE 复合文档，没有 %PDF/PK 魔数但也含 NUL：不能当文本硬解
    out = parse_deal_bytes(b"\xd0\xcf\x11\xe0\x00\x00\x00\x00F", "f.xls")
    assert out["valid"] == 0 and "仅支持" in out["errors"][0]["reason"]


PDF_HEADER = ["成交日期", "证券代码", "证券名称", "买卖标志", "成交数量", "成交价格", "成交金额"]
PDF_ROW = ["2026/09/01", "600519", "贵州茅台", "证券买入", "1,200", "1,680.50", "2016600.00"]


def test_pdf_tables_with_repeated_header_and_title():
    tables = [
        [["中国XX证券 对账单", "", ""], PDF_HEADER, PDF_ROW],
        [PDF_HEADER, ["20260902", "000001", "平安银行", "卖出", "300", "11.20", "3360.00"]],
    ]
    out = parse_deal_pdf_tables(tables)
    assert out["valid"] == 2 and out["errors"] == []
    first, second = out["items"]
    assert first["trade_date"] == "2026-09-01" and first["direction"] == "buy"
    assert first["quantity"] == 1200 and first["price"] == 1680.5
    assert second["trade_date"] == "2026-09-02" and second["direction"] == "sell"


def test_pdf_without_header_reports_actionable_reason():
    out = parse_deal_pdf_tables([[["姓名", "资金账号", "人民币"], ["张三", "1234", "0.00"]]])
    assert out["valid"] == 0 and "没找到交割单表头" in out["errors"][0]["reason"]


def test_pdf_corrupt_bytes_graceful():
    out = parse_deal_bytes(b"%PDF-1.4 not really a pdf", "deal.pdf")
    assert out["valid"] == 0 and out["errors"][0]["line"] == 1


def test_bad_extension_message_lists_pdf():
    assert ".pdf" in parse_deal_bytes(b"\xd0\xcf\x11\xe0\x00\x00", "f.xls")["errors"][0]["reason"]


def test_pdf_recognized_by_magic_when_extension_lost():
    """App 端 SAF 临时文件常丢扩展名：靠 %PDF 魔数认，不能掉进「仅支持」分支。"""
    out = parse_deal_bytes(b"%PDF-1.4 broken", "1759424000000")
    assert "仅支持" not in out["errors"][0]["reason"]


def test_magic_wins_over_wrong_extension():
    """文件名写着 .csv 但内容是 PDF：按内容走 PDF 分支（App 端拿到的常是没意义的句柄名）。"""
    out = parse_deal_bytes(b"%PDF-1.4 broken", "document:141169.csv")
    assert "仅支持" not in out["errors"][0]["reason"]


def test_xlsx_recognized_by_zip_magic():
    out = parse_deal_bytes(b"PK\x03\x04dummy", "document:141169")
    assert out["errors"][0]["reason"].startswith("Excel")


def test_nameless_text_falls_back_to_csv_parsing():
    out = parse_deal_bytes("成交日期,证券代码,证券名称,买卖标志,成交数量,成交价格\n"
                           "2026-09-01,600519,贵州茅台,买入,100,1680.5\n".encode("utf-8"),
                           "document:141169")
    assert out["valid"] == 1 and out["errors"] == []


EASTMONEY_HEADER = ["发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
                    "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额"]


def test_eastmoney_statement_headers_and_fund_flow_rows():
    """东财对账单：别名要认，非买卖流水（银证转账/利息/红利/扣税）静默跳过并计数。"""
    tables = [[
        EASTMONEY_HEADER,
        ["20260929", "证券买入", "159599", "芯片ETF东财", "1100", "2.6060", "-2866.74", "0.14", "0.00", "0.00", "6119.00"],
        ["20260831", "股息红利差异扣税", "600188", "兖矿能源", "0", "0.0000", "-25.60", "0.00", "0.00", "0.00", "5980.76"],
        ["20251222", "利息归本", "", "", "0", "0.0000", "1.03", "0.00", "0.00", "0.00", "20192.49"],
        ["20251202", "银行转证券", "", "", "0", "0.0000", "10000.00", "0.00", "0.00", "0.00", "20191.46"],
    ]]
    out = parse_deal_pdf_tables(tables)
    assert out["valid"] == 1 and out["errors"] == [] and out["skipped"] == 3
    it = out["items"][0]
    assert it["trade_date"] == "2026-09-29" and it["direction"] == "buy"
    assert it["price"] == 2.606                      # ETF 四位小数不能被截成两位
    assert it["settled_amount"] == -2866.74


ENC_PDF = b"%PDF-1.7\n1 0 obj\n<< /Encrypt 2 0 R >>\nendobj\ntrailer\n<< /Encrypt 2 0 R >>\n%%EOF\n"


def test_encrypted_pdf_without_password_is_actionable():
    out = parse_deal_bytes(ENC_PDF, "对账单.pdf")
    assert out["valid"] == 0 and "已加密" in out["errors"][0]["reason"]


def test_encrypted_pdf_with_wrong_password_says_password():
    out = parse_deal_bytes(ENC_PDF, "对账单.pdf", "000000")
    assert "密码不正确" in out["errors"][0]["reason"]


def test_encrypted_pdf_needs_password_even_without_filename():
    """真机 SAF 只给 document:141169：加密提示必须照旧出现，不能退化成「仅支持」。"""
    out = parse_deal_bytes(ENC_PDF, "document:141169")
    assert "该 PDF 已加密" in out["errors"][0]["reason"]
