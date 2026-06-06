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

# 文件上传（自动识别类型：mp4→video, pdf→paper）
curl -X POST http://localhost:8000/api/jobs \
  -F "file=@video.mp4" \
  -F "domain=deep-learning"

# 批量模式（创作者全部视频）
curl -X POST http://localhost:8000/api/jobs/batch \
  -H "Content-Type: application/json" \
  -d '{"bilibili_mid": "12345678", "content_type": "video", "domain": "deep-learning"}'
```

`content_type` 可显式指定，也可由 API 根据 URL/文件自动推断。

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
{"job_id": "j_20260516_abc123", "status": "processing", "rerun_steps": ["08_smart", "09_review"]}
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

```
GET  /api/auth/status                  → 各平台 cookies 状态
POST /api/auth/bilibili/qrcode         → 生成扫码二维码
GET  /api/auth/bilibili/poll?key={key} → 轮询扫码结果
POST /api/auth/youtube/cookies          → 上传 YouTube cookies.txt
```

#### POST /api/auth/bilibili/qrcode

Response `200`:
```json
{
  "qrcode_url": "https://...",
  "qrcode_key": "abc123..."
}
```

#### GET /api/auth/bilibili/poll

Response `200`:
```json
{"status": "waiting", "message": "等待扫码..."}
{"status": "scanned", "message": "已扫码，请在 App 确认"}
{"status": "success", "message": "1080P 已解锁"}
{"status": "expired", "message": "二维码已过期，请刷新"}
```

### 1.6 配置管理

```
GET  /api/config/pools                 → 当前资源池配置
PUT  /api/config/pools                 → 热更新资源池配置
```

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
  pipeline:       "video" | "paper" | "article"
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
  status:         "idle" | "busy" | "draining"
  current_job:    "j_xxx" | ""
  current_step:   "01_scene" | ""
  gpu_name:       "RTX 4090" | ""
  started_at:     ISO timestamp
  last_heartbeat: ISO timestamp
TTL:    30 秒（心跳续期）

Redis 为实时状态；持久记录（统计/历史/备注）存 SQLite workers 表。
```

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

按 content_type 定义不同的步骤 DAG。调度器根据 Job 的 `pipeline` 字段加载对应步骤链。

```yaml
# ── 视频 pipeline (M1) ──
video:
  steps:
    - name: "00_download"
      pool: io
      depends_on: []
      timeout_sec: 600
      retries: 3

    - name: "00b_whisper"
      pool: gpu
      depends_on: ["00_download"]
      condition: "no_subtitle"
      timeout_sec: 1800
      retries: 2
      tags: ["gpu"]                    # 需要 GPU

    - name: "01_scene"
      pool: scene
      depends_on: ["00_download"]
      timeout_sec: 600
      retries: 2

    - name: "02_frames"
      pool: cpu
      depends_on: ["01_scene"]
      timeout_sec: 120
      retries: 1

    - name: "03_dedup"
      pool: cpu
      depends_on: ["02_frames"]
      timeout_sec: 120
      retries: 1

    - name: "04_ocr"
      pool: cpu
      depends_on: ["03_dedup"]
      timeout_sec: 300
      retries: 2

    - name: "05_danmaku"
      pool: io
      depends_on: ["00_download"]
      condition: "has_danmaku"
      timeout_sec: 30
      retries: 1

    - name: "06_punctuate"
      pool: ai
      depends_on: ["00_download"]
      condition: "has_subtitle"        # 检查 srt 是否存在（含 whisper 生成的）
      timeout_sec: 300                 # 调度器在 00b_whisper 完成后重新检查此条件
      retries: 3
      tags: []                         # 任何 AI Worker 都能做

    - name: "07_mechanical"
      pool: io
      depends_on: ["04_ocr", "05_danmaku", "06_punctuate"]
      timeout_sec: 30
      retries: 1

    - name: "08_smart"
      pool: ai
      depends_on: ["07_mechanical"]
      timeout_sec: 600
      retries: 2
      tags: ["vision"]                 # 需要有视觉能力的 AI Worker

    - name: "09_review"
      pool: ai
      depends_on: ["08_smart"]
      timeout_sec: 120
      retries: 2
      tags: []                         # 任何 AI Worker

# ── 论文 pipeline (M1) ──
paper:
  steps:
    - name: "00_download"
      pool: io
      depends_on: []
      timeout_sec: 300
      retries: 3

    - name: "10_pdf_parse"
      pool: cpu
      depends_on: ["00_download"]
      timeout_sec: 120
      retries: 1

    - name: "11_sections"
      pool: cpu
      depends_on: ["10_pdf_parse"]
      timeout_sec: 60
      retries: 1

    - name: "12_figures"
      pool: cpu
      depends_on: ["10_pdf_parse"]
      timeout_sec: 120
      retries: 1

    - name: "14_smart_paper"
      pool: ai
      depends_on: ["11_sections", "12_figures"]
      timeout_sec: 600
      retries: 2
      tags: []

    - name: "15_review"
      pool: ai
      depends_on: ["14_smart_paper"]
      timeout_sec: 120
      retries: 2
      tags: []

# ── 文章 pipeline (M5) ──
article:
  steps:
    - name: "00_download"
      pool: io
      depends_on: []
      timeout_sec: 120
      retries: 3

    - name: "20_extract"
      pool: io
      depends_on: ["00_download"]
      timeout_sec: 60
      retries: 1

    - name: "21_smart_article"
      pool: ai
      depends_on: ["20_extract"]
      timeout_sec: 600
      retries: 2
      tags: []

    - name: "22_review"
      pool: ai
      depends_on: ["21_smart_article"]
      timeout_sec: 120
      retries: 2
      tags: []
```

新增内容类型只需在此文件添加一个 pipeline，无需改调度器/Worker 代码。

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

```json
[
  {"path": "assets/scene_0000_1.5s.jpg", "timestamp": 1.5, "source": "scene"},
  {"path": "assets/sample_0005_45.0s.jpg", "timestamp": 45.0, "source": "sample"}
]
```

### 4.5 dedup.json — 去重结果

在 candidates 基础上追加字段：

```json
[
  {"path": "assets/scene_0000_1.5s.jpg", "timestamp": 1.5, "source": "scene", "keep": true, "phash": "d4c0d4e0f0f8fcfe"},
  {"path": "assets/scene_0001_15.2s.jpg", "timestamp": 15.2, "source": "scene", "keep": false, "phash": "d4c0d4e0f0f8fcff"}
]
```

### 4.6 ocr.json — OCR 结果

```json
[
  {
    "path": "assets/scene_0000_1.5s.jpg",
    "timestamp": 1.5,
    "texts": ["0.32", "loss", "epoch"],
    "full_text": "0.32 loss epoch"
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

```json
{
  "overall": 5,
  "scores": {
    "completeness": 5,
    "accuracy": 5,
    "structure": 4,
    "terminology": 5,
    "readability": 5,
    "screenshots": 4
  },
  "missing_concepts": ["多头注意力的具体计算流程"],
  "top3_improvements": [
    "可以补充更多训练曲线的解读",
    "弹幕提到的关联论文可以展开"
  ]
}
```

## 5. 错误码

| HTTP 状态码 | 错误类型 | 说明 |
|-------------|---------|------|
| 400 | `invalid_url` | URL 格式不合法 |
| 400 | `invalid_domain` | 未知领域 |
| 400 | `file_too_large` | 上传文件超过 2GB |
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

注意：此处的重试次数和 `pipelines.yaml` 中每步定义的 `retries` 取**较小值**。pipelines.yaml 是步骤级上限，RETRY_POLICY 是错误类型级上限。
