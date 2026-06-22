# ADR-0005: Vue 3 + Vite + Tailwind CSS

## 背景

需要 Web 前端：手机投递+进度+笔记阅读，电脑分屏回放+标注。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| Vue 3 | 轻量、响应式好、移动端适配成熟 | 生态略小于 React |
| React | 生态最大 | 偏重、移动端需额外配置 |
| 纯 HTML + HTMX | 极简、服务端渲染 | 交互复杂时难写（WebSocket/视频播放） |
| Flutter Web | 跨端一致 | 体积大、中文渲染问题 |

## 决定

Vue 3 + Composition API + Vite + Tailwind CSS + Pinia。

## 理由

1. 手机优先场景，Vue + Tailwind 响应式方案成熟
2. Composition API 和 Python 思维方式接近
3. Vite 构建快，开发体验好
4. Tailwind 原子类不需要写 CSS 文件
5. WebSocket 进度推送 + video.js 播放器在 Vue 中集成简单

## 影响

前端独立 SPA，通过 API 与后端通信。静态文件由容器内 Nginx 托管，公网经边缘 Caddy（自签 TLS + Basic Auth，反向 SSH 隧道回连核心）反代（见 [ADR-0006](0006-gateway-cloudflare-tunnel.md)）。
