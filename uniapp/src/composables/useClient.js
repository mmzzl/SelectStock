// uniapp/src/composables/useClient.js
import { createClient } from "../api/client.js";
import { holdingsApi } from "../api/holdings.js";
import { diagApi } from "../api/diag.js";
import { tradesApi } from "../api/trades.js";
import { selectionApi } from "../api/selection.js";
import { messagesApi } from "../api/messages.js";
import { upgradeApi } from "../api/upgrade.js";
import { sectorApi } from "../api/sector.js";
import { fundApi } from "../api/fund.js";
import { newsApi } from "../api/news.js";
import { loadSession, clearSession, setSession, setTokens } from "../store/session.js";

const BASE = import.meta.env.VITE_API_BASE || "";

let _cached = null;
export function useClient() {
  if (_cached) return _cached;
  const client = createClient({
    baseUrl: BASE,
    getToken: () => loadSession().token,
    // access 令牌只有 30 分钟，服务端 /auth/refresh 会连 refresh_token 一起换发。
    // 以前 App 只把 refresh 存在 storage 里从不调用，所以半小时一到就踢人回登录页。
    getRefreshToken: () => loadSession().refresh,
    onTokenRefreshed: (tokens) => setTokens(tokens),
    onUnauthorized: () => {
      clearSession();
      globalThis.uni?.reLaunch?.({ url: "/pages/login/index" });
    },
  });
  _cached = {
    client, setSession,
    holdings: holdingsApi(client),
    diag: diagApi(client),
    trades: tradesApi(client),
    selection: selectionApi(client),
    messages: messagesApi(client),
    upgrade: upgradeApi(client),
    sector: sectorApi(client),
    fund: fundApi(client),
    news: newsApi(client),
  };
  return _cached;
}
