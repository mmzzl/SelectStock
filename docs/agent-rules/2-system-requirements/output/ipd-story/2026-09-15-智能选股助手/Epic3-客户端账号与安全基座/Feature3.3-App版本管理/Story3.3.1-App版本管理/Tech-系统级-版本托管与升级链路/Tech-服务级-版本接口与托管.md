# Tech-服务级-版本接口与托管.md

1、【技术故事】 - 开发者（待填写）

系统级 Tech：Tech-系统级-版本托管与升级链路（Story3.3.1 / REQ-014）。
职责：后端 `GET /api/v1/app/version` 版本检查接口（version_code/version_name/platform/force/url/md5/size_bytes/release_notes/released_at/status）+ APK 托管目录；仅返回 status=enabled 的最新版。

2、【A/C验收条件】 - 开发者（待填写）

- 仅返回 enabled 的最新版本记录；disabled 不进入提示。
- 返回字段齐全（含 force/md5/size_bytes/release_notes）。
- 比较以 version_code（数值）为准，非 version_name 文本。
- APK URL 由托管目录生成，防任意路径/目录穿越。
- 非功能：版本检查 p95 ≤1s（PERF-02 子集）；接口为公开路由（登录前可用）；入 OpenAPI（OPN-01）。
- 验收：enabled 新版 + force=1 → 返回并强制不可跳过；disabled → 不返回。

3、【依赖与风险】 - 开发者（待填写）

- 依赖：既有版本记录 + 托管目录；用户提示流程在客户端。
- 风险：托管 URL 注入（低）→服务端生成校验。

4、【技术思路】 - 开发者（待填写）

待开发负责人填写。