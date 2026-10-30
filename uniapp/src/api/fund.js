// uniapp/src/api/fund.js —— 主力资金榜：窗口、求和、占比全在服务端算完，前端只渲染
export const fundApi = (c) => ({
  rank: (query = {}) => c.get("/api/v1/fund/rank", { query }),
});
