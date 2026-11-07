import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// `just dev-web` proxies the API to the dev instance on 8781.
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8781",
    },
  },
});
