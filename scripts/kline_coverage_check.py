"""K线覆盖率体检（B，docs §48.3）：每票每月覆盖率 + 全场单日票数，两种洞分开抓。

背景是 §48.3 核出的两处静默缺数：`stock_kline` 的日线是 f=9、f=0 两批互补的爬虫写的
（同一 (code,date) 不重复），2024 年有 1,898 只票月度覆盖不足 10 个交易日，另有 883 只票在
f=9 段 2023-01→2026-04 **整段为空**、只靠 f=0 补上。缺数时下游一切照常出结果，没人报警——
按「静默缺数＝缺陷」这条规矩，这里既报也留口子让人补。

两个尺子各管一种病：

- **每票每月覆盖率**＝这票这个月有票的天数 ÷ 这个月**真实的交易日数**（分母来自全场并集，
  不是 21/22 拍脑袋）。整只票没数据、某月被跳过都在这尺子上露头。为避免误报，只报**上月活跃**的票：
  当月新股上市、当月才摘牌的不算洞。
- **薄日**＝某全场并集有行的日子，票数却不足最满那天的九成——那是采集只爬了一半（当天宕机、
  被反爬掐了），跟「整只票缺」是两种病，补法也不一样。
  当天不算：日线爬虫要跑到傍晚（沪市队尾能漂过 17:05），当天必然看着「薄」，天天误报等于没人看。
  代价是当月每票的分子可能已含今天那一根，比例最多乐观一天——一个交易日少了会在第二天还露出来。

**覆盖不足 ≠ 我们的洞**：实测 2026-10-29 那轮 35 个「洞」里 34 个是市场本来就没那些天的票
（002717 上游只到 10-12、512390 到 09-04、688496 到 09-10、301139 到 10-27）。所以每个票·月
拿备源（新浪日线，`kline_spider` 的 fallback）问一次「上游那个月有几天」，拆成
**真缺数 / 停牌退市 / 核不到上游**三类，只有第一类和第三类进告警——按上游只比自己的话，
告警 accuracy 是 1/35，等于天天狼来了，真缺数反而被淹掉。`--no-verify` 退回不核对（全记核不到）。

告警走 `alert_pusher` 的 `ding_ops` 渠道，同一天只推一条；默认不真发（`--alert` 才发），
发送失败不记日期、下一轮重试。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pymongo
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COL_KLINE = "stock_kline"
FREQUENCIES = (0, 9)          # 两批互补：覆盖率必须并起来算
STATUS_PATH = ROOT / "data" / "kline_coverage" / "status.json"
THRESHOLD = 0.9               # 月度覆盖率低于九成算洞
THIN_RATIO = 0.9              # 单日票数不足最满那天九成算薄日
MONTHS = 3                    # 默认只看最近三个月（当月还在走，分母用它自己的交易日数）
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/CN_MarketData.getKLineData")
UPSTREAM_BARS = 250           # 备源给「最近 N 根」，250 根 ≈ 一年，够包住三个月窗口
REQUEST_TIMEOUT = 15

logger = logging.getLogger("kline_coverage")


def month_of(value) -> str:
    """"2026-10-29 15:00" 或 "2026-10-29" → "2026-10"。"""
    return str(value)[:7]


def month_window(today: str, n: int) -> tuple[str, str]:
    """最近 n 个自然月的 [起, 止) 日期串（含当月）。`date` 是字符串，比较走字典序就够。"""
    idx = _to_idx(today[:7])
    return _from_idx(idx - n + 1) + "-01", _from_idx(idx + 1) + "-01"


def _to_idx(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def _from_idx(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def prev_month(month: str) -> str:
    return _from_idx(_to_idx(month) - 1)


def holes(monthly: dict, need: dict, threshold=THRESHOLD, report_months=None) -> list[dict]:
    """找覆盖率不达标的 (票, 月)，只报上月活跃的股票。

    `monthly` 键是 (code, month)、值是这票这个月有票的**天数**；`need` 是这些月的全场交易日数。
    """
    months = list(report_months) if report_months else sorted(need)
    out = []
    for (code, month), have in monthly.items():
        if month not in months:
            continue
        required = need.get(month) or 0
        if not required or have / required >= threshold:
            continue
        before = monthly.get((code, prev_month(month)), 0)
        need_before = need.get(prev_month(month)) or 0
        if not need_before or before / need_before < threshold:
            continue                       # 上月就不活跃：新股上市或已摘牌，不是洞
        out.append({"code": code, "month": month, "have": have, "need": required,
                    "ratio": round(have / required, 4)})
    return sorted(out, key=lambda h: (h["ratio"], h["month"], h["code"]))


def month_bounds(month: str) -> tuple[str, str]:
    """"2026-09" → ("2026-09-01", "2026-10-01")，给上游按窗口截日用。"""
    idx = _to_idx(month)
    return _from_idx(idx) + "-01", _from_idx(idx + 1) + "-01"


def sina_symbol(code: str) -> str:
    if code.startswith(("6", "5")):
        return f"sh{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"        # 北交所 920/8xxxxx，接口前缀确实是 bj（实测 920002 有到今天的行为）
    return f"sz{code}"


def _sina_bars(code: str):
    """爬虫的备源：只用来判「那段时间市场到底有没有票」。给最近 250 根（够覆盖三个月窗口）。"""
    import requests
    import time

    resp = requests.get(SINA_URL, params={"symbol": sina_symbol(code), "scale": "240",
                                          "ma": "no", "datalen": str(UPSTREAM_BARS)},
                        timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    time.sleep(0.15)            # 一个一个票·月地问，别把备源打出节流
    return rows if isinstance(rows, list) else []


def upstream_days(code: str, beg: str, end: str, fetch=_sina_bars):
    """上游在这段窗口里有几个交易日。取不到（超时/被掐）返回 None＝不能断定，别当成 0。"""
    try:
        rows = fetch(code)
    except Exception:  # noqa: BLE001 核不了上游就退回「不能断定」，宁可多报
        return None
    return len({str(r.get("day", ""))[:10] for r in rows or []
                if beg <= str(r.get("day", ""))[:10] < end})


def split_holes(found, counts, threshold=THRESHOLD):
    """把「覆盖不足的票·月」拆成三类：我们的洞 / 停牌退市 / 核不到上游。

    `counts` 按 (票, 月) 给上游那个月有几天。判据是**那个月有几天**，不是「上游最后一次有行情是哪天」：
    停牌后复牌的票（实测 301139 九月整月只 1 天、十月复牌）用后者会被误判成我们的洞。
    """
    real, halted, unknown = [], [], []
    for h in found:
        n = counts.get((h["code"], h["month"]))
        if n is None:
            unknown.append(h)
        elif n >= h["need"] * threshold:
            real.append(h)         # 上游整月都有 ⇒ 那几天是我们没爬到
        else:
            halted.append(h)
    return real, halted, unknown


def verify_counts(found, ask=upstream_days):
    """每个「票·月」向上游问一次「那个月市场里有几天」，键按 (code, month) 存。

    去重是因为同一票可能连续几个月都在洞里，而月份不同答案就不同（九月停牌、十月复牌）。
    """
    counts: dict[tuple[str, str], int | None] = {}
    for h in found:
        key = (h["code"], h["month"])
        if key not in counts:
            beg, end = month_bounds(h["month"])
            counts[key] = ask(h["code"], beg, end)
    return counts


def complete_days(counts: dict, today: str) -> dict:
    """剔掉当天：日线爬虫要跑到傍晚（沪市队尾能漂过 17:05），当天永远看着「薄」。"""
    return {day: n for day, n in counts.items() if day != today}


def thin_days(counts: dict, ratio=THIN_RATIO) -> list[dict]:
    """票数不足最满那天九成的日子。全场都没票的日子（周末/休市）根本不会出现在 `counts` 里。"""
    if not counts:
        return []
    full = max(counts.values())
    return [{"date": day, "codes": n, "full": full}
            for day, n in sorted(counts.items()) if n < full * ratio]


def alert_text(real_holes, halted, unknown, thin_list, months) -> tuple[str, str]:
    problems = []
    if real_holes:
        problems.append(f"真缺数 {len(real_holes)} 票·月")
    if unknown:
        problems.append(f"上游没核到 {len(unknown)} 票·月")
    if thin_list:
        problems.append(f"薄日 {len(thin_list)} 个")
    title = "[K线覆盖] " + ("、".join(problems) if problems else "没有我们的洞")
    lines = [f"- 窗口：{months[0]}…{months[-1]}（{len(months)} 个月，两批 f=9/f=0 **并集**口径）",
             f"- 真缺数（上游那个月有行情、我们没有）：{len(real_holes)} 个票·月"]
    for h in real_holes[:10]:
        lines.append(f"    - {h['code']} {h['month']} 仅 {h['have']}/{h['need']} 天"
                     f"（{h['ratio'] * 100:.0f}%）")
    if unknown:
        lines.append(f"- 问不到上游、不能断定的：{len(unknown)} 个（一起报，别静默吞）")
        for h in unknown[:5]:
            lines.append(f"    - {h['code']} {h['month']} {h['have']}/{h['need']} 天")
    if halted:
        lines.append(f"- 另有 {len(halted)} 个票·月**不是我们的洞**：上游同样没那段行情"
                     f"（停牌/退市），例如 {', '.join(h['code'] for h in halted[:5])}")
    lines.append(f"- 薄日（当天票数不足最满那天九成）：{len(thin_list)} 个")
    for d in thin_list[:10]:
        lines.append(f"    - {d['date']} 只有 {d['codes']} 只票（最满 {d['full']}）")
    lines.append("- 影响：真缺数的月份里这批票的因子/筹码/标签都少行，榜与回测会静默偏窄，不报错")
    lines.append("- 补法：`--codes` 定向重爬那批票；只报不补是因为补一天要几千次请求")
    return title, "\n".join(lines)


def mongo_db():
    cfg_path = ROOT / "stock_selector" / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    m = (cfg or {}).get("mongodb") or {}
    client = pymongo.MongoClient(host=m.get("host", "127.0.0.1"),
                                 port=int(m.get("port", 27017)),
                                 serverSelectionTimeoutMS=8000)
    return client[m.get("database", "eastmoney_news")]


def load_monthly(col, start: str, end: str) -> dict:
    """(code, month) → 该票该月有票的天数。按天先去重再数，两批重叠同一天只算一次。"""
    pipe = [{"$match": {"frequency": {"$in": list(FREQUENCIES)},
                        "date": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": {"code": "$code", "month": {"$substr": ["$date", 0, 7]},
                                "day": {"$substr": ["$date", 0, 10]}}}},
            {"$group": {"_id": {"code": "$_id.code", "month": "$_id.month"},
                        "n": {"$sum": 1}}}]
    return {(d["_id"]["code"], d["_id"]["month"]): d["n"] for d in col.aggregate(pipe)}


def load_day_counts(col, start: str, end: str) -> dict:
    """日 → 当天有票的只数（也是「全场真实交易日」的真源，当月分母就从这里来）。"""
    pipe = [{"$match": {"frequency": {"$in": list(FREQUENCIES)},
                        "date": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": {"day": {"$substr": ["$date", 0, 10]}, "code": "$code"}}},
            {"$group": {"_id": "$_id.day", "n": {"$sum": 1}}}]
    got = {d["_id"]: d["n"] for d in col.aggregate(pipe)}
    return dict(sorted(got.items()))


def months_between(start: str, end: str) -> list[str]:
    return [_from_idx(k) for k in range(_to_idx(start[:7]), _to_idx(end[:7]))]


def need_by_month(day_counts: dict) -> dict:
    need: dict[str, int] = {}
    for day in day_counts:
        need[month_of(day)] = need.get(month_of(day), 0) + 1
    return need


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


def maybe_alert(real, halted, unknown, thin_list, months, status, *, today: str,
                enabled: bool = False, channel_key: str = "ding_ops"):
    """真有我们的洞才推，同一天只推一条；发送失败不记日期（下一轮重试）。"""
    if not real and not unknown and not thin_list:
        return "no_hole"               # 全是停牌/退市＝市场本来就没票，不打扰人
    if not enabled:
        return "disabled"
    title, text = alert_text(real, halted, unknown, thin_list, months)
    if status.get("alert_date") == today:
        return "deduped"
    try:
        import alert_pusher
        name, url, secret = alert_pusher.resolve_webhook(alert_pusher.DINGTALK, channel_key)
    except Exception as exc:  # noqa: BLE001 渠道没配好不能拖垮体检
        logger.warning("[alert] 取不到钉钉渠道，只留日志: %s", exc)
        return "no_channel"
    if alert_pusher.send(alert_pusher.DINGTALK, url, secret, title, text):
        status["alert_date"] = today
        logger.info("[alert] 已推送覆盖率告警（渠道 %s）", name)
        return "sent"
    logger.warning("[alert] 推送失败，下一轮重试")
    return "send_failed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=MONTHS, help="看最近几个自然月（含当月）")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--thin-ratio", type=float, default=THIN_RATIO)
    ap.add_argument("--top", type=int, default=15, help="打印最差的前多少个票·月")
    ap.add_argument("--alert", action="store_true", help="真推钉钉（默认只在有问题时打印）")
    ap.add_argument("--channel-key", default="ding_ops")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="不向上游核对，洞全部记「核不到」一起报（默认每个票·月问一次备源）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = datetime.now().strftime("%Y-%m-%d")
    start, end = month_window(today, max(args.months, 1))
    # 多取一个月做「上月活跃」判据，但只对窗口内的月份报警
    look_back, _ = month_window(start, 2)
    db = mongo_db()
    col = db[COL_KLINE]
    monthly = load_monthly(col, look_back, end)
    day_counts = complete_days(load_day_counts(col, start, end), today)
    need = need_by_month(day_counts)
    months = months_between(start, end)
    found = holes(monthly, need, threshold=args.threshold, report_months=months)
    thin = thin_days(day_counts, ratio=args.thin_ratio)

    counts = verify_counts(found) if args.verify else {}
    real, halted, unknown = split_holes(found, counts, threshold=args.threshold)

    print(f"[coverage] 窗口 {months[0]}…{months[-1]}（并集口径：f=9 ∪ f=0）")
    print(f"  交易日 {len(day_counts)} 天，覆盖票·月 {len(monthly)} 组，"
          f"阈值 {args.threshold:.0%} ⇒ 覆盖不足 {len(found)} 个票·月")
    print(f"    真缺数（上游那个月有行情）{len(real)}、停牌/退市 {len(halted)}、"
          f"核不到上游 {len(unknown)}{'（--no-verify，全记核不到）' if not args.verify else ''}")
    for label, items in (("真缺数", real), ("核不到", unknown), ("停牌/退市", halted)):
        for h in items[: args.top]:
            print(f"    [{label}] {h['code']} {h['month']} {h['have']}/{h['need']} 天 "
                  f"= {h['ratio'] * 100:.0f}%")
    print(f"  薄日（票数 < 最满那天 {args.thin_ratio:.0%}）：{len(thin)} 个")
    for d in thin[: args.top]:
        print(f"    {d['date']} {d['codes']} 只（最满 {d['full']}）")

    status = read_status()
    status["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status["window"] = months[-1]
    status["holes"] = len(found)
    status["real_holes"] = len(real)
    status["halted"] = len(halted)
    status["unknown"] = len(unknown)
    status["thin_days"] = len(thin)
    sent = maybe_alert(real, halted, unknown, thin, months, status, today=today,
                       enabled=args.alert, channel_key=args.channel_key)
    status["last_alert_result"] = sent
    write_status(status)
    print(f"  告警：{sent}（同一天只推一条；要真发加 --alert）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
