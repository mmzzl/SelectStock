"""Alpha158Trend 扩展特征：训练与打分必须同一份列序（批次 N，2026-10-25）。"""
import pytest

from stock_selector_qlib.alpha import TREND_FACTORS, TREND_NAMES, Alpha158Trend


@pytest.fixture(scope="module")
def feature_names():
    import qlib

    qlib.init(provider_uri="/data/work/qlib_data/cn", kernels=1)
    handler = Alpha158Trend(
        instruments=["SH600000"], start_time="2026-09-01", end_time="2026-09-10",
        label=["Ref($close,-2)/Ref($close,-1)-1"], fit_start_time="2026-09-01",
        fit_end_time="2026-09-10", infer_processors=[], learn_processors=[])
    fields, names = handler.get_feature_config()
    return names


def test_config_extends_alpha158_with_trend_block(feature_names):
    assert len(feature_names) == 158 + len(TREND_NAMES)
    assert feature_names[-len(TREND_NAMES):] == TREND_NAMES


def test_trend_names_unique_and_carry_prefix():
    names = [name for name, _ in TREND_FACTORS]
    assert len(set(names)) == len(names) == 8
    assert all(name.startswith("tf_") for name in names)


def test_trend_exprs_only_use_whitelisted_ops():
    import re

    allowed = {"Mean", "Ref", "Sub", "Div", "Max", "close", "volume", "amount"}
    for _, expr in TREND_FACTORS:
        tokens = re.split(r"[\s(),]", expr)
        bad = [t for t in tokens if t and t.lstrip("$") not in allowed and not t.isdigit()]
        assert not bad, (expr, bad)
