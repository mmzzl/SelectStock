"""tests/conftest.py — sys.path 注入 + 环境隔离。

- 注入 plugin/ 与仓库根目录（portfolio / stock_selector 可导入）
- 每个测试：PLUGIN_KLINE_DIR 指向空 tmp（不读真实 5186 只 parquet），
  PLUGIN_RULES_FILE 指向不存在路径（默认规则，测试可再 monkeypatch 覆盖），
  两份后验产物指向合成表（见 `posterior_file`）——线上文件每天都在变，测试不能靠它。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]

for _p in (str(_PLUGIN_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


@pytest.fixture(scope="session")
def posterior_file(tmp_path_factory):
    """合成一张判定台账后验表，只服务一件事：**测试不许读 data/judgement_ledger 的线上台账**。

    那张 parquet 每天 19:00 全市场重算，文案数字跟着规则集变（2026-10-17 停用形态卖出规则后
    「20 日内自行转卖」那一列直接是空）。用例要是直读它，同一份代码明天就跑出另一种文案，
    那不是断言，是撞运气。这里给的是固定值，`tests/test_posterior.py` 那种自带文件的照旧覆盖。
    """
    import pandas as pd

    def row(action):
        return {"action": action, "heat_bin": "全部", "n": 8_969_490,
                "up_rate_posterior": 0.475, "ci_low": 0.4745, "ci_high": 0.4755,
                "ci_width_pp": 0.1, "credible": True,
                "fwd10_mean_pct": 0.09, "fwd10_median_pct": -0.24, "fwd10_p5_pct": -12.74,
                "fwd20_mean_pct": 0.15, "fwd20_p5_pct": -17.9, "flip_sell_20_pct": 46.1,
                "era_fwd_mean_pct": "-0.10,-0.13,+0.50", "era_sign_stable": False,
                "daymean_fwd10_pct": -0.14, "days": 900,
                "vs_hold_daymean_pp": -0.47, "vs_hold_t": -3.1, "vs_hold_days": 120,
                "vs_hold_era_stable": True}

    path = tmp_path_factory.mktemp("posterior") / "posterior.parquet"
    pd.DataFrame([row(a) for a in ("hold", "sell", "add")]).to_parquet(path, index=False)
    return path


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch, posterior_file):
    monkeypatch.setenv("PLUGIN_KLINE_DIR", str(tmp_path / "kline"))
    monkeypatch.setenv("PLUGIN_RULES_FILE", str(tmp_path / "no_rules.json"))
    monkeypatch.setenv("POSTERIOR_FILE", str(posterior_file))
    # 规则集回放的产物只有点过「应用」之后才有：缺文件时文案走「还没跑过回放」那条退化路径
    monkeypatch.setenv("RULESET_POSTERIOR_FILE", str(tmp_path / "no_replay.json"))
    # 实时价有 30 秒进程内缓存：不清掉的话，上一个用例抓到的假价会漏进下一个用例
    from portfolio import posterior as posterior_mod
    from portfolio import pricing

    posterior_mod._cache, posterior_mod._cache_key = None, ()
    pricing._live_cache.clear()
