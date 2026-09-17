### 【Story】Story1.3.1-选股结果快照落库 🔴

> 来源：REQ-003（P0）

【用户故事】⭐

作为个人投资者（使用者本人），我想要每次选股运行的结果被持久化保存（头记录 + 明细），以便后续随时回看与复盘。

【场景说明】⭐

针对结果沉淀场景：每次选股运行生成 select_runs 头记录 + select_results 明细记录，唯一约束 (run_id, code, signal) 防重；失败运行也保留头记录便于留痕。
**痛点背景**：结果仅 CLI 输出、无落库；诉求=双表快照、明细唯一、失败留痕。
- 来源：REQ-003（关联 ch02 场景1/2）

---

【A/C 验收条件】

一、功能性验收条件 ⭐

1、业务规则：
- select_runs：id/owner_type(user|system)/owner_id/trigger_type(manual|scheduled)/strategies/status(running|success|failed)/result_count/started_at/finished_at/duration_ms/error。
- select_results：id/run_id/code/name/signal/reason/extra(JSON)；唯一约束 (run_id, code, signal)。
- 落库时机：任务开始建头记录（running），结束时写明细与终态。
- 失败运行保留头记录（error 字段），便于留痕与统计。
- owner 由服务端按触发源生成，客户端不可覆盖。

2、实例化场景描述：

   正常场景：成功运行落库
   Given（前提条件）：提交一次选股并执行成功
   When（行为）：任务结束
   Then（结果）：select_runs 新增 1 条 success（含 owner/trigger/strategies/result_count/duration），select_results 新增 N 条明细

   正常场景：明细唯一性
   Given（前提条件）：插件返回含重复 (code, signal)
   When（行为）：落库
   Then（结果）：唯一约束去重，同 run 内该组合仅 1 条记录（静默去重）

   异常场景：失败运行留痕
   Given（前提条件）：任务失败
   When（行为）：终态写入
   Then（结果）：select_runs.status=failed 且 error 有值，已写明细保留

   异常场景：owner 标记正确
   Given（前提条件）：manual 与 system 任务各一次
   When（行为）：落库
   Then（结果）：manual 记录 owner_type=user+owner_id；system 记录 owner_type=system

   安全场景：读取隔离
   Given（前提条件）：用户 A 的 manual 运行
   When（行为）：用户 B 尝试读取
   Then（结果）：返回无权限语义（REQ-015）

   安全场景：无敏感 extra 字段
   Given（前提条件）：extra 为 JSON 扩展字段
   When（行为）：落库
   Then（结果）：仅存储选股相关元数据，不含认证/密钥等敏感信息

   边界场景：零命中 / 大量命中
   Given（前提条件）：插件返回空信号列表；或单次命中 500 条
   When（行为）：落库
   Then（结果）：零命中头记录 success、result_count=0、无明细；500 条全部写入耗时 ≤1s（p95）无截断

【操作步骤】⭐

无需用户操作（任务执行内部自动落库）；用户通过结果 Tab/历史查询感知结果（经由 REQ-004）。

【期望效果】⭐

每次运行均有完整头+明细落库，同 run 内无重复 (code, signal)，失败运行留痕可追溯，落库写入 p95 ≤1s（REQ-003 非功能）。

【需求规格约束】🔲

表结构字段定义详见后续总体设计（ch07 §7.5 DB 交付要求）。

二、非功能性验收条件 ⭐

【产品需求】

- 性能：落库写入 p95 ≤1s（单次运行规模内）。
- 可靠性：明细不重复（唯一约束兜底）；数据规模增长本期不自动清理（个人工具量级受控）。
- 可测试性：可构造固定信号集的 mock 插件，用 SQL 断言头/明细字段全部符合预期（TES-03）。

【安全需求】⭐ 必填（QS-007）

- 私有运行不可被他人读取（归属过滤）（SEC-01）。
- owner_type/owner_id 服务端生成、客户端不可覆盖（防越权伪装）。
- extra 不存储敏感信息（避免密钥/认证落库）。

【海外合规】🔲

不涉及（个人自用国内工具，QS-006 裁剪）。

---

【依赖与风险】⭐

1、需求关联性，内部功能依赖：依赖 REQ-002（提交）、REQ-004（查询）、REQ-015（隔离）。
2、技术风险、测试风险：数据规模增长（低）→本期不自动清理，量级受控。
3、跨团队协作（沟通/确认/附件）：不涉及（复用 repositories 双实现，已归档 doc/kb）。

【技术思路】👤

待开发负责人填写

【工作量评估】👤

待评估

【责任人】👤

规划：（待填写）
交互：（待填写）

> 交付标记：🔴 P0（REQ-003）