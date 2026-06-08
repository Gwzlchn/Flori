# ADR-0007: 远程 Worker 通过轮询 Redis 接入

> **Status: Superseded** — 被 [ADR-0009](0009-worker-gateway-outbound-https.md) 取代。本方案要求远程 worker 直连公网 Redis (TLS) 并经 MinIO 中转文件，等于把两个有状态中心组件暴露到公网。现行方案改为 worker 经 API 的 `/api/runner/*` 单条出站 HTTPS 接入（注册/认领/上报/产物代理），Redis/MinIO 不再对 worker 暴露。下文保留作历史记录。

Worker 可能运行在无法被外部连入的内网机器上（如内网 GPU 服务器、有 Claude 订阅的桌面机）。这些机器只能出站访问外网。需要一种统一机制让任意远程 Worker 接入系统。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 轮询公网 Redis | Worker 只需出站、与本地 Worker 代码完全一致 | 需要一台公网可达的 Redis |
| HTTP 长轮询 API | 不需要额外组件 | 主机若无公网 IP，仍需中转 |
| 消息队列 (RabbitMQ) | 专业可靠 | 多一个重量级组件 |

## 决定

远程 Worker 出站连接公网可达的 Redis (TLS) 轮询任务队列，文件通过 MinIO 中转。

本地 Worker 和远程 Worker 运行完全相同的代码，区别只在环境变量（`MINIO_URL` 有无）决定 StorageBackend 是 Local 还是 Remote。

> 如果主机有公网 IP，Worker 可以直连主机 Redis，不需要额外中转。

## 理由

1. Worker 只能出站 → 必须轮询一个公网可达的服务
2. Redis 已经是系统的队列组件 → Worker 复用同一套协议，零额外代码
3. StorageBackend 抽象 → Worker 不关心文件在本地还是 MinIO
4. Tag 亲和性 → 远程 Worker 声明自己的能力（gpu/vision/claude-cli），只接匹配的任务
5. Redis TLS + 强密码 + 禁危险命令 → 公网暴露风险可控

## 适用场景

| 场景 | Worker 类型 | 典型能力标签 |
|------|-----------|-------------|
| 内网 GPU 服务器 | gpu | `gpu`, `vision` |
| 有 Claude 订阅的桌面机 | ai | `vision`, `claude-cli` |
| 云端 CPU 实例 | cpu | `heavy` |
| 家里的旧电脑 | cpu | `text-only` |

## 数据流

```
核心提交任务:
  1. 核心 → 中转 MinIO: 上传输入文件
  2. 核心 → 中转 Redis: 推入对应池队列

远程 Worker 执行:
  3. Worker ← 中转 Redis: 轮询取任务（Tag 匹配）
  4. Worker ← 中转 MinIO: 下载输入文件（RemoteStorage.pull）
  5. Worker 本地执行步骤
  6. Worker → 中转 MinIO: 上传产物（RemoteStorage.push）
  7. Worker → 中转 Redis: 发布完成事件

核心收尾:
  8. 核心 ← 中转 Redis: 收到完成事件
  9. 核心 ← 中转 MinIO: 拉回产物到本地
```

## 影响

- 分层部署时需要一台公网服务器运行 Redis (TLS) + MinIO (HTTPS)
- Worker 镜像需要 `redis` + `minio` Python 包（本地部署时 minio 不需要）
- 中转被攻破只影响远程 Worker 通信，用户访问和核心数据不受影响
