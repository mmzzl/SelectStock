# Tech-服务级-选股提交与状态机接口.md

1、【技术故事】 - 开发者（待填写）

系统级 Tech：Tech-系统级-异步选股执行链路（Story1.1.2 / REQ-002）。
职责：后端 `POST /select/run` 提交接口（≤2s p95 返回 run_id）+ running/success/failed 状态机 service + 任务结束落库触发（REQ-003/008）。

2、【A/C验收条件】 - 开发者（待填写）

- 入参勾选策略列表，空参 400；鉴权 401；并发校验（已有 running → 拒绝，REQ-016）。
- manual 运行 owner_type=user、owner_id 服务端生成；触发方式 manual。
- 状态机：running → success（写明细）/ failed（写 error）；终态幂等。
- 非功能：提交 p95 ≤2s（PERF-03）；task 30 分钟内完成（PERF-04）；开放接口入 OpenAPI（OPN-01）。
- 验收：mock 插件正常 → 提交返回 run_id、任务结束后状态 success、明细可查（REQ-004）。

3、【依赖与风险】 - 开发者（待填写）

- 依赖：REQ-001 策略、REQ-003 落库、REQ-008 事件、REQ-016 治理。
- 风险：长任务（中）→超时/重试由治理模块兜底。

4、【技术思路】 - 开发者（待填写）

待开发负责人填写；参考 select 模块 service + repositories 双实现。