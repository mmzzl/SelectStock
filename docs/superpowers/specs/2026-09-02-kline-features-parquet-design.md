# K线指标预计算脚本 - 设计文档

> 日期：2026-09-02

## 1. 目的

将 MongoDB 中的日 K 线数据 + talib 预计算的 MA/EMA/MACD/RSI/KDJ/WR/ATR/BOLL/OBV/VOL_MA/ROC 指标，
按股票分文件存入 parquet，支持增量更新。

## 2. 数据源（已探查确认）

- MongoDB: `eastmoney_news` 库，`stock_kline` 集合（1202 万条日线）
- 过滤条件：`frequency=9, adjust='qfq'`（主日线数据，2015-12-07 至今持续更新）
  - `frequency=0` 序列已停更（2022-12~2026-05），弃用
- 文档字段：`code, date('2025-10-23 15:00' 字符串), open, high, low, close, volume, amount`
- 索引可用：`{code:1, frequency:1, date:-1}` 用于按 code 高效拉取
- 股票名称：`bk_stocks` 集合（stock_code=6位纯数字，stock_name），5566 只

## 3. 输出

- 目录：`data/stock_kline/{code}.parquet`（每只股票一个文件）
- 压缩：`zstd`（`pandas.to_parquet(compression="zstd")`），读取方 `read_parquet` 自动解压，值无变化，仅 CPU/IO 略有开销
- 每行 = 一只股票一天的一条 K 线 + 全部指标（date 去重，`YYYY-MM-DD`）

### 3.1 输出列

| 类别 | 列 |
| --- | --- |
| 标识 | code, name, date |
| 原始K线 | open, high, low, close, volume, amount |
| 均线 | ma5, ma10, ma20, ma60 |
| 指数均线 | ema12, ema26 |
| MACD | macd_dif, macd_dea, macd_hist |
| 震荡 | rsi14 |
| 随机 | kdj_k, kdj_d, kdj_j |
| 威廉 | wr14 |
| 波动 | atr14 |
| 布林 | boll_upper, boll_mid, boll_lower |
| 量能 | obv, vol_ma5 |
| 动量 | roc10 |

## 4. 指标计算方式

- 均线/EMA/MACD/RSI/WR/ATR/BOLL/OBV/ROC → `talib` 原生函数
- KDJ → `talib.STOCH(9,3,3)`（慢K/慢D），J=3K-2D
- VOL_MA5 → pandas rolling(5)

## 5. 增量更新机制

- 首次运行：`--full` 全量构建所有股票
- 增量运行（默认）：
  1. 遍历 `bk_stocks` 全部 code（或 `--code` 指定单只）
  2. 若 `{code}.parquet` 已存在，读取其最大 `date` = last_date
  3. 从 Mongo 拉取 `date > last_date` 的新 K 线（Mongo 只读新增，IO 增量）
  4. 已有全历史（读 parquet）+ 新增行 → **整条序列用 talib/pandas 滑动窗口重算全部指标** → 按 code 加 date 去重 → 写回
- 为什么全序列重算：MA/EMA/MACD 等滑动窗口指标以及**累计型 OBV** 都依赖完整历史。
  若只喂 warm-up 窗口算增量条，连续运行时指标会随 warm-up 起点漂移，和全量结果不一致。
  全序列重算保证任意时刻增量结果与全量重跑结果完全一致；talib 为 C 向量化，单股票 ~2000 行重算为微秒级，开销可忽略。
- 性能：每只股票仅从 Mongo 读每交易日 1 条新增，全市场每日增量 ≈5000 行

## 6. CLI 用法

```bash
python scripts/build_kline_features.py --full          # 首次全量
python scripts/build_kline_features.py                 # 增量（默认）
python scripts/build_kline_features.py --code 600519   # 单只调试
```

## 7. 容错

- 单只股票拉取/计算失败：打印警告并跳过，不影响其他股票
- 目标任务内重试断点：跳过已成功写入（parquet 已含最新 date 的股票）