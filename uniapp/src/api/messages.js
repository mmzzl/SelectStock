// uniapp/src/api/messages.js
export const messagesApi = (c) => ({
  list: (query = {}) => c.get("/api/v1/messages", { query }),
  markRead: (id) => c.put(`/api/v1/messages/${id}/read`),
  markAllRead: () => c.put("/api/v1/messages/read_all"),
});
