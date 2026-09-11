"""统一内核调度入口（系统唯一对外核心函数）。

调度全流程：
1. 加载配置
2. 加载全部策略插件
3. 调用数据源获取股票列表、K 线数据
4. 遍历执行指定选股策略
5. 汇总标准化结果返回
"""

from pathlib import Path

from ..datasource import AKShareSource, BaseDataSource
from ..models import SelectResult
from ..utils.io_helper import to_json
from .strategy_loader import StrategyLoader


def _load_config() -> dict:
    """加载全局配置文件 settings.yaml。"""
    import yaml

    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _create_datasource(config: dict) -> BaseDataSource:
    """根据配置创建数据源实例（无 akshare 时降级为内置模拟数据源）。"""
    ds_type = (config.get("datasource") or {}).get("type", "ak")
    ak_config = (config.get("datasource") or {}).get("ak") or {}
    try:
        import akshare  # noqa: F401
    except ImportError:
        from ..datasource.mock_source import MockSource
        return MockSource(ak_config)
    return AKShareSource(ak_config)


def list_strategies(config: dict | None = None) -> list[str]:
    """获取所有可用策略名称（供前端下拉、接口暴露）。"""
    params = (config or _load_config()).get("strategy_params") or {}
    return StrategyLoader(params).load_all().names()


def run_select(
    strategies: list[str] | str | None = None,
    datasource: BaseDataSource | None = None,
    config: dict | None = None,
    as_json: bool = False,
):
    """执行选股，返回标准化结果列表。

    Args:
        strategies: 指定需要执行的策略名称（字符串或列表），None 使用配置默认策略。
        datasource: 自定义数据源实例；None 则按配置创建。
        config: 注入的配置字典；None 则加载 settings.yaml。
        as_json: 为 True 时返回 JSON 字符串，否则返回 list[SelectResult]。

    Returns:
        list[SelectResult] 或 JSON 字符串。
    """
    config = config or _load_config()
    params = config.get("strategy_params") or {}
    selector_cfg = config.get("selector") or {}

    # 1. 加载全部策略插件，构建注册表
    loader = StrategyLoader(params).load_all()

    # 2. 确定需要执行的策略
    if strategies is None:
        target = selector_cfg.get("default_strategies") or loader.names()
    elif isinstance(strategies, str):
        target = [s.strip() for s in strategies.split(",") if s.strip()]
    else:
        target = list(strategies)

    # 3. 创建数据源并获取数据
    ds = datasource or _create_datasource(config)
    stock_list = ds.get_stock_list()

    tolerate = selector_cfg.get("tolerate_errors", True)
    results: list[SelectResult] = []

    # 4. 遍历目标策略执行选股
    for name in target:
        try:
            strategy = loader.create(name)
        except KeyError:
            continue  # 跳过不存在的策略，不影响整体

        for stock in stock_list:
            try:
                df = ds.get_kline(stock["code"])
                if df is None or df.empty:
                    continue
                sub = strategy.run(df)
                for r in sub or []:
                    # 补充策略未填的 code/name（默认从当前股票补齐）
                    if not r.code:
                        r.code = stock["code"]
                    if not r.name:
                        r.name = stock.get("name", "")
                results.extend(sub or [])
            except Exception:
                if not tolerate:
                    raise

    # 5. 汇总返回
    return to_json(results) if as_json else results
