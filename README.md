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
                 │  └─ 回测经 Celery(Redis db3) 投递给 worker 进程跑 backtest_main.py，逐日进度回写 backtest_jobs
                 ▼
展示层   uniapp/  Vue 移动端：只展示、不计算
```

四层职责严格单向：**脚本产数据 → 插件算信号 → 后端编排落库 → 前端展示**。
下面一节先讲选股插件（数据流、内核、策略、回测），其后是各采集脚本的用法。
本地多服务端口分配与联调清单见 `uniapp/README.md`；安卓 APK 的 HBuilderX 云打包步骤见 `uniapp/README.md`「安卓 App 出包」；常驻进程拓扑、部署步骤与访问方式见文末「后台部署与运维」。

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
├── backtest/        # 回测：engine 逐日撮合 / broker 资金与 T+1 / rules + rule_eval 规则 DSL / kernel_strategy 同源适配 / metrics / store
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

**同源**是硬约束，有两层含义：

- **数据同源**：选股、回测、诊断读同一批 parquet 文件、同一套列语义，
  `RuleEvaluator` 的指标白名单与 parquet 列一一对应。切换数据源不需要改任何策略代码，
  但会同时改变新鲜度与耗时。
- **判据同源**：策略的 as-of 判据 `signal_at(df)` 只有一份实现，实盘选股喂"整段历史"、
  回测喂"≤基准日的切片"，同一段代码跑两次 —— 所以"策略历史上会不会买"与"今天选出什么"可互相印证，
  不存在只在回测里成立的信号逻辑。详见下一节《选股与回测同源（as-of）》。

### 核心运行逻辑（内核 `run_select`）

1. `StrategyLoader.load_all()` 运行时扫描 `strategies/` 包，按 `obj().name` 建注册表
   —— 探测实例只用于取名，参数统一由 `create()` 注入。
2. `resolve_strategies()` 决定本次执行的策略：`None` 用 `settings.selector.default_strategies`；
   传了未注册的名字直接 `ValueError`（列出可用策略），避免静默空跑。
3. 数据源：`datasource=` 参数优先（测试与回测可注入），否则按 `datasource.type` 创建；
   只有自己创建的实例才在 `finally` 里 `close()`。创建后先调 `ds.prepare()`，把各策略
   `required_columns()/required_rows()` 的并集下发下去（**只放宽不收紧**），
   避免"剪列后策略取不到指标列"被降级成静默 0 命中。
4. **每股只取数一次**，内层遍历全部策略 —— IO 次数 = 股票数，与策略个数无关。
5. 结果归位：策略可留空 `code/name/signal/strategy/price`，内核用取数上下文回填
   （code ← 循环变量，name ← 股票列表或 `ds.name_of(code)`，signal/strategy ← 策略名，
   price ← 基准日收盘价），`asof` 统一取 `ds.max_date()`。
6. 任一结果带 `score` 时按分数降序重排并赋 `rank`；全无分数则保持产出顺序。
7. 单只股票取数或策略求值异常：`tolerate_errors: true` 记警告继续，`false` 立即抛出；
   股票池非空却 `loaded == 0` 视为数据源故障，抛 `RuntimeError`。

结束时输出 `选股完成: 策略=… 股票池=… 有效=… 命中=… 失败=… 基准日=…`，
基准日即 `ds.max_date()`，也就是这批信号对应的交易日。

`SelectResult` 字段（前 4 个是对后端/前端的稳定契约，后面为可选增强）：

| 字段 | 含义 |
|---|---|
| `code` / `name` | 股票代码、名称 |
| `signal` | 信号标识（当前等于策略名） |
| `reason` | 中文选股依据 |
| `extra` | 策略自定义扩展字典 |
| `strategy` | 产出该结果的策略名 |
| `asof` | 基准交易日（信号对应哪天，不是"运行时间"） |
| `price` | 基准日收盘价 |
| `score` | 信号强度 0~1，策略未给则为 `None` |
| `rank` | 名次，有评分时由内核按分数降序赋值 |

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
| `GET /api/strategy/detail` | 每个策略的 `{name, description, as_of, required_columns, required_rows, params}`，前端做中文标签用 |
| `POST /api/selector/run` | 入参 `{"strategies":[...]}`（缺省用配置默认），返回 `data:[{code,name,signal,reason,extra,strategy,asof,price,score,rank}]` |

后端 `stock_selector_api` 通过环境变量 `SELECTION_PLUGIN_BASE_URL` 指向本服务，配置项
`selection_plugin_mode: http|mock`。

### 调用链与接口契约

```
uniapp  ──/api/v1/selection/*──►  后端 stock_selector_api  ──HTTP──►  本插件(api.py) ──► run_select()
```

后端 `services/selection_service.py` 的消费方式决定了返回结构不能随意改：

- `GET /api/strategy/list` 的 `data` **必须是 `list[str]`**，后端直接 `list(...)` 当下拉数据源。
- `GET /api/strategy/detail` 由后端 `GET /api/v1/selection/strategy-detail` 透传（mock 模式返回内置
  假数据），前端用它把英文策略名映射成中文短标签，客户端不硬编码策略名。
- `POST /api/selector/run` 后端映射 `code/name/signal/reason` + P1 富化字段
  `strategy/asof/price/score/rank`（`signal` 缺省回落到 `action`），落库到 `select_results`，
  httpx 超时 600s。`rank` 是 SQL 保留字，**列名/API 字段名为 `rank_no`**，
  由 uniapp `logic/view.js` 归一回 `rank`。
- 后端 job 状态机 `queued/running/done/failed`，`_inflight` 单飞（同一时刻只有一个选股在跑）。

因此**扩充返回字段是一次三方联动**：插件 `SelectResult` → 后端 `select_results` 的幂等 `ALTER TABLE`
→ uniapp `logic/view.js` 的展示映射。改结构请三处同步，否则新字段会被后端丢弃、前端不可见。
后端落库是**写入白名单**（`SelectResultCreate` 未声明的字段静默丢弃），所以加字段必须同时改
`db/sql/selection.py`、`schemas/selection.py`、`selection_service._http_run` 三处。

本地起一套互不干扰的验证链路：插件 `python -m stock_selector.main --port 18003`；
后端 `SELECTION_PLUGIN_BASE_URL=http://127.0.0.1:18003 DATABASE_URL=... uvicorn main:app --port 18085`
（注意 `backend/main.py` 的 `__main__` 分支写死 8000，换端口必须走 uvicorn 命令行）；
前端 `VITE_PROXY_TARGET=http://127.0.0.1:18085 npx vite`（`vite.config.js` 的 `/api`、`/ws` 代理由
该变量派生，不设置时默认 `http://127.0.0.1:8010`，即本机已部署的 API；生产地址走 `uniapp/.env.production`，见 `uniapp/README.md`）。

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

策略只依赖一个抽象契约，框架不 import 任何具体策略（依赖倒置）。**首选实现 as-of 判据 `signal_at`**：

```python
# stock_selector/strategies/my_strategy.py
from ..core.base_strategy import BaseSelectStrategy
from ..models.result import SelectResult

class MyStrategy(BaseSelectStrategy):
    def required_rows(self) -> int:          # 判据需要多少回溯行（默认 120）
        return 40

    def signal_at(self, df) -> SelectResult | None:   # 只依据 df 判断"截至最后一行"是否出信号
        close = float(df["close"].iloc[-1])
        if not self.config.get("floor", 10) < close:
            return None
        return SelectResult(code="", name="", signal="my_strategy", score=0.8, reason="…")
```

约定：

- **放进目录即注册** —— `StrategyLoader` 运行时扫描 `strategies/`；两个判据都没实现的类
  会被跳过并告警，不会以"永远 0 命中"的形式混进策略列表。
- `signal_at(df)` 是**唯一 as-of 判据**：传入的 df 已按基准日截断，策略不感知日期、
  也不访问 df 之外的数据。`run(df)` 默认就是它的列表化，需要一次产出多条结果时才覆盖 `run`。
- `name` 默认由类名蛇形派生（`TrendRebound` → `trend_rebound`），是接口、参数、`signal` 三处共用的标识。
- 参数来自 `settings.yaml` 的 `strategy_params.<策略名>`，通过 `self.config` 读取，务必带默认值。
- `code/name/signal/strategy/price/asof/score/rank` 可由内核回填，策略通常只填 `signal/reason/score`。
- 当前两个内置策略：`trend_rebound`（MA20 斜率向上 + 回踩均线 ±5% 内 + 当日收阳企稳，
  score = 越贴近均线越高）、`box_oscillate`（前 30 日箱体〔不含当日〕，收盘有效突破上沿 +2%
  且前一日尚未破位，score = 突破充分度 + 放量加成）。

```bash
python -m pytest tests -q          # 内核/数据源/策略/特征/回测/消息面 192 项
```

### 选股与回测同源（as-of）

同一段 `signal_at` 代码有两个驱动器，这就是"策略在历史上会不会买"与"策略今天选出什么"能互相印证的原因：

| 驱动器 | 取数 | 调用 |
|---|---|---|
| 实盘选股 `run_select()` | 数据源给整段历史（最后一行 = 最新交易日） | `run(df)` → `signal_at(df)` |
| 同源回测 `KernelStrategy` | `ParquetProvider.window(code, T, rows)` 只给 ≤T 的切片 | `signal_at(切片)` |

`stock_selector/backtest/kernel_strategy.py` 把选股策略接到回测引擎上：

- **入场**与实盘逐字一致；基准日停牌（切片最后一行早于 T）不出信号，绝不用历史行冒充当日决策。
- 候选按 `score` 降序提交，引擎受持仓只数限制从头成交，因此排序就是选股优先级。
- **出场不在选股策略职责内**（策略只回答"此刻是否该买"），由适配器按
  `max_hold_days / stop_loss_pct / take_profit_pct` 三条机械规则决定；
  所以同源回测的收益数字依赖这组参数，不能只归因于策略本身。
- 引擎开跑时通过 `attach_broker` 钩子把账户交给策略，用于读取持仓成本与买入日期。
- 入场取数全部走策略自己的 `window()` 切片，因此 `KernelStrategy` 声明 `needs_market_prices = False`，
  引擎据此跳过"逐日读遍全部 parquet"的全市场报价快照（规则策略缺省仍需要，行为不变）。
  没有这一步时，500 只 × 半年区间的同源回测会退化成 5186 只 × 每日的掩码扫描，耗时从秒级涨到十几分钟。

```bash
# 用选股策略本身做回测（同一份判据），只取前 500 只股票，结果不落库
python scripts/backtest_main.py --strategy trend_rebound --universe 500 \
    --start 2026-03-02 --end 2026-09-24 \
    --max-hold-days 10 --stop-loss-pct 0.05 --take-profit-pct 0.15 --no-save
```

> 缺省（不带 `--strategy`）仍是 `trading_rules` 表达式驱动的规则策略，两条路线共用同一个撮合引擎。

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

两种策略来源共用同一个撮合引擎：

```bash
# A. 规则驱动（缺省）：买卖条件读 MongoDB trading_rules
python scripts/backtest_main.py --start 2025-09-01 --end 2026-09-01 \
    --capital 100000 --max-positions 5 --position-cap-pct 0.2

# B. 选股插件驱动（同源，见上一节）
python scripts/backtest_main.py --strategy trend_rebound --universe 500 \
    --start 2026-03-02 --end 2026-09-24 --no-save
```

规则模式的 `buy`/`sell`/`add` 是布尔表达式（如 `price <= ma10 * 0.8 and adx <= 25 * 1.15`），
由 `RuleEvaluator` 在指标列白名单内求值；结果写入 `backtest_results` / `backtest_trades` /
`backtest_equity`，`--no-save` 可只跑不落库。`--start/--end` 真正约束撮合区间（此前被忽略，
回测实际跑满了全部历史），策略仍然能看到区间之前的数据用于指标预热。

引擎逻辑（`backtest/engine.py`）—— 逐日推进，每一天严格按此顺序：

1. `broker.settle()`：T+1 资金解冻（昨日卖款今日才并入可用 `cash`，当日卖出款进 `settling` 不可即用）。
2. `evaluate_holdings()` 拿持仓迁移指令 → **先卖** → **再加仓**（`add` 不占持仓只数配额）。
3. 仓位数未满 `max_positions` 时才 `propose_buys()` 买新标的，受 `position_cap_pct`
   （单票成本 / 当前总资产）与 `target_amount`（每笔固定买入金额）约束。买入前会给出
   全市场报价快照，但策略可靠 `needs_market_prices = False` 声明不消费它来省掉这次全量读盘。
4. 按 `cash + settling + 持仓市值` 记一个权益点。

账户规则（`backtest/broker.py`）：以**当日收盘价**成交、按 100 股整手取整、佣金
`commission_rate` 且有 `commission_min` 下限、卖出另计 `stamp_tax_rate` 印花税、已实现盈亏按持仓均价成本含费计算；
资金不足 / 不足一手 / 超单票上限 / 超持仓只数都抛 `ValueError`，由引擎捕获后跳过该笔（不影响其余撮合）。
停牌（当日 parquet 无该行）不撮合，估值回退到最近一次已知收盘价，避免净值凭空跳变。
`Metrics` 产出 `total_return / annual_return / max_drawdown / win_rate / sharpe / profit_factor / trade_count`
（按 245 交易日年化），`BacktestStore` 落 MongoDB。

策略与引擎之间靠 `_StrategyProxy` 适配：引擎只认 `propose_buys(date, cash, prices)` 和
`evaluate_holdings(date, holdings, prices)` 两个可选回调，没有的方法视为返回空列表。
因此规则策略（`RulesStrategy`）与选股插件策略（`KernelStrategy`，入场即 `signal_at`）
都能挂到同一个引擎上，撮合、资金、指标、落库完全共用。

> ⚠️ `RuleEvaluator` 用 `eval()` 且已关闭 `__builtins__`、限制指标列白名单，但属性访问未收口。
> 一旦 `trading_rules` 允许 App 端用户编辑，需先换成 ast 白名单求值（该决策尚未确定）。

后台管理还有个「回测」页：参数白名单校验后投递 Celery 任务，由 worker 跑上面这套内核，
逐日进度回显到页面，后端只投递与查状态、零计算。详见下文「后台部署与运维」一节。

### 消息面（`news/`）

`stock_selector/news/` 是消息面两层解读的**实现层**（`fetch` 取 Mongo 新闻与自然日窗口、`analyze` 调 LLM、
`targets` 拆标的、`store` 双写 parquet+Mongo、`stats` 热度/趋势跨天聚合、`names` 代码→名称回填、
`normalize` secid 归一）；命令行壳是 `scripts/analyze_news_llm.py`，详细字段与用法见下文
"新闻解读脚本"一节。目前消息面与选股内核**尚未打通** —— `watch_target` 的量还没有作为因子进入
`SelectResult` 或规则表达式，属于 P2。

### 实测记录（2026-09-24，本机全市场）

- 数据：`build_kline_features.py` 补齐后 parquet 最新交易日 **2026-09-24**（5186 只 × 36 列）。
- 选股：`python -m stock_selector.main`（默认两策略，进程内直接调 `run_select()`）耗时 **128.4s**，
  命中 **567** 条（`box_oscillate` 67 / `trend_rebound` 500），`asof` 全部为 `2026-09-24`，
  `rank` 连续且按 `score` 降序。
- **同源互证（关键）**：把同一个 `trend_rebound` 判据放到历史区间逐日求值
  （`ParquetProvider.window` 切片，前 300 只 × 144 个交易日），得到 **4344** 个信号、
  日均 30.2 个 ⇒ 命中率 **10.1%**；而实盘 2026-09-24 全市场命中率是 500/5186 = **9.6%**。
  两条独立路径的密度吻合，说明"回测里的策略"和"今天选出来的票"确实是同一份判据。
- 同源回测（`--strategy trend_rebound --universe 500 --start 2026-03-02 --end 2026-09-24`，
  出场 `max_hold_days=10 / stop 5% / take 15%`）：

  | 参数 | 平仓笔数 | 总收益 | 胜率 | 盈亏比 | 最大回撤 |
  |---|---|---|---|---|---|
  | capital 100k, target 15k（上限不粘连） | 96 | **-21.6%** | 36.5% | 0.58 | — |
  | capital 100k, target 20k（正好贴单票上限） | 59 | -26.2% | 32.2% | 0.42 | -26.8% |
  | capital 200k, target 20k（上限宽松，仅半仓运作） | 100 | -16.1% | 34.0% | 0.59 | -16.7% |

  两个结论：**①** 该判据配这组机械出场在本区间是亏的，P1 交付的是"可验证"，不是"能赚钱"；
  **②** `target_amount` 贴着 `capital × position_cap_pct` 时，净值一回撤后续买入就会被单票上限
  静默拒掉（96 笔 → 59 笔，且更差），CLI 现在会在开跑前打这条警告。回测数字对参数极敏感，
  换参数就是另一个结论，不能把上面任一串数字当成对策略的评价。
- 回归四套全绿：根目录 `tests/` **194 项**（含 21 项同源回测测试，其中"未来行情不能改变历史
  as-of 判据"是防泄漏的关键断言）、后端 **162**、诊断插件 **24**、uniapp **39**。
  插件接口新增字段是**向后兼容**的（在 `extra` 之后追加带默认值的字段），因此后端/前端测试不受影响。
- 全链路实测（本机，插件 18003 → 后端 18085 → App 5174，`trend_rebound` 单策略）：一次选股落库
  **500 条**，`rank_no` 按 `score` 降序连续、`asof` 全部为数据最新交易日，App 选股页与历史回看页
  截图核对通过（名次/中文策略标签/基准价/信号强度/基准交易日均已上屏）。

### 前端字段贯通（P1-7）

| 层 | 位置 | 作用 |
|---|---|---|
| 插件 | `SelectResult`（`strategy/asof/price/score/rank`） | 唯一产出方，所有算术在此完成 |
| 后端建表 | `db/sql/selection.py` + `db/__init__.py::_ensure_select_result_columns` | 新列 + 幂等 `ALTER TABLE`，老库直接升级 |
| 后端写入 | `schemas/selection.py::SelectResultCreate` | **写入白名单**，未声明字段静默丢弃 |
| 后端转发 | `services/selection_service.py::_http_run` | 插件 `rank` → 列 `rank_no` |
| 后端接口 | `GET /api/v1/selection/strategy-detail` | 透传插件策略元信息给客户端 |
| 客户端 | `logic/view.js::toSelectionResultRow/toStrategyOption/fmtTime` | 纯映射，不做计算；`rank_no`→`rank`，UTC→本地分钟 |
| 页面 | `pages/selection/index.vue`、`history.vue` | 名次徽标、中文标签、基准价/强度/基准交易日 |

两个刻意为之的细节：`score` 用三位小数展示（相邻名次常只差 0.001，两位会全显示成 1.00 而看不出排序）；
`created_at` 是 naive UTC，展示前转本地并压到分钟，否则比本机时间早 8 小时。

### 已知限制（截至 2026-09-24）

已解决（P1）：判据同源、`score/rank/asof/price/strategy` 回填、策略声明所需列与行数
（`required_columns/required_rows` + `prepare()`）、回测 `--start/--end` 被忽略的缺陷、
**新字段贯通到后端落库与 App 上屏**（P1-7）。仍存在的：

- **评分饱和**：`box_oscillate` 的 `score` 在强势突破票上普遍顶到 1.0，跨策略排序退化成粗排序
  （同分项只按代码序），展示名次不等于强度名次。要让排名有意义需要连续化打分（分位/幅度裁剪）。
- **新鲜度靠手工跑 `build_kline_features.py` 保证**：结果所有 `asof` 都来自数据源最新交易日，
  parquet 与 MongoDB 两侧会各自漂移，排查"信号不对"先看两边的最新日期。
- 全市场单次约 130–165s（后端超时 600s），串行、未做进程池并行。
- **回测撮合仍是"当日收盘价成交"**：as-of 判据用的正是这根收盘 bar，等于假设你能以算出信号
  那个价格成交，实盘只能在次日开盘后成交，因此所有收益数字都带一层乐观偏差。要贴近实盘
  需要改成 T+1 开盘撮合（尚未做）。
- `--universe N` 取的是**代码升序前 N 只**（`000001` 起），不是随机样本，跨策略/跨市场结论会偏；
  样本外推要么传显式代码清单，要么补分层抽样。
- `metrics.trade_count` 是**平仓笔数**（`closed_pnls` 长度），不是委托笔数；对照 `backtest_trades`
  里的 buy/sell 行数看才完整。
- 无 HTTP 回测入口（只能跑 CLI）、无数据刷新调度；消息面因子尚未进入选股/回测（P2）。
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

## 后台部署与运维（supervisor + nginx）

整套后台由 supervisor 常驻五个进程，对外经 nginx 的 8080 端口反代（nginx 容器复用 `lex_math_new` 项目的 `lexmath-nginx`）。所有配置的源副本都在 `deploy/`，改完再拷去生效位置。

| supervisor program | 端口 | 职责 | 配置副本 |
|---|---|---|---|
| `selectstock-api` | 8010 | FastAPI 后台 + 无构建前端（静态页同进程托管） | `deploy/selectstock-api.conf` |
| `selectstock-celery-worker` | — | 真正跑回测内核的进程（Redis db3 队列，concurrency 1） | `deploy/selectstock-celery-worker.conf` |
| `selectstock-plugin-selector` | 8002 | 选股内核的 HTTP 适配层，后端薄代理 | `deploy/selectstock-plugin-selector.conf` |
| `selectstock-plugin-portfolio` | 8001 | 持仓诊断插件（判定与收益公式都在这里） | `deploy/selectstock-plugin-portfolio.conf` |
| `selectstock-scheduler` | — | 采集调度：APScheduler 跑 `config/inputs.conf` 的 `script://` 任务 | `deploy/selectstock-scheduler.conf` |

**端口为什么不是 8000**：文档约定后端占 8000，但本机 8000 已被 `/data/work/lex_math_new/backend` 占用，所以 API 改用 **8010**，对外统一走 nginx **8080**。换端口只需同时改 `deploy/selectstock-api.conf` 的 `--port` 与 `deploy/nginx/selectstock.conf` 的 upstream。

### 前置条件

- Redis 在 `127.0.0.1:6379`，回测任务用 **db 3**（与旧 `stock_select` 项目分库隔离），broker 与 result 同库
- MongoDB（`lexmath-mongo` 容器）可达：内核从 `stock_kline` 读行情，结果写回 `eastmoney_news`
- Python 依赖：API 侧 `celery / redis / psutil` 见 `stock_selector_api/pyproject.toml`；worker 还要仓库根 `requirements.txt` 里的内核依赖（pandas / pyarrow / pymongo 等）

### 部署步骤

```bash
cd /data/work/SelectStock

# 1) 依赖（已装可跳过）
python3 -m pip install -e ./stock_selector_api          # API 侧：celery/redis/psutil
python3 -m pip install -r requirements.txt              # worker 侧：回测与选股内核依赖

# 2) 密钥：JWT_SECRET 放在不入库的 .env（缺省值会让任何人伪造令牌）
python3 -c "import secrets,pathlib; p=pathlib.Path('stock_selector_api/.env'); \
p.write_text('JWT_SECRET='+secrets.token_urlsafe(48)+chr(10)) if not p.exists() else print('已存在，跳过')"

# 3) supervisor 五个常驻进程
sudo cp deploy/selectstock-*.conf /etc/supervisor/conf.d/
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status                               # 全部 RUNNING 才算起来

# 4) nginx 反代（复用 lexmath-nginx 容器；compose 里需保留 "8080:8080" 端口映射）
cp deploy/nginx/selectstock.conf /data/work/lex_math_new/deploy/nginx/conf.d/selectstock.conf
docker exec lexmath-nginx nginx -t && docker exec lexmath-nginx nginx -s reload
```

supervisord 本身由发行版自带的 `supervisor.service` 托管并已 `enabled`，重启后五个进程自动拉起。`deploy/systemd/supervisor.service` 只是无系统包时的参考副本，不需要安装。

### 使用方法

- 访问 **http://&lt;局域网IP&gt;:8080/**（备用直连 `:8010`，绕过 nginx）。默认账号 `admin / admin123`，登录后先改密码（要求 8 位以上、含大小写字母与数字）
- **端口链路**：浏览器/手机只认 **8080**（`lexmath-nginx` 容器内 `listen 8080`，compose 映射 `8080:8080`，`/` 与 `/ws/` 都转发到 upstream `host.docker.internal:8010`，即宿主机上的 `selectstock-api`）。**8010 是后端内部端口**，平时不用直连；容器里必须写 `host.docker.internal` 而不是 `127.0.0.1`。uni-app App 端用的也是 8080 入口（`VITE_API_BASE=http://<局域网IP>:8080`，见 `uniapp/README.md`「安卓 App 出包」）
- **回测页**：「发起回测」→ 填区间/资金/仓位/策略来源 → 列表每 4 秒轮询进度（`逐日撮合 i/N`）→ 「查看」看指标卡、净值曲线与最近 30 笔成交。全量明细在 Mongo `eastmoney_news.backtest_results / backtest_trades / backtest_equity`，作业表 `backtest_jobs` 只留精简摘要
- **系统监控页**：CPU / 内存 / Swap / 各挂载分区使用率 / 后端进程 RSS，5 秒自动刷新，用来判断回测 worker 有没有把机器吃满
- **权限**：`backtest:list` 看历史、`backtest:run` 发起回测、`sys:monitor:list` 看系统监控。admin 三样全有，readonly 只有 `backtest:list`
- 8001 / 8002 两个插件只给后端内部调用，不要从浏览器访问，也不要对外网开放

### 故障排查

| 症状 | 查哪里 |
|---|---|
| 发起回测一直 `queued`、进度 0% | `sudo supervisorctl status selectstock-celery-worker`；页面顶部 worker chip 显示"不在线"就是没人消费，再确认 Redis 6379 与 db 3 |
| 回测 `failed` | 作业「说明」列有错误摘要；`tail -50 logs/celery-worker.err.log` 看内核栈。`queued/running` 超 3 小时无状态会被自动判 failed，避免把用户的单飞位占死 |
| 页面 502 / 打不开 | `tail -50 logs/api.err.log`，确认 8010 在听；再 `docker exec lexmath-nginx nginx -T \| grep 8080` 看反代 |
| 改了 conf 不生效 | supervisor 要 `sudo supervisorctl reread && sudo supervisorctl update`；nginx 要 `nginx -t` 后 `nginx -s reload` |
| 选股 / 持仓诊断报插件不可用 | 8001、8002 两个 program 是否 RUNNING |
| 定时采集没跑 | `logs/scheduler.out.log`；`systems/conf.py` 对不存在的配置文件静默返回空 dict，先确认 `config/inputs.conf` 路径真的存在 |

日志统一在 `logs/`：`api` / `celery-worker` / `plugin-selector` / `plugin-portfolio` / `scheduler` 各自的 `.out.log` 与 `.err.log`。

> 注意有**两套互不重叠**的调度器：仓库根 `scheduler/scheduler.py` 跑 `config/inputs.conf` 的 `script://`（采集脚本）；后端进程内的 APScheduler 跑 `stock_selector_api/backend/conf/inputs.conf` 的 `task://`（设备巡检 5 分钟、17:30 选股、18:00 持仓诊断）。所以 API 进程要保持 `SCHEDULE_ENABLED=true`，同时**不要加 `--workers`**，否则定时任务会重复执行、SQLite 还会写锁竞争。

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