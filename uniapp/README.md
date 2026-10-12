# 智能选股 UniApp 客户端

## 运行（H5，开发）
1. `npm install`（离线则跳过，纯逻辑测试仍可跑）
2. 三服务：backend 8010（默认 8000 被占，见 `../README.md` 文末）/ plugin 8001 / selector 8002（见 docs/deploy/2026-09-20-app.md）
3. `npm run dev:h5` → 打开本地地址（/api、/ws 代理 8010，可 `VITE_PROXY_TARGET` 覆盖）

## 纯逻辑测试（无需安装依赖）
`npm test`（node --test：scaffold/guard/client/view/ws/version）

## 构建
- `npm run build:h5` → dist/build/h5（整体发布）
- `npm run build:app` → dist/build/app，再按下面「安卓 App 出包」云打包；wgt 使用「制作wgt资源」

## 安卓 App 出包（只有 HBuilderX，无 Android SDK）
本机无 Android Studio/gradle，APK 只能走 **DCloud 云打包**。前置：`manifest.json` 里 `appid` 填 dev.dcloud.net.cn 创建的应用 ID（当前 `__UNI__9BE0E99`），`app-plus.distribute.android.packagename` 为字符串（`com.select.stock`）；DCloud 账号已**绑定手机号**，否则提交时报硬错误。

1. `npm run build:app`
2. **把产物拷到仓库外再导入**（不能直接打本仓库的 uni-app 项目）：
   `rsync -a --delete dist/build/app/ /data/work/hx-pack/select-stock/`
   原因有二：`cli pack` 对 CLI 创建的 uni-app 源码项目会跑 `uni build` 而漏掉 `-p app`，只编出 H5 资源，日志先「编译成功」再「项目编译失败」；`dist/build/app` 嵌在项目内，HBuilderX 会向上解析到父级 `uniapp` 源码项目。
3. 启动 HBuilderX GUI（`/home/home/下载/HBuilderX/HBuilderX`，5.26；`cli` 必须连着运行中的 GUI 才能用），然后导入并打包：
   ```bash
   cd /home/home/下载/HBuilderX
   ./cli project open --path /data/work/hx-pack/select-stock   # 列表里应是 select-stock(App)
   ./cli pack --project select-stock --platform android \
     --android.packagename com.select.stock \
     --android.androidpacktype 3 --ignoreWarnings true
   ```
   `androidpacktype`：0 自有证书 / 1 公共 / 2 DCloud 老版 / 3 云端证书。1、2 对新应用直接拒打，测试用 3；`--safemode`、`--config` 只适用于 uni-app 类型项目，App 类型不要用 `--safemode`。
4. 约 3 分钟返回**临时下载地址（限 5 次）**，立刻下载：
   `curl -sSL -o /data/work/hx-pack/select-stock.apk "<下载地址>"`；≤100MB 免费。
5. 验证地址注入是否进了包（构建期配置一律走 `VITE_*` + `import.meta.env`，见 `.env.production`）：
   `unzip -p select-stock.apk 'assets/apps/__UNI__9BE0E99/www/app-service.js' | grep -o 'baseUrl:"[^"]*" \|url:"w[^"]*"'`

真机注意：`VITE_API_BASE` 用的是公网域名 `https://www.luro.uno`（Cloudflare + `cloudflared` 隧道 → 本机 nginx 8080 → `:8010`），外网/流量下也能连，且 HTTPS 规避了 Android 9+ 的明文流量限制；隧道不可用时可临时改回 `http://192.168.10.83:8080`（须与后端同局域网）。**WS 注意**：保活分两层。服务端 uvicorn 默认每 20 秒发 WS **协议层** ping（`ws_ping_interval=20`，`websockets` 实现），浏览器和原生基座都会自动回 pong，所以隧道并不会因为页面安静被掐（2026-10-12 实测：客户端一帧不发也能连着 165 秒没断，之前记的「Cloudflare 100 秒空闲断开」在这条链路上不成立）。`logic/ws.js` 另外每 20 秒发一帧**应用层** `{"type":"ping"}`（`/ws/notify` 回 `pong`），价值是客户端自己发现半开连接——隧道悄悄断了不会触发 `onclose`，页面就再也不更新；连续 2 拍收不到 `pong` 就主动拆掉重连。**发送口径要小心**：uni 的 `SocketTask.send()` 先取 `options.data`（h5 与 App 运行时都是），裸 `WebSocket.send()` 只认字符串——传错边等于发出 `undefined`，首帧鉴权根本到不了服务端，表现是无限重连；`createNotifySocket` 用 `objectPayload` 区分两端，默认按 `globalThis.uni.connectSocket` 是否存在来判断。快速验证可跳过打包，菜单 运行 → 运行到手机或模拟器（标准调试基座）。HBuilderX 日志在 `~/.local/share/HBuilder X/.log`，`cli logcat pack` 常为空。

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
- 契约以 backend 实际代码为准（已修正：诊断历史/明细路由为 `/api/portfolio/runs`、`/api/portfolio/diag/{run_id}`（无 v1 前缀）；`read_status: true` 表示未读）。
