"""盘中行情上行：把腾讯快照轮询成 redis 里的最新价 + tick 流，供策略引擎消费。

产出（redis db 5，键约定见 stock_selector/realtime/bus.py）：
    rt:quote      每只票的最新快照（Hash）
    rt:ticks      只有变化才发的 tick 流（Stream，MAXLEN 限长）
    rt:heartbeat  上一轮统计，判断链路是否还活着

链路需求里写的是「websocket 服务 → 消息队列」，这里落地为「免费快照轮询 → redis stream」：
本机没有正式行情授权，腾讯快照是可用口径里最快的一种（全市场一拍约 11 秒）；换真实
Level-1 源时只替换 `stock_selector/quote/tencent.py` 的 fetch，上游键约定与下游引擎都不用改。

股票池口径：`--from-app` 只盯「最近一次选股命中 + 当前持仓」（约 360 只，换日自动重取），
`--all` 才是全市场 6600 只。盯全市场在 15 秒一拍里只是把免费接口的配额吃光——盘中真正要动
的票只有等买点的命中票和等卖点的持仓票。

用法:
    python scripts/quote_service.py --code 600000 000001                # 调试两只
    python scripts/quote_service.py --watchlist data/watchlist.txt
    python scripts/quote_service.py --from-app --interval 5             # 选股命中+持仓（推荐口径）
    python scripts/quote_service.py --all --interval 15                 # 全市场（限流风险自担）
    python scripts/quote_service.py --rounds 1 --limit 50               # 冒烟一轮
    python scripts/quote_service.py --status                            # 看链路心跳
"""

import argparse
import logging
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_selector.quote import tencent
from stock_selector.realtime import QuotePoller, bus, resolve_universe, session
from stock_selector.realtime import pool as app_pool
from stock_selector.realtime.pool import PoolError

logger = logging.getLogger("quote_service")


def read_watchlist(path: Path) -> list[str]:
    """一行一个代码，`#` 起头是注释。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()]


def show_status(client) -> int:
    stats = bus.last_heartbeat(client)
    if not stats:
        print(f"{bus.KEY_HEARTBEAT} 为空：轮询服务还没跑过（或 TTL 已过期）")
        return 1
    print(f"最新一轮 #{stats.get('round')} 状态={stats.get('status')} "
          f"拉到={stats.get('quotes')}/{stats.get('requested')} 新 tick={stats.get('ticks')} "
          f"耗时={stats.get('elapsed_s')}s")
    print(f"{bus.KEY_TICKS} 长度={bus.stream_len(client)}  "
          f"{bus.KEY_QUOTE} 只数={client.hlen(bus.KEY_QUOTE)}")
    print("是否在交易时段:", session.in_trading_window())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="盘中快照轮询上行 redis（策略引擎的上游）")
    p.add_argument("--code", nargs="*", default=[], help="自选代码（六位，可带 sh/sz 前缀）")
    p.add_argument("--watchlist", type=Path, help="自选清单文件（一行一个代码）")
    p.add_argument("--all", action="store_true", help="股票池取本地指标宽表的全部代码（全市场）")
    p.add_argument("--from-app", action="store_true",
                   help="股票池=最近一次选股命中 + 当前持仓（读 app.db，换日自动重取）")
    p.add_argument("--app-db", type=Path, default=app_pool.DEFAULT_DB, help="app.db 路径")
    p.add_argument("--limit", type=int, default=0, help="股票池上限，0=不限")
    p.add_argument("--interval", type=float, default=5.0, help="两轮之间的秒数")
    p.add_argument("--timeout", type=float, default=6.0, help="单次请求超时")
    p.add_argument("--rounds", type=int, default=0, help="跑多少轮后退出，0=常驻")
    p.add_argument("--maxlen", type=int, default=bus.DEFAULT_MAXLEN, help="tick 流保留长度")
    p.add_argument("--host", default=bus.HOST)
    p.add_argument("--port", type=int, default=bus.PORT)
    p.add_argument("--db", type=int, default=bus.DB)
    p.add_argument("--status", action="store_true", help="只看链路心跳，不轮询")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    codes = list(args.code)
    if args.watchlist:
        codes += read_watchlist(args.watchlist)
    client = bus.connect(args.host, args.port, args.db)
    if args.status:
        return show_status(client)

    universe: list[str] = []
    plan_loader = None
    if args.from_app:
        # 换日自动重取：盘后 17:30 那批选股是第二天要盯的名单，只在启动读一次会静默过期
        plan_loader = partial(app_pool.load, args.app_db, limit=args.limit, extra=codes)
        try:
            universe = plan_loader().codes
        except PoolError as exc:
            raise SystemExit(f"--from-app 取池失败：{exc}") from exc
    else:
        universe = resolve_universe(codes, all_market=args.all, limit=args.limit)
    if not universe:
        # 空池不是「今天没票」而是参数错了，必须显式说，不能静默 0 轮退出
        raise SystemExit("股票池为空：请给 --code/--watchlist，或用 --all 取全市场、--from-app 取选股+持仓")

    poller = QuotePoller(client, universe, interval=args.interval, maxlen=args.maxlen,
                         fetch=partial(tencent.fetch, timeout=args.timeout),
                         universe_loader=(lambda: plan_loader().codes) if plan_loader else None)
    logger.info("开始轮询 %d 只，间隔 %.1f 秒", len(universe), args.interval)
    poller.run(rounds=args.rounds or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
