// uniapp/src/api/trades.js
export const tradesApi = (c) => ({
  // target：App 端是 plus.io.chooseFile 返回的路径字符串（uni.chooseFile 在 App 端没实现），H5 端是 input 的 File 对象
  // password：加密 PDF 的打开密码，只在上传里带一次，服务端不落库
  // probe：App 端读文件的逐级诊断串，只进服务端日志和错误提示
  previewFile: (target, name = "file", password = "", probe = "") =>
    c.upload("/api/v1/trades/import/preview", {
      ...(typeof target === "string" ? { filePath: target } : { file: target }),
      name,
      ...((password || probe) ? {
        formData: { ...(password ? { password } : {}), ...(probe ? { probe } : {}) },
      } : {}),
    }),
  // App 读 SAF content:// 句柄时 uni.uploadFile 会静默丢掉文件段，这条完全绕开它
  previewBase64: (b64, filename = "", password = "", probe = "") =>
    c.post("/api/v1/trades/import/preview", {
      body: { file_b64: b64, filename, password, probe },
    }),
  previewText: (text) =>
    c.request("POST", "/api/v1/trades/import/preview",
      { body: text, header: { "Content-Type": "text/plain" } }),
  commit: (rows, batch_note = "") =>
    c.post("/api/v1/trades/import/commit", { body: { rows, batch_note } }),
  list: (query = {}) => c.get("/api/v1/trades", { query }),
  create: (row) => c.post("/api/v1/trades", { body: row }),
  remove: (id) => c.del(`/api/v1/trades/${id}`),
});
