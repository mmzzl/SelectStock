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
├── quote/           # 实时行情解析：tencent 快照（涨跌停价/换手/量比/五档，交易所口径）
├── realtime/        # 盘中上行：session 交易时段 / bus redis 键契约 / poller 快照轮询成 tick 流
├── risk/            # 风控层：snapshot 行情口径 / context 单票上下文 / sizing 仓位与滑点 / rules 五条规则 / engine 短路编排
├── intraday/        # 盘中计算：bars 分钟条 / indicators talib 分钟指标 / signals 三类信号 / engine 信号→风控→决策 / store 落盘 / events 基本面事件
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
   │  scripts/build_kline_features.py     # talib 指标 + pct/lu/vol_ratio/pos60 + 形态/量能 46 列
   ▼
data/stock_kline/{code}.parquet           # 6631 只(5186 股+1445 基金) × 82 列 指标宽表（唯一共享数据面）
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
6. **信号先过风控，才成为决策**（`risk.enabled: true` 时）：命中的票逐只交给
   `risk.RiskEngine`，被拦下的整只剔除、不进结果；放行的把结论（`allow`/`adjust`）、建议下单
   股数、预估成交价与滑点写进 `extra` 和 `decision` 字段。细则见下一节《风控层》。
7. 任一结果带 `score` 时按分数降序重排并赋 `rank`；全无分数则保持产出顺序。
8. 单只股票取数或策略求值异常：`tolerate_errors: true` 记警告继续，`false` 立即抛出；
   股票池非空却 `loaded == 0` 视为数据源故障，抛 `RuntimeError`。

结束时输出 `选股完成: 策略=… 股票池=… 有效=… 命中=… 风控拦截=… 失败=… 基准日=…`，
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
| `decision` | 风控后的最终决策：`allow`（原样放行）/ `adjust`（放行但改了下单量）；被拦的信号不会出现在结果里，风控关闭时是空串 |

### 风控层（`stock_selector/risk/`）

**策略只产生信号，信号不等于要下单**——决策必须独立于策略。这一层回答的是「这个信号能不能吃、
吃多少、按什么价吃」，五条规则按固定顺序执行、命中即短路：

| 顺序 | 规则 | 拦掉什么 | 为什么排在这里 |
|---|---|---|---|
| 1 | `tradability` 涨跌停可成交性 | 涨停买不进、跌停卖不出 | 标的根本吃不到，后面的计算都是白费 |
| 2 | `liquidity` 流动性下限 | 近 20 日日均成交额 < `min_avg_amount` 的小盘票 | 盘口薄，信号再好看也滑没了 |
| 3 | `position` 单票仓位上限 | 超过 `总资金 × max_position_pct` 的部分（改成 `adjust`，不是剔除） | 先把下单量定下来，滑点才有单额可估 |
| 4 | `slippage` 滑点预估 | 半价差 + 冲击成本 + 信号价漂移超阈值，或单额占日成交额超 `max_participation` | 免费快照有延迟，看到的价格不一定成交得到 |
| 5 | `model` 买入风险分 | 预测 10 日超额低于剔除阈值的票（研究依据见《涨停与风险分》） | 要跑 700 根日线的特征，最贵，放最后 |

模块分工：`snapshot` 统一行情口径，`context` 把一只票的日线+快照收敛成一个上下文，
`sizing` 只算数（整手、上限、半价差、√冲击成本、漂移），`rules` 只判定，`engine` 只编排。
单位口径全层一致：金额=元、成交量=手、市值=亿元（读取时即换算）、滑点=万分比。

```python
from stock_selector.risk import RiskEngine, build

engine = RiskEngine.from_settings(config)      # 读 settings.yaml 的 risk: 子树
engine.preflight()                             # 开了 ML 腿但模型不在 → 立刻报错，不静默放行
decision = engine.review(build("000532", "buy", day="2026-10-09", kline=df))
decision.verdict, decision.shares, decision.est_fill_price      # allow | adjust
decision.reason                                # 中文理由，告警链路要原样说给用户
```

**行情口径为什么不能只读日线**：本地日线只有涨停标记 `lu`、**没有跌停列**，成交额自
2026-05-21 起断供（全是 0，流动性退到 `volume × close × 100` 估算），涨跌停阈值还得分
主板/创业/科创/北交所/ST/基金。所以涨跌停价、换手率、精确成交额取
`data/market_snapshot/{日}.parquet` —— 交易所下发的原值，由 `scripts/fetch_market_snapshot.py`
每个交易日 15:30 落盘（整轮 12 秒；命中率不足 60% 直接拒绝写出残缺文件）。缺文件时的降级
顺序写死在 `risk/snapshot.py`，规则层不再各判各的：①当日落盘快照 → ②实时快照（腾讯，只给
当天；回测/盘后用 `allow_live=False` 关掉）→ ③都没有则字段为 `None`，规则**跳过判定**而不是
猜一个价。盘后内核按每只票自己的基准日取数，所以历史日期的信号不会混进今天的行情。

本系统只做建议，不接券商下单：风控的输出是「建议股数 + 预估成交价 + 中文理由」，落库列
`risk_verdict/risk_reason/risk_shares/risk_amount/risk_fill_price/risk_slippage_bp` 由后端
薄代理原样搬运，App 只展示（架构红线 4：算术不下沉到界面）。被拦下的票不进结果列表，所以 App
上每一只都是"这单真能成交"的口径；内核按规则名汇总拦截数（`风控拦截明细: liquidity=1 model=5`），
复盘时能看清是谁拦的。

### 盘中实时链路（`stock_selector/realtime/`）

需求文档里的「行情服务 → 消息队列 → 策略引擎」在本机落地为
「腾讯快照轮询 → redis stream → 盘中引擎」：没有正式行情授权，免费快照是可用口径里最快的一种
（3 只一拍 0.07 秒，全市场一拍约 11 秒）。**换真实 Level-1 源只替换 `quote/tencent.py` 的
`fetch`**，redis 键约定与下游引擎一行都不用改。

redis 用本机已有的 7.2.5（免密，**db 5**，与 celery 的 db 3、爬虫的 db 0 隔开）：

| 键 | 类型 | 内容 |
|---|---|---|
| `rt:quote` | Hash(code → 快照 JSON) | 最新行情，策略引擎随手查当前价，不必等下一拍；TTL 4 小时 |
| `rt:ticks` | Stream | **只有变化才发**的 tick 流，`MAXLEN ~` 限长（默认 2 万条） |
| `rt:decisions` | Stream | 过完风控的最终决策（放行与被拦的都进），告警进程消费；限长 5000 |
| `rt:heartbeat` | String(JSON) | 轮询服务上一轮统计（轮数/命中/耗时/状态） |
| `rt:engine-heartbeat` | String(JSON) | 策略引擎上一轮统计（读到/回放/评估/决策/拦截/耗时） |
| `rt:metric:{code}` | Hash | 盘中实时指标，由策略引擎写、告警与前端读 |

```bash
python scripts/quote_service.py --code 600000 000001        # 调试自选
python scripts/quote_service.py --watchlist data/watchlist.txt --interval 5
python scripts/quote_service.py --all --interval 15         # 全市场（限流风险自担）
python scripts/quote_service.py --status                    # 只看链路心跳
```

轮询的一拍只有三种结果，都写进心跳，不静默：`ok`（发了 N 条变化）、`market_closed`
（快照时间戳不是今天 → 休市/未开盘，**一条都不发**：把上个交易日的价当实时价推给策略比不发
更危险）、`fetch_failed`（整轮为空 → 数据源故障，指数退避到 60 秒上限，恢复即回到正常节奏）。
交易时段判断在 `realtime/session.py`（09:25–11:30、13:00–15:05，周末直接睡），节假日不硬编码
日历表——靠交易所自己在快照里打的更新时间戳判定，比会过期的调休表稳。

### 盘中策略引擎与告警（`stock_selector/intraday/`）

链路的后半段，两个各自独立的常驻进程：

```
rt:ticks ─► intraday_engine：累计量还原分钟条 ─► talib 分钟指标 ─► 信号 ─► risk.RiskEngine
                                                                        │
                              rt:metric:{code} ◄──（指标）──────────────┤
                              data/intraday_decisions/{date}.parquet ◄──┤─► rt:decisions ─► alert_pusher ─► 钉钉/企业微信
```

**风控只有一套**：盘中信号和盘后选股交给同一个 `RiskEngine`、同样五条规则，所以「盘中说可以买」
与「选股建议买」不会是两套数。被拦下的信号照样落盘、照样进 `rt:decisions`（`verdict=block` +
拦它的规则名 + 中文理由），只是不推消息；告警摘要单列一段「风控已拦截（不给下单建议，原因如下）」
并写清风控的理由——用户要能区分「没有机会」和「有机会但下不了单」，这两种情况的后续动作完全不同。

三类监控的 22 种 `kind`（阈值全在 `settings.yaml` 的 `intraday.signals`，`--kinds` 可只开子集）：

| 类别 | 信号种类 | 判据来源 |
|---|---|---|
| 技术面 | 均线金叉/死叉、MACD 金叉/死叉、RSI 超卖回升/超买、布林上下轨突破、放量拉升、放量大跌、量比放大、换手过高、上穿 20 日压力位、跌破 20 日支撑位 | 分钟条 talib 指标（参数与日线宽表同一套：SMA 5/10/20/60、MACD 12/26/9、RSI 14、BBANDS 20/2/2）+ 形态列 `res20`/`sup20` |
| 盘口 | 打单流入/流出、五档托单/压单 | 外盘−内盘的**相邻拍增量** × 现价（滚动 `flow_window` 分钟）；Σ买档量 / Σ卖档量 |
| 基本面 | 利多/利空事件 | mongo `eastmoney_news.watch_target` 当日多空目标（十分钟刷一次） |
| 成交性 | 触及涨停/跌停 | 快照里交易所给的涨跌停价，贴板标记由 `risk/snapshot.normalize` 统一算 |

两条口径值得单说：分钟指标只在新的分钟条**收口**时重算（每票每分钟一次），交叉类一律在收口条上
判——未收口那根的量还会继续涨，用它判「放量」就是同一分钟报三次、每次倍数都不一样；价格类信号
（量比/换手/触板/事件）直接读 `rt:quote` 的最新快照，不跑 talib。

全市场 6600 只的盘中计算很容易变成自我 DoS，预算全部写在 `intraday:` 里：`max_eval` 限制一轮跑
talib 的只数，`once_per_day` 让同票同信号**当天只报一次**（买入和卖出同一个口径；去重表写在
redis 的 `rt:emitted:{日期}`，所以引擎重启不会把上午报过的信号再报一遍，跨天自动放开），
`cooldown` 是它的退路——关掉日级去重、或去重表写不进 redis 时按这个秒数抑制，`replay_horizon`
让启动时回放的旧 tick **只补分钟条、不产信号**（那些价格是几分钟前的，此刻报出来只会诱导一个
下不到的单）。日线宽表（流动性/冲击/风险分/压力位都要它）只在真有信号要过风控时才读，按
`kline_cache` 只数 LRU 缓存。

```bash
python scripts/quote_service.py --all --interval 15              # 1) 行情上行
python scripts/intraday_engine.py --all --max-eval 300 --mongo   # 2) 指标 → 信号 → 风控 → 决策
python scripts/intraday_engine.py --status                       #    看引擎心跳
export ALERT_CHANNEL=dingtalk ALERT_WEBHOOK_URL='…' ALERT_WEBHOOK_SECRET='…'
python scripts/alert_pusher.py --dry-run                         # 3) 先看消息，确认无误再常驻
```

上面三个脚本已由 supervisor 托管常驻（`selectstock-quote-service` / `-intraday-engine` /
`-alert-pusher`，配置在 `deploy/`），手工命令用于调试与冒烟；告警那份因为仓库里不带密钥，
默认 `autostart=false`，填好 webhook 才需要 `supervisorctl start`。

告警进程**只排版不算术**：建议股数、预估成交价、滑点全部来自引擎与风控，它做单位换算和攒批
（默认 30 秒合成一条 markdown，避免刷屏、也避开机器人 20 条/分钟限流），一批超过 40 条
（`--max-lines`）时每段截断并指回当日决策文件——机器人对消息长度有上限，超长消息是整条拒收，
比少列几条更糟。发送失败重试两次就放弃（决策已落盘，消息可丢）。启动时游标放在**流尾**，重启不会
把上一交易日的存量当新信号重推（要重发用 `--replay`）。密钥只从环境变量读，日志里只打主机名——
`access_token` 与 `sign` 都在 URL query 里，打出来等于泄露机器人。需求文档的「调用券商 API 下单」
按约定**不做**：本系统只推告警。

复盘落盘用 parquet（一天一个文件，同 `(code, kind, minute)` 后写覆盖先写，重跑当天不产生重复行）
+ 可选 mongo，而不是需求文档里的 TDengine：本机内存已经紧张，而复盘的读法是「按天全量扫 + 按代码
过滤」，parquet 分区文件正好对上，和 `data/stock_kline`、`data/market_snapshot` 是同一套习惯。
指标列一律带中文理由与 `source`（`live` = 完整快照，`tick` = 只有 tick 的降级快照，此时滑点走
保守下限而不是假装看得见盘口）。

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

| type | 来源 | 适用 | 全市场实测(6631 个文件) |
|---|---|---|---|
| `parquet`（默认） | `data/stock_kline/{code}.parquet` | 常规选股；与回测/诊断同一数据面，含 30+ talib 指标与 46 个形态/量能列 | 6 列+120 行 **163s**；全 82 列会更慢，用 `columns` 剪列或让策略声明 `required_columns()` |
| `mongo` | MongoDB `stock_kline`（`frequency:9, adjust:qfq`） | 行情要求最新、或只查少量代码 | ~0.5s/只 ≈ 40 分钟，不适合全市场 |
| `ak` | akshare 东财接口 | 需外网可达（本机实测已不可达） | 不可用 |
| `mock` | 内置模拟数据 | 演示/测试 | 秒级 |

指标宽表由采集脚本生成，选股前需保证数据已更新：

```bash
python scripts/build_kline_features.py            # 增量更新（默认）
python scripts/build_kline_features.py --full     # 全量重建（股票 + 场内基金）
python scripts/build_kline_features.py --recompute  # 不读 Mongo，就地给已有 parquet 补新列
python scripts/build_kline_features.py --no-etf   # 增量但只处理股票，跳过场内基金
python scripts/build_kline_features.py --code 600519   # 只重建指定一只（ETF 代码同样可用）
python scripts/build_kline_features.py --limit 20      # 只处理前 20 只（联调用；0=不限）
```

清单除了 `bk_stocks` 的股票，还会从 `stock_kline` 里捞出有日线的**场内基金**（前缀
15/16/18/50/51/52/53/55/56/58，含 ETF/LOF/REITs，实测 1445 只），名字问腾讯行情。基金没有
「板块涨停」概念（跨境 ETF 甚至不限幅），所以它们的 `lu` 恒为 `False`，不会造出假信号。
上游 `scripts/kline_spider.py` 的 `in_universe()` 同步放开了基金号段（`sh 5x`、`sz 15/16/18`），
否则 Mongo 里的基金日线会停在爬取的最后一天。
`parquet` 数据源的股票池就是目录下的文件名，因此这些基金**会一并进入选股与回测范围**；不要就用
`--no-etf` 并清掉 `data/stock_kline/` 里的基金文件。

#### 形态与量能列（`stock_selector/features/patterns.py`）

K线形态只用「当根及之前」的日线描述，46 列全部由 `add_pattern_columns()` 向量产出，和 talib 指标
写进同一批 parquet，因此规则表达式（`trading_rules`）可以直接写 `hammer_low and vol_expanding`、
`close < neckline_sup`，不必在策略里重算。

| 组 | 列 |
|---|---|
| 量能语境 | `vol_ma20` `vol_trend`(ma5/ma20) `vol_z` `vol_expanding` `vol_contracting` `vol_ground` `vol_blast` |
| 单根解剖 | `body_ratio` `upper_ratio` `lower_ratio` `gap_pct` |
| 单根形态（按位置分档） | `hammer_low` `hanging_man_high` `shooting_star_high` `doji_low` `doji_high` `doji_high_blast` |
| 2~3 根组合 | `bullish_engulf` `bearish_engulf` `morning_star` `evening_star` `piercing_line` `dark_cloud` `harami` `three_soldiers` `three_crows` |
| 中长期形态（只在突破当根置位） | `w_bottom_break` `m_top_break` `hs_bottom_break` `hs_top_break` `v_reversal` `arc_bottom` `box_break_up/dn` `tri_break_up/dn` `flag_up_break/flag_dn_break` |
| 关键价位与买点参考 | `neckline_sup` `neckline_res` `box_upper` `box_lower` `sup20` `res20` `pullback_stabilize` `vol_diverge` |

四条实现约束，改这个模块前先读：

- **dtype 只能是 float64 或 bool**：`RuleEvaluator` 会对每个值做 `float()`，字符串列会让规则直接炸。
- **as-of 不前看未来**：形态确认天然滞后（枢轴要等右侧 `PIVOT_SPAN=5` 根、颈线要等突破），
  滞后一律以「晚几根才置位」的形式出现，绝不把未来写进过去。用例把历史截断重算，
  要求逐列等于全量结果的前缀（`tests/test_features_patterns.py::test_pattern_columns_are_asof_safe`）。
- **高低位靠 `pos60` 区分**：同一个长下影在低位是 `hammer_low`（止跌）、在高位是
  `hanging_man_high`（见顶），列名后缀 `_low/_high` 就是这个意思，所以形态列必须在
  `add_base_columns()` 之后算。
- **触发率是量级校验，不是精度承诺**：400 只 / 96 万根实测，单根形态 1%~5%、组合 0.2%~2.6%、
  突破类 0.02%~1.3%、`vol_expanding` 16%。异常地高（如圆弧底曾到 10%）说明判据退化成了
  「持续状态」，要收敛成事件；异常地零（如旗形曾恒 `False`）说明判据互斥。形态可骗线、
  量可对倒，这些列只是概率参考，仍需叠均线与大盘环境。

成本：形态列让每只多算 ~0.05s，全市场增量构建约 **+320s**；停牌/退市的老文件不会被增量路径碰到，
新增列后跑一次 `--recompute` 就地补齐（约 9 分钟，不读 Mongo）。

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
risk:                      # 信号→决策的独立闸门，见《风控层》
  enabled: true
  capital: 200000          # 账户总资金（元）：单票上限与滑点都按它折算
  max_position_pct: 0.2
  min_avg_amount: 30000000 # 近 20 日日均成交额下限（元）
  max_slippage_bp: 100     # 预估滑点上限（万分比）
  use_limit: true          # 涨跌停可成交性
  ml: {enabled: true, model_dir: "data/models"}   # 买入风险分腿
intraday:                # 盘中计算引擎，见《盘中策略引擎与告警》
  interval: 5            # 读 rt:ticks 的间隔（秒）
  max_eval: 300          # 一轮最多几只跑 talib（全市场盘中的自我限流阀）
  once_per_day: true     # 同票同信号当天只报一次（买入/卖出同口径），跨天自动放开
  cooldown: 300          # 重复抑制窗口（秒）：once_per_day 关掉或去重表写不进时的退路
  replay_horizon: 60     # 旧过这个秒数的 tick 只补分钟条、不产信号
  flow_window: 5         # 打单流入的滚动窗口（分钟）
  signals:               # 信号阈值：换手/量比/放量倍数/突破确认幅度…全部外置
    min_bars: 60         # 分钟条预热根数，不足只跑价格类信号
    kinds: null          # 给了就是白名单，只跑列出的种类
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
- 当前三个内置策略：`trend_rebound`（MA20 斜率向上 + 回踩均线 ±5% 内 + 当日收阳企稳，
  score = 越贴近均线越高）、`box_oscillate`（前 30 日箱体〔不含当日〕，收盘有效突破上沿 +2%
  且前一日尚未破位，score = 突破充分度 + 放量加成）、`pattern_reversal`（低位 V 型反转确认，
  命中即同档 score=0.6；**未进 `default_strategies`，需显式指定才跑**）。

`pattern_reversal` 的口径是用 1200 只票、2016-2026 的 10 日收益做超额定下来的，判据一律要求
"两个时代 + 四个子时期同号"：`v_reversal`（12 根内跌过 ≥10% 且已从 6 日低点回升 ≥8%）
+ `pos60 ≤ 0.35` + 非涨停 + 否决当日爆量。六个看涨 K 线形态（锤子/启明星/吞没/刺透/低位十字星/
圆弧底）在同样门槛下相对"无形态"对照是 -0.08 / -0.10pp（16-21 / 22-26），已确认无信息；
MA5、量能门槛、MA20 加速下行否决、突破形态否决、pos60 下限、量比闸门、大盘择时闸门同样被外部
样本否掉，理由逐条写在策略文件顶部。

基线口径要说清楚：**对"同年全样本"的超额里大部分是市场 β**。换成"信号当天的全样本均值"做同日
基线后，定版组合只剩 +0.51pp(16-21, t=1.6) / +0.40pp(22-26, t=1.3)，而裸 `v_reversal` 是
-0.27 / -0.60pp——反弹状态本身不产生选股 alpha，是"低位 + 不爆量"两道过滤把它翻正的；
pos60 的断点实测就在 0.35（0.35 以上 -1.56/-0.92pp，四子时期全负）。同理，"超额集中在恐慌年"
（2018 +5.8、2020 +7.9、2022 +4.5pp；慢牛年 2021 -0.4、2023 -0.0）现在知道那是 β 的形状，
不是策略的择时能力。所以它的定位是候选股的一组倾斜权重，不是独立开仓的理由。

还有第三条必须一起报的轴：**股票抽样**。上面那 +0.51/+0.40pp 只在自己的抽样
（`codes()[::6][:1200]`，实际约 852 只）里成立；换成同一份数据的其它等距抽样就翻符号
（非基金列表 `[::4][:1200]` → -0.61/-0.79pp；风险分面板 `[::3]` 1710 只 → +0.02/-1.04pp），
ETF 只占命中的 2%，不是它们撑起来的。加权方式也一样能翻符号（日均加权 vs 全池行等权 →
负的 vs +0.9~1.2pp，本策略一律按日均加权报）。所以它的股票级超额应读成「≈0，在 ±1pp 内
随抽样摆动」。对照之下风险剔除层在两个独立样本上给出同一结论（852 只 +0.28pp/10 日、
1710 只 +5.4%/年）——**证据强度差一个量级**，这也是为什么交付的是剔除层而不是买点。

```bash
python -m pytest tests -q          # 内核/数据源/策略/特征/回测/消息面 235 项
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

**出场参数（`--max-hold-days` / `--stop-loss-pct` / `--take-profit-pct`）读法，在 `pattern_reversal`
上按同日基线实测（1200 只、2016-2026、内核同一套"止损→止盈→到期"顺序）：**

- **别加止盈**。TP 3%/1%/0.5% 把个股胜率从 60.7% 抬到 70.8%/77.9%/80.0%，而同日超额从
  +0.27pp 掉到 -0.03/-0.02/-0.04pp —— 全部止盈档都把超额打成 0 以下。这个信号的收益在右尾，
  止盈就是砍右尾。
- **放宽止损能同时抬胜率，但不增加收益**。止损 5%→12%：个股胜率 60.7%→67.8%、同日超额
  +0.39/+0.06→+0.60/+0.29pp（两时代同升，四子时期 3 正 1 约零）；组合层面（等距 300 只、
  2023-10~2026-10、持有 10 日）+50.1%/-17.3%回撤/胜率 45.4% → +49.5%/-21.8%/胜率 54.0%，
  中位单笔由 -479 元转正 +176 元。收益一分没多、回撤多 4.5pp —— 这是风险偏好交换，不是改进。
  单笔尾部代价：p5 由 -8.8% 变 -14.0%，最差 1% 由 -19.7% 变 -24.8%。
- **持有期在个股层面明显前置不足**：f1~f5 的同日超额≈0 甚至为负，f10 才 +0.4~0.5pp，f15
  +0.65~1.07pp（t 最高），f20 回落。但"15 日更好"不能外推成组合结论：用 16-21 挑最优持有期会
  选到 20 日，拿到 22-26 只剩 +0.39pp（≈固定 10 日）；同一窗口同一 universe 下组合是
  10 日 +50.1% vs 15 日 +54.8% 但回撤 -17.3% vs -21.5%，两条路径分不出胜负。**缺省 10 日不动**。
- `metrics.win_rate`/中位单笔会随出场规则大幅变动，跨 run 比较只有在全程"同一规则、同一抽样"
  时才成立；抽样方式变了（见《已知局限》里 `_sampled_codes` 一条）历史数字就不可比。

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

风控决策字段走的是同一条链，没有第二套口径：内核 `SelectResult.extra` → 后端 `risk_verdict/
risk_reason/risk_shares/risk_amount/risk_fill_price/risk_slippage_bp` 六列（同样要过写入白名单，
漏声明就静默丢值）→ `logic/view.js::orderPlanText/riskAdjustText` → 结果页一行
「建议 3800 股 · 3.9 万元 · 预估成交 10.31 · 预估滑点 12.5bp」。前端只做单位换算与文案拼接，
不重算下单量；风控关闭时跑出的旧批次这些列全空，页面上这一行整行不显示（不是显示成「建议 0 股」）。

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
- `--universe N` 现在是**等距抽样**（`_sampled_codes`），不是"取前 N 只"。旧写法
  `codes()[:N]` 在升序目录上只会拿到最老编号的深市主板（`--universe 300` → 全是 000xxx，
  600/002/300/688 一只都没有），跨板块结论直接偏掉：同一个 3 年窗口同一组参数，修正前是
  +21.6%/sharpe 0.55/PF 1.24，修正后 +50.1%/1.03/1.54，且"去掉最大 5 笔后总盈亏"从 -13k 变
  +17.9k。**此前所有 `--universe` 数字都按这条重读**。抽样里含场内基金（159/51x 等），
  因为数据面就是目录下的文件；要剔除得自己传代码清单。
- `metrics.trade_count` 是**平仓笔数**（`closed_pnls` 长度），不是委托笔数；对照 `backtest_trades`
  里的 buy/sell 行数看才完整。
- 无 HTTP 回测入口（只能跑 CLI）、无数据刷新调度；消息面因子尚未进入选股/回测（P2）。
- `settings.yaml` 的 `server.port` 默认 8000，本机该端口被一个无关服务占用，实际部署请用
  `--port` 覆盖（如 18000/18002）并让后端 `SELECTION_PLUGIN_BASE_URL` 指向同一端口。

## 涨停与风险分：买入剔除层（`risk.ml`）

2026-10-11 那一轮涨停/因子研究的交付物。研究本身想回答的是"能不能用机器学习替掉手写判据去
赚钱"，结论是**能赚的那部分是"别买"，不是"买什么"**。

**涨停事件本身**：次日溢价是真的（+1.95pp）但 10 日就归零，板后追买的所有买点口径都为负；
埋伏痕迹（缩量/横盘/首板距离）对板后表现没有预测力。所以没做"涨停策略"，只留下了风险分。

**模型**：`scripts/train_risk_model.py` 用 parquet 宽表的因果列 + 派生共 52 个特征
（`stock_selector/features/risk_features.py`，与实盘打分同一份代码），标签是
`ex10 = 10 日收益 − 同日全体票-日均值`（同日基线是这轮唯一认可的尺子，时代均值口径会让 β 冒充 α），
HistGradientBoosting 走 2019-20/2021-22/2023-24/2025-26 四折 walk-forward，折间留 40 天空窗。

**买入腿判死**（不能当独立策略上线）：全市场样本下头部腿确实逐年 8/8 为正、零成本时 top20
年化超额 +12%，但换手成本 = 243 × 往返费 / 持有天数，10 日持有就是 12%/年；单边 25bp
（散户：佣金万 2.5 双边 + 卖出印花税 0.05% + 小票滑点）超额归零，40bp 全 -7~-9%。
α 还衰减得快（逐笔 ex20 +0.41pp < ex10 +0.66pp），延长持有救不回来；能装下仓位的是日成交
中位 0.84 亿的小票，正好是滑点最贵的地方。概率标签（再涨停/摸 +10%/破位）、EV 排序、
超跌风格闸门也全部判死——机制分别是"概率高=价格已付过钱"和"风格刚生效=已拥挤"。

**剔除腿是真钱，而且成本中性**（同一套 10 日滚动分批，两边同扣成本后差值与成本档位无关）：

| 口径 | 结果 |
| --- | --- |
| 全市场扣掉预测分最低 10/20/30/50% | +2.94 / +4.24 / **+5.41** / +7.32 %/年，8/8 年为正、四子时期全正 |
| trend_rebound 命中池（出场=止损 5% 优先，否则第 10 日收盘） | 全买 -0.18pp → 扣后 30% **+0.14pp**，逐日配对 **+0.28pp/10 日（t+5.7）、8/8 年** |
| pattern_reversal 命中池 | 提升只有 +0.15pp、7/8 年——`pos60 ≤ 0.35` 挡掉的就是同一批票（全市场被剔组 pos60 中位 0.54，池内被剔组 0.21） |

模型没有新信息，它学到的就是"别买高位刚爆过量那批"（最低分票 pos60≈0.90、65-70% 在板后），
但这条负面信息在**没有位置过滤的池上**（trend_rebound、自选股、mongo 规则策略）值 +6.8%/年量级。

**接线**：`settings.yaml` 的 `risk.ml.enabled: true`（已默认开启，2026-10-12 从
`selector.risk_filter` 归入风控层，见下一节）。命中的票在出结果前过一遍 `risk_score.decide()`，
预测 10 日超额低于阈值（`reject_threshold_ex10 = -0.0037`，即样本外分的后 30% 分位）的直接剔除，
保留的把分数写进 `extra.risk_ex10_pp`（百分点）。
**只剔不排**——分数不参与 `score`/`rank`。

```bash
python3 scripts/train_risk_model.py              # 1/3 抽样，约 1700 只，产出 data/models/risk_ex10.*
python3 scripts/train_risk_model.py --stride 1   # 全市场，内存约 6GB
```

运维要点：
- 模型按 mtime 缓存，重训后**不必重启服务**；但 `risk_score.py`/`selector.py` 改动要重启 8010。
- 回溯行数取 `SCORE_ROWS = 700`（远大于 `MIN_BARS = 260`）：`age`（距上次涨停根数）训练时用
  全历史，窗口太短会把"很久没涨停"的票截成 NaN，实测 260 根窗口有 6/284 条剔除判定翻转，
  700 根为 0/258；parquet 是整文件读进来再截断的，多取行数不增加 IO。
- 代价：parquet 源多读 ~45 列，1500 只票 7.9s → 15.9s（全市场约 +35s，后端超时 600s 够用）。
- 失败姿态与数据源一致：模型缺失或 meta 特征清单与代码不一致 → RuntimeError，绝不静默放行；
  历史太短/整行大面积缺失 → 返回 None，判不了就让它通过，不猜。

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

整套后台由 supervisor 常驻八个进程，对外经 nginx 的 8080 端口反代（nginx 容器复用 `lex_math_new` 项目的 `lexmath-nginx`）。所有配置的源副本都在 `deploy/`，改完再拷去生效位置。

| supervisor program | 端口 | 职责 | 配置副本 |
|---|---|---|---|
| `selectstock-api` | 8010 | FastAPI 后台 + 无构建前端（静态页同进程托管） | `deploy/selectstock-api.conf` |
| `selectstock-celery-worker` | — | 真正跑回测内核的进程（Redis db3 队列，concurrency 1） | `deploy/selectstock-celery-worker.conf` |
| `selectstock-plugin-selector` | 8002 | 选股内核的 HTTP 适配层，后端薄代理 | `deploy/selectstock-plugin-selector.conf` |
| `selectstock-plugin-portfolio` | 8001 | 持仓诊断插件（判定与收益公式都在这里） | `deploy/selectstock-plugin-portfolio.conf` |
| `selectstock-scheduler` | — | 采集调度：APScheduler 跑 `config/inputs.conf` 的 `script://` 任务 | `deploy/selectstock-scheduler.conf` |
| `selectstock-quote-service` | — | 盘中快照轮询 → Redis db5 的 `rt:quote` / `rt:ticks` | `deploy/selectstock-quote-service.conf` |
| `selectstock-intraday-engine` | — | 盘中策略计算：分钟条 → 信号 → 风控 → `rt:decisions` + 当日 parquet | `deploy/selectstock-intraday-engine.conf` |
| `selectstock-alert-pusher` | — | 决策 → 钉钉/企业微信摘要；**仓库副本 `autostart=false`**，填好 webhook 才开 | `deploy/selectstock-alert-pusher.conf` |

**盘中三进程只在交易时段有用**：收盘后它们空转（没有新 tick 就没有决策），不产出内容也不影响盘后链路。告警进程的 `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_SECRET` 是机器人凭据，**只写 `/etc/supervisor/conf.d/` 里那份**，仓库这份留空并关着自启；它和后端 `conf/notify_channels.json` 的推送渠道是两套东西，别改错地方。

**端口为什么不是 8000**：文档约定后端占 8000，但本机 8000 已被 `/data/work/lex_math_new/backend` 占用，所以 API 改用 **8010**，对外统一走 nginx **8080**。换端口只需同时改 `deploy/selectstock-api.conf` 的 `--port` 与 `deploy/nginx/selectstock.conf` 的 upstream。

### 前置条件

- Redis 在 `127.0.0.1:6379`，回测任务用 **db 3**（与旧 `stock_select` 项目分库隔离），broker 与 result 同库；盘中实时链路用 **db 5**（`rt:*` 一组键，见《盘中策略引擎与告警》）
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

# 3) supervisor 八个常驻进程（盘中告警那份 autostart=false，填好 webhook 再 start）
sudo cp deploy/selectstock-*.conf /etc/supervisor/conf.d/
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status                               # 除告警进程外全部 RUNNING 才算起来

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