// uniapp/src/api/client.js
export class UnauthorizedError extends Error {
  constructor(m = "未认证") { super(m); this.name = "UnauthorizedError"; }
}
export class PasswordExpiredError extends Error {
  constructor(m = "密码已过期") { super(m); this.name = "PasswordExpiredError"; }
}
export class BizError extends Error {
  constructor(message, code) { super(message); this.name = "BizError"; this.code = code; }
}

function defaultAdapter() {
  const u = globalThis.uni;
  if (u?.request) return {
    request: (cfg) => u.request(cfg),
    upload: (cfg) => u.uploadFile(cfg),
  };
  return {
    async request(cfg) {
      const r = await fetch(cfg.url, {
        method: cfg.method,
        headers: cfg.header,
        body: cfg.data == null ? undefined : (typeof cfg.data === "string" ? cfg.data : JSON.stringify(cfg.data)),
      });
      return { statusCode: r.status, data: await r.json().catch(() => ({})) };
    },
    upload() { throw new Error("环境无上传实现"); },
  };
}

// uni.request 传输层失败时 reject 的是 {errMsg:"request:fail ..."}，没有 message，
// 页面 catch 到就把 err 写成 undefined —— 用户看到的就是一片空白且没有原因。
function netError(e) {
  return new Error((e && (e.message || e.errMsg)) || "网络请求失败");
}

const GET_TIMEOUT_MS = 20000;
const RETRY_DELAY_MS = 800;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// App 端（Android JS 核心）没有 URLSearchParams 全局，且调用方普遍传默认空对象，
// 因此自己拼 query：空对象要得到 ""，undefined/null 一律跳过。
function toQuery(query) {
  const parts = [];
  for (const k of Object.keys(query)) {
    const v = query[k];
    if (v === undefined || v === null) continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

function handle({ statusCode, data }, { onUnauthorized }) {
  if (statusCode === 401) { onUnauthorized?.(); throw new UnauthorizedError(data?.message); }
  if (statusCode === 428) throw new PasswordExpiredError(data?.message);
  if (statusCode >= 400) throw new BizError(data?.message || `HTTP ${statusCode}`, data?.code);
  // App 端拿到 gzip/br 或 HTML 错误页时，uni.request 会把响应原样给回字符串，
  // 而 statusCode 还是 200——不拦就在这里静默变成「空白页 + 没有报错」。
  if (typeof data === "string") {
    try { data = JSON.parse(data); }
    catch { throw new BizError(`响应不是 JSON（${data.slice(0, 60)}）`, -1); }
  }
  if (data && typeof data === "object" && "code" in data) {
    if (data.code !== 0) throw new BizError(data.message || "请求失败", data.code);
    return data.data;
  }
  return data;
}

export function createClient({ adapter, getToken, onUnauthorized, baseUrl = "" } = {}) {
  const ad = adapter || defaultAdapter();
  return {
    async request(method, path, { query, body, header, silentUnauthorized, timeout } = {}) {
      const qs = query ? toQuery(query) : "";
      const token = getToken ? getToken() : "";
      // 登录接口自己的 401 是「用户名或密码错误」，不是「会话失效」。走全局处理会
      // clearSession + reLaunch 回登录页，把刚写上去的错误提示连同表单一起冲掉，
      // 用户看到的就是「点了登录，页面闪一下又空了」。
      const auth = silentUnauthorized ? null : onUnauthorized;
      const cfg = {
        url: `${baseUrl}${path}${qs}`,
        method,
        data: body,
        // 默认 20 秒够用；调用方可以按接口放宽（见 api/selection.js 的 rank：
        // 引擎冷缓存时那一次要给整个票池打分，实测 34.5 秒）
        timeout: timeout || GET_TIMEOUT_MS,
        header: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...header },
      };
      // 公网隧道（Cloudflare 边缘 + 移动网络）首包能到 1.5s 以上，偶发一次失败就让页面
      // 停在「暂无记录」再也翻不过来，所以 GET 补一次重试；POST 不重试（选股会重复建任务）。
      if (method.toUpperCase() !== "GET") {
        let res;
        try { res = await ad.request(cfg); } catch (e) { throw netError(e); }
        return handle(res, { onUnauthorized: auth });
      }
      let res;
      try {
        res = await ad.request(cfg);
      } catch (e) {
        await sleep(RETRY_DELAY_MS);
        try { res = await ad.request(cfg); } catch (e2) { throw netError(e2); }
      }
      return handle(res, { onUnauthorized: auth });
    },
    async upload(path, { filePath, file, name = "file", formData = {}, query, header = {} } = {}) {
      const qs = query ? toQuery(query) : "";
      const token = getToken ? getToken() : "";
      const res = await ad.upload({
        url: `${baseUrl}${path}${qs}`,
        // App 端只能给 filePath 字符串；H5 端 uni.uploadFile 单给 file 会直接 reject "file error"
        // （它只认 filePath 或 files:[{name,file}]），所以 File 必须走 files 数组。
        ...(filePath ? { filePath } : {}), ...(file ? { files: [{ name, file }] } : {}),
        name, formData,
        header: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...header },
      });
      let data = res.data;
      if (typeof data === "string") { try { data = JSON.parse(data); } catch { /* 透传 */ } }
      return handle({ statusCode: res.statusCode, data }, { onUnauthorized });
    },
    get(p, o) { return this.request("GET", p, o); },
    post(p, o) { return this.request("POST", p, o); },
    put(p, o) { return this.request("PUT", p, o); },
    del(p, o) { return this.request("DELETE", p, o); },
  };
}
