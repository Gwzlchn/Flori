/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端单测配置(独立于 vite.config.ts,不带 dev server proxy)。
// 容器内跑:见 docker-compose.fe-test.yml(node:22-alpine,宿主不装依赖)。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary'],
      include: ['src/**/*.{ts,vue}'],
      // 入口/类型声明/测试自身不计入覆盖率分母
      exclude: ['src/main.ts', 'src/env.d.ts', 'src/**/*.d.ts', 'src/**/*.{test,spec}.ts'],
    },
  },
})
