import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // TODO: 后端联调时打开 —— 把 /api 与 /ws 代理到 FastAPI
      // '/api': 'http://localhost:8000',
      // '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
