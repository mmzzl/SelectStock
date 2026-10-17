"""可信度分箱共用件：板块归类 + (bias20, pos60, vol_ratio) 三分位落箱。

这段是**建表脚本与线上插件共用的唯一真源**，两边都从这里取 `assign_bin` / `bin_values`，
否则「线上今天落在第 2 箱、表里那格是按第 1 箱统计的」这种错配不会报错，只会悄悄给出错的数字。

落箱口径（edges 按升序 [e0, e1]）：
    0 → x <= e0
    1 → e0 < x <= e1
    2 → x > e1
非有限值（NaN/±inf/None）落 -1，表示「这个特征不可判」，调用方据此退化而不是硬塞进某一箱。
"""

from __future__ import annotations

import numpy as np

BOARD_MAIN = "主板"
BOARD_GEM = "创业板"      # 300/301
BOARD_STAR = "科创板"     # 688
BOARD_BJ = "北交所"       # 8xx/4xx/920
BOARD_FUND = "场内基金"   # 15/16/5x ETF·LOF·REITs

# 场内基金代码段。它不是「小类」：同样的 +10%/-5%/10 日屏障，基金的止盈率比股票低 8pp
# （实测 22.6% vs 30.9%），归进主板会把股票格子的数字整体拉偏，比它自己宣称的 CI 还宽。
FUND_PREFIXES = ("15", "16", "50", "51", "52", "53", "55", "56", "58")

# 可下单股票池的代码段（正向白名单）：沪 60/68、深 00/30。写成正向而不是「非基金」，
# 因为池子还要顺手把北交所（8/4/920）挡在外面——那是逐笔扫描 v1、合取批次 B 用的同一个口径，
# 换池子等于换自由度，所以这里只有这一处真源，脚本别再各写一遍元组。
STOCK_PREFIXES = ("60", "68", "00", "30")


def is_stock_code(code) -> bool:
    """60/68/00/30 打头才算股票池；159/51x/52x/55x/56x/58x 是场内基金，8/4/920 是北交所。"""
    return str(code).startswith(STOCK_PREFIXES)


# 建模型/查表都按这个固定顺序编码类别：线上把板块名翻译成下标，
# 不依赖 pandas categorical 的推断顺序（换了 pandas 版本也不会串位）。
BOARD_CATEGORIES = (BOARD_MAIN, BOARD_GEM, BOARD_STAR, BOARD_BJ, BOARD_FUND)

# 参与分箱的三个特征（顺序即建表的 key 顺序）
BIN_FEATURES = ("bias20", "pos60", "vol_ratio")
BIN_COLUMNS = {"bias20": "bias_bin", "pos60": "pos_bin", "vol_ratio": "vol_bin"}
# 边缘列前缀（表里落的是 bias_e0/pos_e0/vol_e0，不是完整特征名）
EDGE_PREFIX = {"bias20": "bias", "pos60": "pos", "vol_ratio": "vol"}

N_BINS = 3


def classify_board(code: str) -> str:
    """按代码前缀归入 主板 / 创业板 / 科创板 / 北交所 / 场内基金。

    与 `stock_selector.features.base_columns.board_limit_threshold` 的涨跌停线口径一致，
    但拆成五类（那边把创业板/科创板合成一类、北交所一类）。未知前缀归主板。
    """
    c = str(code)
    if c.startswith(FUND_PREFIXES):
        return BOARD_FUND
    if c.startswith(("300", "301")):
        return BOARD_GEM
    if c.startswith("688"):
        return BOARD_STAR
    if c.startswith(("8", "4", "920")):
        return BOARD_BJ
    return BOARD_MAIN


def board_index(board: str):
    """板块名 → 模型类别下标（训练侧 `pd.Categorical(BOARD_CATEGORIES).codes` 同序）。

    未知板块返回 None：调用方据此退化，而不是硬塞进 0（主板）冒充。
    """
    try:
        return BOARD_CATEGORIES.index(str(board))
    except ValueError:
        return None


def is_st_name(name) -> bool:
    """从证券简称判 ST/*ST（±5% 涨跌停）。

    日线里只有**当前**简称（实测 6631 个文件的 name 列全表恒定），拿它当历史状态会前视，
    所以 ST 只用作**剔除**：建样本/训练时剔掉当前 ST 的代码，线上遇到 ST 持仓直接不给分
    （见 portfolio/conviction.py），而不是套一个在非 ST 样本上学出来的概率。
    """
    return "ST" in str(name or "").upper()


def assign_bin(value, edges) -> int:
    """单个标量落 0/1/2；非有限值返回 -1。edges 为升序 (e0, e1)。

    side='left'：0 → x <= e0、1 → e0 < x <= e1、2 → x > e1（与 pd.cut(right=True) 同形）。
    """
    v = _finite(value)
    if v is None:
        return -1
    return int(np.searchsorted(np.asarray(edges, dtype=float), v, side="left"))


def bin_values(values: np.ndarray, edges) -> np.ndarray:
    """向量化落箱：返回 int16 数组，非有限值 -1。"""
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, -1, dtype=np.int16)
    ok = np.isfinite(arr)
    out[ok] = np.searchsorted(np.asarray(edges, dtype=float), arr[ok], side="left")
    return out


def bucket_key(bias20, pos60, vol_ratio, edges: dict) -> tuple[int, int, int]:
    """(bias_bin, pos_bin, vol_bin)。任一特征不可判时对应位为 -1，查表自然落空。"""
    return (
        assign_bin(bias20, edges["bias20"]),
        assign_bin(pos60, edges["pos60"]),
        assign_bin(vol_ratio, edges["vol_ratio"]),
    )


def edge_columns(feature: str) -> tuple[str, str]:
    """建表时某特征两条边缘的列名（也用于插件从表首行读回 edges）。"""
    prefix = EDGE_PREFIX[feature]
    return f"{prefix}_e0", f"{prefix}_e1"


def _finite(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None
