"""买入风险分：只用来**剔除**候选，不用来排序。

模型预测"这只票从今天收盘买入、持有 10 日的同日基线超额收益"（基线=当日全体票-日均值，
已扣市场 β）。训练与验证见 `scripts/train_risk_model.py`，产出 `data/models/risk_ex10.*`。

样本外证据（1710 只、2016-2026、walk-forward 四折、只用非涨停可成交行）：
预测分最低的 30% 一批，10 日超额 **-0.74pp（t=-18.6）**，2019-2026 **8 个年度全部为负**，
四个子时期 t 分别为 -6.8/-11.1/-13.3/-9.4。这是整轮涨停/因子研究里唯一过了
"年代 + 四子时期"双签的结果，所以它的定位是风控闸门，不是买点。

组合级验证（2026-10-11，见 README《涨停与风险分》）：把它接到 trend_rebound 的命中池上，
按策略自己的出场规则（跌破成本 5% 止损，否则第 10 日收盘走）算，10 日超额从
-0.18pp 变成 +0.14pp，配对提升 **+0.28pp/10 日（t=+5.7），8 个年度全为正**；
全市场口径下"剔掉最低 30% 那一档"值 **+5.4%/年**，且对成本档不敏感（剔除不加换手）。

用它做排序是另一回事：高分腿的历史表现随样本口径变化很大，交付时不承诺"分高就该买"。

失败姿态（与数据源一致，宁可显式报错也不静默放行）：
  - 模型文件缺失 → RuntimeError，提示先跑训练脚本；
  - meta 里的特征清单与当前 `risk_features.FEATURE_NAMES` 不一致 → RuntimeError，
    否则列序错位会给出看起来合理、实际无意义的分数。
行数不足（`MIN_BARS`）或整行大面积缺失（新股/长期停牌）时返回 None：信息不够就不判，
让候选照常通过。个别列为 NaN（从未涨停 → age/lu20 缺失）不影响打分，训练时同理。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .risk_features import FEATURE_NAMES, MIN_BARS, RAW_COLUMNS, build_features

logger = logging.getLogger("stock_selector")

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL_DIR = ROOT / "data" / "models"
MAX_NAN_SHARE = 0.34      # 一行里超过三分之一的特征缺失就认为不可判
SCORE_ROWS = 700          # 实盘回溯根数（远大于 MIN_BARS，理由见 required_rows）

_cache: dict[str, tuple[object, dict, float]] = {}


def _model_paths(model_dir: Path) -> tuple[Path, Path]:
    return (model_dir / "risk_ex10.joblib", model_dir / "risk_ex10.meta.json")


def _resolve_dir(raw: str | Path | None) -> Path:
    """相对路径按仓库根解析，避免服务从不同工作目录启动时找不到模型。"""
    path = Path(raw) if raw else DEFAULT_MODEL_DIR
    return path if path.is_absolute() else ROOT / path


def load(model_dir: str | Path | None = None) -> tuple[object, dict, float]:
    """加载（并缓存）模型、元数据与绝对剔除阈值。

    缓存按文件 mtime 失效：重新训练后不必重启服务，下一次打分就用新模型。
    """
    directory = _resolve_dir(model_dir)
    model_path, meta_path = _model_paths(directory)
    if not model_path.is_file() or not meta_path.is_file():
        raise RuntimeError(
            f"风险分模型不存在（{model_path}）。先执行 "
            "python3 scripts/train_risk_model.py 生成 data/models/risk_ex10.*"
        )
    key = str(model_path)
    stamp = model_path.stat().st_mtime
    if key in _cache and _cache[key][3] == stamp:
        return _cache[key][:3]

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if list(meta.get("features") or []) != FEATURE_NAMES:
        raise RuntimeError(
            f"风险分模型与当前特征清单不一致（模型 {len(meta.get('features') or [])} 列 / "
            f"代码 {len(FEATURE_NAMES)} 列）。列序错位会静默给出错误分数，"
            "请用 python3 scripts/train_risk_model.py 重新训练。"
        )
    threshold = float(meta["reject_threshold_ex10"])
    bundle = (joblib.load(model_path), meta, threshold, stamp)
    _cache[key] = bundle
    logger.info("风险分模型已加载: 训练于 %s，样本外 %s 只/%s 行，阈值 %.6f",
                meta.get("trained_at"), meta.get("n_stocks"), meta.get("n_rows"), threshold)
    return bundle[:3]


def required_columns() -> list[str]:
    """打分需要的原始列（交给数据源剪列清单）。"""
    return list(RAW_COLUMNS)


def required_rows() -> int:
    """打分需要的回溯根数：比 MIN_BARS 大得多，是为了 `age`。

    `age` = 距上次涨停的根数，训练时用的是全历史，窗口内没有涨停就会被截成 NaN。
    实测（284 只抽样，与全历史分数的差）：窗口 260 根时 6/284 条剔除判定翻转、最大偏移
    0.37pp；700 根时 0/258 翻转、最大偏移 0.20pp。parquet 是按整文件读进来再截断的，
    多取行数不增加 IO，所以直接取 700。
    """
    return SCORE_ROWS


def score(kline: pd.DataFrame, model_dir: str | Path | None = None) -> float | None:
    """给出基准日（最后一行）的预测 10 日同日基线超额收益；信息不足返回 None。

    NaN 不必清空：`age`/`lu20` 在从未涨停的票上天然缺失，`updn` 在 20 日内没有阴线时缺失，
    训练时同样是 NaN，HistGradientBoosting 会把缺失值当成一个分支自己学。
    只有整行大面积缺失（新股、长期停牌后复牌）才拒判——那时分数没有意义。
    """
    if kline is None or len(kline) < MIN_BARS:
        return None
    missing = [c for c in RAW_COLUMNS if c not in kline.columns]
    if missing:
        logger.debug("风险分缺少列 %s，跳过打分", missing)
        return None
    model, _, _ = load(model_dir)
    values = build_features(kline).iloc[[-1]].to_numpy(dtype="float32")
    if np.isnan(values).mean() > MAX_NAN_SHARE:
        logger.debug("风险分特征缺失 %.0f%%，跳过打分", 100 * np.isnan(values).mean())
        return None
    return float(model.predict(values)[0])


def decide(kline: pd.DataFrame, model_dir: str | Path | None = None,
           threshold: float | None = None) -> tuple[bool, float | None]:
    """返回 (是否建议剔除, 分数)。分数不可得时不剔除。"""
    value = score(kline, model_dir)
    if value is None:
        return False, None
    cut = threshold if threshold is not None else load(model_dir)[2]
    return value < cut, value
