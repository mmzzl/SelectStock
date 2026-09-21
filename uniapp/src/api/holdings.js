// uniapp/src/api/holdings.js
export const holdingsApi = (c) => ({
  list: (query = {}) => c.get("/api/v1/holdings", { query }),
  create: (body) => c.post("/api/v1/holdings", { body }),
  update: (id, body) => c.put(`/api/v1/holdings/${id}`, { body }),
  remove: (id) => c.del(`/api/v1/holdings/${id}`),
  pnl: (query = {}) => c.get("/api/v1/holdings/pnl", { query }),
});
