"""Alpha158Style / Alpha158Full 扩展特征：列序守护（批次 O，2026-10-25）。"""
import re

import pytest

from stock_selector_qlib.alpha import TREND_NAMES
from stock_selector_qlib.alpha_style import STYLE_FACTORS, STYLE_NAMES, Alpha158Full, Alpha158Style


@pytest.fixture(scope="module")
def configs():
    import qlib

    qlib.init(provider_uri="/data/work/qlib_data/cn", kernels=1)
    kw = dict(instruments=["SH600000"], start_time="2026-09-01", end_time="2026-09-10",
              label=["Ref($close,-2)/Ref($close,-1)-1"], fit_start_time="2026-09-01",
              fit_end_time="2026-09-10", infer_processors=[], learn_processors=[])
    s_fields, s_names = Alpha158Style(**kw).get_feature_config()
    f_fields, f_names = Alpha158Full(**kw).get_feature_config()
    return s_names, f_names


def test_style_config_extends_alpha158(configs):
    s_names, _ = configs
    assert len(s_names) == 158 + len(STYLE_NAMES)
    assert s_names[-len(STYLE_NAMES):] == STYLE_NAMES


def test_full_config_is_superset(configs):
    _, f_names = configs
    assert len(f_names) == 175
    assert set(STYLE_NAMES) | set(TREND_NAMES) <= set(f_names)
    assert f_names[-len(STYLE_NAMES):] == STYLE_NAMES


def test_style_names_unique_and_carry_prefix():
    names = [name for name, _ in STYLE_FACTORS]
    assert len(set(names)) == len(names) == len(STYLE_NAMES)
    assert all(name.startswith("so_") for name in names)


def test_style_exprs_only_use_whitelisted_ops():
    allowed = {"Mean", "Ref", "Sub", "Div", "Max", "Min", "EMA", "Std", "Skew", "Abs",
               "If", "Gt", "Lt", "close", "open", "high", "low", "volume", "amount"}
    for _, expr in STYLE_FACTORS:
        tokens = re.split(r"[\s(),]", expr)
        bad = [t for t in tokens if t and t.lstrip("$") not in allowed and not t.isdigit()]
        assert not bad, (expr, bad)
