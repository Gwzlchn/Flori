import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// `just dev-web` proxies the API to the dev instance on 8781.
export default defineConfig({
  plugins: [vue()],
  // The page CSP allows fonts only from 'self', so small assets such as KaTeX's fonts must stay
  // files instead of data: URIs.
  build: { assetsInlineLimit: 0 },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8781",
    },
  },
});
