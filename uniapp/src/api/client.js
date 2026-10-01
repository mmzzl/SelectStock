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
  if (data && typeof data === "object" && "code" in data) {
    if (data.code !== 0) throw new BizError(data.message || "请求失败", data.code);
    return data.data;
  }
  return data;
}

export function createClient({ adapter, getToken, onUnauthorized, baseUrl = "" } = {}) {
  const ad = adapter || defaultAdapter();
  return {
    async request(method, path, { query, body, header } = {}) {
      const qs = query ? toQuery(query) : "";
      const token = getToken ? getToken() : "";
      const res = await ad.request({
        url: `${baseUrl}${path}${qs}`,
        method,
        data: body,
        header: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...header },
      });
      return handle(res, { onUnauthorized });
    },
    async upload(path, { filePath, name = "file", formData = {}, query, header = {} } = {}) {
      const qs = query ? toQuery(query) : "";
      const token = getToken ? getToken() : "";
      const res = await ad.upload({
        url: `${baseUrl}${path}${qs}`,
        filePath, name, formData,
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
