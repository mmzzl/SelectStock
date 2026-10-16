"""候选判据门槛（rule_gates）单元测试。

每道筛都要各打回一次：如果一条「看起来对」的候选能随便绕过某道筛，那道筛就没有信息量，
`出场 0/14` 那次的教训就是这个。
"""

import json

import pytest


def good_stat(side="sell"):
    """一条按四道筛都该过的读数：方向对、t 硬、三段同号、扣成本后仍为负、量够。"""
    if side == "sell":
        return {"n": 250_000, "days": 1800, "flat_pct": 27.73, "base_pct": 35.25,
                "lift": 0.787, "vs_base_t": -30.2, "exc20_pp": -0.85,
                "era_flat_diff_pp": "-0.90,-0.70,-0.80"}
    return {"n": 250_000, "days": 1800, "flat_pct": 42.0, "base_pct": 35.25,
            "lift": 1.19, "vs_base_t": 12.4, "exc20_pp": 0.90,
            "era_flat_diff_pp": "+0.80,+0.90,+0.70"}


def verdict(stat=None, side="sell", trigger_days_per_year=8.0):
    from stock_selector.backtest.rule_gates import evaluate_state

    return evaluate_state(stat if stat is not None else good_stat(side), side,
                          trigger_days_per_year)


class TestSampleGate:
    def test_pass_case_clears_every_gate(self):
        out = verdict()
        assert out["verdict"] == "过", out["failed"]

    def test_add_direction_passes(self):
        assert verdict(side="add")["verdict"] == "过"

    def test_too_few_points_is_failed_by_sample_gate(self):
        stat = good_stat() | {"n": 9_999}
        out = verdict(stat)
        assert out["verdict"] == "不过"
        assert "sample" in out["failed"]

    def test_too_few_days_is_failed_by_sample_gate(self):
        stat = good_stat() | {"days": 199}
        assert "sample" in verdict(stat)["failed"]

    def test_missing_lift_means_no_verdict(self):
        """样本量够但配对读数没产出（基线状态在这些日子里没数）→ 说「不判」，别说「不过」。"""
        stat = {"n": 500_000, "days": 250, "flat_pct": 30.0}
        out = verdict(stat)
        assert out["verdict"] == "不判"
        assert "配对读数缺失" in out["note"]


class TestSignificanceGate:
    def test_weak_t_is_rejected(self):
        """|t|=2 在上千万样本里毫无意义：多重比较的 Bonferroni 兜底要 4。"""
        stat = good_stat() | {"vs_base_t": -2.5}
        assert "t" in verdict(stat)["failed"]

    def test_boundary_t_passes(self):
        assert verdict(good_stat() | {"vs_base_t": -4.0})["verdict"] == "过"

    def test_wrong_sign_t_is_rejected(self):
        """t 很大但方向反（卖出判据圈中的点后来比全体更好）必须打回。"""
        stat = good_stat() | {"vs_base_t": 30.2}
        assert "t" in verdict(stat)["failed"]


class TestDirectionGate:
    def test_sell_with_lift_above_one_is_rejected(self):
        stat = good_stat() | {"lift": 1.208, "flat_pct": 42.47}
        out = verdict(stat)
        assert "direction" in out["failed"]

    def test_add_with_lift_below_one_is_rejected(self):
        stat = good_stat("add") | {"lift": 0.916, "flat_pct": 32.83}
        assert "direction" in verdict(stat, side="add")["failed"]


class TestEraGate:
    def test_one_era_flipping_is_rejected(self):
        stat = good_stat() | {"era_flat_diff_pp": "-0.90,+0.10,-0.80"}
        assert "era" in verdict(stat)["failed"]

    def test_nan_era_is_rejected(self):
        """某一段样本不足就留 nan：宁可不判，也别让两段带着第三段的空位算「同号」。"""
        stat = good_stat() | {"era_flat_diff_pp": "-0.90,nan,-0.80"}
        assert "era" in verdict(stat)["failed"]


class TestCostGate:
    def test_effect_smaller_than_round_trip_is_rejected(self):
        from stock_selector.backtest.rule_gates import ROUND_TRIP_PP

        assert ROUND_TRIP_PP == pytest.approx(0.35)
        stat = good_stat() | {"exc20_pp": -0.20}
        assert "cost" in verdict(stat)["failed"]

    def test_exc_is_measured_against_the_right_direction(self):
        """卖出判据的 exc20_pp 必须为负（触发点后来更差）才算钱，为正就是白付换手。"""
        assert "cost" in verdict(good_stat() | {"exc20_pp": 0.90})["failed"]


class TestFrequencyGate:
    def test_triggers_more_than_30_days_per_year_is_rejected(self):
        """A 股 T+1、资金有上限：一年响 30 天以上的退出规则执行不了，赢比再高也是纸面数。"""
        out = verdict(trigger_days_per_year=30.1)
        assert "freq" in out["failed"]

    def test_boundary_frequency_passes(self):
        assert verdict(trigger_days_per_year=30.0)["verdict"] == "过"


class TestLoadCandidates:
    def _write(self, tmp_path, payload):
        p = tmp_path / "candidates.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_loads_valid_candidates_with_fields(self, tmp_path):
        from stock_selector.backtest.rule_gates import load_candidates

        p = self._write(tmp_path, {"candidates": [
            {"id": "ma10", "side": "sell", "label": "跌破10日线5%",
             "expr": "close <= ma10 * 0.95",
             "why": "均线跌破说明短期承接盘消失，趋势先行的资金已经在撤"}]})
        out = load_candidates(p)
        assert out[0]["side"] == "sell"
        assert set(out[0]["fields"]) >= {"close", "ma10"}

    def test_missing_mechanism_is_rejected(self, tmp_path):
        """「先看出哪条字段组合好看」没有理由，正是我们要挡的搜索器行为。"""
        from stock_selector.backtest.rule_gates import load_candidates

        p = self._write(tmp_path, {"candidates": [
            {"id": "x", "side": "sell", "expr": "rsi14 > 90", "why": "rsi高"}]})
        with pytest.raises(ValueError, match="机制"):
            load_candidates(p)

    def test_bad_side_is_rejected(self, tmp_path):
        from stock_selector.backtest.rule_gates import load_candidates

        p = self._write(tmp_path, {"candidates": [
            {"id": "x", "side": "hold", "expr": "rsi14 > 90",
             "why": "超买之后均值回归，兑现这一段涨幅"}]})
        with pytest.raises(ValueError, match="side"):
            load_candidates(p)

    def test_illegal_expression_is_rejected(self, tmp_path):
        from stock_selector.backtest.rule_gates import load_candidates

        p = self._write(tmp_path, {"candidates": [
            {"id": "x", "side": "sell", "expr": "close == ma10",
             "why": "收盘价等于均线的时候趋势已经走完"}]})
        with pytest.raises(ValueError, match="表达式"):
            load_candidates(p)

    def test_candidate_count_cap_is_enforced(self, tmp_path):
        """候选数上限 = 多重比较的预算：几百条一起跑，总有几条「白捡的显著」。"""
        from stock_selector.backtest.rule_gates import MAX_CANDIDATES, load_candidates

        cands = [{"id": f"c{i}", "side": "sell", "expr": f"rsi14 > {50 + i}",
                  "why": "超买兑现：涨太快之后短期买盘耗尽，价格向均值回归"}
                 for i in range(MAX_CANDIDATES + 1)]
        p = self._write(tmp_path, {"candidates": cands})
        with pytest.raises(ValueError, match="候选"):
            load_candidates(p)
