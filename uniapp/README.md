# 智能选股 UniApp 客户端

## 运行（H5，开发）
1. `npm install`（离线则跳过，纯逻辑测试仍可跑）
2. 三服务：backend 8000 / plugin 8001 / selector 8002（见 docs/deploy/2026-09-20-app.md）
3. `npm run dev:h5` → 打开本地地址（/api、/ws 代理 8000）

## 纯逻辑测试（无需安装依赖）
`npm test`（node --test：scaffold/guard/client/view/ws/version）

## 构建
- `npm run build:h5` → dist/build/h5（整体发布）
- `npm run build:app` → HBuilderX 云打包或本地打包；wgt 使用「制作wgt资源」

## 手动冒烟清单（双端）
- [ ] 未登录访问任一 Tab → 登录页；登录成功进持仓；退出回登录
- [ ] 交割单双步导入（上传/粘贴 → 预览行级错误 → 提交）持仓联动（样例：`scripts/samples/sample_deal.csv`，3 行全有效）
- [ ] 利润/持仓/选股/诊断：全部数值与后端/插件一致，页面无计算（R1-3）
- [ ] 消息未读 + WS 实时 + 断线重连按 latest_inbox_id 补齐
- [ ] H5 发布新版刷新可见；App wgt 热更/整包安装；forced 不可跳过
- [ ] 修改密码页按后端既有接口接线（/api/v1/auth/change-password）
- [ ] 桌面宽度下页面容器可读（PC 自适应收口）

## 端到端联调（U12 人工项）
- 三服务 + scheduler 启动后逐项勾上面清单；另验：选股触发→轮询 done→收到 `selection_completed`；诊断 run 明细与后端一致；定时任务（可临时调近时间）自动产生 run 与通知。

## 风险
- 离线装包：结构 + 纯逻辑可验证，UI 打包留待联网。
- 契约以 backend 实际代码为准（已修正：诊断明细路由为 `/api/v1/portfolio/diag/{run_id}`；`read_status: true` 表示未读）。
