"""qlib_update_bin 的「日历对齐 ≠ 面板对齐」那一半。

背景（2026-11-02 实测）：面板日历 `calendars/day.txt` 已经推到 11-02，但 6,937 只票里
有 3,845 只的 `close.day.bin` 停在 10-16，`instruments/all.txt` 里它们的结束日也停在 10-16。
增量链每天照跑，却一只都没被追加进去——因为 `main()` 只比**日历**：

    missing = [d for d in cal if d > panel[-1]]

日历已经是 11-02 了，`missing` 是空，于是打印「无需更新」并 `return 0`，连 `fill_holes`
都不跑。半个月里 17:15 那一趟天天如此。

这一组用例把「必须用 bin 的实际长度再核一遍」钉住。

为什么冻结点自己解不开：`dump_update` 给一只票追加的行是「比 `instruments/all.txt` 里
它的结束日更晚」那几天（dump_bin.py `_dump_features`），而 all.txt 只在真有票被追加时
才整体重写。某只票的结束日一旦冻住，之后每次算出的待追加日都从冻结点起算，
只要窗口没覆盖到冻结点之后，它就永远等不到自己的那一天。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import qlib_update_bin as ub  # noqa: E402

CAL = [f"2026-10-{i:02d}" for i in range(1, 13)]   # 12 天，够看「落后几格」


def _panel(tmp_path: Path) -> Path:
    root = tmp_path / "cn"
    (root / "calendars").mkdir(parents=True)
    (root / "calendars" / "day.txt").write_text("\n".join(CAL) + "\n", encoding="utf-8")
    return root


def _bin(root: Path, sym: str, start: int, values: list[float]) -> None:
    """写一只票的 close.day.bin：首格 = 起始日历下标，之后一天一格。"""
    import numpy as np

    d = root / "features" / sym.lower()
    d.mkdir(parents=True, exist_ok=True)
    np.hstack([[start], np.array(values, dtype="<f")]).astype("<f").tofile(
        str(d / "close.day.bin"))


def test_bin_end_index_reads_the_last_covered_day(tmp_path):
    """首格是起始下标、文件长度是格数，两者相加减一就是这只票自己写到哪天。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * 5)          # 下标 0..4
    assert ub.bin_end_index(root / "features" / "sh600001" / "close.day.bin") == 4
    _bin(root, "SH600002", 7, [1.0] * 5)          # 下标 7..11（到日历末尾）
    assert ub.bin_end_index(root / "features" / "sh600002" / "close.day.bin") == 11


def test_bin_end_index_is_none_for_a_missing_file(tmp_path):
    """面板里没这只票（新股要走 dump_all/dump_fix），不能当成「落后」去造文件。"""
    root = _panel(tmp_path)
    assert ub.bin_end_index(root / "features" / "sh600009" / "close.day.bin") is None


def test_a_bin_at_the_calendar_tail_is_not_stale(tmp_path):
    root = _panel(tmp_path)
    _bin(root, "SH600001", 6, [1.0] * 6)          # 下标 6..11 = 日历末尾
    assert ub.stale_symbols(root, ["SH600001"], CAL) == []


def test_a_bin_behind_the_calendar_tail_is_stale(tmp_path):
    """这就是那 3,845 只的形状：日历已到 11-02，票还停在 10-16。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * 6)          # 停在 10-06
    assert ub.stale_symbols(root, ["SH600001"], CAL) == ["SH600001"]


def test_a_missing_feature_dir_is_skipped_not_reported(tmp_path):
    """面板里根本没目录的票交给 dump_all/dump_fix，这里不报「落后」（否则警告永远响）。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * 6)
    assert ub.stale_symbols(root, ["SH600001", "SH600009"], CAL) == ["SH600001"]


def test_tolerance_lets_a_one_day_lag_through(tmp_path):
    """tolerance 是给「当天刚收盘、还没轮到它」留的余量，不是用来掩盖多日落后。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * 11)         # 停在 10-11，差 1 格
    assert ub.stale_symbols(root, ["SH600001"], CAL, tolerance=0) == ["SH600001"]
    assert ub.stale_symbols(root, ["SH600001"], CAL, tolerance=1) == []


def test_stale_check_uses_close_so_a_missing_amount_column_is_not_staleness(tmp_path):
    """尺子是 close：amount/vwap 可能因为导出时缺字段而根本没建，那不是「落后」。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 6, [1.0] * 6)          # 只有 close，没有 amount.day.bin
    assert ub.stale_symbols(root, ["SH600001"], CAL) == []


def test_main_does_not_exit_early_when_only_the_panel_is_behind(tmp_path, monkeypatch, capsys):
    """核心回归：日历已经对齐、但名单里的票落后时，**不能**打印「无需更新」就退出 0。

    这是那半个月空转的直接原因。`mongo_db()` 在 `missing` 之前就被调用，所以这里让它
    正常返回一个「库内日历与面板一致」的假库（= 日历已对齐、`missing` 为空），
    让执行流真正走到那个退出判断；然后看输出里有没有「无需更新」。
    """
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * 6)          # 落后于日历尾部

    class _Col:
        def distinct(self, *a, **kw):
            return CAL

    class _Db:
        def __getitem__(self, _):
            return _Col()

    monkeypatch.setattr(ub, "panel_calendar", lambda d: CAL)
    monkeypatch.setattr(ub, "pool_symbols", lambda scope, symbols_file=None: ["SH600001"])
    monkeypatch.setattr(ub, "mongo_db", lambda: _Db())
    monkeypatch.setattr(sys, "argv", ["qlib_update_bin.py", "--qlib-dir", str(root),
                                      "--dry-run"])   # dry-run：验到「决定继续」为止，不写面板
    assert ub.main() == 0
    out = capsys.readouterr().out
    assert "无需更新" not in out                   # 旧代码在这里就 return 0 了
    assert "落后面板日历尾部" in out


def test_main_still_exits_when_everything_is_current(tmp_path, monkeypatch, capsys):
    """全都推到日历尾部、库里也没有更新的日子 ⇒ 照旧「无需更新」退出，不能变成天天全量重跑。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 6, [1.0] * 6)

    class _Col:
        def distinct(self, *a, **kw):
            return CAL                       # 库内日历与面板一致

    class _Db:
        def __getitem__(self, _):
            return _Col()

    monkeypatch.setattr(ub, "panel_calendar", lambda d: CAL)
    monkeypatch.setattr(ub, "pool_symbols", lambda scope, symbols_file=None: ["SH600001"])
    monkeypatch.setattr(ub, "mongo_db", lambda: _Db())
    monkeypatch.setattr(sys, "argv", ["qlib_update_bin.py", "--qlib-dir", str(root)])
    assert ub.main() == 0
    assert "无需更新" in capsys.readouterr().out


# ------------------------------- instruments 被写晚：把死票当活票（dump_update 的既有错法）

def _instruments(root: Path, rows: list[tuple[str, str, str]]) -> None:
    d = root / "instruments"
    d.mkdir(parents=True, exist_ok=True)
    (d / "all.txt").write_text(
        "\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")


def test_instruments_end_is_pulled_back_to_the_bins_real_end(tmp_path):
    """窗口里有行、但那几天全是 NaN（退市/长期停牌）⇒ dump_update 把结束日写成末日，
    比 bin 实际长度晚。必须按 bin 真实末日收回，否则下游把死票当活票。"""
    root = _panel(tmp_path)
    _bin(root, "SH600193", 0, [1.0] * 6)                 # bin 只到 10-06
    _instruments(root, [("SH600193", "2026-10-01", "2026-10-12")])   # 被写成日历末日
    fixed = ub.reconcile_instruments(root, CAL)
    assert fixed == [("SH600193", "2026-10-12", "2026-10-06")]
    text = (root / "instruments" / "all.txt").read_text(encoding="utf-8")
    assert "2026-10-06" in text and "2026-10-12" not in text


def test_a_correct_instruments_file_is_left_alone(tmp_path):
    """all.txt 和 bin 一致时不能动它——每次跑都重写文件会让 diff 全是噪音。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 6, [1.0] * 6)                 # bin 到 10-12 = 末日
    _instruments(root, [("SH600001", "2026-10-07", "2026-10-12")])
    assert ub.reconcile_instruments(root, CAL) == []
    assert (root / "instruments" / "all.txt").read_text(encoding="utf-8").strip() \
        == "SH600001\t2026-10-07\t2026-10-12"


def test_instruments_earlier_than_the_bin_is_not_touched(tmp_path):
    """只往早改：all.txt 比 bin 晚才是错；比 bin 早说明这票还没被追加，交给 dump_update，
    这里猜一次就可能把正常的票改坏。"""
    root = _panel(tmp_path)
    _bin(root, "SH600001", 6, [1.0] * 6)                 # bin 到 10-12
    _instruments(root, [("SH600001", "2026-10-07", "2026-10-09")])
    assert ub.reconcile_instruments(root, CAL) == []


def test_a_symbol_without_a_bin_is_skipped(tmp_path):
    """面板里没有这只票的目录（新股要走 dump_all/dump_fix），没有尺子就不改。"""
    root = _panel(tmp_path)
    _instruments(root, [("SH600009", "2026-10-01", "2026-10-12")])
    assert ub.reconcile_instruments(root, CAL) == []


# --------------------------- 末格 NaN：长度对齐但尾巴是空的（2026-11-03 实测 2,048 只）

def test_a_nan_tail_cell_is_stale_even_when_the_length_matches(tmp_path):
    """17:15 那次增量在行情没落库时照样把 11-02 追加成了 NaN 格：长度账面对齐、末格空洞。
    这类票用「落后」判不出来，必须看末格本身是不是数。"""
    root = _panel(tmp_path)
    values = [1.0] * (len(CAL) - 1) + [float("nan")]     # 覆盖到日历末尾，但末格 NaN
    _bin(root, "SH600001", 0, values)
    assert ub.stale_symbols(root, ["SH600001"], CAL) == ["SH600001"]


def test_a_real_value_at_the_tail_is_not_stale(tmp_path):
    root = _panel(tmp_path)
    _bin(root, "SH600001", 0, [1.0] * len(CAL))
    assert ub.stale_symbols(root, ["SH600001"], CAL) == []
