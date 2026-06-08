# ADR-0009: 远程 Worker 经出站 HTTPS 网关接入

> 取代 [ADR-0007](0007-remote-worker-polling.md)。

## 背景

Worker 可能运行在无法被外部连入的内网机器上（内网 GPU 服务器、有 Claude 订阅的桌面机），只能出站访问外网。ADR-0007 让这些 worker 直连公网 Redis (TLS) 轮询队列、经 MinIO 中转文件，等于把 Redis 与 MinIO 两个有状态中心组件暴露到公网，攻击面与运维负担都偏大；且 worker 镜像要带 redis + minio 客户端。需要一种只暴露单一 HTTP 面、worker 仅出站即可接入的机制。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 轮询公网 Redis + MinIO 中转（ADR-0007） | worker 与本地 worker 代码一致 | 暴露 Redis + MinIO 两个有状态组件到公网 |
| 出站 HTTPS 网关（GitLab-runner 式） | 只暴露一个 HTTP 面，worker 仅出站；per-worker token 可吊销；产物经网关代理，中心存储不暴露 | worker 多一层认领/上报 transport |
| 消息队列 (RabbitMQ) | 专业可靠 | 多一个重量级公网组件 |

## 决定

远程 worker 经 API 上的 `/api/runner/*` 网关，用单条出站 HTTPS 接入：

1. **接入门禁**：持接入 token（`POST /api/workers/registration-token` 铸发，可复用可重置）调 `POST /api/runner/register`，服务端签发 per-worker token（仅返回一次）并单写 Redis liveness + DB 行。
2. **认领**：长轮询 `POST /api/runner/jobs/request`，服务端在窗口内反复 `claim_step`，认到即把 pipeline/domain/style_tags enrich 进 claim 返回；worker 无需回读 Redis。
3. **上报**：`/complete`·`/fail`·`/release`·`/progress`·`/usage` 端点，服务端代为执行编排写回。
4. **产物**：经 `GET/PUT /api/runner/jobs/{id}/artifacts[/{rel}]` 代理读写，MinIO/本地存储永不暴露给 worker（`GatewayStorage`）。
5. **心跳/下线**：`/heartbeat` 刷新存活并回发 draining 控制位，`/offline` 主动下线；per-worker token 随 worker 删除即吊销。

worker 进程按环境变量自适应三种模式（`worker/main.py`）：

| 模式 | 环境变量 | Redis/DB | 存储 | 认领 |
|------|---------|----------|------|------|
| 本地 / 单机 | 不设 `GATEWAY_URL` | 直连 | LocalStorage | RedisTransport |
| 混合 | `GATEWAY_URL` + `REDIS_URL` 都设 | 作内层兜底 | GatewayStorage | 走网关，redis 镜像一份 |
| 纯网关（真零隧道） | 仅设 `GATEWAY_URL` | 不连 | GatewayStorage | 全走网关 |

## 理由

1. worker 只能出站 → 单条出站 HTTPS 即可接入，无需公网 Redis/MinIO
2. 攻击面收敛到一个受控 HTTP 面；per-worker token 可单独吊销
3. 产物经网关代理 → 中心存储不对 worker 暴露，目录穿越在网关侧拦截
4. Tag 亲和性保留 → worker 注册时声明能力/排斥标签，认领按 token 授权池裁剪
5. 纯网关模式 worker 镜像不需 redis/minio 客户端

## 与其它 ADR 的关系

- 取代 ADR-0007（公网 Redis 轮询 + MinIO 中转）。
- 与 [ADR-0006](0006-gateway-cloudflare-tunnel.md) 正交：Cloudflare Tunnel 是**用户**公网入口，本 ADR 是**远程 worker**接入通路，两者互不取代。

## 影响

- 分层部署不再需要公网 Redis (TLS) + MinIO (HTTPS)，只需 API 可达。
- 纯网关 worker 镜像无需 redis/minio Python 包。
- API 承担 worker 控制面 + 产物代理；中心 Redis/MinIO 仅核心内部使用。
