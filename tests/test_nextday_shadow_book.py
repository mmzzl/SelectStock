"""影子盘的「一本账 = 一个臂」：换模型跑必须落到自己的账本，否则两臂的行会互相盖掉。

起因：`--model-dir` 早就有，但 `LEDGER` 是模块级写死的那一份 ⇒ 拿 W3 跑 `--list` 会把 W1 的账本整本重写
（`write_ledger` 是覆盖式落盘，行里又没有模型标识）。这两条测试先立住"账本可分"这件事。
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qlib_nextday_shadow.py"


@pytest.fixture(scope="module")
def sh():
    spec = importlib.util.spec_from_file_location("qlib_nextday_shadow_for_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.set_book(SCRIPT.parent.parent / "data" / "qlib" / "nextday_shadow")


ROW_W1 = {"signal_date": "2026-10-28", "instrument": "SZ000001", "rank": 1, "run_id": "batchWx_c1"}
ROW_W3 = {"signal_date": "2026-10-28", "instrument": "SZ000002", "rank": 1, "run_id": "batchW_c3"}


def test_each_model_dir_gets_its_own_book(sh, tmp_path):
    sh.set_book(tmp_path / "w3")
    sh.write_ledger([ROW_W3])
    assert (tmp_path / "w3" / "ledger.jsonl").exists()
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchW_c3"]

    sh.set_book(tmp_path / "w1")
    sh.write_ledger([ROW_W1])
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchWx_c1"]
    sh.set_book(tmp_path / "w3")
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchW_c3"]


def test_default_book_is_untouched_by_a_second_arm(sh, tmp_path, monkeypatch):
    default = sh.LEDGER
    before = default.read_bytes() if default.exists() else None
    sh.set_book(tmp_path / "w3")
    sh.write_ledger([ROW_W3])
    after = default.read_bytes() if default.exists() else None
    assert after == before, "换账本不该动线上那份 nextday_shadow/ledger.jsonl"


def test_cli_takes_the_book_path(sh, tmp_path, capsys):
    assert sh.main(["--out-dir", str(tmp_path / "w3"), "--report"]) == 0
    assert "账是空的" in capsys.readouterr().out
    assert not (tmp_path / "w3").exists(), "只读的一次 --report 不该把目录建出来"


def test_each_row_says_which_model_wrote_it(sh, tmp_path):
    sh.set_book(tmp_path / "w3", run_id="batchW_c3")
    sh.write_ledger([{**ROW_W3, "run_id": None}])
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchW_c3"]


def test_a_second_arm_cannot_silently_overwrite_another_book(sh, tmp_path):
    sh.set_book(tmp_path / "one", run_id="batchWx_c1")
    sh.write_ledger([ROW_W1])
    sh.set_book(tmp_path / "one", run_id="batchW_c3")
    with pytest.raises(RuntimeError, match="batchWx_c1"):
        sh.write_ledger([ROW_W3])
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchWx_c1"], "被拒的那次不许动原账"


def test_a_book_without_model_stamp_is_not_up_for_grabs(sh, tmp_path):
    """老账（§37.14.4 之前落的）行里没有 run_id ⇒ 不能证明是谁的 ⇒ 换臂一样得拒。"""
    sh.set_book(tmp_path / "legacy")
    sh.write_ledger([{**ROW_W1, "run_id": None}])
    sh.set_book(tmp_path / "legacy", run_id="batchW_c3")
    with pytest.raises(RuntimeError, match="没有模型标识"):
        sh.write_ledger([ROW_W3])
    assert len(sh.read_ledger()) == 1


def test_adopt_book_is_the_only_way_to_take_over_an_unstamped_book(sh, tmp_path):
    sh.set_book(tmp_path / "leg2")
    sh.write_ledger([{**ROW_W1, "run_id": None}])
    sh.set_book(tmp_path / "leg2", run_id="batchWx_c1", adopt=True)
    sh.write_ledger(sh.read_ledger() + [ROW_W3])
    assert [r["run_id"] for r in sh.read_ledger()] == ["batchWx_c1", "batchWx_c1"]
