"""回测 CLI：读取 trading_rules + parquet 指标，运行回测并落库。

用法:
    python scripts/backtest_main.py \
        --start 2025-09-01 --end 2026-09-01 \
        --capital 100000 --max-positions 5 --position-cap-pct 0.2
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from pymongo import MongoClient

from stock_selector.backtest.broker import Broker
from stock_selector.backtest.engine import BacktestEngine
from stock_selector.backtest.provider import ParquetProvider
from stock_selector.backtest.rules import RulesStrategy
from stock_selector.backtest.store import BacktestStore


def load_mongo_config() -> dict:
    cfg = ROOT / "stock_selector" / "config" / "config.yaml"
    if cfg.exists():
        return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    return {"mongodb": {"host": "127.0.0.1", "port": 27017, "database": "eastmoney_news"}}


def get_client() -> MongoClient:
    mongo = load_mongo_config().get("mongodb") or {}
    return MongoClient(
        host=mongo.get("host", "127.0.0.1"),
        port=int(mongo.get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )


def load_rules(client, database: str) -> dict:
    """读取启用的 buy/sell/add 规则条件。"""
    rules: dict[str, str | None] = {"buy": None, "sell": None, "add": None}
    col = client[database]["trading_rules"]
    for doc in col.find({"enabled": True}):
        rtype = doc.get("type")
        if rtype not in rules:
            continue
        condition = doc.get("condition")
        if condition and rules[rtype] is None:  # 取优先级第一条
            rules[rtype] = condition
    return rules


def run_backtest(args) -> dict:
    client = get_client()
    database = load_mongo_config()["mongodb"]["database"]
    try:
        rules = load_rules(client, database)
        if not rules["buy"] or not rules["sell"]:
            raise SystemExit("缺少 buy/sell 规则，无法回测")

        provider = ParquetProvider(ROOT / "data" / "stock_kline")
        strategy = RulesStrategy(
            provider=provider,
            buy_rule=rules["buy"],
            sell_rule=rules["sell"],
            add_rule=rules["add"],
            target_amount=args.target_amount,
        )
        engine = BacktestEngine(
            strategy=strategy,
            data=provider,
            capital=args.capital,
            max_positions=args.max_positions,
            position_cap_pct=args.position_cap_pct,
            commission_rate=args.commission_rate,
            commission_min=args.commission_min,
            stamp_tax_rate=args.stamp_tax_rate,
            strategy_name="rules",
        )
        result = engine.run()

        store = BacktestStore(client[database])
        store.save_result(result)
        return result
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="trading_rules + parquet 回测")
    parser.add_argument("--start", default="2025-09-01", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-09-01", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--max-positions", type=int, default=5, help="持仓只数上限")
    parser.add_argument("--position-cap-pct", type=float, default=0.2, help="单票仓位上限比例")
    parser.add_argument("--target-amount", type=float, default=20000.0, help="单笔买入目标金额")
    parser.add_argument("--commission-rate", type=float, default=0.00025, help="佣金率")
    parser.add_argument("--commission-min", type=float, default=5.0, help="最低佣金")
    parser.add_argument("--stamp-tax-rate", type=float, default=0.0005, help="卖出印花税率")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--start-capital",
        type=float,
        default=100000.0,
        help="(deprecated) use --capital",
    )
    args = parser.parse_args()

    if args.start_capital != 100000.0:
        args.capital = args.start_capital

    result = run_backtest(args)

    if args.json:
        payload = {
            "task_id": result.task_id,
            "strategy": result.strategy,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "metrics": result.metrics,
            "trades": result.trades,
            "equity": result.equity,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"task_id    : {result.task_id}")
    print(f"strategy   : {result.strategy}")
    print(f"period     : {result.start_date} ~ {result.end_date}")
    print("--- metrics ---")
    for k, v in result.metrics.items():
        print(f"  {k:16s}: {v}")


if __name__ == "__main__":
    main()
