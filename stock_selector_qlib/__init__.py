"""qlib 插件：把「训练好的 qlib 模型」以与 stock_selector(8002) 相同的形状对外服务。

替换方式（预注册在 stock_selector/docs/qlib路线数据口径与验收门槛.md §9）：
- 选股：本包 api.py 是与 8002 同契约的 FastAPI 应用（同样的三条路由、同样的
  {"code":0,"data":[SelectResult.to_dict()]}），后端只改 settings.selection_plugin_base_url
  就换引擎，代码不用动。
- 回测：没有 HTTP 缝（Celery 任务 backtest.run 直接 import 内核），所以换的是
  backtest_tasks 里的 engine 分派，产出的摘要字段与自研内核完全同形状。

红线：服务进程不训练、不加载全历史面板。分数只由「落盘的 booster + 落盘的稳健标准化
统计 + 短窗 Alpha158」决定，所以它必须和 scripts/qlib_run.py 的分数逐只相同。
"""
