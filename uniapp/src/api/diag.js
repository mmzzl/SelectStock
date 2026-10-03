// uniapp/src/api/diag.js —— 持仓诊断：手动触发 + 历史回看（属主由令牌决定）
export const diagApi = (c) => ({
  run: () => c.post("/api/v1/diag/run"),
  runs: (query = {}) => c.get("/api/v1/diag/runs", { query }),
  detail: (runId) => c.get(`/api/v1/diag/runs/${runId}`),
});
