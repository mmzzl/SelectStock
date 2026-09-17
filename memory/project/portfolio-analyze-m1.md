---
name: portfolio-analyze-m1
description: 持仓诊断 M1 幂等代理架构与红线（/api/portfolio/analyze + /api/portfolio/runs，双表）
type: project
---

## 事实记录

### 2026-09-17: 持仓诊断 M1 采用「代理插件 + 双表落库 + 幂等重放」

**触发场景**: 实现 M2（真实插件）、M3（鉴权收敛）或新增同类「代理外部插件并落库」的接口
**结论**:
- 路由：`POST /api/portfolio/analyze`（入参 AnalyzeBody: user_id/natural_day/holdings）、`GET /api/portfolio/runs`（按 user_id 隔离）。**不走 /api/v1**，挂在 main.py 的 `app.include_router(portfolio_router, prefix="/api")`，不挂密码到期网关。
- 幂等键：服务端对 holdings 规范化（排序 + code/name/quantity/cost_price 固定字段）算 `holdings_md5` 作 batch_id；键 (user_id, natural_day, batch_id) 唯一 → 命中即复用 run_id，不重复建 run/明细。
- 红线 R1 零算术：判定（sell/hold/add）与收益/市值 100% 收敛插件透传，后端只代理/落库/展示。插件不可用 → run.status=failed + BizError(502)。
- 双表：portfolio_diag_runs（头，run_id 主键）+ portfolio_diag_results（明细，唯一 (run_id,code)）。插件 results 原样透传落库，另在 run 头冗余 total_market_value/total_pnl 以支撑列表页。
- 同步代理：M1 成功后 status=completed；重放同键返回既有 run 的 results/summary（_summary_from_run 从 run 头 + 明细重建）。
**原因**: 方案文档 §2/§3 + 红线契约清单 R1/R2/R3；判定/收益留给插件领域模型，后端可独立演进。

### 2026-09-17: 插件客户端双模式（mock|http）

**触发场景**: 调 portfolio_plugin 或接线真实插件端口
**结论**: `services/portfolio_plugin.py`：`call_plugin_analyze(payload) → {results, summary}`。`config.plugin_mode` 默认 "mock"（确定性 sell/hold/add 轮换 + 收益快照，仅供契约测试）；置 "http" 走 httpx POST `{plugin_base_url}/api/portfolio/analyze`。测试 conftest 显式 `settings.plugin_mode="mock"`。
**原因**: M2 插件未实现前离线可跑契约；http 模式留好接口边界。