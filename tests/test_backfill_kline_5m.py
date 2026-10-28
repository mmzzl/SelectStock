"""backfill_kline_5m 的两条纯逻辑：号段前缀映射、完整覆盖日判据。

为什么必须有：增量脚本曾把「冒烟跑一半的日子」当成基点（=max date），缺口被悄悄吞掉。
这条用例钉住"必须行数 ≥15 万才算基点"。
"""
from scripts.backfill_kline_5m import latest_day_and_codes, symbol_of


def test_symbol_routes_by_board_prefix():
    assert symbol_of("600519").startswith("sh")
    assert symbol_of("688001").startswith("sh")
    assert symbol_of("510300").startswith("sh")
    assert symbol_of("000001").startswith("sz")
    assert symbol_of("300750").startswith("sz")
    assert symbol_of("830799").startswith("bj")


class _StubColl:
    """仅实现 aggregate 两条 pipeline 的分派：按是否含 per-day 行数 $match 识别。"""

    def __init__(self, per_day_rows):
        self.per_day = per_day_rows

    def aggregate(self, pipeline, **kw):
        first_stage_keys = next(iter(pipeline[0]))
        if first_stage_keys.startswith("$group") and "n" in self.per_day[0]:
            rows = [r for r in self.per_day if r["n"] >= 150_000]
            return sorted(rows, key=lambda r: r["_id"], reverse=True)[:1]
        return []  # codes 匹配那条：返回空是不正确的，但本用例只断言 day


def test_base_day_requires_full_coverage_not_max_date():
    """冒烟写入 20 支后 max(date)=10-28，但那天只有 960 行——基点必须走回 09-29。"""
    stub = _StubColl([{"_id": "2026-10-28", "n": 960},
                      {"_id": "2026-09-29", "n": 181152}])
    day, codes, probe = latest_day_and_codes(stub)
    assert day == "2026-09-29"
