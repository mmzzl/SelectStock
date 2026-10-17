"""资金流采集层：把东财全市场单日批量快照落成「按交易日期分区」的可积累存储。

设计动机（2026-10-17，任务 #171 阶段 1）：东财逐只历史接口一天只能覆盖 134/5,215 只
（IP 被节流，重试用尽就放弃），攒不出「连续 12 个月无断档」；改成全市场单日批量接口后
一天 60 页就能拿齐，代价是只能取「当天」，历史一过就补不回来——所以本层的唯一职责是
**把每一天忠实地钉成一个文件，并且让「缺哪天」一眼可见**。

约定三件事（口径真源在 `stock_selector/docs/资金流数据口径与防前瞻红线.md`）：
- 分区键是**数据自己的交易日**（由接口返回的数据时刻推出），不是采集当天——周末重跑只会
  刷新同一个交易日，不会造出「周六的资金流」。
- 主键 `(trade_date, code)` 唯一；同名日分区合并时**新值非空才覆盖旧值**，缺数不会把好数据擦掉。
- 每行都带 `data_ts`（接口标的行情时刻）与 `collected_at`（本次抓取时刻）两个时间戳，
  防前瞻（T 日数据只能用于 T+1 决策）靠这两个字段可审计，不靠口头承诺。
"""

from .store import (
    DAILY_SUBDIR,
    DAILY_COLUMNS,
    expected_trading_days,
    gap_report,
    merge_partition,
    normalize_and_flag,
    partition_path,
    partition_rows,
    stored_dates,
    write_partitions,
)
