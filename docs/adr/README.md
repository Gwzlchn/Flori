# 架构决策记录 (ADR)

> 每个重要技术选型一个文件。格式：背景 → 选项 → 决定 → 理由。

| ADR | 决定 | 日期 |
|-----|------|------|
| [0001](0001-language-python.md) | Python 3.11+ | 2026-05-16 |
| [0002](0002-queue-redis.md) | Redis (Sorted Set + Pub/Sub) 做任务队列 | 2026-05-16 |
| [0003](0003-storage-local-first.md) | 本地文件系统优先，MinIO 做远程 Worker 中转 | 2026-05-17 |
| [0004](0004-llm-multi-provider.md) | 多 Provider AI 网关（替代 Claude CLI） | 2026-05-17 |
| [0005](0005-frontend-vue3.md) | Vue 3 + Vite + Tailwind | 2026-05-16 |
| [0006](0006-gateway-cloudflare-tunnel.md) | Cloudflare Tunnel 做公网入口（已废弃：实际用 Caddy + 反向 SSH，远程 worker 接入见 0009） | 2026-05-16 |
| [0007](0007-remote-worker-polling.md) | 远程 Worker 通过轮询 Redis 接入（已被 0009 取代） | 2026-05-17 |
| [0008](0008-search-sqlite-fts5.md) | SQLite FTS5 做全文搜索 | 2026-05-16 |
| [0009](0009-worker-gateway-outbound-https.md) | 远程 Worker 经出站 HTTPS 网关接入（取代 0007） | 2026-06-08 |
| [0010](0010-review-feedback-loop.md) | AI 评审升级为闭环（三回路 + 修订红线 + 分期） | 2026-06-22 |
| [0011](0011-worker-runtime-orchestration.md) | Worker 运行时编排（暂停/恢复·per-worker 并发·12h 宽限·download→io） | 2026-06-22 |
| [0012](0012-case-evidence-authoritative-sources.md) | 案例取证/权威来源（fetch 判决书+报道·带引用·评审逐条核·改写 0010） | 2026-06-22 |
| [0013](0013-version-semver-build-sha.md) | 版本号 = 单一语义版本 + 构建短 sha,所有组件共用（递增:patch+1·逢10进位·大重构 major+1） | 2026-06-26 |
| [0014](0014-observability-and-job-dag.md) | 可观测体系 + 每个 job 的流水线 DAG（纯 CSS/SVG·pipelines.yaml 单一事实源） | 2026-06-26 |
