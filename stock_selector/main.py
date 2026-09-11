"""入口1：命令行独立运行。

用法：
    python -m stock_selector.main
    python -m stock_selector.main --strategies trend_rebound,box_oscillate --json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_selector.core.selector import list_strategies, run_select


def main() -> None:
    parser = argparse.ArgumentParser(description="插件化量化选股系统 - 命令行入口")
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="指定策略名称，逗号分隔；缺省使用配置默认策略",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出标准 JSON",
    )
    args = parser.parse_args()

    print(f"可用策略: {list_strategies()}")
    results = run_select(strategies=args.strategies, as_json=True)

    if args.json:
        print(json.dumps(json.loads(results), ensure_ascii=False, indent=2))
    else:
        print(results)


if __name__ == "__main__":
    main()