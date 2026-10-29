"""复权因子采集脚本（A1-b）里能纯测的部分：入库文档的幂等 _id、续跑判据、以及按板块定的涨跌停宽限。

抓的是东财 datacenter 的分红送配明细（一页 500 行足够一票全部历史），网络侧不在单测里碰。
这三件写错都会静默污染入库：_id 不稳＝同一天重复堆行；续跑判据错＝重抓或漏抓；
涨跌停宽限写死 10% 会把创业板/科创板 ±20%、北交所 ±30% 的正常波动全报成「日历漏了」。
"""
import importlib.util

SCRIPT = "scripts/kline_factor_spider.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("kline_factor_spider", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_factor_docs_are_idempotent_per_code_date():
    mod = _load_script()
    steps = [{"date": "2024-06-03", "prev_close": 20.0, "cash": 1.0, "bonus": 0.0,
              "transfer": 0.0, "step": 0.95, "factor": 1.0526, "residual": 0.0,
              "suspect": False}]
    docs = mod.factor_docs("600519", steps, crawl_time="2026-10-29 15:00:00")
    assert len(docs) == 1
    d = docs[0]
    assert d["_id"] == "600519|2024-06-03"
    assert d["code"] == "600519" and d["factor"] == 1.0526
    assert d["suspect"] is False and d["crawl_time"] == "2026-10-29 15:00:00"


def test_pending_skips_codes_already_done():
    mod = _load_script()
    done = [{"code": "000001", "ok": True}, {"code": "000002", "ok": False}]
    assert mod.pending(["000001", "000002", "000003"], done) == ["000002", "000003"]


def test_pending_only_honours_fresh_runs_when_since_given():
    """给了 since 就是增量模式：老日子抓过的票要重新排队（新区间里可能出了新除权）。"""
    mod = _load_script()
    done = [{"code": "000001", "ok": True, "done_at": "2026-01-05 15:00:00"}]
    assert mod.pending(["000001"], done, since="2026-01-05 15:00:00") == []
    assert mod.pending(["000001"], done, since="2026-02-01 00:00:00") == ["000001"]


def test_limit_pct_follows_board():
    mod = _load_script()
    assert mod.limit_pct("600519") == 0.10
    assert mod.limit_pct("000001") == 0.10
    assert mod.limit_pct("300750") == 0.20     # 创业板
    assert mod.limit_pct("688111") == 0.20     # 科创板
    assert mod.limit_pct("830799") == 0.30     # 北交所


def test_attempt_turns_any_crash_into_a_recorded_failure(monkeypatch):
    """一轮全量抓数 6,900 多票，不能因为某一只票出了意外（接口加字段、写库主键冲突）就整轮断掉。

    实测就断在 002043：同一天两笔分红写出重复 `_id`，`insert_many` 抛 BulkWriteError，
    进程直接死在后面 6,300 多票上——所以异常必须折成 `ok=False` 落到 run 表里，`--status` 看得见。
    """
    mod = _load_script()
    calls = []

    class _Db:                        # 真库是 pymongo 的 db，`db[COL_KLINE]` 取得 collection
        def __getitem__(self, name):
            return name

    def boom(kcol, code):
        raise RuntimeError("dup key 002043|2026-06-01")

    monkeypatch.setattr(mod, "run_code", boom)
    res = mod.attempt(_Db(), "002043")
    assert res["code"] == "002043" and res["ok"] is False
    assert res["error"] == "RuntimeError: dup key 002043|2026-06-01"

    def fine(kcol, code):
        assert kcol == mod.COL_KLINE
        return {"code": code, "ok": True, "table": []}

    def write(db, r):
        calls.append(r["code"])

    monkeypatch.setattr(mod, "run_code", fine)
    monkeypatch.setattr(mod, "write_code", write)
    assert mod.attempt(_Db(), "600519")["ok"] is True
    assert calls == ["600519"]

    monkeypatch.setattr(mod, "write_code", lambda db, r: calls.append("不该发生"))
    assert mod.attempt(_Db(), "600519", dry_run=True)["ok"] is True
    assert calls == ["600519"]


def test_daily_bars_merges_the_two_spider_batches_ascending():
    """f=9 与 f=0 是互补的两批，必须并起来读；单用一批会有一半票是空的（§48.3 的洞）。"""
    mod = _load_script()

    class _Cur:
        def __init__(self, docs):
            self._docs = docs

        def sort(self, *a, **k):
            return self

        def __iter__(self):
            return iter(self._docs)

    class _Col:
        def __init__(self, docs):
            self._docs = docs

        def find(self, query, projection=None):
            return _Cur([d for d in self._docs if d["frequency"] == query["frequency"]])

    col = _Col([
        {"code": "000001", "frequency": 0, "date": "2023-01-04 15:00", "open": 12.0,
         "high": 12.4, "low": 11.9, "close": 12.2},
        {"code": "000001", "frequency": 9, "date": "2026-10-29 15:00", "open": 11.3,
         "high": 11.55, "low": 11.29, "close": 11.54},
        {"code": "000001", "frequency": 9, "date": "2026-10-30 15:00", "open": 0,
         "high": 0, "low": 0, "close": 0},                       # 停牌占位行，不进序列
    ])
    bars = mod.daily_bars(col, "000001")
    assert [b["date"] for b in bars] == ["2023-01-04", "2026-10-29"]
    assert bars[-1]["close"] == 11.54
    assert set(bars[0]) >= {"date", "open", "high", "low", "close"}
