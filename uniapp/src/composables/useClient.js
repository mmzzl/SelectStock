// uniapp/src/composables/useClient.js
import { createClient } from "../api/client.js";
import { holdingsApi } from "../api/holdings.js";
import { diagApi } from "../api/diag.js";
import { tradesApi } from "../api/trades.js";
import { selectionApi } from "../api/selection.js";
import { messagesApi } from "../api/messages.js";
import { upgradeApi } from "../api/upgrade.js";
import { sectorApi } from "../api/sector.js";
import { loadSession, clearSession, setSession } from "../store/session.js";

const BASE = import.meta.env.VITE_API_BASE || "";

let _cached = null;
export function useClient() {
  if (_cached) return _cached;
  const client = createClient({
    baseUrl: BASE,
    getToken: () => loadSession().token,
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
  };
  return _cached;
}
