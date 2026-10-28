"""backfill_kline_5m_amount 的纯函数件：报文解析 / 量纲判定 / 只补空额的写库选择。

网络件不测（Cookie 播种、RST 重试在实盘轮里验）；这里钉的是三条会静默出脏数的口：
1) parse_payload 的时间带秒必须截到分（库里 date 只到分，对不上就一根都更新不上）；
2) 量纲判定必须收「手」与「股」两个带、拒 ×1e4 那种看不懂的（杀了它，51 万 vs 51 亿的距离）；
3) write_amount 只对 amount==0 的行出 $set——库里已有值（快照、东财）绝不能被本轮盖掉，
   且库里没有的 bar 不落 upsert。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backfill_kline_5m_amount", ROOT / "scripts" / "backfill_kline_5m_amount.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_kline_5m_amount"] = mod
_spec.loader.exec_module(mod)


def mkbar(d, o, cl, h, l, vol, amt):
    return f'"{d},{o},{cl},{h},{l},{vol},{amt}"'


def test_parse_payload_truncates_seconds_and_reads_amount():
    text = ('{"data":{"code":"0.000001","klines":['
            + mkbar("2026-10-28 09:35:00", "10.00", "10.10", "10.11", "9.99", "1234", "84500.0")
            + ']}}')
    code, bars = mod.parse_payload(text)
    assert code == "0.000001"
    assert bars == [("2026-10-28 09:35", 84500.0, 1234.0, 10.10)]


def test_parse_payload_no_data_is_empty_not_crash():
    assert mod.parse_payload('{"rc":0,"data":null}') == ("", [])


def test_scale_ok_accepts_hand_and_share_units_rejects_wan():
    # 手 vs 股 的分水岭是 100 倍；带宽 4 倍。×1=手、×0.01=股、×1e4 找不到带 ⇒ 拒。
    bars_hand = [("d", 10_100.0, 1_000.0, 10.10)]
    bars_share = [("d", 101.0, 1_000.0, 10.10)]
    bars_wan = [("d", 101_000_000.0, 1_000.0, 10.10)]
    assert mod.scale_ok(bars_hand) == "yuan"
    assert mod.scale_ok(bars_share) == "shares"
    assert mod.scale_ok(bars_wan) is None
    assert mod.scale_ok([("d", 0.0, 0.0, 10.10)]) is None


def test_write_amount_touches_only_zero_amount_rows():
    class FakeColl:
        def __init__(self): self.ops = []
        def bulk_write(self, ops, ordered=False):
            self.ops = list(ops)
            class R: modified_count = len(ops); upserted_count = 0
            return R()

    fake = FakeColl()
    bars = [("2026-10-28 09:35", 0.0, 1000.0, 10.10),      # EM 给 0（停牌段）→ 不写
            ("2026-10-28 09:40", 88_000.0, 1000.0, 10.10)]
    n = mod.write_amount(fake, "000001", bars)
    assert n == 1 and len(fake.ops) == 1
    op = fake.ops[0]
    d = op._doc["$set"]
    # 过滤器钉住 amount==0.0，且只 set 金额这一个字段
    assert op._filter == {"code": "000001", "date": "2026-10-28 09:40", "amount": 0.0}
    assert d == {"amount": 88000.0}


def test_secid_mapping_and_lmt_fits_request():
    assert mod.secid_of("000001") == "0.000001"
    assert mod.secid_of("600001") == "1.600001"
    assert mod.secid_of("833533") == "0.833533"
