// uniapp/src/api/news.js —— 消息面：方向/强度/理由全由服务端 LLM 层产出，前端只渲染
export const newsApi = (c) => ({
  daily: (query = {}) => c.get("/api/v1/news/daily", { query }),
  // 原始消息流（东财 10 秒线）：判定不在这一层，页面只展示源本身说了什么
  feed: (query = {}) => c.get("/api/v1/news/feed", { query }),
  item: (id) => c.get("/api/v1/news/item", { query: { id } }),
});

