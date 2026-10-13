// uniapp/src/api/news.js —— 消息面：方向/强度/理由全由服务端 LLM 层产出，前端只渲染
export const newsApi = (c) => ({
  daily: (query = {}) => c.get("/api/v1/news/daily", { query }),
});
