"""回测参数白名单校验：后端唯一允许的「计算」就是把前端参数收敛成内核 Namespace。"""
import pytest

from core.backtest_params import DEFAULTS, form_spec, merge, validate
from core.exceptions import BizError


def test_validate_rejects_unknown_keys():
    with pytest.raises(BizError):
        validate({"start": "2026-01-01", "end": "2026-02-01", "capitalization": 1})


def test_validate_requires_iso_dates():
    with pytest.raises(BizError):
        validate({"start": "20260101"})


def test_validate_start_before_end():
    with pytest.raises(BizError):
        validate({"start": "2026-03-01", "end": "2026-03-01"})


def test_validate_rejects_target_amount_at_position_cap():
    """单笔金额 ≥ 初始资金×单票上限时，净值一回撤就再也买不进，回测会静默空跑。"""
    with pytest.raises(BizError):
        validate({"capital": 100000, "position_cap_pct": 0.2, "target_amount": 20000})


def test_validate_defaults_are_self_consistent():
    """页面不填任何字段（提交空 params）也要能跑，否则一点「发起回测」就 400。

    注意这里喂的是 `{}` 而不是 DEFAULTS：DEFAULTS 里那些 qlib 不读的键（max_hold_days=10 等）
    是内核的缺省值，不是用户提交的内容；把它们当提交喂进 validate 会被引擎白名单拒掉，
    那正是「填了 qlib 不实现的参数」该有的反应。
    """
    merged = merge(validate({}))
    assert merged["target_amount"] < merged["capital"] * merged["position_cap_pct"]
    assert merged["start"] < merged["end"]
    # 只切引擎、其余全缺省：qlib 缺省即 2026-10-25 扫描的甜点臂（topk=20 / band 出榜 300）
    qlib = merge(validate({"engine": "qlib"}))
    assert qlib["hold_rule"] == "band" and qlib["topk"] == 20 and qlib["exit_rank"] == 300
    assert qlib["risk_degree"] == 0.35
    # 页面把缺省表单整包提交时 n_drop=5 也要能过；band 不读它
    assert merge(validate({"n_drop": DEFAULTS["n_drop"]}))["topk"] == 20


def test_validate_band_rule_params():
    """band 纪律：出榜必须不比进榜严、候选够填满仓位、不能带着 n_drop 冒充换过票。"""
    ok = validate({"engine": "qlib", "topk": 10, "hold_rule": "band",
                   "entry_rank": 50, "exit_rank": 100})
    assert ok["exit_rank"] == 100
    with pytest.raises(BizError):
        validate({"engine": "qlib", "hold_rule": "band", "entry_rank": 50, "exit_rank": 30})
    with pytest.raises(BizError):
        validate({"engine": "qlib", "hold_rule": "band", "topk": 30, "entry_rank": 20})
    with pytest.raises(BizError):
        validate({"engine": "qlib", "hold_rule": "band", "n_drop": 3})
    with pytest.raises(BizError):
        validate({"engine": "qlib", "hold_rule": "hold"})
    # dropout 才读 n_drop；qlib 的那几个键自研内核一条都不读（整包 SimpleNamespace 喂进去会静默失效）
    assert validate({"engine": "qlib", "hold_rule": "dropout", "n_drop": 3})["n_drop"] == 3
    for key in ("hold_rule", "entry_rank", "exit_rank", "topk", "n_drop", "benchmark",
                "risk_degree"):
        with pytest.raises(BizError):
            validate({"engine": "self", key: 10 if key != "benchmark" else "SH510300"})


def test_validate_coerces_numeric_strings_and_bounds():
    out = validate({"engine": "self", "universe": "40", "stop_loss_pct": "0.03", "capital": 50000,
                    "target_amount": 5000})
    assert out["universe"] == 40 and isinstance(out["universe"], int)
    assert out["stop_loss_pct"] == 0.03 and isinstance(out["stop_loss_pct"], float)
    with pytest.raises(BizError):
        validate({"stop_loss_pct": 0.9})
    with pytest.raises(BizError):
        validate({"capital": 1})
    # 只降初始资金、不调单笔金额 → 会撞单票上限，必须报错而不是产出空回测
    with pytest.raises(BizError):
        validate({"capital": 50000})


def test_validate_empty_strategy_means_rules_mode():
    assert validate({"strategy": ""})["strategy"] == ""


def test_merge_fills_defaults_and_ignores_unknown():
    merged = merge({"capital": 30000, "unknown": 1})
    assert merged["capital"] == 30000
    assert "unknown" not in merged
    assert merged["start"] == DEFAULTS["start"]


def test_validate_engine_switch():
    """引擎开关：缺省 qlib（最优组合只有它有）；喂进 qlib 不实现的止损要挡下来。"""
    assert merge(None)["engine"] == "qlib"
    assert validate({"engine": "self"})["engine"] == "self"
    assert validate({"engine": "qlib", "topk": 30, "n_drop": 5})["engine"] == "qlib"
    with pytest.raises(BizError, match="不支持这些参数"):
        validate({"engine": "qlib", "stop_loss_pct": 0.05})
    with pytest.raises(BizError, match="回测引擎只支持"):
        validate({"engine": "wrong"})


def test_form_spec_shape():
    spec = form_spec()
    assert spec["defaults"] == DEFAULTS
    assert spec["numeric"]["max_positions"]["type"] == "int"
    assert spec["numeric"]["capital"]["min"] == 1000.0
