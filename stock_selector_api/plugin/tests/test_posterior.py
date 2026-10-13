"""后验表直读：诊断文案里的可信度数字来自台账，不来自写死的常数。"""
import pandas as pd

from portfolio import posterior


def _write(tmp_path, rows):
    path = tmp_path / "posterior.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _row(action="hold", **kw):
    base = {
        "action": action, "heat_bin": "全部", "n": 8_969_490,
        "up_rate_posterior": 0.47566, "ci_low": 0.47533, "ci_high": 0.47599,
        "ci_width_pp": 0.07, "credible": True,
        "fwd10_mean_pct": 0.09, "fwd10_median_pct": -0.239, "fwd10_p5_pct": -12.74,
        "fwd20_mean_pct": 0.15, "fwd20_p5_pct": -17.5, "flip_sell_20_pct": 46.11,
        "era_fwd_mean_pct": "-0.10,-0.13,+0.50", "era_sign_stable": False,
    }
    base.update(kw)
    return base


def test_lookup_reads_all_bin_row(tmp_path, monkeypatch):
    path = _write(tmp_path, [_row(), _row(heat_bin="0-44", n=1_632_552)])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    row = posterior.lookup("hold")
    assert row["n"] == 8_969_490
    assert row["up_pct"] == 47.57
    assert row["flip_sell_20_pct"] == 46.11
    assert row["fwd20_p5_pct"] == -17.5
    assert row["era_sign_stable"] is False


def test_lookup_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTERIOR_FILE", str(tmp_path / "nope.parquet"))
    posterior._cache, posterior._cache_key = None, ()
    assert posterior.lookup("hold") is None


def test_conviction_line_uses_ledger_not_constants(tmp_path, monkeypatch):
    """台账里 46.11% 时文案必须跟着变——常数 45.3 只是台账缺席时的退化值。"""
    from portfolio.analyzer import _conviction_line

    path = _write(tmp_path, [_row(flip_sell_20_pct=51.0, fwd20_p5_pct=-20.0,
                                  era_sign_stable=True)])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    text = _conviction_line({"latest_price": 10.0, "action": "hold",
                             "sell_trigger_price": 9.6}, 12.0, 1000)
    assert "20 日内 51% 自行转卖" in text
    assert "p5 -20.0%≈再亏 2,000 元" in text
    assert "45.3" not in text and "样本不足" not in text


def test_conviction_line_marks_narrow_sample(tmp_path, monkeypatch):
    from portfolio.analyzer import _conviction_line

    path = _write(tmp_path, [_row(n=120, credible=False, ci_width_pp=9.4)])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    text = _conviction_line({"latest_price": 10.0, "action": "hold",
                             "sell_trigger_price": None}, 10.0, 100)
    assert "样本不足(宽9.4pp)" in text


def test_lookup_exposes_day_weighted_diff(tmp_path, monkeypatch):
    """日等权 + 同日 hold 差分必须能读出来；缺值（hold 自己那格）要成 None/False，不能是 NaN/True。"""
    path = _write(tmp_path, [
        _row("sell", daymean_fwd10_pct=-0.14, days=3097,
             vs_hold_daymean_pp=-0.472, vs_hold_t=-6.7,
             vs_hold_pos_rate=0.4188, vs_hold_days=3049, vs_hold_era_stable=True),
        _row(era_sign_stable=True)])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    sell = posterior.lookup("sell")
    assert (sell["vs_hold_pp"], sell["vs_hold_t"], sell["vs_hold_days"]) == (-0.472, -6.7, 3049.0)
    assert sell["vs_hold_era_stable"] is True
    hold = posterior.lookup("hold")
    assert hold["vs_hold_pp"] is None and hold["vs_hold_era_stable"] is False


def test_sell_line_shows_paired_diff_not_point_mean(tmp_path, monkeypatch):
    """sell 文案只印配对差分：同一格里点级均值是 +0.87%、差分是 −0.47pp，符号相反。

    印后者是因为判据在暴跌/反弹日扎堆，点级均值会把一条卖出规则夸成买入理由。
    """
    from portfolio.analyzer import _conviction_line

    path = _write(tmp_path, [_row("sell", fwd10_mean_pct=0.874,
                                  daymean_fwd10_pct=-0.14,
                                  vs_hold_daymean_pp=-0.472, vs_hold_t=-6.7,
                                  vs_hold_days=3049, vs_hold_era_stable=True)])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    text = _conviction_line({"latest_price": 10.0, "action": "sell",
                             "sell_trigger_price": 9.6}, 9.0, 100)
    assert "后 10 日比同日 hold -0.47pp·三段同号(配对3049日 t=-6.7)" in text
    assert "+0.87%" not in text


def test_cross_hold_line_falls_back_on_old_ledger(tmp_path, monkeypatch):
    """旧台账没有差分列时退化到上涨频率，而不是印 nan 或干脆缺这一行。"""
    from portfolio.analyzer import _conviction_line

    path = _write(tmp_path, [_row("add")])
    monkeypatch.setenv("POSTERIOR_FILE", str(path))
    posterior._cache, posterior._cache_key = None, ()
    text = _conviction_line({"latest_price": 10.0, "action": "add",
                             "sell_trigger_price": None}, 10.0, 100)
    assert "同类判据后 10 日上涨" in text and "nan" not in text
