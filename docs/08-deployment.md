# 08 · 部署

> 从一键启动到完整三机部署，覆盖所有部署场景。

## 1. 部署场景

| 场景 | 机器 | 适用 |
|------|------|------|
| **单机局域网** | 任意一台机器 | 局域网内使用 |
| **单机 + 公网** | 同上 + 边缘机（Caddy + 反向 SSH 隧道） | 手机/外网访问 |
| **分层部署** | 核心机/NAS + 边缘机 + （可选 GPU/远程 worker） | 有边缘机或独立 worker 时 |

## 2. 单机部署

> 以下为说明性示例；**以仓库根目录的 `docker-compose.yml` 为准**（生产用预构建镜像
> `image: flori:latest`，开发态才 `build:` + 挂载源码，见 `docker-compose.dev.yml`）。

### docker-compose.yml

```yaml
services:
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    restart: unless-stopped

  api:
    build: ./api
    ports:
      - "8000:8000"
    volumes:
      - ${DATA_DIR:-./data}:/data
      - db_data:/db
    environment:
      - REDIS_URL=redis://redis:6379
      - DATA_DIR=/data
      - DB_PATH=/db/analyzer.db
      - API_TOKEN=${API_TOKEN}
    depends_on:
      - redis
    restart: unless-stopped

  scheduler:
    build: ./scheduler
    volumes:
      - ${DATA_DIR:-./data}:/data
      - db_data:/db
    environment:
      - REDIS_URL=redis://redis:6379
      - DATA_DIR=/data
      - DB_PATH=/db/analyzer.db
    depends_on:
      - redis
    restart: unless-stopped

  worker-io:
    build: ./worker
    command: python3 worker.py --type io
    volumes:
      - ${DATA_DIR:-./data}:/data
    environment:
      - REDIS_URL=redis://redis:6379
      - DATA_DIR=/data
    security_opt:
      - no-new-privileges:true
    depends_on:
      - redis
    restart: unless-stopped

  worker-cpu:
    build: ./worker
    command: python3 worker.py --type cpu
    volumes:
      - ${DATA_DIR:-./data}:/data
    environment:
      - REDIS_URL=redis://redis:6379
      - DATA_DIR=/data
    security_opt:
      - no-new-privileges:true
    deploy:
      resources:
        limits:
          memory: 4G
    depends_on:
      - redis
    restart: unless-stopped

  worker-ai:
    build: ./worker
    command: python3 worker.py --type ai
    volumes:
      - ${DATA_DIR:-./data}:/data
      # CLI 订阅用户:先 `scripts/seed-worker-home.sh <worker名>` 把凭证 seed 进该 worker 家目录
      # (${DATA_DIR}/workers/<worker名>/,每 worker 独立副本各自续期、无并发写冲突;CLI 会话
      # transcript 也落数据卷=纳管,agentic 全轨迹审计从这回收),再取消注释:
      # - ${DATA_DIR:-./data}/workers/<worker名>:/home/worker
      # (environment 需加 HOME=/home/worker;★不要直挂宿主 ~/.claude——不可控且并发续期会写坏凭证)
    environment:
      - REDIS_URL=redis://redis:6379
      - DATA_DIR=/data
      # API Key（按需配置，至少一个）
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY:-}
      - HTTPS_PROXY=${HTTPS_PROXY:-}
    security_opt:
      - no-new-privileges:true
    deploy:
      replicas: 2
    depends_on:
      - redis
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports:
      - "3000:80"
    depends_on:
      - api
    restart: unless-stopped

volumes:
  redis_data:
  db_data:
```

### .env.example

```bash
# === 必填 ===
API_TOKEN=your-random-64-char-token

# === AI Provider API Keys (至少配一个) ===
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# DEEPSEEK_API_KEY=sk-...
# GOOGLE_API_KEY=AIza...

# === 路径 ===
DATA_DIR=./data

# === 代理 (访问 AI API 需要，无需代理可留空) ===
# HTTPS_PROXY=http://host.docker.internal:7890  # 访问外部 API 时使用，视网络环境配置
```

### 一键启动

```bash
cp .env.example .env
# 编辑 .env，填入 API_TOKEN
docker compose up -d
# 访问 http://localhost:3000
```

### 本地目录订阅（`source_type=local_dir`）的监听目录

`local_dir` 订阅把宿主某目录当作来源：放进去的文件被枚举并经 `file://` 复制进 pipeline，无网络下载。compose 已把宿主 `${FLORI_INBOX_DIR}`（默认 `.local/inbox`，gitignored 本地区，避免运行时数据落仓库树）挂到 **api 与 worker-cpu 同一容器内路径 `/data/inbox`**（两端路径必须一致：api 跑枚举/扫描，worker 复制源文件，`file://` url 在 worker 容器内按该路径解析）。

- 用法：把文件丢进宿主 `${FLORI_INBOX_DIR}`，建订阅时填 `source_type=local_dir`、`source_id=/data/inbox`（**容器内**路径，不是宿主路径）。
- 换目录：在 `.env` 设 `FLORI_INBOX_DIR=/srv/my-inbox`（宿主绝对路径），容器内仍是 `/data/inbox`。生产建议设到数据盘、与 `FLORI_DATA_DIR` 同源（如 `/volume2/DATA/flori/inbox`），运行时数据统一不进仓库树。
- 安全：`file://` 分支绕过 SSRF 防护（本地文件非网络），`source_id` 是受信任的运维输入；个人工具 Basic Auth 场景风险可接受。挂载为只读（`:ro`）。

## 3. 加公网：边缘机 Caddy + 反向 SSH 隧道

核心机/NAS 在 NAT 内、零公网端口；由一台公网边缘机（如 ECS）跑 Caddy（自签 TLS + Basic Auth）做入口，核心机用 autossh 反向 SSH 隧道把自己的 api/redis/minio 暴露到边缘回环。配方在 tracked 的 `deploy/{edge,tunnel}`（`${ENV}` 模板 + `.env.example`），详见 `deploy/README.md` 与 [ADR-0009](adr/0009-worker-gateway-outbound-https.md)。（历史上曾计划用 Cloudflare Tunnel，见已 Superseded 的 [ADR-0006](adr/0006-gateway-cloudflare-tunnel.md)。）

```bash
# 1) 核心机/NAS 侧起反向 SSH 隧道（把 api/redis/minio 暴露到边缘回环）
cp deploy/edge/.env.example deploy/edge/.env   # 填 EDGE_HOST / EDGE_DOMAIN / MINIO_* / FLORI_BASIC_HASH
# 放 SSH 私钥到 deploy/tunnel/ssh/id_ed25519（本地，不入 git）
docker compose -f deploy/tunnel/docker-compose.tunnel.yml up -d

# 2) 边缘机起 Caddy（自签 TLS + Basic Auth，用户名 flori）+ 前端
scp deploy/edge/* 边缘:/opt/flori-edge/
ssh 边缘 'cd /opt/flori-edge && docker compose --env-file .env up -d'

# 3) 前端镜像走全自动 CD：git push → CI 建 ghcr 公开镜像 → 边缘 Watchtower(10s)自动 pull+重建（无手动推送）
#    根生产 compose 的 Watchtower 仍是 120s;edge 为了前端快速跟随 CI 单独设为 10s。
```

边缘 Caddy（`deploy/edge/Caddyfile`）：`EDGE_DOMAIN` 站点使用 `/opt/flori-edge/certs/<domain>.fullchain.pem` 与 `<domain>.key` 的可信证书；`EDGE_HOST`、`localhost`、`127.0.0.1` 站点使用 internal 证书作为 IP/本机入口。对人面入口（SPA + 非 runner 的 `/api/*`）全站 Basic Auth（用户名 `flori`，密码哈希 `FLORI_BASIC_HASH`）；`/api/runner/*` 是机机接口、自带 per-worker Bearer，放行不挂 Basic。`/mcp` 放行 Basic,由 NAS mcp-http 强制 Bearer。`/api/*` 和 `/mcp` 反代到反向 SSH 隧道口，前端反代到边缘本机 frontend 回环端口 8090。

### deploy/edge/.env 关键项

```bash
EDGE_HOST=your.edge.ip          # 边缘机公网 IP（也作无 SNI 默认站点）
EDGE_DOMAIN=flori.wiki         # 可信证书域名；证书文件需在 /opt/flori-edge/certs/
FLORI_BASIC_HASH=...            # caddy hash-password 生成的 flori 用户密码哈希
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
```

## 4. 分层部署：主机 + 中转 + GPU

### 中转服务器 docker-compose.yml

```yaml
services:
  redis:
    image: redis:7-alpine
    command: >
      redis-server
      --requirepass ${REDIS_PASSWORD}
      --tls-port 6380
      --port 0
      --tls-cert-file /tls/redis.crt
      --tls-key-file /tls/redis.key
      --tls-ca-cert-file /tls/ca.crt
      --rename-command CONFIG ""
      --rename-command EVAL ""
      --rename-command SCRIPT ""
      --appendonly yes
    ports:
      - "6380:6380"
    volumes:
      - redis_data:/data
      - ./tls:/tls:ro
    restart: unless-stopped

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    ports:
      - "9000:9000"
      - "9001:9001"
    environment:
      - MINIO_ROOT_USER=${MINIO_ACCESS_KEY}
      - MINIO_ROOT_PASSWORD=${MINIO_SECRET_KEY}
    volumes:
      - minio_data:/data
    restart: unless-stopped

volumes:
  redis_data:
  minio_data:
```

### GPU 机器启动命令

> **audio/播客流水线需要 whisper-capable worker**：`02_whisper` 步在 `gpu` 池，仅由 `--type gpu` 且装了 `[gpu]` 依赖（faster-whisper）的 worker 执行；该 worker 在无 GPU 的机器上用 CPU（int8）转写，较慢但可用。若集群无此 worker，含音频/无字幕视频的 job 会在约 90s 后 fail-fast 报「无可用 worker」而非永久挂起。默认 `docker compose up` 只起 download/cpu/ai worker，不含 whisper worker。

现行接入走 worker-gateway 单出站 HTTPS（见 [ADR-0009](adr/0009-worker-gateway-outbound-https.md)）：
worker 机只需能出站访问主机 API，不暴露入站端口、不直连 Redis/MinIO。

**worker 无状态**：id 由 `WORKER_NAME` 确定性派生(`{type}-sha256(name)[:8]`)、configs/prompts 在镜像、
产物经网关、凭证走 env —— **不挂 `/data` 卷**。唯一可选挂载是 GPU 的 whisper 模型 warm 缓存。

```bash
# 任意内网机,纯出站 HTTPS,无状态(--type 换 cpu/io/ai/gpu):
docker run -d --restart unless-stopped \
  -e GATEWAY_URL=https://<主机域名> \
  -e GATEWAY_TLS_INSECURE=1 \                 # 自签/裸IP 网关需要;有受信证书可删
  -e WORKER_REGISTRATION_TOKEN=<管理页铸造的 flw- token> \
  -e WORKER_NAME=cpu-1 \                       # 确定性 id;同机多 worker 各给唯一名,不撞
  -e CONFIG_DIR=/app/configs \
  ghcr.io/${IMAGE_OWNER:-gwzlchn}/flori:latest \
  python -m worker.main --type cpu
```

**凭证一律走 env**（管理页「接入新 Worker」会按类型自动生成命令)：
- `ai`：`-e ANTHROPIC_API_KEY=<KEY>`（及 `DEEPSEEK_API_KEY` 等)。
- `io`(下载)：**零凭证预置**——B站/YouTube cookies 由中心在认领下载步时经 runner API
  下发(docs/03 §1.7.1),在管理页扫码/上传一次即全部 worker 生效。
- `gpu`(whisper)：`--gpus all`，并需带 `[gpu]` extra 的镜像(`FROM …/flori:latest` + `RUN pip install ".[gpu]"`)；
  可选模型 warm 缓存(免每次重下)`-v whisper-cache:/cache -e MODEL_CACHE_DIR=/cache`。

一条命令接入，纯出站 HTTPS；删除 worker 即吊销其 token。除 GPU 模型缓存(可选)外,worker 本地无任何状态。
(可选只读文件)外，worker 不需任何持久化卷。

> 旧的「中转 Redis(TLS)+MinIO」直连模型见上方 compose，已被网关模型取代，仅在需要 worker 直连内部组件时保留。

## 5. 首次使用引导

```
1. docker compose up -d          → 全套服务启动
2. 浏览器打开 http://localhost:3000 (或公网域名)
3. 设置 → B站 → 扫码登录        → 解锁 1080P
4. 首页 → 粘贴 B站 URL → 投递    → 第一个任务开始处理
5. 等待 ~20 分钟                  → 查看笔记
```

## 6. 升级

```bash
git pull
docker compose build
docker compose up -d
# Redis 数据持久化，不丢任务状态
```

## 7. 备份 / 恢复 / 磁盘回收

生产 compose 用**命名卷**（`*_flori-data`、`*_redis-data`，前缀=compose 项目名，默认目录名 `flori`），数据不在宿主可见目录里。下面三个脚本（`scripts/`）通过一次性容器进卷操作，**无需在宿主装任何工具**，全部 `-h/--help` 可查、默认安全。

> 若把 `FLORI_DATA_DIR` 设成了绝对路径（bind-mount，如 NAS 上 `/volume1/DATA/Flori`），三个脚本会自动直接操作该目录，无需改参数。

### 7.1 备份 — `scripts/backup.sh`

把 **SQLite 库（`/data/db/analyzer.db`）** + **Redis 状态（dump.rdb / appendonly）** 打包成带时间戳的 `tar.gz`。

```bash
scripts/backup.sh                 # 输出到 ./backups/flori-backup-<ts>.tar.gz
BACKUP_DIR=/mnt/nas scripts/backup.sh    # 自定义输出目录
```

- **无需停服**：只读挂载命名卷拷数据；Redis 先尽力 `redis-cli SAVE` 落盘（容器不在则告警跳过）。
- **幂等**：每次产独立时间戳文件，不覆盖、不动源卷；可放 cron。
- 视频等大源媒体**不在**备份内（体积大、可重下）；要它们用 `gc-jobs.sh` 反向管理或单独拷 jobs 卷。

cron 建议（每天 03:00 备份，保留最近 14 份）：
```cron
0 3 * * * cd /opt/flori && BACKUP_DIR=/mnt/nas/flori scripts/backup.sh \
  && ls -1t /mnt/nas/flori/flori-backup-*.tar.gz | tail -n +15 | xargs -r rm -f
```

### 7.2 恢复 — `scripts/restore.sh`

从 `backup.sh` 产出的 tar.gz 把 DB + Redis 写回卷。**危险操作，会覆盖现有数据**，默认要求确认。

```bash
scripts/restore.sh ./backups/flori-backup-20260620-101500.tar.gz        # 交互确认(输入 YES)
scripts/restore.sh <文件> --yes                                          # 无人值守跳过确认
```

- 恢复前先校验 tar 含 `db/` 或 `redis/` 成员，不合格直接退出、绝不动卷。
- 默认会尝试 `docker compose stop api scheduler worker-*`（失败不致命，`--no-stop` 可关）；恢复后**由你手动** `docker compose up -d`（脚本不自动起，避免半途读写）。

### 7.3 磁盘回收 — `scripts/gc-jobs.sh`

**审计缺口修复**：单机 `LocalStorage.cleanup` 是 no-op，源媒体 `/data/jobs/<job_id>/input/source.*` 永久堆积、磁盘只增不减。本脚本按年龄回收，**默认只删大源媒体、保留笔记/图等产物**。

```bash
scripts/gc-jobs.sh                            # 干跑:列出 30 天前的源媒体(不删)
scripts/gc-jobs.sh --older-than 14 --apply    # 真删 14 天前的源媒体
scripts/gc-jobs.sh --what all --apply         # 删整个 job 目录(含笔记,谨慎)
scripts/gc-jobs.sh --min-free-gb 50 --apply   # 仅当 /data 剩余 < 50GiB 才回收
```

- **默认 `--dry-run`**：只算、只列、不删；必须显式 `--apply` 才落地。
- `--what source`（默认）只删 `jobs/*/input/source.*`；`--what all` 删整 job 目录。
- **永不碰 DB 或非 job 数据**，只在 `/data/jobs/<job_id>/` 下动手。
- 打印回收项数 + 字节数（GiB/MiB）。

cron 建议（每周日 04:00 回收 30 天前源媒体、磁盘紧张才动手）：
```cron
0 4 * * 0 cd /opt/flori && scripts/gc-jobs.sh --older-than 30 --min-free-gb 30 --apply
```

### 7.4 日志轮转 + 健康检查（已在 compose）

`docker-compose.yml` 已内置两项容器加固：

- **`logging:` json-file 轮转**（`x-logging` 锚点，单文件 10m × 3，应用于 api/scheduler/worker/redis）：默认 docker json 日志不轮转，长跑会撑爆磁盘。
- **api `healthcheck:`**：探 `/openapi.json`（始终免鉴权），配合 `restart: unless-stopped` 让异常容器被及时重启、watchtower 据此判活。

### 7.5 版本固定 / 回滚 — `scripts/rollback.sh`

镜像标签已参数化为 `${IMAGE_TAG:-latest}`，CI 每次提交都打 `:latest` + `:<git-sha>`，watchtower 跟 `:latest` 自动滚动。坏提交滚到生产时，固定到一个已知良好的 sha 即可回滚：

```bash
scripts/rollback.sh 76e8705                 # 回滚 api/scheduler/worker 到该提交镜像
scripts/rollback.sh 76e8705 api             # 只回滚 api
# 带 .local 覆盖的部署:
COMPOSE_FILES="-f docker-compose.yml -f .local/docker-compose.uptest.yml" scripts/rollback.sh 76e8705
```

固定到不可变的 `:<sha>` 标签后，watchtower 不会再把它滚到 `:latest`（标签不同）。恢复自动更新：重新用 `:latest` 部署（`docker compose up -d <服务>`）。
