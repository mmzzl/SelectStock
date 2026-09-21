// uniapp/src/api/selection.js
export const selectionApi = (c) => ({
  strategies: () => c.get("/api/v1/selection/strategies"),
  run: (strategies) => c.post("/api/v1/selection/run", { body: { strategies } }),
  runs: (query = {}) => c.get("/api/v1/selection/runs", { query }),
  runDetail: (jobId) => c.get(`/api/v1/selection/runs/${jobId}`),
});
