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
    command: python -m worker.main --pools io
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
    command: python -m worker.main --pools cpu
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
    command: python -m worker.main --pools ai
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

> **audio / 播客流水线需要 whisper-capable worker**：当前 `02_whisper` 明确路由到 `cpu` 池，
> worker 发布镜像已包含 `[gpu]` extra，可在无 GPU 时用 CPU int8 转写。GPU 机器要参与该步骤时应声明
> `--pools gpu cpu`；`gpu` 表示额外能力，`cpu` 才能认领当前 Whisper 步。池与步骤的真实映射以
> `configs/pipelines.yaml` 和 `configs/pools.yaml` 为准。

现行接入走 worker-gateway 单出站 HTTPS（见 [ADR-0009](adr/0009-worker-gateway-outbound-https.md)）：
worker 机只需能出站访问主机 API，不暴露入站端口、不直连 Redis/MinIO。

**worker 无状态**：id 由 `WORKER_NAME` 确定性派生(`{type}-sha256(name)[:8]`)、configs/prompts 在镜像、
产物经网关、凭证走 env —— **不挂 `/data` 卷**。唯一可选挂载是 GPU 的 whisper 模型 warm 缓存。

```bash
# 任意内网机,纯出站 HTTPS,无状态;按能力显式列出一个或多个 --pools:
docker run -d --restart unless-stopped \
  -e GATEWAY_URL=https://<主机域名> \
  -e GATEWAY_TLS_INSECURE=1 \                 # 自签/裸IP 网关需要;有受信证书可删
  -e WORKER_REGISTRATION_TOKEN=<管理页铸造的 flw- token> \
  -e WORKER_NAME=cpu-1 \                       # 确定性 id;同机多 worker 各给唯一名,不撞
  -e CONFIG_DIR=/app/configs \
  ghcr.io/${IMAGE_OWNER:-gwzlchn}/flori:latest \
  python -m worker.main --pools cpu
```

**凭证一律走 env**（管理页「接入新 Worker」会按能力池生成命令)：
- `ai`：`-e ANTHROPIC_API_KEY=<KEY>`（及 `DEEPSEEK_API_KEY` 等)。
- `io`(下载)：**零凭证预置**——B站/YouTube cookies 由中心在认领下载步时经 runner API
  下发(docs/03 §1.7.1),在管理页扫码/上传一次即全部 worker 生效。
- GPU 加速 Whisper：`--gpus all` + `--pools gpu cpu`；发布的 worker 镜像已包含 `[gpu]` extra；
  可选模型 warm 缓存(免每次重下)`-v whisper-cache:/cache -e MODEL_CACHE_DIR=/cache`。完全离线运行时先校验
  snapshot,再用 `WHISPER_MODEL_NAME=base` 和
  `WHISPER_MODEL_PATH=/cache/models--<owner>--<repo>/snapshots/<commit>` 绑定逻辑模型与绝对目录；
  两者必须匹配当前设备选择的模型,worker 才会直接加载该目录并跳过 Hugging Face Hub 元数据查询。

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
scripts/backup.sh
# 停止所有持有 SQLite 的旧后端，再按实际 profile 同批启动新版本。
docker compose stop api scheduler mcp-http worker-cpu worker-ai
docker compose up -d api scheduler mcp-http worker-cpu worker-ai
```

先用新版本的 DR 工具完成全资产备份，再停止所有持有 SQLite 的旧后端。同批启动同一版本的 api、scheduler、mcp-http 和 Worker 后，首个后端进程在跨进程锁内迁移，其余进程等待并复验；schema、ledger、readiness 和组件版本一致后才算升级完成。Redis 的持久状态不因容器重建而丢失。

### 6.1 SQLite 迁移门

- 非空旧库升级前会在 `db/migration-backups/analyzer.pre-v{from}-to-v{target}.db` 创建、校验并原子发布 migration safety snapshot。快照失败则不开始迁移；pending chain 任一步失败时，DDL、数据回填、ledger 和 `user_version` 在同一事务内整链回滚。
- safety snapshot 只覆盖 SQLite，不替代升级前包含 jobs、Redis、MinIO 和配置的完整 DR 备份。同一个版本区间重试会刷新同名安全快照，新建空库不创建。
- 在真实 `sqlite3.connect` 和 `PRAGMA journal_mode=WAL` 前，启动门会取得稳定的 DB/sidecar 视图；存在 WAL 或 hot journal 时，在隔离副本恢复最后 committed 状态。合法但陈旧或不可读的普通 SHM 仅是 advisory，不能掩盖 WAL 中的未来版本。
- 非法 WAL 或 hot journal、同时存在非空 WAL 与 rollback journal、空主库旁存在非空 sidecar、sidecar 为 symlink 或特殊文件，以及 WAL 路径连续三次无法取得稳定采样，都会 fail-closed。即使未来 schema 只存在于 committed crash WAL 中，也会在真实 DB/WAL 被改写或 SHM 被创建前拒绝启动。
- 成功迁移后，旧镜像可能因为不支持更高 schema 而拒绝启动；镜像回滚不能替代数据库兼容性判断。

## 7. 备份 / 恢复 / 磁盘回收

生产 compose 可使用命名卷或 bind mount。灾备脚本优先从运行中的 api、Redis、MinIO 容器发现真实 `/data` 挂载；也可用 `FLORI_DATA_DIR` / `FLORI_DATA_VOLUME`、`REDIS_DATA_DIR` / `REDIS_VOLUME`、`MINIO_DATA_DIR` / `MINIO_VOLUME` 显式指定。脚本只调用 Docker 一次性容器，宿主无需安装 Python、SQLite 或 Redis 工具，全部 `-h/--help` 可查。

`FLORI_SCHEMA_MANIFEST` 默认指向 tracked `shared/migrations/manifest.json`，backup、restore 和 drill 都强制需要。自定义路径必须来自同一可信发布，且同目录包含完整 migration package；DR 校验会加载并执行该 package，不得指向归档内文件或其它不可信代码。

NAS 可把 MinIO 嵌套挂在 data 根下；脚本会把该子树从 data 资产排除，再作为独立 MinIO 资产收录和切换，避免重复打包或恢复时误删挂载点。

### 7.1 备份 — `scripts/backup.sh`

生成覆盖完整持久状态的灾备代：data 根（SQLite、jobs、prompts/profiles、运行持久文件）、Redis、MinIO 与运行配置。SQLite 使用 online backup；运行中的 Redis 先 `SAVE` 得到一致性 RDB，再在隔离临时卷物化生产 `appendonly yes` 可直接加载的 AOF。新归档使用 DR format v2，每代包含 manifest、逐文件 SHA-256、模式/uid/gid、schema/version、不可变迁移历史及指纹、RPO 窗口和外部 `.sha256`。

```bash
scripts/backup.sh                                      # 输出到 ./backups/
scripts/backup.sh /mnt/nas/flori --result-file /mnt/nas/flori/backup-result.json
# 仅在已确认子树可重建时显式排除;参数可重复,路径相对对应资产根
scripts/backup.sh /mnt/nas/flori --minio-exclude .minio.sys/tmp
```

- 归档先写 `.partial`，资产稳定性、SQLite `integrity_check`、manifest 和全部摘要通过后才 `fsync + os.replace` 原子发布；同 generation 拒绝覆盖。
- 发布前还会验证源数据库 ledger、manifest 历史前缀，并在临时副本上执行生产迁移链。任一不一致时不发布归档及外部 `.sha256`。
- 备份不主动停应用；复制期间 data/MinIO 发生变化会 fail-closed，不发布混代归档。高写入期应在维护窗口重试。
- `workers/*/.cache` 是可重建的模型/下载缓存，可能包含上游工具维护的相对符号链接；归档固定排除该子树，但仍保留同一 worker 目录下的持久状态。其它符号链接继续 fail-closed。
- `--data-exclude` / `--minio-exclude` 是默认关闭的受控逃生口，路径分别按 data / MinIO 根解析、可重复，最终写入对应 asset manifest 的 `excluded_external_subtrees`。只能排除经确认可重建的精确运行缓存；禁止排除整个 `.minio.sys`，其中的格式、bucket 配置、IAM、版本和生命周期等元数据属于恢复资产。不得用排除参数绕过业务资产的稳定性失败。
- 完整 data 根会包含大源媒体，容量规划不能沿用旧版“只备 DB”的估算。需要缩短保留期时先用 `gc-jobs.sh` 管理可重下源文件。
- `BACKUP_RESULT_FILE` 输出机器可读状态；`FLORI_CONFIG_DIR=""` 可只跳过配置资产。真实 secret 值不得写进命令、日志或 tracked 配置。

cron 建议（每天 03:00 备份，保留最近 14 份）：
```cron
0 3 * * * cd /opt/flori && BACKUP_DIR=/mnt/nas/flori scripts/backup.sh \
  && ls -1t /mnt/nas/flori/flori-backup-*.tar.gz | tail -n +15 | xargs -r rm -f
```

### 7.2 恢复 — `scripts/restore.sh`

恢复会替换完整持久资产。脚本先只读校验外部摘要、归档成员、逐文件摘要、SQLite 完整性和 schema 兼容门；任何失败都在写目标前退出。校验通过后才停止全部目标挂载持有者，将各资产写入隐藏新代，全部预置完成后执行跨资产两阶段切换；accept 阶段开始前失败会反向回滚，进入 accept 后遵循下述统一前滚规则。

```bash
scripts/restore.sh <备份.tar.gz> --check                    # 只校验，不修改目标
scripts/restore.sh <备份.tar.gz>                            # 交互确认，成功后容器保持停止
scripts/restore.sh <备份.tar.gz> --yes --restart            # 无人值守恢复并重启本脚本停止的容器
```

- 默认停止所有实际持有 data/Redis/MinIO/config 目标的运行容器；任一停止失败即中止。`--no-stop` 只用于无运行持有者的隔离空环境，检测到 holder 仍会拒绝，不能绕过停写门。
- `RESTORE_CONFIG_DIR` 非空时才切换归档配置；默认只校验配置资产，不覆盖镜像内 `/app/configs`。
- 本地 migration manifest 默认决定可恢复 schema 范围；`FLORI_MAX_DB_USER_VERSION` 只能收窄上限，不能放宽。format v2 还要求归档迁移历史是本地清单的完全相同前缀，并在临时副本上通过生产迁移 runner。legacy format v1 可以没有 history，但仍需通过同一版本范围和启动 dry-run。
- accept 阶段开始前的切换错误会统一反向回滚；当前进程进入 accept 后，普通错误只尝试统一 roll-forward。进程重启后的持久决定以 marker 为准：全部仍为 `committed` 时回滚，任一资产进入 `accepted/finalizing` 后其余资产只能前滚。`finalizing` 表示新代已生效，只剩旧代或 stage 清理。
- result JSON 记录 generation、校验项、恢复资产、跳过资产、RTO 和待清理项。`cleanup_pending` 表示已提交后的清理待续；`commit_recovered_after_error=true` 与 `error_type` 表示 accept 阶段的原错误已由统一前滚恢复，不代表回滚。未传 `--restart` 时，验收后显式启动输出中列出的容器。
- marker 损坏或存在孤立 stage 时不得手工删除现场。修复 I/O 或权限后，在 holder 保持停止的条件下重跑同一 restore，由入口恢复逻辑完成回滚或 finalizing。

### 7.3 空环境灾备演练 — `scripts/dr-drill.sh`

```bash
DRILL_RESULT_DIR=/mnt/nas/flori/dr-evidence scripts/dr-drill.sh
scripts/dr-drill.sh --result-file /mnt/nas/flori/dr-evidence/latest.json
```

演练在一次性容器临时根创建 SQLite/job/profile/Redis/MinIO/config 样本，使用同一 migration manifest、registry 和 runner 创建并验证当前 schema，覆盖原子发布、损坏归档拒绝、空环境业务读取和跨资产故障回滚，输出实测 RPO/RTO。它不挂 Docker socket，也不挂载现有生产卷。发布前的 real-docker integration 还会用隔离真实 Redis/MinIO 卷恢复到全新目标；生产 `appendonly yes` Redis 必须读回 key，生产 MinIO client 必须逐字节读回 multipart 对象并保持 size、etag 与 user metadata。

### 7.4 磁盘回收 — `scripts/gc-jobs.sh`

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

### 7.5 日志轮转 + 健康检查（已在 Compose）

生产、开发、edge、remote worker 与 tunnel Compose 均内置容器加固：

- **Docker `local` logging driver**：生产和 edge/remote worker 默认 `10m x 3`，tunnel 为 `5m x 3`，开发为 `5m x 2`，全部启用压缩。常驻服务都必须显式配置上限；新增 Compose 服务时由配置不变量测试防止遗漏。
- **动态步骤容器与步骤文件日志**：`DockerStepRunner` 创建的临时容器同样显式使用 `local` driver `10m x 3`，不会因脱离 Compose 而失去上限。步骤 `step.log` 由 `FLORI_STEP_LOG_MAX_BYTES` 限制容量并原子保留尾部。
- **api liveness**：Compose healthcheck 与 Caddy upstream 探测 `/api/health/live`。它只说明 API 进程能够响应，不把 Redis、存储或 Worker 故障误判为容器死亡并触发重启风暴。
- **接单 readiness**：调度或流量门禁使用 `/api/health/ready`。磁盘不足、SQLite/Redis/中心存储不可写、scheduler 过期或 required pool 无可接单 Worker 时返回 503；可选能力离线只返回 200 degraded。完整字段和阈值见 `docs/03-contracts.md`。

### 7.6 版本固定 / 回滚 — `scripts/rollback.sh`

镜像标签已参数化为 `${IMAGE_TAG:-latest}`，CI 为受影响镜像的发布构建打 `:latest` + `:<git-sha>`，watchtower 跟 `:latest` 自动滚动。坏提交滚到生产时，固定到一个已知良好的 sha 即可回滚：

```bash
scripts/rollback.sh 76e8705                 # 回滚 api/scheduler/worker 到该提交镜像
scripts/rollback.sh 76e8705 api             # 只回滚 api
# 带 .local 覆盖的部署:
COMPOSE_FILES="-f docker-compose.yml -f .local/docker-compose.uptest.yml" scripts/rollback.sh 76e8705
```

固定到不可变的 `:<sha>` 标签后，watchtower 不会再把它滚到 `:latest`（标签不同）。恢复自动更新：重新用 `:latest` 部署（`docker compose up -d <服务>`）。

`rollback.sh` 只回滚镜像，目标镜像必须支持当前数据库 schema。若数据库已迁移到旧镜像不支持的版本，应在停写状态恢复完整 DR 备份；只有确认其它持久资产没有变化时，才可使用经过校验的 migration safety snapshot 回退 SQLite。不得强行启动不兼容的旧镜像。
