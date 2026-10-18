"""滚动重训的仪器测试：冻结票池、逐日 IC 落盘、两臂配对 t。

这些件都刻意**不 import qlib**——面板加载要 3.4 GB、两分钟，而这里守的是纯数据处理口径：
池文件读错、序列少一列、配对检验拿错列，都是落盘之后才会发现、且会让整批读数作废的坏法。
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run = _load("qlib_run")
pair = _load("qlib_ic_pair")


def mi_frame(days, instruments, values) -> pd.Series:
    """造 (datetime, instrument) 两层索引的序列——qlib 各入口给的就是这个形状。"""
    idx = pd.MultiIndex.from_product([pd.to_datetime(days), instruments],
                                     names=["datetime", "instrument"])
    return pd.Series(np.asarray(values, dtype=float), index=idx)


# ---------- 冻结票池 ----------

def test_read_pool_file_accepts_instruments_format(tmp_path):
    path = tmp_path / "pool.txt"
    lines = [f"SH{600000 + i}\t2016-01-01\t2026-10-16" for i in range(40)]
    lines += ["", "# 注释行", "SZ000001\t2016-01-01\t2026-10-16", "SH600000\t重复\t一遍"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pool = run.read_pool_file(path)
    assert pool[0] == "SH600000"
    assert "SZ000001" in pool
    assert len(pool) == 41, "重复 symbol 必须去掉，注释与空行不算票"
    assert pool.count("SH600000") == 1


def test_read_pool_file_keeps_order(tmp_path):
    syms = [f"SH{600100 + i}" for i in range(35)]
    path = tmp_path / "pool.txt"
    path.write_text("\n".join(reversed(syms)) + "\n", encoding="utf-8")
    assert run.read_pool_file(path) == list(reversed(syms)), "排序会让「同一个池」无法肉眼核对"


def test_read_pool_file_rejects_small_pool(tmp_path):
    path = tmp_path / "pool.txt"
    path.write_text("\n".join(f"SH{600000 + i}" for i in range(20)), encoding="utf-8")
    with pytest.raises(ValueError, match="少于 30 只"):
        run.read_pool_file(path)


def test_read_pool_file_rejects_non_stock_codes(tmp_path):
    syms = [f"SH{600000 + i}" for i in range(32)] + ["SH510300", "SZ159915"]
    path = tmp_path / "pool.txt"
    path.write_text("\n".join(syms) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="非 A 股代码"):
        run.read_pool_file(path)


# ---------- instruments 落盘 ----------

def test_write_instruments_lowercases_file_name(tmp_path):
    inst = tmp_path / "instruments"
    inst.mkdir()
    (inst / "all.txt").write_text(
        "SH600000\t2016-01-01\t2026-10-16\nSZ000001\t2016-01-04\t2026-10-16\n", encoding="utf-8")
    path = run.write_instruments(tmp_path, "batchL_ctrl", ["SH600000", "SZ000001"])
    # qlib 查 instruments 表之前会把名字 lower()，大写落盘的文件它永远找不到（批次 L 首次起跑即挂在这）
    assert path.name == "batchl_ctrl.txt"
    assert path.stem == "batchl_ctrl"
    assert path.read_text(encoding="utf-8").splitlines()[0] == "SH600000\t2016-01-01\t2026-10-16"


# ---------- 逐日 IC ----------

def _two_series(n_days=4, n_names=40, seed=3):
    rng = np.random.default_rng(seed)
    days = pd.date_range("2026-01-05", periods=n_days, freq="D")
    instruments = [f"SH{600000 + i}" for i in range(n_names)]
    size = n_days * n_names
    pred = mi_frame(days, instruments, rng.normal(size=size))
    label = mi_frame(days, instruments, rng.normal(size=size))
    return pred, label


def test_daily_ic_frame_drops_thin_days():
    pred, label = _two_series()
    thin_day = pd.Timestamp("2026-01-05")
    # 那一天只留 10 只票（其余 30 只删掉）：横截面太薄，秩相关纯噪声
    drop = [(thin_day, f"SH{600000 + i}") for i in range(30)]
    keep = ~pred.index.isin(drop)
    ic = run.daily_ic_frame(pred[keep], label[keep])
    assert thin_day not in ic.index, "不足 30 只的那天不能以同等权重拉动均值"
    assert len(ic) == 3
    assert list(ic.columns) == ["n", "pearson", "spearman"]


def test_ic_summary_keys_match_history():
    """键名和已归档的 run_*.json 必须一致，否则新旧批次放不进同一张表。"""
    pred, label = _two_series()
    keys = set(run.ic_summary(run.daily_ic_frame(pred, label)))
    assert keys == {"days", "median_cross_section", "rank_ic_mean", "rank_ic_std",
                    "rank_icir_daily", "rank_ic_tstat", "pearson_ic_mean",
                    "share_days_positive"}


def test_daily_ic_wrapper_is_aggregate_of_frame():
    pred, label = _two_series()
    assert run.daily_ic(pred, label) == run.ic_summary(run.daily_ic_frame(pred, label))


def test_ic_summary_on_empty_is_empty_dict():
    empty = pd.DataFrame(columns=["n", "pearson", "spearman"])
    empty.index.name = "day"
    assert run.ic_summary(empty) == {}


# ---------- 落盘 ----------

def test_dump_ic_daily_and_predictions_roundtrip(tmp_path):
    pred, label = _two_series(n_days=5)
    ic_valid = run.daily_ic_frame(pred.head(200), label.head(200))
    ic_test = run.daily_ic_frame(pred, label)
    spread = pd.Series([0.01, -0.02, 0.03, 0.04, 0.05],
                       index=pd.to_datetime(ic_test.index), name="ls_spread")
    path = run.dump_ic_daily(tmp_path, "unit_test", ic_valid, ic_test, spread)
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["spearman_valid", "pearson_valid", "n_valid",
                                   "spearman_test", "pearson_test", "n_test", "ls_spread"]
    assert frame.index.name == "day"
    assert frame["ls_spread"].tolist() == pytest.approx(spread.tolist())

    pred_path = run.dump_predictions(tmp_path, "unit_test", pred)
    dumped = pd.read_parquet(pred_path)
    assert list(dumped.columns) == ["datetime", "instrument", "score"]
    assert dumped["datetime"].str.match(r"\d{4}-\d{2}-\d{2}").all(), "带时分秒会让按日 join 对不上"
    assert len(dumped) == len(pred)


def test_dump_ic_daily_survives_empty_quintile(tmp_path):
    """五分档为空时 spread 是空序列：这一段不能把整个逐日表一起带走。"""
    pred, label = _two_series(n_days=3)
    ic = run.daily_ic_frame(pred, label)
    path = run.dump_ic_daily(tmp_path, "no_spread", ic, ic, pd.Series(dtype=float))
    assert pd.read_parquet(path)["ls_spread"].isna().all()


# ---------- 配对检验 ----------

def test_pair_test_uses_only_common_days():
    idx_a = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    idx_b = pd.to_datetime(["2026-01-06", "2026-01-07", "2026-01-08"])
    a = pd.Series([0.05, 0.06, 0.07], index=idx_a)
    b = pd.Series([0.02, 0.03, 0.04], index=idx_b)
    out = pair.pair_test(a, b, overlap=1, min_days=1)
    assert out["days_paired"] == 2
    assert out["mean_diff"] == pytest.approx(0.04)
    assert out["window"] == ["2026-01-06", "2026-01-07"]


def test_pair_test_shift_is_significant_and_sqrt5_adjusted():
    days = pd.to_datetime(pd.date_range("2026-01-05", periods=80, freq="D"))
    rng = np.random.default_rng(11)
    noise = rng.normal(scale=0.02, size=len(days))
    # 差值本身要带波动，否则「标准差=0」会直接把 t 判成 NaN，测不到 ÷√5 那一步
    shift = 0.01 + rng.normal(scale=0.002, size=len(days))
    a = pd.Series(noise + shift, index=days)
    b = pd.Series(noise, index=days)
    out = pair.pair_test(a, b, overlap=5, min_days=60)
    assert out["mean_diff"] == pytest.approx(0.01, abs=1e-3)
    assert out["corr_ab"] == pytest.approx(1.0, abs=0.01)
    assert out["t_raw"] / out["t_adjusted"] == pytest.approx(math.sqrt(5), rel=1e-3), \
        "重叠标签必须 ÷√5"
    assert out["t_adjusted"] > 2
    assert out["enough_days"] is True
    assert out["share_diff_positive"] == 1.0


def test_pair_test_flags_too_few_days():
    days = pd.to_datetime(pd.date_range("2026-01-05", periods=10, freq="D"))
    a = pd.Series(np.linspace(0.01, 0.02, 10), index=days)
    b = pd.Series(np.zeros(10), index=days)
    assert pair.pair_test(a, b, min_days=60)["enough_days"] is False


def test_pair_test_constant_diff_has_no_t():
    days = pd.to_datetime(pd.date_range("2026-01-05", periods=70, freq="D"))
    a = pd.Series(np.full(70, 0.05), index=days)
    b = pd.Series(np.zeros(70), index=days)
    out = pair.pair_test(a, b)
    assert math.isnan(out["t_raw"]) and math.isnan(out["t_adjusted"])
    assert out["mean_diff"] == pytest.approx(0.05)


def test_ic_path_error_lists_available_runs(tmp_path):
    pd.DataFrame({"spearman_test": [0.1]}, index=pd.to_datetime(["2026-01-05"])).to_parquet(
        tmp_path / "ic_daily_live2024.parquet")
    with pytest.raises(FileNotFoundError, match="已有逐日读数.*live2024"):
        pair.ic_path("typo_run", tmp_path)


def test_load_column_rejects_unknown_column(tmp_path):
    pd.DataFrame({"spearman_test": [0.1]}, index=pd.to_datetime(["2026-01-05"])).to_parquet(
        tmp_path / "ic_daily_arm.parquet")
    assert pair.load_column("arm", "spearman_test", tmp_path).iloc[0] == pytest.approx(0.1)
    with pytest.raises(KeyError, match="ls_spread"):
        pair.load_column("arm", "ls_spread", tmp_path)


# ---------- 留出段守卫与命令行口径 ----------

def test_holdout_ok_requires_strictly_later_segments():
    assert run.holdout_ok("2026-07-01", "2026-07-01", "2026-06-30") is True
    # 起点等于训练末尾 = 第一天就是背过的数据，不能算留出
    assert run.holdout_ok("2026-06-30", "2026-07-01", "2026-06-30") is False
    assert run.holdout_ok("2026-07-01", "2026-06-01", "2026-06-30") is False
    # 训练吃到日历末尾：valid/test 必然落在里面
    assert run.holdout_ok("2026-10-01", "2026-10-01", "2026-10-16") is False


def test_pool_file_and_pool_end_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["qlib_run.py", "--pool-file", "p.txt",
                                      "--pool-end", "2026-03-31"])
    with pytest.raises(SystemExit, match="别两个一起给"):
        run.main()


def test_fixed_trees_must_be_positive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["qlib_run.py", "--fixed-trees", "0"])
    with pytest.raises(SystemExit, match="至少 1 棵"):
        run.main()


def test_export_without_write_live_model_refuses_before_training(monkeypatch, tmp_path):
    """指向 data/qlib/model 就等于当场换线上：必须在训练**之前**拒绝，别烧完 5 分钟才报错。"""
    monkeypatch.setenv("QLIB_MODEL_DIR", str(tmp_path / "live_model"))
    monkeypatch.setattr(sys, "argv", ["qlib_run.py", "--export-model"])
    with pytest.raises(SystemExit, match="实盘模型目录"):
        run.main()
