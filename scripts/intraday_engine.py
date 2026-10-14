"""盘中策略计算引擎：消费 rt:ticks → 分钟指标 → 信号 → 风控 → 决策落盘 + 进告警流。

产出：
    redis rt:metric:{code}       盘中实时指标（均线/MACD/RSI/BOLL/量比/换手）
    redis rt:decisions           过完风控的最终决策流（告警进程消费）
    redis rt:engine-heartbeat    上一轮统计：读了多少 tick、评估多少只、拦了几条
    data/intraday_decisions/{YYYY-MM-DD}.parquet   当日全量决策，复盘读它
    mongo intraday_decision      可选（--mongo），App 按代码查历史决策时用

前置：`scripts/quote_service.py` 必须在跑，否则 rt:ticks 没有新数据，引擎只会读到旧流。
（需求文档里的 TDengine 换成 parquet + mongo：本机内存已经紧张，复盘的读法也不需要时序库。）

股票池口径要与上游一致（`--from-app`）：引擎按池过滤 tick，池外的票不建分钟条也不评估
（空池表示「不限定」而不是「一只都不看」）；换日时自动重取，持仓市值同时作为「单票已用
额度」进风控的 sizing。

用法:
    python scripts/intraday_engine.py --code 600000 300750            # 调试两只
    python scripts/intraday_engine.py --watchlist data/watchlist.txt
    python scripts/intraday_engine.py --from-app                       # 选股命中+持仓（推荐口径）
    python scripts/intraday_engine.py --rounds 1 --no-ml --cooldown 0  # 冒烟一轮
    python scripts/intraday_engine.py --status                         # 看引擎心跳
"""

import argparse
import logging
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_selector.core.selector import _load_config
from stock_selector.intraday import EngineConfig, IntradayEngine, store
from stock_selector.intraday.signals import SignalConfig
from stock_selector.realtime import bus, resolve_universe, session
from stock_selector.realtime import pool as app_pool
from stock_selector.realtime.pool import PoolError
from stock_selector.risk import RiskEngine

logger = logging.getLogger("intraday_engine")


def read_watchlist(path: Path) -> list[str]:
    """一行一个代码，`#` 起头是注释（与 scripts/quote_service.py 同一份清单格式）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()]


def parse_held(pairs: list[str]) -> dict[str, float]:
    """`--held 600000=20000` → {代码: 已持有市值(元)}。单票仓位上限要从总限额里扣掉它。"""
    out = {}
    for pair in pairs or []:
        code, _, value = pair.partition("=")
        if not code.strip() or not value.strip():
            raise SystemExit(f"--held 要写成 代码=市值(元)，收到的是 {pair!r}")
        out[code.strip().zfill(6)] = float(value)
    return out


def show_status(client) -> int:
    stats = bus.last_heartbeat(client, key=bus.KEY_ENGINE_HEARTBEAT)
    if not stats:
        print(f"{bus.KEY_ENGINE_HEARTBEAT} 为空：引擎还没跑过（或 TTL 已过期）")
        return 1
    print(f"最新一轮 状态={stats.get('status')} 读到 tick={stats.get('ticks')} "
          f"回放={stats.get('replayed')} 池外跳过={stats.get('skipped')} "
          f"变化={stats.get('changed')} 池={stats.get('pool')} "
          f"评估={stats.get('evaluated')} 决策={stats.get('decisions')} 拦截={stats.get('blocked')} "
          f"耗时={stats.get('elapsed_s')}s")
    print(f"{bus.KEY_TICKS} 长度={bus.stream_len(client, bus.KEY_TICKS)}  "
          f"{bus.KEY_DECISIONS} 长度={bus.stream_len(client, bus.KEY_DECISIONS)}  "
          f"{bus.KEY_QUOTE} 只数={client.hlen(bus.KEY_QUOTE)}")
    print("是否在交易时段:", session.in_trading_window())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="盘中策略计算引擎（信号 → 风控 → 决策）")
    p.add_argument("--code", nargs="*", default=[], help="自选代码（六位）")
    p.add_argument("--watchlist", type=Path, help="自选清单文件（一行一个代码）")
    p.add_argument("--all", action="store_true", help="股票池取本地指标宽表全部代码（全市场）")
    p.add_argument("--from-app", action="store_true",
                   help="股票池=最近一次选股命中 + 当前持仓（读 app.db，换日自动重取，与上游轮询同口径）")
    p.add_argument("--app-db", type=Path, default=app_pool.DEFAULT_DB, help="app.db 路径")
    p.add_argument("--limit", type=int, default=0, help="股票池上限，0=不限")
    p.add_argument("--interval", type=float, help="读流间隔秒数（默认取配置的 intraday.interval）")
    p.add_argument("--rounds", type=int, default=0, help="跑多少轮后退出，0=常驻")
    p.add_argument("--max-buy-codes", type=int, default=3,
                   help="一天最多给几只买入下单建议（按盯盘名单名次取前 N，0=不限）。"
                        "A 股 T+1 且资金有限，几百只一起推等于没推")
    p.add_argument("--cooldown", type=int, help="重复抑制窗口（秒），只在 --no-daily-dedupe 或去重表写不进时生效")
    p.add_argument("--no-daily-dedupe", action="store_true",
                   help="关掉「同票同信号当天只报一次」，退回 --cooldown 窗口")
    p.add_argument("--max-eval", type=int, help="一轮最多给多少只算分钟指标")
    p.add_argument("--kinds", nargs="*", help="只开这些信号种类，缺省全开")
    p.add_argument("--held", nargs="*", default=[], metavar="代码=市值",
                   help="手工给出的已持有市值（元），单票仓位上限要先扣掉它；--from-app 时改用 app.db 的成本口径")
    p.add_argument("--no-ml", action="store_true", help="关掉买入风险分（少读 700 根日线，启动快）")
    p.add_argument("--out-dir", type=Path, default=store.DEFAULT_DIR, help="决策 parquet 目录")
    p.add_argument("--mongo", action="store_true", help="决策同时写 mongo（复盘查代码时用）")
    p.add_argument("--host", default=bus.HOST)
    p.add_argument("--port", type=int, default=bus.PORT)
    p.add_argument("--db", type=int, default=bus.DB)
    p.add_argument("--status", action="store_true", help="只看引擎心跳，不跑循环")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    client = bus.connect(args.host, args.port, args.db)
    if args.status:
        return show_status(client)

    codes = list(args.code)
    if args.watchlist:
        codes += read_watchlist(args.watchlist)
    plan_loader = None
    auto_held: dict[str, float] = {}
    plan = None
    if args.from_app:
        if args.held:
            raise SystemExit("--from-app 的持仓市值自动按 app.db 的成本口径取，"
                             "与 --held 手工口径只能选一个（混着给会不知道信哪个）")
        plan_loader = partial(app_pool.load, args.app_db, limit=args.limit, extra=codes)
        try:
            plan = plan_loader()
        except PoolError as exc:
            raise SystemExit(f"--from-app 取池失败：{exc}") from exc
        pool, auto_held = plan.codes, plan.held
    else:
        pool = resolve_universe(codes, all_market=args.all, limit=args.limit)
    if not pool:
        raise SystemExit("股票池为空：请给 --code/--watchlist，或用 --all 取全市场、"
                         "--from-app 取「选股命中+持仓」")

    settings = _load_config()
    intraday = dict(settings.get("intraday") or {})
    raw_signals = intraday.pop("signals", None)   # 信号阈值给 SignalConfig，不是引擎参数
    engine_cfg = EngineConfig.from_dict(intraday)
    if args.interval:
        engine_cfg.interval = args.interval
    if args.cooldown is not None:
        engine_cfg.cooldown = args.cooldown
    if args.no_daily_dedupe:
        engine_cfg.once_per_day = False
    if args.max_eval:
        engine_cfg.max_eval = args.max_eval
    engine_cfg.held = auto_held or parse_held(args.held)

    signal_cfg = SignalConfig.from_dict(raw_signals)
    if args.kinds:
        signal_cfg.kinds = tuple(args.kinds)

    risk = RiskEngine.from_settings(settings)
    if args.no_ml:
        risk.cfg.ml_enabled = False
    else:
        risk.preflight()        # 模型缺失要在启动时就炸，不能装着风控跑成摆设
    # 买入名额由盘中定，不由风控默认值定：盘后内核不限只数（它产的是名单，不是订单）。
    risk.cfg.max_buy_codes = args.max_buy_codes
    if plan is not None:
        engine_cfg.ranks = dict(plan.ranks)
        engine_cfg.bought_today = frozenset(plan.bought_today)
        engine_cfg.cash_left = plan.cash_left(risk.cfg.capital)

    engine = IntradayEngine(client, pool, risk, cfg=engine_cfg, signal_cfg=signal_cfg,
                            out_dir=args.out_dir, to_mongo=args.mongo,
                            pool_loader=plan_loader)
    logger.info("盘中引擎启动：%d 只（池口径=%s），间隔 %.1f 秒，去重=%s，风控 %s"
                "（买入前 %d 只、今日买入 %d 只锁 T+1、可用资金 %s、卖出只报持仓 %d 只）",
                len(pool), "选股命中+持仓" if plan_loader else ("全市场" if args.all else "手工清单"),
                engine_cfg.interval,
                "当天每票每信号只报一次" if engine_cfg.once_per_day
                else f"冷却 {engine_cfg.cooldown} 秒",
                "开" if risk.cfg.enabled else "关",
                risk.cfg.max_buy_codes, len(engine_cfg.bought_today),
                f"{engine_cfg.cash_left:.0f} 元" if engine_cfg.cash_left is not None else "未给",
                len(engine_cfg.held))
    engine.run(rounds=args.rounds or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
