// uniapp/src/api/upgrade.js
export const upgradeApi = (c) => ({
  latest: (query = {}) => c.get("/api/app/latest", { query }),
  version: () => c.get("/api/app/version"),
});
