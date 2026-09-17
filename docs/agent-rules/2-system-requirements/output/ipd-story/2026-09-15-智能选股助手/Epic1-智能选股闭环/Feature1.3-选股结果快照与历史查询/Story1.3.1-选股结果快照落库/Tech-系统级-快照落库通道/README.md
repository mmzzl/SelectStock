# Tech-系统级-快照落库通道

1、【技术故事】 - TL & 模块负责人

支撑 Story1.3.1（REQ-003）的落库链路：选股任务开始建 select_runs 头记录（running），结束时写 select_results 明细与终态；唯一约束 (run_id, code, signal) 防重；失败运行保留头记录（error 字段）留痕。owner 由服务端按触发源生成。

关联的系统需求编号：REQ-003；依赖 REQ-002（触发）、REQ-004（查询）、REQ-015（隔离）。

2、【A/C验收条件】 - TL & 模块负责人

2.1 功能性验收条件

1、业务规则、边界：
- select_runs：id/owner_type(user|system)/owner_id/trigger_type(manual|scheduled)/strategies/status(running|success|failed)/result_count/started_at/finished_at/duration_ms/error。
- select_results：id/run_id/code/name/signal/reason/extra(JSON)；唯一约束 (run_id, code, signal)，同 run 内重复组合静默去重。
- 落库时机：任务开始建头记录（running），结束写明细与终态；失败运行保留头记录。

2、系统功能约束：
- owner_type/owner_id 服务端生成、客户端不可覆盖；extra 不存敏感信息。
- manual 运行仅属主可读，system 运行全员可读（REQ-015）。

3、实例化场景（正常场景和异常场景）：
- 正常：成功运行 → select_runs 1 条 success（含 owner/trigger/strategies/result_count/duration）+ select_results N 条明细。
- 异常：插件重复返回 (code, signal) → 唯一约束去重；任务失败 → status=failed 且 error 有值、已写明细保留；B 读 A 的 manual run → 无权限语义。

2.2 非功能性验收条件

1）性能分析：落库写入 p95 ≤1s（REQ-003 非功能，单次运行规模内）。
2）可靠性：明细唯一约束兜底重复写入；失败可重试不产生重复运行数据。
3）可维护性：select_runs 含 started_at/finished_at/duration_ms/error 全量留痕（LOG-03）。
4）数据可扩展性：含注记与明细结构承载扩展（SCA-01-ch07 §7.5 结构基础）。
5）安全性：归属服务端生成防越权伪装（SEC-01）；extra 不落敏感信息。
6）其他：可测试性——mock 插件固定信号集，SQL 断言头/明细字段全符合预期（TES-03）。

3、【依赖与风险】 - TL & 模块负责人

1、需求关联性，内部功能依赖，外部跨团队协作：依赖 REQ-002/004/015；不涉及外部团队。
2、技术风险、测试风险：数据规模增长（低）→本期不自动清理（个人量级受控）。

4、【技术思路】 - TL & 模块负责人

1、技术如何实现，方案如何设计，指导开发：
   架构边界：select 模块新增运行/结果持久化仓库与快照 service，复用既有 repositories 双实现（ch07 §7.5 DB 交付命名规则）。
2、技术方案：运行头表与明细表写入见服务级 Tech 文件；具体实现由开发负责人深化。