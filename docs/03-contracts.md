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
GET /api/jobs?status=processing&domain=deep-learning&source=bilibili&limit=20&offset=0
```

查询参数：`status`、`collection_id`、`domain`、`source`（均可选，AND 组合）、`limit`（默认 20，1–200）、`offset`（默认 0，0–2147483647；int32 max,远低于 SQLite int64 溢出点,越界 422）。Response `200`（每项含 `domain` / `collection_id`）：
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
      "domain": "deep-learning",
      "collection_id": "c_xxx",
      "created_at": "2026-05-16T20:00:00+08:00"
    }
  ]
}
```

#### GET /api/jobs/facets — 任务分面计数

全量 jobs 按 `source` / `domain` / `status` 分组计数，供前端过滤 chip 显示（后端聚合，非客户端基于已加载列表）。Response `200`：
```json
{
  "source": {"bilibili": 30, "arxiv": 8, "youtube": 6},
  "domain": {"deep-learning": 42, "finance": 30},
  "status": {"done": 60, "processing": 2, "failed": 1}
}
```

#### GET /api/jobs/{id} — 任务详情

Response `200`（`collection_name` 由 `collection_id` join 出，无归属/集合已删则 `null`）：
```json
{
  "job_id": "j_20260516_abc123",
  "content_type": "video",
  "title": "示例视频标题",
  "url": "https://www.bilibili.com/video/BV1example001",
  "status": "processing",
  "progress_pct": 60,
  "domain": "deep-learning",
  "source": "bilibili",
  "collection_id": "c_xxx",
  "collection_name": "我的合集",
  "meta": {"duration_sec": 485},
  "created_at": "2026-05-16T20:00:00+08:00",
  "updated_at": "2026-05-16T20:03:12+08:00",
  "published_at": "2026-05-10T18:00:00+08:00",
  "steps": [
    {"name": "01_download",   "label": "下载",     "status": "done",    "started_at": "2026-05-16T20:00:01+08:00", "finished_at": "2026-05-16T20:00:31+08:00", "duration_sec": 30.0, "meta": {}, "error": null},
    {"name": "02_whisper",    "label": "语音转写", "status": "skipped", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "03_scene",      "label": "场景检测", "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 120.5, "meta": {"scenes": 76}, "error": null},
    {"name": "04_frames",     "label": "关键帧",   "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 15.2, "meta": {"total": 80, "scene": 76, "sample": 4}, "error": null},
    {"name": "05_dedup",      "label": "截图去重", "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 8.1, "meta": {"total": 80, "kept": 76}, "error": null},
    {"name": "06_ocr",        "label": "OCR",      "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 45.0, "meta": {"total": 76, "nonempty": 70}, "error": null},
    {"name": "07_danmaku",    "label": "弹幕",     "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 0.2, "meta": {"comments": 13}, "error": null},
    {"name": "08_punctuate",  "label": "标点",     "status": "done",    "started_at": "...", "finished_at": "...", "duration_sec": 12.0, "meta": {}, "error": null},
    {"name": "09_mechanical", "label": "机械版",   "status": "running", "started_at": "...", "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "10_smart",      "label": "智能版",   "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "11_review",     "label": "质量评审", "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null}
  ]
}
```

字段说明（除 `JobResponse` 的公共字段外）：
- `url`：原始投递 URL（上传任务可为 `null`）。
- `updated_at`：最近一次状态/进度更新时间（ISO8601，可为 `null`）。
- `published_at`：源内容在 B 站等平台的发布时间（「上传于」），由 `01_download` 写入 `input/metadata.json`，读不到则 `null`。
- `collection_name`：由 `collection_id` join 出的集合名，无归属/集合已删则 `null`。
- 每个 `steps[]` 项：`label`（步骤中文名，取自 `pipelines.yaml`，缺省 `null`）、`started_at` / `finished_at`（ISO8601，未开始/未结束为 `null`）、`duration_sec`（未完成为 `null`）、`meta`（步骤产出统计）、`error`（失败时的错误信息，否则 `null`）。

#### GET /api/jobs/{id}/concepts — 该内容命中的概念（反查）

返回 `occurrences` 含本 job 的概念（按本 job 的 `domain` 过滤；LIKE 粗筛 + 精确过滤防子串误命中）。每行是完整 glossary 行（`GlossaryTermResponse` 全字段，含 `created_at` / `updated_at`，见 1.10）外加 `job_occurrences` = 本 job 命中的出现位置数组。未找到 job 返回 `404`。

```json
[
  {
    "domain": "deep-learning",
    "term": "注意力机制",
    "definition": "...",
    "occurrences": [{"job_id": "j_xxx", "content_type": "video", "location": "scene-3"}],
    "related": ["Transformer"],
    "status": "accepted",
    "is_topic": false,
    "definition_locked": false,
    "created_at": "...",
    "updated_at": "...",
    "job_occurrences": [{"job_id": "j_xxx", "content_type": "video", "location": "scene-3"}]
  }
]
```

#### GET /api/jobs/{id}/steps/{step}/log — 步骤运行日志

返回某步骤的运行日志（经 StorageBackend 读 `logs/{step}.log`，本地/MinIO 通用），供前端展开排错。默认尾部截断到 256KB（超出时前缀一行 `...(truncated, last 256KB)...`）；`?raw=1` 返回完整日志（供下载）。Response `200` `text/plain`（UTF-8，非法字节以替换符兜底）。

```
GET /api/jobs/j_xxx/steps/10_smart/log        → 尾部 256KB
GET /api/jobs/j_xxx/steps/10_smart/log?raw=1  → 完整
```

错误：`400` 非法 step（含 `/` / `..` / 空字节）、`404` 日志不存在。

#### POST /api/jobs/{id}/retry — 重试失败任务

从失败步骤开始重跑（仅对 status=failed 的 Job）。Response `200`：
```json
{"job_id": "j_20260516_abc123", "status": "processing", "retry_from": "10_smart"}
```

#### POST /api/jobs/retry-failed — 批量重试失败任务

<!-- contract: 二期 retry-failed 加可选 collection_id 过滤(scoped 重试) -->
重试所有 `status=failed` 的 job(各自从首个失败步重跑)。可选 query `collection_id` 只重试该集合内的失败 job(不传=全局)。Response `200`：`{"retried": <int>}`。前端入口:job 列表页工具栏「重试全部失败」(全局) + 集合详情页「重试本集合失败」(scoped)。

```bash
curl -X POST 'http://localhost:8000/api/jobs/retry-failed'                       # 全局
curl -X POST 'http://localhost:8000/api/jobs/retry-failed?collection_id=col_xxx' # 仅该集合
```

#### POST /api/jobs/{id}/rerun — 强制重跑

从指定步骤开始重跑（对已完成的 Job 重新生成）。清除该步骤及所有下游的 `.done` 标记，由指纹机制决定哪些实际需要重跑。

```bash
curl -X POST http://localhost:8000/api/jobs/j_xxx/rerun \
  -d '{"from_step": "10_smart"}'
```

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "from_step": "10_smart"}
```

典型场景：对 AI 笔记质量不满意 → rerun from 10_smart → Claude 重新生成。

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
GET /api/jobs/{id}/notes/smart          → text/markdown (AI 笔记;?file= 取指定版本)
GET /api/jobs/{id}/note-versions        → application/json (智能笔记各版本+总分,见下)
GET /api/jobs/{id}/review               → application/json (评审;?file= 取版本化评审)
GET /api/jobs/{id}/assets/{filename}    → image/* (截图/图表等;长缓存)
GET /api/jobs/{id}/artifacts            → application/json (产物清单,按步骤分组;隐藏 job.json/点文件)
GET /api/jobs/{id}/artifact?path=<rel>  → 任意产物文件 (按扩展名定 content-type;仅放行已存在且未隐藏的)
GET /api/jobs/{id}/media?path=<rel>     → video/audio Range/206 流式 (<video>/<audio> 播放;单段封顶 2MB)
```

视频特有端点：
```
GET /api/jobs/{id}/notes/mechanical     → text/markdown (机械版笔记)
GET /api/jobs/{id}/notes/transcript     → text/markdown (逐字稿)
```

> 说明:源视频/音频经 `GET .../media?path=input/source.mp4` 走 Range 流式(非独立 `/source` 端点);
> 任意单个产物用 `GET .../artifact?path=<相对路径>`(非 `/output/{filename}`)。`job.json`(含凭证)
> 与 `.` 开头的内部/凭证文件一律隐藏、不可经产物端点取。`/note-versions` 返回:
> `{"versions": [{"provider","model","version","file","review_file","overall"}...]}`(按 version 倒序)。
>
> `/artifacts` 返回:`{"groups":[{"step","label","total_bytes","files":[{"path","kind","size"}...]}...],"total_bytes":<int>}`。
> `size`/`total_bytes` 为字节(本地盘 rglob 自带 st_size、MinIO list_objects 自带 obj.size,不逐文件 stat);
> `total_bytes`(顶层)=全部已分组产物体积合计,供前端透出每步/整 job 产物体积。

### 1.3 系统状态

#### GET /api/status

返回全量系统状态：`version` + 有序 `components`（系统健康总览页 §2）+ live 四段（`workers`/`pools`/`jobs`/`disk`）+ `throughput_1h`。逐组件探测各自 try+超时（redis 2s / minio 3s）：单项异常 → 该组件 `status="unknown"`（采集失败≠挂）或 `down`（连接拒绝/超时），其余照常返回，**绝不整体 500**。`components` 是**有序数组**（顺序固定 `api→scheduler→redis→minio`，前端按 `name` 作 key），便于追加新组件不破坏类型。`components.detail` 不暴露密钥/连接串。

```json
{
  "version": "0.2.0+f1d86f0",
  "//version": "FLORI_VERSION = 语义版本(pyproject [project].version)+构建短sha,如 0.2.0+f1d86f0;构建sha 未注入则仅语义版本。顶层 version = components[kind=api].version 的冗余。前端拆「+」显示 v<语义> + 构建号",
  "components": [
    {"name": "api", "kind": "api", "status": "up", "version": "0.2.0+f1d86f0",
     "last_heartbeat": "2026-06-24T07:21:55+00:00", "uptime_sec": 273840, "detail": null,
     "extra": {"rss_mb": 128.4}},
    {"name": "scheduler", "kind": "scheduler", "status": "up", "version": "0.2.0+f1d86f0",
     "last_heartbeat": "2026-06-24T07:21:54+00:00", "uptime_sec": 18290, "detail": null,
     "extra": {"loop_lag_sec": 0.8, "loop_interval_sec": 30, "pid": 7}},
    {"name": "redis", "kind": "redis", "status": "up", "version": "7.2.4",
     "last_heartbeat": "2026-06-24T07:21:55+00:00", "uptime_sec": 932011, "detail": null,
     "extra": {"used_memory_human": "48.2M", "used_memory_mb": 48.2, "maxmemory_mb": 256.0,
               "connected_clients": 11, "ping_ms": 1.2}},
    {"name": "minio", "kind": "minio", "status": "up", "version": "RELEASE.2025-09-07T16-13-09Z",
     "last_heartbeat": "2026-06-24T07:21:55+00:00", "uptime_sec": null, "detail": null,
     "extra": {"bucket": "flori", "bucket_exists": true, "probe_ms": 18.4, "mode": "remote",
               "objects": 1842, "size_bytes": 5368709120}}
  ],
  "//minio.version": "MinIO 服务端版本(经 MinioAdmin.info() 取 servers[].version,与 bucket 探活同 health 一次拉);取不到(凭证/网络/旧 SDK)或本地盘 mode=local 则为 null。失败一律吞为 null,绝不让 /api/status 变慢/报错",
  "//minio.extra.objects/size_bytes": "MinIO bucket 对象数 + 总字节。MinIO 无聚合 API → 须全量 list 求和(贵),故 api 侧后台缓存(每 600s 刷,RemoteStorage.capacity 经 to_thread),build_full_status 只读缓存;无缓存(刚起/采集失败)则不带这俩字段(前端显 —)。绝不在 /api/status 同步扫",
  "//components.status": "up|degraded|down|unknown（组件专用四态，非 worker 的 online-*/stale）。scheduler 据 component:scheduler 心跳新鲜度（复用 worker_status 的 30/900 窗口）+ loop_lag>5s 叠 degraded；redis 据 ping/内存；minio 据 bucket_exists；mode=local 时 minio=unknown（本地盘）",
  "workers": {
    "io":       {"online": 1, "busy": 0},
    "cpu":      {"online": 1, "busy": 1},
    "ai":      {"online": 2, "busy": 1},
    "gpu":      {"online": 0, "busy": 0}
  },
  "pools": {
    "io":     {"capacity": 1024, "used": 0, "queue": 0},
    "cpu":    {"capacity": 1024, "used": 1, "queue": 5},
    "ai":     {"capacity": 1024, "used": 1, "queue": 3},
    "gpu":    {"capacity": 1024, "used": 0, "queue": 0}
  },
  "//pools": "scene 已并入 cpu 池(无独立 scene 池);capacity = redis 运行时覆盖优先,否则 pools.yaml 默认(1024≈不限,实际并发由 per-worker WORKER_CONCURRENCY 控制)",
  "jobs": {"total": 44, "done": 12, "processing": 4, "failed": 1, "pending": 27},
  "disk": {"used_gb": 15.2, "available_gb": 600.0, "total_gb": 615.2, "used_pct": 2.5},
  "//disk": "total_gb/used_pct 新增（disk_usage 本就返回 total，零成本）",
  "throughput_1h": {"done": 18, "failed": 2},
  "//throughput_1h": "近 1h 进入终态的 job 计数；用 jobs.updated_at 近似终态时刻（rerun 改 updated_at 致重复计入罕见）",
  "traffic": {"pull_bytes": 12884901888, "push_bytes": 3221225472},
  "//traffic": "网关产物代理中转流量累计字节：pull=出库(NAS→worker,GET /artifacts 下发字节,即 worker 拉取)、push=入库(worker→NAS,PUT /artifacts 收到字节,即 ECS→NAS)。读 redis traffic:{pull,push}:total（§3.4）；best-effort 计数(失败回 0)"
}
```

#### GET /api/usage — AI 用量聚合

全量 AI 调用聚合（系统健康总览页「系统状态」展示）：累计 token/缓存/成本 + 平均缓存命中率 + 按 model 分。命中率 = `cache_read /(input + cache_read + cache_creation)`。

```json
{
  "calls": 128, "total_input_tokens": 410233, "total_output_tokens": 88210,
  "total_cache_creation_tokens": 51200, "total_cache_read_tokens": 302100,
  "total_cost_usd": 1.234567, "total_num_turns": 256, "total_duration_sec": 1820.5,
  "cache_hit_rate_pct": 39.6,
  "by_model": [
    {"provider": "claude-cli", "model": "claude-opus-4", "calls": 96,
     "input_tokens": 300000, "output_tokens": 60000,
     "cache_creation_tokens": 40000, "cache_read_tokens": 250000,
     "cost_usd": 1.10, "cache_hit_rate_pct": 42.4}
  ],
  "//cost": "claude-cli 订阅成本为「等价 API 成本」（非真实账单），前端按 provider==claude-cli 标「(等价)」"
}
```

#### GET /api/pricing — LiteLLM 价表状态

api 侧持有的 LiteLLM 价表元信息（系统状态页「AI 用量」卡展示）。`fetched_at` 为末次成功拉取（refresh）时间（ISO，或 `null`=从未拉到 / 仅启动时读了旧缓存且无 sidecar）。价表持久化在 MinIO 伪 job `_pricing/litellm.json`，更新时间另存 sidecar `_pricing/litellm.meta.json`（`{"fetched_at": ISO}`，价表本体不含时间戳，载入时回填）。

```json
{"ready": true, "model_count": 1342,
 "fetched_at": "2026-06-24T03:00:01+00:00",
 "source_url": "https://cdn.jsdelivr.net/gh/BerriAI/litellm@main/model_prices_and_context_window.json"}
```

#### POST /api/pricing/refresh — 手动更新价表

立即拉一次 LiteLLM 最新价表 → 更新内存 + 存回 MinIO（本体 + sidecar 更新时间）。成功返回新的 `status()`（同 `GET /api/pricing`）；上游拉取失败 → `502`（**保留旧表，绝不 crash / 不致 cost 归零**）。

#### GET /api/pricing/raw — 原始价表

返回当前内存中的原始 LiteLLM 价表全量 `dict`（key=模型名，值=单价等字段），供前端新标签/弹窗查看。空表返回 `{}`。

#### GET /api/events?limit=50 — 系统事件流

scheduler emit 的环形列表（Redis `events:system`，最近在上，保留最近 200）。scheduler 在 孤儿回收(`orphan_reclaimed`)/卡步(`step_stuck`)/无worker(`no_worker`)/worker清理(`worker_cleaned`)/任务失败(`job_failed`) 处 `push_event`；每条 `{ts, kind, job_id?, step?, pool?, reason?, error?, worker_id?}`；无事件→空数组。

```json
{"events": [{"ts": 1719100800.0, "kind": "orphan_reclaimed", "job_id": "j_abc", "step": "transcribe", "reason": "worker w_3 lost"}]}
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

`GET /api/workers` 返回的 `status` 是后端按心跳新鲜度+是否在跑+管理员叠加位读时派生的公共态（`online-idle` / `online-busy` / `offline` / `stale` / `paused`，见 §3.4）；下文示例中的 `idle`/`busy` 是历史字段示意，实际响应为派生态。

#### POST /api/workers/registration-token — 铸接入 token

铸/重置一次性接入 token（可复用、可重置，重铸即作废旧的）。远程 worker 注册时持此 token 经 `POST /api/runner/register` 换取 per-worker token（gateway 接入流程见 §1.7）。

Response `200`:
```json
{"token": "flw-xxxxxxxx"}
```

#### GET /api/workers/registration-token — 接入 token 状态

不回明文,仅状态:`{"exists": bool, "expires_in_sec": int|null}`（剩余有效秒,无过期/不存在为 null）。env `WORKER_REGISTRATION_TOKEN` 配的长期 token 不经 redis,不在此反映。路由须置于 `GET /api/workers/{id}` 之前,否则被路径参数路由遮蔽。

#### GET /api/workers/{id}/jobs — Worker 任务历史

该 worker 执行过的步骤记录。`?limit=` 默认 50，范围 1–200。

Response `200`:
```json
[
  {
    "job_id": "j_xxx", "step": "10_smart", "status": "done",
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
      "current_step": "10_smart",
      "tasks_completed": 142,
      "tasks_failed": 3,
      "total_duration_sec": 28800.0,
      "traffic": {"pull": 8589934592, "push": 1073741824},
      "first_seen": "2026-05-10T08:00:00+08:00",
      "started_at": "2026-05-17T09:00:00+08:00",
      "last_heartbeat": "2026-05-17T12:30:15+08:00",
      "admin_note": "内网机器，有 Claude Max 订阅"
    },
    {
      "id": "gpu-e5f6g7h8",
      "type": "gpu",
      "pools": ["gpu", "cpu"],
      "concurrency": 1,
      "hostname": "gpu-server",
      "gpu_name": "RTX 4090",
      "spec": {"version": "0.2.0+f1d86f0", "cpu": 16, "mem_mb": 32000, "platform": "Linux-x86_64", "python": "3.11.9"},
      "status": "idle",
      "tasks_completed": 88,
      "tasks_failed": 1,
      "first_seen": "2026-05-12T10:00:00+08:00",
      "last_heartbeat": "2026-05-17T12:30:10+08:00"
    }
  ]
}
```

> `traffic`（redis-only，默认 `{}`）：该 worker 经网关产物代理的中转流量累计字节 `{pull, push}`——`pull`=出库(NAS→worker，worker 拉取产物)、`push`=入库(worker→NAS，worker 回传产物)。按 `worker_id` 从 redis `traffic:{pull,push}` hash（§3.4）归因填充；从未中转过的 worker 为 `{"pull": 0, "push": 0}`。`GET /api/workers/{id}` 同样带此字段。

#### GET /api/workers/{id} — Worker 详情

除上述字段外，额外返回最近执行的任务历史：

```json
{
  "id": "ai-a1b2c3d4",
  "...": "...",
  "recent_tasks": [
    {"job_id": "j_xxx", "step": "10_smart", "status": "done", "duration_sec": 45.2, "finished_at": "..."},
    {"job_id": "j_yyy", "step": "11_review", "status": "done", "duration_sec": 12.1, "finished_at": "..."},
    {"job_id": "j_zzz", "step": "10_smart", "status": "failed", "error": "timeout", "finished_at": "..."}
  ]
}
```

#### PUT /api/workers/{id} — 更新 Worker 配置

```bash
# 暂停 Worker（停止认领新任务，跑完当前步后等待；服务端写独立 admin_status 叠加位，
# 与运行时 busy/idle 解耦 → busy worker 暂停后跑完当前步不会丢暂停态）
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"status": "paused"}'

# 恢复 Worker（status 传 active / idle / resume 均视为恢复）
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"status": "active"}'

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
GET  /api/config/pools                 → 当前资源池配置(pools.yaml,默认上限)
PUT  /api/config/pools                 → 热更新资源池配置(写 pools.yaml)
GET  /api/config/pool-limits           → 各池 {default(pools.yaml), override(redis 运行时覆盖,可 null)}
PUT  /api/config/pool-limits           → 运行时覆盖各池上限(写 redis、不动 pools.yaml;body {pool:int}=设、{pool:null}=清除回落默认;即时对所有 worker 含网关生效;0=暂停该池;unknown pool/非法值 400)
GET  /api/config/styles                → 可用风格标签列表
GET  /api/pipelines                    → 流水线只读:各 pipeline 步骤 DAG {name, steps:[{key,label,pool,needs:[key...]}]};needs=依赖(YAML needs→内部 depends_on),前端据此画分层 DAG;模板/'.'前缀/default 不计
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
POST   /api/runner/heartbeat                               → 刷新存活（暂停态由 claim_step 据 admin_status 兜底，不经心跳回发）；可带 load={cpu_pct,mem_pct,loadavg}（本机 live 负载，写 redis worker hash → GET /api/workers 的 worker.load）
POST   /api/runner/offline                                 → 主动下线
POST   /api/runner/jobs/request                            → 长轮询认领一步（认到即返回 enrich 后的 claim）
POST   /api/runner/jobs/{id}/steps/{step}/complete         → 上报完成
POST   /api/runner/jobs/{id}/steps/{step}/fail             → 上报失败
POST   /api/runner/jobs/{id}/steps/{step}/release          → 释放认领（不计成败）
POST   /api/runner/jobs/{id}/steps/{step}/progress         → 上报运行中进度（转发到 events:{id}）
POST   /api/runner/jobs/{id}/steps/{step}/alive            → 步进度心跳（on_tick 每 10s，仅子进程存活时；供远程 job 卡死检测）
POST   /api/runner/usage                                   → 记录一次 AI 用量（exec_id 去重）。body 含 worker_id（api 以鉴权 token 认定为准）、input/output_tokens、cache_creation/cache_read_input_tokens（命中率=read/(input+read+creation)）、cost_usd、duration_sec、num_turns、cached；claude-cli 经 `claude -p --output-format json` 取真实 usage+total_cost_usd。api 侧据 LiteLLM 价表（每天拉 `model_prices_and_context_window.json` 存 MinIO `_pricing/litellm.json`,缓存感知 per-token 单价）对**非 cli** provider 填权威 cost_usd（命中时覆盖上报值；空表/未命中回退上报值）；claude-cli 用其 CLI total_cost_usd（订阅=等价 API 成本,不覆盖）
GET    /api/runner/jobs/{id}/artifacts                     → 产物清单（GatewayStorage.pull 据此）
GET    /api/runner/jobs/{id}/artifacts/{rel}              → 取单个产物字节（计出库流量 traffic:pull，按 worker 归因；404 不计）
PUT    /api/runner/jobs/{id}/artifacts/{rel}              → 回传单个产物字节（计入库流量 traffic:push，按 worker 归因）
```

`POST /api/runner/register` Response `200`:
```json
{"worker_id": "ai-a1b2c3d4", "worker_token": "flwt-..."}
```

### 1.8 集合管理

Base: `/api/collections`。集合是内容分组；当 `source_type`+`source_id` 非空时该集合即"订阅集合"，会自动从来源追更新内容。来源由 source-adapter 模式扩展（见 `shared/subscriptions/`）。订阅没有独立实体，全部由集合的字段拼装为 `subscription` 对象返回。

<!-- contract: source_type 全量取值(2026-06-22 起六种适配器全部接线并通过测试) -->

`source_type` 取值（全部已实现并注册到 `SOURCE_ADAPTERS`，`enumerate_source` 可分派）：

| `source_type` | 来源 | `source_id` 写法 | 来源标签 | 内容类型 |
|---|---|---|---|---|
| `bilibili_up` | B 站 UP 主全部投稿 | UP 的 mid（纯数字） | `bilibili` | video |
| `bilibili_fav` | B 站收藏夹 | media_id（纯数字）或 favlist URL（取其中 `fid`） | `bilibili` | video |
| `bilibili_collection` | B 站合集/系列 | 合集/列表 URL，或紧凑式 `mid:season:sid` / `mid:series:sid` | `bilibili` | video |
| `youtube_channel` | YouTube 频道/用户全部投稿 | 频道 URL（`/@handle`、`/channel/UC...`、`/c/...`、`/user/...`）、裸 handle（`@xxx`）或裸频道 id（`UC...`） | `youtube` | video |
| `rss` | 通用 RSS/Atom feed（含 RSSHub/公众号桥、博客、arxiv、播客、YouTube 频道 RSS 等） | feed URL | `rss` | 按 entry 判定：arxiv→paper、youtube→video、audio enclosure→audio，否则 article |
| `local_dir` | 本地目录（挂进 api+worker 容器的监听目录） | 容器内绝对路径（约定 `/data/inbox`） | `local` | 按扩展名：pdf→paper、mp4/mkv/webm/mov→video、mp3/m4a/wav/flac→audio、md/txt/html→article（其它扩展名忽略） |

- 同一来源种类细分到同一**来源标签**（`SOURCE_LABELS`）：三种 B 站来源都收敛到 `bilibili`。
- 去重键 `item_id`（记在 `ingested_items` 表，按 `(collection_id, item_id)`）随来源不同：B 站=bvid、youtube=videoId、rss=entry id（缺则 link）、local_dir=`相对路径|大小|mtime秒`（文件被原地修改后 item_id 变化→重新入库）。
- `local_dir` 用 `file://` url 投递，01_download 复制源文件进 job（无网络下载）；故订阅创建/同步与 worker 必须在同一容器内能解析该路径（compose 把宿主 `${FLORI_INBOX_DIR}` 挂到 api+worker 的 `/data/inbox`，见 `docs/08-deployment`）。

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
  "last_synced_at": "2026-05-16T20:00:00+08:00",
  "last_sync_status": "ok",
  "last_sync_error": null
}
```

其中 `enabled` = 集合的 `sync_enabled`（自动追更开关），`last_synced_at` 可为 `null`（从未同步）。
<!-- contract: 二期 订阅同步状态分级,驱动侧栏/详情状态点 -->
`last_sync_status` ∈ `ok` / `error` / `syncing` / `null`（`null`=从未同步；`syncing`=同步进行中；`ok`=上次成功；`error`=上次失败，`last_sync_error` 含截断的错误摘要）。前端 5 态:订阅中(绿)/暂停(灰)/从未(琥珀)/出错(红)/同步中(蓝)。

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

请求体字段：`name`、`domain`（必填）、`description`、`tags`（默认 `[]`）、`source_type`/`source_id`（成对给出才算订阅）、`sync_now`（默认 `true`，仅订阅集合有效，建后立即首次同步）。

<!-- contract: 集合存纯名 name + 派生来源标签 source_label（不拼接入库），显示 = name + 来源徽标 -->

`name` 规则：手动集合必填；订阅集合可留空（`""` 或不传），首次同步拿到**来源真实名**（UP 真实昵称/频道名/RSS feed 标题/目录 basename）后自动命名为该**纯名**（如 `PAKEN财经说`，**不拼来源标签**）。来源名拿不到时停留在占位名（source_id）。用户显式填的名不会被自动命名覆盖。
来源标签**不入库**：由 `source_type` 派生，在响应的 `subscription.source_label`（`bilibili`/`youtube`/`rss`/`local`）返回；前端显示 = `name` + 来源徽标。`CollectionResponse.subscription` 含 `{source_type, source_id, source_label, enabled, last_synced_at, last_sync_status, last_sync_error}`。

<!-- contract: 订阅创建/同步行为 -->

订阅集合约束：`domain` 必须是真实领域，不能为空或 `general`；同一来源全局唯一（已订阅会被拒）。首次同步失败不阻塞集合创建（集合照常建好）。去重按 `(collection_id, item_id)` 记录在 `ingested_items` 表（item_id 含义随来源，见上表），跨来源统一。同步流程统一为 `enumerate_source(source_type, source_id, ctx)` 枚举来源全集 →  按 `ingested_item_ids` 去重 → 新内容自动建 job 归入本集合（适配器只枚举全集、不自去重）。

Response `201`：`CollectionResponse`。

错误：`400` 手动集合 name 为空 / 订阅集合 domain 为 general / 该来源已订阅。

#### GET /api/collections — 集合列表

```
GET /api/collections?domain=deep-learning
```

`domain` 可选，按领域过滤。Response `200`：`CollectionResponse` 数组（注意是裸数组，非 `{total, items}` 包裹）。

#### GET /api/collections/{id} — 集合详情

Response `200`：`CollectionResponse`。错误：`400` collection_id 非法（含 `..` / `/` / 空字节）、`404` 不存在。
<!-- contract: 二期 详情额外带 status_counts(集合内 job 各状态计数);列表端点该字段为 null -->
详情比列表多一个顶层 `status_counts`：本集合内 job 各状态计数,如 `{"done":1,"processing":0,"failed":2,"pending":0}`（恒含这四键、0 补齐,可能有额外状态);列表端点该字段为 `null`。供集合页显示状态分布 + 「重试本集合失败」。

#### PUT /api/collections/{id} — 修改集合

```bash
curl -X PUT http://localhost:8000/api/collections/c_xxx \
  -H "Content-Type: application/json" \
  -d '{"name": "新名字", "description": "...", "tags": ["a"], "sync_enabled": false}'
```

请求体均可选（`null`=不改）：`name`、`description`、`tags`、`sync_enabled`。`sync_enabled` 仅订阅集合可改（对普通集合传该字段返回 `400`）。Response `200`：`CollectionResponse`。错误：`400` 非法 id / 非订阅集合改 `sync_enabled`、`404` 不存在。

<!-- contract: 删除集合两模式 ?purge=false|true;均清该集合 ingested_items -->

#### DELETE /api/collections/{id} — 删除集合

两模式（query `purge`，默认 `false`）：
- `purge=false`（默认，解绑保留内容）：名下 job 的 `collection_id` 置空（job/笔记保留），删集合行。
- `purge=true`（连内容一起删，前端需二次确认）：删名下 job 行 + FTS 行（产物/MinIO 清理走既有 job 删除路径）。

两种都清该集合的 `ingested_items`（便于重订阅时重新入库）。Response `204` 无响应体。错误：`400` 非法 id、`404` 不存在。

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

`limit`（默认 20，1–200）、`offset`（默认 0，0–2147483647；int32 max,远低于 SQLite int64 溢出点,越界 422）。Response `200`：`JobListResponse`（`{total, items}`，items 为 `JobResponse`）：

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

#### POST /api/domains/{domain}/rename — 改英文标识(domain key)

<!-- contract: 二期 issue1-b 真改 domain key,事务迁移所有引用 -->
改领域英文 key。领域是派生键(无表),散在 `jobs`/`collections`/`glossary`(+ `notes_fts5` 冗余列)+ `profiles/{domain}.yaml`。一个事务原子迁移:先迁 profile 文件(可回滚)→ 再事务迁移 DB 引用,DB 失败回滚文件。

```bash
curl -X POST http://localhost:8000/api/domains/finance/rename \
  -H "Content-Type: application/json" -d '{"new_domain": "investing"}'
```

请求体:`new_domain`(必填,新键/slug)。Response `200`:`{"old","new","moved":{"jobs","collections","glossary"},"domain":<新键总览条目>}`。
错误:`400` new 非法/为空/与旧相同/old 或 new 为 `general`、`409` 目标标识已被使用(库里有行 或 profile 已存在)。

> 展示元数据(重命名/图标/配色)修改走已有 `PUT /api/profiles/{domain}`（见 1.12，`ProfileUpdateRequest` 已含可选 `display_name`/`icon`/`color`/`description`,部分合并、保留 `terminology`)。侧栏「…」菜单的「重命名/改图标配色」即调它(`stores/domains.ts` updateMeta);**不另开 domains meta 端点**,避免同一份 yaml 持久化两处分叉。**不迁移 domain key**(英文标识不变;真改 key 为二期单独迁移端点)。

#### GET /api/domains/{domain} — 领域工作台

聚合该领域的情景层（集合 + 最近内容）与语义层（概念 + 主题）。Response `200`：

```json
{
  "domain": "deep-learning",
  "stats": { "domain": "deep-learning", "collection_count": 4, "job_count": 42, "concept_count": 120, "subscription_count": 2, "last_active_at": "…" },
  "collections": [
    {"id": "c_xxx", "name": "某 UP", "job_count": 12, "is_subscription": true, "source_id": "12345678", "sync_enabled": true,
     "recent": [{"job_id": "j_xxx", "content_type": "video", "status": "done", "created_at": "…", "title": "…", "progress_pct": 100, "source": "bilibili", "domain": "deep-learning", "collection_id": "c_xxx"}]}
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
- `collections`：精简集合卡（非完整 `CollectionResponse`），`id/name/job_count/is_subscription/source_id/sync_enabled` + `recent`（**该集合各自的最近 5 条**，字段同 `recent_jobs` 项;每集合独立取,避免「全域最近 12」分组时大集合误显「暂无最近内容」）。
- `recent_jobs`：**全域**最近 12 条(供「未归集合」分组),字段同 `JobResponse` 子集（`job_id/content_type/status/created_at/title/progress_pct/source/domain/collection_id`）。
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

定义 + 出现处 + 关联概念。Response `200`，字段为 `GlossaryTermResponse`（与 `/api/glossary/{d}/{t}` 完全同形，见 1.10）：

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
  "created_at": "2026-05-16T20:00:00+08:00",
  "updated_at": "2026-05-16T20:00:00+08:00"
}
```

- `status`：`suggested`（AI 采集的候选，待审）/ `accepted`（已采纳）。
- `occurrences`：该术语出现过的来源，元素 `{job_id, content_type, location}`，由抽取步骤累积。
- `is_topic`：是否为主题概念。`definition_locked`：定义是否已钉住（钉住后自动采集不再覆盖定义）。
- `created_at` / `updated_at`：ISO8601 字符串，缺失时为 `null`。
- **同一形态**：所有返回单条术语的端点（`/api/glossary` 列表与详情、`/api/glossary/{d}/{t}`、`/api/domains/{d}/terms/{t}`）字段完全一致（后端统一走 `GlossaryTermResponse.from_row`）。

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
| `offset` | 0 | 0–2147483647（int32 max,远低于 SQLite int64 溢出点;越界 422 `invalid_request`） |

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

#### GET /api/providers — 列 AI provider 及可用性

供前端"选 provider 重跑"挑选；未配 key 的标灰（`available=false`）。本地 ollama（`local`）默认不展示。Response `200`：

```json
{
  "providers": [
    {"name": "anthropic", "type": "api", "available": true,  "label": "API"},
    {"name": "claude-cli", "type": "cli", "available": true,  "label": "订阅"},
    {"name": "openai",    "type": "api", "available": false, "label": "API"}
  ]
}
```

- `name`：provider 键（`providers.yaml` 中的键）。
- `type`：取自 provider 配置的 `type`（如 `api` / `cli`）。
- `available`：是否已具备调用条件（配了 API key 等）。
- `label`：`type == "cli"` 时为 `"订阅"`，否则 `"API"`（前端展示用）。

`POST /api/jobs/{id}/rerun-smart` 的 `provider` 必须是本端点列出且 `available=true` 的 provider。

## 2. WebSocket

鉴权：WebSocket 握手无法设置 `Authorization` 头，故 token 经 query 参数传入——
`/api/ws/jobs/{id}?token=<API_TOKEN>` 与 `/api/ws/global?token=<API_TOKEN>`。
校验策略与 REST 的 `verify_token` 一致（fail-closed）：设了 `API_TOKEN` 则必须匹配，
未设则需 `API_ALLOW_NO_AUTH=1`（仅可信内网）才放行，否则握手被 `close(1008)` 拒绝。

### WS /api/ws/jobs/{id} — 单任务进度

服务端推送事件：

```json
{"event": "step_ready",    "step": "03_scene"}
{"event": "step_start",    "step": "03_scene", "worker": "cpu-a1b2"}
{"event": "step_progress", "step": "03_scene", "current": 15000, "total": 40080, "pct": 37, "message": "scanning frames"}
{"event": "step_done",     "step": "03_scene", "duration_sec": 120.5, "meta": {"scenes": 76}}
{"event": "step_failed",   "step": "10_smart", "error": "Claude rate limit", "retries": 1}
{"event": "step_skipped",  "step": "02_whisper", "reason": "subtitle exists"}
{"event": "job_done",      "progress_pct": 100}
{"event": "job_failed",    "error": "10_smart: Claude rate limit after 3 retries"}
```

### WS /api/ws/global — 全局状态

每 2 秒推送一次 **live 子集**：`workers` / `pools` / `jobs`（含 pending） / `disk`（含 `total_gb`/`used_pct`）四段。**不含** `version`/`components`/`throughput_1h`（组件探测是慢变量，每 2s 跑会给 redis/minio 加无谓负载）——全量取 HTTP 轮询 `GET /api/status`（进页 1 次 + 每 15s + 手动刷新）。契约从「推全四段」收窄为「推 live 子集」：live 子集本就是原四段，对现有 WS 消费方无破坏。前端合并策略：WS 到达只覆盖 live 四段，`components`/`version`/`throughput` 保持上次轮询值。

## 3. Redis 数据结构

### 3.1 任务队列（Sorted Set，按优先级）

```
Key:    queue:{pool_name}
Type:   ZSET
Member: {"job_id": "j_xxx", "step": "03_scene"}  (JSON string)
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
Value:  "1" 表示冻结（保留作资源槽/前端手动冻结池用途;scene→cpu 自动冻结已移除——scene 已并入 cpu 池）

Key:    pool_limit_overrides
Type:   HASH
Fields: {pool_name: integer}    ← 池上限运行时覆盖(前端 PUT /api/config/pool-limits 写);claim 时覆盖优先于 pools.yaml 默认(1024);缺该字段=回落默认
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
  01_download:    "done"
  03_scene:       "running"
  10_smart:       "waiting"
  ...

Key:    job:{job_id}:retries
Type:   HASH
Fields: 每个步骤名 → 已重试次数
  10_smart:       "1"

Key:    job:{job_id}:step_worker
Type:   HASH
Fields: 每个 running 步骤 → 执行它的 Worker ID
  03_scene:       "cpu-a1b2c3d4"
```

### 3.4 Worker 注册

```
Key:    worker:{worker_id}
Type:   HASH
Fields:
  type:           "cpu" | "gpu" | "ai" | "io"
  pools:          "scene,cpu,io"
  tags:           "vision,claude-cli"              ← 能力标签
  reject_tags:    "private,confidential"              ← 排斥标签（可选）
  hostname:       "gpu-server" | ""
  status:         "idle" | "busy" | "offline"        ← 运行时态(busy/idle，非对外公共态)
  admin_status:   "" | "paused"                       ← 管理员暂停叠加位，与运行时 status 解耦
  current_job:    "j_xxx" | ""
  current_step:   "03_scene" | ""
  gpu_name:       "RTX 4090" | ""
  remote_addr:    "1.2.3.4" | ""                      ← 网关 worker 连接来源 IP；本机直连为空
  spec:           JSON {version,cpu,mem_mb,platform,python}  ← worker 自报版本/机器配置(redis-only,前端详情展示)
  load:           JSON {cpu_pct,mem_pct,loadavg}        ← worker 心跳自报本机 live 负载(redis-only;纯 /proc 采,各项可为 null)
  started_at:     ISO timestamp
  last_heartbeat: ISO timestamp
TTL:    30 秒（心跳续期）

Redis 为实时状态；持久记录（统计/历史/备注）存 SQLite workers 表。
```

#### 组件心跳 + 系统事件流（系统健康总览页）

```
Key:    component:{name}                                ← name ∈ {scheduler}（api/redis/minio 靠实时探活，不写心跳）
Type:   HASH
Fields: {version, started_at, loop_lag_sec, loop_interval_sec, pid, ts}  ← scheduler 每 10s 续约
TTL:    900 秒（= stale_window）：超窗 key 自动消失 → GET /api/status 读不到 → 组件 down（非永久 degraded）

Key:    events:system                                   ← 系统事件环形列表（scheduler emit；最近在上）
Type:   LIST（LPUSH + LTRIM 0 199）
Member: JSON {ts, kind, ...}  kind ∈ {orphan_reclaimed,step_stuck,no_worker,worker_cleaned,job_failed}
        供 GET /api/events?limit=50（LRANGE）。本批次 emit 接线后置，端点已就绪、空表兼容。
```

#### 网关中转流量（产物代理计数）

```
Key:    traffic:{direction}                             ← direction ∈ {pull, push}
Type:   HASH  field=worker_id  value=累计字节
Key:    traffic:{direction}:total                       ← 同方向总量(field 固定为哨兵 "_",免每次读全表求和)
Type:   HASH  field="_"  value=累计字节

pull = 出库(NAS→worker)：GET /api/runner/jobs/{id}/artifacts/{rel} 返回字节(worker 从 ECS 拉取产物)
push = 入库(worker→NAS)：PUT /api/runner/jobs/{id}/artifacts/{rel} 收到字节(worker 回传，即 ECS→NAS)

埋点在 api/routes/runner.py 的 get/put_artifact（worker_id 取自 verify_worker_token，权威）；
404/空 body 不计。**best-effort**：incr_traffic 内吞所有异常，计数失败绝不影响产物传输。
读出：GET /api/status 的 traffic 块(读 :total) + GET /api/workers item 的 traffic 字段(按 worker_id 读 hash)。
```

**公共状态是读时派生，不直接存。** 运行时 `status`（`idle` / `busy` / `offline`，worker 自报）与管理员暂停态 `admin_status`（`"" / "paused"`，仅 API 写）是**两个独立字段**；`GET /api/workers` 不信任运行时 `status`，而是按 `shared/status.py` 的 `compute_worker_status()` 用 `last_heartbeat` 新鲜度 + `current_job` + 管理员 `admin_status` 叠加位现算出对外公共态。拆成两字段是为了让 `claim/release/心跳` 写运行时 `status` 时**不会覆盖暂停态**（旧实现 draining 复用 `status` 字段会被覆盖）：

| 公共态 | 含义 |
|--------|------|
| `online-busy` | 心跳新鲜且有在跑任务 |
| `online-idle` | 心跳新鲜且空闲 |
| `paused` | 管理员置 `admin_status=paused` 且仍在线（停止认领新任务，跑完当前步后等待，恢复前不接新活） |
| `offline` | 心跳超 `online_window`（默认 30s）但未到 `stale_window` |
| `stale` | 心跳缺失或超 `stale_window`（默认 900s），GC 信号 |

判定优先级：`paused`（仅在线生效）→ `offline` → `stale` → `online-busy` → `online-idle`。窗口阈值取自 `configs/pools.yaml` 的 `worker_status` 段，缺省回退内置默认。容器跑 UTC，故由后端统一派生，前端只渲染、不再用本地时区自算。

> 暂停态的调度交互：被暂停的 worker 在 `scheduler._pool_has_workers` 里算「无可用 worker」，故只剩暂停 worker 服务的池里、已就绪的步会等待，超 `NO_WORKER_GRACE_SEC`（默认 12h）才被 fail-fast。配合「夜间只跑 io worker / 白天暂停某类 worker」的运维窗口。

### 3.5 事件发布

```
Channel: step_completed
Payload: {"job_id": "j_xxx", "step": "03_scene", "status": "done", "duration": 120.5, "worker": "cpu-a1b2"}

Channel: step_failed
Payload: {"job_id": "j_xxx", "step": "10_smart", "status": "failed", "error": "...", "worker": "ai-c3d4"}

Channel: step_started
Payload: {"job_id": "j_xxx", "step": "03_scene", "worker": "cpu-a1b2"}

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
  image: flori/step-base
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
| `run` | 步骤模块（`steps.video.step_03_scene` 等），由 worker 执行 |
| `extends` | 继承的隐藏模板名（`.cpu-step` / `.ai-step` / `.review`） |
| `needs` | 上游 job 列表，决定 DAG 顺序；无 `needs` 即可与同级并行 |
| `pool` | 资源池（io / scene / cpu / ai / gpu） |
| `image` | 步骤镜像（`flori/step-base` / `flori/step-heavy` / `flori/step-gpu`） |
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
    "01_download":
      run: steps.common.step_01_download
      pool: io
      retry: 3

    "02_whisper":
      run: steps.video.step_02_whisper
      image: flori/step-gpu
      pool: gpu
      needs: ["01_download"]
      timeout: 1800
      retry: 2
      tags: ["gpu"]
      rules:
        - exists: "input/*.srt"
          when: skip                   # 已有字幕则跳过 whisper

    "06_ocr":
      extends: .cpu-step
      run: steps.video.step_06_ocr
      image: flori/step-heavy
      needs: ["05_dedup"]
      timeout: $OCR_TIMEOUT
      retry: $OCR_RETRIES

    "08_punctuate":
      extends: .ai-step
      run: steps.video.step_08_punctuate
      needs: ["01_download"]
      timeout: 300
      retry: 3
      rules:
        - exists: "input/*.srt"
          when: on                     # 有字幕（含 whisper 产出）才标点
      ai:
        primary: {provider: $AI_PUNCT_PRIMARY_PROVIDER, model: $AI_PUNCT_PRIMARY_MODEL}
        fallback: {provider: $AI_PUNCT_FALLBACK_PROVIDER, model: $AI_PUNCT_FALLBACK_MODEL}

    "10_smart":
      extends: .ai-step
      run: steps.video.step_10_smart
      needs: ["09_mechanical"]
      tags: ["vision"]
      ai:
        primary: {provider: $AI_SMART_PRIMARY_PROVIDER, model: $AI_SMART_PRIMARY_MODEL}
        fallback: {provider: $AI_SMART_FALLBACK_PROVIDER, model: $AI_SMART_FALLBACK_MODEL}
        text_fallback: {provider: $AI_SMART_TEXT_PROVIDER, model: $AI_SMART_TEXT_MODEL}

    "11_review":
      extends: .review
      run: steps.video.step_11_review
      needs: ["10_smart"]
      ai:
        primary: {provider: $AI_REVIEW_PRIMARY_PROVIDER, model: $AI_REVIEW_PRIMARY_MODEL}
        fallback: {provider: $AI_REVIEW_FALLBACK_PROVIDER, model: $AI_REVIEW_FALLBACK_MODEL}
```

**各 content_type 的 job 链**（`needs` 推导）：

- **video**：`01_download` → `03_scene` → `04_frames` → `05_dedup` → `06_ocr`；`07_danmaku`/`08_punctuate`/`02_whisper` 由 `01_download` 旁路触发；`09_mechanical` 汇合 `06_ocr`+`07_danmaku`+`08_punctuate` → `10_smart` → `11_review`。
- **paper**：`01_download` → `02_pdf_parse` → (`03_sections`, `04_figures`) → `05_smart_paper` → `06_review`。
- **article**：`01_download` → `02_parse_article` → `03_article_sections` → `04_smart_article` → `05_review`。
- **audio**：`01_download` → `02_whisper` → `03_transcript_parse` → `04_smart_podcast` → `05_review`。

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

### 4.2.1 sources.yaml — 来源网络路由（可选）

把"哪些步骤按来源分网络出口 / 哪些来源需走出站代理"从 `scheduler` 代码外置到配置。缺此文件时 scheduler 回落内置默认（`_NET_STEPS` / `_PROXY_SOURCES`），向后兼容。

```yaml
net_routing:
  net_steps: ["01_download", "07_danmaku"]   # 受来源网络路由影响的步骤
  proxy_sources: ["youtube"]                  # 需走出站代理的来源(其余直连)
```

`enqueue_step` 对 `net_steps` 命中的步骤，按来源站点判 `net-proxy` / `net-direct`：`net-proxy` 同时进 `require_tags`（硬门控，只有声明该 tag 的 worker 能认领），`net-direct` 仅进软 `tags`。新增需代理的境外源改此 YAML 即可，不必动 Python。经 `AppConfig.net_routing` 注入；`reload_config` / `resubmit` 后即时生效。

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

`filename` 是 `assets/` 下的文件名（步骤统一命名为 `frame-{NNNN}.jpg`，四位全局自增序号 = 占位符 `[img:N]` 的 N；时间戳/场景号不进文件名，只留在本清单里），`scene_index` 标出来源场景、`source` 标出取帧方式（`scene`/`sample`）：

```json
[
  {"index": 0, "scene_index": 0, "timestamp_sec": 1.5, "filename": "frame-0000.jpg", "source": "scene"},
  {"index": 1, "scene_index": 3, "timestamp_sec": 45.0, "filename": "frame-0001.jpg", "source": "sample"}
]
```

### 4.5 dedup.json — 去重结果

在 candidates 基础上追加 `keep` / `phash`（缺图或读图异常时追加 `reason`）：

```json
[
  {"index": 0, "scene_index": 0, "timestamp_sec": 1.5, "filename": "frame-0000.jpg", "source": "scene", "keep": true, "phash": "d4c0d4e0f0f8fcfe"},
  {"index": 1, "scene_index": 0, "timestamp_sec": 15.2, "filename": "frame-0001.jpg", "source": "scene", "keep": false, "phash": "d4c0d4e0f0f8fcff"}
]
```

### 4.6 ocr.json — OCR 结果

仅对 `keep=true` 的帧做 OCR。`text` 是各识别行用换行拼接的纯文本，`boxes` 是逐行的框/置信度明细：

```json
[
  {
    "index": 0,
    "filename": "frame-0000.jpg",
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

### 4.9 evidence.json — 权威来源（案例取证，ADR-0012）

案例类笔记（`domain=finance` 或 `style_tags` 含 `case-study`）由「取证」步（`12_evidence`，video pipeline）产出 `output/evidence.json`；非案例类不产出（步内自门控 skip）。前端「权威来源」tab 经 `GET /api/jobs/{id}/evidence`（裸字节透传，未取证返回 404）渲染。

```json
{
  "schema_version": 1,
  "fetched_at": "2026-06-23",
  "ocr_refs": ["〔2018〕88号"],
  "case_match": {
    "subject": "案件一句话",
    "anchors": ["命中锚点"],
    "confidence": "high | medium | low",
    "note": "一手命中/缺口说明"
  },
  "evidence": [
    {
      "id": "E1",
      "type": "行政处罚决定 | 刑事裁定 | 公司公告 | 报道",
      "title": "标题",
      "url": "真实URL",
      "publisher": "发布方",
      "ref": "文号/案号",
      "source_tier": "一手官方 | 上市公司公告 | 媒体逐字转载 | 二手新闻",
      "match_confidence": "high | medium | low",
      "excerpt": "原文摘要",
      "key_facts": [{ "figure": "金额/数字/事实", "quote": "原文片段" }]
    }
  ],
  "notes": "取证说明",
  "parse_failed": false
}
```

- `confidence` / `match_confidence` 由**文号 case-match** 派生：抓回正文含 OCR 文号/当事人 → `high`；只对上当事人 → `medium`；对不上或仅二手 → `low`。
- `source_tier` 标来源层级；笔记引用角标 **`[E#]`** 对应 `id`：`11_smart` 据 evidence 注入来源块并以 `[E#]` 引用一手事实，`12_review` 读 evidence 核 `[E#]` 忠实性（DAG：`09_mechanical → 10_evidence → 11_smart → 12_review`）。**一手优先，抓不到如实降级，绝不用二手冒充一手。**
- 取证失败：`parse_failed: true` + 空 `evidence`。

## 5. 错误码

错误体统一为 `{"error": <机器码>, "message": <说明>}`（由 `api/main.py` 注册的 exception_handler
产出）。`error` 为 **HTTP 状态码派生的通用机器码**：

| HTTP 状态码 | error（机器码） | 说明 |
|-------------|-----------------|------|
| 400 | `bad_request` | 请求参数非法（job_id 含非法字符 / style_tags 非 JSON / collection_id 不存在 等） |
| 401 | `unauthorized` | Bearer Token 无效或未配置鉴权 |
| 403 | `forbidden` | 无权限 |
| 404 | `not_found` | 资源不存在（job / 产物文件 / 领域 等） |
| 409 | `conflict` | 资源冲突（如领域已存在） |
| 413 | `payload_too_large` | 上传文件超过 2GB |
| 422 | `invalid_request` | 请求体校验失败（FastAPI 校验） |
| 500 | `error` | 服务内部错误 |

Response body:
```json
{"error": "not_found", "message": "job not found"}
```

> 契约与实现现状（避免再漂移）：
> - `POST /api/jobs` 的 `url` 接受 http(s) 链接**或裸 B 站 BV 号**（`detect_source` 解析），不强制
>   http(s) 前缀，故不返回独立的 `invalid_url`。
> - 同 URL / 同 BV 重投**不返回 409**，而是建新任务（job_id 加随机后缀消歧），故不返回
>   `job_already_exists`。
> - 限流（429 `rate_limit`）与「无在线 worker」（503 `no_workers`）目前**未在 API 层实现**。

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
