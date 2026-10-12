// uniapp/src/api/sector.js —— 板块热力图：全部字段来自服务端预计算，前端只渲染
export const sectorApi = (c) => ({
  heat: (query = {}) => c.get("/api/v1/sector/heat", { query }),
  board: (boardCode, window) => c.get("/api/v1/sector/board", { query: { board_code: boardCode, window } }),
  kline: (code, days) => c.get("/api/v1/sector/kline", { query: { code, days } }),
});
