# stock_select 量化选股系统

盘中数据采集与选股相关脚本。数据以 parquet 落盘（zstd 压缩），支持增量更新。

## 资金流采集脚本 scripts/fetch_fund_flow.py

抓取三类资金流数据并写入 parquet：个股资金流向历史日线、个股资金流向排行快照、行业资金流向快照。

> 反爬说明：东方财富接口（个股历史、个股排行）反爬严重时不可用。排行已默认切到同花顺（稳定）；个股历史日线仅东财有，脚本带指数退避重试，网络恢复后自动补齐。

### 数据结构

```
data/fund_flow/
├── individual/{code}.parquet    # 个股资金流向历史日线：主键 code,date，列含主力/超大单/大单/中单/小单 净额与净占比
├── rank/YYYY-MM-DD.parquet      # 个股排行快照：主键 collect_date,code,indicator(今日/3日/5日/10日/20日)，列含流入/流出/净额/换手率
└── industry/YYYY-MM-DD.parquet  # 行业快照：主键 collect_date,industry，列含净额涨跌幅/领涨股
```

### 用法

```bash
# 抓取今日三类数据（排行默认同花顺）
python scripts/fetch_fund_flow.py

# 只抓某一类
python scripts/fetch_fund_flow.py --only individual   # 个股历史日线
python scripts/fetch_fund_flow.py --only rank         # 个股排行快照
python scripts/fetch_fund_flow.py --only industry     # 行业快照

# 回补最近 30 个自然日的快照 (rank/industry)
python scripts/fetch_fund_flow.py --backfill 30

# 指定股票抓个股历史；可多次传参
python scripts/fetch_fund_flow.py --stock 600094 --stock 000001

# 排行改用东方财富数据源（默认同花顺，东财易被反爬）
python scripts/fetch_fund_flow.py --only rank --source em
```

> 个股历史的默认股票池取最近一日 rank 快照里的全部代码；无快照时需用 `--stock` 指定。

日志输出到 `logs/fund_flow_YYYYMMDD.log`。

## 新闻解读脚本 scripts/analyze_news_llm.py

用智谱 GLM 解读 MongoDB（`stock_selector/config/config.yaml` 中的 mongodb 配置）里的东财新闻，落盘**两层结构**（parquet + MongoDB）：

- **第一层 news_analysis**：每条新闻一条记录，含摘要/情绪/利好利空标的/风险/关注清单等
- **第二层 watch_target**：由利好/利空/关注/风险拆解并按 "标的+方向" 当日合并，支持"今天关注哪些股票/板块"的聚合与跨天统计

设计文档：`docs/superpowers/specs/2026-09-07-news-llm-analysis-two-layer.md`。

### 配置

`config.yaml` 中已加 `llm` 节和 `mongodb` 节。api_key 留空时读环境变量 `ZHIPU_API_KEY`：

```yaml
llm:
  provider: "zhipu"
  api_key: ""            # 或 export ZHIPU_API_KEY=xxx
  model: "GLM-4.7-Flash"
  temperature: 0.3
  max_tokens: 2048
  timeout: 60

mongodb:
  host: "127.0.0.1"
  port: 27017
  database: "eastmoney_news"
  collection: "news"
```

### 数据源与名称回填

- 输入：`eastmoney_news.news` 集合，字段映射：`summary`→正文、`showTime`→发布时间（`YYYY-MM-DD HH:MM:SS` 字符串）、`stockList`→相关标的（secid 格式）
- `--days` 按**自然日**过滤（`showTime >= 今天-N天 的 00:00:00`），不是 24h 滑动窗口
- **名称回填**：`stockList` 的 secid 只有代码，运行时从 `bk_stocks`/`sector_stocks` 查询 代码→名称，hint 带上真实个股/板块名称（供 LLM prompt 与 watch_target name 回填使用）；查不到的保持空

### 数据结构

```
data/news_analysis/YYYY-MM-DD.parquet   # 第一层：news_id 增量去重合并，保留最新
data/news_watch/YYYY-MM-DD.parquet      # 第二层：当日全量拆解合并，整文件覆盖
MongoDB eastmoney_news.news_analysis    # 第一层副本（_id = collect_date|news_id，整批重建）
MongoDB eastmoney_news.watch_target     # 第二层副本（_id = md5 key，整日 delete+insert）
```

第一层量化字段：`sentiment`(利好/利空/中性)、`sentiment_score`(-10~10 强度)、`impact_period`(短期/中期/长期)、
`bullish`/`bearish`(利好/利空标的清单：`[{category, name, code, score, reason}]`，score 利好为 +1~10、利空为 -10~-1)、
`risks`(利好兑现的风险提示：`[{score, desc, impact, mitigation}]`，score 为 -10~-1)、
`watchlist`(当前应重点关注：`[{category, name, code, priority, reason}]`，priority=高/中/低)、
`follow_signals`(后续需跟踪验证的信号)、`action`(当下操作含义)、`stock_hints`(归一化后的相关代码)、
`summary`/`market_reaction`/`conclusion`(LLM 观点文本)、`raw`(LLM 原始输出)。

### 用法

```bash
# 分析最近 1 天新闻（自然日），写两层 parquet + Mongo
python scripts/analyze_news_llm.py

python scripts/analyze_news_llm.py --days 3        # 最近3天
python scripts/analyze_news_llm.py --limit 10      # 最多10条
python scripts/analyze_news_llm.py --demo          # 内置示例新闻联调（Mongo 空/读失败时自动兜底）
python scripts/analyze_news_llm.py --no-mongo      # 只写 parquet，不写 Mongo（调试/CI）
```

运行后控制台输出 `=== 分析摘要 ===`（逐条）+ `=== 今日关注 ===`（当日 watch_target 聚合）。

### 跨天统计（不调 LLM）

```bash
# 热度排行：近 N 天 bullish 标的，按 code 聚合 news_count/total_score
python scripts/analyze_news_llm.py --stats heat --start 2026-09-01 --end 2026-09-08 --category 板块 --top 20

# 个股/板块趋势：逐日净分 + 累计（cumsum）
python scripts/analyze_news_llm.py --stats trend --code BK0464 --start 2026-09-01 --end 2026-09-08
```

`--stats` 与分析互斥；默认查 Mongo（`--no-mongo` 时退到 parquet 惰性聚合）；`start`/`end` 为 `YYYY-MM-DD`，`end` 默认今天+1。`trend` 必填 `--code`。

### 容错

- Mongo 读失败/无新闻 → 自动退化 `--demo`
- 单条 LLM 分析失败 → 重试 3 次 + 降级中性行（不产 watch_target），当日文件仍落盘
- Mongo 写失败 → WARNING 日志，不影响 parquet（parquet 为准）
- 非 dict 元素 → 跳过并 WARNING

日志输出到 `logs/news_analyze_YYYYMMDD.log`。