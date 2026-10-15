import importlib.util

SCRIPT = "scripts/fix_star_volume_unit.py"


def _load():
    spec = importlib.util.spec_from_file_location("fix_star_volume_unit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(_id=1, day="2026-08-03 15:00", volume=10_000_000, close=50.0,
         amount=5.0e8):
    """默认这行贴着「股」：额/(量×价)=1.0。"""
    return {"_id": _id, "date": day, "volume": volume, "close": close,
            "amount": amount}


def test_classify_uses_amount_ratio_not_source():
    mod = _load()
    assert mod.classify(5.0e8, 10_000_000, 50.0) == "shares"     # 比值 1.0
    assert mod.classify(5.0e8, 100_000, 50.0) == "lot"           # 比值 100
    assert mod.classify(None, 10_000_000, 50.0) == "unknown"     # 没有成交额
    assert mod.classify(5.0e8, 0, 50.0) == "skip"                # 停牌：无量可折
    assert mod.classify(2000.0, 10_000_000, 50.0) == "unknown"   # 0.004=指数，带外不乱猜


def test_build_ops_only_touches_share_rows():
    mod = _load()
    from collections import defaultdict
    tally = defaultdict(lambda: {"lot": 0, "shares": 0})
    unknowns = defaultdict(list)
    stat = defaultdict(int)
    rows = [("688001", "tencent", _doc(1)),
            ("688001", "tencent", _doc(2, day="2026-08-04 15:00")),
            ("688001", "sina", _doc(3, volume=100_000)),          # 已是手
            ("688001", "sina", _doc(4, volume=0)),                # 停牌
            ("688001", "sina", _doc(5, amount=None))]             # 判不了
    ops = mod.build_ops(rows, tally, unknowns, stat)
    assert [o._filter["_id"] for o in ops] == [1, 2]
    assert tally[("688001", "tencent")]["shares"] == 2
    assert tally[("688001", "sina")]["lot"] == 1
    assert stat["zero"] == 1
    assert unknowns[("688001", "sina")] == [(5, 10_000_000)]


def test_make_op_keeps_raw_and_matches_old_volume():
    mod = _load()
    op = mod.make_op(7, 12_388_324, "688001", "2026-10-09", "tencent")
    assert op._filter == {"_id": 7, "volume": 12_388_324}   # 原值变了就失配，重跑不会二次折算
    assert op._doc["$set"] == {"volume": 123_883, "volume_raw": 12_388_324,
                               "vol_unit": "lot"}


def test_converted_row_reads_as_lot_afterwards():
    mod = _load()
    op = mod.make_op(1, 10_000_000, "688001", "2026-08-03", "tencent")
    new = op._doc["$set"]["volume"]
    assert mod.classify(5.0e8, new, 50.0) == "lot"          # 幂等：第二轮自然跳过


def test_unknown_rows_follow_the_same_source_verdict():
    mod = _load()
    from collections import defaultdict
    # 689 这种 CDR：腾讯不给成交额，但同 (代码,来源) 的可判定行全体是「股」
    tally = {("689009", "tencent"): {"lot": 0, "shares": 2},
             ("688001", "sina"): {"lot": 5, "shares": 0},
             ("688002", "sina"): {"lot": 3, "shares": 4}}
    unknowns = {("689009", "tencent"): [(11, 6_435_987)],
                ("688001", "sina"): [(12, 1000)],
                ("688002", "sina"): [(13, 1000)]}
    ops = mod.resolve_unknowns(unknowns, tally)
    assert [o._filter["_id"] for o in ops] == [11]     # 全体一致才动；结论不一致就放着


def test_star_code_regex_covers_cdr():
    mod = _load()
    assert mod.STAR_PREFIXES == ("688", "689")
