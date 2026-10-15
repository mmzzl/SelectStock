"""portfolio/conviction.py — 直读 LightGBM 可信度模型，给持仓诊断补一组可核对的数字。

口径（全部来自 scripts/train_conviction_model.py 写出的 conviction_model.json，别在这里改数）：
- `conviction_pct` = 模型分数经**样本外分位档校准**后的实测止盈率，含义是
  P(未来 horizon 根内先触 close*(1+take) | 先触 take 或先触 stop 之一，同根双触算止损)。
  中性窗口（两边都没触，占 ~44%）已剔除，所以它是**条件概率，不是期望收益**。
- 文案里的 N 是「去重叠 + 留 embargo 的独立窗口数」：相邻交易日样本共用 9/10 根前向 bar，
  旧的 81 格 Beta 表把它们当独立样本，CI 窄了 3.16 倍（73 个可信格只剩 28 个）。
- 场内基金（15/16/5x 段）单成一类，不再并进主板——同样屏障下基金止盈率比股票低 8pp。
- ST 未建模（日线只有当前简称，历史 ST 状态不可回补，而 ±5% 涨跌停会变形屏障几何）：
  直接不给分，而不是套一个在非 ST 样本上学出来的概率。
- 特征装配走 `stock_selector/conviction/features.py` 的 `row_to_vector`，与训练同源。

退化姿态：模型缺失/读失败/核心特征不齐/ST → 数字字段返回 None（绝不崩、也不编一个数）。

`game_v` / `game_s` / `game_text` 是 **定性参考值、非精确量化**：只用最新一行现成的
vol_ratio（量能承接强弱）和 close 相对 res20/sup20（上方筹码压力）拍一个 0~1 的粗档位。
它们不是精确量化指标，历史数据也不完整（sup20/res20 常缺、vol_ratio 没有横截面分位），
只用于页面给个方向感，不能当交易依据。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from stock_selector.conviction import bucket, features

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "conviction"
DEFAULT_MODEL = DATA_DIR / "conviction_model.lgb.txt"
DEFAULT_META = DATA_DIR / "conviction_model.json"

_cache: dict | None = None
_cache_key: tuple = ()


def _path(env: str, default: Path) -> Path:
    return Path(os.environ.get(env) or default)


def evaluate(row: dict | None) -> dict:
    """最新指标行 → 冻结契约那 7 个字段（缺数据时为 None，调用方直接 update）。"""
    fields = {
        "conviction_pct": None,
        "conviction_level": None,
        "conviction_text": None,
        "take_profit_price": None,
        "game_v": None,
        "game_s": None,
        "game_text": None,
    }
    if not row:
        return fields
    fields["game_v"], fields["game_s"], fields["game_text"] = _game(row)

    pack = _package()
    if not pack:
        return fields
    meta, booster = pack["meta"], pack["booster"]
    contract = meta["label_contract"]
    close = _num(row.get("close"))
    if close is not None:
        fields["take_profit_price"] = round(close * (1.0 + contract["take"]), 2)

    if bucket.is_st_name(row.get("name")):
        return fields                                   # ST 没建模 → 不给分，也不冒充同类
    vec, ok = features.row_to_vector(row)
    if not ok:
        return fields                                   # 核心特征不齐 → 不猜
    score = float(booster.predict([vec], num_iteration=meta["num_iteration"])[0])
    seg = _lookup(meta["calibration"], score)
    if seg is None:
        return fields
    board = bucket.classify_board(row.get("code", ""))
    base = meta.get("base_rate_by_board_pct", {}).get(board) or meta["base_rate_pct"]
    pct = round(seg["rate"] * 100.0, 1)
    fields["conviction_pct"] = pct
    fields["conviction_level"] = _level(pct, base)
    fields["conviction_text"] = _text(pct, seg, meta, base)
    return fields


def _level(pct: float, base_pct: float) -> str:
    """相对**同类（同板块）基准**的高/中/低。

    剧本成功率天然在 ~30% 量级，绝对阈值会把所有票判成「低」；而基金摸 +10% 本来就比股票难
    （样本外 22.6% vs 30.9%），拿全市场均值当基准又会让所有基金永远「低」。所以基准按板块取。
    """
    if base_pct <= 0:
        return "低"
    if pct >= base_pct * 1.05:
        return "高"
    if pct <= base_pct * 0.95:
        return "低"
    return "中"


def _text(pct: float, seg: dict, meta: dict, base_pct: float) -> str:
    """例：剧本成功率：后10日先触+10%的概率32.7%（样本外同档独立窗口N=12,340实测，同类基准30.9%，模型AUC0.54）

    N 是「所有板块里落到同一预测档」的样本外窗口数（校准阶梯按全样本分箱，不分板块），
    所以「同类」只修饰基准那一项——基准才是按板块取的，别把 N 也说成同类。
    """
    c = meta["label_contract"]
    auc = meta["validation"]["pooled"].get("auc")
    extra = f"，模型AUC{auc:.2f}" if auc is not None else ""
    base = (f"剧本成功率：后{c['horizon']}日先触+{c['take'] * 100:.0f}%的概率{pct:.1f}%"
            f"（样本外同档独立窗口N={seg['n']:,}实测，同类基准{base_pct:.1f}%{extra}）")
    spread = (meta.get("discrimination") or {}).get("spread_pp")
    return base + ("·区分度弱" if spread is not None and spread < 3.0 else "")


def _lookup(table: list, score: float):
    """校准阶梯：落在哪一档取该档实测率；越界用最近档（两端是开放区间）。"""
    if not table:
        return None
    for seg in table:
        if seg["lo"] <= score <= seg["hi"]:
            return seg
    return table[-1] if score > table[-1]["hi"] else table[0]


def _game(row: dict):
    """(game_v, game_s, game_text)：仅用最新一行现成列的**定性代理**，不是精确量化。

    game_v：vol_ratio 粗档——0.5→0、2.0→1 线性夹逼（量能承接越强越接近 1）；
    game_s：close 在 (sup20, res20) 区间中的相对位置——越贴近 res20 上方压力越大。
    任一输入缺失就退回 None，绝不编数。
    """
    vol_ratio = _num(row.get("vol_ratio"))
    game_v = None
    if vol_ratio is not None:
        game_v = round(min(max((vol_ratio - 0.5) / 1.5, 0.0), 1.0), 3)

    close = _num(row.get("close"))
    res = _num(row.get("res20"))
    sup = _num(row.get("sup20"))
    game_s = None
    if close is not None and res is not None and sup is not None and res > sup:
        game_s = round(min(max((close - sup) / (res - sup), 0.0), 1.0), 3)

    parts = []
    if game_v is not None:
        parts.append("承接强" if game_v >= 0.6 else "承接中等" if game_v >= 0.35 else "承接偏弱")
    if game_s is not None:
        parts.append("上方压力偏大" if game_s >= 0.7 else "压力中性" if game_s >= 0.4 else "上方空间开阔")
    return game_v, game_s, "·".join(parts) if parts else None


def _package() -> dict:
    """加载（并缓存）模型 + meta；按两个文件的 mtime+size 失效，缺失返回 {}。"""
    global _cache, _cache_key
    model, meta_path = _path("CONVICTION_MODEL_FILE", DEFAULT_MODEL), \
        _path("CONVICTION_META_FILE", DEFAULT_META)
    try:
        key = (str(model), model.stat().st_mtime, model.stat().st_size,
               str(meta_path), meta_path.stat().st_mtime, meta_path.stat().st_size)
    except OSError:
        return {}
    if _cache is None or _cache_key != key:
        _cache, _cache_key = _load(model, meta_path), key
    return _cache or {}


def _load(model_path, meta_path) -> dict:
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    try:
        import lightgbm as lgb

        booster = lgb.Booster(model_file=str(model_path))
    except Exception as exc:          # 模型没训/正在重建不能让诊断整页报错
        logger.warning("可信度模型读取失败 %s：%s", model_path, exc)
        return {}
    if booster.num_feature() != len(meta["features"]):
        logger.warning("可信度模型列数 %d 与 meta %d 不符，退化为不可用",
                       booster.num_feature(), len(meta["features"]))
        return {}
    return {"meta": meta, "booster": booster}


def _num(v):
    """NaN/None/非数 → None：缺列时让调用方退化，而不是印一个 nan 或硬塞一个 0。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None
