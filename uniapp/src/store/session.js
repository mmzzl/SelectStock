// uniapp/src/store/session.js
import { resolveRoute } from "../logic/guard.js";

const K_TOKEN = "ss_access_token";
const K_REFRESH = "ss_refresh_token";
const K_ME = "ss_me";

let memory = { token: "", refresh: "", me: null };

export function setSession({ access_token, refresh_token, user }) {
  memory = { token: access_token || "", refresh: refresh_token || "", me: user || null };
  const u = globalThis.uni;
  if (u?.setStorageSync) {
    u.setStorageSync(K_TOKEN, memory.token);
    u.setStorageSync(K_REFRESH, memory.refresh);
    if (user) u.setStorageSync(K_ME, user);
  }
}

/**
 * 续期只换两份令牌，user 原样保留。
 * 走 setSession 会把它按「没传 user」处理成 null ——「我的」页用户名当场变空，
 * 等于每半小时续一次期就丢一次身份。服务端 /auth/refresh 的返回里本来就没有 user。
 */
export function setTokens({ access_token, refresh_token } = {}) {
  memory = {
    ...memory,
    token: access_token || memory.token,
    refresh: refresh_token || memory.refresh,
  };
  const u = globalThis.uni;
  if (u?.setStorageSync) {
    u.setStorageSync(K_TOKEN, memory.token);
    u.setStorageSync(K_REFRESH, memory.refresh);
  }
}

export function loadSession() {
  const u = globalThis.uni;
  if (u?.getStorageSync) {
    memory = {
      token: u.getStorageSync(K_TOKEN) || "",
      refresh: u.getStorageSync(K_REFRESH) || "",
      me: u.getStorageSync(K_ME) || null,
    };
  }
  return memory;
}

export function clearSession() {
  memory = { token: "", refresh: "", me: null };
  const u = globalThis.uni;
  if (u?.removeStorageSync) [K_TOKEN, K_REFRESH, K_ME].forEach((k) => u.removeStorageSync(k));
}

export function isLoggedIn() {
  return Boolean(loadSession().token);
}

export function guard(path, { passwordExpired = false } = {}) {
  return resolveRoute({ path, loggedIn: isLoggedIn(), passwordExpired });
}