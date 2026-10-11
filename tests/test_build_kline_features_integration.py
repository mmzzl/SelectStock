import importlib.util
import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/build_kline_features.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_kline_features", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _df(n=70):
    close = np.linspace(10, 12, n)
    df = pd.DataFrame(
        {
            "code": "603000",
            "name": "Test",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "open": close, "high": close * 1.02, "low": close * 0.98,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
        }
    )
    return df


def test_output_columns():
    mod = _load_script()
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(mod.OUTPUT_COLUMNS)
    # 形态/量能列必须显式进清单：compute_indicators 末尾按 OUTPUT_COLUMNS 截断，漏了就静默丢列
    assert set(mod.PATTERN_COLUMNS) <= set(mod.OUTPUT_COLUMNS)


def test_compute_indicators_has_derived():
    mod = _load_script()
    df = _df()
    out = mod.compute_indicators(df)
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(out.columns)
    close = df["close"].to_numpy()
    assert out["pct"].iloc[3] == pytest.approx((close[3] / close[2] - 1) * 100)
    assert out["vol_ratio"].iloc[30] == pytest.approx(100.0 / 100.0)
    assert out["lu"].dtype == bool
    assert out["pos60"].dropna().between(0, 1).all()


def test_compute_indicators_has_pattern_columns():
    """形态列的 dtype 契约：bool 列不能被 NaN 退化成 object，否则 to_parquet 直接报错。"""
    mod = _load_script()
    out = mod.compute_indicators(_df(n=120))
    assert set(mod.PATTERN_COLUMNS) <= set(out.columns)
    assert out["vol_trend"].dtype == float
    assert out["neckline_sup"].dtype == float
    for col in ("hammer_low", "w_bottom_break", "vol_expanding", "pullback_stabilize"):
        assert out[col].dtype == bool, col


def test_recompute_backfills_pattern_columns(tmp_path, monkeypatch):
    """老 parquet 缺新列时，--recompute 走的路径要能就地补齐，不必重回 MongoDB 全量重建。"""
    mod = _load_script()
    old = mod.compute_indicators(_df(n=120)).drop(columns=mod.PATTERN_COLUMNS)
    assert "hammer_low" not in old.columns
    path = tmp_path / "603000.parquet"
    old.to_parquet(path, index=False)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)

    rewritten, missing = mod.recompute_existing()
    assert (rewritten, missing) == (1, 0)
    back = pd.read_parquet(path)
    assert set(mod.PATTERN_COLUMNS) <= set(back.columns)
    assert len(back) == len(old)
    assert np.allclose(back["close"].to_numpy(), old["close"].to_numpy())


def test_is_fund_code():
    mod = _load_script()
    assert mod.is_fund_code("159599")          # 深市 ETF
    assert mod.is_fund_code("513310")          # 沪市跨境 ETF
    assert mod.is_fund_code("501001")          # 沪市 LOF
    assert not mod.is_fund_code("600519")
    assert not mod.is_fund_code("000001")
    assert not mod.is_fund_code("399001")      # 指数不是基金


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, _key, _direction):
        return self.docs


class _Col:
    """假 stock_kline 集合：只实现脚本真正用到的 find/aggregate。"""

    def __init__(self, docs, codes):
        self.docs = docs
        self.codes = codes

    def find(self, query, _proj):
        return _Cursor([d for d in self.docs if d["code"] == query["code"]])

    def aggregate(self, _pipeline, **_kw):
        return [{"_id": c} for c in self.codes]


class _Client:
    def __init__(self, col):
        self.col = col

    def __getitem__(self, _database):
        return {"stock_kline": self.col}


def _kline_docs(code, closes):
    return [
        {"code": code, "date": f"2026-01-{i + 1:02d} 15:00", "open": c, "high": c * 1.01,
         "low": c * 0.99, "close": c, "volume": 1000.0, "amount": 0.0}
        for i, c in enumerate(closes)
    ]


def test_collect_fund_codes_keeps_only_fund_prefixes():
    mod = _load_script()
    client = _Client(_Col([], ["159599", "513310", "600519", "399001", "bad", "1595"]))
    assert mod.collect_fund_codes(client, "db") == ["159599", "513310"]


def test_process_code_neutralizes_limit_up_for_funds(tmp_path, monkeypatch):
    """ETF 没有板块涨停线，lu 必须恒 False，否则拿涨停规则跑 ETF 回测会读出假涨停。"""
    mod = _load_script()
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)
    spikes = [10.0] * 12 + [11.2]          # 最后一根 +12%，股票口径会判成涨停
    for code in ("159599", "600519"):
        client = _Client(_Col(_kline_docs(code, spikes), []))
        assert mod.process_code(client, "db", code, code, True) is True
        lu = pd.read_parquet(tmp_path / f"{code}.parquet")["lu"]
        if code == "159599":
            assert not lu.any()
        else:
            assert lu.any()


def test_collect_fund_names_batches_and_tolerates_failure(monkeypatch):
    mod = _load_script()
    urls = []

    class _Resp:
        content = 'v_sz159599="51~芯片ETF东财~159599~2.556~";v_sh513310="51~中韩半导体~513310~4.70~";'.encode("gbk")

    def fake_get(url, timeout=None, **kw):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    codes = ["159599", "513310"]
    got = mod.collect_fund_names(codes)
    assert got == {"159599": "芯片ETF东财", "513310": "中韩半导体"}
    assert "sz159599" in urls[0] and "sh513310" in urls[0]      # 5 字头归沪市、1 字头归深市

    def boom(url, timeout=None, **kw):
        raise OSError("行情源挂了")

    monkeypatch.setattr(mod.requests, "get", boom)
    assert mod.collect_fund_names(codes) == {}                  # 名字拿不到就退回代码，不该炸整轮构建
