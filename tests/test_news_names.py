from stock_selector.news.names import enrich_hints, load_name_maps


class FakeCol:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection):
        return list(self.docs)


class FakeDB:
    def __init__(self):
        self.by_name = {}

    def __getitem__(self, name):
        return self.by_name[name]


def test_enrich_hints_board_and_stock():
    hints = [
        {"code": "BK0464", "name": "", "market": "板块"},
        {"code": "159945", "name": "", "market": "A股"},
        {"code": "", "name": "GOOG", "market": "美股"},
    ]
    out = enrich_hints(hints, board_map={"BK0464": "汽车整车"}, stock_map={"159945": "XX股份"})
    assert out[0]["name"] == "汽车整车"
    assert out[1]["name"] == "XX股份"
    assert out[2]["name"] == "GOOG"  # 美股不受影响


def test_enrich_hints_no_maps():
    hints = [{"code": "BK0464", "name": "", "market": "板块"}]
    assert enrich_hints(hints) == hints
    assert enrich_hints(hints, board_map={}, stock_map={}) == hints


def test_enrich_hints_does_not_mutate():
    hints = [{"code": "BK0464", "name": "", "market": "板块"}]
    enrich_hints(hints, board_map={"BK0464": "汽车整车"})
    assert hints[0]["name"] == ""


def test_load_name_maps():
    db = FakeDB()
    db.by_name["bk_stocks"] = FakeCol([
        {"bk_code": "BK0464", "bk_name": "汽车整车"},
        {"stock_code": "159945", "stock_name": "XX股份"},
        {"stock_code": "300207", "stock_name": "欣旺达"},
    ])
    db.by_name["sector_stocks"] = FakeCol([
        {"stock_code": "sh.600036", "stock_name": "招商银行"},
    ])
    board, stock = load_name_maps(db)
    assert board["BK0464"] == "汽车整车"
    assert stock["159945"] == "XX股份"
    assert stock["300207"] == "欣旺达"
    assert stock["600036"] == "招商银行"  # 前缀剥离后命中
