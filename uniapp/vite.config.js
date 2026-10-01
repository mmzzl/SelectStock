import { defineConfig, loadEnv } from "vite";
import uni from "@dcloudio/vite-plugin-uni";

const uniPlugin = uni.default || uni;

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // dev 代理的后端；并行验证改动时可指向另一端口（VITE_PROXY_TARGET=http://127.0.0.1:8011）
  const target = env.VITE_PROXY_TARGET || "http://127.0.0.1:8010";
  return {
    plugins: [uniPlugin()],
    // 构建期配置一律走 VITE_* + import.meta.env（Vite 自动注入并静态替换）。
    // 注意：以前用 define 配 globalThis.__X__ 是无效的——define 只替换裸标识符，
    // 成员表达式不替换，导致 API 地址与版本号注入一直是空的。
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: true },
        "/ws": { target: target.replace(/^http/, "ws"), ws: true },
      },
    },
  };
});