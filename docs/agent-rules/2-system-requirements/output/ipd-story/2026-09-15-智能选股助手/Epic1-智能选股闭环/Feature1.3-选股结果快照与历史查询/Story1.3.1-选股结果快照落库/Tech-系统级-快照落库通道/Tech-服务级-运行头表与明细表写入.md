# Tech-服务级-运行头表与明细表写入.md

1、【技术故事】 - 开发者（待填写）

系统级 Tech：Tech-系统级-快照落库通道（Story1.3.1 / REQ-003）。
职责：选股任务持久化写入：运行开始建 select_runs 头记录（running），结束写 select_results 明细与终态；唯一约束 (run_id, code, signal) 防重；失败保留头记录（error）。

2、【A/C验收条件】 - 开发者（待填写）

- select_runs 头记录字段齐全；owner_type/owner_id/trigger_type 由服务端生成，客户端不可覆盖。
- select_results 明细字段 + extra(JSON)（不含敏感信息）；同 run 内重复 (code, signal) 静默去重。
- 失败运行 status=failed、error 有值，已写明细保留；终态幂等。
- 数据写入 p95 ≤1s（REQ-003 非功能，单次运行规模内）。
- 验收：mock 固定信号集（含重复组合）→ 头/明细字段全对、重复去重、失败留痕。

3、【依赖与风险】 - 开发者（待填写）

- 依赖：REQ-002 触发、REQ-015 隔离（owner 规则）。
- 风险：写入失败（低）→与终态状态机协同重试，不产生重复数据。

4、【技术思路】 - 开发者（待填写）

待开发负责人填写；复用 repositories 双实现与命名规则（ch07 §7.5）。