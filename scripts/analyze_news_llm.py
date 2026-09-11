"""用 LLM(智谱 GLM) 解读 MongoDB 中的新闻，落盘两层结构（parquet + MongoDB）。

两层结构（见 docs/superpowers/specs/2026-09-07-news-llm-analysis-two-layer.md）：
    第一层 news_analysis: data/news_analysis/YYYY-MM-DD.parquet 每条新闻一条
    第二层 watch_target:  data/news_watch/YYYY-MM-DD.parquet      每个"标的+方向"一条
    MongoDB: eastmoney_news 库 news_analysis / watch_target 集合（parquet 为准）
    名称回填: 从 bk_stocks/sector_stocks 查代码->名称，丰富 hint 的 name

用法:
    python scripts/analyze_news_llm.py                        # 真实读 Mongo，最近1天
    python scripts/analyze_news_llm.py --days 3               # 最近3天（自然日）
    python scripts/analyze_news_llm.py --limit 10             # 最多10条
    python scripts/analyze_news_llm.py --demo                 # 内置示例新闻
    python scripts/analyze_news_llm.py --no-mongo             # 只写 parquet
    python scripts/analyze_news_llm.py --stats heat --start 2026-09-01 --end 2026-09-08 --category 板块 --top 20
    python scripts/analyze_news_llm.py --stats trend --code BK0464 --start 2026-09-01 --end 2026-09-08
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from stock_selector.news.analyze import analyze_news
from stock_selector.news.fetch import fetch_news
from stock_selector.news.names import load_name_maps
from stock_selector.news.schema import ANALYSIS_SCHEMA, WATCH_SCHEMA
from stock_selector.news.stats import (
    load_watch_range, rank_heat, rank_heat_mongo, trend, trend_mongo,
)
from stock_selector.news.store import (
    create_indexes, save_analysis_mongo, save_analysis_parquet,
    save_watch_mongo, save_watch_parquet,
)
from stock_selector.news.targets import build_watch_targets

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"
OUTPUT_DIR = ROOT / "data" / "news_analysis"
WATCH_DIR = ROOT / "data" / "news_watch"
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("analyze_news_llm")

DEMO_NEWS = [{
    "_id": "demo-001",
    "title": "理想汽车26.5亿元入股欣旺达动力 投后估值301亿",
    "summary": "9月4日晚间，欣旺达公告称，子公司欣旺达动力拟进行C++轮增资扩股，引入北京理想汽车有限公司。理想汽车拟出资26.5亿元，认购欣旺达动力约14亿元新增注册资本，对应8.79%股份，投后估值301.45亿元。",
    "showTime": "2026-09-06 07:08:00",
    "stockList": ["90.BK0464", "0.300207"],
}]


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if "llm" not in cfg:
        raise SystemExit(f"配置缺少 llm 节，请先在 {CONFIG_PATH} 添加")
    return cfg


def connect_mongo(mongo_cfg: dict):
    """连接 Mongo；失败返回 None。"""
    try:
        from pymongo import MongoClient
    except ImportError:
        logger.warning("[mongo] pymongo 未安装")
        return None
    try:
        client = MongoClient(host=mongo_cfg.get("host", "127.0.0.1"),
                             port=int(mongo_cfg.get("port", 27017)),
                             serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        return client[mongo_cfg["database"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mongo] 连接失败(%s)，降级", exc)
        return None


def _demo_items():
    return [{"news_id": n["_id"], "title": n["title"], "summary": n.get("summary", ""),
             "source": "demo", "url": "", "publish_time": n.get("showTime", ""),
             "hints": []} for n in DEMO_NEWS]


def run_analysis(args, cfg):
    llm_cfg = cfg.get("llm", {})
    mongo_cfg = cfg.get("mongodb", {})
    collect_date = datetime.today().strftime("%Y-%m-%d")

    if args.demo:
        news_list = _demo_items()
    else:
        db = connect_mongo(mongo_cfg)
        news_list = []
        name_maps = None
        if db is not None:
            try:
                name_maps = load_name_maps(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mongo] 名称映射加载失败(%s)，hint 回退仅代码", exc)
            try:
                news_list = fetch_news(db[mongo_cfg.get("collection", "news")],
                                       days=args.days, limit=args.limit, name_maps=name_maps)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mongo] 读取失败(%s)，改用 --demo 演示", exc)
                news_list = []
        if not news_list:
            logger.warning("MongoDB 无新闻，改用 --demo 演示...")
            news_list = _demo_items()

    logger.info("开始分析 %d 条新闻，model=%s", len(news_list), llm_cfg.get("model"))
    rows = []
    hints_by_news = {}
    for i, news in enumerate(news_list, 1):
        logger.info("[%d/%d] 分析: %s", i, len(news_list), news.get("title", "")[:40])
        hints_by_news[news["news_id"]] = news.get("hints", [])
        rows.append(analyze_news(llm_cfg, news, hints=news.get("hints", []),
                                 collect_date=collect_date))

    df = pd.DataFrame(rows, columns=ANALYSIS_SCHEMA)
    save_analysis_parquet(df, collect_date, OUTPUT_DIR)

    watch_docs = build_watch_targets(rows, hints_by_news, collect_date, llm_cfg.get("model", ""))
    watch_df = pd.DataFrame(watch_docs, columns=WATCH_SCHEMA)
    save_watch_parquet(watch_df, collect_date, WATCH_DIR)

    if not args.no_mongo:
        db = connect_mongo(mongo_cfg)
        if db is not None:
            try:
                create_indexes(db)
                save_analysis_mongo(db, df, collect_date)
                save_watch_mongo(db, watch_docs, collect_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mongo] 写入失败(%s)，parquet 仍为准", exc)

    print_summary(df)
    print_watch_summary(watch_docs)


def print_summary(df):
    if df.empty:
        return
    print("\n=== 分析摘要 ===")
    for _, r in df.iterrows():
        score = int(r["sentiment_score"])
        print(f"[{r['sentiment']}/{'%+d' % score}] {r['summary']}  ({str(r['title'])[:30]})")
        for label, key in (("利好", "bullish"), ("利空", "bearish"), ("关注", "watchlist")):
            items = [x for x in (r.get(key) or []) if x]
            if items:
                print(f"  {label}: " + "、".join(
                    f"{x.get('name', '')}({x.get('score', 0):+d})"
                    if label != "关注" else f"{x.get('name', '')}({x.get('priority', '')})"
                    for x in items))
        for x in (r.get("risks") or []):
            if x:
                print(f"  风险: {x.get('desc', '')}")


def print_watch_summary(watch_docs):
    print("\n=== 今日关注 ===")
    if not watch_docs:
        print("(无)")
        return
    for t in watch_docs:
        ident = t["code"] or t["name"]
        if t["direction"] == "risk":
            print(f"[{t['category']}] {t['name']} ({t['score']:+d})")
        elif t["direction"] == "watch":
            print(f"[{t['category']}] {ident} {t['name']} ({t['direction']} {t['score']:+d}, {t['priority']}, {len(t['source_news_ids'])}条新闻)")
        else:
            print(f"[{t['category']}] {ident} {t['name']} ({t['direction']} {t['score']:+d}, {len(t['source_news_ids'])}条新闻)")


def run_stats(args, cfg):
    start = args.start or (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    end = args.end or (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    mongo_cfg = cfg.get("mongodb", {})
    if not args.no_mongo:
        db = connect_mongo(mongo_cfg)
        if db is not None:
            col = db["watch_target"]
            if args.stats == "heat":
                rows = rank_heat_mongo(col, start, end, category=args.category,
                                       direction=args.direction or "bullish", top_n=args.top or 30)
                print("\n=== 热度排行 ===")
                for r in rows:
                    print(f"{r['_id']} {r.get('name', '')} news={r['news_count']} score={r['total_score']}")
                return
            if args.stats == "trend":
                rows = trend_mongo(col, args.code, start, end)
                print("\n=== 趋势 ===")
                for r in rows:
                    print(f"{r['collect_date']} score={r['score']:+d} cum={r['cum']:+d}")
                return
    df = load_watch_range(WATCH_DIR, start, end)
    if args.stats == "heat":
        out = rank_heat(df, category=args.category, direction=args.direction or "bullish",
                        top_n=args.top or 30)
        print("\n=== 热度排行(parquet) ===")
        for _, r in out.iterrows():
            print(f"{r['code']} {r['name']} news={r['news_count']} score={r['total_score']}")
    elif args.stats == "trend":
        out = trend(df, args.code)
        print("\n=== 趋势(parquet) ===")
        for _, r in out.iterrows():
            print(f"{r['collect_date']} score={r['score']:+d} cum={r['cum']:+d}")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    fh = logging.FileHandler(LOG_DIR / f"news_analyze_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 解读新闻，落盘两层结构并支持跨天统计")
    parser.add_argument("--days", type=int, default=1, help="读取最近 N 天新闻（0=不管时间）")
    parser.add_argument("--limit", type=int, default=50, help="最多分析条数")
    parser.add_argument("--demo", action="store_true", help="用内置示例新闻演示")
    parser.add_argument("--no-mongo", action="store_true", help="只写 parquet，不写 Mongo")
    parser.add_argument("--stats", choices=["heat", "trend"], help="跨天统计（与分析互斥）")
    parser.add_argument("--start", default=None, help="统计起始 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="统计结束 YYYY-MM-DD")
    parser.add_argument("--category", default=None, help="统计 category 过滤（heat）")
    parser.add_argument("--direction", default=None, help="统计 direction 过滤（heat），默认 bullish")
    parser.add_argument("--top", type=int, default=30, help="热度排行条数")
    parser.add_argument("--code", default=None, help="趋势标的 code（trend）")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    if args.stats:
        if args.stats == "trend" and not args.code:
            parser.error("--stats trend 需要 --code")
        run_stats(args, cfg)
        return

    run_analysis(args, cfg)


if __name__ == "__main__":
    main()
