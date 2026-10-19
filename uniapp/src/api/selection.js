// uniapp/src/api/selection.js
export const selectionApi = (c) => ({
  strategies: () => c.get("/api/v1/selection/strategies"),
  strategyDetail: () => c.get("/api/v1/selection/strategy-detail"),
  run: (strategies) => c.post("/api/v1/selection/run", { body: { strategies } }),
  runs: (query = {}) => c.get("/api/v1/selection/runs", { query }),
  runDetail: (jobId) => c.get(`/api/v1/selection/runs/${jobId}`),
  // 持仓的「当日榜内名次」：引擎对当天整个票池的名次，和名单里的 rank 不是一个口径。
  // 用默认 20 秒就够：这条链路不等打分——当日榜单没算好时引擎立刻回 ready=false 并自己起
  // 后台预热（放开全市场后冷打分要几百秒，放进请求里必然被超时打断，2026-10-18 曾因此放宽到
  // 60 秒，那个前提已经不存在了）。
  rank: (query = {}) => c.get("/api/v1/selection/rank", { query }),
});
