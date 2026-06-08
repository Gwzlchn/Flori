# 架构决策记录 (ADR)

> 每个重要技术选型一个文件。格式：背景 → 选项 → 决定 → 理由。

| ADR | 决定 | 日期 |
|-----|------|------|
| [0001](0001-language-python.md) | Python 3.11+ | 2026-05-16 |
| [0002](0002-queue-redis.md) | Redis (Sorted Set + Pub/Sub) 做任务队列 | 2026-05-16 |
| [0003](0003-storage-local-first.md) | 本地文件系统优先，MinIO 做远程 Worker 中转 | 2026-05-17 |
| [0004](0004-llm-multi-provider.md) | 多 Provider AI 网关（替代 Claude CLI） | 2026-05-17 |
| [0005](0005-frontend-vue3.md) | Vue 3 + Vite + Tailwind | 2026-05-16 |
| [0006](0006-gateway-cloudflare-tunnel.md) | Cloudflare Tunnel 做公网入口 | 2026-05-16 |
| [0007](0007-remote-worker-polling.md) | 远程 Worker 通过轮询 Redis 接入（已被 0009 取代） | 2026-05-17 |
| [0008](0008-search-sqlite-fts5.md) | SQLite FTS5 做全文搜索 | 2026-05-16 |
| [0009](0009-worker-gateway-outbound-https.md) | 远程 Worker 经出站 HTTPS 网关接入（取代 0007） | 2026-06-08 |
