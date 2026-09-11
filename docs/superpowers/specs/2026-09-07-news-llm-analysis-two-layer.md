# 新闻 LLM 分析两层存储 - 设计文档

> 日期：2026-09-07（v2：含风险拆解、聚合键、归一化、重建策略整改）

## 1. 目的

将 `scripts/analyze_news_llm.py` 从"伪代码 + parquet 单层宽表"改造为：
1. **真实接入 MongoDB** 读取东财新闻（`eastmoney_news.news`）。
2. **两层数据结构**：新闻分析原文归档 + 规范化的关注目标（股票/板块），同时落 parquet 和 MongoDB，支持"今天关注哪些股票/板块"的查询聚合与跨天统计。

## 2. 数据源（已探查确认）

- MongoDB: `eastmoney_news` 库，`news` 集合（当前 12207 条）
- 文档字段与映射：

| Mongo 字段 | 类型 | 说明 | 映射到 |
| --- | --- | --- | --- |
| `_id` | 24位hex字符串 | 新闻唯一 id | news_id |
| `title` | str | 标题 | title |
| `summary` | str | 正文摘要（无独立 content 字段） | content |
| `showTime` | str `"YYYY-MM-DD HH:MM:SS"` | 发布时间（字符串，可直接字典序比较） | publish_time |
| `code` | str | 新闻编号（如 202603133670583366），非股票代码 | - |
| `stockList` | list[str] | 东财标注相关标的，secid 格式 | stock_hints |
| `image` | list | 图片 | - |
| `pinglun_Num` / `share` / `realSort` / `titleColor` / `crawlTime` | 展示/爬取元数据 | - | - |

> 注意：实际字段名是 `stockList`/`image`（非 `stockListArray`/`imageArray`）；文档**没有** `url`/`source` 字段。

### 2.1 stockList 的 secid 格式与归一化

样例（已从全库统计）：
- `90.BK0464`、`1007.BK000199` → **板块**（BK 开头）
- `0.159945` / `1.688256` / `150.014238` → A股个股
- `105.GOOG` / `106.NIO` / `107.UMAC` / `153.XIACY` → 美股
- `201.HK49` → 港股
- `116.06030` / `999.356570` → 前缀存在于库中，含义待确认（不强行归一）

**归一化规则 `normalize_secid(secid) -> NormalizedHint`**：
- `90.BK0464` / `1007.BK000199` → `{"code": "BK0464"|"BK000199", "name": "", "market": "板块"}`
  规则：BX 前缀直接取 `BKxxxx` 段，丢弃市场前缀。
- `0.159945` / `1.688256` / `150.014238` → `{"code": "159945"|"688256"|"014238", "name": "", "market": "A股"}`
  规则：丢弃市场前缀取 6 位数字段。
- `105.GOOG` 等 → `{"code": "", "name": "GOOG", "market": "美股"}`
  规则：非数字后缀段视作 name，code 置空。
- 无法识别前缀 → `{"code": "", "name": "", "market": "未知"}`，仅作文本提示，不进候选代码表。

归一化结果供 3 处使用：(1) LLM prompt 线索；(2) watch_target 拆解时的**候选代码表**（回填空 code）；(3) news_analysis 落盘 stock_hints。

### 2.2 --days 语义：自然日而非24h滑动窗口

过滤口径：`showTime >= (今天 - N天) 的 00:00:00`。
示例（2026-09-07 运行，`--days 1`）：`{"showTime": {"$gte": "2026-09-06 00:00:00"}}`。
理由：新闻按"某一天发的"理解更直观，避免上午跑漏掉昨夜新闻。

## 3. 两层数据结构

### 3.1 第一层：news_analysis（每条新闻一条记录）

parquet 列 = 现有 `ANALYSIS_SCHEMA`（22列），字段：
`collect_date, news_id, title, source, url, publish_time, content_excerpt, summary, sentiment, sentiment_score, impact_period, bullish, bearish, risks, watchlist, follow_signals, action, key_points, market_reaction, conclusion, llm_model, raw`

新增 1 列：
- `stock_hints`: list[str]，入参 stockList 归一化后的代码串（如 `["BK0464", "159945"]`），裸 secid 也保留在 raw_hints 备用 —— 统一为 `stock_hints` 存归一化代码，`raw` 字段保留 LLM 原始返回，secid 原文不再单独列。

- `source`: 固定 `"eastmoney"`（东财新闻无 source 字段）
- `url`: 空串（文档无 url，东财无公开单页链接）
- `publish_time`: 由 `showTime` 字符串直接取（保持 `"YYYY-MM-DD HH:MM:SS"` 格式，字典序排序有效）

### 3.2 第二层：watch_target（一个"标的+方向"当日一条记录）

由 `bullish` / `bearish` / `watchlist` / `risks` 拆解后**当日合并**而成。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| collect_date | date | 分析日期（`YYYY-MM-DD` 字符串） |
| code | str | 标的代码（板块 BK 代码 / 个股 6位；无法确定空串） |
| name | str | 标的名称 |
| category | str | `板块` / `行业` / `个股` / `风险` |
| direction | str | `bullish` / `bearish` / `watch` / `risk` |
| score | int | 影响方向与强度（-10~10） |
| priority | str | `高`/`中`/`低`（watch 才有，其余空） |
| reason | str | LLM 理由（多条新闻时取第一条非空） |
| source_news_ids | list[str] | 来源新闻 id 列表（当日聚合） |
| llm_model | str | 模型 |

**拆解规则**：
- `bullish[]` → direction=bullish, score=score（category 用自带的 `板块/行业/个股`）
- `bearish[]` → direction=bearish, score=score
- `watchlist[]` → direction=watch, score=0, priority=priority
- `risks[]` → direction=risk, score=score, **category=风险, name=desc 前 20 字（原 schema 无 name/code）**, code=空
- **元素格式防御**：`bullish/bearish/watchlist` 遍历时仅处理 `isinstance(x, dict)` 的元素，字符串元素跳过（记 WARNING）
- **code 回填**：标的自带 code 为空时，用候选代码表匹配——name 与归一化 hint 的 name 相同，或该新闻 stock_hints 中恰好只有一个同市场候选 → 回填；无法确定保持空串，**不猜**

**聚合键（关键整改）**：
```
target_key = f"{code}|{direction}" if code else f"NAME:{name}|{direction}"
```
同一 `collect_date` 内相同 target_key 的记录合并：
- source_news_ids 取 union（当日跨新闻聚合）
- score 取第一条（强度不可简单相加，避免重复累积）
- name/priority/reason 取第一条非空
- 合并后落一行

> 选 code 为主键：LLM 对同一股票跨新闻 name 可能不一致（"欣旺达" vs "欣旺达动力"），但 code 稳定 → 用 code 保证跨新闻可合并；无 code 时退而求其次用 name。

## 4. 存储布局

### 4.1 parquet

```
data/news_analysis/{collect_date}.parquet        # 第一层：news_analysis
data/news_watch/{collect_date}.parquet           # 第二层：watch_target
```

- zstd 压缩
- `news_analysis` 增量合并按 `news_id` 去重保留最新（现有逻辑保留）
- `watch_target` 由当日 news_analysis 全量拆解合并后**整文件覆盖**（与 Mongo 重建一致）

### 4.2 MongoDB

新增两个集合（`eastmoney_news` 库）：

**news_analysis**：每条新闻一条文档
```
_id = f"{collect_date}|{news_id}"     # 幂等 upsert
doc = 字段同 parquet 对应行（bullish/bearish/risks/watchlist 为嵌套数组）
```
写入：先 `delete_many({"_id": {"$in": ids}})` 再 `insert_many`（当日整批重建，杜绝残留）。
索引：`{collect_date:1}`、`{news_id:1}`、`{_id:1}`（默认）。

**watch_target**：每个 target_key 一条文档
```
key = md5(f"{collect_date}|{direction}|{code or name}").hexdigest()[:16]
_id = key                            # 确定性短 key，与展示无关
doc = ...  + code/name 独立字段
```
写入：**delete_many({collect_date: 当日}) → insert_many(当日全部)**（整日重建，无 addToSet 残留问题）。
索引：`{collect_date:1, direction:1}`、`{code:1}`、`{category:1}`、`{collect_date:1, code:1}`（趋势查询）。

> `_id` 仅作唯一约束，不用来承载展示信息（name/code 独立字段）。全部失败容忍见第 9 节。

## 5. LLM 调用增强

- user prompt 追加已知相关标的段，**用归一化后的线索**：
  ```
  已知相关标的（来自东财 stockList，可沿用其代码）：
  - BK0464（板块）
  - 159945（A股）
  - GOOG（美股,代码未知）
  ```
- SYSTEM_PROMPT 增加说明：bullish/bearish/watchlist 的 code **优先沿用上面已知代码**；无法确定时给空串，不要编造。
- `--demo` 模式 stockList 为空，提示段省略。

## 6. CLI 用法（保持兼容）

```bash
python scripts/analyze_news_llm.py                 # 真实读 Mongo，分析最近1天（自然日）
python scripts/analyze_news_llm.py --days 3        # 最近3天
python scripts/analyze_news_llm.py --limit 10      # 最多10条
python scripts/analyze_news_llm.py --demo          # 内置示例新闻（Mongo 空时）
python scripts/analyze_news_llm.py --no-mongo      # 只写 parquet，不写 Mongo（调试/CI）
```

## 7. 控制台摘要增强

除了现有逐条摘要，尾部追加"今日关注"聚合（直接读当日 watch_target 拆解结果）：
```
=== 今日关注 ===
[板块] BK0464 二线动力电池 (bullish +8, 2条新闻)
[个股] 300207 欣旺达 (bullish +8, 高, 1条新闻)
[风险] 欣旺达动力未盈利 (-6)
```

## 8. 跨天统计查询（方案A：惰性聚合，不建冗余第三层）

跨天支持：**热度排行** + **个股趋势**。parquet 与 Mongo 等价实现，先不物化 summary。

### 8.1 热度排行（近 N 天）

口径：`collect_date ∈ [start, end)` 的 watch_target，按 `category(+direction)` 分组，
`news_count` = source_news_ids 去重总数，`total_score` = ∑score。

parquet 端：

```python
def rank_heat(df, category=None, direction="bullish", top_n=30):
    # df = concat(近N天 data/news_watch/*.parquet)
    cond = (df["direction"] == direction)
    if category:
        cond &= (df["category"] == category)
    part = df[cond]
    agg = part.groupby("code").agg(
        name=("name", "first"),
        news_count=("source_news_ids", lambda s: len(set().union(*s))),
        total_score=("score", "sum"),
    ).sort_values("total_score", ascending=False).head(top_n)
```

Mongo 端：

```python
col.aggregate([
  {"$match": {"collect_date": {"$gte": start, "$lt": end}, "direction": direction,
              **({"category": category} if category else {})}},
  {"$group": {"_id": "$code",
              "name": {"$first": "$name"},
              "news_count": {"$sum": {"$size": "$source_news_ids"}},
              "total_score": {"$sum": "$score"}}},
  {"$sort": {"total_score": -1}}, {"$limit": top_n},
])
```

> 因 watch_target 是"当日已合并行"，source_news_ids 是该标的正日去重新闻集合；跨天相加 = 去重总数，口径正确。

### 8.2 个股/板块趋势

口径：指定 `code`，近 N 天逐日净分（bullish 为正、bearish/risk 为负，watch 为 0 天然不计），
再用 cumsum 看事件强度增强/消退。

parquet 端：`df[df.code==code].groupby("collect_date")["score"].sum().sort_index().cumsum()`
Mongo 端：`$match:{code, collect_date:{$gte:start}}` + `$group by collect_date ∑score`，cumsum 在应用端算。

### 8.3 统计 CLI（并入 analyze_news_llm.py）

```bash
python scripts/analyze_news_llm.py --stats heat --start 2026-09-01 --end 2026-09-08 --category 板块 --top 20
python scripts/analyze_news_llm.py --stats trend --code BK0464 --start 2026-09-01 --end 2026-09-08
```

- `--stats` 与正常分析互斥：单独走统计查询，不拉 LLM
- Mongo 查询优先；`--no-mongo` 时退到 parquet 惰性 concat
- `start/end` 解析为 `YYYY-MM-DD`，end 默认 today+1

## 9. 容错

- Mongo 读失败 → 走 --demo（同现状）
- 单条新闻分析失败 → 现有重试 3 次 + 降级中性记录（不产出 watch_target）
- **Mongo 写失败 → WARNING 日志，不影响 parquet（parquet 为准）**；整批 delete+insert 任一失败按批回滚（先删除则本次不插，留待下次重建补齐）
- `--no-mongo` 跳过 Mongo 写
- 拆解元素非 dict → 跳过并 WARNING
- LLM 频率限制（429）→ 现有 backoff 重试；连续失败降级中性，保证当日文件仍可落盘

## 10. 附录：待确认

- secid 前缀 `116` / `999` / `150` 的确切含义（当前不强行归一，影响仅为候选代码表覆盖率）
- watch_target 是否需要在控制台之外提供"净分"快捷视图（与 market_signals/watch_list 的后续联动不在本次范围）