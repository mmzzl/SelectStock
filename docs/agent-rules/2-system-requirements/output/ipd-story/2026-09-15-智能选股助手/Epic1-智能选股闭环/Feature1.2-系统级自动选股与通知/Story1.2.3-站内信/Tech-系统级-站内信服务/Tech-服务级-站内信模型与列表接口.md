# Tech-服务级-站内信模型与列表接口.md

1、【技术故事】 - 开发者（待填写）

系统级 Tech：Tech-系统级-站内信服务（Story1.2.3 / REQ-009）。
职责：inbox_messages 模型（id/user_id/type/run_id?/title/content/read_status/created_at）与 `GET /api/v1/messages` 列表接口（按当前用户过滤 + 分页 + 返回未读数）。

2、【A/C验收条件】 - 开发者（待填写）

- 列表接口强按 user_id 过滤（REQ-015），越权不返回他人消息；分页正确无漏无重。
- 列表倒序返回；未读数含于响应；100+ 显示"99+"（客户端处理）。
- 事件覆盖 select_completed/select_failed（后续可扩展）。
- 非功能：列表查询 p95 ≤1s（PERF-02）；接口入 OpenAPI（OPN-01）。
- 验收：3 条消息 2 条未读 → 倒序展示、未读数=2；B 查 A 的消息 → 空/无权限。

3、【依赖与风险】 - 开发者（待填写）

- 依赖：REQ-008 写入、REQ-015 隔离。
- 风险：消息增长（低）→本期不自动清理。

4、【技术思路】 - 开发者（待填写）

待开发负责人填写；复用 repositories 双实现。