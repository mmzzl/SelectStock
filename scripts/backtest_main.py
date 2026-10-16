"""回测 CLI：两种策略来源，同一个引擎。

1) 缺省：读取 MongoDB trading_rules 的 buy/sell/add 表达式驱动（规则策略）
2) --strategy NAME：直接复用选股插件里的 as-of 策略，做到"选股与回测同源可验证"

用法:
    python scripts/backtest_main.py \
        --start 2025-09-01 --end 2026-09-01 \
        --capital 100000 --max-positions 5 --position-cap-pct 0.2

    # 用选股策略回测（出场走机械规则），等距抽 300 只股票
    python scripts/backtest_main.py --strategy trend_rebound --universe 300 \
        --start 2026-01-01 --end 2026-09-01 --max-hold-days 10 --stop-loss-pct 0.05
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
from stock_selector.backtest.kernel_strategy import KernelStrategy
from stock_selector.backtest.provider import ParquetProvider
from stock_selector.backtest.rules import RulesStrategy
from stock_selector.backtest.store import BacktestStore
from stock_selector.core.selector import create_strategy


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


def _sampled_codes(codes: list[str], n: int) -> list[str]:
    """等距抽样，不是取前 n 个：codes() 升序时前 n 个全是最老编号的深市主板，板块直接偏掉。"""
    if n >= len(codes):
        return codes
    return [codes[i * len(codes) // n] for i in range(n)]


def run_backtest(args, progress_cb=None):
    client = get_client()
    database = load_mongo_config()["mongodb"]["database"]
    try:
        provider = ParquetProvider(ROOT / "data" / "stock_kline")
        # 规则模式实际生效的串才记进 params（策略模式不读它，记了反而像是生效过）
        exit_rules: dict = {}

        if args.strategy:
            selection_strategy = create_strategy(args.strategy)
            strategy = KernelStrategy(
                strategy=selection_strategy,
                provider=provider,
                target_amount=args.target_amount,
                universe=_sampled_codes(provider.codes(), args.universe) if args.universe else None,
                max_hold_days=args.max_hold_days,
                stop_loss_pct=args.stop_loss_pct,
                take_profit_pct=args.take_profit_pct,
            )
            strategy_name = f"kernel:{args.strategy}"
        else:
            rules = load_rules(client, database)
            # 规则集快照优先：管理页面「应用」的那套判据要和线上判定用同一条表达式串，
            # 否则回测验的是 MongoDB 里的老规则、持有可信度算的是新规则，两边结论对不上。
            sell_rule = getattr(args, "sell_rule", None) or rules["sell"]
            add_rule = getattr(args, "add_rule", None) or rules["add"]
            if not rules["buy"] or not sell_rule:
                raise SystemExit("缺少 buy/sell 规则，无法回测")
            strategy = RulesStrategy(
                provider=provider,
                buy_rule=rules["buy"],
                sell_rule=sell_rule,
                add_rule=add_rule,
                target_amount=args.target_amount,
            )
            rule_set_id = getattr(args, "rule_set_id", None)
            strategy_name = f"rules:{rule_set_id}" if rule_set_id else "rules"
            exit_rules = {"rule_set_id": rule_set_id, "sell_rule": sell_rule,
                          "add_rule": add_rule}

        engine = BacktestEngine(
            strategy=strategy,
            data=provider,
            capital=args.capital,
            max_positions=args.max_positions,
            position_cap_pct=args.position_cap_pct,
            commission_rate=args.commission_rate,
            commission_min=args.commission_min,
            stamp_tax_rate=args.stamp_tax_rate,
            strategy_name=strategy_name,
            start_date=args.start,
            end_date=args.end,
            progress_cb=progress_cb,
            params={
                "capital": args.capital,
                "max_positions": args.max_positions,
                "position_cap_pct": args.position_cap_pct,
                "target_amount": args.target_amount,
                "commission_rate": args.commission_rate,
                "commission_min": args.commission_min,
                "stamp_tax_rate": args.stamp_tax_rate,
                "universe": args.universe,
                "max_hold_days": args.max_hold_days,
                "stop_loss_pct": args.stop_loss_pct,
                "take_profit_pct": args.take_profit_pct,
                **exit_rules,
            },
        )
        result = engine.run()

        if args.no_save:
            print("--no-save：本次结果未写入 MongoDB")
        else:
            BacktestStore(client[database]).save_result(result)
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
        "--strategy",
        default=None,
        help="改用选股插件策略回测（如 trend_rebound / box_oscillate），缺省用 trading_rules",
    )
    parser.add_argument("--universe", type=int, default=0, help="同源回测只取前 N 只股票（0=全市场）")
    parser.add_argument("--max-hold-days", type=int, default=10, help="持有 N 个交易日后清仓（0=关闭）")
    parser.add_argument("--stop-loss-pct", type=float, default=0.05, help="止损幅度（0=关闭）")
    parser.add_argument("--take-profit-pct", type=float, default=0.0, help="止盈幅度（0=关闭）")
    parser.add_argument("--no-save", action="store_true", help="只回测不落库（调试/CI）")
    parser.add_argument(
        "--start-capital",
        type=float,
        default=100000.0,
        help="(deprecated) use --capital",
    )
    parser.add_argument("--sell-rule", default=None,
                        help="用规则集快照的卖出表达式覆盖 trading_rules（仅规则模式）")
    parser.add_argument("--add-rule", default=None,
                        help="用规则集快照的加仓表达式覆盖 trading_rules（仅规则模式）")
    parser.add_argument("--rule-set-id", default=None, help="规则集快照 id，写进结果参数供复现")
    args = parser.parse_args()

    if args.start_capital != 100000.0:
        args.capital = args.start_capital

    if args.strategy and args.target_amount >= args.capital * args.position_cap_pct:
        # 单票上限按总资产校验，贴边的参数会在净值一旦回撤后静默拒掉所有买入，看起来像"没有信号"
        print(
            f"警告: --target-amount {args.target_amount:.0f} ≥ 单票上限 "
            f"{args.capital * args.position_cap_pct:.0f}（capital × position_cap_pct），"
            f"净值回落后成交会被上限拒绝而大幅减少，请把单笔金额调到该上限以下。",
            file=sys.stderr,
        )

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
    if args.strategy:
        print(
            "exit       : "
            f"max_hold_days={args.max_hold_days} stop_loss={args.stop_loss_pct} "
            f"take_profit={args.take_profit_pct}（出场为机械规则，与选股判据无关）"
        )
    print("--- metrics ---")
    for k, v in result.metrics.items():
        print(f"  {k:16s}: {v}")


if __name__ == "__main__":
    main()
