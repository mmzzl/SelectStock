"""2026-10-29 用户拍板：涨跌停「买不进/卖不掉」这条限制从回测和选股里一并取消。

原话两条：「跌停买不进，回测和选股这条不适应，这个规则取消吧」「开盘涨停也取消呀」。
落到代码是三处，都要钉住：

1. 板后低吸的规则价不再有失效线（`api._dip_levels` 不再给 `buy_invalid_price`）；
2. 板后低吸标签的触发带去掉 0.905 下界（`qlib_run.LABEL_DIP_FMT`）——它和 1 同源，
   注释里写明「改一处必须同步另一处」，这里就是那一处的锁；
3. qlib 回测撮合不再挡涨跌停。第 3 处有个**静默空操作**的坑：qlib 的
   `Exchange.__init__`（qlib/backtest/exchange.py:139-140）看到 `limit_threshold is None`
   会回填 `C.limit_threshold`，而 region cn 在 `qlib/config.py:298` 把它设成 0.095——
   只把 EXCHANGE_KWARGS 里那一行删掉，撮合照旧挡单。所以必须真的改 C。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "stock_selector_api" / "plugin")):
    if p not in sys.path:
        sys.path.insert(0, p)

from stock_selector_qlib import api, backtest  # noqa: E402


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / "scripts" / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_板票行不再出跌停失效线():
    lv = api._dip_levels({"code": "002636", "price": 10.0}, 0.1001)
    assert lv["buy_ref_price"] == 9.5            # 低吸买线本身不动（他 10-28 定的入场）
    assert lv["buy_invalid_price"] is None       # 开盘跌停也照样挂单，不设失效线
    assert lv["sell_target_price"] is None
    assert "不设失效线" in lv["level_note"]
    assert "9.05" not in lv["level_note"]        # 旧失效价不许残留在文案里


def test_板策略详情不再声明跌停失效线():
    detail = api._BOARD_DETAIL
    assert "0.905" not in detail["description"]
    assert "0.905" not in detail["params"]["invalid_rule"]
    assert "不设失效线" in detail["params"]["invalid_rule"]


def test_板后低吸标签不再有_905_下界():
    qlib_run = _load_script("qlib_run")
    formula = qlib_run.label_formula(1, "dip")
    assert "0.905" not in formula                # 跌停那一刀取消
    assert "0.95" in formula                     # 低吸上界（低开 5% 起）仍在
    assert "0.0975" in formula                   # 「今天板」门仍在


def test_qlib_回测撮合不再有涨跌停挡单():
    assert "limit_threshold" not in backtest.EXCHANGE_KWARGS
    assert "不挡单" in backtest.LIMIT_NOTE
    assert "偏乐观" in backtest.LIMIT_NOTE       # 一字板按可成交算，这句必须随读数一起出去


def test_关掉挡单必须改掉_qlib_默认值而不是只删参数():
    """qlib 的回填只认 `C.limit_threshold`：Exchange 里传 None == 没传。"""
    from qlib.config import C

    C.limit_threshold = 0.095
    backtest.no_limit_blocking()
    assert C.limit_threshold is None


def test_脚本回测路径用的是同一份撮合参数():
    """两条回测路径不许各抄一份常量——抄了就是一处改、另一处静默留旧口径。"""
    qlib_run = _load_script("qlib_run")
    assert qlib_run.EXCHANGE_KWARGS is backtest.EXCHANGE_KWARGS
    assert "limit_threshold" not in qlib_run.EXCHANGE_KWARGS
