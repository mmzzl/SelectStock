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
