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

### NAS大视频只读源库

NAS source library与`local_dir`的用途不同:`local_dir`是会复制进Job的入箱,
source library是不复制、不进MinIO、不随Job删除的大视频冷源。配置与启动:

```bash
# .env;路径必须是宿主绝对路径,只保留在本机配置
FLORI_SOURCE_LIBRARY_ENABLED=1
FLORI_SOURCE_LIBRARY_ROOT_ID=library
FLORI_SOURCE_LIBRARY_DIR=<absolute NAS video library path>

docker compose --profile distributed --profile source-library up -d
```

Compose把该目录以`:ro`挂到API和`worker-source`的`/sources/library`。API负责准入时full hash;
`worker-source` 以`--pools io cpu ai`运行,且只在真实能打开root时自报`source-root:<id>`。
08需要读取原字节生成来源清单,因此source Worker必须像普通AI Worker一样配置可用provider凭证;
没有匹配AI能力时该步会等待,不会绕过root约束投递到看不到原片的Worker。
使用`docker-compose.executor.yml`时还会把`FLORI_SOURCE_LIBRARY_DIR`作为DooD宿主路径,
把同一root以ro挂给嵌套step容器;因此该值不得用相对路径。

运维顺序是:先在源库外生成`relative_path + size_bytes + sha256`的manifest,再调
`POST /api/jobs`。准入会再读全文件验证,执行前Worker还会重验;未挂载/文件缺失时Job可以pending等待正确Worker,已挂载但内容改变时步骤fail-closed。已投递文件应保持不可变;替换内容必须用新digest创建新Job。

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
# 从当前实际 compose profile 取完整长期持有者清单,覆盖 base 与 NAS override worker。
# base部署使用 COMPOSE=(docker compose);NAS uptest使用下一行完整叠加命令。
COMPOSE=(docker compose -f docker-compose.yml -f .local/docker-compose.uptest.yml \
  --env-file .env --profile distributed)
mapfile -t FLORI_WRITERS < <("${COMPOSE[@]}" config --services \
  | grep -E '^(api|scheduler|mcp-http|worker-.*|.*-worker|nas-dl|foreign-dl)$')
"${COMPOSE[@]}" stop "${FLORI_WRITERS[@]}"
"${COMPOSE[@]}" up -d "${FLORI_WRITERS[@]}"
```

先用新版本的 DR 工具完成全资产备份，再停止所有持有 SQLite 的旧后端。同批启动同一版本的 api、scheduler、mcp-http 和 Worker 后，首个后端进程在跨进程锁内迁移，其余进程等待并复验；schema、ledger、readiness 和组件版本一致后才算升级完成。Redis 的持久状态不因容器重建而丢失。

### 6.1 SQLite 迁移门

- 非空旧库升级前会在 `db/migration-backups/analyzer.pre-v{from}-to-v{target}.db` 创建、校验并原子发布 migration safety snapshot。快照失败则不开始迁移；pending chain 任一步失败时，DDL、数据回填、ledger 和 `user_version` 在同一事务内整链回滚。
- safety snapshot 只覆盖 SQLite，不替代升级前包含 jobs、Redis、MinIO 和配置的完整 DR 备份。同一个版本区间重试会刷新同名安全快照，新建空库不创建。
- 在真实 `sqlite3.connect` 和 `PRAGMA journal_mode=WAL` 前，启动门会取得稳定的 DB/sidecar 视图；存在 WAL 或 hot journal 时，在隔离副本恢复最后 committed 状态。合法但陈旧或不可读的普通 SHM 仅是 advisory，不能掩盖 WAL 中的未来版本。
- 非法 WAL 或 hot journal、同时存在非空 WAL 与 rollback journal、空主库旁存在非空 sidecar、sidecar 为 symlink 或特殊文件，以及 WAL 路径连续三次无法取得稳定采样，都会 fail-closed。即使未来 schema 只存在于 committed crash WAL 中，也会在真实 DB/WAL 被改写或 SHM 被创建前拒绝启动。
- 成功迁移后，旧镜像可能因为不支持更高 schema 而拒绝启动；镜像回滚不能替代数据库兼容性判断。

### 6.2 Video 多 Part 离线切换（已退役）

schema v7 到 v8 的 Video 多 Part 切换是一次性离线迁移，需要同时搬 SQLite、对象键、根 `job.json` 和
Redis 执行身份。生产库已完成该迁移，配套的 `shared.multipart_migration` 工具随之退役，历史实现和
运行手册在 git 里。

启动门仍然保留：生产 compose 的 `FLORI_REQUIRE_OFFLINE_MIGRATIONS=1` 会拒绝让停在 v7 且仍有 Video
的旧库只迁数据库启动。真要复活这种库，先从 git 历史取回该工具，或在停写状态恢复对应年代的完整 DR
备份，不要绕过启动门。

## 7. 备份 / 恢复 / 磁盘回收

生产 compose 可使用命名卷或 bind mount。灾备脚本优先从运行中的 api、Redis、MinIO 容器发现真实 `/data` 挂载；也可用 `FLORI_DATA_DIR` / `FLORI_DATA_VOLUME`、`REDIS_DATA_DIR` / `REDIS_VOLUME`、`MINIO_DATA_DIR` / `MINIO_VOLUME` 显式指定。脚本只调用 Docker 一次性容器，宿主无需安装 Python、SQLite 或 Redis 工具，全部 `-h/--help` 可查。

`FLORI_SCHEMA_MANIFEST` 默认指向 tracked `shared/migrations/manifest.json`，backup、restore 和 drill 都强制需要。自定义路径必须来自同一可信发布，且同目录包含完整 migration package；DR 校验会加载并执行该 package，不得指向归档内文件或其它不可信代码。

NAS 可把 MinIO 嵌套挂在 data 根下；脚本会把该子树从 data 资产排除，再作为独立 MinIO 资产收录和切换，避免重复打包或恢复时误删挂载点。

### 7.1 备份 — `scripts/backup.sh`

生成覆盖完整持久状态的灾备代：data 根（SQLite、jobs、prompts/profiles、运行持久文件）、Redis、MinIO 与运行配置。SQLite 使用 online backup；运行中的 Redis 先 `SAVE` 得到一致性 RDB，再在隔离临时卷物化生产 `appendonly yes` 可直接加载的 AOF。新归档使用 DR format v2，每代包含 manifest、逐文件 SHA-256、模式/uid/gid、schema/version、不可变迁移历史及指纹、稳定 deployment ID、RPO 窗口和外部 `.sha256`。

```bash
export FLORI_DEPLOYMENT_ID=flori-nas-production          # 非密钥,同一部署保持不变
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
- 源资产默认以只读 bind 挂入备份容器。若只在已冻结的隔离副本上因 SQLite sidecar 无法打开，才可显式设
  `FLORI_BACKUP_SOURCE_MOUNT_MODE=rw`；禁止用它绕过生产停写或直接放宽生产源挂载。
- `BACKUP_RESULT_FILE` 输出机器可读状态,必须与 archive 和 `.sha256` 同目录,使线上导入能按 receipt 目录完成全链校验。`FLORI_CONFIG_DIR=""` 可只跳过配置资产。真实 secret 值不得写进命令、日志或 tracked 配置。
- `FLORI_DEPLOYMENT_ID` 是非密钥稳定实例标识。`backup.sh`和直接`dr_snapshot.py create`
  都拒绝空值或`unbound`;同一部署不得随版本、容器或主机重启变化。

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
- 破坏性恢复要求当前`FLORI_DEPLOYMENT_ID`与归档`deployment.id`一致;不一致时在创建
  目标目录和切换前拒绝。跨机克隆/迁移必须同时提供`--allow-cross-deployment`与
  `--confirm-cross-deployment REPLACE_OTHER_FLORI_DEPLOYMENT`;这两项是高风险双确认,
  不能作为日常恢复开关。`--check`只读校验不要求当前部署身份。
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

## 8. 两轨备份:exact DR 与 portable content backup

系统有**两套互不替代**的备份轨道。选错轨道比不备份更危险,因为它给出虚假的恢复
预期。判据只有一条:**你要恢复的是"某一时刻的运行环境",还是"业务内容本身"**。

| | exact DR(§7) | portable content backup |
|---|---|---|
| 入口 | `scripts/backup.sh` / `restore.sh` | `scripts/content-backup.sh` / `content-import.sh` / `content-gc.sh` |
| 收录 | SQLite + Redis + MinIO + `/data` 整卷 | 业务事实 + 失败审计 + **有效 manifest 证明的产物** |
| 含凭据 | **是**(必须受访问控制) | **否**(allowlist + secret scan 双门) |
| 含运行态 | 是(队列、租约、心跳) | 否 |
| 恢复语义 | 回到**那一刻的整机状态** | 在**当前 Schema** 上重建内容,状态由当前 pipeline 重投影 |
| 跨 Schema | 否(恢复旧 SQLite) | **是**(空库由当前 migration 创建) |
| 去重 | 否(整卷) | 是(内容寻址 CAS) |
| 典型场景 | 磁盘故障、实施前回滚点、误删回退 | 清库重建、迁移到新机、增量归档、复现某批 Job |

**边界(不要混用)**:
- portable import **不是回滚手段**。实施 portable 前必须先做一次 exact DR 并
  `restore.sh --check`(§2.10 阶段0)。
- exact DR **不能**跨 Schema 重建,也不能作为可共享的明文内容仓库。
- portable 仓库自身需要**至少两份物理副本**才算备份;CAS 只解决逻辑重复,
  不解决单盘故障。

设置页入口是 `/settings/recovery`。API 把宿主
`${FLORI_CONTENT_REPOSITORY_DIR}` 只挂到容器 `/content-repo`,必须与 `/data` 分离;
生产建议设为独立数据盘目录,并用 NAS snapshot/复制任务保留第二份物理副本。页面的
“创建增量备份”会在 API 容器内启动隔离子进程,不需要 Docker socket;关闭或重启 API
会中断该操作,下次进入页面会显示 `interrupted`。仓库无 TTL 写锁不会被页面自动清除,
必须确认旧进程已死后走 CLI 运维处理。
同一宿主目录不得同时bind到`/data`和`/content-repo`或`/tmp/flori-work`;备份启动
与子进程写前会按目录inode复验,发现别名将fail-closed且不创建operation/仓库。

“生成恢复交接”不是在线还原按钮。它会重算所选快照的全部 blob 摘要,并生成绑定 snapshot digest、
plan digest、稳定 deployment ID、固定 target generation 与当前版本的离线命令。相同交接
失败后必须原命令重跑,import journal 才能续接同一代际,不能另换时间戳。这样可以从浏览器发起准备,
又不会让仍持有 SQLite/产物共享租约的 API 清空自身数据库。真正执行仍按 §8.2 的 exact
DR、完整停写、重新 plan、移走旧库、`--into-live` 与恢复服务顺序完成。

### 8.1 只读备份

```bash
# 默认读命名卷 flori-data,增量;首次会全量哈希
install -d -m 700 /volume2/DATA/flori-backup-results
scripts/content-backup.sh --repo /volume2/DATA/content-repo \
    --result-file /volume2/DATA/flori-backup-results/last-backup.json

# 仓库自洽性校验(不证业务正确性)
scripts/content-backup.sh --repo /volume2/DATA/content-repo --verify
```

**日常增量已覆盖全部文本密钥扫描**。`_incremental_hit`只跳过命中CAS的二进制
输出;`.json/.md/.txt/.html/.srt`等文本仍在每轮完整重读、核对摘要并应用当前扫描规则。
因此新扫描规则会由下一次普通备份自动覆盖已有有效文本,不需要先跑full-rehash。

`--full-rehash`是全介质摘要/位腐蚀审计:它会额外重读视频等大二进制CAS。按独立
scrub周期或怀疑存储损坏时执行,不要把它加入每次增量任务:

```bash
scripts/content-backup.sh --repo /volume2/DATA/content-repo --full-rehash \
    --result-file /volume2/DATA/flori-backup-results/full-media-scrub.json
```

这一趟可能很慢(大视频仓库会全量重读)。普通备份同样会对文本命中密钥整次
fail-closed并给出路径,审阅后用
`--allow-secret-blob-file` 放行;放行项会写进 `snapshot.policy.secret_scan_exceptions`
并把 `secrets_included` 置真,跟着快照走。扫描是有界内存的全文件流式扫描,含跨块
匹配窗口;`stats.blob_scans_truncated` 必须为 0。

NAS 多 Part 默认仍是外部引用。要求恢复时不再依赖原媒体目录,备份时显式 vendoring:

```bash
scripts/content-backup.sh --repo /volume2/DATA/content-repo --vendor-media \
    --source-root nas-main=/volume2/DATA/media \
    --result-file /volume2/DATA/flori-backup-results/vendor-media.json
```

工具按 Part 的 `source_ref/source_digest/size_bytes` 读取并核验,再把视频收入 CAS。
不 vendoring 时必须保留 snapshot `completeness.external_media_roots` 列出的 source root,
并在导入时逐一映射。路径本身不是身份,摘要才是。

生产侧真正零写的做法(推荐,尤其在不停服时):先用 `:ro` 把 SQLite 三件套
(`analyzer.db`/`-wal`/`-shm`)复制到隔离 scratch,再对副本备份,`jobs/` 全程 `:ro`。
不停服时备份会与 scheduler/worker 并发,M1/M2 协议(§2.7-4)会 fail-closed 而不是
把竞态吞成"增量",因此**并发下失败或覆盖率偏低是预期结果,不要放宽门去换好看的数字**。

验收指标看 result JSON 的 `stats`:
- `manifests_seen` / `manifests_missing` / `terminal_steps` —— manifest 覆盖率。
  存量 Job 在 2.2.x 之前没有 manifest,**低覆盖率是预期**,由 04 的 backfill 解决。
- `unknown_paths` 必须为 0。`--allow-unknown-file` 或 `--allow-unknown` 只允许产出诊断
  快照,不会把省略的未知字节伪装为完整闭包;这类快照仍是 `portable_ready=false`。
- `external_source_parts` / `nas_source_roots` —— NAS 引用型 Part 不自带媒体字节,
  恢复时依赖对应 source root 仍然在位。
- `excluded_reasons` —— 排除原因分布。
- `completeness.portable_ready` —— 只有它为 true 且 `readiness_reasons=[]`,快照才具备
  manifest、配置、扫描和媒体的完整闭包。统计看起来接近 100% 不能替代这个布尔门。

### 8.2 清库恢复完整流程

> **先判定当前快照,不要把历史演练数据当成现状。** 只有 v2 snapshot 且
> `completeness.portable_ready=true` 才能单门进入线上恢复。v1 或任何不完整 v2
> 可以在隔离目标做检查;写线上必须再显式提供
> `--allow-incomplete-portable-snapshot` 并设置
> `FLORI_ACCEPT_INCOMPLETE_PORTABLE=1`,两道高风险确认缺一即拒绝。无论哪种情况,
> 阶段0 的 exact DR 兜底都不可省略。

```bash
# 0) 前置:稳定部署身份 + exact DR 兜底(portable 不是回滚手段)
export FLORI_DEPLOYMENT_ID=flori-nas-production
install -d -m 700 /mnt/nas/flori /mnt/nas/flori/import-results
scripts/backup.sh /mnt/nas/flori --result-file /mnt/nas/flori/dr.json
scripts/restore.sh /mnt/nas/flori/flori-backup-<gen>.tar.gz --check

# 1) 只读预演:确认 portable_ready、配置/来源映射、真实写入字节与冲突
scripts/content-import.sh --repo <repo> --db /data/db/analyzer.db \
    --config-root /data/import-staging/prompts \
    --source-root nas-main=/mnt/nas/media --plan

# 2) 停写:API/scheduler/MCP/全部本地worker都持有共享namespace lease。NAS必须复用
#    活栈的完整叠加参数,再从真实config枚举base与override服务。
COMPOSE=(docker compose -f docker-compose.yml -f .local/docker-compose.uptest.yml \
  --env-file .env --profile distributed)
mapfile -t FLORI_WRITERS < <("${COMPOSE[@]}" config --services \
  | grep -E '^(api|scheduler|mcp-http|worker-.*|.*-worker|nas-dl|foreign-dl)$')
"${COMPOSE[@]}" stop "${FLORI_WRITERS[@]}"
export FLORI_REMOTE_WORKERS_QUIESCED=1
export FLORI_DR_RECEIPT=/mnt/nas/flori/dr.json

# 3) 导入到全新库(绝不复制快照的 schema_migrations)
scripts/content-import.sh --repo <repo> --db /data/db/analyzer.db --into-live \
    --config-root /data/prompts --source-root nas-main=/mnt/nas/media \
    --target-generation gen-$(date -u +%Y%m%dT%H%M%SZ) \
    --result-file /mnt/nas/flori/import-results/last.json

# 4) 恢复刚才枚举的全部服务。scheduler 做纯 CPU 的索引/概念投影;未完成任务保持
#    pending_activation,不会被自动 enqueue。逐个审核后从 UI 或 API 激活。
"${COMPOSE[@]}" up -d "${FLORI_WRITERS[@]}"
curl -X POST http://127.0.0.1:8000/api/jobs/<job-id>/activate
```

base单文件部署把`COMPOSE`数组简化为`(docker compose)`。不要手写
`worker-cpu worker-ai`代替枚举;NAS override还可能包含`claude-worker`、
`nas-cpu-worker`、`nas-dl`等服务。
维护租约是漏停时的最后拒绝门,不是缩短停写清单的理由。跨机worker仍按
`FLORI_REMOTE_WORKERS_QUIESCED`人工确认。

`mcp-http`默认构造路径在打开Database/storage前取得与API/scheduler/worker相同的
共享维护租约,ASGI初始化失败、lifespan退出都会先关DB再释放。仓库内直接打开
Database/create_storage的backfill、merge、purge、reencrypt和manifest migration脚本
都是一次性离线工具;运行它们前也必须用同一清单停写,不得与portable import/restore
并行。长期在线入口只有API、scheduler、worker与mcp-http,四类均受租约约束。

`--repo`、`--result-file`、`--source-root` 都是宿主路径;result 父目录和 source root
必须在命令前由操作者预先创建。wrapper 会绑定并复验这些目录的 `dev:ino`,拒绝
symlink、执行期换挂载及任何与 portable 仓库重叠的来源目标。线上导入还会确认 exact
DR 的 `deployment.id` 与当前 `FLORI_DEPLOYMENT_ID` 一致,并逐目标检查 DB/jobs/config/
MinIO 都被归档且没有相关排除子树。exact DR 的 result 必须与 archive/sidecar 同目录;
只有同一部署的可回滚资产才能授权写入。

导入后只有一个 `jobs.status`,没有 `original_status/runtime_status` 双真源。完整且当前
定义仍匹配的 Job 投影为 `done`;需要继续的 Job 统一为 `pending_activation`。激活 API
先检查当前 pipeline 的可用 Worker,再以 DB CAS 转成 `pending` 并发出生命周期命令。
首次 CAS 同事务写入恢复激活 receipt。重复请求只有在任务已是 `pending` 且该 receipt
存在时才幂等补发命令;普通 `pending` 任务会拒绝,不会借恢复入口重置或创建第二份状态。

**恢复后的索引与概念补齐(重要,分两段)**:

导入**刻意不写** `notes_fts5`。补齐由 scheduler 既有的幂等通道负责:
`JobFinalizer.reconcile_completion_effects`(`scheduler/job_finalizer.py`)由后台周期
循环触发,谓词是纯 SQL 的 `list_unindexed_done_jobs`(`status='done'` 且 `notes_fts5`
里没有该 job),**不依赖 Redis**,因此对刚恢复的库直接生效。

> 这里有一个必须记住的反直觉点:**导入侧一旦先把 `notes_fts5` 填上,上面那个谓词
> 就永远为假**,scheduler 再也不会认领这些 Job,`canonical_evidence` 于是永久为空。
> 所以"导入不建 FTS"不是偷懒,是正确的所有权划分。import 的 result JSON 里
> `projection.search.awaiting_backfill` 列出待补齐 Job,`owned_by` 标明归属。

启动 scheduler 后应观察到 `notes_fts5` / `note_chunks` / `canonical_evidence`
由空变为有数据。canonical evidence 就绪后,同一 reconcile 通道从当前证据做
`concept_occurrences` 的确定性纯 CPU 重放。它不调用 LLM、不改 glossary 定义,
也不要求重跑概念步骤。来源字节摘要和投影摘要与 occurrence 全量替换在同一事务内
CAS 发布;重建 canonical evidence 的索引事务会先失效旧 marker,重复来源在新证据
对账后才重新成为 no-op,并发旧来源不能覆盖较新的投影。

> **补齐会卡在哪(P4 演练实测)**:补齐要求 manifest 声明过的产物**全部**在恢复库里。
> 若某个证据复算依赖没被任何步骤声明为 output,它就不进快照,恢复后
> `build_canonical_evidence_records_with_reader` 抛 `support artifact is missing`,
> 该 Job 每拍重试且**永不收敛**。首次演练 11 个待补齐 Job 里有 7 个因
> `intermediate/pdf_page_support.json` 漏声明而卡死(已在 03_structure 补声明)。
> 那条 support_artifact 契约测试只覆盖 document/03_structure 一个步、一类字段,
> **守不住这一类问题**;跨流水线的泛化对账在
> `tests/test_artifact_declaration_contract.py`(契约回读路径、编排面直写路径、
> 下载步扩展名、合并步 assets),它才是这一类的防线。
> 排查顺序:看 scheduler 日志里的 `completion_effect_failed` -> 确认缺哪个文件 ->
> 回查该文件是否在产出步骤的 `outputs` 声明内。补声明会让相关步骤 manifest 变
> stale 并触发重跑,这是预期行为。

### 8.3 回滚路径

- **切换前失败**:丢弃新库与 import staging,原环境未被触碰(§2.10 阶段5)。
  journal 是独立文件(`/data/content-import/journal.sqlite3`),**不放在目标库目录内**,
  因此丢弃目标库不会连崩溃证据一起删,可用 `--list-imports` 排查。
- **中断**:同 `--target-generation` 重跑即续跑;目标库被丢弃重建后绑定指纹会变,
  工具会拒绝沿用旧进度并要求新 generation(防"空库报成功")。
- **切换后发现问题**:停写,用阶段0 的 exact DR 恢复。

### 8.4 磁盘回收(GC)

```bash
scripts/content-gc.sh --repo <repo> --mark                 # 只读,看可达集合
scripts/content-gc.sh --repo <repo> --sweep                # 默认 dry-run
scripts/content-gc.sh --repo <repo> --sweep --apply --grace-days 7
scripts/content-gc.sh --repo <repo> --scrub                # 全量重算完整性
```

保留集合 = `latest` + 每月锚点 `monthly-YYYY-MM`(备份成功自动建,当月不覆盖)+
手工 named refs + 最近 N 条 receipt 引用。sweep 与 backup 互斥(同一把写锁),
import 只读仓库不被阻塞;dry-run 不取锁,只读挂载也能跑。grace period 按**引用组**
判定,不会出现"留下 snapshot 却删掉它引用的 record/blob"。
