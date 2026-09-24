# stock_select 量化选股系统

盘中数据采集与选股相关脚本。数据以 parquet 落盘（zstd 压缩），支持增量更新。

## 系统总览

```
指标面   data/stock_kline/{code}.parquet  ◄── scripts/build_kline_features.py
         （选股 / 回测 / 诊断共用）        ◄── MongoDB eastmoney_news.stock_kline 原始日线
                 │
                 ▼
计算层   scripts/（薄 CLI）            stock_selector/（选股插件·实现层）
           fetch_fund_flow.py 资金流     core/        策略契约·插件加载·run_select 内核
           analyze_news_llm.py 消息面    datasource/  parquet | mongo | ak | mock
           backtest_main.py    回测      strategies/  可插拔策略（放进目录即注册）
           train_bayes.py      模型      backtest/    逐日撮合引擎 + Broker + 规则 DSL
           notice_*.py         公告      features/    指标与窗口因子、模型拟合
                 │                        news/        消息面两层解读
                 ▼  HTTP /api/selector/run · /api/strategy/list
编排层   stock_selector_api/  FastAPI 后端：任务编排 + select_jobs/select_results 落库，零财务计算
                 │  REST /api/v1/*  +  WebSocket /ws/notify
                 ▼
展示层   uniapp/  Vue 移动端：只展示、不计算
```

四层职责严格单向：**脚本产数据 → 插件算信号 → 后端编排落库 → 前端展示**。
下面一节先讲选股插件（数据流、内核、策略、回测），其后是各采集脚本的用法。
本地多服务端口分配与联调清单见 `uniapp/README.md`。

## 插件化量化选股系统 stock_selector/

策略插件化的选股内核，三种运行形态复用同一个 `run_select()`，结果完全一致。需求与架构见
`docs/spec/插件化量化选股系统 - 需求与架构设计文档.md`，技术因子手册见 `stock_selector/docs/READ.me`。

### 分层架构

```
stock_selector/                  # 实现层：全部业务与算法
├── main.py          # 入口1：命令行（薄）
├── api.py           # 入口2：HTTP 服务（薄适配层，零业务）
├── core/            # 框架层：策略契约 base_strategy / 插件加载 strategy_loader / 唯一内核 selector
├── strategies/      # 策略插件层：新增策略只在此目录加文件
├── datasource/      # 数据源层：parquet / mongo / ak / mock，统一 BaseDataSource 契约
├── models/          # 标准化结果结构体 SelectResult
├── features/        # 指标与因子：base_columns 派生列 / window 12 因子 / modeling 拟合评估
├── backtest/        # 回测：engine 逐日撮合 / broker 资金与 T+1 / rules + rule_eval 规则 DSL / metrics / store
├── news/            # 消息面：LLM 两层解读（fetch / analyze / targets / store / stats）
└── utils/           # io_helper 等

scripts/                         # 薄 CLI：只解析参数，再调用上面的实现
```

> 全仓一致的分工：**可复用逻辑放 `stock_selector/`，`scripts/` 只做命令行壳**。
> `backtest_main.py`、`analyze_news_llm.py`、`train_bayes.py` 都是这个模式，便于被调度器、测试和 HTTP 复用。

四条架构红线：

1. **`api.py` 零业务** —— 只收参数、调内核、序列化，保证 HTTP 结果与命令行结果 100% 一致。
2. **单内核** —— `run_select()` 是系统唯一对外核心函数，不存在第二套选股实现。
3. **不静默降级** —— 数据源不可用、全市场零行情一律显式 `RuntimeError`（这是历史上"选股永远 0 命中"的根因）。
4. **财务计算不下沉到界面** —— 评分、收益、仓位等算术归本插件，后端与 uniapp 只做展示与编排。

### 数据流与"同源"约束

```
MongoDB eastmoney_news.stock_kline        # 1200 万+ 原始日线（frequency:9, adjust:qfq）
   │  scripts/build_kline_features.py     # talib 指标 + pct/lu/vol_ratio/pos60
   ▼
data/stock_kline/{code}.parquet           # 5186 只 × 36 列 指标宽表（唯一共享数据面）
   ├─ 选股  ParquetSource（剪列 + 行窗口）─► run_select() ─► list[SelectResult]
   ├─ 回测  ParquetProvider（逐日 rows/price）─► BacktestEngine
   ├─ 诊断  KlineProvider（app 侧插件）
   └─ 模型  features/window ─► train_bayes ─► models/bayes_limitup.joblib
                                             + data/bayes_candidates/{date}.parquet
```

**同源**是硬约束：选股、回测、诊断读同一批 parquet 文件、同一套列语义，
`RuleEvaluator` 的指标白名单与 parquet 列一一对应 —— 这样"策略在历史上是否会买"和
"策略今天选出什么"可互相验证。因此切换数据源不需要改任何策略代码，但换数据源会同时改变新鲜度与耗时。

### 核心运行逻辑（内核 `run_select`）

1. `StrategyLoader.load_all()` 运行时扫描 `strategies/` 包，按 `obj().name` 建注册表
   —— 探测实例只用于取名，参数统一由 `create()` 注入。
2. `resolve_strategies()` 决定本次执行的策略：`None` 用 `settings.selector.default_strategies`；
   传了未注册的名字直接 `ValueError`（列出可用策略），避免静默空跑。
3. 数据源：`datasource=` 参数优先（测试与回测可注入），否则按 `datasource.type` 创建；
   只有自己创建的实例才在 `finally` 里 `close()`。
4. **每股只取数一次**，内层遍历全部策略 —— IO 次数 = 股票数，与策略个数无关。
5. 结果归位：策略可留空 `code/name/signal`，内核用取数上下文回填
   （code ← 循环变量，name ← 股票列表或 `ds.name_of(code)`，signal ← 策略名）。
6. 单只股票取数或策略求值异常：`tolerate_errors: true` 记警告继续，`false` 立即抛出；
   股票池非空却 `loaded == 0` 视为数据源故障，抛 `RuntimeError`。

结束时输出 `选股完成: 策略=… 股票池=… 有效=… 命中=… 失败=… 基准日=…`，
基准日即 `ds.max_date()`，也就是这批信号对应的交易日。

### 三种用法

```bash
# 1) 命令行：默认策略全市场选股
python -m stock_selector.main
python -m stock_selector.main --strategies trend_rebound --json

# 2) Python 模块导入
python -c "from stock_selector import run_select; print(len(run_select()))"

# 3) HTTP 服务（供后端/前端调用）
bash scripts/start_selector.sh                     # 默认 0.0.0.0:8002
python -m uvicorn stock_selector.api:app --host 127.0.0.1 --port 18002
```

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET /api/strategy/list` | `{"code":0,"data":["box_oscillate","trend_rebound"]}`，后端下拉策略来源 |
| `POST /api/selector/run` | 入参 `{"strategies":[...]}`（缺省用配置默认），返回 `data:[{code,name,signal,reason,extra}]` |

后端 `stock_selector_api` 通过环境变量 `SELECTION_PLUGIN_BASE_URL` 指向本服务，配置项
`selection_plugin_mode: http|mock`。

### 调用链与接口契约

```
uniapp  ──/api/v1/selection/*──►  后端 stock_selector_api  ──HTTP──►  本插件(api.py) ──► run_select()
```

后端 `services/selection_service.py` 的消费方式决定了返回结构不能随意改：

- `GET /api/strategy/list` 的 `data` **必须是 `list[str]`**，后端直接 `list(...)` 当下拉数据源。
- `POST /api/selector/run` 后端只映射 `code/name/signal/reason` 四个字段（`signal` 缺省回落到 `action`），
  落库到 `select_results`（也只有这 4 列），httpx 超时 600s。
- 后端 job 状态机 `queued/running/done/failed`，`_inflight` 单飞（同一时刻只有一个选股在跑）。

因此**扩充返回字段是一次三方联动**：插件 `SelectResult` → 后端 `select_results` 的幂等 `ALTER TABLE`
→ uniapp `logic/view.js` 的展示映射。改结构请三处同步，否则新字段会被后端丢弃、前端不可见。

### 数据源（`stock_selector/config/settings.yaml` 的 `datasource.type`）

| type | 来源 | 适用 | 全市场实测(5186 只) |
|---|---|---|---|
| `parquet`（默认） | `data/stock_kline/{code}.parquet` | 常规选股；与回测/诊断同一数据面，含 30+ talib 指标 | 6 列+120 行 **163s**；全 36 列 423s |
| `mongo` | MongoDB `stock_kline`（`frequency:9, adjust:qfq`） | 行情要求最新、或只查少量代码 | ~0.5s/只 ≈ 40 分钟，不适合全市场 |
| `ak` | akshare 东财接口 | 需外网可达（本机实测已不可达） | 不可用 |
| `mock` | 内置模拟数据 | 演示/测试 | 秒级 |

指标宽表由采集脚本生成，选股前需保证数据已更新：

```bash
python scripts/build_kline_features.py            # 增量更新（默认）
python scripts/build_kline_features.py --full     # 全量重建所有股票
python scripts/build_kline_features.py --code 600519   # 只重建指定一只
python scripts/build_kline_features.py --limit 20      # 只处理前 20 只（联调用；0=不限）
```

`parquet.columns` 可剪列（越大提速越多），`parquet.rows` 只取最近 N 个交易日；相对目录按仓库根解析，
服务从任意工作目录启动都能读到数据。数据源不可用会**显式报错**而非静默降级为 mock。

四种数据源都实现同一个 `BaseDataSource` 契约，策略层对此完全无感：

| 方法 | 作用 |
|---|---|
| `get_stock_list()` | 股票池 `[{"code","name"}]` |
| `get_kline(code)` | 单只标准化日线 DataFrame（`date` 升序） |
| `name_of(code)` | 代码→名称，供内核回填结果（可选） |
| `max_date()` | 本批数据的基准交易日（可选） |
| `close()` | 释放连接（可选） |

`ParquetSource` 为控内存**不缓存行情帧**，只保留代码→名称映射（全市场 5000+ 只 × 1800 行缓存会吃掉数 GB）；
跨请求复用请由调用方以子进程/任务方式管理。`MongoSource` 支持 `600519` / `600519.SH` / `SH600519` 三种代码写法，
连接参数缺省时读 `stock_selector/config/config.yaml` 的 `mongodb` 节。

### 配置骨架（`stock_selector/config/settings.yaml`）

```yaml
datasource:
  type: parquet            # parquet | mongo | ak | mock —— 切换不影响策略代码
  parquet: {dir: "data/stock_kline", columns: [...], rows: 120}
selector:
  default_strategies: ["trend_rebound", "box_oscillate"]
  tolerate_errors: true    # 单票失败是否继续
  stock_limit: 0           # 股票池上限，0=不限（调试常用 200）
strategy_params:
  trend_rebound: {period: 20, pullback_pct: 0.05}
server: {host: "0.0.0.0", port: 8000}
```

`run_select(config=...)` 可直接注入字典覆盖文件配置，这是测试与回测复用内核的入口。

### 策略插件层与新增策略

策略只依赖一个抽象契约，框架不 import 任何具体策略（依赖倒置）：

```python
# stock_selector/strategies/my_strategy.py
from ..core.base_strategy import BaseSelectStrategy
from ..models.result import SelectResult

class MyStrategy(BaseSelectStrategy):
    def run(self, df) -> list[SelectResult]:
        period = int(self.config.get("period", 20))   # 参数务必带默认值
        ...
        return [SelectResult(code="", name="", signal="my_strategy", reason="…")]
```

约定：

- **放进目录即注册** —— `StrategyLoader` 运行时扫描 `strategies/`，无需改任何框架代码或 import 清单。
- `name` 默认由类名蛇形派生（`TrendRebound` → `trend_rebound`），是接口、参数、`signal` 三处共用的标识。
- 参数来自 `settings.yaml` 的 `strategy_params.<策略名>`，通过 `self.config` 读取。
- `code/name/signal` 可留空，内核按取数上下文回填；`reason` 是给人看的中文依据。
- 当前两个内置策略：`trend_rebound`（MA20 斜率向上 + 回踩均线 ±5% 内 + 当日收阳企稳）、
  `box_oscillate`（前 30 日箱体〔不含当日〕，收盘有效突破上沿 +2% 且前一日尚未破位）。
- **局限**：策略看到的是"整段历史 + 最后一行"，判据落在最新交易日；逐日 as-of（只看 ≤T 的数据）
  属于 P1，做完才能做到"同一策略既能选股又能被回测逐日验证"。

```bash
python -m pytest tests -q          # 内核/数据源/策略/特征/回测/消息面 160 项
```

### 特征与模型层（`features/`）

- `base_columns.py`：板块感知涨停线（主板 9.8 / 创业板·科创板 19.5 / 北交所 29.5），派生
  `pct`、`lu`（涨停标记）、`vol_ratio`（量比）、`pos60`（60 日高低分位）。
- `window.py`：20 日窗口 12 个量价因子（`pct_20d / slope_20d / pos_60d / ma5_ma20 / vol_ratio_avg /
  up_day_vol_ratio / vol_slope / vol_corr_pct / drawdown_20d / test_limit_cnt / limit_up_early / max_gain_3d`）；
  成交量按 8× 滚动中位数 clip 抑制异常；`mp.Pool` 并行构建全市场数据集。
- `modeling.py`：`GaussianNB`（调高正类先验，把概率当排序分用）、按日期 80/20 **时间切分防泄漏**
  （训练集全部早于验证集），指标为 `auc / avg_precision / prec@50,100,200 / base_rate`。
- 训练与产出（`--train` / `--predict` / `--code` 三者必须且只能选一个）：

```bash
python scripts/train_bayes.py --train               # → models/bayes_limitup.joblib + reports/bayes_train_report.md
python scripts/train_bayes.py --predict --top-n 100 # → data/bayes_candidates/{基准日}.parquet
python scripts/train_bayes.py --code 600519         # 单票 12 特征与打分诊断
```

### 回测

```bash
python scripts/backtest_main.py --start 2025-09-01 --end 2026-09-01 \
    --capital 100000 --max-positions 5 --position-cap-pct 0.2
```

买卖条件读 MongoDB `trading_rules`（`buy`/`sell`/`add` 布尔表达式，如
`price <= ma10 * 0.8 and adx <= 25 * 1.15`），由 `RuleEvaluator` 在指标列白名单内求值；
结果写入 `backtest_results` / `backtest_trades` / `backtest_equity`。

引擎逻辑（`backtest/engine.py`）—— 逐日推进，每一天严格按此顺序：

1. `broker.settle()`：T+1 资金解冻（昨日卖款今日才并入可用 `cash`，当日卖出款进 `settling` 不可即用）。
2. `evaluate_holdings()` 拿持仓迁移指令 → **先卖** → **再加仓**（`add` 不占持仓只数配额）。
3. 仓位数未满 `max_positions` 时才 `propose_buys()` 买新标的，受 `position_cap_pct`
   （单票成本 / 当前总资产）与 `target_amount`（每笔固定买入金额）约束。
4. 按 `cash + settling + 持仓市值` 记一个权益点。

账户规则（`backtest/broker.py`）：以**当日收盘价**成交、按 100 股整手取整、佣金
`commission_rate` 且有 `commission_min` 下限、卖出另计 `stamp_tax_rate` 印花税、已实现盈亏按持仓均价成本含费计算；
资金不足 / 不足一手 / 超单票上限 / 超持仓只数都抛 `ValueError`，由引擎捕获后跳过该笔（不影响其余撮合）。
停牌（当日 parquet 无该行）不撮合，估值回退到最近一次已知收盘价，避免净值凭空跳变。
`Metrics` 产出 `total_return / annual_return / max_drawdown / win_rate / sharpe / profit_factor / trade_count`
（按 245 交易日年化），`BacktestStore` 落 MongoDB。

策略与引擎之间靠 `_StrategyProxy` 适配：引擎只认 `propose_buys(date, cash, prices)` 和
`evaluate_holdings(date, holdings, prices)` 两个可选回调，没有的方法视为返回空列表 ——
所以选股侧的 `BaseSelectStrategy` 与回测侧的规则策略是两套契约，**统一为同一个 as-of 策略接口是 P1 的核心工作**。

> ⚠️ `RuleEvaluator` 用 `eval()` 且已关闭 `__builtins__`、限制指标列白名单，但属性访问未收口。
> 一旦 `trading_rules` 允许 App 端用户编辑，需先换成 ast 白名单求值（该决策尚未确定）。

### 消息面（`news/`）

`stock_selector/news/` 是消息面两层解读的**实现层**（`fetch` 取 Mongo 新闻与自然日窗口、`analyze` 调 LLM、
`targets` 拆标的、`store` 双写 parquet+Mongo、`stats` 热度/趋势跨天聚合、`names` 代码→名称回填、
`normalize` secid 归一）；命令行壳是 `scripts/analyze_news_llm.py`，详细字段与用法见下文
"新闻解读脚本"一节。目前消息面与选股内核**尚未打通** —— `watch_target` 的量还没有作为因子进入
`SelectResult` 或规则表达式，属于 P2。

### 已知限制（截至 2026-09-24）

- **选股与回测尚未同源**：回测侧用 `trading_rules` 表达式驱动的规则策略，选股侧用 `BaseSelectStrategy`
  看最后一行，两套契约没有合并，因此"回测验证过的信号"和"今天选出来的票"还不能互相印证（P1 核心项）。
- 返回项只有 `code/name/signal/reason`，无评分/排名/基准交易日（`extra` 已预留但无策略写入）；
  `signal` 是策略英文名（如 `trend_rebound`）而非中文，后端 `select_results` 也只落这 4 列。
- 结果新鲜度受 parquet 更新进度限制，需自行保证 `build_kline_features.py` 已跑过；
  parquet 与 MongoDB 两侧会各自漂移，排查"信号不对"先看两边的最新日期。
- 全市场单次 163s（后端超时 600s），串行、未做进程池并行，也没有"策略声明所需列"的机制 ——
  用了指标列的策略必须手工把它们加进 `datasource.parquet.columns`，否则策略取不到该列，
  异常会被 `tolerate_errors` 降级成每票一条 warning、整体表现为 0 命中。
- 无 HTTP 回测入口（只能跑 CLI）、无数据刷新调度。
- `settings.yaml` 的 `server.port` 默认 8000，本机该端口被一个无关服务占用，实际部署请用
  `--port` 覆盖（如 18000/18002）并让后端 `SELECTION_PLUGIN_BASE_URL` 指向同一端口。

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

## session-context：跨会话上下文维护

开发过程中由 AI 维护两层知识，断线/重开对话后不丢上下文：

| 层 | 位置 | 内容 |
|---|---|---|
| 任务进度（临时） | `.opencode/session-context/active-<task>.md` | 当前任务目标、进度、下一步、阻塞 |
| 项目经验（永久） | `memory/`（`MEMORY.md` 索引 + `project/` 事实条目） | 架构决策、红线、代码库约定，跨任务可复用 |

### 手动命令

| 你说 | 行为 |
|---|---|
| `保存上下文` / `checkpoint` | 盘点后把当前任务进度写入 active 文件 |
| `恢复上下文` / `resume` | 盘点并列出所有活跃任务，继续指定任务 |
| `查看上下文` / `context` | 显示当前 active 文件 |
| `沉淀` / `sediment` | 任务结束仪式：可复用经验写入 `memory/`，删除该 active 文件 |

### 自动模式（无需手动触发）

- 完成任务步骤 / 做架构决策 / 学到代码库模式 / 用户纠正 → AI 立即写入 active 或 memory
- 检测到切换新任务 → 自动 mini-沉淀旧任务并开新 active 文件

> `active-*.md` 无文件 = 无活跃任务。新对话想自主续上次任务时，先让我读 `.opencode/session-context/` 和 `memory/MEMORY.md`。