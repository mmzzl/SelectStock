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
    """缺省值本身必须过校验，否则页面点「发起回测」不填任何字段就会直接 400。"""
    merged = merge(validate(DEFAULTS))
    assert merged["target_amount"] < merged["capital"] * merged["position_cap_pct"]
    assert merged["start"] < merged["end"]


def test_validate_coerces_numeric_strings_and_bounds():
    out = validate({"universe": "40", "stop_loss_pct": "0.03", "capital": 50000,
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


def test_form_spec_shape():
    spec = form_spec()
    assert spec["defaults"] == DEFAULTS
    assert spec["numeric"]["max_positions"]["type"] == "int"
    assert spec["numeric"]["capital"]["min"] == 1000.0
