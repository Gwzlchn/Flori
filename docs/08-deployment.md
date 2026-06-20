# 08 · 部署

> 从一键启动到完整三机部署，覆盖所有部署场景。

## 1. 部署场景

| 场景 | 机器 | 适用 |
|------|------|------|
| **单机局域网** | 任意一台机器 | 局域网内使用 |
| **单机 + 公网** | 同上 + Cloudflare Tunnel | 手机/外网访问 |
| **分层部署** | 主机 + 中转服务器 + GPU | 有独立 GPU 机器时 |

## 2. 单机部署

> 以下为说明性示例；**以仓库根目录的 `docker-compose.yml` 为准**（生产用预构建镜像
> `image: mnemo:latest`，开发态才 `build:` + 挂载源码，见 `docker-compose.dev.yml`）。

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

  worker-download:
    build: ./worker
    command: python3 worker.py --type download
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
      # CLI 订阅用户取消下面的注释：
      # - ~/.claude:/home/user/.claude
      # - ~/.local/share/claude:/home/user/.local/share/claude:ro
      # - ~/.local/bin/claude:/usr/local/bin/claude:ro
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

## 3. 加公网：Cloudflare Tunnel

在主机 docker-compose.yml 中加一个 cloudflared 容器：

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    restart: unless-stopped
```

Cloudflare Dashboard 配置：
1. 创建 Tunnel，获取 Token
2. 配置路由：`video-notes.yourdomain.com` → `http://api:8000`
3. 开启 Cloudflare Access（邮箱验证）

前端静态文件也通过 API 服务返回（或单独配一条 Tunnel 路由到 frontend:80）。

### .env 追加

```bash
CLOUDFLARE_TUNNEL_TOKEN=your-tunnel-token
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
GPU 机只需能出站访问主机 API，不暴露任何入站端口、不直连 Redis/MinIO。

whisper 需 `[gpu]` 依赖(faster-whisper)，base 镜像未含——先构建一个带该 extra 的镜像：

```dockerfile
FROM ghcr.io/${IMAGE_OWNER:-gwzlchn}/mnemo:latest
RUN pip install --no-cache-dir ".[gpu]"
```

```bash
docker run -d --gpus all \
  --name mnemo-gpu-worker \
  -e GATEWAY_URL=https://<主机域名> \
  -e WORKER_REGISTRATION_TOKEN=<管理页铸造的接入 token> \
  -e IDLE_TIMEOUT=600 \
  --tmpfs /tmp:size=2G \
  --memory 8g \
  --security-opt no-new-privileges:true \
  <上面构建的镜像> \
  python -m worker.main --type gpu
```

一条命令接入，纯出站 HTTPS，空闲 10 分钟自动退出；删除 worker 即吊销其 token。

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

生产 compose 用**命名卷**（`*_mnemo-data`、`*_redis-data`，前缀=compose 项目名，默认目录名 `ai-knowledge-base`），数据不在宿主可见目录里。下面三个脚本（`scripts/`）通过一次性容器进卷操作，**无需在宿主装任何工具**，全部 `-h/--help` 可查、默认安全。

> 若把 `MNEMO_DATA_DIR` 设成了绝对路径（bind-mount，如 NAS 上 `/volume1/DATA/Mnemo`），三个脚本会自动直接操作该目录，无需改参数。

### 7.1 备份 — `scripts/backup.sh`

把 **SQLite 库（`/data/db/analyzer.db`）** + **Redis 状态（dump.rdb / appendonly）** 打包成带时间戳的 `tar.gz`。

```bash
scripts/backup.sh                 # 输出到 ./backups/mnemo-backup-<ts>.tar.gz
BACKUP_DIR=/mnt/nas scripts/backup.sh    # 自定义输出目录
```

- **无需停服**：只读挂载命名卷拷数据；Redis 先尽力 `redis-cli SAVE` 落盘（容器不在则告警跳过）。
- **幂等**：每次产独立时间戳文件，不覆盖、不动源卷；可放 cron。
- 视频等大源媒体**不在**备份内（体积大、可重下）；要它们用 `gc-jobs.sh` 反向管理或单独拷 jobs 卷。

cron 建议（每天 03:00 备份，保留最近 14 份）：
```cron
0 3 * * * cd /opt/mnemo && BACKUP_DIR=/mnt/nas/mnemo scripts/backup.sh \
  && ls -1t /mnt/nas/mnemo/mnemo-backup-*.tar.gz | tail -n +15 | xargs -r rm -f
```

### 7.2 恢复 — `scripts/restore.sh`

从 `backup.sh` 产出的 tar.gz 把 DB + Redis 写回卷。**危险操作，会覆盖现有数据**，默认要求确认。

```bash
scripts/restore.sh ./backups/mnemo-backup-20260620-101500.tar.gz        # 交互确认(输入 YES)
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
0 4 * * 0 cd /opt/mnemo && scripts/gc-jobs.sh --older-than 30 --min-free-gb 30 --apply
```

### 7.4 配套建议：日志轮转 + 健康检查（compose 侧）

脚本只管数据安全，**容器层面的两项加固建议在 `docker-compose.yml` 各服务上补**（由 compose 维护方添加，本节仅作推荐说明）：

- **`logging:` json-file 轮转**：默认 docker json 日志不轮转，长跑会撑爆磁盘。建议每服务加
  ```yaml
  logging:
    driver: json-file
    options: { max-size: "10m", max-file: "3" }
  ```
- **`healthcheck:`**：给 `redis`（`redis-cli ping`）、`api`（探 `/openapi.json`）加健康检查，配合 `restart: unless-stopped` 让异常容器被及时重启、`depends_on: condition: service_healthy` 保证启动顺序。

> 这两项不影响数据，纯运维健壮性；与上面三脚本互补。
