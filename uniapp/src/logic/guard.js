// uniapp/src/logic/guard.js —— 纯函数，无 uni 依赖，便于 node --test
export const TAB_PATHS = [
  "pages/holdings/index",
  "pages/selection/index",
  "pages/fund/index",
  "pages/messages/index",
  "pages/mine/index",
];

export const PAGE_PATHS = [
  ...TAB_PATHS,
  "pages/login/index",
  "pages/holdings/edit",
  "pages/holdings/import",
  "pages/trades/index",
  "pages/profit/index",
  "pages/selection/history",
  "pages/sector/index",
  "pages/sector/board",
  "pages/sector/kline",
  "pages/news/index",
  "pages/news/feed",
  "pages/news/detail",
  "pages/diag/index",
  "pages/diag/detail",
  "pages/mine/password",
];

const FIRST_TAB = TAB_PATHS[0];
const LOGIN = "pages/login/index";
const PASSWORD = "pages/mine/password";

export function resolveRoute({ path, loggedIn, passwordExpired = false }) {
  if (passwordExpired && loggedIn && path !== PASSWORD) return { redirect: PASSWORD };
  if (!loggedIn) return path === LOGIN ? { ok: true } : { redirect: LOGIN };
  if (path === LOGIN) return { redirect: FIRST_TAB };
  if (!PAGE_PATHS.includes(path)) return { redirect: FIRST_TAB };
  return { ok: true };
}