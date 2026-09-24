"""统一内核调度入口（系统唯一对外核心函数）。

调度全流程：
1. 加载配置
2. 加载全部策略插件
3. 调用数据源获取股票列表、K 线数据（每只股票只取数一次）
4. 对该股票遍历执行全部目标策略
5. 汇总标准化结果返回
"""

import logging
from pathlib import Path

from ..datasource import AKShareSource, BaseDataSource, MongoSource, ParquetSource
from ..models import SelectResult
from ..utils.io_helper import to_json
from .strategy_loader import StrategyLoader

logger = logging.getLogger("stock_selector")


def _load_config() -> dict:
    """加载全局配置文件 settings.yaml。"""
    import yaml

    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _create_datasource(config: dict) -> BaseDataSource:
    """根据 datasource.type 创建数据源实例。

    数据源不可用时直接报错，不再静默降级为模拟数据——历史上 akshare 拉不到
    行情会被 tolerate_errors 吞掉，表现为"选股永远 0 命中"而非明确故障。
    """
    ds_cfg = config.get("datasource") or {}
    ds_type = ds_cfg.get("type", "mongo")

    if ds_type == "parquet":
        source = ParquetSource(_sub(ds_cfg, "parquet"))
        if not source.available():
            raise RuntimeError(
                f"本地指标宽表不可用（目录 {source.dir} 无 parquet 文件），"
                "请先执行 python scripts/build_kline_features.py 或切换 datasource.type"
            )
        return source
    if ds_type == "mongo":
        return MongoSource(_sub(ds_cfg, "mongo"))
    if ds_type == "ak":
        try:
            import akshare  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("datasource.type=ak 需要安装 akshare 且外网可达") from exc
        return AKShareSource(_sub(ds_cfg, "ak"))
    if ds_type == "mock":
        from ..datasource.mock_source import MockSource

        return MockSource(_sub(ds_cfg, "ak"))
    raise RuntimeError(f"未知数据源类型: {ds_type}（可选 mongo/parquet/ak/mock）")


def _sub(ds_cfg: dict, key: str) -> dict:
    return ds_cfg.get(key) or {}


def list_strategies(config: dict | None = None) -> list[str]:
    """获取所有可用策略名称（供前端下拉、接口暴露）。"""
    params = (config or _load_config()).get("strategy_params") or {}
    return StrategyLoader(params).load_all().names()


def resolve_strategies(target: list[str] | str | None, loader: StrategyLoader,
                       config: dict) -> list[str]:
    """确定本次需要执行的策略；未注册的名称直接拒绝，避免静默空跑。"""
    if target is None:
        names = (config.get("selector") or {}).get("default_strategies") or loader.names()
    elif isinstance(target, str):
        names = [s.strip() for s in target.split(",") if s.strip()]
    else:
        names = [str(s).strip() for s in target if str(s).strip()]

    available = loader.names()
    unknown = [n for n in names if n.lower() not in {a.lower() for a in available}]
    if unknown:
        raise ValueError(f"未注册的策略: {', '.join(unknown)}；可用策略: {', '.join(available)}")
    return names


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

    Raises:
        ValueError: 指定了未注册的策略名。
        RuntimeError: 数据源未返回任何行情（全市场取数失败）。
    """
    config = config or _load_config()
    params = config.get("strategy_params") or {}
    selector_cfg = config.get("selector") or {}

    # 1. 加载全部策略插件，构建注册表
    loader = StrategyLoader(params).load_all()
    target = resolve_strategies(strategies, loader, config)
    # 每股只取数一次，遍历全部策略复用，避免"策略 × 股票"重复拉取
    instances = [(name, loader.create(name)) for name in target]

    # 2. 创建数据源并获取股票列表
    owns_source = datasource is None
    ds = datasource or _create_datasource(config)
    try:
        stock_list = ds.get_stock_list()
        limit = int(selector_cfg.get("stock_limit") or 0)
        if limit > 0:
            stock_list = stock_list[:limit]

        tolerate = selector_cfg.get("tolerate_errors", True)
        results: list[SelectResult] = []
        loaded = 0
        failed = 0

        # 3. 遍历股票取数一次，执行全部目标策略
        for stock in stock_list:
            code = str(stock.get("code", ""))
            if not code:
                continue
            try:
                df = ds.get_kline(code)
            except Exception as exc:
                failed += 1
                logger.warning("取数失败 %s: %s", code, exc)
                if not tolerate:
                    raise
                continue
            if df is None or df.empty:
                continue
            loaded += 1

            for name, strategy in instances:
                try:
                    hits = strategy.run(df) or []
                except Exception as exc:
                    failed += 1
                    logger.warning("策略 %s 处理 %s 失败: %s", name, code, exc)
                    if not tolerate:
                        raise
                    continue
                for r in hits:
                    if not r.code:
                        r.code = code
                    if not r.name:
                        r.name = stock.get("name") or ds.name_of(code)
                    if not r.signal:
                        r.signal = name
                    results.append(r)

        # 4. 全市场一条行情都没取到属于数据源故障，必须显式暴露而非返回空集
        if stock_list and loaded == 0:
            raise RuntimeError(
                f"数据源 {type(ds).__name__} 未返回任何行情（股票池 {len(stock_list)} 只，"
                f"失败 {failed} 次），请检查 datasource.type 配置与数据是否已生成"
            )
        logger.info(
            "选股完成: 策略=%s 股票池=%d 有效=%d 命中=%d 失败=%d 基准日=%s",
            target, len(stock_list), loaded, len(results), failed, ds.max_date(),
        )
    finally:
        if owns_source:
            ds.close()

    # 5. 汇总返回
    return to_json(results) if as_json else results
