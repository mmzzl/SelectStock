"""「明日榜」影子盘（docs §37.6 开关①）：用 batchW_c1 打分出榜，记两本前瞻账：
E1/E2 两条可执行腿，以及 §37.9 里唯一付得起成本的 **band 组合规则**（前 30 建仓、跌出前 60 才卖）。

为什么存在：批次 W 的 W1 在留出段过了预注册门槛（E1 净 +0.107%/日、t 3.3），但留出段终究是回测。
这条脚本只做纸面：**不进 App、不推送、不动线上模型与 data/rules/active.json**，
每晚把当日榜落进 `data/qlib/nextday_shadow/ledger.jsonl`，次日行情到位后回填 E1/E2，攒够 20 个交易日再对着回测量一次。

口径钉死在这里，别在读数脚本里重新发明：
- 名单 17:30 才出 ⇒ 入场一律 **T+1 开盘价**（E1 当天收盘看 / E2 次日开盘卖），不是 T 收盘（§35.5 那条红线）。
- 打分人口与线上同源（`QLIB_POOL_SOURCE=universe` 时读 `data/qlib/universe.txt`，并按 §30 剔 300/301/688/689）。
- 缺次日行情 = pending，**不按 0 入账**。
- 榜面记前 60 名只给 band 判定用；`buyable` 之外的行不进 E1 那本账（预注册的是前 30）。
- 跨夜的腿（E2、持仓 close→close、基准 close→close）过除权闸门：面板是未复权裸价。

用法：
    python3 scripts/qlib_nextday_shadow.py --list                 # 今天(T=面板末日)出榜
    python3 scripts/qlib_nextday_shadow.py --settle               # 回填所有还缺腿的历史榜
    python3 scripts/qlib_nextday_shadow.py --report               # 前瞻账读数（含同池基准）
    python3 scripts/qlib_nextday_shadow.py --list --settle --report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from stock_selector_qlib import artifacts, nextday, serving  # noqa: E402

MODEL_DIR = REPO / "data" / "qlib" / "model_batchW_c1"
OUT_DIR = REPO / "data" / "qlib" / "nextday_shadow"
LEDGER = OUT_DIR / "ledger.jsonl"
BOOK_RUN_ID: str | None = None      # set_book() 决定；落盘时给每行盖章，也用来挡「换臂盖错账」
BOOK_ADOPT = False                    # --adopt-book：承认这本没标识的老账归本臂
TOPK = 30                           # 与 §37 预注册一致：前 30 建仓，不回头挑 K
BAND = 60                           # §37.9 L3：跌出前 60 名才卖 ⇒ 榜面要记到前 60


def set_book(out_dir, run_id: str | None = None, adopt: bool = False) -> None:
    """一本账 = 一个臂。`write_ledger` 是覆盖式落盘 ⇒ 换模型跑必须先切账本，
    否则 W3 的榜会把 W1 的账整本盖掉。给了 run_id 还会在落盘前挡一道、并给每行盖章。"""
    global OUT_DIR, LEDGER, BOOK_RUN_ID, BOOK_ADOPT
    OUT_DIR = Path(out_dir)
    LEDGER = OUT_DIR / "ledger.jsonl"
    BOOK_RUN_ID = run_id
    BOOK_ADOPT = adopt


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_ledger(rows: list[dict]) -> None:
    if BOOK_RUN_ID:
        old = read_ledger()
        others = {r.get("run_id") for r in old if r.get("run_id") and r["run_id"] != BOOK_RUN_ID}
        unstamped = [r for r in old if not r.get("run_id")]
        if others or (unstamped and not BOOK_ADOPT):
            who = "、".join(sorted(others)) if others else f"{len(unstamped)} 行没有模型标识"
            raise RuntimeError(
                f"{LEDGER} 是别人的账（{who}）⇒ 换臂要另开 --out-dir；"
                f"确认这本归本臂就用 --adopt-book")
        rows = [{**r, "run_id": BOOK_RUN_ID} for r in rows]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    tmp.replace(LEDGER)


def bin_series(field: str, inst: str, days: list[str]) -> pd.Series | None:
    """读 qlib bin 面板的一列（裸价），返回按交易日索引的 Series；文件不在就 None。"""
    p = Path(serving._qlib_dir()) / "features" / inst.lower() / f"{field}.day.bin"
    if not p.exists():
        return None
    a = np.fromfile(p, dtype="<f")
    if a.size < 2:
        return None
    start = int(a[0])
    n = min(len(days) - start, a.size - 1)
    vals = np.full(len(days), np.nan)
    vals[start:start + n] = a[1:1 + n]
    return pd.Series(vals, index=days)


def pick_pool(art: artifacts.Artifacts, pool_source: str) -> list[str]:
    """出榜人口 = 基准腿人口，必须是同一个函数算出来的两份，否则"超额"就是比的两个人群。

    §30 的板块剔除（300/301/688/689）在这里生效——买不了的票既不进榜，也不进基准。
    """
    if pool_source == "universe" and serving.UNIVERSE_FILE.exists():
        syms = [s.strip() for s in serving.UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()
                if s.strip()]
    else:
        syms = list(art.pool)
    return [s for s in syms if not serving.board_excluded(s)]


def write_list(art: artifacts.Artifacts, pool_source: str = "model",
               for_date: str | None = None) -> int:
    """出榜并落账。`for_date` 只用来回补漏跑的那一晚（分数只用 ≤ 该日的因子，不看未来）；
    这样的行标 `mode="backfill"`，report 里与 live 分开算——补记的历史不能当前瞻。"""
    days = serving.calendar()
    t = for_date or days[-1]
    if t not in days:
        raise SystemExit(f"{t} 不在 qlib 交易日历里（面板末日 {days[-1]}）")
    pool = pick_pool(art, pool_source)
    pred, stats = serving.score_frame(art, beg=None, end=t, pool=pool)
    today = pred.loc[t] if t in pred.index.get_level_values("datetime") else pd.Series(dtype=float)
    ranked = nextday.top_by_score(today, BAND)
    if not ranked:
        print(f"[明日榜] {t} 一支都没打到分（stats={stats}）——这是缺陷，不是空仓")
        return 0
    mode = "live" if for_date is None else "backfill"
    # 记到前 60 名是为了判 band（跌出前 60 才卖）；`buyable` 标出真正可买的前 30，
    # 这样 E1 那本账仍然只读预注册过的那 30 支，不会被 31–60 名悄悄改掉。
    rows = [{"signal_date": t, "instrument": c, "rank": i + 1, "score": float(today[c]),
             "buyable": i < TOPK,
             "e1_pct": None, "e2_pct": None, "hold_pct": None, "gap_pct": None,
             "settled": False, "mode": mode}
            for i, c in enumerate(ranked)]
    write_ledger(nextday.merge_ledger_rows(read_ledger(), rows))
    print(f"[明日榜] {t} 出榜 {len(ranked)} 支（前 {TOPK} 可买 · {TOPK + 1}–{BAND} 只作 band 判定，"
          f"{mode}）｜打分行 {stats['scored']} / 票池 {stats['pool']}｜人口口径 {pool_source}"
          f"｜前 3：{', '.join(ranked[:3])}")
    return len(ranked)


def _bars(inst: str, days: list[str], fields: tuple = ("open", "close")) -> dict[str, pd.Series]:
    out = {}
    for f in fields:
        s = bin_series(f, inst, days)
        if s is not None:
            out[f] = s
    return out


def settle(art: artifacts.Artifacts | None = None, pool_source: str = "model") -> int:
    """回填四条腿 + 重放 band 组合账。

    - E1 入场 T+1 开盘、出场 T+1 收盘；E2 出场 T+2 开盘；`hold_pct`＝T 收盘→T+1 收盘（留档）；
      `gap_pct`＝T 收盘→T+1 开盘（§37.13：band 账里当天开盘卖出的那支只吃这一段）。
    - **结算过的行不等于不用再补**：E1 在 T+1 就能算，E2 要到 T+2 ⇒ 早先"settled 就跳过"会把 E2
      永远漏掉（静默缺数），现在只在两条腿都齐时才跳过。
    - 基准腿人口与名单同源（`pick_pool`），缺这条腿就报不出超额。
    - 跨夜的腿（E2 / hold_pct / gap_pct / 基准 e0）都过除权闸门：面板是未复权裸价，
      除权日会跳出一个假暴跌，记进账里就是把脏数据当行情。
    """
    rows = read_ledger()
    if not rows:
        print("[明日榜] 账上是空的，先 --list")
        return 0
    days = serving.calendar()
    ci = {d: i for i, d in enumerate(days)}
    dates = sorted({r["signal_date"] for r in rows})
    pool = pick_pool(art, pool_source) if art is not None else None
    bars_cache: dict[str, dict[str, pd.Series]] = {}
    filled = 0
    base_rows: list[dict] = []
    if pool is None:
        print("[明日榜] ⚠ 没有 artifact ⇒ 只结算名单腿、**同池基准腿这期不补**，"
              "report 的超额会缺这一天（不是 0，是算不出来）")
    for d in dates:
        i = ci.get(d)
        if i is None or i + 1 >= len(days):
            continue                       # 连 T+1 都没到，E1 也算不出来
        d1 = days[i + 1]
        d2 = days[i + 2] if i + 2 < len(days) else None   # T+2 没到只缺 E2，E1 照结
        picks = [r for r in rows if r["signal_date"] == d and nextday.is_pick(r)]
        for r in picks:
            if (r.get("e1_pct") is not None and r.get("e2_pct") is not None
                    and r.get("gap_pct") is not None):
                continue
            cols = bars_cache.setdefault(r["instrument"], _bars(r["instrument"], days))
            o1, c1, c0 = _v(cols.get("open"), d1), _v(cols.get("close"), d1), _v(cols.get("close"), d)
            o2 = _v(cols.get("open"), d2)
            was = r.get("e1_pct") is None
            if r.get("e1_pct") is None:
                r["e1_pct"] = nextday.e1_pct(o1, c1)
            if r.get("e2_pct") is None:
                r["e2_pct"] = nextday.e2_pct(o1, o2)
            if r.get("hold_pct") is None:
                r["hold_pct"] = nextday.hold_pct(c0, c1)
            if r.get("gap_pct") is None:          # 隔夜腿：band 账里「今早卖掉的那一支」只吃这一段
                r["gap_pct"] = nextday.gap_pct(c0, o1)
            if was and r["e1_pct"] is not None:
                r["settled"] = True
                filled += 1
        # 同池等权基准：§37 判的是超额，缺这条腿前瞻账就只能报绝对值
        if pool and _need_pool_row(rows, d):
            leg = {}
            for inst in pool:
                cols = bars_cache.setdefault(inst, _bars(inst, days))
                o1, c1, o2 = (_v(cols.get("open"), d1), _v(cols.get("close"), d1),
                              _v(cols.get("open"), d2))
                if o1 or c1:               # 停牌到 T+1 都没成交 ⇒ 整个成员从基准里缺席，不按 0 摊
                    leg[inst] = (o1, c1, o2, _v(cols.get("close"), d))
            if leg:
                base = nextday.pool_baseline(leg)
                base["signal_date"] = d
                base["n_pool"] = len(leg)
                base_rows.append(base)
                print(f"[明日榜] {d} 同池基准腿落账：E1 {base['e1_pct']:+.3f}%｜E2 "
                      f"{('%+.3f%%' % base['e2_pct']) if base['e2_pct'] is not None else '还没到 T+2'}"
                      f"｜收盘→收盘 {('%+.3f%%' % base['e0_pct']) if base['e0_pct'] is not None else '缺'}"
                      f"（{len(leg)} 支）")
    rows = nextday.merge_ledger_rows(rows, base_rows)
    band_rows = replay_band(rows, make_fetch(days, ci, bars_cache))
    rows = nextday.merge_ledger_rows(rows, band_rows)
    write_ledger(rows)
    pend = sum(1 for r in rows if nextday.is_pick(r) and not r.get("settled"))
    miss = sum(1 for r in band_rows if r["n_open_priced"] < r["n_open"])
    print(f"[明日榜] 本次新结算 {filled} 笔，仍缺腿 {pend} 笔（缺腿不按 0 入账）"
          f"｜band 组合账重放 {len(band_rows)} 天"
          + (f"｜⚠ {miss} 天有开盘簿持仓现取不到隔夜腿（那些票当天的跌涨没进账，见 --report）"
             if miss else ""))
    return filled


def make_fetch(days: list[str], ci: dict[str, int], bars_cache: dict):
    """给 band 账用的取腿钩子：持仓里**掉出榜面**的名字账上没有行情列，得现从面板取。

    没有它，`band_day` 只能把这些票从隔夜腿里掉出去（不当 0，但那天它们的涨跌就不在账上）；
    而 §37.13 量出来这个缺口正好落在隔夜为负的窗口上 ⇒ 影子盘每晚必须传。
    """
    def fetch(d: str, inst: str) -> dict:
        i = ci.get(d)
        if i is None or i + 1 >= len(days):
            return {}
        cols = bars_cache.setdefault(inst, _bars(inst, days))
        d1 = days[i + 1]
        o1, c1, c0 = _v(cols.get("open"), d1), _v(cols.get("close"), d1), _v(cols.get("close"), d)
        return {"gap_pct": nextday.gap_pct(c0, o1), "e1_pct": nextday.e1_pct(o1, c1)}
    return fetch


def replay_band(rows: list[dict], fetch=None) -> list[dict]:
    """把 band 规则（前 30 建仓 / 跌出前 60 才卖 / 明早开盘成交）在账上重放一遍。

    每次跑都从头重放、不落状态文件 ⇒ 补了数据再跑，同一本账给同一串结果。
    """
    out = nextday.replay_band(rows, k=TOPK, band=BAND, fetch=fetch)
    for r in out:
        r["mode"] = "backfill" if _is_backfill(rows, r["signal_date"]) else "live"
    return out


def _need_pool_row(rows: list[dict], d: str) -> bool:
    """基准腿缺、或还缺 E2/收盘→收盘 这两条晚到的腿 ⇒ 要重算（缺数不等于 0，也不能永远不补）。"""
    hit = next((r for r in rows if r["signal_date"] == d and r["instrument"] == nextday.POOL_ROW), None)
    return hit is None or hit.get("e2_pct") is None or hit.get("e0_pct") is None


def _v(s: pd.Series | None, d: str):
    if s is None or d not in s.index:
        return None
    v = s.loc[d]
    return None if pd.isna(v) or v <= 0 else float(v)


def report(min_days: int = 20) -> None:
    rows = read_ledger()
    if not rows:
        print("[明日榜] 账是空的，先 --list")
        return
    by = nextday.daily_excess(rows)
    by = by[by.笔 >= TOPK // 2]                # 半天缺数太多就不进这个序列
    bf = {d for d in by.index if _is_backfill(rows, d)}
    live = by.loc[[d for d in by.index if d not in bf]]
    settled = [r for r in rows if r.get("settled") and nextday.is_pick(r)]
    no_base = [d for d in live.index
               if live.loc[d, "e1"] is None or pd.isna(live.loc[d, "e1"])]
    print(f"=== 明日榜前瞻账：live 出手 {len(live)} 个交易日"
          f"{f'（另有补记 {len(bf)} 天，只当管道核对，不计入前瞻）' if bf else ''}"
          f" / 已结算 {len(settled)} 笔 ===")
    if len(live) < min_days:
        print(f"还没到最小判据 {min_days} 个交易日，下面的数字只能当进度看，不能当结论")
    if no_base:
        print(f"⚠ 其中 {len(no_base)} 天缺同池基准腿（{', '.join(no_base[:3])}{'…' if len(no_base) > 3 else ''}）"
              f"⇒ 这几天只有绝对值、没有超额")
    for leg, name in (("e1", "E1 明早开盘买·当天收盘看"), ("e2", "E2 明早开盘买·次日开盘卖")):
        x = pd.to_numeric(live[leg], errors="coerce").dropna().to_numpy()
        if len(x) < 3:
            print(f"{name}：live 超额只 {len(x)} 天，算不出 t")
            continue
        t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
        abs_ = pd.to_numeric(live["绝对e1"], errors="coerce").dropna().to_numpy()
        print(f"{name}：日均超额 {x.mean():+.3f}pp｜配对 t {t:.1f}｜超额为正的天占 "
              f"{(x > 0).mean() * 100:.1f}%｜名单绝对 {abs_.mean() if len(abs_) else float('nan'):+.3f}%/日"
              f"｜扣 0.12% 往返后 {nextday.net_after_cost(x.mean()):+.3f}pp/日")
    _report_band(rows, min_days)
    print("最近 5 个出手日（e1/e2=超额，绝对e1=名单均值，笔=已结算票数）：")
    if len(by):
        tail = by.tail(5).copy()
        b = _band_frame(rows).reindex(tail.index)
        for col, name in (("excess_pp", "band超额"), ("cost_pp", "band成本"), ("n_hold", "band持仓")):
            if col in b:
                tail[name] = b[col]
        print(tail.to_string(float_format=lambda v: f"{v:.3f}"))
    else:
        print("（账上还没有结算完整的出手日）")


def _band_frame(rows: list[dict]) -> pd.DataFrame:
    b = [r for r in rows if r.get("instrument") == nextday.BAND_ROW]
    if not b:
        return pd.DataFrame()
    return pd.DataFrame(b).set_index("signal_date")


def _report_band(rows: list[dict], min_days: int) -> None:
    """band 组合账（§37.9 L3）：回测里唯一付得起成本的那条规则，前瞻必须验它，不是验 E1。"""
    fd = _band_frame(rows)
    if fd.empty:
        print("--- band 组合账（前 30 建仓 / 跌出前 60 才卖）：还没落账，多半是持仓腿 hold_pct 还缺 ---")
        return
    d = fd[fd["settled"].astype(bool)]
    d = d[[not _is_backfill(rows, x) for x in d.index]]
    print(f"--- band 组合账（前 {TOPK} 建仓 / 跌出前 {BAND} 才卖，T+1 开盘成交）：live {len(d)} 天 ---")
    if len(d) < 3:
        print("天数太少，只当进度看")
        return
    # 超额与成本成对取：单边 dropna 会让两条序列错位，那读数就不知道是谁减谁了
    pair = pd.DataFrame({"ex": pd.to_numeric(d["excess_pp"], errors="coerce"),
                         "cost": pd.to_numeric(d["cost_pp"], errors="coerce").fillna(0.0)}).dropna()
    ex, cost = pair["ex"].to_numpy(), pair["cost"].to_numpy()
    net = ex - cost
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
    tn = net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))
    print(f"日均毛超额 {ex.mean():+.3f}pp｜t {t:.1f}｜为正的天占 {(ex > 0).mean() * 100:.1f}%"
          f"｜日均成本 {cost.mean():.3f}pp｜日均换手 {pd.to_numeric(d['turnover'], errors='coerce').mean() * 100:.0f}%"
          f"｜**扣费后 {net.mean():+.3f}pp/日（t {tn:.1f}，为正占 {(net > 0).mean() * 100:.1f}%）**")
    if d["net_pp"].notna().any():
        w = d.loc[pd.to_numeric(d["net_pp"], errors="coerce").idxmin()]
        print(f"最差一天 {w.name}：扣费后 {float(w['net_pp']):+.2f}pp"
              f"（持仓 {int(w['n_hold'])} 支、买 {int(w['n_new'])} 卖 {int(w['n_exit'])}）"
              f"｜§37.9 已经警示过这条是「多数天赚一点点、个别天挨一刀」的形状")
    _report_legs(d)
    if len(d) < min_days:
        print(f"（判据要 {min_days} 个 live 交易日：扣费后超额 > 0 且 t ≥ 2）")


def _num(fd: pd.DataFrame, col: str) -> pd.Series:
    """老账页里没有这一列（§37.13 之前落的 band 行没有两腿）⇒ 返回全 NaN，不当 0。"""
    if col not in fd:
        return pd.Series(index=fd.index, dtype=float)
    return pd.to_numeric(fd[col], errors="coerce")


def _report_legs(d: pd.DataFrame) -> None:
    """把 band 账拆成「隔夜 + 日内」两条腿打出来，并报警开盘簿里取不到价的缺口。

    §37.13 的教训：卖出那支的隔夜腿一旦漏记，而这段窗口的隔夜本身是负的，账面就凭空多出一截超额。
    """
    ov, inn, np_ = _num(d, "overnight_pct"), _num(d, "intraday_pct"), _num(d, "pool_pct")
    hole = int((_num(d, "n_open") > _num(d, "n_open_priced")).sum())
    print(f"两腿拆解：持仓隔夜（T收→T+1开）{ov.mean():+.3f}%/日"
          f"｜日内（T+1开→T+1收）{inn.mean():+.3f}%/日"
          f"｜同池基准收盘→收盘 {np_.mean():+.3f}%/日")
    if hole:
        print(f"⚠ 有 {hole}/{len(d)} 天开盘簿里的持仓现取不到隔夜腿 ⇒ 那天卖出的票没进账、"
              f"报出来的超额**偏高**（缺的是负的那一段）。先补面板再读这段。")


def _is_backfill(rows: list[dict], d: str) -> bool:
    return any(r["signal_date"] == d and r.get("mode") == "backfill" for r in rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="按面板末日（或 --for-date）出榜并落账")
    ap.add_argument("--for-date", default=None, metavar="YYYY-MM-DD",
                    help="补记漏跑的一晚：只用 ≤ 该日的因子打分，行标 backfill，report 里不计入前瞻")
    ap.add_argument("--settle", action="store_true", help="回填历史榜的 E1/E2")
    ap.add_argument("--report", action="store_true", help="打前瞻账读数")
    ap.add_argument("--model-dir", default=str(MODEL_DIR))
    ap.add_argument("--adopt-book", action="store_true",
                    help="确认这本「行里没有模型标识」的老账归本臂（第一次跑老账本要加一次）")
    ap.add_argument("--out-dir", default=str(OUT_DIR),
                    help="这本账落哪儿（**一个臂一份**：换 --model-dir 就要换这里，"
                         "否则新臂的榜会覆盖旧臂的账）")
    ap.add_argument("--pool", choices=("universe", "model"), default="model",
                    help="打分人口：model=artifact 里冻结的 300 只研究池（**默认**，与 §37 回测同人口，"
                         "前瞻账才能直接对着回读数比；注意这池按 2016-2022 流动性挑、带前视）；"
                         "universe=线上那份 data/qlib/universe.txt（换成它 = 账目与回测不可比，要重新起算）")
    args = ap.parse_args(argv)
    set_book(args.out_dir)
    art = None
    if args.list or args.settle:
        serving.init_qlib()
        art = artifacts.load(Path(args.model_dir))
        set_book(args.out_dir, art.run_id, adopt=args.adopt_book)
        if args.list:
            print(f"[明日榜] 模型 {art.run_id} 标签 {art.meta.get('label')}｜票池 {len(art.pool)}")
            write_list(art, args.pool, args.for_date)
    if args.settle:
        settle(art, args.pool)
    if args.report:
        report()
    if not (args.list or args.settle or args.report):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
