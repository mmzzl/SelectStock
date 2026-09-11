from stock_selector.news.normalize import normalize_secid


def test_board_secid():
    assert normalize_secid("90.BK0464")["code"] == "BK0464"
    assert normalize_secid("90.BK0464")["market"] == "板块"


def test_board_1007():
    out = normalize_secid("1007.BK000199")
    assert out["code"] == "BK000199" and out["market"] == "板块"


def test_a_share_prefixed():
    assert normalize_secid("0.159945")["code"] == "159945"
    assert normalize_secid("1.688256")["code"] == "688256"
    assert normalize_secid("150.014238")["code"] == "014238"
    for s in ("0.159945", "1.688256", "150.014238"):
        assert normalize_secid(s)["market"] == "A股"


def test_us_market():
    out = normalize_secid("105.GOOG")
    assert out["code"] == "" and out["name"] == "GOOG" and out["market"] == "美股"


def test_hk_market():
    out = normalize_secid("201.HK49")
    assert out["name"] == "HK49" and out["market"] == "港股"


def test_unknown_prefix_returns_unknown():
    for s in ("116.06030", "999.356570"):
        out = normalize_secid(s)
        assert out["market"] == "未知" and out["code"] == "" and out["name"] == ""


def test_garbage_input():
    for s in ("", None, "bad"):
        out = normalize_secid(s)
        assert out["market"] == "未知" and out["code"] == "" and out["name"] == ""
