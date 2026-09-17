# 连板吸筹效应识别 · 朴素贝叶斯训练管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将「偷偷低吸 → 试盘洗盘 → 连续涨停」四阶段盘口现象量化为 12 个特征，用 GaussianNB 训练连板识别模型，产出模型文件 + 验证报告 + 每日 TopN 候选 + 误报复盘。

**Architecture:** 两条腿分工。① `scripts/build_kline_features.py` 增加 4 个通用基础日线派生列（pct/lu/vol_ratio/pos60)，逻辑抽到可测试包 `stock_selector/features/base_columns.py`；② 新建 `scripts/train_bayes.py`（CLI 编排）+ `stock_selector/features/`（窗口特征/标签/建模，可单测），12 个 20 日窗口聚合特征训练时现算不落盘。模型全样本训练 + 先验调高（默认 0.15），时间切分验证，输出排序指标（precision@topN / AUC）。

**Tech Stack:** Python 3.12 · pandas/pyarrow(zstd) · scikit-learn(GaussianNB) · joblib · numpy · pytest · multiprocessing

**Conventions (learned from codebase):**
- Language: Python 3.12，脚本 `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` 后 import 包；包内绝对导入
- Naming: snake_case 函数/变量，模块内中文 docstring + logging
- Error handling: 单只股票失败打印警告跳过，不影响全流程；脏数据保守裁剪而非抛错
- Testing: pytest，文件 `tests/test_*.py`；parquet 测试用 pytest `tmp_path` 写临时 parquet（参考 tests/conftest.py `_write_stock`）；脚本模块用 importlib 按路径加载测试
- Logging: `logger = logging.getLogger("...")`；脚本层 `setup_logging()` 写 `logs/`
- 数据目录：`data/stock_kline/{code}.parquet`（zstd），既有列含 rsi14/ma5/vol_ma5 等
- DB: N/A（本计划不新增持久化，无 ORM/SQL）；API: N/A（不涉及 HTTP 服务）；Config: 沿用 `stock_selector/config/config.yaml` 但本计划无新配置项（训练超参走 CLI 参数）

**Domain Skills (from session context):**
- 单测编写: `test-code-generator` — 依据测试用例表生成 pytest（本计划已含完整测试代码，可直接采用）
- 测试方法: `test-driven-development` — TDD RED-GREEN 循环，每任务先写失败测试
- 排障调试: `systematic-debugging` — 出现失败时先收集证据再定位，禁止盲改
- 代码编写: `code-compliance-check` — 提交前 lint 与编码规范检查

---

## 上游质量门（Innovation Mode）

输入来自 brainstorming Phase 3 微设计 `docs/superpowers/specs/2026-09-11-bayes-limitup-design.md`（已批准 + 两轮 spec 评审通过）。Innovation 模式无上游 evaluator → 跳过，直接进入计划。

## 1. 文件映射

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `stock_selector/features/__init__.py` | 新增 | 包标记（空） |
| `stock_selector/features/base_columns.py` | 新增 | `board_limit_threshold` / `is_limit_up` / `add_base_columns`（pct/lu/vol_ratio/pos60） |
| `stock_selector/features/window.py` | 新增 | `clean_volume` / `compute_window_features`(12特征) / `assign_labels` / `build_dataset` |
| `stock_selector/features/modeling.py` | 新增 | `fit_gaussian` / `precision_at_k` / `evaluate_time_split` |
| `scripts/build_kline_features.py` | 修改 | 导入 `add_base_columns`，OUTPUT_COLUMNS 增加 4 列并计算 |
| `scripts/train_bayes.py` | 新增 | CLI：`--train` / `--predict` / `--code` / `--top-n` / `--date` / `--prior-pos` |
| `requirements.txt` | 修改 | 追加 `scikit-learn`、`joblib` |
| `tests/test_features_base_columns.py` | 新增 | TDD 单测 |
| `tests/test_features_window.py` | 新增 | TDD 单测 |
| `tests/test_features_modeling.py` | 新增 | TDD 单测（真实 GaussianNB 小样本） |
| `tests/test_train_bayes.py` | 新增 | 脚本 importlib 冒烟测试 |

> 说明：构建脚本/训练脚本保持薄，逻辑全部在 `stock_selector/features/` 包内可测。
> 设计 spec：`docs/superpowers/specs/2026-09-11-bayes-limitup-design.md`（后文简称「设计」）。

---

### Task 1: features 包 + base_columns（板块涨停线 / 4 基础列）

**Files:**
- Create: `stock_selector/features/__init__.py`
- Create: `stock_selector/features/base_columns.py`
- Test: `tests/test_features_base_columns.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| BC-01 | 设计§4 | normal | `board_limit_threshold("600519")` | `== 9.8`；`628/60` 前缀（主板） | unit | `tests/test_features_base_columns.py::test_board_threshold_main` | `python -m pytest tests/test_features_base_columns.py -q` |
| BC-02 | 设计§4 | normal | `board_limit_threshold("300750")/("301236")/("688256")` | 均 `== 19.5` | unit | `::test_board_threshold_gem_kcb` | 同上 |
| BC-03 | 设计§4 | normal | `board_limit_threshold("830799")/("430047")/("920001")` | 均 `== 29.5`；缺位补 6 位前缀（`"830799".startswith` 命中 `8`） | unit | `::test_board_threshold_bj` | 同上 |
| BC-04 | 设计§4 | boundary | `board_limit_threshold("999999")` | 默认主板 `== 9.8` | unit | `::test_board_threshold_default` | 同上 |
| BC-05 | 设计§4 | normal | `is_limit_up("300750", 19.6)` | `True`；`is_limit_up("600519", 9.7)` → `False`（9.7<9.8）；`is_limit_up("600519", 9.8)` → `True`（边界含等号） | unit | `::test_is_limit_up_boundary` | 同上 |
| BC-06 | 设计§6.1 | normal | `add_base_columns` 含 date 递增的 65 行 close/volume/vol_ma5（含代码 "603000"） | 返回含新增 4 列 `pct/lu/vol_ratio/pos60`；`pct[i]==(close[i]/close[i-1]-1)*100`（i≥1，首行 NaN）；`lu` 在 `pct>=9.8` 的行 True；`vol_ratio[i]==volume[i]/vol_ma5[i]`；`pos60` 首 59 行 NaN、其余在 [0,1]；原列保留 | unit | `::test_add_base_columns` | 同上 |

- [ ] **Step 1: 写失败测试 `tests/test_features_base_columns.py`**

```python
import numpy as np
import pandas as pd
import pytest

from stock_selector.features.base_columns import (
    add_base_columns,
    board_limit_threshold,
    is_limit_up,
)


def test_board_threshold_main():
    assert board_limit_threshold("600519") == 9.8
    assert board_limit_threshold("000001") == 9.8
    assert board_limit_threshold("002594") == 9.8


def test_board_threshold_gem_kcb():
    for code in ("300750", "301236", "688256"):
        assert board_limit_threshold(code) == 19.5


def test_board_threshold_bj():
    for code in ("830799", "430047", "920001"):
        assert board_limit_threshold(code) == 29.5


def test_board_threshold_default():
    assert board_limit_threshold("999999") == 9.8


def test_is_limit_up_boundary():
    assert is_limit_up("300750", 19.6)
    assert not is_limit_up("600519", 9.7)
    assert is_limit_up("600519", 9.8)


def test_add_base_columns():
    n = 65
    close = np.linspace(10.0, 13.0, n)
    volume = np.full(n, 100.0)
    volume[30] = 1000.0
    df = pd.DataFrame(
        {
            "code": "603000",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "close": close,
            "volume": volume,
            "vol_ma5": pd.Series(volume).rolling(5).mean().to_numpy(),
        }
    )
    out = add_base_columns(df.copy())
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(out.columns)
    expected_pct = (pd.Series(close).pct_change() * 100).to_numpy()
    np.testing.assert_allclose(out["pct"].to_numpy(), expected_pct, equal_nan=True)
    # 603000 主板 9.8：pct = (1/10)*100*? → 用真实值校验
    assert out["lu"].dtype == bool
    # vol_ratio
    expected_vr = pd.Series(volume) / pd.Series(volume).rolling(5).mean()
    np.testing.assert_allclose(out["vol_ratio"].to_numpy(), expected_vr.to_numpy(), equal_nan=True)
    assert out["pos60"].iloc[:59].isna().all()
    assert out["pos60"].iloc[59:].between(0, 1).all()
    assert {"code", "date", "close", "volume"} <= set(out.columns)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_features_base_columns.py -q`
Expected: `ModuleNotFoundError: No module named 'stock_selector.features'`

- [ ] **Step 3: 实现 `stock_selector/features/__init__.py`（空文件）与 `base_columns.py`**

```python
"""基础日线派生列：板块涨停线判定 + pct/lu/vol_ratio/pos60。"""

import numpy as np
import pandas as pd

LIMIT_MAIN = 9.8
LIMIT_GEM = 19.5
LIMIT_BJ = 29.5


def board_limit_threshold(code: str) -> float:
    """按股票代码前缀返回主板/创业板/科创板/北交所的涨停涨幅阈值(%)。"""
    if code.startswith(("300", "301", "688")):
        return LIMIT_GEM
    if code.startswith(("8", "4", "920")):
        return LIMIT_BJ
    return LIMIT_MAIN


def is_limit_up(code: str, pct: float) -> bool:
    """当且仅当涨幅 pct(%) 达到该板块涨停线时返回 True。"""
    return pct >= board_limit_threshold(code)


def add_base_columns(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上原位新增 4 个基础派生列并返回。

    依赖 df 已有列：code, close, volume, vol_ma5。
    新增：
      - pct      当日涨跌幅(%)
      - lu       板块感知涨停标记(bool)
      - vol_ratio  量比 = volume / vol_ma5
      - pos60    现价在 60 日高低区间的分位
    """
    close = df["close"].astype(float)
    pct = close.pct_change() * 100.0
    df["pct"] = pct

    thr = df["code"].map(board_limit_threshold)
    df["lu"] = pct >= thr

    vol_ma5 = df["vol_ma5"].astype(float)
    df["vol_ratio"] = df["volume"].astype(float) / vol_ma5.replace(0, np.nan)

    max60 = close.rolling(60).max()
    min60 = close.rolling(60).min()
    df["pos60"] = (close - min60) / (max60 - min60 + 1e-9)
    return df
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_features_base_columns.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add stock_selector/features/ tests/test_features_base_columns.py
git commit -m "feat(features): 板块涨停线判定 + pct/lu/vol_ratio/pos60 基础列"
```

> **调用 `code-compliance-check`** 在 commit 前检查（TDD 每任务提交前执行）。

---

### Task 2: window.py 窗口特征 + 标签 + 全市场数据集构建

**Files:**
- Create: `stock_selector/features/window.py`
- Test: `tests/test_features_window.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| WF-01 | 设计§7 | normal | `clean_volume`：60 行 volume≈100 常量，第 30 行 volume=5000 | 第 30 行被裁剪到 `8*rolling20中位数=800`，其余不变；列保持 | unit | `tests/test_features_window.py::test_clean_volume_clip` | `python -m pytest tests/test_features_window.py -q` |
| WF-02 | 设计§6.1/§5 | normal | 构造 120 行可控行情（plot：前 100 日 10→12 缓慢上行、之后 8 日涨停) | `compute_window_features` 返回 12 列全部存在于 `pct_20d/slope_20d/pos_60d/ma5_ma20/vol_ratio_avg/up_day_vol_ratio/vol_slope/vol_corr_pct/drawdown_20d/test_limit_cnt/limit_up_early/max_gain_3d`；尾部涨停段 `test_limit_cnt` 段内值 ≥1、`max_gain_3d` >0 | unit | `::test_window_features_columns_and_value` | 同上 |
| WF-03 | 设计§5 | normal | `compute_window_features` 返回 NaN 集中在最前 59 行（pos_60d 依赖 60 日） | `pct_20d` 前 19 行 NaN；`pos_60d` 前 59 行 NaN | unit | `::test_window_features_nan_leading` | 同上 |
| WF-04 | 设计§4 | normal | 构造 40 行：前 20 日无涨停、T 日 ret20=0.1、pos60=0.3；后 10 日含 4 次涨停（按 600000 主板 9.8） | `assign_labels`：T 行 label==1(Lu_future≥3) | unit | `::test_assign_labels_positive` | 同上 |
| WF-05 | 设计§4 | boundary | 同上但后 10 日含 2 次涨停 | `assign_labels` T 行 label 为 NaN（中间模糊区剔除） | unit | `::test_assign_labels_fuzzy_drop` | 同上 |
| WF-06 | 设计§4 | normal | 同上但后 10 日 0 次涨停且前窗涨停数 ≤1 | `assign_labels` T 行 label==0 | unit | `::test_assign_labels_negative` | 同上 |
| WF-07 | 设计§4 | boundary | 候选门槛外（ret20=0.30） | label==NaN（非候选不参与） | unit | `::test_assign_labels_non_candidate` | 同上 |
| WF-08 | 设计§6.2 | normal | tmp_dir 含 2 个 parquet（600000/300750，各 ≥80 行，一个含后段 4 涨停） | `build_dataset(tmp_dir)` 返回 DataFrame，列含 `code/date/label` + 12 特征；label∈{0,1} 非空行数 ≥1；两 code 都出现 | unit | `::test_build_dataset` | 同上 |

测试数据构造辅助（所有 WF 用例共用，放测试文件模块级函数）：

```python
def _mk(code, start=10.0, n=120, limup_days=(), vol=100.0):
    rng = np.random.default_rng(0)
    close = np.full(n, start)
    for i in range(1, n):
        if i in limup_days:
            close[i] = close[i - 1] * 1.10  # 主板涨停近似
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.0005, 0.01))
    vol_arr = np.full(n, vol)
    for d in limup_days:
        vol_arr[d] = vol * 2
    return pd.DataFrame({
        "code": code,
        "date": pd.date_range("2025-06-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol_arr, "amount": vol_arr * close,
    })
```

- [ ] **Step 1: 写失败测试 `tests/test_features_window.py`**

```python
import numpy as np
import pandas as pd
import pytest

from stock_selector.features.window import (
    assign_labels,
    build_dataset,
    clean_volume,
    compute_window_features,
)

FEATURES = [
    "pct_20d", "slope_20d", "pos_60d", "ma5_ma20", "vol_ratio_avg",
    "up_day_vol_ratio", "vol_slope", "vol_corr_pct", "drawdown_20d",
    "test_limit_cnt", "limit_up_early", "max_gain_3d",
]


def _mk(code, start=10.0, n=120, limup_days=(), vol=100.0):
    rng = np.random.default_rng(0)
    close = np.full(n, start)
    for i in range(1, n):
        if i in limup_days:
            close[i] = close[i - 1] * 1.10
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.0005, 0.01))
    vol_arr = np.full(n, vol)
    for d in limup_days:
        vol_arr[d] = vol * 2
    return pd.DataFrame({
        "code": code,
        "date": pd.date_range("2025-06-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol_arr, "amount": vol_arr * close,
    })


def test_clean_volume_clip():
    df = _mk("600000", n=60)
    df.loc[30, "volume"] = 5000.0
    out = clean_volume(df)
    med = df["volume"].rolling(20).median().iloc[30]
    assert out["volume"].iloc[30] == pytest.approx(8 * med)
    assert out.loc[30, "volume"] == pytest.approx(8 * df["volume"].iloc[30] / 5000 * 100)


def test_window_features_columns_and_value():
    df = _mk("600000", n=140, limup_days=range(125, 133))
    feat = compute_window_features(df, "600000")
    assert set(FEATURES) <= set(feat.columns)
    tail = feat.iloc[125:133]
    assert (tail["test_limit_cnt"] >= 1).all()
    assert (tail["max_gain_3d"] > 0).all()


def test_window_features_nan_leading():
    df = _mk("600000", n=120)
    feat = compute_window_features(df, "600000")
    assert feat["pct_20d"].iloc[:19].isna().all()
    assert feat["pos_60d"].iloc[:59].isna().all()


def test_assign_labels_positive():
    df = _mk("600000", n=45, limup_days=range(35, 39))
    lab = assign_labels(df, "600000")
    t = 30
    assert lab["label"].iloc[t] == 1


def test_assign_labels_fuzzy_drop():
    df = _mk("600000", n=45, limup_days=(35, 36))  # 恰好 2 次涨停
    lab = assign_labels(df, "600000")
    assert np.isnan(lab["label"].iloc[30])


def test_assign_labels_negative():
    df = _mk("600000", n=45)  # 后 10 日无涨停
    lab = assign_labels(df, "600000")
    assert lab["label"].iloc[30] == 0


def test_assign_labels_non_candidate():
    df = _mk("600000", n=45)
    df.loc[30, "close"] = df.loc[10, "close"] * 1.30  # ret20≈0.30 超门槛
    lab = assign_labels(df, "600000")
    assert np.isnan(lab["label"].iloc[30])


def _write_parquet(dirpath, code, df):
    df.to_parquet(dirpath / f"{code}.parquet", index=False, compression="zstd")


def test_build_dataset(tmp_path):
    _write_parquet(tmp_path, "600000", _mk("600000", n=100, limup_days=range(90, 94)))
    _write_parquet(tmp_path, "300750", _mk("300750", n=100))
    ds = build_dataset(tmp_path)
    assert {"code", "date", "label"} <= set(ds.columns)
    assert set(FEATURES) <= set(ds.columns)
    assert ds["label"].dropna().isin([0, 1]).all()
    assert len(ds) > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_features_window.py -q`
Expected: `ModuleNotFoundError: No module named 'stock_selector.features.window'`

- [ ] **Step 3: 实现 `stock_selector/features/window.py`**

```python
"""20 日窗口特征 + 标签 + 全市场数据集构建。"""

import logging
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from stock_selector.features.base_columns import (
    add_base_columns,
    board_limit_threshold,
    is_limit_up,
)

logger = logging.getLogger("features.window")

WINDOW = 20
FEATURE_COLUMNS = [
    "pct_20d", "slope_20d", "pos_60d", "ma5_ma20", "vol_ratio_avg",
    "up_day_vol_ratio", "vol_slope", "vol_corr_pct", "drawdown_20d",
    "test_limit_cnt", "limit_up_early", "max_gain_3d",
]

# 标签参数（与设计§4一致）
RET20_RANGE = (0.02, 0.25)
POS60_RANGE = (0.15, 0.55)
LU20_MAX = 1
FUT_WIN = 10
POS_MIN_LU = 3
NEG_MAX_LU = 1
VOL_CLIP_MED = 8


def _linreg_slope_norm(values: np.ndarray) -> float:
    if len(values) < 2:
        return np.nan
    x = np.arange(len(values), dtype=float)
    slope = np.polyfit(x, values, 1)[0]
    mean = np.nanmean(values)
    return slope / mean if mean else np.nan


def clean_volume(df: pd.DataFrame) -> pd.DataFrame:
    """将 volume 裁剪到 8 × rolling(20) 中位数，防止脏数据击穿量比特征。"""
    df = df.copy()
    med = df["volume"].rolling(20).median()
    cap = VOL_CLIP_MED * med
    df["volume"] = df["volume"].clip(upper=cap)
    return df


def compute_window_features(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """计算 12 个 20 日窗口特征，返回与 df 对齐（保留 date 列）的特征表。"""
    df = df.sort_values("date").reset_index(drop=True)
    vol = df["volume"].astype(float)
    close = df["close"].astype(float)
    pct = close.pct_change() * 100.0
    thr = board_limit_threshold(code)

    out = pd.DataFrame(index=df.index)
    out["date"] = df["date"]
    out["pct_20d"] = close / close.shift(WINDOW) - 1.0
    out["slope_20d"] = close.rolling(WINDOW).apply(_linreg_slope_norm, raw=True)
    max60 = close.rolling(60).max()
    min60 = close.rolling(60).min()
    out["pos_60d"] = (close - min60) / (max60 - min60 + 1e-9)
    out["ma5_ma20"] = close.rolling(5).mean() / close.rolling(20).mean()
    out["vol_ratio_avg"] = (vol / vol.rolling(5).mean()).rolling(WINDOW).mean()
    out["up_day_vol_ratio"] = (
        pct.where(pct > 0, np.nan).rolling(WINDOW).mean()
        / pct.where(pct < 0, np.nan).rolling(WINDOW).mean()
    )
    out["vol_slope"] = vol.rolling(WINDOW).apply(_linreg_slope_norm, raw=True)
    out["vol_corr_pct"] = pct.rolling(WINDOW).corr(vol)
    out["drawdown_20d"] = (1.0 - close / close.rolling(WINDOW).max()).rolling(WINDOW).max()
    out["test_limit_cnt"] = ((pct > 7.0) & (pct < thr)).rolling(WINDOW).sum().shift(1)
    out["limit_up_early"] = (pct >= thr).rolling(WINDOW).sum().shift(1)
    pct3 = pct.rolling(3, min_periods=1).sum()
    out["max_gain_3d"] = pct3.rolling(WINDOW).max().shift(1)
    return out


def assign_labels(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """按设计§4 打标签：候选门槛 + 未来窗口涨停数。label: 1/0/NaN(剔除)。"""
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    pct = close.pct_change() * 100.0
    lu = (pct >= board_limit_threshold(code)).astype(float)

    max60 = close.rolling(60).max()
    min60 = close.rolling(60).min()
    pos60 = (close - min60) / (max60 - min60 + 1e-9)

    ret20 = close / close.shift(WINDOW) - 1.0
    lu20_past = lu.rolling(WINDOW).sum().shift(1)
    lu10_future = lu.rolling(FUT_WIN).sum().shift(-FUT_WIN)

    cand = (
        ret20.between(RET20_RANGE[0], RET20_RANGE[1])
        & pos60.between(POS60_RANGE[0], POS60_RANGE[1])
        & (lu20_past <= LU20_MAX)
    )
    label = pd.Series(np.nan, index=df.index)
    label.loc[cand & (lu10_future >= POS_MIN_LU)] = 1
    label.loc[cand & (lu10_future <= NEG_MAX_LU)] = 0
    lab = pd.DataFrame({"date": df["date"], "label": label})
    return lab


def _process_one(args):
    path, = args
    try:
        df = pd.read_parquet(path)
        if len(df) < 80:
            return None
        df = clean_volume(df)
        df = add_base_columns(df)
        code = str(df["code"].iloc[0])
        feat = compute_window_features(df, code)
        lab = assign_labels(df, code)
        m = feat.merge(lab, on="date")
        m["code"] = code
        m["date"] = feat["date"]
        keep = m.dropna(subset=FEATURE_COLUMNS)
        return keep[["code", "date"] + FEATURE_COLUMNS + ["label"]]
    except Exception as exc:  # 单只失败不影响全流程
        logger.warning("[SKIP] %s: %s", path.name, exc)
        return None


def build_dataset(kline_dir, processes: int = 8, limit: int = 0) -> pd.DataFrame:
    """扫描 kline_dir 全部 parquet，返回全市场候选样本表（12 特征 + label）。

    label 为 NaN 的中间模糊区/非候选行保留（供上层丢弃），但全特征 NaN 的行剔除。
    若某文件缺新列，由 add_base_columns 从 OHLCV 现算补齐（设计§6.2 容错）。
    """
    paths = sorted(Path(kline_dir).glob("*.parquet"))
    if limit:
        paths = paths[:limit]
    frames = []
    with Pool(processes) as pool:
        for res in pool.imap_unordered(_process_one, [(p,) for p in paths], chunksize=25):
            if res is not None:
                frames.append(res)
    if not frames:
        return pd.DataFrame(columns=["code", "date"] + FEATURE_COLUMNS + ["label"])
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_features_window.py -q`
Expected: `8 passed`

> 调试提示：若 `WF-04` 失败，检查构造数据是否让 T 行的 `ret20` 落在 [0.02,0.25]、`pos60` ∈ [0.15,0.55]（可先打印 `df` 在 T 行附近的值核对）。**调用 `systematic-debugging`**——先打印证据再改。

- [ ] **Step 5: Commit**

```bash
git add stock_selector/features/window.py tests/test_features_window.py
git commit -m "feat(features): 20日窗口12特征 + 标签规则 + 全市场数据集构建"
```

---

### Task 3: build_kline_features.py 集成 4 个基础列

**Files:**
- Modify: `scripts/build_kline_features.py`
- Test: `tests/test_build_kline_features_integration.py`（新增）

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| BK-01 | 设计§6.1 | regression | importlib 加载 `scripts/build_kline_features.py` | `OUTPUT_COLUMNS` 包含 `pct/lu/vol_ratio/pos60` | unit | `tests/test_build_kline_features_integration.py::test_output_columns` | `python -m pytest tests/test_build_kline_features_integration.py -q` |
| BK-02 | 设计§6.1 | normal | 构造含 code/name/date/OHLCV 的 70 行 df，调 `compute_indicators` | 返回列含 4 新列；`pct[3]` 计算正确；`vol_ratio[3]` 正确；`lu` 为 bool；`pos60` 尾行 ∈ [0,1] | unit | `::test_compute_indicators_has_derived` | 同上 |

- [ ] **Step 1: 写失败测试 `tests/test_build_kline_features_integration.py`**

```python
import importlib.util
import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/build_kline_features.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_kline_features", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _df(n=70):
    close = np.linspace(10, 12, n)
    df = pd.DataFrame(
        {
            "code": "603000",
            "name": "Test",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "open": close, "high": close * 1.02, "low": close * 0.98,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": np.full(n, 1000.0),
        }
    )
    return df


def test_output_columns():
    mod = _load_script()
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(mod.OUTPUT_COLUMNS)


def test_compute_indicators_has_derived():
    mod = _load_script()
    df = _df()
    out = mod.compute_indicators(df)
    assert {"pct", "lu", "vol_ratio", "pos60"} <= set(out.columns)
    close = df["close"].to_numpy()
    assert out["pct"].iloc[3] == pytest.approx((close[3] / close[2] - 1) * 100)
    assert out["vol_ratio"].iloc[30] == pytest.approx(100.0 / 100.0)
    assert out["lu"].dtype == bool
    assert out["pos60"].dropna().between(0, 1).all()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_build_kline_features_integration.py -q`
Expected: 2 个断言失败（新列不在 `OUTPUT_COLUMNS` / 返回列集）

- [ ] **Step 3: 修改 `scripts/build_kline_features.py`**

在文件头部 import 区内追加包导入（放在 `import yaml` 之后）：

```python
from stock_selector.features.base_columns import add_base_columns
```

修改列常量（原 `OUTPUT_COLUMNS = BASE_COLUMNS + INDICATOR_COLUMNS`）：

```python
DERIVED_COLUMNS = ["pct", "lu", "vol_ratio", "pos60"]
OUTPUT_COLUMNS = BASE_COLUMNS + INDICATOR_COLUMNS + DERIVED_COLUMNS
```

修改 `compute_indicators` 末尾（在 `out["last_close"]` 之后）：

```python
    out["vol_ma5"] = df["volume"].astype(float).rolling(5).mean()
    out["last_close"] = df["close"].astype(float).shift(1)
    out = add_base_columns(out)
    return out[OUTPUT_COLUMNS]
```

> 说明：`add_base_columns` 需 `vol_ma5` 已存在（上一行刚算）；`lu/pos60` 与 `vol_ma5` 仅用 close/volume/code。全量与增量的整序列重算路径都会经过 `compute_indicators`，保证两模式结果一致（设计§6.1）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_build_kline_features_integration.py -q`
Expected: `2 passed`；再跑全量回归 `python -m pytest tests/ -q` 确认既有测试未破坏

- [ ] **Step 5: Commit**

```bash
git add scripts/build_kline_features.py tests/test_build_kline_features_integration.py
git commit -m "feat(build_kline): 输出列增加 pct/lu/vol_ratio/pos60 基础派生列"
```

---

### Task 4: modeling.py 朴素贝叶斯拟合 + 时间切分评估

**Files:**
- Create: `stock_selector/features/modeling.py`
- Test: `tests/test_features_modeling.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| MD-01 | 设计§8/§9 | normal | `fit_gaussian` 在可分特征上训练（正样本该特征明显更高） | 训练集自预测：正样本 proba(1) 均值 > 负样本均值；`prior_pos` 参数生效（proba 量级受先验抬升） | unit | `tests/test_features_modeling.py::test_fit_gaussian_class_separable` | `python -m pytest tests/test_features_modeling.py -q` |
| MD-02 | 设计§9 | normal | `precision_at_k`：y=[1,1,0,1,0], proba=[0.9,0.8,0.7,0.2,0.1], k=2 | `== 1.0`（前 2 均正例）；k=4 → `0.75`（3/4） | unit | `::test_precision_at_k` | 同上 |
| MD-03 | 设计§9 | normal | `time_split` 用 dates（60 个升序唯一日期） | 训练日期集合 ⊂ 验证日期集合前（train max < val min），train/test 比例 ≈ 80/20 | unit | `::test_time_split_no_leak` | 同上 |
| MD-04 | 设计§9 | normal | `evaluate_time_split` 用可分合成样本 | 返回 metrics 含 `auc`/`avg_precision`/`prec_at_tops`(dict)/`base_rate`/`n_pos_valid`；miss 表含 `proba`/`label` 列 | unit | `::test_evaluate_time_split` | 同上 |

- [ ] **Step 1: 写失败测试 `tests/test_features_modeling.py`**

```python
import numpy as np
import pandas as pd
import pytest

from stock_selector.features.modeling import (
    evaluate_time_split,
    fit_gaussian,
    precision_at_k,
    time_split,
)


def test_precision_at_k():
    y = np.array([1, 1, 0, 1, 0])
    proba = np.array([0.9, 0.8, 0.7, 0.2, 0.1])
    assert precision_at_k(y, proba, 2) == 1.0
    assert precision_at_k(y, proba, 4) == 0.75


def test_time_split_no_leak():
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    idx = np.arange(60)
    train_idx, test_idx = time_split(idx, dates)
    assert dates[train_idx].max() < dates[test_idx].min()
    assert abs(len(train_idx) / 60 - 0.8) < 0.02


def _separable(n_pos=200, n_neg=800, seed=0):
    rng = np.random.default_rng(seed)
    x_pos = rng.normal(2.0, 0.5, size=(n_pos, 3))
    x_neg = rng.normal(0.0, 0.5, size=(n_neg, 3))
    X = np.vstack([x_pos, x_neg])
    y = np.array([1] * n_pos + [0] * n_neg)
    dates = np.concatenate([
        pd.date_range("2026-01-05", periods=n_pos, freq="B"),
        pd.date_range("2025-01-05", periods=n_neg, freq="B"),
    ])
    dates = np.sort(dates)
    return X, y, dates


def test_fit_gaussian_class_separable():
    X, y, _ = _separable()
    idx = rng_index = np.random.default_rng(1).permutation(len(y))[:200]
    model = fit_gaussian(X[idx], y[idx], prior_pos=0.3)
    proba = model.predict_proba(X[idx])[:, 1]
    assert np.mean(proba[y[idx] == 1]) > np.mean(proba[y[idx] == 0])


def test_evaluate_time_split():
    X, y, dates = _separable()
    res = evaluate_time_split(X, y, dates, prior_pos=0.3)
    for key in ("auc", "avg_precision", "prec_at_tops", "base_rate", "n_pos_valid", "misses"):
        assert key in res
    assert 0.5 < res["auc"] <= 1.0
    assert isinstance(res["misses"], pd.DataFrame)
    assert set(res["misses"].columns) >= {"proba", "label"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_features_modeling.py -q`
Expected: `ModuleNotFoundError: No module named 'stock_selector.features.modeling'`

- [ ] **Step 3: 实现 `stock_selector/features/modeling.py`**

```python
"""GaussianNB 拟合 + 时间切分评估（排序指标）。"""

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.naive_bayes import GaussianNB

TOP_NS = (50, 100, 200)


def fit_gaussian(X, y, prior_pos: float = 0.15) -> GaussianNB:
    """全样本训练；调高正类先验使输出概率转为排序打分（设计§8）。"""
    model = GaussianNB(priors=[1.0 - prior_pos, prior_pos])
    model.fit(X, y)
    return model


def precision_at_k(y_true, proba_pos, k: int) -> float:
    """排序取概率前 k 个，正例占比。y/proba 需同长度。"""
    if k <= 0 or len(y_true) == 0:
        return 0.0
    k = min(k, len(y_true))
    top = np.argsort(-proba_pos)[:k]
    return float(np.mean(y_true[top]))


def time_split(indices, dates):
    """按日期 80/20 时间切分，保证 train 全部早于 test（防泄漏）。"""
    dates = pd.to_datetime(pd.Series(dates)).to_numpy()
    order = np.argsort(dates)
    sorted_dates = dates[order]
    cut = int(len(sorted_dates) * 0.8)
    split_date = sorted_dates[cut]
    train_idx = indices[dates <= split_date]
    test_idx = indices[dates > split_date]
    return train_idx, test_idx


def evaluate_time_split(X, y, dates, prior_pos: float = 0.15, top_ns=TOP_NS) -> dict:
    """训练(全量前80%日期) → 验证(后20%日期真实分布) → 排序指标 + 误报样本表。

    返回:
      auc / avg_precision / prec_at_tops(dict) / base_rate / train_size /
      test_size / n_pos_valid / labels(验证集) / probs(验证集) / misses(DataFrame)
    """
    idx = np.arange(len(y))
    train_idx, test_idx = time_split(idx, dates)
    X_train, y_train = X[train_idx], y[train_idx]
    X_test, y_test = X[test_idx], y[test_idx]
    model = fit_gaussian(X_train, y_train, prior_pos=prior_pos)
    proba = model.predict_proba(X_test)[:, 1]

    top_ok = min(len(proba), max(top_ns))
    precs = {f"prec_at_{k}": precision_at_k(y_test, proba, k) for k in top_ns}
    misses = pd.DataFrame({"proba": proba, "label": y_test})

    n_pos = int(y.sum())
    return {
        "auc": float(roc_auc_score(y_test, proba)) if (len(np.unique(y_test)) > 1) else float("nan"),
        "avg_precision": float(average_precision_score(y_test, proba)) if (len(np.unique(y_test)) > 1) else float("nan"),
        "prec_at_tops": precs,
        "base_rate": float(n_pos / len(y)),
        "n_pos_valid": int(y_test.sum()),
        "train_size": int(len(train_idx)),
        "test_size": int(len(test_idx)),
        "labels": y_test,
        "probs": proba,
        "misses": misses,
        "model": model,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_features_modeling.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add stock_selector/features/modeling.py tests/test_features_modeling.py
git commit -m "feat(features): GaussianNB 拟合 + 时间切分评估 + precision@topN"
```

---

### Task 5: train_bayes.py CLI（训练/预测/单票诊断）

**Files:**
- Create: `scripts/train_bayes.py`
- Test: `tests/test_train_bayes.py`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Test Target | Command |
|---|---|---|---|---|---|---|---|
| TB-01 | 设计§10/§11 | regression | importlib 加载 `scripts/train_bayes.py` | 模块含 `main`；常量 `KLINE_DIR/MODELS_DIR/REPORTS_DIR/CAND_DIR` 指向预期目录 | unit | `tests/test_train_bayes.py::test_module_loads` | `python -m pytest tests/test_train_bayes.py -q` |
| TB-02 | 设计§10/§11 | normal | `--code 600000 --kline-dir <tmp>` 指向 80+ 行 parquet | 打印诊断表（code/date/label 前后窗口特征行），退出码 0，不抛错 | unit(CLI) | `::test_diag_code` | 同上 |
| TB-03 | 设计§8/§10 | normal | `--train --kline-dir <tmp2> --models-dir <tmp> --reports-dir <tmp>` 指向含 600000(含涨停后段)+600001(无涨停) 两个 parquet | 生成 `bayes_limitup.joblib` 与 `bayes_train_report.md`；joblib 可 load 且含 `model/features/prior_pos`；report 含 `precision@` 字样 | unit(CLI) | `::test_train_writes_artifacts` | 同上 |

- [ ] **Step 1: 写失败测试 `tests/test_train_bayes.py`**

```python
import importlib.util
import joblib
import numpy as np
import pandas as pd
import pytest

SCRIPT = "scripts/train_bayes.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("train_bayes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk(code, n=200, limup_days=(), start=10.0):
    rng = np.random.default_rng(0)
    close = np.full(n, start)
    for i in range(1, n):
        if i in limup_days:
            close[i] = close[i - 1] * 1.10
        else:
            close[i] = close[i - 1] * (1 + rng.normal(0.0005, 0.01))
    vol = np.full(n, 100.0)
    for d in limup_days:
        vol[d] = 250.0
    return pd.DataFrame({
        "code": code, "name": "T",
        "date": pd.date_range("2025-01-02", periods=n, freq="B"),
        "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": vol, "amount": vol * close,
    })


def test_module_loads():
    mod = _load_script()
    assert callable(mod.main)
    assert str(mod.KLINE_DIR).endswith("stock_kline")


def test_diag_code(tmp_path):
    mod = _load_script()
    code = "600000"
    (tmp_path / f"{code}.parquet").parent.mkdir(exist_ok=True)
    _mk(code).to_parquet(tmp_path / f"{code}.parquet", index=False, compression="zstd")
    mod.main(["--code", code, "--kline-dir", str(tmp_path)])  # 不抛错即通过


def test_train_writes_artifacts(tmp_path):
    mod = _load_script()
    for code, days in (("600000", range(190, 194)), ("600001", ())):
        _mk(code, limup_days=days).to_parquet(
            tmp_path / f"{code}.parquet", index=False, compression="zstd"
        )
    model_file = tmp_path / "m.joblib"
    report_file = tmp_path / "r.md"
    mod.main([
        "--train", "--kline-dir", str(tmp_path),
        "--models-dir", str(tmp_path), "--reports-dir", str(tmp_path),
        "--model-file", str(model_file), "--report-file", str(report_file),
        "--prior-pos", "0.15",
    ])
    assert model_file.exists() and report_file.exists()
    with open(model_file, "rb") as fh:
        payload = joblib.load(fh)
    assert "model" in payload and "features" in payload and "prior_pos" in payload
    assert "precision@50" in report_file.read_text(encoding="utf-8")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_train_bayes.py -q`
Expected: `ModuleNotFoundError`（脚本不存在）

- [ ] **Step 3: 实现 `scripts/train_bayes.py`**

```python
"""连板吸筹效应识别 —— GaussianNB 训练/预测 CLI。

用法:
    python scripts/train_bayes.py --train                          # 全流程训练+验证报告
    python scripts/train_bayes.py --predict --top-n 100 [--date 2026-09-08]
    python scripts/train_bayes.py --code 003040
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib  # noqa: E402
import pandas as pd  # noqa: E402

from stock_selector.features.modeling import TOP_NS, evaluate_time_split  # noqa: E402
from stock_selector.features.window import FEATURE_COLUMNS, assign_labels, build_dataset, clean_volume  # noqa: E402
from stock_selector.features.base_columns import add_base_columns  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = ROOT / "data" / "stock_kline"
MODELS_DIR = ROOT / "models"
REPORTS_DIR = ROOT / "reports"
CAND_DIR = ROOT / "data" / "bayes_candidates"

logger = logging.getLogger("train_bayes")
DEFAULT_PRIOR = 0.15


def _g(new_dir: Path) -> None:
    new_dir.mkdir(parents=True, exist_ok=True)


def diag_code(code: str, kline_dir: Path):
    """单票诊断：打印最新交易日的 12 特征 + 前窗计数 + 打分（若有模型）。"""
    path = Path(kline_dir) / f"{code}.parquet"
    df = pd.read_parquet(path)
    df = clean_volume(df)
    df = add_base_columns(df)
    from stock_selector.features.window import compute_window_features

    feat = compute_window_features(df, code)
    lab = assign_labels(df, code)
    row = df.merge(feat, on="date").merge(lab, on="date").dropna(subset=FEATURE_COLUMNS)
    if row.empty:
        logger.info("[DIAG] %s 无完整特征窗口", code)
        return
    tail = row.iloc[-1]
    lines = ["code=%s  date=%s" % (code, tail["date"]), "  label=%s" % tail.get("label")]
    for f in FEATURE_COLUMNS:
        lines.append("  %s=%.4g" % (f, tail[f]))
    logger.info("\n".join(lines))


def train(kline_dir: Path, models_dir: Path, reports_dir: Path,
          model_file: Path, report_file: Path, prior_pos: float):
    t0 = time.time()
    ds = build_dataset(kline_dir)
    ds = ds.dropna(subset=["label"])
    if ds.empty:
        raise SystemExit("无可用训练样本，检查 kline_dir=%s" % kline_dir)

    X = ds[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = ds["label"].to_numpy(dtype=int)
    dates = pd.to_datetime(ds["date"]).to_numpy()

    res = evaluate_time_split(X, y, dates, prior_pos=prior_pos, top_ns=TOP_NS)
    payload = {
        "model": res["model"],
        "features": FEATURE_COLUMNS,
        "prior_pos": prior_pos,
        "train_start": pd.Timestamp(dates.min()).strftime("%Y-%m-%d"),
        "train_end": pd.Timestamp(dates.max()).strftime("%Y-%m-%d"),
    }
    _g(models_dir)
    _g(reports_dir)
    with open(model_file, "wb") as fh:
        joblib.dump(payload, fh)

    base = res["base_rate"]
    rows = ["# 连板吸筹效应识别 · 训练验证报告", "",
            "- 训练样本: %d (正 %d / 负 %d, 正样本率 %.3f%%)"
            % (len(y), int(y.sum()), int((y == 0).sum()), 100.0 * base),
            "- 时间切分: 前 80%% 日期训练 / 后 20%% 验证 (test=%d, 验证集正样本 %d)"
            % (res["test_size"], res["n_pos_valid"]),
            "- 先验(正类): %.2f" % prior_pos,
            "",
            "| 指标 | 值 | 随机基线 |",
            "|---|---|---|",
            "| AUC | %.4f | 0.5000 |" % res["auc"],
            "| Average Precision | %.4f | %.4f |" % (res["avg_precision"], base),
            "| precision@50 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_50"], base),
            "| precision@100 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_100"], base),
            "| precision@200 | %.3f | %.3f |" % (res["prec_at_tops"]["prec_at_200"], base),
            "",
            "### 误报复盘 (验证集 top200 false-positive)",
            "见 `reports/miss_predictions.csv`。"]
    report_file.write_text("\n".join(rows), encoding="utf-8")

    miss = res["misses"].sort_values("proba", ascending=False).head(200)
    miss = miss[miss["label"] == 0]
    miss.to_csv(reports_dir / "miss_predictions.csv", index=False, encoding="utf-8-sig")
    logger.info("train done in %.1fs. model=%s report=%s",
                time.time() - t0, model_file, report_file)


def predict(kline_dir: Path, cands_dir: Path, model_file: Path, top_n: int, asof: str | None):
    with open(model_file, "rb") as fh:
        payload = joblib.load(fh)
    model = payload["model"]
    features = payload["features"]

    cand_rows = []
    for path in sorted(Path(kline_dir).glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
            if len(df) < 80:
                continue
            df = clean_volume(df)
            df = add_base_columns(df)
            code = str(df["code"].iloc[0])
            from stock_selector.features.window import compute_window_features

            feat = compute_window_features(df, code)
            lab = assign_labels(df, code)
            row = df.merge(feat, on="date").merge(lab, on="date").dropna(subset=features)
            if row.empty:
                continue
            if asof is not None:
                row = row[row["date"] <= asof]
            latest = row.iloc[-1]
            x = latest[features].to_numpy(dtype=float).reshape(1, -1)
            proba = float(model.predict_proba(x)[0, 1])
            cand_rows.append({
                "code": code, "date": latest["date"], "label": latest.get("label"),
                "proba": proba,
            })
        except Exception as exc:  # 单票失败跳过
            logger.warning("[SKIP] %s: %s", path.name, exc)

    out = pd.DataFrame(cand_rows).sort_values("proba", ascending=False).head(top_n)
    _g(cands_dir)
    fname = (asof or pd.Timestamp.today().strftime("%Y-%m-%d"))
    target = cands_dir / f"{fname}.parquet"
    out.to_parquet(target, index=False, compression="zstd")
    logger.info("Top%d 候选已写入 %s", top_n, target)
    print(out[["code", "date", "proba"]].to_string(index=False))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="连板吸筹效应识别 · GaussianNB 训练/预测")
    parser.add_argument("--train", action="store_true", help="全流程训练+验证报告")
    parser.add_argument("--predict", action="store_true", help="输出当日 TopN 候选")
    parser.add_argument("--code", type=str, default=None, help="单票特征/打分诊断")
    parser.add_argument("--top-n", type=int, default=100, help="预测候选数(默认100)")
    parser.add_argument("--date", type=str, default=None, help="预测基准日 (YYYY-MM-DD)")
    parser.add_argument("--prior-pos", type=float, default=DEFAULT_PRIOR, help="正类先验(默认0.15)")
    parser.add_argument("--kline-dir", type=str, default=str(KLINE_DIR))
    parser.add_argument("--models-dir", type=str, default=str(MODELS_DIR))
    parser.add_argument("--reports-dir", type=str, default=str(REPORTS_DIR))
    parser.add_argument("--model-file", type=str, default=None)
    parser.add_argument("--report-file", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    models_dir = Path(args.models_dir)
    reports_dir = Path(args.reports_dir)
    model_file = Path(args.model_file) if args.model_file else models_dir / "bayes_limitup.joblib"
    report_file = Path(args.report_file) if args.report_file else reports_dir / "bayes_train_report.md"

    if sum([args.train, args.predict, args.code is not None]) != 1:
        parser.error("--train/--predict/--code 三者必须且只能选一个")
    if args.code:
        diag_code(args.code, Path(args.kline_dir))
    elif args.train:
        train(Path(args.kline_dir), models_dir, reports_dir, model_file, report_file, args.prior_pos)
    elif args.predict:
        predict(Path(args.kline_dir), CAND_DIR, model_file, args.top_n, args.date)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_train_bayes.py -q`
Expected: `3 passed`

> 调试提示：若 `TB-03` 报 sklearn 未安装 → Task 6 尚未执行，先装依赖再跑本任务测试。**调用 `systematic-debugging`** 核对报错栈第一行决定。

- [ ] **Step 5: Commit**

```bash
git add scripts/train_bayes.py tests/test_train_bayes.py
git commit -m "feat(train_bayes): GaussianNB 训练/预测/单票诊断 CLI"
```

---

### Task 6: 依赖 + 全回归

**Files:**
- Modify: `requirements.txt`（追加两行）

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Level | Command |
|---|---|---|---|---|---|---|---|
| DEP-01 | 设计§8 | env | `python -c "import sklearn, joblib"` | 无异常；版本可打印 | manual/env | — | `python -c "import sklearn, joblib; print(sklearn.__version__)"` |

- [ ] **Step 1: 修改 `requirements.txt` 末尾追加**

```
scikit-learn
joblib
```

- [ ] **Step 2: 安装依赖**

Run: `python -m pip install scikit-learn joblib`
Expected: 安装成功（requirements 新增两包）

- [ ] **Step 3: 验证可导入**

Run: `python -c "import sklearn, joblib; print(sklearn.__version__)"`
Expected: 打印版本号，无 ModuleNotFoundError

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: 全部通过（既有 backtest/news 测试 + 本计划新增 ~23 个用例）

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): 增加 scikit-learn/joblib"
```

---

### Task 7: 真实数据训练 + 楚天龙诊断（运行期验证，非纯单测）

**Files:**
- Run: `python scripts/train_bayes.py --train`
- Run: `python scripts/train_bayes.py --code 003040`

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Level | Command |
|---|---|---|---|---|---|---|---|
| RV-01 | 设计§10 | regression | `data/stock_kline/` 5186 个 parquet 就绪 | 生成 `models/bayes_limitup.joblib`、`reports/bayes_train_report.md`、`reports/miss_predictions.csv`；报告含 precision@50/100/200 与基线 | manual(运行验证) | E2E | `python scripts/train_bayes.py --train` |
| RV-02 | 设计§13 | regression | 003040 存在于 kline_dir | `--code 003040` 打印 12 特征 + label，不抛错；其 label 行存在 | manual | E2E | `python scripts/train_bayes.py --code 003040` |
| RV-03 | 设计§10 | regression | 模型已生成 | 生成 `data/bayes_candidates/2026-09-11.parquet`（或 --date 指定日），rows ≤ top_n，proba 降序 | manual | E2E | `python scripts/train_bayes.py --predict --top-n 100` |

- [ ] **Step 1: 运行真实训练**

> ⚠️ 大数据量（约 97 万候选样本 × 12 特征），多进程构建，预计 5~20 分钟。

```bash
python scripts/train_bayes.py --train
```

Expected:
- 日志出现 `train done in XXs`，无 SKIP 风暴（个位数 SKIP 属正常容错）
- `models/bayes_limitup.joblib` 与 `reports/bayes_train_report.md` 生成
- 报告内 `AUC` 与 `precision@50/100/200` 与随机基线（0.53%）对比可读

> 若 `[SKIP]` 大量出现 → 检查 kline_dir 文件是否损坏。**调用 `systematic-debugging`** 看 SKIP 首条原因。

- [ ] **Step 2: 楚天龙单票诊断**

```bash
python scripts/train_bayes.py --code 003040
```

Expected: 打印 003040 最新交易日 12 特征；对照设计§5 特征含义核对 `test_limit_cnt`/`max_gain_3d` 与 8 月连板段相吻合（若有历史窗口）

- [ ] **Step 3: 生成当日候选**

```bash
python scripts/train_bayes.py --predict --top-n 100
```

Expected: 打印 Top100 表；`data/bayes_candidates/2026-09-11.parquet` 生成，`proba` 降序

- [ ] **Step 4: 阅读报告并截图式保存结果**

```bash
Get-Content reports\bayes_train_report.md
```

Expected: 报告完整。将 AUC / prec@topN 数值抄录到提交说明中（供复盘）。

- [ ] **Step 5: Commit（若产物纳入版本库；默认产物不入库，仅当模型较小且你认为值得时）**

```bash
git add models/bayes_limitup.joblib reports/bayes_train_report.md
git status
```

> 默认不提交产物（模型文件为生成物）。若 `git status` 显示无需入库内容，跳过本步即可。

---

### Task 8: 全量重建 build_kline_features（让 5186 只文件带上新列）

**Files:**
- Run: `python scripts/build_kline_features.py --full`（需 MongoDB 可达；若不达，说明并降级为增量说明）

**Test Cases:**

| ID | Source | Type | Preconditions/Input | Expected Assertions | Automation | Level | Command |
|---|---|---|---|---|---|---|---|
| RB-01 | 设计§6.1 | regression | MongoDB 可达；5186 只 | `logs/kline_features_YYYYMMDD.log` 显示 processed≈5186（或接近），failed 极少；抽样 `data/stock_kline/600519.parquet` 读取含 pct/lu/vol_ratio/pos60 4 列 | manual | E2E | `python scripts/build_kline_features.py --full` |

- [ ] **Step 1: 确认 MongoDB 可达**

```bash
python -c "from pymongo import MongoClient; c=MongoClient(host='127.0.0.1',port=27017,serverSelectionTimeoutMS=5000); print(c.server_info()['version'])"
```

Expected: 打印 Mongo 版本号。若 ConnectionFailure → 停止本任务，向用户说明：`--full` 需 Mongo；但 train_bayes 的 `build_dataset` 有 add_base_columns 现算兜底，不影响训练结果，仅文件内无新列。

- [ ] **Step 2: 全量重建**

```bash
python scripts/build_kline_features.py --full
```

> ⚠️ 全量 5186 只逐只重算，预计 30 分钟~2 小时。可改用 `--limit` 分批验证后再全量。

Expected:
- 日志：`完成。processed≈5186 skipped=0 failed≈0 total=5186`
- 若中途失败，记录 failed 数并重跑（脚本内置重试断点：已写最新 date 的股票会被 skip）

- [ ] **Step 3: 抽样验证 4 列落盘**

```bash
python -c "import pandas as pd; df=pd.read_parquet(r'data\stock_kline\600519.parquet'); print([c for c in ('pct','lu','vol_ratio','pos60') if c in df.columns]); print(df[['date','pct','lu','vol_ratio','pos60']].tail(3).to_string())"
```

Expected: 4 列名全部打印；尾行 pct 非空、lu 为 bool、vol_ratio/pos60 数值合理

- [ ] **Step 4: 收尾说明**

```bash
# 若 Mongo 可达且成功重建，可在 commit 中记录（可选，按需执行）：
git status data/stock_kline/
git commit -m "chore(data): 全量重建 parquet，新增 pct/lu/vol_ratio/pos60 列" --dry-run

# 若跳过（Mongo 不可达），打印说明即可：
echo "Mongo 不可达，全量重建已跳过；train_bayes 已自动通过 add_base_columns 现算补齐缺失列，功能不受影响。"
```

---

## 2. 验收标准汇总

1. `python -m pytest tests/ -q` 全部通过（含新增 ~23 用例）
2. `models/bayes_limitup.joblib`（含 model/features/prior_pos）+ `reports/bayes_train_report.md`（AUC + precision@50/100/200 对比基线）+ `reports/miss_predictions.csv` 生成
3. `data/bayes_candidates/YYYY-MM-DD.parquet` 每日候选可生成
4. `--code 003040` 单票诊断可用，输出 12 特征
5. 全量重建后 `data/stock_kline/*.parquet` 含 pct/lu/vol_ratio/pos60（或确认降级路径生效）

## 3. 风险与注意

- **Mongo 不可达**：Task 8 `--full` 无法执行 → 降级为现算兜底，功能不受影响
- **样本不平衡**：全样本训练 + 正类先验 0.15；输出概率为排序打分，非真实后验，评估只看排序指标
- **运行时长**：Task 7 训练 5~20 分钟、Task 8 全量 0.5~2 小时，务必用超时充足的终端会话执行
- **候选门槛与特征重叠**：pct_20d/pos_60d/limit_up_early 在候选集内区分度有限（设计§9 已知局限），AUC 预期主要来自量能/试盘特征