"""§50 验收仪器：成本锚读数 vs 旧「当日收盘 −10%」读数（直接走插件，量的就是 App 会显示的东西）。

预注册判据在 docs §50.4。这里能自动判的是服务侧那三条：
- **R2 成本锚响应**：给他真实持仓（默认 600272，成本 13.66 ⇒ 线 12.294）最近 N 个交易日的
  新口径读数，要求现价靠近线时**逐日不降**，且末值高于旧口径同期印出的那个数；
- **R6 已破线不编数**：现价 ≤ 线 ⇒ `conviction_pct` 必须是 None 且文案写「已跌破」；
- **R7 不再呆住**：最近 N 个交易日里新口径读数的变化次数（旧口径 26 天只跳 1 档）。

R1/R3/R4/R5 是模型侧的，由 `scripts/train_barrier_model.py` 在训练时算并写进
`conviction_model.json` 的 `barrier.acceptance`；这个脚本会把它们一起抄出来，方便一页看完。

用法（模型落盘后）：
    python scripts/barrier_acceptance.py                       # 600272 成本 13.66 + 601398 成本 8.45
    python scripts/barrier_acceptance.py --code 600272 --cost 13.66 --days 40
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "stock_selector_api" / "plugin"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from portfolio import conviction                                  # noqa: E402
from stock_selector.conviction import features as fx              # noqa: E402

HOLDINGS = {"600272": 13.66, "601398": 8.45}
COLS = ["close", "ma20", "pos60", "vol_ratio", "vol_z", "rsi14", "adx14", "atr_n", "lu",
        "atr14", "res20", "sup20"]


def _row(code: str, tail: pd.DataFrame) -> dict:
    r = tail.iloc[-1]
    out = {c: float(r[c]) for c in COLS if c in r.index}
    out["code"] = code
    out["name"] = str(r.get("name", ""))
    return out


def _history(code: str, kline_dir: Path) -> pd.DataFrame:
    df = pd.read_parquet(kline_dir / f"{code}.parquet").sort_values("date")
    return df.dropna(subset=[f for f in fx.REQUIRED_FEATURES if f in df.columns] or None)


def evaluate_pair(kline_dir: Path, code: str, cost: float, days: int) -> tuple[pd.DataFrame, dict]:
    """逐日并排两口径（都走插件，动作固定 sell：卖侧就是事件原值）。"""
    df = _history(code, kline_dir).tail(days)
    rows = []
    for i in range(len(df)):
        win = df.iloc[: i + 1]
        r = _row(code, win)
        new = conviction.evaluate(r, action="sell", cost_price=cost)
        old = conviction.evaluate({**r}, action="sell")          # 不传成本 = 旧口径
        line = cost * 0.9
        rows.append({"date": str(win["date"].iloc[-1])[:10], "close": r["close"],
                     "距成本%": round((r["close"] / cost - 1) * 100, 1),
                     "碰线还差%": round((1 - line / r["close"]) * 100, 2),
                     "旧(收盘-10%)": old["conviction_pct"], "新(成本线)": new["conviction_pct"],
                     "新文案": new["conviction_text"]})
    table = pd.DataFrame(rows)
    verdict = {"code": code, "cost": cost, "line": round(line, 3), "days": len(table)}
    vals = pd.to_numeric(table["新(成本线)"], errors="coerce")
    verdict["distinct_values"] = int(vals.dropna().nunique())
    verdict["changes"] = int(vals.dropna().drop_duplicates().shape[0])
    verdict["max_up_down_moves"] = int((vals.diff().fillna(0) > 0).sum())
    # R2：靠近线的过程中不许降（只比「碰线还差」在缩小的相邻两日）
    closer = table["碰线还差%"].diff() < 0
    dec = vals.diff() < 0
    verdict["r2_violations"] = int((closer & dec).sum())
    verdict["r6_below_line"] = _r6_case(kline_dir, code, cost)
    return table, verdict


def _r6_case(kline_dir: Path, code: str, cost: float):
    """构造一个「现价已在线下」的行：把最后一行的 close 压到线以下，看插件给不给概率。"""
    df = _history(code, kline_dir).tail(1)
    r = _row(code, df)
    line = cost * 0.9
    r["close"] = line * 0.99
    out = conviction.evaluate(r, action="sell", cost_price=cost)
    return {"pct_is_none": out["conviction_pct"] is None,
            "says_broken": "已跌破" in (out["conviction_text"] or ""),
            "no_probability_word": "概率" not in (out["conviction_text"] or "")}


def main() -> None:
    ap = argparse.ArgumentParser(description="成本锚可信度验收读数（§50.4 R2/R6/R7）")
    ap.add_argument("--kline-dir", default=str(ROOT / "data" / "stock_kline"))
    ap.add_argument("--meta", default=str(ROOT / "data" / "conviction" / "conviction_model.json"))
    ap.add_argument("--code", default=None)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--days", type=int, default=26)
    ap.add_argument("--json", default=None, help="把判据结果写成 json（落档/回归对照用）")
    args = ap.parse_args()

    pack = conviction._package()
    if not pack:
        print("可信度模型读不到（CONVICTION_MODEL_FILE/META 指向哪儿了？）")
        raise SystemExit(2)
    model_on = "barrier_booster" in pack
    print(f"成本锚臂加载：{'是' if model_on else '否（回退旧口径，新读数会全空）'}")
    acc = json.loads(Path(args.meta).read_text(encoding="utf-8")).get("barrier", {}) \
        .get("acceptance", {})
    if acc:
        print("模型侧判据（训练时算的）：", json.dumps(acc, ensure_ascii=False, indent=2)[:1500])

    cases = {args.code: args.cost} if args.code else HOLDINGS
    out = {"model_side": acc, "cases": []}
    for code, cost in cases.items():
        if cost is None:
            continue
        table, verdict = evaluate_pair(Path(args.kline_dir), code, cost, args.days)
        pd.set_option("display.width", 200)
        print(f"\n=== {code} 成本 {cost} ⇒ 线 {cost * 0.9:.3f} ===")
        print(table.drop(columns=["新文案"]).to_string(index=False))
        print("末行文案：", table["新文案"].iloc[-1])
        print("判据：", json.dumps(verdict, ensure_ascii=False))
        out["cases"].append({"code": code, "cost": cost, "verdict": verdict,
                             "tail": table.drop(columns=["新文案"]).to_dict("records")})
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写", args.json)


if __name__ == "__main__":
    main()
