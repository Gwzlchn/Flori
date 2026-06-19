# 03 · 接口契约

> API 端点、WebSocket 事件、Redis 消息、文件 Schema、错误码。实现时以此为准。

## 1. REST API

Base URL: `/api`

### 1.1 任务管理

#### POST /api/jobs — 创建任务

```bash
# 视频 URL（带风格标签）
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"url": "BV1example001", "content_type": "video", "domain": "deep-learning", "style_tags": ["case-study"]}'

# 论文 URL
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"url": "https://arxiv.org/abs/2301.00001", "content_type": "paper", "domain": "ml"}'

# 文章 URL
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"url": "https://mp.weixin.qq.com/s/xxx", "content_type": "article"}'

# 音频 / 播客 URL
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/episode.mp3", "content_type": "audio"}'
```

JSON 创建不接受文件；文件上传走独立的 `POST /api/jobs/upload`（见下）。

`content_type` 可显式指定，也可由 API 根据 URL 自动推断（arxiv→paper、网页→article、播客→audio，其余按 video）。

#### POST /api/jobs/upload — 文件上传创建

`multipart/form-data`：`file`（必填）+ `domain`（默认 `general`）+ `style_tags`（JSON 字符串，默认 `[]`）。
按扩展名识别类型：`.pdf`→paper，`.mp4/.mkv/.webm/.flv`→video，`.mp3/.m4a/.wav/.aac`→audio，`.html/.htm/.txt`→article，其余按 video。上限 2GB。

```bash
curl -X POST http://localhost:8000/api/jobs/upload \
  -F "file=@video.mp4" \
  -F "domain=deep-learning" \
  -F 'style_tags=["case-study"]'
```

Response `201`（同 `POST /api/jobs`）。

Response `201`:
```json
{
  "job_id": "j_20260516_abc123",
  "content_type": "video",
  "status": "pending",
  "created_at": "2026-05-16T20:00:00+08:00"
}
```

#### GET /api/jobs — 任务列表

```
GET /api/jobs?status=processing&limit=20&offset=0
```

Response `200`:
```json
{
  "total": 44,
  "items": [
    {
      "job_id": "j_20260516_abc123",
      "content_type": "video",
      "title": "示例视频标题",
      "status": "processing",
      "progress_pct": 60,
      "source": "bilibili",
      "created_at": "2026-05-16T20:00:00+08:00"
    }
  ]
}
```

#### GET /api/jobs/{id} — 任务详情

Response `200`:
```json
{
  "job_id": "j_20260516_abc123",
  "content_type": "video",
  "title": "示例视频标题",
  "status": "processing",
  "progress_pct": 60,
  "domain": "deep-learning",
  "source": "bilibili",
  "meta": {"duration_sec": 485},
  "created_at": "2026-05-16T20:00:00+08:00",
  "steps": [
    {"name": "00_download",   "status": "done",    "duration_sec": 30.0, "meta": {}},
    {"name": "01_scene",      "status": "done",    "duration_sec": 120.5, "meta": {"scenes": 76}},
    {"name": "02_frames",     "status": "done",    "duration_sec": 15.2, "meta": {"total": 80, "scene": 76, "sample": 4}},
    {"name": "03_dedup",      "status": "done",    "duration_sec": 8.1, "meta": {"total": 80, "kept": 76}},
    {"name": "04_ocr",        "status": "done",    "duration_sec": 45.0, "meta": {"total": 76, "nonempty": 70}},
    {"name": "05_danmaku",    "status": "done",    "duration_sec": 0.2, "meta": {"comments": 13}},
    {"name": "06_punctuate",  "status": "running", "duration_sec": null, "meta": {}},
    {"name": "07_mechanical", "status": "waiting", "duration_sec": null, "meta": {}},
    {"name": "08_smart",      "status": "waiting", "duration_sec": null, "meta": {}},
    {"name": "09_review",     "status": "waiting", "duration_sec": null, "meta": {}}
  ]
}
```

#### POST /api/jobs/{id}/retry — 重试失败任务

从失败步骤开始重跑（仅对 status=failed 的 Job）。Response `200`：
```json
{"job_id": "j_20260516_abc123", "status": "processing", "retry_from": "08_smart"}
```

#### POST /api/jobs/{id}/rerun — 强制重跑

从指定步骤开始重跑（对已完成的 Job 重新生成）。清除该步骤及所有下游的 `.done` 标记，由指纹机制决定哪些实际需要重跑。

```bash
curl -X POST http://localhost:8000/api/jobs/j_xxx/rerun \
  -d '{"from_step": "08_smart"}'
```

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "from_step": "08_smart"}
```

典型场景：对 AI 笔记质量不满意 → rerun from 08_smart → Claude 重新生成。

#### POST /api/jobs/{id}/resubmit — 按新 pipeline 重新提交

pipeline 配置变更后（如修改步骤参数、prompt 模板），重新提交已有 Job。指纹机制自动跳过输入未变的步骤，只重跑受影响的部分。

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing"}
```

#### DELETE /api/jobs/{id} — 删除任务

删除任务记录和所有产物文件。Response `204`。

#### POST /api/jobs/{id}/rerun-smart — 换 provider 重跑智能笔记 + 评审

用指定 AI provider 重新生成智能笔记并重评（生成新版本，旧版本保留）。服务端把 provider 覆盖写进 `job.json` 的 `ai_overrides`（智能/评审步读取，worker rerun 时 pull 到新 `job.json`），再从智能步起重跑。

```bash
curl -X POST http://localhost:8000/api/jobs/j_xxx/rerun-smart \
  -d '{"provider": "anthropic"}'
```

请求体：`{"provider": "anthropic"}`（必填，须是 `GET /api/providers` 列出且 `available=true` 的 provider）。

写入 `job.json`（关键字段）：
```json
{"ai_overrides": {"10_smart": "anthropic", "11_review": "anthropic"}}
```

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "provider": "anthropic"}
```

provider 不可用（未配 API key）返回 `400 provider '<name>' 不可用(未配置 API key)`。

### 1.2 笔记与产物

通用端点（所有内容类型）：
```
GET /api/jobs/{id}/notes/smart          → text/markdown (AI 笔记)
GET /api/jobs/{id}/review               → application/json (评审)
GET /api/jobs/{id}/assets/{filename}    → image/* (截图/图表等)
GET /api/jobs/{id}/output/{filename}    → 任意产物文件
```

视频特有端点：
```
GET /api/jobs/{id}/notes/mechanical     → text/markdown (机械版笔记)
GET /api/jobs/{id}/notes/transcript     → text/markdown (逐字稿)
GET /api/jobs/{id}/source               → video/mp4 (支持 Range/206)
```

### 1.3 系统状态

#### GET /api/status

```json
{
  "workers": {
    "download": {"online": 1, "busy": 0},
    "cpu":      {"online": 1, "busy": 1},
    "ai":      {"online": 2, "busy": 1},
    "gpu":      {"online": 0, "busy": 0}
  },
  "pools": {
    "io":     {"capacity": 999, "used": 0, "queue": 0},
    "scene":  {"capacity": 1,   "used": 0, "queue": 2},
    "cpu":    {"capacity": 3,   "used": 1, "queue": 5},
    "ai":     {"capacity": 2,   "used": 1, "queue": 3},
    "gpu":    {"capacity": 1,   "used": 0, "queue": 0}
  },
  "jobs": {"total": 44, "done": 12, "processing": 4, "failed": 1, "pending": 27},
  "disk": {"used_gb": 15.2, "available_gb": 600.0}
}
```

#### GET /api/health

```json
{
  "status": "healthy",
  "checks": {
    "redis": "ok",
    "db": "ok",
    "disk_free_gb": 600.0,
    "workers_online": 4
  }
}
```

### 1.4 Worker 管理

`GET /api/workers` 返回的 `status` 是后端按心跳新鲜度+是否在跑+管理员叠加位读时派生的公共态（`online-idle` / `online-busy` / `offline` / `stale` / `draining`，见 §3.4）；下文示例中的 `idle`/`busy` 是历史字段示意，实际响应为派生态。

#### POST /api/workers/registration-token — 铸接入 token

铸/重置一次性接入 token（可复用、可重置，重铸即作废旧的）。远程 worker 注册时持此 token 经 `POST /api/runner/register` 换取 per-worker token（gateway 接入流程见 §1.7）。

Response `200`:
```json
{"token": "mnw-xxxxxxxx"}
```

#### GET /api/workers/{id}/jobs — Worker 任务历史

该 worker 执行过的步骤记录。`?limit=` 默认 50，范围 1–200。

Response `200`:
```json
[
  {
    "job_id": "j_xxx", "step": "08_smart", "status": "done",
    "started_at": "2026-05-17T12:00:00Z", "finished_at": "2026-05-17T12:00:45Z",
    "duration_sec": 45.2, "error": null
  }
]
```

#### GET /api/workers — Worker 列表

```json
{
  "workers": [
    {
      "id": "ai-a1b2c3d4",
      "type": "ai",
      "pools": ["ai"],
      "hostname": "office-pc",
      "status": "busy",
      "current_job": "j_20260516_abc123",
      "current_step": "08_smart",
      "tasks_completed": 142,
      "tasks_failed": 3,
      "total_duration_sec": 28800.0,
      "first_seen": "2026-05-10T08:00:00+08:00",
      "started_at": "2026-05-17T09:00:00+08:00",
      "last_heartbeat": "2026-05-17T12:30:15+08:00",
      "admin_note": "内网机器，有 Claude Max 订阅"
    },
    {
      "id": "gpu-e5f6g7h8",
      "type": "gpu",
      "pools": ["gpu", "scene", "cpu", "io"],
      "hostname": "gpu-server",
      "gpu_name": "RTX 4090",
      "status": "idle",
      "tasks_completed": 88,
      "tasks_failed": 1,
      "first_seen": "2026-05-12T10:00:00+08:00",
      "last_heartbeat": "2026-05-17T12:30:10+08:00"
    }
  ]
}
```

#### GET /api/workers/{id} — Worker 详情

除上述字段外，额外返回最近执行的任务历史：

```json
{
  "id": "ai-a1b2c3d4",
  "...": "...",
  "recent_tasks": [
    {"job_id": "j_xxx", "step": "08_smart", "status": "done", "duration_sec": 45.2, "finished_at": "..."},
    {"job_id": "j_yyy", "step": "09_review", "status": "done", "duration_sec": 12.1, "finished_at": "..."},
    {"job_id": "j_zzz", "step": "08_smart", "status": "failed", "error": "timeout", "finished_at": "..."}
  ]
}
```

#### PUT /api/workers/{id} — 更新 Worker 配置

```bash
# 设置 Worker 为排空状态（完成当前任务后不再接新任务）
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"status": "draining"}'

# 添加运维备注
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"admin_note": "内网机器，有 Claude Max 订阅"}'
```

#### DELETE /api/workers/{id} — 移除 Worker 记录

移除已下线 Worker 的历史记录。Response `204`。

### 1.5 平台认证

B站扫码登录走 `/api/bili/*`（cookie 入库 DB）；YouTube cookies 与平台 cookie 文件状态走 `/api/auth/*`：

```
POST /api/bili/login/start             → 生成扫码二维码（passport QR）
GET  /api/bili/login/poll?qrcode_key=  → 轮询扫码结果
GET  /api/bili/status                  → 当前 B站登录态
POST /api/bili/logout                  → 清除已入库 B站 cookie
GET  /api/auth/status                  → bilibili.txt / youtube.txt 文件状态
POST /api/auth/youtube/cookies         → 上传 YouTube cookies.txt
```

#### POST /api/bili/login/start

Response `200`（`qr_png` 是可直接当 `img src` 的 PNG data URI）：
```json
{
  "qrcode_key": "abc123...",
  "qr_png": "data:image/png;base64,...",
  "url": "https://..."
}
```

#### GET /api/bili/login/poll

`state` ∈ `waiting` / `scanned` / `expired` / `confirmed`；`confirmed` 时服务端从 Set-Cookie 取 SESSDATA 等入库：
```json
{"state": "waiting",   "logged_in": false, "uname": null}
{"state": "scanned",   "logged_in": false, "uname": null}
{"state": "confirmed", "logged_in": true,  "uname": "用户昵称"}
{"state": "expired",   "logged_in": false, "uname": null}
```

#### GET /api/bili/status

Response `200`:
```json
{"logged_in": true, "uname": "用户昵称"}
```

### 1.6 配置管理

```
GET  /api/config/pools                 → 当前资源池配置
PUT  /api/config/pools                 → 热更新资源池配置
GET  /api/config/styles                → 可用风格标签列表
```

#### GET /api/config/styles

返回可用风格标签（从 `prompts/styles/*.yaml` 读取，每文件取其 `tag` 字段，缺省回退文件名）。供前端创建任务时勾选 `style_tags`。Response `200`（字符串数组）：
```json
["case-study", "deep-dive", "quick-summary"]
```

### 1.7 Worker 网关（`/api/runner/*`）

远程 worker 经单条出站 HTTPS 接入这组端点：注册换 token、长轮询认领步骤、上报结果、经网关代理读写产物（worker 不直连 Redis/MinIO，见 [ADR-0009](adr/0009-worker-gateway-outbound-https.md)）。`register` 用接入 token（`POST /api/workers/registration-token` 铸发）门禁，其余端点用注册时签发的 per-worker token（`Authorization: Bearer`）。

```
POST   /api/runner/register                                → 换发 per-worker token
POST   /api/runner/heartbeat                               → 刷新存活，回发 draining 控制位
POST   /api/runner/offline                                 → 主动下线
POST   /api/runner/jobs/request                            → 长轮询认领一步（认到即返回 enrich 后的 claim）
POST   /api/runner/jobs/{id}/steps/{step}/complete         → 上报完成
POST   /api/runner/jobs/{id}/steps/{step}/fail             → 上报失败
POST   /api/runner/jobs/{id}/steps/{step}/release          → 释放认领（不计成败）
POST   /api/runner/jobs/{id}/steps/{step}/progress         → 上报运行中进度（转发到 events:{id}）
POST   /api/runner/usage                                   → 记录一次 AI 用量（exec_id 去重）
GET    /api/runner/jobs/{id}/artifacts                     → 产物清单（GatewayStorage.pull 据此）
GET    /api/runner/jobs/{id}/artifacts/{rel}              → 取单个产物字节
PUT    /api/runner/jobs/{id}/artifacts/{rel}              → 回传单个产物字节
```

`POST /api/runner/register` Response `200`:
```json
{"worker_id": "ai-a1b2c3d4", "worker_token": "mnwt-...", "heartbeat_sec": 10}
```

### 1.8 集合管理

Base: `/api/collections`。集合是内容分组；当 `source_type`+`source_id` 非空时该集合即"订阅集合"，会自动从来源（目前仅 B 站 UP 主）追更新内容。订阅没有独立实体，全部由集合的字段拼装为 `subscription` 对象返回。

`CollectionResponse` 公共结构：

```json
{
  "id": "c_xxx",
  "name": "集合名",
  "domain": "deep-learning",
  "description": "",
  "tags": ["tag1"],
  "job_count": 12,
  "created_at": "2026-05-16T20:00:00+08:00",
  "subscription": null
}
```

`subscription` 仅订阅集合非 null，结构为：

```json
{
  "source_type": "bilibili_up",
  "source_id": "12345678",
  "enabled": true,
  "last_synced_at": "2026-05-16T20:00:00+08:00"
}
```

其中 `enabled` = 集合的 `sync_enabled`（自动追更开关），`last_synced_at` 可为 `null`（从未同步）。

#### POST /api/collections — 创建集合

普通集合只传 `name`/`domain`；同时给 `source_type`+`source_id` 即创建订阅集合。

```bash
# 普通集合
curl -X POST http://localhost:8000/api/collections \
  -H "Content-Type: application/json" \
  -d '{"name": "我的合集", "domain": "deep-learning", "tags": ["case-study"]}'

# 订阅集合（B 站 UP 主，建后立即首次同步）
curl -X POST http://localhost:8000/api/collections \
  -H "Content-Type: application/json" \
  -d '{"name": "某 UP", "domain": "deep-learning", "source_type": "bilibili_up", "source_id": "12345678", "sync_now": true}'
```

请求体字段：`name`（必填）、`domain`（必填）、`description`、`tags`（默认 `[]`）、`source_type`/`source_id`（成对给出才算订阅）、`sync_now`（默认 `true`，仅订阅集合有效，建后立即首次同步）。

订阅集合约束：`domain` 必须是真实领域，不能为空或 `general`；同一来源全局唯一（已订阅会被拒）。首次同步失败不阻塞集合创建（集合照常建好）。

Response `201`：`CollectionResponse`。

错误：`400` 订阅集合 domain 为 general / 该来源已订阅。

#### GET /api/collections — 集合列表

```
GET /api/collections?domain=deep-learning
```

`domain` 可选，按领域过滤。Response `200`：`CollectionResponse` 数组（注意是裸数组，非 `{total, items}` 包裹）。

#### GET /api/collections/{id} — 集合详情

Response `200`：`CollectionResponse`。错误：`400` collection_id 非法（含 `..` / `/` / 空字节）、`404` 不存在。

#### PUT /api/collections/{id} — 修改集合

```bash
curl -X PUT http://localhost:8000/api/collections/c_xxx \
  -H "Content-Type: application/json" \
  -d '{"name": "新名字", "description": "...", "tags": ["a"], "sync_enabled": false}'
```

请求体均可选（`null`=不改）：`name`、`description`、`tags`、`sync_enabled`。`sync_enabled` 仅订阅集合可改（对普通集合传该字段返回 `400`）。Response `200`：`CollectionResponse`。错误：`400` 非法 id / 非订阅集合改 `sync_enabled`、`404` 不存在。

#### DELETE /api/collections/{id} — 删除集合

删集合即解绑：名下 job 的 `collection_id` 置空（job 保留），再删集合行。Response `204` 无响应体。错误：`400` 非法 id、`404` 不存在。

#### POST /api/collections/{id}/sync — 立即同步

仅订阅集合可调，枚举来源 → 与已入库去重 → 新内容自动建 job 归入本集合，并刷新 `last_synced_at`。

```bash
curl -X POST http://localhost:8000/api/collections/c_xxx/sync
```

Response `200`：

```json
{"total": 50, "new": 3, "skipped": 47}
```

错误：`400` 非法 id / 非订阅集合、`404` 不存在、`502` 同步失败（如来源访问失败）。

#### GET /api/collections/{id}/jobs — 集合内任务列表

```
GET /api/collections/c_xxx/jobs?limit=20&offset=0
```

`limit`（默认 20，1–200）、`offset`（默认 0）。Response `200`：`JobListResponse`（`{total, items}`，items 为 `JobResponse`）：

```json
{
  "total": 12,
  "items": [
    {
      "job_id": "j_xxx",
      "content_type": "video",
      "status": "done",
      "created_at": "2026-05-16T20:00:00+08:00",
      "title": "标题",
      "progress_pct": 100,
      "source": "bilibili",
      "domain": "deep-learning",
      "collection_id": "c_xxx"
    }
  ]
}
```

错误：`400` 非法 id、`404` 不存在。

---

### 1.9 领域（知识中心）

Base: `/api/domains`。领域是派生视图，无 `domains` 表——领域集合 = distinct `domain`（来自 jobs ∪ collections ∪ glossary）**∪ 有 `prompts/profiles/{domain}.yaml` 的领域**（即「新建知识库」创建的、暂无内容的空领域也算）。展示元数据（`display_name` / `icon` / `color` / `description` / `role`）持久化在该 profile yaml。所有端点对 `{domain}` 做合法性校验（含 `..` / `/` / 空字节或为空返回 `400`）。

#### GET /api/domains — 领域总览

每个领域的集合数 / 内容数 / 概念数 / 订阅数 / 最近活跃 + 展示元数据，用于卡片网格。Response `200`：

```json
{
  "domains": [
    {
      "domain": "deep-learning",
      "collection_count": 4,
      "job_count": 42,
      "concept_count": 120,
      "subscription_count": 2,
      "last_active_at": "2026-05-16T20:00:00+08:00",
      "display_name": "深度学习",
      "icon": "brain",
      "color": "#6366f1",
      "description": "...",
      "role": "资深深度学习研究员"
    }
  ]
}
```

`last_active_at` = 该域 job 的 `MAX(updated_at)`，无 job 时为 `null`。列表按 `domain` 升序。`display_name` / `icon` / `color` / `description` / `role` 来自 profile，未设则该键不出现（前端可回退按 `domain` 名派生）。

#### POST /api/domains — 新建知识库（领域）

把展示元数据写进 `prompts/profiles/{domain}.yaml`，领域随即出现在总览（即使暂无内容，工作台也可正常打开为空）。

```bash
curl -X POST http://localhost:8000/api/domains \
  -H "Content-Type: application/json" \
  -d '{"domain": "crypto", "display_name": "加密货币", "icon": "coins", "color": "#f59e0b", "role": "链上研究员", "description": "去中心化金融"}'
```

请求体：`domain`（必填，键/slug，用于 URL 与过滤）、`display_name` / `icon` / `color` / `role` / `description`（均可选）。Response `201`：该领域的总览条目（结构同 `GET /api/domains` 的一项，计数为 0）。

错误：`400` domain 非法或为 `general`（默认领域无需新建）、`409` 该领域已存在（profile 已存在）。

> 元数据后续修改走 `PUT /api/profiles/{domain}`（见 1.12，已支持 `display_name`/`icon`/`color`/`description`）。

#### GET /api/domains/{domain} — 领域工作台

聚合该领域的情景层（集合 + 最近内容）与语义层（概念 + 主题）。Response `200`：

```json
{
  "domain": "deep-learning",
  "stats": { "domain": "deep-learning", "collection_count": 4, "job_count": 42, "concept_count": 120, "subscription_count": 2, "last_active_at": "…" },
  "collections": [
    {"id": "c_xxx", "name": "某 UP", "job_count": 12, "is_subscription": true, "source_id": "12345678", "sync_enabled": true}
  ],
  "recent_jobs": [
    {"job_id": "j_xxx", "content_type": "video", "status": "done", "created_at": "…", "title": "…", "progress_pct": 100, "source": "bilibili", "domain": "deep-learning", "collection_id": "c_xxx"}
  ],
  "top_concepts": [
    {"term": "Transformer", "definition": "…", "source_count": 8, "status": "accepted", "is_topic": true}
  ],
  "topics": [
    {"topic": "case-study", "count": 5}
  ],
  "suggested_count": 7
}
```

- `stats`：即 `GET /api/domains` 中该域那条。
- `collections`：精简集合卡（非完整 `CollectionResponse`），仅 `id/name/job_count/is_subscription/source_id/sync_enabled`。
- `recent_jobs`：最近 12 条，字段同 `JobResponse` 子集（`job_id/content_type/status/created_at/title/progress_pct/source/domain/collection_id`）。
- `top_concepts`：术语 Top 30（含 `suggested` 候选，各带 `status`），按 `source_count`（佐证来源数）降序；`is_topic` 标记是否为主题概念。
- `topics`：该域所有 job 的 `style_tags` 去重计数，按 count 降序。
- `suggested_count`：状态为 `suggested` 的候选术语数。

错误：`404` 领域不存在。

#### GET /api/domains/{domain}/topic-concepts — 主题概念列表

该领域中被标为主题（`is_topic=1`）的概念，按出现数降序，空则 `[]`。Response `200`：

```json
[
  {
    "term": "Transformer",
    "definition": "…",
    "occurrence_count": 8,
    "related": ["Attention", "Self-Attention"],
    "is_topic": true
  }
]
```

#### GET /api/domains/{domain}/terms/{term} — 概念详情

定义 + 出现处 + 关联概念。Response `200`（即 db 的 glossary 行，字段同 `GlossaryTermResponse`）：

```json
{
  "domain": "deep-learning",
  "term": "Transformer",
  "definition": "…",
  "occurrences": [
    {"job_id": "j_xxx", "content_type": "video", "location": "…"}
  ],
  "related": ["Attention"],
  "status": "accepted",
  "is_topic": true,
  "definition_locked": false,
  "created_at": "2026-05-16T20:00:00+08:00",
  "updated_at": "2026-05-16T20:00:00+08:00"
}
```

`status`：`accepted` / `suggested`。错误：`404` 术语不存在。

#### GET /api/domains/{domain}/topics/{topic} — 主题页

该领域内 `style_tags` 含该标签的内容（跨集合 / 跨来源聚合）。`limit`（默认 50，1–200）。Response `200`：

```json
{
  "domain": "deep-learning",
  "topic": "case-study",
  "jobs": [
    {"job_id": "j_xxx", "content_type": "video", "status": "done", "created_at": "…", "title": "…", "progress_pct": 100, "source": "bilibili", "domain": "deep-learning", "collection_id": "c_xxx"}
  ],
  "total": 5
}
```

`total` 为本次返回（受 `limit` 截断后）的 `jobs` 条数，非全量计数。

#### GET /api/domains/{domain}/concept-timeline — 概念时间线

各概念的出现（occurrences）经其 `job_id` → `job.created_at` 映射后，按粒度分桶计数，供工作台「时间线」视图。`granularity`：`day`（`YYYY-MM-DD`）/ `week`（`YYYY-Www`，ISO 周）/ `month`（`YYYY-MM`，默认）；非法值返回 `422`。空领域返回空序列（不 404）。

```
GET /api/domains/deep-learning/concept-timeline?granularity=month
```

Response `200`：

```json
{
  "granularity": "month",
  "buckets": ["2026-04", "2026-05"],
  "totals": {"2026-04": 5, "2026-05": 12},
  "concepts": [
    {"term": "Transformer", "buckets": {"2026-04": 2, "2026-05": 6}, "total": 8}
  ]
}
```

`buckets` = 出现过的桶（升序）；`totals` = 每桶的跨概念总计；`concepts` 按 `total` 降序，每项 `buckets` 为该概念各桶计数。

### 1.10 术语库 / 概念图

> 按 `domain` 维度维护的术语表。术语有两种来源：AI 抽取步骤自动采集（落 `status=suggested` 候选）、用户手动新增（直接 `accepted`）。`accepted` 的术语会同步进对应 domain 的 `Profile.terminology`，供后续 AI 步骤复用。`is_topic` 标记主题概念，用于概念图。主键为 `(domain, term)`。

所有端点走 Basic/Token 鉴权。`domain` / `term` 路径段不得含 `..`、`/`、`\x00`，否则 `400`。

**`GlossaryTermResponse` 字段**：

```json
{
  "domain": "deep-learning",
  "term": "注意力机制",
  "definition": "一种让模型动态聚焦输入关键部分的机制",
  "occurrences": [
    {"job_id": "j_20260516_abc123", "content_type": "video", "location": "scene-12"}
  ],
  "related": ["Transformer", "自注意力"],
  "status": "accepted",
  "is_topic": true,
  "definition_locked": false,
  "created_at": "2026-05-16T20:00:00+08:00"
}
```

- `status`：`suggested`（AI 采集的候选，待审）/ `accepted`（已采纳）。
- `occurrences`：该术语出现过的来源，元素 `{job_id, content_type, location}`，由抽取步骤累积。
- `is_topic`：是否为主题概念。`definition_locked`：定义是否已钉住（钉住后自动采集不再覆盖定义）。
- `created_at`：ISO8601；缺失时为空串 `""`。

#### GET /api/glossary — 列术语

可按 `domain` / `status` 过滤（均可选），按 `term` 升序返回。

```
GET /api/glossary?domain=deep-learning&status=suggested
```

Response `200`：`GlossaryTermResponse` 数组（同上结构）。

#### POST /api/glossary?domain= — 手动新增术语

直接落 `status=accepted` 并同步进 `Profile.terminology`。`domain` 为 query 参数（必填），术语内容在 body。`term` 去空白后不得为空，否则 `400`。

```bash
curl -X POST "http://localhost:8000/api/glossary?domain=deep-learning" \
  -H "Content-Type: application/json" \
  -d '{"term": "注意力机制", "definition": "动态聚焦输入关键部分", "related": ["Transformer"]}'
```

请求体 `GlossaryTermRequest`：

```json
{"term": "注意力机制", "definition": "可省略", "related": ["可省略"]}
```

Response `201`：`GlossaryTermResponse`（`status` 恒为 `accepted`）。

#### GET /api/glossary/{domain}/{term} — 术语详情

含 `occurrences` 关联来源列表。未命中 `404`。Response `200`：`GlossaryTermResponse`。

#### PUT /api/glossary/{domain}/{term} — 修改术语

仅改 `definition` / `related`；不动 `status` / `occurrences` / `is_topic`。body 中字段为 `null`（或省略）则保留原值。未命中 `404`。

```bash
curl -X PUT "http://localhost:8000/api/glossary/deep-learning/注意力机制" \
  -H "Content-Type: application/json" \
  -d '{"definition": "更新后的定义", "related": ["Transformer", "自注意力"]}'
```

Response `200`：更新后的 `GlossaryTermResponse`。

#### DELETE /api/glossary/{domain}/{term} — 删除术语

仅删术语表记录，不动 `Profile`（避免误删手工维护的条目）。Response `204`。

#### POST /api/glossary/{domain}/{term}/accept — 采纳候选

候选术语 `status` → `accepted`，并把定义同步进 `Profile.terminology`，使后续 AI 步骤可用。未命中 `404`。

```bash
curl -X POST "http://localhost:8000/api/glossary/deep-learning/注意力机制/accept"
```

Response `200`：更新后的 `GlossaryTermResponse`（`status=accepted`）。

#### POST /api/glossary/{domain}/{term}/topic — 标记/取消主题概念

置该术语 `is_topic`。未命中 `404`。请求体：

```json
{"is_topic": true}
```

```bash
curl -X POST "http://localhost:8000/api/glossary/deep-learning/注意力机制/topic" \
  -H "Content-Type: application/json" \
  -d '{"is_topic": true}'
```

Response `200`：更新后的 `GlossaryTermResponse`。

### 1.11 全文检索

#### GET /api/search — 笔记全文检索

基于 SQLite FTS5（`trigram` tokenizer，对中文做子串匹配）。**`q` 至少 3 个字符才可能命中**，更短或空查询直接返回空结果（`total: 0`）。`q` 经服务端转义防 MATCH 注入。

```bash
curl "http://localhost:8000/api/search?q=注意力机制&domain=deep-learning&limit=20"
```

查询参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `q` | `""` | 检索词；trigram 至少 3 字符 |
| `collection_id` | — | 限定集合 |
| `domain` | — | 限定领域 |
| `content_type` | — | 限定内容类型（video/paper/article/audio） |
| `limit` | 20 | 1–100 |
| `offset` | 0 | ≥0 |

Response `200`（`note_type` 区分命中的是哪类笔记，如 `smart`/`mechanical`/`transcript`；`snippet` 带 `<mark>` 高亮标签、`…` 省略号）：
```json
{
  "total": 7,
  "items": [
    {
      "job_id": "j_20260516_abc123",
      "title": "示例视频标题",
      "note_type": "smart",
      "snippet": "…介绍了<mark>注意力机制</mark>的核心思想…",
      "content_type": "video",
      "domain": "deep-learning",
      "collection_id": "c_xxx"
    }
  ]
}
```

### 1.12 Profile 管理（`/api/profiles/*`）

每个 domain 一个 `prompts/profiles/{domain}.yaml`，承载该领域的角色设定/输出风格/术语表（`terminology`），供生成笔记时注入 prompt。术语库采纳一条术语时会同步写入对应 Profile 的 `terminology`。

```
GET    /api/profiles                      → Profile 列表（每个 domain 概览）
GET    /api/profiles/{domain}             → 单个 Profile 全文
PUT    /api/profiles/{domain}             → 创建/更新 Profile（不存在则建）
POST   /api/profiles/{domain}/terms       → 追加一条术语（去重）
DELETE /api/profiles/{domain}/terms/{term} → 删除一条术语
```

#### GET /api/profiles

Response `200`（数组）：
```json
[
  {"domain": "deep-learning", "role": "资深深度学习研究员", "terminology_count": 42}
]
```

#### GET /api/profiles/{domain}

返回该 domain 的 YAML 解析结果原样。不存在返回 `404 profile '<domain>' not found`。
```json
{
  "domain": "deep-learning",
  "role": "资深深度学习研究员",
  "domain_context": "...",
  "output_style": {"...": "..."},
  "terminology": ["注意力机制: 让模型聚焦关键输入的加权机制", "梯度下降"],
  "do_not": ["不要逐字翻译英文术语"]
}
```

#### PUT /api/profiles/{domain}

请求体（全部可选，仅传入字段被更新，其余保留；Profile 不存在则新建）：
```json
{
  "role": "资深深度学习研究员",
  "domain_context": "...",
  "output_style": {"tone": "严谨"},
  "terminology": ["注意力机制", "梯度下降"],
  "do_not": ["不要逐字翻译英文术语"],
  "display_name": "深度学习",
  "icon": "brain",
  "color": "#6366f1",
  "description": "..."
}
```
`display_name` / `icon` / `color` / `description` 为知识库展示元数据（与 `POST /api/domains` 同一份 profile yaml；改这些即改卡片显示）。Response `200`：返回更新后的完整 Profile（同 `GET`）。

#### POST /api/profiles/{domain}/terms

请求体 `{"term": "梯度下降"}`。已存在则不重复追加。Profile 不存在返回 `404`。Response `200`：
```json
{"terminology": ["注意力机制", "梯度下降"]}
```

#### DELETE /api/profiles/{domain}/terms/{term}

按裸字符串精确匹配从 `terminology` 移除该条。Profile 不存在返回 `404`。Response `200`：
```json
{"terminology": ["注意力机制"]}
```

`domain` / `term` 含 `..` `/` `\x00` 返回 `400 invalid domain name`。

### 1.13 AI Provider 列表

#### POST /api/jobs/{id}/rerun — 强制重跑

请求体 `{"from_step": "08_smart"}`（必填）。`from_step` 须是该 pipeline 内的步骤名，会清除该步骤及所有下游的 `.done` 标记，由指纹机制决定哪些实际重跑。

#### POST /api/jobs/{id}/rerun-smart — 换 provider 重跑智能笔记 + 评审

用指定 AI provider 重新生成智能笔记并重评（生成新版本，旧版本保留）。服务端把 provider 覆盖写进 `job.json` 的 `ai_overrides`（智能/评审步读取，worker rerun 时 pull 到新 `job.json`），再从智能步起重跑。

```bash
curl -X POST http://localhost:8000/api/jobs/j_xxx/rerun-smart \
  -d '{"provider": "anthropic"}'
```

请求体：`{"provider": "anthropic"}`（必填，须是 `GET /api/providers` 列出且 `available=true` 的 provider）。

写入 `job.json`（关键字段）：
```json
{"ai_overrides": {"10_smart": "anthropic", "11_review": "anthropic"}}
```

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "provider": "anthropic"}
```

provider 不可用（未配 API key）返回 `400 provider '<name>' 不可用(未配置 API key)`。

## 2. WebSocket

### WS /api/ws/jobs/{id} — 单任务进度

服务端推送事件：

```json
{"event": "step_ready",    "step": "01_scene"}
{"event": "step_start",    "step": "01_scene", "worker": "cpu-a1b2"}
{"event": "step_progress", "step": "01_scene", "current": 15000, "total": 40080, "pct": 37, "message": "scanning frames"}
{"event": "step_done",     "step": "01_scene", "duration_sec": 120.5, "meta": {"scenes": 76}}
{"event": "step_failed",   "step": "08_smart", "error": "Claude rate limit", "retries": 1}
{"event": "step_skipped",  "step": "00b_whisper", "reason": "subtitle exists"}
{"event": "job_done",      "progress_pct": 100}
{"event": "job_failed",    "error": "08_smart: Claude rate limit after 3 retries"}
```

### WS /api/ws/global — 全局状态

每 2 秒推送一次系统状态（格式同 GET /api/status）。

## 3. Redis 数据结构

### 3.1 任务队列（Sorted Set，按优先级）

```
Key:    queue:{pool_name}
Type:   ZSET
Member: {"job_id": "j_xxx", "step": "01_scene"}  (JSON string)
Score:  priority (负数，越小越优先)
```

优先级计算：`score = -(已完成步骤数)`

### 3.2 资源池计数

```
Key:    pool:{pool_name}:count
Type:   STRING (integer)
Value:  当前已占用槽数

Key:    pool:{pool_name}:frozen
Type:   STRING
Value:  "1" 表示冻结（scene 运行时冻结 cpu 池）
```

### 3.3 Job 状态（调度器维护）

```
Key:    job:{job_id}
Type:   HASH
Fields:
  pipeline:       "video" | "paper" | "article" | "audio"
  status:         "pending" | "downloading" | "processing" | "done" | "failed"
  domain:         "deep-learning" | "ml" | ...
  style_tags:     '["case-study"]'                 ← JSON array
  created_at:     ISO timestamp

Key:    job:{job_id}:steps
Type:   HASH
Fields: 每个步骤名 → 状态
  00_download:    "done"
  01_scene:       "running"
  08_smart:       "waiting"
  ...

Key:    job:{job_id}:retries
Type:   HASH
Fields: 每个步骤名 → 已重试次数
  08_smart:       "1"

Key:    job:{job_id}:step_worker
Type:   HASH
Fields: 每个 running 步骤 → 执行它的 Worker ID
  01_scene:       "cpu-a1b2c3d4"
```

### 3.4 Worker 注册

```
Key:    worker:{worker_id}
Type:   HASH
Fields:
  type:           "cpu" | "gpu" | "ai" | "download"
  pools:          "scene,cpu,io"
  tags:           "vision,claude-cli"              ← 能力标签
  reject_tags:    "private,confidential"              ← 排斥标签（可选）
  hostname:       "gpu-server" | ""
  status:         "idle" | "busy" | "draining" | "offline"   ← 存量字段，非对外公共态
  current_job:    "j_xxx" | ""
  current_step:   "01_scene" | ""
  gpu_name:       "RTX 4090" | ""
  started_at:     ISO timestamp
  last_heartbeat: ISO timestamp
TTL:    30 秒（心跳续期）

Redis 为实时状态；持久记录（统计/历史/备注）存 SQLite workers 表。
```

**公共状态是读时派生，不直接存。** SQLite/Redis 里 `status` 存的是存量态（`idle` / `busy` / `stale` / `draining` / `offline`，worker 自报或管理员置位）；`GET /api/workers` 不信任该字段，而是按 `shared/status.py` 的 `compute_worker_status()` 用 `last_heartbeat` 新鲜度 + `current_job` + 管理员 `draining` 叠加位现算出对外公共态：

| 公共态 | 含义 |
|--------|------|
| `online-busy` | 心跳新鲜且有在跑任务 |
| `online-idle` | 心跳新鲜且空闲 |
| `draining` | 管理员置 draining 且仍在线（完成当前任务后不再接新任务） |
| `offline` | 心跳超 `online_window`（默认 30s）但未到 `stale_window` |
| `stale` | 心跳缺失或超 `stale_window`（默认 900s），GC 信号 |

判定优先级：`draining`（仅在线生效）→ `offline` → `stale` → `online-busy` → `online-idle`。窗口阈值取自 `configs/pools.yaml` 的 `worker_status` 段，缺省回退内置默认。容器跑 UTC，故由后端统一派生，前端只渲染、不再用本地时区自算。

### 3.5 事件发布

```
Channel: step_completed
Payload: {"job_id": "j_xxx", "step": "01_scene", "status": "done", "duration": 120.5, "worker": "cpu-a1b2"}

Channel: step_failed
Payload: {"job_id": "j_xxx", "step": "08_smart", "status": "failed", "error": "...", "worker": "ai-c3d4"}

Channel: step_started
Payload: {"job_id": "j_xxx", "step": "01_scene", "worker": "cpu-a1b2"}

Channel: events:{job_id}
Payload: (WebSocket 事件格式，同上 §2)
```

## 4. 文件 Schema

### 4.1 pipelines.yaml — 步骤链定义

GitLab-CI 风格：顶层 `default` 全局默认 + `.` 前缀隐藏模板（不直接运行）+ 每个 content_type 一段 `variables`/`jobs`。加载时把 `default`、`extends` 模板、job 字段按键深合并归一化为内部 step 结构，步骤顺序由 `needs` 推导出 DAG。调度器据 Job 的 `pipeline` 字段加载对应段。

**顶层结构**：

```yaml
# 全局默认：所有 job 自动继承、可逐字段覆盖。
default:
  image: mnemo/step-base
  timeout: 600
  retry: 0

# 隐藏模板（'.' 前缀，不直接运行）：同类步只写差异，extends 按键深合并。
.cpu-step:
  pool: cpu
  timeout: 120
  retry: 1

.ai-step:
  pool: ai
  timeout: 600
  retry: 2

.review:
  pool: ai
  timeout: 120
  retry: 2
```

**job 字段**：

| 字段 | 说明 |
|------|------|
| `run` | 步骤模块（`steps.video.step_01_scene` 等），由 worker 执行 |
| `extends` | 继承的隐藏模板名（`.cpu-step` / `.ai-step` / `.review`） |
| `needs` | 上游 job 列表，决定 DAG 顺序；无 `needs` 即可与同级并行 |
| `pool` | 资源池（io / scene / cpu / ai / gpu） |
| `image` | 步骤镜像（`mnemo/step-base` / `mnemo/step-heavy` / `mnemo/step-gpu`） |
| `timeout` | 超时秒数，支持 `$VAR` 引用本段 `variables` |
| `retry` | 重试次数，支持 `$VAR` |
| `tags` | 需求标签，匹配 worker 能力标签（如 `gpu` / `vision`） |
| `rules` | 条件门：`exists` 命中后 `when: on`（启用）或 `when: skip`（跳过） |
| `ai` | AI provider 路由：`primary` / `fallback` / `text_fallback`，各取 `{provider, model}` |

**每段 `variables`** 是该 content_type 的单一事实源（AI provider/model、OCR 超时等），job 用 `$VAR` 引用。

**视频 pipeline 示例**（截取，完整见 `configs/pipelines.yaml`）：

```yaml
video:
  variables:
    OCR_TIMEOUT: 1800
    OCR_RETRIES: 1
    AI_SMART_PRIMARY_PROVIDER: anthropic
    AI_SMART_PRIMARY_MODEL: claude-sonnet-4-6
    AI_SMART_FALLBACK_PROVIDER: openai
    AI_SMART_FALLBACK_MODEL: gpt-4o
    AI_SMART_TEXT_PROVIDER: deepseek
    AI_SMART_TEXT_MODEL: deepseek-v4-pro
    # ...（review / punct 的 provider 变量略）
  jobs:
    "00_download":
      run: steps.common.step_00_download
      pool: io
      retry: 3

    "00b_whisper":
      run: steps.video.step_00b_whisper
      image: mnemo/step-gpu
      pool: gpu
      needs: ["00_download"]
      timeout: 1800
      retry: 2
      tags: ["gpu"]
      rules:
        - exists: "input/*.srt"
          when: skip                   # 已有字幕则跳过 whisper

    "04_ocr":
      extends: .cpu-step
      run: steps.video.step_04_ocr
      image: mnemo/step-heavy
      needs: ["03_dedup"]
      timeout: $OCR_TIMEOUT
      retry: $OCR_RETRIES

    "06_punctuate":
      extends: .ai-step
      run: steps.video.step_06_punctuate
      needs: ["00_download"]
      timeout: 300
      retry: 3
      rules:
        - exists: "input/*.srt"
          when: on                     # 有字幕（含 whisper 产出）才标点
      ai:
        primary: {provider: $AI_PUNCT_PRIMARY_PROVIDER, model: $AI_PUNCT_PRIMARY_MODEL}
        fallback: {provider: $AI_PUNCT_FALLBACK_PROVIDER, model: $AI_PUNCT_FALLBACK_MODEL}

    "08_smart":
      extends: .ai-step
      run: steps.video.step_08_smart
      needs: ["07_mechanical"]
      tags: ["vision"]
      ai:
        primary: {provider: $AI_SMART_PRIMARY_PROVIDER, model: $AI_SMART_PRIMARY_MODEL}
        fallback: {provider: $AI_SMART_FALLBACK_PROVIDER, model: $AI_SMART_FALLBACK_MODEL}
        text_fallback: {provider: $AI_SMART_TEXT_PROVIDER, model: $AI_SMART_TEXT_MODEL}

    "09_review":
      extends: .review
      run: steps.video.step_09_review
      needs: ["08_smart"]
      ai:
        primary: {provider: $AI_REVIEW_PRIMARY_PROVIDER, model: $AI_REVIEW_PRIMARY_MODEL}
        fallback: {provider: $AI_REVIEW_FALLBACK_PROVIDER, model: $AI_REVIEW_FALLBACK_MODEL}
```

**各 content_type 的 job 链**（`needs` 推导）：

- **video**：`00_download` → `01_scene` → `02_frames` → `03_dedup` → `04_ocr`；`05_danmaku`/`06_punctuate`/`00b_whisper` 由 `00_download` 旁路触发；`07_mechanical` 汇合 `04_ocr`+`05_danmaku`+`06_punctuate` → `08_smart` → `09_review`。
- **paper**：`00_download` → `10_pdf_parse` → (`11_sections`, `12_figures`) → `14_smart_paper` → `15_review`。
- **article**：`00_download` → `16_parse_article` → `17_article_sections` → `18_smart_article` → `19_review`。
- **audio**：`00_download` → `00b_whisper` → `20_transcript_parse` → `21_smart_podcast` → `22_review`。

新增内容类型只需在此文件添加一段 `variables`/`jobs`，无需改调度器/Worker 代码。

### 4.2 pools.yaml — 资源池配置

```yaml
pools:
  io:
    limit: 999
  scene:
    limit: 1
    exclusive_group: cpu_bound
  cpu:
    limit: 3
    exclusive_group: cpu_bound
  ai:
    limit: 2
    rate_limit_sec: 5
  gpu:
    limit: 1
    fallback: cpu

exclusive_groups:
  cpu_bound:
    scene_acquires_all_cpu: true
```

### 4.3 scenes.json — 场景检测输出

```json
{
  "fps": 30.0,
  "duration_sec": 485.0,
  "scenes": [
    {"index": 0, "start_frame": 0, "end_frame": 450, "start_sec": 0.0, "end_sec": 15.0},
    {"index": 1, "start_frame": 450, "end_frame": 912, "start_sec": 15.0, "end_sec": 30.4}
  ]
}
```

### 4.4 candidates.json — 候选帧

`filename` 是 `assets/` 下的文件名（步骤已自带 `scene_{index}_{ts}s.jpg` 编码），`scene_index` 标出来源场景：

```json
[
  {"index": 0, "scene_index": 0, "timestamp_sec": 1.5, "filename": "scene_0000_1.5s.jpg"},
  {"index": 1, "scene_index": 3, "timestamp_sec": 45.0, "filename": "scene_0001_45.0s.jpg"}
]
```

### 4.5 dedup.json — 去重结果

在 candidates 基础上追加 `keep` / `phash`（缺图或读图异常时追加 `reason`）：

```json
[
  {"index": 0, "scene_index": 0, "timestamp_sec": 1.5, "filename": "scene_0000_1.5s.jpg", "keep": true, "phash": "d4c0d4e0f0f8fcfe"},
  {"index": 1, "scene_index": 0, "timestamp_sec": 15.2, "filename": "scene_0001_15.2s.jpg", "keep": false, "phash": "d4c0d4e0f0f8fcff"}
]
```

### 4.6 ocr.json — OCR 结果

仅对 `keep=true` 的帧做 OCR。`text` 是各识别行用换行拼接的纯文本，`boxes` 是逐行的框/置信度明细：

```json
[
  {
    "index": 0,
    "filename": "scene_0000_1.5s.jpg",
    "timestamp_sec": 1.5,
    "text": "0.32\nloss\nepoch",
    "boxes": [
      {"text": "0.32", "confidence": 0.987, "box": [[10, 8], [60, 8], [60, 28], [10, 28]]}
    ]
  }
]
```

### 4.7 danmaku.json — 弹幕

```json
[
  {"time_sec": 1.68, "text": "前排学习"},
  {"time_sec": 15.3, "text": "这个推导讲得真清楚"}
]
```

### 4.8 review.json — 评审结果

智能笔记的质量评审（评最新版智能笔记）。6 维度评分（每项 1–5 整数）+ `overall`（缺失时按维度均值自动补，保留 1 位小数）+ 三类附加产出。评审步写两份：`output/review.json`（最新，供术语采集/默认读取）+ 按所评笔记版本 1:1 落一份版本化评审。`note_file` / `provider` / `model` / `generated_at` 由落盘时补记。

- `key_terms`：这篇笔记**讲清楚**的关键概念 + 一句话候选定义 →（经术语库采纳后）沉淀进概念库。
- `missing_concepts`：笔记**遗漏**的重要概念，知识缺口，**仅供评审面板/选题查漏，不入库**。
- `top3_improvements`：最重要的 3 条改进建议。
- `parse_failed`：AI 未返回有效 JSON 时落 fallback（各维度记 3、`top3_improvements` 提示重试），此字段标 `true`（写入步骤 meta，供前端提示重评）。

```json
{
  "completeness": 5,
  "accuracy": 5,
  "structure": 4,
  "terminology": 5,
  "visual_integration": 4,
  "readability": 5,
  "overall": 4.7,
  "key_terms": [
    {"term": "多头注意力", "definition": "并行多组注意力以捕捉不同子空间的依赖关系"},
    {"term": "位置编码", "definition": "为无序的 token 序列注入位置信息的向量"}
  ],
  "missing_concepts": ["多头注意力的具体计算流程"],
  "top3_improvements": [
    "可以补充更多训练曲线的解读",
    "弹幕提到的关联论文可以展开",
    "术语首次出现处建议加一句解释"
  ],
  "note_file": "output/versions/notes_smart_anthropic_claude-sonnet-4-6_20260516-200500.md",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "generated_at": "2026/05/16 20:05:30"
}
```

> 旧 schema 已废弃：分数不再嵌套进 `scores` 子对象（现为顶层扁平整数键），`screenshots` 维度更名为 `visual_integration`，并新增 `visual_integration` 之外的 `key_terms` 输出。


## 5. 错误码

| HTTP 状态码 | 错误类型 | 说明 |
|-------------|---------|------|
| 400 | `invalid_url` | URL 格式不合法 |
| 400 | `invalid_domain` | 未知领域 |
| 413 | `file_too_large` | 上传文件超过 2GB |
| 401 | `unauthorized` | Bearer Token 无效 |
| 404 | `job_not_found` | Job ID 不存在 |
| 404 | `file_not_found` | 请求的产物文件不存在 |
| 409 | `job_already_exists` | 相同 URL 的任务已存在 |
| 429 | `rate_limit` | 投递频率超限（每分钟 10 次） |
| 500 | `internal_error` | 服务内部错误 |
| 503 | `no_workers` | 没有在线 Worker |

Response body:
```json
{"error": "invalid_url", "message": "URL must start with http:// or https://"}
```

## 6. 步骤错误分类与重试策略

Worker 根据错误类型决定是否重试、如何退避：

| error_type | 重试？ | 退避策略 | 说明 |
|-----------|--------|---------|------|
| `input_missing` | 不重试 | — | 前置步骤没完成，不应到达这里 |
| `input_invalid` | 不重试 | — | 输入文件损坏/格式错误，需人工检查 |
| `processing` | 最多 1 次 | 立即重试 | ffmpeg/OCR 等偶发错误 |
| `ai` | 最多 3 次 | 指数退避 30s/60s/120s | AI Provider 调用失败 |
| `ai_rate_limit` | 最多 3 次 | 固定 30s | AI Provider 限速，等一会儿再试 |
| `timeout` | 最多 1 次 | 等 10s | 可能是临时负载高 |
| `resource` | 不重试 | — | 磁盘满/OOM，需人工处理 |

Worker 重试决策逻辑：

```python
RETRY_POLICY = {
    "input_missing":    {"max": 0},
    "input_invalid":    {"max": 0},
    "processing":       {"max": 1, "delay": 0},
    "ai":               {"max": 3, "delay": [30, 60, 120]},
    "ai_rate_limit":     {"max": 3, "delay": 30},
    "timeout":          {"max": 1, "delay": 10},
    "resource":         {"max": 0},
}
```

注意：此处的重试次数和 `pipelines.yaml` 中每个 job 定义的 `retry` 取**较小值**。pipelines.yaml 是步骤级上限，RETRY_POLICY 是错误类型级上限。
