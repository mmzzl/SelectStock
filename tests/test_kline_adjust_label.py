"""`stock_kline` 的 `adjust` 标签过去是错的（标 qfq、实为裸价，§48.1）⇒ 读取方一律不再按它过滤。

按标签过滤有两个坑：标签本身不可信（过滤出来的东西口径说不清），以及爬虫改成标 `raw` 之后，
老过滤器会**静默 0 命中**——下游照样出结果，只是少最近那一段。价格口径改由 `stock_adj_factor`
的因子台阶决定，标签只当采集记录。
"""
import importlib.util

SCRIPTS = {
    "build_kline_features": "scripts/build_kline_features.py",
    "splice_kline_from_mongo": "scripts/splice_kline_from_mongo.py",
}


def _load(path, name=None):
    spec = importlib.util.spec_from_file_location(name or path, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_feature_builders_do_not_filter_on_the_adjust_label():
    for name, path in SCRIPTS.items():
        mod = _load(path, name)
        flt = getattr(mod, "KLINE_FILTER", None) or getattr(mod, "SOURCE_FILTER")
        assert "adjust" not in flt, f"{name} 还在按 adjust 过滤：{flt}"
        assert "frequency" in flt, f"{name} 丢了 frequency 会混进别周期的行：{flt}"


def test_mongo_source_query_ignores_the_adjust_label():
    from stock_selector.datasource.mongo_source import MongoSource

    captured = {}

    class _Cur:
        def sort(self, *a, **k):
            return self

        def limit(self, n):
            return self

        def __iter__(self):
            return iter([])

    class _Col:
        def find(self, flt=None, projection=None):
            captured["query"] = dict(flt or {})
            return _Cur()

    class _Db:
        def __getitem__(self, name):
            return _Col()

    ds = MongoSource({"rows": 3})
    ds._client = object()                 # 阻断真实连接
    ds._db = lambda: _Db()
    assert ds.get_kline("600519") is None
    assert captured["query"] == {"code": "600519", "frequency": 9}


def test_relabel_only_touches_the_mislabeled_rows():
    """12M 行的标签回补是不可撤销的大写：查询必须锁死「标着 qfq 的那批」，已经对的一行不碰。"""
    mod = _load("scripts/relabel_kline_adjust.py", "relabel_kline_adjust")
    touched = []

    class _Res:
        matched_count = 7
        modified_count = 7

    class _Col:
        def update_many(self, flt, update):
            touched.append((flt, update))
            return _Res()

    n = mod.relabel_chunk(_Col(), ["600519", "000001"])
    assert n == 7
    flt, update = touched[0]
    assert flt == {"code": {"$in": ["600519", "000001"]}, "adjust": "qfq"}
    assert update == {"$set": {"adjust": "raw"}}


def test_relabel_chunks_cover_the_whole_universe_without_overlap():
    mod = _load("scripts/relabel_kline_adjust.py", "relabel_kline_adjust")
    codes = [f"{i:06d}" for i in range(1001)]
    chunks = list(mod.chunks(codes, size=250))
    assert [len(c) for c in chunks] == [250, 250, 250, 250, 1]
    assert sum(len(c) for c in chunks) == len(codes)
    assert len({x for c in chunks for x in c}) == len(codes)
