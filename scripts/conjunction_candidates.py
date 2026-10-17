"""conjunction_candidates.py — 生成合取判据的预注册清单（只生成，不跑扫描）。

为什么要合取（2026-10-17，用户定的「二分法」路线）：单条件那一层已经扫到头了 ——
972 条基础条件 × 64 种卖法 = 62,208 格只剩 1 条及格，买入腿 0/952 能独立成立。
「买到冷门」不是某个指标单独的问题，是几个状态同时成立才算冷门，所以这一批改成
两根轴的合取（AND），用二分法把「哪一根轴加了才有效」筛出来。

为什么阈值一个都不新增（这是这批能算数的前提）：60 条合取全部由 v1 已经注册的
12 条档（6 根轴 × 尾档两端）两两组合而成，C(6,2)=15 对 × 4 象限 = 60 条。
组合本身不引入新自由度 —— 我们没有再看一眼数据去挑分位点，所以多重比较的代价
只花在 60 × 64 = 3,840 个格子上（剩余试次预算 173,984 的 2.2%）。

每条都带机制说明（`why`）：说不出「为什么这两个状态一起成立时跟单独立成立不是一回事」
的条目不该进队列。同一轴族的两根轴（vol_ratio × vol_z）那几条写的是稳健性对照，
不是新假设 —— 跑出来只在一边成立就说明读数是口径假象。

用法：
    python3 scripts/conjunction_candidates.py            # 生成 config/rule_candidates_conj.json
    python3 scripts/conjunction_candidates.py --dry-run  # 只打印校验结果

生成后由用户触发扫描（本脚本不跑、也不连 Mongo）：
    python3 scripts/profit_scan.py --inventory-file config/rule_candidates_conj.json ...
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_selector.backtest import rule_expr

INVENTORY = ROOT / "logs/profit_scan/base_conditions.json"
OUT = ROOT / "config/rule_candidates_conj.json"
BATCH_ID = "20261017-conj-v1"
# 与 v1 同一把尺子：这条线之后的数据这批候选一次都不看
HOLDOUT_START = "2021-12-31"
N_PROFILES = 64          # profiles(): 4 种持有期 × 4 止盈 × 4 止损

# 六根轴，每根只取 v1 已注册的两个尾档，阈值原样复用（一个数字都没重挑）
AXES = {
    "pos": {
        "field": "pos60", "name": "60日位置",
        "L": {"id": "n_pos60_q0_10", "tail": "低位"},
        "H": {"id": "n_pos60_q90_100", "tail": "高位"},
    },
    "vr": {
        "field": "vol_ratio", "name": "量比",
        "L": {"id": "n_vol_ratio_q0_10", "tail": "缩量"},
        "H": {"id": "n_vol_ratio_q90_100", "tail": "放量"},
    },
    "vz": {
        "field": "vol_z", "name": "量能Z",
        "L": {"id": "n_vol_z_q0_10", "tail": "量能收缩"},
        "H": {"id": "n_vol_z_q90_100", "tail": "量能放大"},
    },
    "rsi": {
        "field": "rsi14", "name": "RSI14",
        "L": {"id": "n_rsi14_q0_10", "tail": "超卖"},
        "H": {"id": "n_rsi14_q90_100", "tail": "超买"},
    },
    "adx": {
        "field": "adx14", "name": "ADX14",
        "L": {"id": "n_adx14_q0_10", "tail": "无趋势"},
        "H": {"id": "n_adx14_q90_100", "tail": "强趋势"},
    },
    "bias": {
        "field": "close/ma20", "name": "20日线乖离",
        "L": {"id": "p_close_ma20_d5_below", "tail": "线下5%"},
        "H": {"id": "p_close_ma20_d5_above", "tail": "线上5%"},
    },
}

# 15 个轴对 × 4 象限的机制说明，象限顺序固定为 LL / LH / HL / HH（前轴 × 后轴）。
# vz 与 vr 是同一根「成交量能」的相对/绝对两种口径，所以含 vz 的轴对在下方标注为对照。
WHY = {
    ("pos", "vr"): [
        "区间底部 + 无人交易：抛压也不在，是真冷门而不是急跌，等的是量不是价",
        "区间底部 + 放量：低位有人接货，经典建仓形态；也可能是最后一跌的恐慌换手，所以要看卖法",
        "区间顶部 + 缩量：涨不动只能存量对倒，顶部最常见的失力形态",
        "区间顶部 + 放量：高位换手兑现，获利盘出货与新多接力同价，方向要后来才知道",
    ],
    ("pos", "vz"): [
        "同 pos×vr 的 LL，把相对量比换成绝对量能 Z 做口径对照",
        "同 pos×vr 的 LH（对照）",
        "同 pos×vr 的 HL（对照）",
        "同 pos×vr 的 HH（对照）",
    ],
    ("pos", "rsi"): [
        "跌了很久 + 短期也弱：时间成本那一半已就位，缺的是「有没有钱来」",
        "跌了很久 + 短期已冲高：底部刚启动就追，是最容易买在反弹尾巴的一格",
        "高位 + 短期超卖：强势票回调，趋势还在但短期弱，抄底与破位在这一格分不清",
        "高位 + 短期超买：拥挤再加透支，卖出证据最强的一格",
    ],
    ("pos", "adx"): [
        "低位 + 无方向：底部横盘无人问津 —— 本批要检验的核心假设（持仓被时间磨死）",
        "低位 + 强方向：正在趋势性下跌（ADX 不分方向），逆着买等于接刀",
        "高位 + 无方向：高位横盘滞涨，方向消失后靠什么继续涨",
        "高位 + 强方向：主升段，趋势是持有方的朋友",
    ],
    ("pos", "bias"): [
        "区间底部 + 已跌破月线 5%：双重弱势，趋势卖出证据比单看一根足",
        "区间底部 + 站上月线 5%：整段位置低但刚从均线下方拉起，底部反转的第一格",
        "区间顶部 + 跌破月线 5%：新高之后回落破线，趋势资金离场",
        "区间顶部 + 高于月线 5%：新高位大幅站上均线，追高与持盈的分歧点",
    ],
    ("vr", "vz"): [
        "两个量能指标同时确认「真缩量」：把冷门状态钉死，避免单个口径的假象",
        "相对缩量但绝对量能放大 = 全场放量而这只票没跟上，相对冷门（背离格）",
        "相对放量但绝对收缩 = 全场缩量而这只票还有成交，相对活跃（背离格）",
        "同时确认「真放量」：稳健判定，与 LL 一起构成本轴族的对照对",
    ],
    ("vr", "rsi"): [
        "无量阴跌：卖压体现在时间上而不是成交量上",
        "轻盘拉升：少量资金就能把 RSI 推高，最容易出假突破",
        "下跌中放量：恐慌换手或主力承接，同一价格两种解释",
        "放量上攻且短期已透支：涨得最猛也最贵的一格",
    ],
    ("vr", "adx"): [
        "死水：缩量 + 无方向，冷门程度的下限",
        "存量趋势推动：趋势靠少数人对倒维持，能不能成交要单独看涨跌停",
        "震荡放量：换手高却不出方向，摩擦成本最高的一格",
        "资金推动的趋势：最像「有人在做」的一格",
    ],
    ("vr", "bias"): [
        "破位后无人补：缩量加均线下方，阴跌继续",
        "轻盘拉高：浮盈靠不住，量能没跟上",
        "跌破均线且有对手盘：卖信号强度最高的一格（真有人在卖）",
        "主升放量：获利盘随时兑现，止盈与持盈的分界",
    ],
    ("vz", "rsi"): [
        "同 vr×rsi 的 LL（绝对量能口径对照）",
        "同 vr×rsi 的 LH（对照）",
        "同 vr×rsi 的 HL（对照）",
        "同 vr×rsi 的 HH（对照）",
    ],
    ("vz", "adx"): [
        "同 vr×adx 的 LL（对照）",
        "同 vr×adx 的 LH（对照）",
        "同 vr×adx 的 HL（对照）",
        "同 vr×adx 的 HH（对照）",
    ],
    ("vz", "bias"): [
        "同 vr×bias 的 LL（对照）",
        "同 vr×bias 的 LH（对照）",
        "同 vr×bias 的 HL（对照）",
        "同 vr×bias 的 HH（对照）",
    ],
    ("rsi", "adx"): [
        "阴跌磨底：短期弱且没有加速，最熬人的一格",
        "趋势性下跌：短期弱叠加方向强，最不该抄底",
        "震荡上沿：冲高即回，趋势不给接力",
        "加速段：涨得最顺也离顶部最近",
    ],
    ("rsi", "bias"): [
        "双超卖（短期弱 + 破线）：均值回复的买入候选",
        "强势票短期回调到位（趋势仍在、短期弱）：向下摊低与加仓的经典分歧格",
        "破位后的急反弹：反抽性质，兑现窗口而不是趋势恢复",
        "超买 + 高乖离：回撤风险最大，提前止盈的候选",
    ],
    ("adx", "bias"): [
        "无方向 + 破线：震荡阴跌，趋势与位置都不站在持有方",
        "无方向 + 线上滞涨：震荡上沿",
        "强趋势 + 破线：趋势明确向下，灾难线之外的止损候选",
        "强趋势 + 高乖离：持盈还是止盈的分歧点，本批主要检验「让利润跑」能不能跑",
    ],
}


def load_sources(path: Path) -> dict[str, dict]:
    inv = json.loads(path.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in inv["conditions"]}
    want = [ax[t]["id"] for ax in AXES.values() for t in ("L", "H")]
    missing = [i for i in want if i not in by_id]
    if missing:
        raise SystemExit(f"基础条件清单缺这些档（不能凭空造阈值）：{missing}")
    return by_id


def check_pairs() -> None:
    """WHY 必须正好覆盖 C(6,2)=15 个轴对，少一对就少四条候选，多一对就是凭空加轴。"""
    want = {tuple(sorted(p)) for p in itertools.combinations(AXES, 2)}
    have = {tuple(sorted(p)) for p in WHY}
    if want != have:
        raise SystemExit(f"轴对不齐，缺 {sorted(want - have)}，多 {sorted(have - want)}")
    for k, v in WHY.items():
        if len(v) != 4:
            raise SystemExit(f"{k} 的象限说明不是 4 条")


def build(by_id: dict[str, dict]) -> list[dict]:
    conds: list[dict] = []
    for (ta, tb), whys in WHY.items():
        for (sa, sb), why in zip(list(itertools.product("LH", "LH")), whys):
            ca, cb = by_id[AXES[ta][sa]["id"]], by_id[AXES[tb][sb]["id"]]
            conds.append({
                "id": f"c_{ta}{sa}_{tb}{sb}",
                "kind": "conjunction",
                "field": f"{AXES[ta]['field']}+{AXES[tb]['field']}",
                "ref": "",
                "expr": f"{ca['expr']} and {cb['expr']}",
                "label": (f"{AXES[ta]['name']}{AXES[ta][sa]['tail']} 且 "
                          f"{AXES[tb]['name']}{AXES[tb][sb]['tail']}"),
                "threshold": None,
                # 合取没有「分位档」这个概念（联合触发率要跑完才知道），留空让 band_key 按 id 排
                "percentile": None,
                "sample_trigger_pct": None,
                "fields": sorted(set((ca.get("fields") or rule_expr.validate(ca["expr"]))
                                     + (cb.get("fields") or rule_expr.validate(cb["expr"])))),
                "why": why,
                "sources": [{"id": ca["id"], "expr": ca["expr"]},
                            {"id": cb["id"], "expr": cb["expr"]}],
            })
    ids = [c["id"] for c in conds]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"生成的条件 id 有重复：{sorted(set(i for i in ids if ids.count(i) > 1))}")
    for c in conds:
        got = rule_expr.validate(c["expr"])          # 文法不过就立刻炸，别留给扫描时
        if sorted(got) != c["fields"]:
            raise SystemExit(f"{c['id']} 字段对不上：validate={sorted(got)} fields={c['fields']}")
    return conds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", default=str(INVENTORY), help="v1 基础条件清单 JSON")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--force", action="store_true", help="覆盖已存在的批次文件")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    check_pairs()
    conds = build(load_sources(Path(args.inventory)))
    n = len(conds)
    if n != 60:
        raise SystemExit(f"预期 60 条合取，实际 {n} 条")
    doc = {
        "batch_id": BATCH_ID,
        "name": "合取判据 v1（预注册批次，跑前不加不减）",
        "note": ("60 条全部由 v1 已注册的 12 条尾档两两 AND 而成，阈值一个都没新增："
                 "组合不引入新自由度，多重比较只花在 60×64=%d 个格子上。"
                 "跑之前不许按读数增删条目；expr 文法与线上判定同一真源 stock_selector/backtest/rule_expr.py。"
                 % (n * N_PROFILES)),
        "holdout_start": HOLDOUT_START,
        "source_inventory": str(Path(args.inventory)),
        "n_axes": len(AXES),
        "n_axis_pairs": len(WHY),
        "n_conditions": n,
        "n_profiles": N_PROFILES,
        "n_cells": n * N_PROFILES,
        "conditions": conds,
    }
    print(f"轴对 {len(WHY)} × 象限 4 = {n} 条合取，× {N_PROFILES} 卖法 = {n * N_PROFILES} 格")
    if args.dry_run:
        for c in conds[:4]:
            print(f"  {c['id']:16s} {c['expr']:52s} [{','.join(c['fields'])}]  {c['label']}")
        print("  ...")
        return 0

    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} 已存在（预注册批次一旦定稿就不该改）。要重写加 --force。")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {out}（只生成，扫描没跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
