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
`docs/spec/插件化量化选股系统 - 需求与架构设计文档.md`，技术因子手册见 `stock_selector/docs/READ.me`，
主力剧本的定性自洽性检查（流动性/承接约束，非概率）见 `stock_selector/docs/主力博弈约束模型.md`。

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
├── risk/            # 风控层：snapshot 行情口径 / context 单票上下文 / sizing 仓位与滑点 / rules 十条规则 / engine 短路编排
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
吃多少、按什么价吃」，十条规则按固定顺序执行、命中即短路：

| 顺序 | 规则 | 拦掉什么 | 为什么排在这里 |
|---|---|---|---|
| 1 | `tradability` 涨跌停可成交性 | 涨停买不进、跌停卖不出 | 标的根本吃不到，后面的计算都是白费 |
| 2 | `t_plus_1` T+1 | 今日有买入成交的股票当天不给卖出建议 | 明天才卖得掉，推了也执行不了 |
| 3 | `family_flip` 同族翻面冷却 | 同一个指标的相反两面（盘口托单/压单、均线金叉/死叉…）在 `flip_cooldown` 秒内翻面后的第二条 | 几十秒内翻面是抖动不是新信息；两条都推等于同一分钟既喊卖又喊买，用户只能关掉通知 |
| 4 | `buy_quota` 买入名额 | 当日盯盘名单排名 `≥ max_buy_codes` 的买入信号 | 资金就这些，一天铺开几百只等于没推 |
| 5 | `liquidity` 流动性下限 | 近 20 日日均成交额 < `min_avg_amount` 的小盘票 | 盘口薄，信号再好看也滑没了 |
| 6 | `position` 单票仓位上限 | 超过 `总资金 × max_position_pct` 的部分（改成 `adjust`，不是剔除） | 先把下单量定下来，滑点才有单额可估 |
| 7 | `cash_budget` 当日可用资金 | 买入金额超出账户当天还剩的钱（压到剩余额度，不够一手就 block） | 要在 `position` 之后：单票上限不管别的持仓占了多少，五只一起放就是 100% 还多 |
| 8 | `slippage` 滑点预估 | 半价差 + 冲击成本 + 信号价漂移超阈值，或单额占日成交额超 `max_participation` | 免费快照有延迟，看到的价格不一定成交得到 |
| 9 | `model` 买入风险分 | 预测 10 日超额低于剔除阈值的票（研究依据见《涨停与风险分》） | 要跑 700 根日线的特征，最贵，放最后 |
| 10 | `adx14_rank` 趋势分位闸门 | 当日全市场 ADX14 分位低于阈值的买入 | 全市场分位要整批数据，只能等前面单票的都判完 |

2/3/4/7 四条判的是**此刻/今天**的处境（今天买过没有、同一个指标刚给过哪个方向、名单里排第几、
还剩多少钱），不是标的本身的性质，所以只有盘中链路给得出输入；盘后内核拿不到就放行并留
`skipped` 指标，行为与加规则之前一致。

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

**行情口径为什么不能只读日线**：本地日线只有涨停标记 `lu`、**没有跌停列**，涨跌停阈值还得分
主板/创业/科创/北交所/ST/基金。所以涨跌停价、换手率、精确成交额取
`data/market_snapshot/{日}.parquet` —— 交易所下发的原值，由 `scripts/fetch_market_snapshot.py`
每个交易日 15:30 落盘（整轮 12 秒；命中率不足 60% 直接拒绝写出残缺文件）。缺文件时的降级
顺序写死在 `risk/snapshot.py`，规则层不再各判各的：①当日落盘快照 → ②实时快照（腾讯，只给
当天；回测/盘后用 `allow_live=False` 关掉）→ ③都没有则字段为 `None`，规则**跳过判定**而不是
猜一个价。盘后内核按每只票自己的基准日取数，所以历史日期的信号不会混进今天的行情。

**日线 `amount` 从哪来**：日线爬虫的两个源（腾讯 `ifzq`、新浪 `getKLineData`）**都不给成交额**，
所以 `scripts/kline_spider.py` 干脆不写这个字段（留 `null`，绝不写 0——0 会被下游当成真·零成交）。
精确值另有两条路进来：① `scripts/merge_snapshot_amount.py` 每天 16:50 把快照成交额并回当天
那根日线（`--days 5` 往回自愈，因为爬虫只写"比库里最新更靠后"的 K 线，哪天少抓一批，补进来的行
当时还没有成交额。2026-10-23 从 15:45 挪到 16:50：沪市那批行本身要 16:24~16:31 才落库，15:45
那次对沪市是「要合并的行还不存在」，于是 17:15 的面板增量读到的是有价无额的一天，
而 `vwap = amount/volume` 缺额时会兜底成 `close`，Alpha158 里唯一用 `$vwap` 的 VWAP0 就退成常数 1）；
② `scripts/backfill_kline_amount.py` 从腾讯的兄弟接口 `newfqkline` 回补空窗
——它比爬虫用的 `ifzq/fqkline`（只有 6 列）多两列，第 8 下标是成交额、单位**万元**，和东财/快照
逐只对得上（差几十元 = 取整）。东财 `push2his` 也能给，但本机打它会被随机 RST（并发一高整批挂），
所以回补走腾讯。两条都只写 `amount`、`upsert=False`，绝不凭空造 K 线；写完要
`build_kline_features.py --full` 重建才会进 parquet（增量路径只追加新交易日）。即便如此，停牌/
退市行仍然没有成交额，所以 `risk/context.py` 的 `volume × close × 100` 兜底要留着——实测两者比值
p5=0.98 / p95=1.03，兜底只丢精度、不会误导流动性判据。

**成交量的单位按板块不同**：拿盘后快照做基准（`成交额/(volume×price)`，手≈100、股≈1），
主板/创业板/场内基金都是**手**，唯独**科创板 688/689 是股**——腾讯的两个接口对科创板都给股。
爬虫自 2026-10-15 起按号段折成手入库（`kline_spider._tencent_volume`）；库里 2026-05-22~10-13 那段
（49,624 行，一半还没打 `source` 标记，所以只能按比值判）已由 `scripts/fix_star_volume_unit.py`
一次性折算完，原值留在 `volume_raw`、统一打 `vol_unit:"lot"`，重跑天然幂等。盘后快照
`data/market_snapshot/*.parquet` 的 `volume` 列对科创板**仍是股**（没动，改了会和已落盘的历史文件打架）。

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
| `rt:engine-heartbeat` | String(JSON) | 策略引擎上一轮统计（读到/回放/池外跳过/池/评估/决策/拦截/耗时） |
| `rt:metric:{code}` | Hash | 盘中实时指标，由策略引擎写、告警与前端读 |
| `rt:watchdog` | Hash(name → JSON) | 看门狗已报故障的链路与起始时刻：重复抑制、恢复通知用它算时长 |

```bash
python scripts/quote_service.py --code 600000 000001        # 调试自选
python scripts/quote_service.py --watchlist data/watchlist.txt --interval 5
python scripts/quote_service.py --from-app --interval 5     # 选股命中 + 持仓（部署用的口径）
python scripts/quote_service.py --all --interval 15         # 全市场（限流风险自担）
python scripts/quote_service.py --status                    # 只看链路心跳
```

**盯哪些票由 `realtime/pool.py` 决定**：`--from-app` 只读（`mode=ro`）打开 `app.db`，取最近一次
`status='done'` 选股的代码（按 `rank_no`，与 App 里同一顺序）并上启用中的持仓，实测约 360 只。
池是**换日自动重取**的：选股在盘后 17:30 产，第二天开盘盯的就是前一天那批名单，常驻进程只在启动
时读一次会静默盯过期清单；读库失败或取到空池则沿用上一批并 WARN（「今天一只都不看」必须是人来
决定的事）。持仓同时按 `quantity × cost_price` 的成本口径进风控 sizing，作为单票已用额度。
盯全市场 6600 只的问题不在算不动，而在 15 秒一拍要发 111 个请求、下游 `max_eval` 的预算全花在
没人看的票上——真正盘中要动的只有等买点的命中票和等卖点的持仓票。

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

**风控只有一套**：盘中信号和盘后选股交给同一个 `RiskEngine`、同样十条规则，所以「盘中说可以买」
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

三条口径值得单说：分钟指标只在新的分钟条**收口**时重算（每票每分钟一次），交叉类一律在收口条上
判——未收口那根的量还会继续涨，用它判「放量」就是同一分钟报三次、每次倍数都不一样；价格类信号
（量比/换手/触板/事件）直接读 `rt:quote` 的最新快照，不跑 talib；**卖出方向只对持仓判定**
（`View.held`，由引擎按池里的持仓市值给出）——池子绝大多数是当天选出来的票，给没有仓位的票推
「卖出」不是信号判错，而是这条建议根本执行不了。上线前实测一个上午 320 条卖出决策里有 308 条
属于这种，光靠攒批归并只是把刷屏打包。

全市场 6600 只的盘中计算很容易变成自我 DoS，预算全部写在 `intraday:` 里：`max_eval` 限制一轮跑
talib 的只数，`once_per_day` 让同票同信号**当天只报一次**（买入和卖出同一个口径；去重表写在
redis 的 `rt:emitted:{日期}`，所以引擎重启不会把上午报过的信号再报一遍，跨天自动放开），
`cooldown` 是它的退路——关掉日级去重、或去重表写不进 redis 时按这个秒数抑制，`replay_horizon`
让启动时回放的旧 tick **只补分钟条、不产信号**（那些价格是几分钟前的，此刻报出来只会诱导一个
下不到的单）。日线宽表（流动性/冲击/风险分/压力位都要它）只在真有信号要过风控时才读，按
`kline_cache` 只数 LRU 缓存。

引擎也按池过滤：池外的 tick 直接丢（不建分钟条、不评估），所以 `--all` 的上游配 `--from-app`
的引擎时，监控范围仍由引擎这一侧收敛；空池表示「没给池口径」而不是「一只都不看」，此时不过滤。
换日时引擎与轮询服务同一时刻重取池，代码与持仓额度一起换。

```bash
python scripts/quote_service.py --from-app --interval 5             # 1) 行情上行
python scripts/intraday_engine.py --from-app --max-eval 300 --mongo # 2) 指标 → 信号 → 风控 → 决策
python scripts/intraday_engine.py --status                       #    看引擎心跳
python scripts/alert_pusher.py --dry-run                         # 3) 先看消息，确认无误再常驻
python scripts/alert_pusher.py --channel-key ding_ops            #    发到页面「通知渠道」配的钉钉
```

上面三个脚本已由 supervisor 托管常驻（`selectstock-quote-service` / `-intraday-engine` /
`-alert-pusher`，配置在 `deploy/`），手工命令用于调试与冒烟。告警进程的 webhook **默认取自管理
页面配的「通知渠道」**（`stock_selector_api/backend/conf/notify_channels.json`，按 `--channel-key`
或该类型第一条启用的渠道）：加签密钥在那边是 AES-GCM 密文，pusher 复用后端的 `core.security`
在发送前才解出明文，日志里只留主机名。`ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_SECRET` 仍然有效，
但退为「临时换群」的覆盖手段。仓库副本默认 `autostart=false`（它要能导入 backend 目录），
确认群里收到过消息再打开。

这条链路坏了的表现是**群里变安静**，而「安静」和「今天没机会」看起来一模一样，所以第四个进程
`scripts/link_watchdog.py` 专门盯链路自己：每 60 秒按 `--status` 同一套口径读 `rt:heartbeat`
与 `rt:engine-heartbeat`，三种情况算故障——读不到心跳键、心跳龄超过阈值、上一轮
`status=fetch_failed`（价是旧的，指标不可信）。阈值会给「进程自己声明的休眠」让路
（拉不到快照退避 60 秒、休市睡到 900 秒都不算卡死），并且开盘后 5 分钟内不判故障（心跳键带
4 小时 TTL，那几分钟当天的心跳可能还没写上）。**只在交易时段判断**，收盘后静默。

重复抑制与恢复通知的状态写在 redis `rt:watchdog`（不在进程内存里）：看门狗重启不会把上午报过
的故障再报一遍，恢复时补一条并带上中断时长——只报坏不报好会让人以为链路一直是坏的。渠道与
`alert_pusher` 同源（同一个 `--channel-key`），读不到 redis 时整轮跳过、一个字都不发（这时状态
表也写不进去，发就等于刷屏）。

```bash
python scripts/link_watchdog.py --status                          # 现在有没有超阈值（盘外 rc=0）
python scripts/link_watchdog.py --dry-run --ignore-session --rounds 1  # 试跑：只打印，不写状态表
python scripts/link_watchdog.py --channel-key ding_ops            # 常驻
```

告警进程**只排版不算术**：建议股数、预估成交价、滑点全部来自引擎与风控，它做单位换算和攒批
（默认 15 分钟合成一条 markdown，避免刷屏、也避开机器人 20 条/分钟限流），一批超过 40 条
（`--max-lines`）时每段截断并指回当日决策文件——机器人对消息长度有上限，超长消息是整条拒收，
比少列几条更糟。发送失败重试两次就放弃（决策已落盘，消息可丢）。启动时游标放在**流尾**，重启不会
把上一交易日的存量当新信号重推（要重发用 `--replay`）。密钥只从环境变量读，日志里只打主机名——
`access_token` 与 `sign` 都在 URL query 里，打出来等于泄露机器人。需求文档的「调用券商 API 下单」
按约定**不做**：本系统只推告警。

消息节奏与三条成交约束是同一件事：**A 股 T+1、账户资金有限，一天里对同一只票喊八次「卖出」
并不能多卖出一次**，只会让人关掉群通知。所以告警侧同票同方向当天只进第一条消息（后面的只计数、
在文末写明归并了几条），风控侧则把「今天买的今天不能卖」「买入超出当日可用资金就压小」「一天
只给名单前 3 只的买入下单建议」判成 block/adjust 并带中文理由，信号侧的卖出方向干脆只对持仓判定
（见上文 `View.held`）；跌停与利空（`--instant`，都只可能来自持仓票）走即时通道，不等窗口。
**一条决策都没丢**：引擎照常全量写 `data/intraday_decisions/<日>.parquet`。

**同一只票不能在同一分钟既喊买又喊卖**（2026-10-22 早上真的发生了：开开实业 600272 在 09:30:19
因「卖盘压单」放行卖出、09:30:54 因「买盘托单」放行买入，两条进同一条消息，用户只能问「那我听哪个」）。
口径：把本机 9 个决策文件（2026-10-12→10-22，7 个交易日、放行 1092 条）按「同一票同一天正反都有」
筛，得到 **88 个票日**（涉及 78 只票、826 条放行决策），**88 个全部**都能对上一对「同一个指标的相反两面」
（MACD 金叉×死叉 83、均线金叉×死叉 82、盘口托单×压单 69、布林上下轨 52、打单流入×流出 25、RSI 超买×超卖 11、
量比×换手 5）——也就是说这不是「两个独立证据打架」，是同一个量抖了一下被当成两条。分三层收口：

- **信号层**（`intraday/signals.py` 的 `FAMILY`）：镜像种类登记成同一个族，同一个族在同一拍里正反都成立
  → 两条都不发（新加信号忘了登记就退化成「不参与族仲裁」，不误伤）；
- **风控层**（第 3 条 `family_flip`，`risk.flip_cooldown` 默认 1800 秒）：同一个族在窗口内翻了面，第二条
  block，理由照样进消息的「风控已拦截」段（「同一指标（盘口）36 秒前刚给出卖出建议，30 分钟内翻面按抖动处理」）；
- **推送层**（`alert_pusher`）：兜住不同指标各说一头的情况（如打单流入买入 + 跌破支撑卖出）——同一只票
  当天正反都给了建议时，两条一起撤下「可执行」，改列一段「信号冲突（同一只票当天既买又卖，两条都不给下单建议）」，
  两边的信号名与时刻都写出来；反方向落在**下一批**时上一条已经发出去了，消息就直说
  「09:30:19 那批已推过卖出（卖盘压单），这条反向不再给下单建议」。

判不了就不判的口径照旧：`family_flip` 只在盘中拿得到族与上一条方向（盘后内核不传 `family` → 放行留
`skipped`），推送层也不替你决定该听哪一条——它只做「两条都别按下单依据」这一件事，要按哪条是你的判断。

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

## 持仓诊断的灾难线（−5%）与四行硬数

诊断的 `hold` 过去只有一句话理由，读不出「这条持有值多少钱」。现在 `suggestion` 里是四个可核对的数：

- **均线卖点在哪**：`sell_trigger_price` 与现价的方向差（由同一条 sell 表达式反算，见 `backtest/rule_trigger.py`，
  不会出现「理由是跌破均线、给出的价却对不上」）。判定用日线指标行、价格用实时价，两者会分叉，所以判成
  hold 也可能出现「实时价已跌破卖点」——这不是矛盾，是盘中该动的信号；
- **同类 hold 的转卖频率**：判成 hold 的时点里 **45.3%** 在 20 个交易日内自己变成 sell（72.4 万个全市场点）；
- **尾部折算成钱**：全体 hold 点 20 日收益 p5 = **−17.5%**，按持仓市值折算成「再亏多少元」。**只报尾部不报
  均值**：所有状态分箱的 10/20 日均值差都小于换手成本，只有 p5 有区分度；
- **回本所需涨幅**：现价低于成本价时给出。

只有判成 hold 才印第二条和第三条——那是 hold 那一格的分布，判成 sell 还印「同类 hold 有 45% 转卖」是自己跟自己抬杠。

**灾难线**（`rules.py` 的 `disaster_stop_pct`，默认 **5.0**——2026-10-28 用户点名统一到 −5%，此前这里是 8.0
且只有回测侧改过，导致浮亏 7.8% 的票照旧判 hold；可用 `PLUGIN_RULES_FILE` 指向的 JSON 一行覆盖，夹紧在 1~30）：
浮亏越过它就判 sell，排在趋势线**之前**，是**唯一一条看成本价的判据**。理由不是「亏损改变预测」——
实测过成本价对后 20 日收益没有方向预测力——是「账户不能被一笔打穿」。

阈值选定过程（只取趋势线判为 hold 的时点，往后 20 个交易日，723,769 个点）：

| 阈值 | 灾难线触发率 | 与趋势线重叠 | 只灾难线触发（=新增告警） | 这部分按线离场的代价 |
|---|---|---|---|---|
| −5% | 50.0% | 37.4% | 12.67% | 卖在 −5%，这批点实际拿 −2.03%，每次**少赚 2.97 pp**；56.1% 到第 20 天已涨回线上 |
| −8% | 33.2% | 29.9%（占触发点 90%） | 3.30% | 卖在 −8%，实际拿 −5.55%，每次少赚 2.45 pp；52.9% 反弹过线 |
| −10% | 24.8% | 23.8%（96%） | 1.04% | 卖在 −10%，实际拿 −7.42%，每次少赚 2.58 pp；50.4% 反弹过线 |

选 −5% 是**按用户点名的风险口径**，不是数据最优：它把「只由灾难线触发」的新增告警从 3.30% 放到 12.67%，
摊到全部 hold 点每个 20 日周期 −0.38 pp，且这批告警里 56.1% 到第 20 天自己涨回线上方。回测侧的尾巴尺子
（`scripts/audit_sell_rules.py`、`scripts/ruleset_posterior.py`）现在都 `import load_rules()` 取这一个数，
不再各写一份常量；文案里的「跌超 N%」跟着回放产物 `contract.tail_threshold_pct` 走，不照字段名 `tail8` 念。
三点必须直说：**① 灾难线 90% 是冗余的**（−8% 触发的时点里九成趋势线在同一窗口内也会响）；
**② 它在期望上是负的**，摊到全部 hold 点 = 每个 20 日持有周期 −0.08 pp（−5% 是 −0.38 pp，−10% 只 −0.03 pp）；
**③ 数据上 −10% 严格优于 −8%**，选 −8% 是按风险口径买的保险，保费 −0.08 pp/周期，不是策略。

验收口径：灾难线只会在趋势线判 hold 的点上新增 sell，`--stats` 里原有 sell 触发次数不升高。

## 持仓判定的可信度：scripts/build_judgement_ledger.py

诊断每天给出 hold/sell/add，但**从来没被核对过**：库里存了判定，没存「这条判定后来怎么样了」。
没有学业，可信度就只能靠感觉。这个脚本把 2011 年至今每一天、每一只票按**插件同一套规则**
（`portfolio.rules.load_rules()`，不在这里抄常量）判成状态，连 T+10/T+20 学业一起落盘：

```bash
python scripts/build_judgement_ledger.py --limit 300 --report   # 抽样只看表，不落盘
python scripts/build_judgement_ledger.py                        # 全量重建：90 秒 / 1,090 万行 / 峰值 ~1.2 GB
```

产出（整表覆盖，重跑幂等）：

- `data/judgement_ledger/ledger_<year>.parquet` —— 逐日逐票：`action/heat/heat_bin/fwd_ret_10/fwd_ret_20/flip_sell_20`
- `data/judgement_ledger/posterior.parquet` —— 按 (状态 × 热度箱) 聚合的 Beta(1,1) 后验

**可信度怎么算**：格子里上涨频率 → Beta(1,1) 后验均值 + 95% 区间。区间宽于 2 pp 就判「样本不足」，
宁可不给数字。区间窄还不够，另加一条：**三段时代（按全体台账日期三分位切）的 10 日均值必须同号**，
否则只是某个年代的红利。

**读数字前必知的四个坑**（脚本头注释同一份）：

1. 热度用 `member.parquet` **最新一天**的成分表回推历史 —— 一只票现在被归进热门概念，可能正是
   因为它涨了。这是已知前视，所以热度只能当**门控**，不能当证据；
2. 台账**只有趋势线，没有灾难线**：−5% 那条要成本价，全市场没有「假想成本价」，所以线上持仓的
   sell 会比台账多；
3. `fwd10` 是 T+10 **交易日**收盘收益，不含任何交易成本；末尾 10 个交易日没有学业、不落盘；
4. 判定规则被改成不可向量化的形状时脚本直接 `SystemExit`，不会静默按另一套口径算出假后验。

已测结论（全市场重建，2026-10-12：台账 10,904,649 行、6,631 只票、3,876 个交易日）：

- **hold 的上涨概率系统性低于抛硬币**：全部 hold 点 n=8,969,490 → **47.57% [47.53, 47.60]**（区间宽
  0.07 pp），10 日均值 +0.09%、中位 −0.24%、p5 −12.74%，且三段时代不同号。「拿着不动」这件事本身
  没有正的方向优势，它的价值只在避免卖点线之外的波动被打断；
- **sell 触发后 10 日均值仍为正**：全部 sell 点 51.18%、均值 +0.87%（时代同号）——卖点线是**波动率
  工具**，不是方向预测工具，别把「判成 sell」读成「要跌了」；
- **热度不给 hold 加分**（只有热板块 49.56% 略高，其余 46~48%），但**该门控 add**：冷板块加仓
  42.90%／均值 −1.19%（三段时代同号为负），热板块加仓 53.05%／均值 +1.46%（三段同号为正），
  gap 超过 10 pp，是全表里唯一又宽又稳、又跨时代同号的差分；
- 尾部没有因为热度变好：热板块 hold 的 p5 = −13.85%，比冷板块 −13.63% 还深一点。

## 退出规则并跑：`--ab-rules`（结论：不换，而且不该指望这一族换得出东西）

现行默认卖出规则**没有标定过**：`price <= ma10 * 0.95` 最早是初始提交里 `tests/test_backtest_rules.py`
的测试夹具（只为验证 `RuleEvaluator` 能跑通），M2 抄成了默认值；`close >= ma5 * 1.03 and rsi14 <= 65`
更是计划文档里现编的。0.95 / 1.03 / 65 是占位值，不是标定值。所以「换一条更聪明的」是个真问题，
但答案不能靠感觉——`--ab-rules` 把 11 条候选放在**同一批 1,096 万个时点**上并跑（只读盘、不落台账，84 秒）：

```bash
python scripts/build_judgement_ledger.py --ab-rules
```

判据在跑之前钉死六条，**同时**成立才换：换手可比（基线 1/3~3 倍）· avoided 更负 · 离场成本更低 ·
灾难点预警率更高 · 三段时代同号 · 代码对半切开两半各自同号。后两条判的必须是**超额**（同期同半体
全体均值已扣掉）——全样本 10 日均值只有 +0.26%，符号由大盘决定，拿原始均值判同号会把 11 条连基线
一起判死，那条门槛就成了没有信息的门槛（第一版就犯了这个错）。

2026-10-13 全市场结果：**0/10 条候选能替换基线**。比"谁赢"更值钱的三条读数：

1. **这一族规则全部 avoided 为正**：基线触发点 10 日均值 +0.87% vs 全体 +0.26%（avoided +0.62pp），
   换成 ATR 尺子（+0.02~+0.72pp）、ma20、布林下轨、`pos60<=0.20`、距 20 日高点回撤 10%，
   **没有一条把符号翻成负**。也就是说「跌破某条线就卖」系统性地卖在超跌反弹之前，换尺子只是
   在同一个负期望的族里挪位置——它不是方向信号，是波动信号（与上面 sell 后验 51.18% 一致）。
2. **想多买预警就要多付换手**：`ma10-0.5ATR` 把灾难点预警率从 62.6% 抬到 87.8%，代价是触发率
   10.8% → 32.0%，离场成本 0.104 → 0.118 pp/时点；反过来 `ma10*0.90` 成本最低（0.060）是因为
   它几乎不触发（2.5%）、预警也只有 23.4%。按 lift（预警率 ÷ 触发率）排，基线 5.82 在 11 条里第 3，
   赢它的两条（`ma10*0.90` 9.24、布林下轨 6.18）都是"少管事"换来的，不是"更准"。
3. **唯一时代同号的是 `pos60<=0.20`**，而它压根不是卖出信号，是「跌透了」的状态标记——它的超额
   +0.21pp 仍为正。这正说明该用这些预处理参数的地方不在卖出触发：在**买入侧的剔除**（ML 风险分
   已经验证只有否决有用，见下文 ml 小节）和 **add 的门控**（热度差分是全表唯一又大又稳的信号）。

所以持仓链路上真正动过且值得动的仍是那两条**不在这族里**的判据：灾难线（唯一看成本、看的是你的钱
而不是形态）和热度门控 add。要再往上走，方向是给 `holdings` 补一票四件套（建仓理由/失效价/硬止损/
时间上限），而不是继续调均线系数。

## 回测的「赢比」有两个口径，必须分开看

页面历史上把一个数标成「盈亏比」，其实是 `profit_factor = 总盈利/总亏损`。它和单笔赔率
`payoff = 平均盈利/平均亏损` 差一个胜率因子：

```
profit_factor = payoff × 胜率 / (1 - 胜率)
```

本项目实测到的胜率长期在 33~37%，所以 **payoff 2.0 只对应 profit_factor ≈1.1，而 profit_factor
2.0 需要 payoff ≈ 3.4**。凑哪个都不等于赚钱——`expectancy`（单笔期望，已扣佣金印花税）才是：
`metrics.py` 现在三个都算，前端卡片同时显示「利润因子(总盈/总亏)」「赔率(均盈/均亏)」「单笔期望(元)」。
`BacktestEngine(..., params=...)` 也已把入参写进 `backtest_results.params`，否则一条历史结果
无法复现（此前 `params` 恒为 `{}`）。

两个扫描脚本：

| 脚本 | 回答什么 | 口径 |
|---|---|---|
| `scripts/backtest_exit_scan.py` | 只动出场（止损/止盈/持有天数）能不能把赢比凑到 2.0 且不亏钱 | 3 时代 × 2 抽样 × 7 组参数，**全部样本**都 payoff≥2 且期望>0 才算过 |
| `scripts/build_signal_posterior.py` | 每条选股判据历史上值多少钱（选股依据） | 命中点的 T+10 相对**当日全样本均值**的超额，Beta 后验 + 95% 区间 + 时代/抽样同号 |

`--ab-rules` 否掉的是「换一条卖出线」，`backtest_exit_scan.py` 否掉的是「加一条止盈线」，
两者是同一族假设（破线/到位即卖 = 波动率工具）的两个方向。

### 2026-10-13 三轴跑完的读数：赢比能凑到 2.0，凑不到「且不亏钱」

**退出扫描 84 格**（2 判据 × 7 组出场 × 3 时代 × 2 抽样）：**0/14 组出场参数**能做到
「所有格 payoff≥2.0 且所有格单笔期望>0」。

- 最接近达标的是 `box_oscillate` 止损5% / 止盈15% / 持有20 日：payoff 1.98~2.31（6 格里 5 格过 2.0），
  但只有 3 格期望为正，负的 3 格全在 2022 时代和 2025 的另一批抽样上。
- 把出场放松到 10%/30%/40 日，`pattern_reversal` 的 payoff 最高到 **4.03**，单笔期望反而
  −669~+2134 元。同一把尺子上「赢比」和「赚钱」是两件事，这就是为什么必须两个数一起看。
- 绝对收益要先减同样本的等权买入持有才能读：三个时代基线分别是 **+36.4% / −3.0% / −2.4%**
  （最近一年 2025-09→2026-09 只有 33.7% 的票上涨）。减掉基线后超额均值为正的只有
  `box_oscillate`(5/15/20) **+1.9pp** 和 `pattern_reversal`(6/18/25) **+0.9pp**，其余 12 组为负。

**判据后验**（`scripts/build_signal_posterior.py` → `data/signal_posterior/signal_posterior.parquet`，
800 只 × 3 时代 × 3 抽样平移 = 每策略 9 格，口径是命中点 T+10 相对当日全样本均值的超额）。
**结论必须按日等权下**（先算当天命中的均值，再对天平均）：行等权会被命中扎堆的恐慌日主导，
同一判据两种加权差 1.6pp 甚至反号，所以两个都落盘、只按日等权读数。

| 判据 | 命中点 | 跑赢当日全体比例 | 超额·日等权 | 超额·行等权 | 日等权正格数 |
|---|---|---|---|---|---|
| `pattern_reversal` | 8,825 | 51.2% | **+0.10pp** | +1.72pp | 6/9（−2.07~+1.76） |
| `box_oscillate` | 19,409 | 40.7% | **−1.08pp** | −0.40pp | 2/9 |
| `trend_rebound` | 166,120 | 43.3% | **−0.32pp** | −0.16pp | 1/9 |

三条判据的日等权超额都贴 0 或在水下，`sign_stable=False`（换时代/换抽样翻号）。这跟上一张表
是同一件事的两面：**出场参数能把 payoff 推到 2.0 以上，但推不出正期望，因为买入腿本身没有超额**
——赢比是截断亏损凑出来的，不是选股选出来的。

`stock_selector/core/signal_posterior.py` 把这张表压成策略级摘要挂进每条命中结果的
`extra.signal_posterior`（表不存在时返回 None，不编数）。这里修掉过一个真缺陷：格子级
`credible` 只等于「Beta 区间宽度 <2pp」即**样本够不够**，原样 OR 上来会让 `trend_rebound`
（−0.32pp、换抽样翻号）报成"可信"。现在拆成两个轴并合成一句话：

| 字段 | 含义 |
|---|---|
| `sample_adequate` | 全部格子的置信区间 <2pp（只说样本够） |
| `sign_stable` | 全部格子的日等权超额在 0 的同一侧（只说结论稳） |
| `credible` | 上面两个**都**成立 |
| `verdict` | `样本不足（置信区间过宽），不定论` / `换时代/换抽样会翻号，未标定` / `正超额·三轴同号` / `负超额·三轴同号（反指）` |

`tests/test_signal_posterior.py` 钉住这条口径：**负而稳定也要标「反指」，方向不决定可信度**；
均值正、CI 窄但翻号的格子必须判 `credible=False`。

这条摘要现在贯通到 App（此前只在内核里，没有任何消费方）：
`select_results` 新增 `posterior_verdict / posterior_excess_pp / posterior_n` 三列
（`db/__init__.py` 幂等补列），`selection_service._http_run()` 从 `extra.signal_posterior` 取，
选股结果页每条下方显示灰色一句 `依据 · <verdict> · 10日超额 …pp · 样本 …点`。
**页面只显示 verdict，不单独显示那个平均数**——单独显示平均数等于把抽样噪声说成结论。
后验表还没建时不整行消失，而是显式写「依据 · 未标定（判据后验表还没建）」，
因为空行会被读成"没问题"，而真实含义是"还没测"。按当前表，三条现役判据没有一条到达"有依据"。
生效需重启 8002 与 8010（`alembic`-less 补列在启动时自动跑）。

### 成交可执行性审计：赢比 2.0 里有多少是撮合假设的钱

回测引擎按**当日收盘价**撮合，且没有任何涨跌停约束（`stock_selector/backtest/engine.py`
只检查取到价，不检查那天能不能成交）。而信号是收盘后算出来的，真实下单最早在次一交易日。
这两条都是对策略有利的假设，所以加只读审计 `--audit`（`scripts/backtest_exit_scan.py::_fill_audit`，
用日线自带的 `pct/high/low` 判板块涨跌停，阈值走 `features/base_columns.board_limit_threshold`）。

跑法：`box_oscillate` × 现役出场参数（止损 5% / 无止盈 / 持有 10 日）× 3 时代 × 有无剔除层两臂，
样本 60 只、平移 0：

| 时代 | 臂 | payoff | 单笔期望 | 卖在跌停日 | 其中一字 | 跌停日占亏损 | 买在涨停日 | 其中一字 | 次晨开盘差 | 涨停单次晨差 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2019 | 无闸门 | 1.38 | −232 元 | 6/98 | 3 | 13% | 22/102 | 4 | +0.27% | +2.56% |
| 2022 | 无闸门 | 2.07 | +67 元 | 3/91 | 0 | 7% | 34/93 | 5 | +1.08% | +3.58% |
| 2025 | 无闸门 | 2.45 | +199 元 | 6/86 | 0 | 21% | 31/91 | 2 | +0.67% | +3.16% |
| 2019 | 有闸门 | 1.62 | −155 元 | 5/53 | 0 | 24% | 26/53 | 6 | +1.25% | +1.70% |
| 2022 | 有闸门 | 2.18 | +278 元 | 3/63 | 0 | 11% | 34/64 | 4 | +1.53% | +3.12% |
| 2025 | 有闸门 | 2.62 | +361 元 | 5/55 | 0 | 23% | 38/59 | 2 | +1.76% | +2.80% |

三条读数：

1. **一字板不是主要问题**（那是 universe 60 的小样本读数）：无论买卖，真正封死（`high==low`）的成交只有个位数，
   加不加「一字不成交」约束对 payoff 基本没影响。**扩到 universe 250 之后这条要修正**：`box_oscillate`
   有闸门臂里 115/1216（9.5%）的买入成交在封死的一字板上（见下节），那批是真的买不到；
   `pattern_reversal` 两个臂都是一字买 0。
2. **跌停日卖掉的那几笔占亏损金额的 7%~24%**（2025 两臂都在 21~23%）。止损是把 payoff 推到
   2.0 的机制，而它砍掉的深亏里每五块钱有一块钱那天出不了货——这部分是假设出来的截断。
3. **最大的一笔假设是进场价**：`box_oscillate` 有 **21%~64% 的买入成交在那天收在涨停线上**
   （箱体突破判据本来就是买强势），而这些票次晨开盘平均要多付 **+0.27%~+1.76%**，
   涨停单那批 **+1.70%~+3.58%**。按每笔 15000 元仓位折算成钱再从单笔期望里扣：

| 时代 | 臂 | 期望(回测口径) | 扣次晨差后 |
|---|---|---|---|
| 2019 | 无闸门 | −232 | **−273** |
| 2022 | 无闸门 | +67 | **−95** |
| 2025 | 无闸门 | +199 | **+99** |
| 2019 | 有闸门 | −155 | **−342** |
| 2022 | 有闸门 | +278 | **+48** |
| 2025 | 有闸门 | +361 | **+97** |

六格只剩三格为正，最好的一格 +99 元/笔（约 +0.66%/笔，还没扣滑点）。更要紧的是
**剔除层那 +161 元的"改进"（2025：199→361）几乎正好等于它多付的次晨差**（100→264 元），
扣完之后两臂都是 +97~+99 元，没有差别；剔除层真正稳的收益是回撤（2019 那格 −27.9%→−9.6%，
2025 −10.5%→−8.3%），不是期望。`pattern_reversal` 是这组里唯一进场 100% 可执行的策略
（抄底点不会收在涨停线上，三时代 `买涨停 0/xx`），次晨差 −0.75%~+0.55%，代价是笔数太少（9~31 笔）。
（这批是 universe 60 的读数，只用于说明机制；样本扩到 250 只后 `box_oscillate` 的扣后期望是**六格全负**，
见下面「把撮合假设叠到剔除层上」那一节，别按这节的"三格为正"下结论。）

**所以读任何回测收益前先问一句撮合假设**：这张表不改任何生产行为（`--audit` 只读），
但它把「回测赢比达到 2.0」和「股票盈利」这两件事拆开了——赢比是出场参数的性质，
期望是不是真的为正取决于进场价能不能拿到。要把撮合改成「次一交易日开盘」是引擎级改动，
会影响 8010 上所有历史回测的可比性，未做，需要单独决策。

### 装上样本外剔除层重跑三轴：8 组配置 0 组同时过线，而两件达标的事落在不同组上

`scripts/backtest_exit_scan.py --reject data/models/risk_oof.parquet`：买入前先查样本外（walk-forward 拼接、
无前视）风险分，低于阈值的那笔直接不买。2 判据 × 2 组出场 × 2 臂（有/无剔除）× 3 时代 × 2 抽样 = 48 格，
universe 250。

**读这张表前必须先知道的池子问题**：这份 OOF 打分只覆盖 **1710/6631** 只票（现役模型按文件序 stride 抽样
训练），所以两臂都只能在这 1710 只里抽。这张表能读的是「**同池同参、只差一道剔除**」的因果对比，
绝对值不能和上面那张 84 格全市场表混比。

| 策略 | 出场 | 臂 | payoff | 单笔期望(元) | 超额均值 | 平均持仓占比 | 同时达标 |
|---|---|---|---|---|---|---|---|
| `box_oscillate` | 5/15/20 | 无剔除 | 1.94~2.56 | −208~+311 | −3.63% | 58~72% | 否 |
| `box_oscillate` | 5/15/20 | **有剔除** | **2.08~2.64** | −87~+260 | +3.17% | 45~62% | 否（2/6 格期望为负） |
| `box_oscillate` | 4/10/15 | 无剔除 | 1.63~1.93 | −96~+99 | −7.78% | 56~63% | 否 |
| `box_oscillate` | 4/10/15 | **有剔除** | 1.85~1.93 | **+42~+330** | +18.17% | 41~51% | 否（赢比差 0.07~0.15） |
| `pattern_reversal` | 5/15/20 | 无剔除 | 1.97~2.62 | −252~+609 | −5.89% | 22~36% | 否 |
| `pattern_reversal` | 5/15/20 | **有剔除** | 1.86~2.29 | **+28~+617** | −2.56% | 20~35% | 否（3/6 格赢比<2.0） |
| `pattern_reversal` | 4/10/15 | 无剔除 | 1.82~2.31 | −135~+408 | −3.61% | 18~32% | 否 |
| `pattern_reversal` | 4/10/15 | 有剔除 | 1.68~2.34 | −55~+263 | −4.71% | 16~29% | 否 |

四条读数：

1. **「赢比 2.0」这个字面目标能达成**：`box_oscillate` 有剔除 + 止损5%/止盈15%/20 日，六格 payoff
   2.08~2.64，全过 2.0（无剔除臂有一格 1.94）。但同一组六格里 **2 格单笔期望为负**（2019+0 −1.9 元、
   2022+0 −87 元）——过线了，账户不赚钱。
2. **反过来做到「六格期望全正」的是另外两组**：`box_oscillate` 有剔除 + 4/10/15（+42~+330 元）和
   `pattern_reversal` 有剔除 + 5/15/20（+28~+617 元），代价是 payoff 只有 1.85~1.93 / 1.86~2.29。
   **这两件事在 48 格里没有一次同时出现**，所以达标口径下 8 组全灭（脚本汇总行 8 行都是 `payoff× pf×`）。
3. **剔除层的方向是一致的、但不落在期望上**：四组有/无对比里三组的期望区间整体上移，回撤改善更硬——
   `box_oscillate` 4/10/15 六格回撤 −26.8/−27.2/−23.7/−27.0/−14.1/−19.9% → **−25.8/−23.4/−11.5/−11.2/−8.7/−12.8%**，
   六个格全收。可它同时把平均持仓占比从 56~63% 压到 41~51%，**超额里有一部分只是"少在场"**
   （2025+0 那格超额 +71.3% 就是在 41% 仓位上拿到的），所以判"挑得准不准"只能看 payoff 和单笔期望；
   再叠上节的撮合审计，`box` 那 +161 元期望改进几乎正好等于它多付的次晨差，扣完两臂持平。
   **结论：剔除层是风控件，不是收益件。**
4. 最可辩护的一格是 `pattern_reversal` 有剔除 + 5/15/20：六格期望全正、回撤 −5%~−19%、
   且它是这轮里唯一进场 100% 可执行的策略（`买涨停 0/xx`，扣次晨差不会变差）。
   弱点也写在表上：每格只有 33~58 笔，样本薄到不能当"稳定"卖。

**所以目标 ①（盈利）和 ④（回测赢比 2.0）在当前买入腿上不能同时交付**，而且缺的那 0.1~0.15 赢比
不是调参调出来的：三条买入判据的日等权超额是 +0.10pp / −1.08pp / −0.32pp（下一节），
没有一个入场信号能同时给出「高赔率 + 正期望」。要真的推动它只有两条路——
把撮合假设改掉（否则这张表的期望还要再打折），以及给买入腿换信息源
（涨停分时质量/板块扩散是还没测完的方向，见 task #53）。

### 把撮合假设叠到剔除层上：box 的正期望 12 格全灭，pattern 的 12 格全活

上一节那张表用的还是「当天收盘就能成交」的假设。这次把两者合起来跑一遍：
`--reject … --audit --exits 0.04:0.10:15 --universe 250 --offsets 0,137`（2 判据 × 2 臂 × 3 时代 × 2 抽样 = 24 格）。
「扣后期望」= 回测单笔期望 − 全部买入成交按次晨开盘价要多付的钱（逐笔 `次日开盘/成交价−1` × 金额，再除以笔数）。

| 策略 | 臂 | payoff | 期望(回测口径) | 买在涨停日 | 其中一字 | 次晨开盘差 | **扣后期望** | 扣后为正 |
|---|---|---|---|---|---|---|---|---|
| `box_oscillate` | 无剔除 | 1.63~1.93 | −96~+99 | 614/1389 (44%) | 80 | +0.27%~+1.14% | **−172~−27** | **0/6** |
| `box_oscillate` | 有剔除 | 1.85~1.93 | +42~+330 | 910/1216 (75%) | 115 | +1.34%~+2.73% | **−172~−48** | **0/6** |
| `pattern_reversal` | 无剔除 | 1.82~2.31 | −135~+408 | 0/362 (0%) | 0 | −1.16%~−0.05% | **+38~+435** | **6/6** |
| `pattern_reversal` | 有剔除 | 1.68~2.34 | −55~+263 | 0/322 (0%) | 0 | −0.60%~+0.07% | **+31~+341** | **6/6** |

三条读数，最后一条会改变结论的方向：

1. **`box_oscillate` 的"正期望"整列是撮合假设造出来的**。它 44%（无剔除）/75%（有剔除）的买入成交
   收在涨停线上，其中 80/115 笔是一字封死——按次晨开盘补上这一段之后，**六个格全部转负**，
   包括上一节最漂亮的那格（2025+0：+329.5 → **−47.8**）。剔除层把涨停买入占比从 44% 推到 75%，
   所以它"提高期望"的方向和"提高不可执行度"的方向是同一个方向。
2. **`pattern_reversal` 恰好相反**：三时代 × 两抽样 × 两臂，**进场没有一笔收在涨停线上**（0/362、0/322），
   次晨开盘平均**更低**（−1.16%~+0.07%），所以按真实下单时点算它的期望是往上走的。
   上一节里两格原始期望为负的（2025+137：−135.2 / −54.9）扣完之后**变成 +38.0 / +30.9**。
   十二格里扣后期望全为正（+31~+435 元/笔）。
3. **所以这两条策略的排序在真实撮合下是反的**：按回测原始期望，2019 那格 box(+247.8) > pattern(+212.6)；
   扣掉撮合假设后 box −75.1、pattern +265.1。这不是小修正——它是"这条策略能不能上线"级别的分歧。
   原因是判据性质：`box_oscillate` 是突破追涨（信号日往往就是涨停日），`pattern_reversal` 是抄底
   （信号日次日往往低开），而**实盘本来就只能次日下单**——所以扣后那一列比原始那一列更接近用户会经历的数。

仍然要说清的限度：`pattern_reversal` 每格只有 41~87 笔、扣后最好格 +435 元/笔靠 2019 普涨年那批撑着，
最近的 2025 两格是 +158 与 +31；它的超额相对同样本买入持有在 2019 是 **−27%~−12%**（普涨年跑不赢），
只有 2022/2025 的四格为正（+6%~+14%）。**赢比那一栏仍然没过**：扣后为正的这 12 格里 payoff 只有
4 格 ≥2.0（1.68~2.34），所以目标 ④ 与目标 ① 至今没有在同一个配置上同时成立。

### 第三个抽样平移把上面那行"12 格全正"打回去了：正期望随股票抽样翻号，集中在最近的时代

上面只用了两个抽样（+0 / +137）。补跑第三个（+274，同一 250 只池、等距轮转，`--offsets 274`），
并把现役出场参数（止损 5% / 无止盈 / 持有 10 日）那组也一起跑完。
下表是 `pattern_reversal` 的**扣后单笔期望**（元/笔，扣掉次晨开盘补的那段）：

| 出场参数 | 臂 | 2019（+0 / +137 / +274） | 2022（+0 / +137 / +274） | 2025（+0 / +137 / +274） |
|---|---|---|---|---|
| 4%/10%/15 日 | 无剔除 | +435 / +407 / +182 | +96 / +111 / +53 | +105 / +38 / **−248** |
| 4%/10%/15 日 | 有剔除 | +265 / +341 / +158 | +134 / +118 / +119 | +158 / +31 / **−180** |
| 现役 5%/无止盈/10 日 | 无剔除 | +962 / +618 / — | +157 / +127 / — | +197 / **−284** / — |
| 现役 5%/无止盈/10 日 | 有剔除 | +482 / +845 / — | +213 / +105 / — | +242 / **−33** / — |

三点结论：

1. **2025 这个最近时代，符号随股票抽样翻号**：同一判据、同一出场参数，换一批等距抽样的股票，
   两臂都从 +31/+158 掉到 −180/−248。上一节那张"12/12 全为正"是两格抽样的产物，
   **不能当结论用**。三抽样 × 三时代 × 两臂 = 18 格里 16 格为正，两格负的都落在 2025。
2. **出场参数确实是杠杆，但不是稳定的那种**：现役 5%/无止盈/10 日在 2019 那两格给到扣后
   +962/+845（payoff 2.65/2.52，是本轮所有配置里最接近"又赢比 2.0 又赚钱"的格子），
   可它在 2025 的 +137 抽样上两臂都翻负（−284/−33，回撤 −36.6%/−24.4%），比 4%/10%/15 日更早崩。
   换句话说，"哪组出场参数最好"取决于这批抽到的股票遇到的是哪个时代。
3. 这和判据后验那节 `sign_stable=False`（日等权超额 +0.10pp，9 格区间 −2.07~+1.76）是同一件事的
   两种算法：**买入腿没有稳定 α，出场参数只是在决定"把亏损截在哪一天"，所以它换时代就翻**。

**对四个目标的当前答案**（截至本轮，全部是三轴 + 扣撮合口径下的实测，不是推断）：
② 有依据且**现在看得见**——每条命中挂 `extra.signal_posterior`，摘要落进 `select_results`
的 `posterior_verdict/excess_pp/n` 三列，选股结果页每条下方显示 `依据 · <verdict> …`
（verdict 合并了「样本够」与「三轴同号」，表没建时显示「未标定」而不是消失）；
③ 有依据——每条诊断挂台账日等权后验，数字每天随调度落盘；
④ **口径已改成四项同时**（赔率 ≥2.0 **且** 胜率 ≥50% **且** 利润因子 ≥2.0 **且** 期望 >0），
三个扫描脚本的判据与回测结果页都按这个口径判；**四项没有任何一个配置九格全过**；
① **首次拿到**：因子分位闸门 `adx14≥90`（universe 1500）九格单笔期望全正 +70~+1228 元/笔，
且相对对照的配对改进九格同号（d胜率 +3.0~+35.0pp、d期望 +35~+988 元）；详见《因子条件筛选》一节。
限制是它只解决 ①：④ 的缺口被量化成「胜率最低 40.5%（差 9.5pp）、赔率最低 1.44（差 0.56）」，
而且收紧闸门能抬胜率、笔数却掉到 30 笔下限以下——**这是一条可以继续压的曲线，不是死路**。
其余六条路线（出场参数 0/14、剔除层 0/8、下单纪律 0/40、择市闸门 0/63、热度闸门 0/5、
因子筛选九格同号 1/73）仍然一条都没拿到 ①；其中择市闸门是唯一**把整族数字朝对的方向推**的
（强势臂 8/9 对弱势臂 5/9，均值期望 +237 对 +107）。
但择市闸门推不进「每格为正」，而且两种状态量（涨跌幅均值 / 上涨家数占比）对同一格能给出相反方向，
说明剩下的差距不是阈值，是**日线特征里没有那个信息**——要往前走只能换信息源（分时、板块扩散、资金流结构）。

**这组数直接把「要不要把撮合改成次日开盘」从工程洁癖变成了决策题**：现在的 8010 回测对
`box_oscillate` 系统性乐观（正期望整列翻负），对 `pattern_reversal` 系统性保守。
改成次日开盘撮合是引擎级改动、会让历史回测不可比，**未做，等用户拍**；拍之前所有对外报的
单笔期望都应按扣后口径读，别按原始口径读。

**诊断侧的同一现象**（判定台账 1090 万个判定点，`heat_bin=全部`）。点级与日等权并排放，是因为两者符号相反：

| 判成 | 样本 | 后 10 日均值(点级) | 日等权均值 | 中位 | p5 |
|---|---|---|---|---|---|
| sell | 118.0 万 | +0.87% | **−0.14%** | +0.30% | −16.2% |
| add | 75.5 万 | +0.49% | **−0.30%** | −0.16% | −16.2% |
| hold | 896.9 万 | +0.09% | +0.54% | −0.24% | −12.7% |

只看「点级」那一列会得出「sell 点事后平均是涨的，所以这条卖线是反向的」——**这个结论是错的**，
它是恐慌日扎堆造出来的：sell 触发点集中在全体都反弹的那几天，绝对均值被那几天的水位抬起来了。
换成**日等权 + 同日配对差分**（同一天里该状态点均值减同日 hold 点均值，再对交易日等权平均，
大盘水位自动抵消）：

| 差分（vs 同日 hold） | 日等权 | t | 正日占比 | 配对交易日 | 三段时代 |
|---|---|---|---|---|---|
| sell − hold | **−0.472pp** | −6.7 | 41.9% | 3,049 | −0.78 / −0.18 / −0.10 |
| add − hold | **−0.682pp** | −9.8 | 36.3% | 3,044 | −0.68 / −0.54 / −0.83 |

这组数现在由 `scripts/build_judgement_ledger.py` 每天随台账一起落到
`data/judgement_ledger/posterior.parquet`（列 `daymean_fwd10_pct / vs_hold_daymean_pp / vs_hold_t /
vs_hold_pos_rate / vs_hold_days / vs_hold_era_pp / vs_hold_era_stable`），诊断文案直接读它，不再靠手算。
口径敏感性要说清：上一轮手算的 −0.454 / −0.746pp 只取「三状态齐备」的 2,930 天，sell 的最早一段时代
因此是 +1.10pp（反号）；台账里的配对只要求该状态与 hold 同日有值（3,049 天），三段就都是负的。
**差分本身稳，"最早那段时代反号"这件事取决于配对日集怎么选**——所以别拿单段时代做决策依据。

按热度分箱的 `add − hold`（同一箱内配对，单调）：

| 热度箱 | add − hold | t | 配对日 |
|---|---|---|---|
| 无板块归属 | **−1.208pp** | −12.6 | 2,699 |
| 0-44 | −1.064pp | −7.1 | 1,877 |
| 44-70 | −0.797pp | −8.6 | 2,432 |
| 70-90 | −0.676pp | −7.5 | 2,600 |
| 90-100 | −0.373pp | −3.5 | 2,481 |

板块越冷、这条 add 判据越反指。热度列本身有已知前视（成分表用最新一天回推历史），所以这一张
只能当**门控**用（越冷越不加），不能当证据用。

读法：

- **sell 线方向是对的但很弱**——同日比 hold 点差 0.47pp/10 日，量级就在换手成本(0.2~0.4pp)附近；
  它的尾部还更厚（p5 −16.2% vs −12.7%），所以它是波动率/尾部判据，不是方向判据。
  不足以支持「改写卖出线能多赚钱」，也不足以支持上一轮 `--ab-rules` 里那个 +0.87pp 的"反向"说法
  ——**那个说法本身是点级口径的产物，本轮已按日等权更正**。
- **add 线是稳定反指**：判成「趋势向上且未过热，可考虑小幅加仓」的点，同一天比 hold 点 10 日后还差
  0.68pp，三段时代全负、t=−9.8，五个热度箱无一例外。这是本表里唯一又大又稳的差分，含义很直接：
  **这条 add 判据现在是在给"当天相对更弱"的票加冕**，收口它的正确动作是加门控或停用，而不是修参数。
- 两条链对同一类「跌狠了会弹」的点下的结论仍然相反：选股侧 `pattern_reversal` 日等权超额 +0.10pp≈0，
  诊断侧 sell 线同日差分 −0.47pp。翻转现役规则会改变实盘行为，需要单独决策，不在本次读数里顺手改。

## 入场纪律实验：挂低买不到就放弃，能不能造出正期望（结论：不能，而且方向反的）

前三节把「卖出参数凑不出赢比 2.0 + 正期望」和「撮合假设本身送钱」都测过了，剩一个自然的下一问：
**买入腿能不能靠下单纪律救回来**。实盘不可能按信号日收盘价成交（信号是收盘后算的），
而审计显示次晨开盘平均比回测价贵一截，所以最直觉的办法是「别追——挂在昨收下方 δ 折价，
触不到就放弃」。`scripts/backtest_entry_discipline.py` 逐笔重演这个规则：

- 锚点是**信号日收盘**（昨收），不是次日开盘：挂单必须在看不到明天的时候决定挂在哪，
  锚在次开就是前视。
- 只在次一交易日成交（T+1 买入当天不许卖，卖出仍按收盘撮合，与回测同口径）。
- 次日最低价没触到挂单价 → 这笔直接不做，不计入分母（「买不到就放弃」的真实含义）。
- 开盘已低于挂单价 → 按开盘成交（跳空向下只会更好）。持有/止损/止盈/到期规则不变，
  但**基准价换成新成交价**——出场跟着真实成本走，这是这个模拟与回测唯一的口径差。
- 费用按券商模型：佣金 0.00025（最低 5 元）+ 卖出印花税 0.0005，整百股取整。
  注意 `pnl` 只扣卖出费，与回测引擎一致，买入佣金在总成本里已计入买价。

40 个组合（2 策略 × 2 出场 × 2 闸门 × 5 折价），每个都是时代 3 × 抽样 3 合并样本，
`达标 = payoff≥2.0 且 跨三抽样每格单笔期望>0`：**0/40**。

| 策略 / 闸门 / 出场 | 折价 0% | 0.5% | 1% | 2% | 3% |
|---|---|---|---|---|---|
| box / 无 / 5-0-10 | −87.7 | −96.2 | −92.8 | −91.9 | −148.3 |
| box / 有 / 5-0-10 | −141.7 | −132.0 | −131.3 | −153.6 | −151.7 |
| box / 无 / 4-10-15 | −81.1 | −86.5 | −77.6 | −86.5 | −127.3 |
| box / 有 / 4-10-15 | −124.7 | −112.7 | −76.4 | −86.8 | −109.3 |
| pattern / 无 / 5-0-10 | **+134.4** | +106.9 | +48.4 | +7.7 | −172.1 |
| pattern / 有 / 5-0-10 | **+200.1** | +191.8 | +128.0 | +100.5 | +20.4 |
| pattern / 无 / 4-10-15 | +69.1 | +36.9 | −10.2 | −65.0 | −132.9 |
| pattern / 有 / 4-10-15 | +75.1 | +76.4 | +73.6 | +36.5 | +22.6 |

（单位：单笔期望 元；box 全部 20 格为负，pattern 只有带闸门的两行跨全部折价为正。）

四条读数：

1. **折价越深，期望越差，且是单调的**。pattern 无闸门 5-0-10 从 +134 元一路掉到 −172 元，
   box 在任何折价下都没有一格为正。挂低价不是「省成本」，是**逆向选择**：
   能填上的票恰恰是第二天继续跌的票，填不上的那批才是原本会赚钱的。
   这解释了为什么上一节「次晨差价」对 pattern 是保守估计——它的利润确实来自「越贵越买」的那批。
2. **payoff 依然凑得到 2.0，代价还是期望**。box 无闸门 5-0-10 在折价 2%/3% 时 payoff 到 2.05/2.07，
   对应的期望是 −92/−148 元。这与退出扫描那一节的结论同形：赢比和赚钱这两件事在这族判据上
   不是同一个旋钮，换下单口径也换不出来。
3. **闸门的作用这次看得最干净**：pattern 带闸门的两行，期望曲线随折价几乎不塌（+200→+20、+75→+23），
   无闸门的同行塌到负。剔除层买的仍然是**稳健性/回撤**，不是更高的均值——
   与前面「闸门把 2019 那格回撤从 −27.9% 收到 −9.6%」是同一件事的两种表达。
4. **成交数必须和期望一起看**：折价 3% 时 pattern 只剩 142/163 笔（比 0% 少 63%~70%），
   「剩下的那批才赚钱」这种说法在这个表里不成立；只看期望确实可以把任何策略做漂亮。

限度：这是逐笔模拟，不管资金与仓位争用（同一批里多天并发时会买不下），
也不改引擎——回测页面与历史 8010 任务的数字仍然是「信号日收盘成交」口径。

## 买入腿择市闸门实验：只在「好日子」下单，能不能造出跨三抽样为正（结论：抬得动均值，抬不进为正）

前面三节把出场参数、样本外剔除层、下单纪律都测到了尽头，全部失败在同一个地方：买入腿本身
没有跨抽样稳定的 α。这一节换最后一个还没测的自由度——**什么时候不买**。
脚本 `scripts/backtest_breadth_gate.py`（复用 `backtest_exit_scan.py` 的抽样与撮合，口径不另立）：

- 状态量两种：`--metric mean`（当天全样本涨跌幅均值，%）、`--metric advance`（当天上涨家数占比，%）。
- 闸门与 `RiskGate` 同接口（`date, code → 放行`），挂在信号与下单之间；**只用当天收盘后就算得出的数**，
  不看任何未来行；当天样本行数不足（停牌日）按放行。
- 方向两个都测：`强势日≥阈值` 与 `弱势日≤阈值` 各跑一遍。只报顺趋势或只报逆势都是研究者偏差。
- 每格判「正」还带 30 笔下限：`all_pos` 里就要求 `min(n) >= --min-trades`，否则剔到只剩 5 笔的配置
  会伪装成「全格为正」。

跑法（两条并行，各约 25 分钟，`pattern_reversal` + 现役出场 4%/10%/15 日，3 时代 × 3 抽样）：

```bash
python3 scripts/backtest_breadth_gate.py --strategies pattern_reversal --exits 0.04:0.10:15 \
  --thresholds=-0.3,0,0.3 --metric mean          # logs/breadth_gate_mean_20261013.log
python3 scripts/backtest_breadth_gate.py --strategies pattern_reversal --exits 0.04:0.10:15 \
  --thresholds=20,40,60 --metric advance         # logs/breadth_gate_advance_20261013.log
```

对照（无门）九格，扣费后单笔期望（元）：

| 时代 | off0 | off137 | off274 |
|---|---|---|---|
| 2019 | +469 | +196 | +434 |
| 2022 | **−6** | +321 | +183 |
| 2025 | **−47** | +37 | +0 |

闸门后的汇总（最好的几臂）：

| 臂 | 期望为正 | payoff 最低 | 笔数最低 | 期望区间 |
|---|---|---|---|---|
| mean 强势≥+0.3 | 8/9 | 1.76 | 23 | −36 ~ +237 |
| advance 强势≥+60 | 8/9 | 1.89 | 23 | −3 ~ +295 |
| mean 对照 | 7/9 | 1.68 | 31 | −47 ~ +176 |
| advance 弱势≤+20 | 9/9（**笔数只有 5**） | 1.18 | 5 | +176 ~ +418 |
| mean 弱势≤+0.3 | 5/9 | 1.51 | 22 | −242 ~ +107 |

四条读数：

1. **择市的方向是真的，但只到「抬均值」为止**。同一批信号，mean 口径下强势臂 8/9、弱势臂 5/9，
   均值期望 +237 对 +107；2025/off0 那格从 −47 抬到 +223，回撤从 −18.9% 收到 −9.5%。
   这是本轮第一次有杠杆把整族数字朝对的方向推动——但它没有任何一臂做到 9/9，
   连 `all_pos`（含 30 笔下限）都没摸到。
2. **顽固格 2025/off137 在全部五个强势臂下都是负的**（−2.9 ~ −42.7），而它对照是 **+36.6**：
   闸门把这格从正做成负。同理 2022/off0 在 mean≥+0.3 是 +37、在 mean≥0 是 −72、
   在 advance≥60 是 +323、在 advance≥40 是 −73。**哪一天算好日子，取决于抽到哪批股票和哪个阈值**，
   这不是可复用的规则，是过拟合的形状。
3. **唯一「9/9 全为正」的臂是退化配置**：每格只剩 5–9 笔、payoff 1.18。脚本的 30 笔下限把它判成
   不通过；如果去掉这个下限，这张表就会给出「找到正期望配置」的假结论。
   **笔数必须和期望同排**，这一条比数字本身重要。
4. **④ 也没达标**：最好的臂 payoff 最低 1.76/1.89，仍 <2.0；payoff 达标的格子（2.4+）依旧只出现在
   剔得最狠、笔数最少的臂上。与前两节同形。

限度：这些数字是**原始收盘撮合**口径（未扣上一节量出的撮合乐观偏差），所以读**相对变化**而不是绝对值；
状态量是全样本等权，换一批股票它的分布本身也会变，阈值不可能跨样本复用；
两种状态量之间能互相打脸，说明这一族判据对「市场状态」的响应还没被识别出来，只是被噪声推着走。

**结论并入主线**：出场参数（0/14）、样本外剔除层（0/8）、下单纪律（0/40）、择市闸门（0/63 格全正）
四条独立路线，都卡在同一处——买入腿的日等权超额实测 ≈0 且换抽样翻号。
**目标 ①+④ 同时成立，缺的不是参数，是判据本身**；要再往下走只能换信息源
（分钟/分时、板块扩散、资金流结构），而不是在同一批日线特征上继续切阈值。

> **后话（因子路线跑完之后回头看这段）**：上面这条"缺的是判据"要修正一半。
> 缺的不是**买点判据**——`pattern_reversal` 命中之后，`adx14` 当日横截面前 10% 的一道闸门
> 就把单笔期望做成了九格全正（见《结论卡：`adx14` 高分位闸门》）。也就是说
> **这一族判据不是没有 α，是 α 不在"买不买"上，而在"这批里面留哪一只"上**：
> 闸门臂的绝对超额仍然是正的，但仓位被压到 14%~20%，所以账户层面的收益没有跟着上去。
> 真正的结论因此是两句，而不是原来那句：①**判据能挑**（在候选池内），
> ②**现有判据的命中率与仓位效率撑不起一个账户**。换信息源那条路仍然该走，
> 但要解决的是"怎么让好信号多到能填满仓位"，不是"再找一条更好的判据"。

## 目标 ④ 改成硬口径：胜率 ≥50% 且 利润因子 ≥2.0，与赔率、期望同排判

原来 ④ 只写「回测赢比 2.0」，而"赢比"在这套代码里有两个口径（`metrics.py` 里分开的两个属性）：
`payoff_ratio` 是**单笔赔率**（均盈/均亏），`profit_factor` 是**总盈/总亏**。
两者关系是 `profit_factor = payoff × 胜率/(1−胜率)`——胜率 33% 时 payoff 2.0 只对应 profit_factor 1.0。
只报 payoff 会把「小亏很多次 + 大赚一次」的截断结果当成好策略，所以从这一轮起，
**④ 的达标口径是四项同时满足**：`单笔期望 > 0` 且 `赔率 ≥ 2.0` 且 `胜率 ≥ 50%` 且 `利润因子 ≥ 2.0`。

改动落在四处，判据是同一份（目标值都是命令行参数，默认即上表）：

| 位置 | 改动 |
|---|---|
| `scripts/backtest_exit_scan.py` | 汇总行加「胜率」区间与「胜率✓/×」，并打「①+④全格达标✓」；新增 `--wr-target` |
| `scripts/backtest_breadth_gate.py` | 逐格打印加胜率与利润因子；`pass` 改成四项同时过（原来只有 payoff × 期望）；新增 `--wr-target/--pf-target` |
| `scripts/backtest_entry_discipline.py` | `summarize` 补 `pf` 字段（原来只有赔率），汇总按四项判；新增 `--wr-target/--pf-target` |
| 回测结果页 `backtest.js` | 指标卡下面加一行**逐项**达标判定（期望/赔率/胜率/利润因子/成交笔数各一个 ✓✗），四项全过才写「①+④ 全项达标」 |

页面上逐项列出来而不是只给一个总判据，是因为「哪一项没过」本身就是信息：
本项目已实测出现过赔率 2.08~2.64 六格全过、胜率却在 41%~62% 之间来回摆的配置，
只印一个"达标/不达标"会把这件事盖住。

## 板块热度闸门（择市闸门的第二条状态量）：第五次卡在同一处

`--metric heat` 用的是**个股级**状态量：所属板块近 3 日等权涨幅的**当日横截面百分位**
（判定台账落盘，每天重新归一，所以阈值跨时代可比；同一天里有的票过闸有的不过，
剔掉的是"冷板块里的信号"而不是"冷日子里的所有信号"）。已知的偏置写在脚本注释里：
成分股名单是台账落盘时的当前快照，历史上回溯贴标签必然带成分漂移。

```bash
python3 scripts/backtest_breadth_gate.py --strategies pattern_reversal --exits 0.04:0.10:15 \
  --metric heat --thresholds 60,90                     # logs/breadth_gate_heat_20261013.log
```

| 臂 | 期望为正 | payoff 最低 | 笔数最低 | 期望区间 |
|---|---|---|---|---|
| 对照(无门) | 7/9 | 1.68 | 31 | −47 ~ +176 |
| 冷板块 ≤90 | **7/9** | **1.77** | 28 | −111 ~ +167 |
| 冷板块 ≤60 | 6/9 | 1.66 | 19 | −97 ~ +227 |
| 热板块 ≥60 | 6/9 | 1.51 | 29 | −232 ~ +101 |
| 热板块 ≥90 | 5/9 | 1.54 | 19 | −167 ~ +113 |

三条读数：

1. **最好的臂和对照打平**：冷板块 ≤90 也是 7/9、payoff 最低 1.77，负格同样集中在
   2025/0 与 2025/137。热板块那一侧明确更差（5/9、6/9，期望区间整体下移）。
   「在冷板块里买超跌反弹」这个直觉在这份数据里**测不出独立贡献**。
2. **④ 仍然没过**：五臂 payoff 最低 1.51~1.77，没有一臂到 2.0；胜率那一栏按本轮
   新口径一并检查，没有一臂九格都 ≥50%。
3. 这是**同一条路线上的第二个状态量**，不是第二条独立路线：换的是"拿什么描述市场状态"，
   不是"从哪拿信息"。它和 `mean`/`advance` 互相打脸这件事本身就是结论——
   这一族判据对市场状态的响应没被识别出来，只是被噪声推着走。

## 因子条件筛选：第六条路线，也是第一次把 ④ 的四个数直接算出来

前五条路线都在问「这条判据要不要开/关」「什么日子才买」。这一条换机制：
**判据命中之后，这只票当时处在什么状态，决定了后面 10 日/持有期内的横截面结果**——
即条件期望 `E[超额 | 特征分位]`，而不是 `E[超额]`。
脚本 `scripts/screen_signal_factors.py`，16 个候选特征（动量、位置、量能、影线、
趋势强度、波动、流动性）× 5 个当日横截面分位箱 = 80 个候选。

三处口径是照着既有验证方法抄的，不是新立的：

- **同日基线**：超额 = 该点 T+10 收益 − 同一天样本内全部股票的 T+10 均值（不减大盘水位）。
- **日等权**：先按天取当天命中的均值，再对天求平均（点级会被扎堆的恐慌日主导，本项目已多次翻号）。
- **分箱用当日横截面百分位**：绝对水位跨时代不可比，百分位每天重新归一，
  所以同一个箱在 2019/2022/2025 说的是同一件事；代价是换 offset 就换了一批股票，
  **offset 轴因此成为这一条路线的主要证伪轴**（脚本里的「留一验证」就是它：
  三个 offset 轮流留一，用另两个当"选中时的估计"，看留出那个的符号是否一致）。

脚本的两套输出口径：

1. **条件均值口径**（`screen_<策略>.parquet`）：`E[T+10 超额 | 分位箱]` + 日级 t。
2. **出场口径**（`screen_exit_<策略>.parquet`）：按**收盘价**把机械出场规则一字不差地重演一遍，
   直接给出这一箱的胜率 / 赔率 / 利润因子 / 单笔期望，以及与"同格全部命中"的差值。
   这条能算准是因为引擎的止损/止盈/持有到期全是按收盘价判（`KernelStrategy.evaluate_holdings`
   拿的是 `prices[code]`），给定入场收盘价与后面 H 天收盘序列，结果就定了——
   它不受仓位上限与 `max_positions` 排队影响，所以能把"选股准不准"与"仓位管理好不好"分开。

跑法（`pattern_reversal`，universe 250，3 时代 × 3 抽样，约 1.5 分钟）：

```bash
python3 scripts/screen_signal_factors.py --strategies pattern_reversal --universe 250 --top 18
```

**条件均值口径：73 个候选，九格同号 0 个。** 多重比较逐道筛子的收敛过程：

| 筛子 | 剩余候选 |
|---|---|
| 九格都有样本（单格 ≥30 命中） | 10 |
| 九格符号同号 | **0** |
| 合并 \|超额\| ≥0.30pp | 0 |
| 合并 \|t\| ≥2.0 | 0 |
| 留一验证 ≥2/3 | 0 |

最强的几个（`adx14[80,100)` +2.81pp、`pct[80,100)` +1.12pp、`ma5_gap[80,100)` +0.69pp）
都是"高动能/当日强势/站上短均线"那一侧，看着有经济含义，但每个都至少有一格翻号，
合并 t 只有 0.02~0.27。**方向对、幅度小、且换样本就翻——和前五条路线同形。**

**出场口径：20 个九格样本足够的候选里，九格同号只有 1 个**（`pct[80,100)`，d期望 +1.35pp、
留一 3/3、覆盖 34%、合并 t 仅 0.14）。同格整体基准（现役 5%/无止盈/10 日出场）
是**胜率 41.2% / 赔率 3.36 / 单笔期望 +0.88pp**：赔率早就过了 2，卡住的是胜率——
这正是把 ④ 拆成四个数之后才看得见的那一半。

### 筛选只负责产候选，判定归回测：闸门臂 9 格配对回测（universe 800）

`scripts/backtest_factor_gate.py`：候选规则做成个股级闸门（与 `HeatGate` 同一接口，
直接挂 `KernelStrategy.reject_gate`），在**同一格、同一批股票、同一个引擎**里与对照臂对照，
唯一差别是"这一天这批命中里买不买某一部分"。这是配对对比，比筛选层的非配对对比灵敏得多。

universe 必须够大：250 只时闸门臂每格只剩 11~23 笔，**低于 30 笔下限，读数作废**——
这不是闸门的问题，是筛选出的条件本身就只覆盖 20%~35% 的命中。先补到 800 只再判。

```bash
python3 scripts/backtest_factor_gate.py --universe 800 --offsets 0,137,274 \
  --exits 0.05:0.00:10 --rules adx14:4,pct:4,liq:4 --mode single   # logs/factor_gate_u800_20261013.log
```

| 臂 | 达标格 | 胜率 | 赔率 | 利润因子 | 单笔期望 | 笔数 |
|---|---|---|---|---|---|---|
| 对照(无门) | 1/9 | 32.3%~54.7% | 1.08~2.27 | 0.66~2.39 | −177~+517 | 64~96 |
| `adx14[80,100)` | 1/9 | 36.2%~71.4% | 1.44~3.07 | 1.20~4.48 | **+104~+935（全正）** | 33~51 |
| `pct[80,100)` | 0/9 | 31.0%~53.6% | 1.26~2.62 | 0.66~1.87 | −190~+354 | 56~87 |
| `liq[80,100)` | 1/9 | 26.5%~52.4% | 1.55~2.24 | 0.76~2.39 | −159~+503 | 41~69 |

配对差值（同格内闸门臂减对照臂）：`adx14[80,100)` 的 d期望 −166~+667、均值 +260，
`pct` −320~+250（均值 −31），`liq` −220~+164（均值 +3）。**三个都不是九格同号**——
闸门"平均更好"，但每一条都至少有一格比对照差。

### 第七步：把闸门收紧，阈值先预注册再读数（universe 800）

收紧闸门是唯一还在动的旋钮，所以**先把协议写下来再看数**：阈值只用 2019/2022 六个格选，
**2025 三格整段留出当验证**；候选阈值 `adx14 分位 ≥85 / ≥90 / ≥95`；出场 5%/无止盈/10 日；
笔数下限 30。

| 闸门 | 达标格 | 胜率 | 赔率 | 单笔期望 | 笔数 | 2025 三格（留出） |
|---|---|---|---|---|---|---|
| `adx14≥85` | 1/9 | 41.9%~71.4% | 1.08~2.87 | −90~+951 | 28~43 | +337 / **−90** / — |
| `adx14≥90` | 0/9 | 37.5%~76.0% | 1.43~4.08 | **+230~+1204（全正）** | 16~37 | +1007 / +867 / +602 |
| `adx14≥95` | 0/9 | 33.3%~100% | 0.95~99 | −7~+1783 | 6~30 | −7 / +196 / +881 |

读法：`≥90` 是唯一在**留出的 2025 三格里也全正**的阈值（而它在选阈值时并没有看 2025）；
`≥95` 退化成每格 6~30 笔，其中一格 11 笔全胜把赔率印成 99——**这种格不能当结论**，
它正是脚本把 30 笔下限写死的原因。`≥90` 的问题在笔数：16~37 笔，六格不到下限。

### 把 universe 补到 1500：① 第一次全格为正，且配对改进九格同号

笔数是唯一的瓶颈（闸门臂每格 28~49 笔，对照 88~110 笔），所以再补一档股票池。
这一格是本项目**六条路线里第一次**让闸门臂的单笔期望在九格里全为正**且**配对改进九格同号：

```bash
python3 scripts/backtest_factor_gate.py --universe 1500 --offsets 0,137,274 \
  --exits 0.05:0.00:10 --rules "adx14:>=90" --mode single   # logs/factor_gate_u1500_20261013.log
```

| 时代 | off0 | off137 | off274 |
|---|---|---|---|
| 2019 | n45 胜51.1% 赔1.89 pf1.98 **+435** | n31 胜83.9% 赔1.94 pf10.06 **+1228** | n39 胜51.3% 赔1.63 pf1.72 **+241** |
| 2022 | n37 胜45.9% 赔1.90 pf1.61 **+226** | n28 胜42.9% 赔2.21 pf1.66 **+298** | n39 胜48.7% 赔2.03 pf1.93 **+311** |
| 2025 | n49 胜44.9% 赔3.22 pf2.63 **+905** | n42 胜40.5% 赔1.73 pf1.18 **+113** | n41 胜43.9% 赔1.44 pf1.13 **+70** |

对照臂同格：胜率 25.5%~48.9%、赔率 1.51~2.56、利润因子 0.73~1.94、期望 **−152~+419（三格为负）**。

| 口径 | 对照 | `adx14≥90` | 差值 |
|---|---|---|---|
| 单笔期望九格全正 | ✗（−152~+419） | **✓（+70~+1228）** | d期望 +35~+988，均 +376，**九格同号✓** |
| 胜率 | 25.5%~48.9% | 40.5%~83.9% | d胜率 +3.0~+35.0pp，均 +13.6pp，**九格同号✓** |
| 赔率 | 1.51~2.56 | 1.44~3.22 | d赔率 −0.64~+0.66，**不同号✗** |
| 利润因子 | 0.73~1.94 | 1.13~10.06 | — |
| 笔数 | 88~110 | 28~49（**8/9 ≥30**） | 仓位 41%~54% → 14%~20% |

四条结论，按可信度从高到低：

1. **①（九格单笔期望全正）第一次拿到了**：`adx14 分位 ≥90` 这一条买入闸门，
   跨 3 时代 × 3 抽样九格全正（+70~+1228 元/笔），而对照臂有三格为负。
   这是前六条路线（出场参数 0/14、剔除层 0/8、下单纪律 0/40、择市闸门 0/63、
   热度闸门 0/5、因子筛选九格同号 1/73）都没有做到的事。
2. **配对差值也是九格同号**：d胜率 +3.0~+35.0pp、d期望 +35~+988 元，每一格都变好。
   这一条比"绝对全正"更难伪造，因为它把时代水位与市场涨跌都抵消掉了。
3. **④ 仍然没过**：胜率最低 40.5%（目标 50%）、赔率最低 1.44（目标 2.0）、
   利润因子最低 1.13（目标 2.0）。**胜率是绑定的那一项**——赔率那一半其实早就够了
   （对照自己就有 2.56），把胜率从 25.5%~48.9% 抬到 40.5%~83.9% 之后，仍然没有一格
   能同时压住赔率不塌。要同时过线，闸门还得再收，收了笔数又不够。
4. **这是"闸门"，不是"新判据"**：`adx14`（趋势强度）是既有列，它没有改变
   `pattern_reversal` 什么时候买，只改了"这批买的时候留哪一只"。所以这条结论的
   适用范围要说准——它是在**已过滤的候选池内**再排序，不是发现新的 α 来源。

必须一起读的三条限度：

- **规则是看着这批数据挑的**。筛选层在 80 个候选里挑出 `adx14[80,100)`，
  阈值 `≥90` 又是照着 2019/2022 选的。能给的检验只有已经给掉的两条：
  三个 offset 的留一验证（3/3）、以及 2025 整段留出（阈值选定时没看过）。
  再加一条弱的：股票池在 800 与 1500 两档是不同的抽样（`_sample` 的 stride 随 n 变），
  同一规则在两档上都给出九格全正。**但"多次筛选后的样本外"这件事本身没法用同一批数据自证。**
- **仓位差混在里面**：闸门把平均仓位从 41%~54% 压到 14%~20%。
  所以总收益那一列不能用来比"挑得准不准"，只能看胜率/赔率/期望——这也是脚本把
  d 期望和仓位并排印出来的原因。
- **撮合仍是收盘价口径**：与前面几节一样，这批数字没有扣次晨开盘差。
  `pattern_reversal` 的买入成交历史上没有一笔收在涨停线上（见《撮合审计》一节），
  所以这一条受撮合假设的影响比 `box_oscillate` 小，但仍然是未扣的数。

### 结论卡：`adx14` 高分位闸门（当前唯一拿到 ① 的配置，尚未上线）

**规则定义（一句话）**：`pattern_reversal` 命中之后，只在**该股当日 `adx14`（趋势强度 ADX14）
位于当日全样本横截面前 10%** 时下单；其余命中不下单。取不到分位（停牌/指标未成形）按放行。
横截面分母是**回测股票池当天有行情的票**，所以换一只池子分位就变——这是它最大的软肋。

**现状**：① 成立、④ 不成立，所以**它现在只是候选，不是现役参数**。

| 口径 | 目标 | 对照 | `adx14≥90` @u1500 | 差 |
|---|---|---|---|---|
| 九格单笔期望全正 | ① | ✗ −152~+419 元 | **✓ +70~+1228 元** | d +35~+988，均 +376，九格同号 |
| 九格胜率 ≥50% | ④ | ✗ 25.5%~48.9% | ✗ 40.5%~83.9% | d +3.0~+35.0pp，均 +13.6pp，九格同号 |
| 九格赔率 ≥2.0 | ④ | ✗ 1.51~2.56 | ✗ 1.44~3.22 | d −0.64~+0.66，**不同号** |
| 九格利润因子 ≥2.0 | ④ | ✗ 0.73~1.94 | ✗ 1.13~10.06 | — |
| 每格笔数 ≥30 | 硬下限 | ✓ 88~110 | ✓ 8/9 格（28~49） | 一格 28 笔，差 2 笔 |

**能复现的三条命令**（顺序即依赖顺序，产物落在 gitignore 的 `data/factor_screen/`）：

```bash
# 1) 产候选：80 个 (特征×箱) 候选，日等权、九格同号 + offset 留一验证（约 1.5 分钟）
python3 scripts/screen_signal_factors.py --strategies pattern_reversal --universe 250 --top 18
# 2) 阈值扫描（协议已预注册：2019+2022 选阈值、2025 留出；≥90 是唯一留出段也全正的）
python3 scripts/backtest_factor_gate.py --universe 800 --offsets 0,137,274 \
  --exits 0.05:0.00:10 --rules "adx14:>=85,adx14:>=90,adx14:>=95" --mode single
# 3) 定档验证：universe 必须够大，否则闸门臂掉到 30 笔下限以下（250 只时只剩 11~23 笔）
python3 scripts/backtest_factor_gate.py --universe 1500 --offsets 0,137,274 \
  --exits 0.05:0.00:10 --rules "adx14:>=90" --mode single
```

日志：`logs/factor_screen_pattern_20261013.log`、`logs/factor_gate_u800_strict_20261013.log`、
`logs/factor_gate_u1500_20261013.log`。

**为什么还不能上线**（按严重程度排）：

1. **④ 没过**——胜率最低差 9.5pp、赔率最低差 0.56，且两者互斥：收紧闸门（≥95）能抬胜率，
   笔数掉到 6~30 笔且有一格 11 笔全胜把赔率印成 99（退化配置）。
2. **横截面分母依赖股票池**：分位是在池内排的，`adx14≥90` 在 1500 只的池里是当日 150 只，
   在实盘全市场 6600 只的池里就是当日 660 只——**同一句话在两处的严格程度不同**。
   上线前必须用全市场分母重测一遍（`--universe` 拉到 6000 以上，或改成分母固定为全市场）。
3. **规则是在同一批数据上挑出来的**：能给的自证只有 offset 留一（3/3）、2025 整段留出、
   800/1500 两档不同抽样复现。"多次筛选后的样本外"没法用同一批数据自证。
4. **它不是新判据，只是闸门**：`adx14` 是既有列，没有改变 `pattern_reversal` 什么时候买，
   只改了"这批买的时候留哪一只"。所以它改善的是**候选池内的排序**，
   不能拿去解释"为什么这只票值得买"。
5. **仓位被压到 14%~20%**（对照 41%~54%）。所以总收益不能用来比"挑得准不准"，
   只能用胜率/赔率/期望；上线时也要考虑是不是要放宽 `max_positions` 把仓位补回去。

**下一步该试什么**（按预期收益排）：① 闸门条件从单特征换成
**`adx14` 高分位 × 判据强度**（`hit.score` 高）的二元组合，用 `--mode all`；
② 把分母固定成全市场重测，确认分位依赖是不是致命的；
③ universe 提到 3000 只以上换笔数，把 ≥95 那条曲线补完整。**不要再单独切 `adx14` 阈值**——
85/90/95 三档已经说明这条曲线的形状是"胜率↑、笔数↓"，继续切只会换个位置停在同样的权衡上。

### 这一轮六条路线合起来的账

| 路线 | 九格全正（①） | 赔率 ≥2.0（④） | 胜率 ≥50%（④） | 备注 |
|---|---|---|---|---|
| 出场参数网格 | 0/14 | 有（`box` 6/6，但同组 2 格期望负） | 无 | 8 组配置 0 组同时过线 |
| 样本外剔除层 | 0/8 | 无 | 无 | 风控件，不是收益件 |
| 入场折价纪律 | 0/40 | 无 | 无 | 方向反的 |
| 择市闸门（涨跌幅/宽度） | 0/63 | 无 | 无 | 唯一把整族均值推对的一条 |
| 板块热度闸门 | 0/5 | 无（最好 1.77） | 无 | 最好一臂与对照打平 |
| 因子分位闸门（横截面条件分布） | **✓（`adx14≥90`，u1500）** | 无（1.44~3.22） | 无（40.5%~83.9%） | 本轮新增 |

**① 与 ④ 仍然没有在同一个配置上同时成立**，但这次不是"两个都没拿到"：
① 有了第一个九格全正的臂，④ 的缺口被量化成"胜率最低差 9.5pp、赔率最低差 0.56"，
而且缺口落在**同一个方向**——收紧闸门能抬胜率，但笔数掉到下限以下；
放宽闸门笔数够了，胜率又回去。这是一条可以用更大 universe 或更长时代继续压的曲线，
不是"此路不通"。要往前推，接下来该试的是**把闸门条件从单特征换成"特征 + 判据强度"的二元组合**
（`adx14` 高分位且 `signal.score` 高的那一批），或者把 universe 提到 3000 只以上换笔数——
而不是再切一遍阈值。



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
| `selectstock-alert-pusher` | — | 决策 → 钉钉/企业微信摘要；webhook 取自页面「通知渠道」，**仓库副本 `autostart=false`** | `deploy/selectstock-alert-pusher.conf` |

**盘中三进程只在交易时段有用**：收盘后它们空转（没有新 tick 就没有决策），不产出内容也不影响盘后链路。告警进程的机器人凭据**不在这里配**：它在管理页面「通知渠道」里维护（密钥密文落盘，pusher 发送时用后端 `core.security` 解密），`ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_SECRET` 只用来临时覆盖，真要长期覆盖时才写进 `/etc/supervisor/conf.d/` 那份，仓库这份永远留空。

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

> 注意有**两套互不重叠**的调度器：仓库根 `scheduler/scheduler.py` 跑 `config/inputs.conf` 的 `script://`（采集脚本）；后端进程内的 APScheduler 跑 `stock_selector_api/backend/conf/inputs.conf` 的 `task://`（设备巡检 5 分钟、17:30 选股、18:20 持仓诊断）。持仓诊断排在 18:20 是因为 `build_kline_features` 18:00 起跑、实测约 3 分钟，抢在它前面诊断出来的指标行还是上一交易日的。所以 API 进程要保持 `SCHEDULE_ENABLED=true`，同时**不要加 `--workers`**，否则定时任务会重复执行、SQLite 还会写锁竞争。

## 实盘手册：`adx14` 闸门 shadow 期怎么跑（准入已达成，尚未切 live）

准入决定（2026-10-13）：**分母修复完成 + 九格单笔期望全为正**即可进入 shadow；
胜率、赔率、利润因子不作前置准入条件，改作 **shadow 期熔断指标**，台账核验不匹配即终止。
撮合口径**保持「信号日收盘成交」不变**，偏差在台账里观测，不做引擎级改造。
仓位利用率接受 14%~20%，不改 `max_positions` 与单票金额。

### 一、回测假设与实盘可成交偏差（必读）

回测页上每一个数字都建立在一条假设上：**信号在收盘后算出来，成交按当天收盘价记**
（`BacktestEngine` 不看次日开盘）。实盘最早能成交的是**次一交易日开盘**，两者之差
只有 shadow 台账看得到——台账把同一批命中按两种入场价各算一遍：

| 口径 | 120 日放行臂 905 笔 |
|---|---|
| 信号日收盘入场（回测口径） | 胜率 52.4% · 赔率 2.06 · 利润因子 2.30 · 期望 **+4.40pp** |
| 次一交易日开盘入场（实盘口径） | 胜率 54.9% · 赔率 2.14 · 利润因子 2.64 · 期望 **+5.19pp** |

**差 −0.79pp，方向对实盘有利**：`pattern_reversal` 是抄底判据，信号次日往往低开，
所以收盘口径对这一条策略是**偏保守**的。这与《成交可执行性审计》一节的旧读数一致
（该策略买入成交 0 笔收在涨停线上、次晨开盘平均 −1.16%~+0.07%）。
**结论：保留收盘口径不改成引擎级次开盘撮合，但所有对外报的期望都要标注这是假设。**

### 二、开关在哪、默认什么状态

`stock_selector/config/settings.yaml` 的 `risk.adx14_rank`：

```yaml
  adx14_rank:
    enabled: false        # 默认关：这是研究出来的准入规则，不是开箱即用项
    mode: "shadow"        # shadow = 只记录不拦；live = 低于 min_pct 就 block
    factor: "adx14"
    min_pct: 90           # 当日全市场横截面前 10%
```

代码落点：
- 第六条风控规则 `risk/rules.py::adx14_rank`，与前五条同一接口、同一失败姿态
  （取不到分位一律放行并留 metric，不猜数）；
- 分母由 `risk/cross_section.py` 在**逐股遍历之前**扫一遍当日全市场算出来
  （只读 `adx14` 一列），结果经 `RiskContext.cross_pct` 喂给每只票——
  与回测用的是同一个函数、同一份列、同一套并列处理；
- `shadow` 模式永远放行，只把 `adx14_pct` / `adx14_gate` 写进决策 metrics。
  **它必须在同一批信号上留下"剔/不剔"的对照，又绝不能真的改变下单**，
  否则就没有对照了——这是 shadow 与 live 唯一的行为差异；
- 这两个字段随每条选股结果落库（`select_results` 的 `adx14_pct` / `adx14_gate`，
  重启时幂等加列），App 上每条命中下方显示
  「adx14 闸门 · 放行/剔除（分位 xx，shadow 期只记录不拦）」。
  **闸门没开或分位取不到时这两列是空的**——不能把 `reject` 落成 `adx14_pct=0`，
  0 分位是"当日全市场最弱"这个事实，而 reject 可能只是分位 83 没到 90 门槛；
- 网页端（8010 静态前端）**没有选股页**，`/api/v1/selection/*` 有接口但没页面挂，
  所以「依据」与闸门留痕现在只在 App 上看得见。

### 三、shadow 期每天做什么

1. **17:30 选股**（现役任务）：把 `enabled` 打开、`mode: shadow`。
   放行/剔除的判定随每条决策写进 `select_results` 的 extra；
2. **收盘后跑台账**（每天一次，约 2 分钟）：

   ```bash
   python3 scripts/build_shadow_ledger.py --days 120        # append，不覆盖历史
   python3 scripts/build_shadow_ledger.py --days 40 --report  # 只看最近 40 天，不写盘
   ```

   产物 `data/shadow_ledger/shadow.parquet`（逐条命中：分位、闸门判定、
   两种成交假设的单笔收益）与 `shadow_summary.json`（两臂指标 + 熔断判定）；
3. **看熔断那一行**：`verdict=ok` 继续，`verdict=trip` 就地终止、不许切 live。

### 四、熔断指标与阈值（`--trip-*` 可调）

| 指标 | 阈值 | 依据 |
|---|---|---|
| 放行臂笔数 | ≥ 60 | 低于此样本量谈胜率没有意义 |
| 胜率 | ≥ 35% | 回测放行臂九格最低 42%，留 7pp 余量 |
| 赔率 | ≥ 1.5 | 回测最低 1.22 |
| 利润因子 | ≥ 1.0 | ① 的实盘口径 |
| 单笔期望 | > 0 | 与回测同口径 |
| d期望（放行−剔除） | > 0 | 闸门确实有用；任一臂无样本则**不判** |
| 成交假设劣化（收盘−次开盘） | ≤ 0.5pp | 撮合假设没崩 |

**必须盯的样本量现实**：这条策略在 120 个交易日里出了 6409 条命中，但 **866 条集中在
7 月中下旬那一波急跌**（7-21 单日 791 条），9 月中旬以后几乎不出信号——最近 40 天只有
244 条命中、放行臂 27 条，低于 60 笔下限，所以那一档是 `trip`（样本不足）而不是"闸门失效"。
**含义**：shadow 期可能要好几个月才攒够样本，而且攒不攒得到取决于有没有下一次急跌。
不要因为"最近 40 天 trip"就判定规则坏了，也不要因为"120 天 ok"就提前切 live——
两个读数都要留在报告里。

### 五、切 live 的条件

1. shadow 台账 `verdict=ok` 且**连续 3 个月**（不是累计一次）；
2. 熔断从未触发（触发过就要重新走一轮，且要写清为什么）；
3. 切之前把 `mode` 从 `shadow` 改成 `live` 并**重启 8010 与 8002**（配置在进程启动时读）；
4. 切 live 之后仓位利用率会真的掉到 14%~20%——这是已接受的代价，不是故障，
   对外解释与告警文案都按这个口径写。

## 本机已关停的服务与恢复办法（2026-10-17）

查内存时关了三样东西，都是可逆的。恢复前先看「为什么关」和「恢复的代价」，别当故障排查。

### 1. 持仓的形态买卖规则（判定链路，不是进程）

线上现在**只有成本灾难线 −5% 在判 sell**，加仓侧同样关着。占位规则零标定，先停用。
生效文件是 `data/rules/active.json`（gitignore，只在本机）。恢复办法、`[]` 与「键缺失」的语义差别、
抽查命令见 `stock_selector_api/README.md` §6「形态买卖规则的停用与恢复」。改文件不用重启进程
（`load_rules()` 每次判定重读）。

### 2. lex 项目（WeKnora 四容器 + lexmath-backend + lexmath-cloudflared）

现状：四个 WeKnora 容器 `Exited`，`lexmath-backend` / `lexmath-cloudflared` 均 `inactive + disabled`。

```bash
docker start WeKnora-postgres WeKnora-docreader WeKnora-app WeKnora-frontend
sudo systemctl enable --now lexmath-backend
# 公网入口要的话再单独起（quick tunnel → localhost:80）：
sudo systemctl enable --now lexmath-cloudflared
```

compose 项目在 `/data/work/WeKnora` 与 `/data/work/lex_math_new`。

**⚠️ 两个绝对别停的容器**：SelectStock 的公网入口和数据库都住在 lex 的 compose 项目里——

- `lexmath-nginx`：`/etc/nginx/conf.d/selectstock.conf` 在它内部（`listen 80` + `listen 8080`，
  `server_name www.luro.uno`，`proxy_pass http://selectstock_api` → `host.docker.internal:8010`），
  并把 80/8080 发布到宿主机。链路是「Cloudflare → 本机 `cloudflared.service`（named tunnel，还在跑）
  → 宿主机 80/8080 → 这个容器 → 后端 8010」。停了它 = SelectStock 网页与 App 全下线；
- `lexmath-mongo`：SelectStock 的 `stock_kline`（1,221 万条）、`profit_scan_rows`、`stock_indicators`、
  `eastmoney_news` 全在这一个实例里。停了它 = 选股/回测/扫描一起断。

要再关 lex 的东西时只能按上面的名字逐个关，**不要 `docker compose down`**。

### 3. llama-server（崩溃循环已修好根因，服务仍关）

现状 `inactive + disabled`。历史：`/usr/local/bin/llama-server` 是个外壳，去固定路径找
`libllama-server-impl.so` 找不到 → 每分钟 114 行重启日志、累计 16,197 次，白白刷 journald。
修法是加了一个 drop-in（`/etc/systemd/system/llama-server.service.d/llama-lib-path.conf`）把
`LD_LIBRARY_PATH` 指到 `/data/work/llama.cpp/build/bin`。**drop-in 在，所以现在启动就能起来**，不用再改别的。

```bash
sudo systemctl enable --now llama-server   # 端口 18080
```

代价：模型 `Qwen3.5-4B-Q4_K_S.gguf` 2.59 GB，启动即常驻 pinned 这么多内存。这台机器的内存本来就是这次调查的主角，
起之前确认当时没有全量扫描/回测在跑。`lexmath-llama.service` 是**另一个也绑 18080、也吃这个模型**的 unit
（它直接跑 `build/bin` 里的真二进制，所以没有库路径问题），一直是 `disabled`、日志为空；
两个不能同时起，动它同样要先问。

### 4. journald

已 `--vacuum-size=500M` 一次性清过（清掉 3.4 GB）。**没有**设永久上限，日志会慢慢再涨回去。
要长期封顶就加 drop-in `SystemMaxUse=500M` 后重启 `systemd-journald`（这一步当时没批，没做）。

## qlib 模型训练手册（以后自己跑一遍的完整步骤）

一句话：**训练 = `scripts/qlib_run.py` 一条命令**；线上打分服务（8003）**只读磁盘上的模型文件**，
不参与训练，也不需要重启就能认新模型。改模型的完整动作就是「训练到别的目录 → 拷进
`data/qlib/model/` → 调一次 refresh」。

口径真源是 `stock_selector/docs/qlib路线数据口径与验收门槛.md`（标签、票池、成本、验收门槛、
每批预注册与读数都在那里）。本节只讲**怎么把一次训练跑出来并换上线**，不重复讲口径。

### 1. 前置条件（30 秒自查，全过才开始）

| 检查 | 命令 | 期望 |
|---|---|---|
| Python + qlib + lightgbm | `/usr/bin/python3 -c "import qlib,lightgbm;print(qlib.__version__, lightgbm.__version__)"` | `0.9.7 4.7.0`（qlib 装在 `~/.local/lib/python3.10/site-packages`） |
| bin 面板存在 | `ls /data/work/qlib_data/cn/calendars` | 有 `day.txt` |
| **面板更到最新交易日** | `/usr/bin/python3 -c "import qlib;qlib.init(provider_uri='/data/work/qlib_data/cn',kernels=1);print(qlib.data.D.calendar()[-1])"` | 最后一个日历日 = 最近一个交易日 |
| 内存够 | `free -g \| awk 'NR==2{print $7}'` | ≥ 5（GB available） |
| 没有在跑的同类任务 | `pgrep -af qlib_run` | 只匹配到自己那条 |

**面板没更到最新**是最常见的坏法：训练会照样跑完、IC 也照样出一个数，只是那段「留出」根本没有数据。
补面板：17:15 定时任务 `scripts/qlib_update_bin.py`（`config/inputs.conf`，2026-10-23 从 16:00 挪过来，
因为沪市行情 16:24~16:31 才落库），手动补一次就
`/usr/bin/python3 -u scripts/qlib_update_bin.py --scope universe --check-plugin`（约 40 秒，跑完自动通知 8003 清缓存）。

`MLFLOW_ALLOW_FILE_STORE=true` 不用手动设，脚本里 `setdefault` 了；不设的话新版 mlflow 会拒绝文件型
backend，训练直接抛异常。

### 2. 先冒烟，确认管道通（约 1 分钟）

```bash
cd /data/work/SelectStock
/usr/bin/python3 -u scripts/qlib_run.py --smoke --run-id smoke_$(date +%H%M)
```

`--smoke` 只是把池压到 120 只、窗口换成 2019→2021，跑的是**同一条链**。它红了就先修环境，别急着跑正式臂。
峰值约 670 MB。

### 3. 正式训练：命令与每个参数的含义

批次 M 实际用过的那两条命令就是标准跑法（原样可抄，只需换名字和日期）：

```bash
# 上线臂：重挑池 + 训练截止 2026-06-30 + 树数钉死
/usr/bin/python3 -u scripts/qlib_run.py --pool-end 2026-03-31 --inst-name live2026jun \
  --run-id live2026jun --seed 7 --fixed-trees 141 \
  --train-beg 2016-01-01 --train-end 2026-06-30 \
  --valid-beg 2026-07-01 --valid-end 2026-07-31 --test-beg 2026-07-01 --test-end 2026-10-16 \
  --export-model data/qlib/model_live2026jun

# 参照臂：读上线臂的池、同样树数，只差 train_end（两臂配对才做得成）
/usr/bin/python3 -u scripts/qlib_run.py --pool-file data/qlib/model_live2026jun/pool.txt \
  --inst-name batchM_ref --run-id batchM_ref --seed 7 --fixed-trees 141 \
  --train-beg 2016-01-01 --train-end 2024-12-31 \
  --valid-beg 2026-07-01 --valid-end 2026-07-31 --test-beg 2026-07-01 --test-end 2026-10-16
```

| 参数 | 作用 | 要注意什么 |
|---|---|---|
| `--train-beg/--train-end` | 训练窗 | `train_end` 一挪，**票池也会换血**（批次 I 实测 59/300 只不同），所以要么冻结池要么两臂同池 |
| `--valid-beg/--valid-end` | 验证段 | 用 `--fixed-trees` 时它不参与选轮数，所以可以故意和 test 重叠，把留出段一天不浪费 |
| `--test-beg/--test-end` | 测试段（= 样本外读数从哪来） | 起点必须**严格晚于** train_end，否则 `holdout_ok` 判无留出段：IC/逐日读数一个都不出，`--backtest` 直接退出 |
| `--universe` | 按训练段日均成交额挑前 N 只 | **默认 300 是内存上限**：实测每行样本 ~3.65 KB，300 只 × 10 年 ≈ 3.2 GB；抬高之前按这个单价重算 |
| `--pool-end` | 重挑池用的流动性窗右端 | 缺省 = `--train-end`。想把「训练吃到最新」和「池按某个已收口时点挑」分开时才给；**与 `--pool-file` 互斥**，两个一起给会直接报错 |
| `--pool-file` | 冻结票池（一行一个 symbol，也吃 instruments 的三列格式） | 滚动重训必带，读上一臂产出的 `pool.txt` |
| `--inst-name` | 写 `instruments/<name>.txt` 的名字，缺省 `liq<池大小>` | **跑研究臂务必另起名**，缺省名会覆盖线上实盘那份 |
| `--seed` | LightGBM 种子 | 缺省不设 = 与历史批次同口径；要做配对检验就给同一个值 |
| `--fixed-trees` | 关掉早停、恰好长 N 棵树 | 训练段吃到日历末尾时，早停等于在自家数据上挑轮数。**数值要对齐现役模型的真实棵数**：`/usr/bin/python3 -c "import lightgbm as lgb;print(lgb.Booster(model_file='data/qlib/model/model.txt').num_trees())"`（现在 = 141）。别用 `grep -c Tree` 数，那个数不准 |
| `--export-model DIR` | 训练完落盘 booster + 归一化 mean/std/列序 + 票池 | 不带 DIR = 写 `data/qlib/model`，那是**线上目录**，会被第 8 节第 3 条的闸门拦下 |
| `--write-live-model` | 明确允许写线上目录 | 单独用它不会写，只是放行；正常流程建议**总是写到别的目录**，上线走第 6 节的拷贝 |
| `--backtest` `--topk` `--n-drop` `--benchmark` | 附带跑 TopkDropout 回测 | 基准只能是 `SH510300`（面板里唯一贯穿 2015→2026 的大盘代理，399xxx 只有 2025 年之后的数据）。回测读数**必须看净值 `report["account"]`，不是 `report["return"]`**（后者是扣成本前的毛利，差 8 个百分点/年） |
| `--to-mongo` | 名单写进 Mongo `qlib_top_stocks` | **缺省关**。这是共享状态，手测时不要开 |
| `--run-id` | 产物文件的名字 | 落盘名都带它，起个能认出的名字 |

**成本**：固定 141 棵一臂约 **100 秒**，带早停约 **5 分钟**；峰值内存 **~2.9 GB** ⇒ **两臂必须串行跑**，
并发会触发 earlyoom 去杀别的进程。放后台就把日志落盘：`... > /tmp/arm_x.log 2>&1 &`。

### 4. 产物在哪

| 文件 | 内容 |
|---|---|
| `data/qlib/run_<run_id>.json` | 聚合读数：IC / ICIR / Q5−Q1 / 票池 / 段划分 / `holdout` / `fixed_trees` / `pool_end` |
| `data/qlib/ic_daily_<run_id>.parquet` | **逐日**宽表（秩相关、Pearson、横截面只数、Q5−Q1 日差）——配对检验的输入，没有它每次滚动重训只能报点估计 |
| `data/qlib/pred_<run_id>.parquet` | 测试段逐日逐票打分 |
| `<export DIR>/model.txt` `norm.npz` `pool.txt` `meta.json` | 8003 只读这四份；`meta.json` 里有 `run_id`/`train_end`/`pool_size` |

### 5. 读数怎么看（三条都会翻结论的规则）

1. **逐日 t 要 ÷√5**：标签是 `Ref($close,-5)/Ref($close,-1)-1`，相邻 5 天共用同一段涨跌，
   普通 t 会系统性高估显著性。配对脚本已经自动除了。
2. **不同窗口的绝对 IC 不能横着比**：批次 M 里参照臂在 67 天窗上 IC 0.093，而它自己 309 天窗上只有 0.0487
   ——这段日子对**所有**臂都容易。要比就同一天配对相减。
3. **点数不等于显著**：见下一节。

### 6. 两臂配对检验（判断「换训练窗」有没有用，只有这一件事算数）

```bash
/usr/bin/python3 -u scripts/qlib_ic_pair.py live2026jun batchM_ref          # 主读数：逐日秩相关
/usr/bin/python3 -u scripts/qlib_ic_pair.py live2026jun batchM_ref --column ls_spread   # 分层价差
```

差 = 第一个臂 − 第二个臂。输出看 `days_paired`（**< 60 天就只能当点估计，别写成结论**）、
`mean_diff`、`t_adjusted`（已 ÷√5）、`corr_ab`（两臂逐日读数相关性，越高说明差异越可能是抖动）。

批次 M 的读数留在这儿当参照尺：同池同树数下，训到 2026-06-30 比训到 2024-12-31 **低** 0.0356，
`t_adjusted = −1.79`（不显著），67 天里 61% 的日子旧臂更好，两臂相关性 0.904。
⇒ 本批按预注册判的是「先不换」，但 2026-10-18 17:19 决定现在就换 ⇒ **线上现在是 `live2026jun`**
（`train_end=2026-06-30`，导出 17:01:20，换线时刻 17:21:28，票池同时换血 44/300）。
被换掉那版的备份在 `data/qlib/model_backup_1018_1721/`，回滚就是第 7 节最后那条 `cp`。

### 7. 把训好的模型换上线（4 步，随时可回滚）

```bash
cd /data/work/SelectStock
# ① 先备份当前线上那份（回滚就靠它）
cp -r data/qlib/model data/qlib/model_backup_$(date +%m%d_%H%M)
# ② 覆盖线上目录（只覆盖四个文件，别 rm 目录）
cp data/qlib/model_live2026jun/{model.txt,norm.npz,pool.txt,meta.json} data/qlib/model/
# ③ 让 8003 清进程内缓存（qlib 把日历缓存一小时，不刷它它坚持说「今天还不是交易日」）
curl -s -X POST http://172.17.0.1:8003/api/admin/refresh
# ④ 直连 8003 验打分（第一个请求要给 300 只池补 130 天因子，实测 34.5 秒；热了 0.03 秒）
curl -s -X POST http://172.17.0.1:8003/api/selector/run -H 'Content-Type: application/json' -d '{"topk":30}'
curl -s http://172.17.0.1:8003/api/health | python3 -m json.tool   # model 段应显示新 run_id
```

七条硬规矩：

- **地址写 `172.17.0.1` 不写 `localhost`**——8003 没监听 127.0.0.1。
- **④ 只能直连 8003**，绝不要走后端 `start_run`：那会推真实钉钉「本次选股命中 30 只」。
- **时间避开 17:30（选股）和 18:20（持仓诊断）**，换模型在 **17:35 之后**动手。
- **换了票池要单独确认一次**：新池里那些原先不在池中的票，要等**下一次 17:15 增量任务**才进增量链，
  换线当天它们的数据只到全量 dump 的最后一天。下一次 17:15 跑完抽查这几只有没有更新到当天。
- 打分池现在是 `--scope universe`（15:55 的 `scripts/qlib_daily_universe.py` 按全市场规则每天重算，
  2026-10-23 起它直接读 bin 面板），17:15 的 `qlib_update_bin.py` 跟着那份 `data/qlib/universe.txt` 补，
  不再读产物里的 `pool.txt`（那是训练池 300 只）。
- 回滚 = 把备份目录的四个文件再 `cp` 回去 + 再调一次 refresh。
- `data/qlib/model` 里的名字（`liq300.txt` 等 instruments）别当备份用，实盘池那份会被覆盖；
  要留旧池就单独存（例：`instruments/liq300_trainend2021.txt`）。

### 8. 四个会把结果静默作废的坑（都踩过）

1. **`--fixed-trees` 给 20 却长出 12 棵**：qlib 的 `LGBModel.fit` 无条件挂 `lgb.early_stopping`，
   lightgbm ≥ 4 收尾会把 booster 裁到 `best_iteration`。脚本的修法是绕过 `fit` 直接 `lgb.train`，
   并在训完**断言真实棵数**，对不上就 `SystemExit`。
2. **接着又变成 600 棵**：`n_estimators` 是 lightgbm `num_iterations` 的别名，会**盖掉**
   `num_boost_round`。现在构造参数里会把 `n_estimators/num_boost_round/num_iterations` 全部剔掉再训。
   顺带一句：`n_estimators=600` 也把批次 L 的早停封顶在 600 轮，那批读数不受影响。
3. **`--export-model` 不带 DIR 就等于当场换线上模型**（8003 每次调用重读磁盘）。所以训练**之前**就拦：
   指向 `data/qlib/model` 而没给 `--write-live-model` 直接退出，不会白花五分钟再拒绝。
4. **`--pool-file` 与 `--pool-end` 一起给**：池已经定死，`--pool-end` 会被无声忽略 ⇒ 直接报错，不给两可。

### 9. 想加一条「滚动重训」的定时任务之前

先想清楚这两件事，缺一件就只能出点估计、不能出结论：
① 池要么冻结要么两臂同池；② 逐日 IC 必须落盘（现在默认落）。
滚动重训的正确做法是**跑两臂（新窗 + 现役窗）再配对**，单跑新窗得到的那个 IC 数字没法归因。
另外本机内存只够串行，两次训练不能同时跑。

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