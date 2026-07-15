# 07 · 安全

> 核心原则：核心机（NAS）和 GPU 零公网端口。公网入口是边缘机的 Caddy（自签 TLS + Basic Auth），核心机经 NAS→边缘 反向 SSH 隧道（autossh）回连。中转服务器（可选）只给 GPU 传话，被攻破只影响 GPU 通信。

## 1. 攻击面

```mermaid
graph TD
    Internet["互联网"]
    CF["边缘机 Caddy<br/>自签 TLS + Basic Auth（用户 flori）"]
    Host["核心机/NAS（零公网端口）<br/>所有敏感数据<br/>Claude 凭证<br/><i>在 NAT 内网，经反向 SSH 隧道回连边缘</i>"]
    Relay["中转服务器（可选）<br/>Redis (TLS) / MinIO (HTTPS)<br/>无敏感数据<br/><i>仅分层部署时需要，给 GPU 用</i>"]
    GPU["GPU 机器（可选）<br/>无敏感数据<br/><i>只出站连中转服务器</i>"]

    Internet -->|"HTTPS 请求"| CF
    CF -->|"反向 SSH 隧道（核心机 autossh 回连边缘）"| Host
    Host ---|"Redis TLS / MinIO HTTPS"| Relay
    GPU -->|"出站连接"| Relay
```

## 2. 各节点风险

### 主机 — 风险低

唯一外部入口是边缘机的 Caddy（自签 TLS + Basic Auth）；核心机零公网端口，靠 NAS→边缘 反向 SSH 隧道（autossh）回连，自身不监听任何公网端口。

| 威胁 | 风险 | 说明 |
|------|------|------|
| 边缘 Caddy 入口 | 低 | Basic Auth（用户 flori）+ 自签 TLS；/api/runner/* 走 per-worker Bearer |
| Claude 凭证泄露 | 低 | 文件权限 600，只在主机本地 |

主机敏感数据清单：
- `~/.claude/` — Claude OAuth 凭证
- `/data/jobs/` — 视频和笔记
- SQLite `credentials` 表 — 视频平台 cookies(Fernet 加密,分发镜像在 redis `cred:*`)

### 中转服务器 — 风险中（被攻破损失低）

仅分层部署（有 GPU）时存在。

| 威胁 | 风险 | 防护 | 被攻破后果 |
|------|------|------|-----------|
| Redis 未授权访问 | 高 | 强密码 + TLS + 禁危险命令 | 能往队列塞垃圾任务 |
| MinIO 文件泄露 | 中 | access key + HTTPS + 24h TTL | 看到临时视频文件 |
| SSH 暴力破解 | 中 | key-only + 非标准端口 | 控制中转（但无敏感数据） |

**中转被攻破最坏情况**：
- ❌ 不会泄露 Claude 凭证（在主机）
- ❌ 不会泄露平台 cookies（在主机）
- ❌ 不会影响用户访问（走边缘 Caddy + 反向 SSH 隧道，不经过中转）
- ⚠️ GPU Worker 断开（改 Redis 密码即可恢复）
- ⚠️ MinIO 临时文件泄露（24h 自动清理）

### GPU 机器 — 风险低

只出站连中转服务器。无敏感数据。Docker 容器隔离。

## 3. 认证体系

```
层级              认证方式                    保护对象
────────────────────────────────────────────────────────
边缘 Caddy        Basic Auth (用户 flori)      Web UI 入口 + /api/* 人面
API              Bearer Token                 API 端点
Redis            requirepass + TLS            任务队列
MinIO            access_key + secret + HTTPS  文件存储
SSH              key-only (禁密码)            服务器管理
```

### 边缘 Caddy Basic Auth

边缘机 Caddy 对人面入口（SPA + 非 runner 的 `/api/*`）全站 Basic Auth（用户名 `flori`，密码哈希存边缘 `.env` 的 `FLORI_BASIC_HASH`）。`/api/runner/*` 是机机接口、自带 per-worker Bearer，放行不挂 Basic（见 `deploy/edge/Caddyfile`）。

### API Bearer Token

即使边缘 Basic Auth 被绕过，应用层仍可用 API Bearer Token 兜底（`API_TOKEN`，只存核心机本地 `.env`）。注：边缘+反向 SSH 部署下 NAS 端 `API_TOKEN` 常置空、由 Caddy Basic Auth 把门；直连暴露 API 时务必设强随机 `API_TOKEN`。

> 边缘层继续负责 Basic Auth 防爆破；应用层另对 job 创建与上传使用 Redis 原子业务限流。
> API token 先常量时间校验，再以不可逆 principal 指纹计数；无鉴权模式按真实 ASGI client address
> 计数，不采信 `X-Forwarded-For`。Redis 不可用时 fail-closed 返回 503，不回落为单进程额度。
> API 默认零公网端口、仅绑本机(`API_BIND_IP`)；直接暴露到公网时仍必须设置强随机
> `API_TOKEN`，并保留边缘限流，不能把业务额度当作通用防爆破替代品。

**fail-closed(空 token 行为)**：未设 `API_TOKEN` 时后端不再静默放行,而是要求显式
`API_ALLOW_NO_AUTH=1` 才放行(仅限可信内网),否则受保护端点返回 `503 auth not configured`。
- compose 默认 `API_ALLOW_NO_AUTH=${API_ALLOW_NO_AUTH:-1}`(本机/可信内网开箱即用)。
- **公网或局域网直连暴露**:设强随机 `API_TOKEN`,并把 `API_ALLOW_NO_AUTH` 置 `0`。
- 注意:开启 `API_TOKEN` 后,前端反代(nginx/Caddy)需把该 token 作为 `Authorization: Bearer`
  注入到 `/api` 请求(否则前端 401)——这一步在前端/边缘配置侧完成。

## 4. 通信安全

| 链路 | 协议 | 加密 | 认证 |
|------|------|------|------|
| 用户 → 边缘 Caddy | HTTPS | TLS（自签，internal CA） | Basic Auth |
| 边缘 → 核心机 | 反向 SSH 隧道 | SSH | SSH key-only |
| 主机 → 中转 Redis | Redis TLS | TLS 1.2+ | requirepass |
| 主机 → 中转 MinIO | HTTPS | TLS 1.3 | access key |
| GPU → 中转 Redis | Redis TLS | TLS 1.2+ | requirepass |
| GPU → 中转 MinIO | HTTPS | TLS 1.3 | access key |
| 主机 → Claude API | HTTPS | TLS 1.3 | OAuth |

## 5. 任务注入防护

攻击者拿到 Redis 密码后能塞恶意任务：

```python
def validate_job_id(job_id: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9_-]{1,100}$', job_id))

def validate_step(step: str) -> bool:
    return step in VALID_STEPS  # 白名单

def validate_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    host = urlparse(url).hostname or ""
    return not any(host.startswith(b) for b in ["127.", "localhost", "10.", "192.168.", "172.16."])
```

Worker 执行前校验 job_id + step + url，不合法直接丢弃。

### 受控取证下载

案例取证中，模型只用 WebSearch 提交候选 URL，不能调用 Bash/curl 或直接决定可信度。服务端下载器执行以下硬门：

- 只允许无 userinfo 的 http(s)，每次跳转都重新规范化 URL 并解析 DNS。
- 解析出的全部地址必须是公网地址；private、loopback、link-local、multicast、reserved 和 unspecified 任一命中即拒绝。
- 生产请求固定连接已校验 IP，HTTPS 保留原 hostname 的 SNI 与证书校验，不使用环境代理，关闭 DNS 校验到连接之间的 rebinding 窗口。
- 只接收允许的文本 MIME，限制跳转次数、超时、单源与总字节；空正文拒绝。
- manifest 与当前 job、稳定 artifact 路径、hash 和 bytes 绑定。旧 schema、跨 job、重复 ID、路径逃逸、符号链接、低置信或文件篡改均不得进入 smart/review 权威上下文，也不得通过 API 暴露外链。

## 6. 密钥管理

| 密钥 | 存储位置 | 谁需要 |
|------|---------|--------|
| API Bearer Token | 主机 .env | API 服务 |
| Redis 密码 | 中转 .env + 主机 .env + GPU .env | 所有连 Redis 的组件 |
| MinIO access key | 中转 .env + 主机 .env + GPU .env | 调度器 + GPU Worker |
| 边缘 Basic Auth 哈希 `FLORI_BASIC_HASH` | 边缘 .env | 边缘 Caddy |
| 反向 SSH 隧道私钥 | 核心机 `deploy/tunnel/ssh/id_ed25519` | autossh 隧道 |
| Claude OAuth | 主机 ~/.claude/ | Claude Worker |
| 平台 cookies(B站/YouTube) | 主机 SQLite credentials(Fernet 加密)+ redis 分发镜像 cred:* | Download Worker(认领时经 runner API/redis 领取,审计 credential_issued) |
| FLORI_SECRET_KEY(凭证 at-rest 加密) | 主机 .env（只在宿主，不入库） | API/scheduler 进程 |

**原则**：Claude 凭证和平台 cookies 只在主机，不传到中转/GPU。

### B站 SESSDATA 的传播边界

扫码登录得到的 SESSDATA **不写进 `job.json`**（job.json 是会经对象存储/网关下发到远端 worker
的通用文档），而是写入该 job 的本机侧载文件 `input/.credentials.json`：
- `shared/storage.is_credential_file` 识别此文件，`RemoteStorage`/`GatewayStorage` **绝不上行/回传**它；
- `api/routes/runner.py` 的产物清单/读取端点对它一律**不列、404**，故远端 worker 取不到；
- 仅同机 `LocalStorage`（核心机的 Download Worker，本地直读 `/data/jobs/...`）能读到它；
- 公共/runner 产物 API 也隐藏它（`.` 前缀 + 专门过滤）。

### app_credentials 的 at-rest 加密

`app_credentials`（SESSDATA 等）现支持**静态加密**，由 `shared/db.py` 在写入/读取时
透明完成（en/decrypt 都发生在 API 进程，凭证写入由 B站登录、读取由 `jobs.py` 取
sessdata 触发）：

- 用 [`cryptography`](https://cryptography.io/) 的 **Fernet**（AES-128-CBC + HMAC，带版本/时间戳），不自造算法。
- 钥匙来自环境变量 **`FLORI_SECRET_KEY`**（一把 urlsafe-base64 的 32 字节 Fernet key）。
  生成一把：

  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

  把输出写进主机 **`.env`** 的 `FLORI_SECRET_KEY=...`（compose 已把它透传给 `api`/`scheduler`）。
- **钥匙只在 `.env`/宿主，绝不入库**——DB 里只有密文，单独拿到 SQLite 文件解不开。
- **务必备份这把 key**：丢了就解不开已存的凭证。但**爆炸半径很小**——凭证可重新登录
  （扫码）再取，丢 key 顶多需要重新登录一次，不丢业务数据。
- **向后兼容**：历史明文行读取时透明透传（Fernet 解密遇 `InvalidToken` → 原样返回），
  下次写入即自动加密；也可用 `scripts/reencrypt-credentials.sh --apply` 一次性批量重写。
  换 key 同理：旧 token 解不开则按透传处理，跑重写脚本即可在新 key 下重新加密。
- **未设 `FLORI_SECRET_KEY`**：凭证仍以**明文**落库（保持旧行为不阻断），并打印一次性
  告警提示设置该 key。`cryptography` 缺失或 key 非法时同样回退明文，不阻断启动。

**残留 / 待办**:无——凭证经 runner API 中心分发(docs/03 §1.7.1),远端 Download Worker
零预置,不再依赖本机 cookie 文件。

## 7. 应急预案

### 中转服务器被攻破

```
1. 改 Redis 密码 + MinIO 密钥（阻断所有连接）
2. 重建中转服务器（重装系统，5 分钟）
3. 用新密码更新主机和 GPU 的 .env
4. 重启主机和 GPU Worker
```

恢复时间：30 分钟。数据损失：零（全在核心机/NAS）。用户访问不中断（走边缘 Caddy + 反向 SSH 隧道）。

### 主机磁盘故障

定期备份 `/data/` 和 `/db/`。RAID 或 ZFS 快照做第一层保护。

## 8. 安全检查清单

```yaml
中转服务器 (如有):
  - [ ] SSH key-only + 非标准端口
  - [ ] 防火墙只开 Redis TLS 端口 + MinIO HTTPS 端口
  - [ ] Redis: requirepass + TLS + 禁 CONFIG/EVAL/SCRIPT
  - [ ] MinIO: 强密码 + HTTPS + bucket 级 policy
  - [ ] 自动安全更新

核心机 / 边缘:
  - [ ] 边缘 Caddy 自签 TLS + Basic Auth（用户 flori）正常
  - [ ] NAS→边缘 反向 SSH 隧道（autossh）连接正常
  - [ ] 边缘 SSH key-only；隧道私钥权限 600
  - [ ] Claude 凭证权限 600
  - [ ] cookies 权限 600
  - [ ] Docker no-new-privileges
  - [ ] 定期备份

GPU (如有):
  - [ ] Redis 密码通过环境变量（不写文件）
  - [ ] Docker --read-only + 内存限制
```
