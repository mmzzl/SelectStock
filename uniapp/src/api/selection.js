// uniapp/src/api/selection.js
export const selectionApi = (c) => ({
  strategies: () => c.get("/api/v1/selection/strategies"),
  strategyDetail: () => c.get("/api/v1/selection/strategy-detail"),
  run: (strategies) => c.post("/api/v1/selection/run", { body: { strategies } }),
  runs: (query = {}) => c.get("/api/v1/selection/runs", { query }),
  runDetail: (jobId) => c.get(`/api/v1/selection/runs/${jobId}`),
  // 持仓的「当日榜内名次」：引擎对当天整个票池的名次，和名单里的 rank 不是一个口径。
  // 超时放宽到 60 秒：8003 冷缓存时这一次要现算全池因子（2026-10-18 实测 34.5 秒/299 只），
  // 默认 20 秒会在每天第一次查的时候必然失败。
  rank: (query = {}) => c.get("/api/v1/selection/rank", { query, timeout: 60000 }),
});
