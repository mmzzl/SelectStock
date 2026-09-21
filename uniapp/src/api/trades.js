// uniapp/src/api/trades.js
export const tradesApi = (c) => ({
  previewFile: (filePath, name = "file") =>
    c.upload("/api/v1/trades/import/preview", { filePath, name }),
  previewText: (text) =>
    c.request("POST", "/api/v1/trades/import/preview",
      { body: text, header: { "Content-Type": "text/plain" } }),
  commit: (rows, batch_note = "") =>
    c.post("/api/v1/trades/import/commit", { body: { rows, batch_note } }),
  list: (query = {}) => c.get("/api/v1/trades", { query }),
  create: (row) => c.post("/api/v1/trades", { body: row }),
  remove: (id) => c.del(`/api/v1/trades/${id}`),
});
