# 03 · 接口契约

> API 端点、WebSocket 事件、Redis 消息、文件 Schema、错误码。实现时以此为准。

## 1. REST API

Base URL: `/api`

### 1.1 任务管理

#### POST /api/jobs — 创建作业(投递内容)

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

`content_type` 可显式指定，也可由 API 根据 URL 自动推断（arxiv→paper、**非 arxiv 直链 `.pdf`（OSDI/usenix 等）→paper**、播客（音频后缀）→audio、其余网页→article，无 http→video）。直链 PDF 经 `detect_source` 归为 `pdf` 源，下载步存 `input/source.pdf` 走论文流水线。

可选字段 `smart_note`（bool，默认 `null`）：是否生成 AI 智能笔记及随附评审。`null`＝按内容类型默认（`article` 默认 `false` 走轻链路；video/paper/audio 默认 `true`）。无论开关如何，**概念提取 + 一句话摘要始终生成**（article v2 的 `05_concepts` 步，进概念库/图谱）。`smart_note=false` 时跳过 `04_smart_article`/`06_review`。该开关存入 `job.meta.flags`，由调度器在 `rules.if_flag` 求值时决定相应步骤是否运行。

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

> **Job ID 格式(P2b 起)**:`jobs_{前缀}_{原生id}_{时间戳}`（所有 job 带时间戳;生成单一来源 `shared/ids.py`）。
> 同一来源内容(同 url)的多个快照(重投/来源更新/pipeline 重建)共享 **`lineage_key`**=`jobs_{前缀}_{原生id}`(去时间戳),
> 其中一个 `is_current=true`(列表/KB 默认只显该版,历史版经下方 `/versions` 跳转)。

#### GET /api/jobs — 作业列表

```
GET /api/jobs?status=processing&domain=deep-learning&source=bilibili&limit=20&offset=0
```

查询参数：`status`、`collection_id`、`domain`、`source`（均可选，AND 组合）、`limit`（默认 20，1–200）、`offset`（默认 0，0–2147483647；int32 max,越界 422）。**按 lineage 归组,默认只返各 lineage 的 current 快照**;每项含 `versions`(同源快照总数,>1 表示有历史版本)。Response `200`：
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
      "created_at": "2026-05-16T20:00:00+08:00",
      "versions": 1
    }
  ]
}
```

#### GET /api/jobs/{id}/versions — 同源快照(lineage)列表

同一 `lineage_key` 的所有快照,按 `created_at` 倒序(详情页历史版本跳转)。Response `200`：
```json
{
  "versions": [
    {"job_id": "jobs_article_ab12cd34ef_260627181500a1b2", "created_at": "2026-06-27T18:15:00+08:00",
     "is_current": true, "status": "done", "title": "示例", "pipeline_digest": "sha256:…"}
  ]
}
```
job 不存在 → `404`。

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
  ],
  "prompt_versions": {"10_smart": 2}
}
```

字段说明（除 `JobResponse` 的公共字段外）：
- `url`：原始投递 URL（上传任务可为 `null`）。
- `updated_at`：最近一次状态/进度更新时间（ISO8601，可为 `null`）。
- `published_at`：源内容在 B 站等平台的发布时间（「上传于」），由 `01_download` 写入 `input/metadata.json`，读不到则 `null`。
- `collection_name`：由 `collection_id` join 出的集合名，无归属/集合已删则 `null`。
- 每个 `steps[]` 项：`label`（步骤中文名，取自 `pipelines.yaml`，缺省 `null`）、`started_at` / `finished_at`（ISO8601，未开始/未结束为 `null`）、`duration_sec`（未完成为 `null`）、`meta`（步骤产出统计）、`error`（失败时的错误信息，否则 `null`）。
- `prompt_versions`：`{step: version}`，本任务各 AI 步派发时用的 prompt 覆盖**版本号快照**（从 `job.json.prompt_overrides[step].version` 读；无覆盖/旧 job 纯字符串形态的步不出现，故常为 `{}`）。前端 Job 详情据此与当前激活版本（`GET /api/prompts/{pipeline}/{step}`）比对，不一致时高亮提示「重跑该步」（白盒版本管理，见 §1.14）。重跑用 `POST /api/jobs/{id}/rerun {from_step}`（清该步及下游 `.done` 重跑）。

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

#### GET /api/jobs/{id}/ai-logs — 完整 AI 审计日志（prompt 白盒化）

返回该 job 各 AI 步的**完整 AI 调用审计**（只读）。读 `output/ai_logs/{step}.jsonl`（每个 AI 步、**每次 LLM 调用一条**；经 StorageBackend，本地/MinIO 通用），按 `job_id` 归成一条 trace。`?step={step}` 只返回该步。每条记录含:路由(provider/api/model/tier_used + 逐 tier `attempts` 尝试链)、延迟(ttft_ms/api_ms/总时长)、prompt(`rendered`=渲染后实际发出的 system+user[常量] / `template`=模板来源 / `values`=注入值)、输出、用量(in/out/cache)、成本、原始返回 `raw`、溯源(flori 版本/git_commit、input_hashes、env worker)、`ok`/`error`(失败调用也整条记)。落盘点 `shared/step_base.call_ai`(best-effort,不破坏主流程)。

```
GET /api/jobs/j_xxx/ai-logs                  → {job_id, steps:[{step, calls:[...]}]}
GET /api/jobs/j_xxx/ai-logs?step=11_smart    → 只该步
```

错误：`400` 非法 job_id/step。无日志时返回 `{job_id, steps: []}`（非 404）。

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

#### POST /api/jobs/{id}/rebuild — 重建为新版本快照(P2c fork)

基于当前 pipeline/prompt 定义,把该 job【fork 成一个新版本快照】(同 lineage、新时间戳 id):
clone 父 job 的产物 + `.{step}.done` 播种新 job_dir → 走 `submit_job`,worker `should_run` 指纹自动只重跑
【定义已变(`def_digest` 不符)的步及其下游】,未变步跳过;**旧版本保留供 A/B**,新版自动成为该 lineage 的 `is_current`。
(不走 `rerun(from_step)`——那 unlink 本地 `.done` 在对象存储是 no-op;用 clone 播种 + 指纹。)

Response `200`:
```json
{"job_id": "jobs_paper_xxx_260627...", "parent_job_id": "jobs_paper_xxx", "lineage_key": "jobs_paper_xxx", "status": "pending"}
```

#### POST /api/jobs/rebuild-stale — 批量重建所有过期内容(P2c)

遍历 current 作业,对【过期者】(其某步 `.done` 存的 `def_digest` ≠ 当前 pipeline 该步 `def_digest`;
缺 `def_digest` 键的老 `.done` 保守判过期)逐个走 `/rebuild`。Response `200`:
```json
{"rebuilt": 3, "items": [{"parent_job_id": "...", "job_id": "...", "from_step": "04_smart_article"}]}
```
> 「过期」判定单一来源:`shared.step_base.def_digest_for(version, ai)`(`_def_digest` 与本端点共用,防漂移);
> `job.pipeline_digest` = `pipeline_digest_for(steps)` 聚合,创建/重建时落库(供快查)。

#### DELETE /api/jobs/{id} — 删除任务

精准级联删除(顺序保证 **DB 行最后删 + 每步幂等**,崩溃可原样重删,不依赖周期 GC):
① 清 redis 队列里该 job 未认领的排队 task(`queue:{pool}` + `queue:enqueued`)+ 编排 hash + `active_jobs` + 在途延迟重试;
② 删产物(本地目录 / 对象存储 `{job_id}/` 前缀);
③ 删 DB:jobs 行 + `job_steps`(FK CASCADE)+ `notes_fts5` + **`ai_usage`** + 集合计数 -1 + 摘除 glossary 出现 + **订阅 `ingested_items`**(该条下轮订阅可重新入库)。
running job:不 kill worker,其推回结果经 `cas_step_status`(steps hash 已删→CAS 失败)被丢弃。Response `204`。
批量删除:前端逐条调本端点(无独立批量端点)。`DELETE /api/collections/{id}?purge=true` 走同款逐 job 精准级联。

> 审计:job / collection / knowledge_base 的增删改经 `shared.audit.audit(entity_type, entity_id, action, actor, detail)`
> 结构化输出(`evt=audit`)到容器日志,在 **Dozzle** 查看(不建表/不建前端页;可扩展:加新实体只传新 `entity_type`)。

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

> `job.json` 另有 `prompt_overrides`（白盒 Phase 2，见 §1.14）：`{step: {content, version}}`（1.1.5 起带激活版本号快照；兼容旧 job.json 的 `{step: content}` 纯字符串），job 创建时由 api 从 DB `prompt_overrides` 按 `(pipeline, domain)` 解析注入，worker step_base 优先用作该步默认 user-prompt 模板的替换（无外置模板的步则作 system prompt）。与 `ai_overrides`（provider 覆盖）正交。

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
  "//traffic": "网关产物代理中转流量累计字节：pull=出库(NAS→worker,GET /artifacts 下发字节,即 worker 拉取)、push=入库(worker→NAS,PUT /artifacts 收到字节,即 ECS→NAS)。读 redis traffic:{pull,push}:total（§3.4）；best-effort 计数(失败回 0)",
  "link_traffic": {
    "ts": 1782500000.0,
    "gateway": {"pull": 12884901888, "push": 3221225472, "pull_bps": 1048576.0, "push_bps": 0.0},
    "tunnel": {"rx": 52934963, "tx": 29419407, "rx_bps": 4096.0, "tx_bps": 2048.0, "up": true,
      "tunnels": [{"name": "api", "rx": 21013394, "tx": 19238566, "fwd": "127.0.0.1:8000:api:8000"}]}
  },
  "//link_traffic": "通联/链路流量【当前快照】,由 tunnel_stats 上报器(容器 flori-tunnel-stats,pid:host 读各 autossh 隧道 eth0 /proc/net/dev)周期写 redis link:traffic,/api/status 透出。gateway=远程 worker↔ECS 网关(产物代理,同 traffic);tunnel=ECS↔NAS 反向 SSH 隧道物理字节(含 api/redis/minio/dozzle/mcp 全部),up=有隧道进程,tunnels[]=每隧道累计;*_bps=上一采样周期速率(字节/秒)。按节点时间趋势走 GET /api/link-traffic/history。无上报器/无边缘 → null"
}
```

#### GET /api/link-traffic/history — 通联富时间线(按节点趋势)

通联「树」点节点/链路时取该节点的时间序列画趋势。tunnel_stats 上报器周期采样累计字节(最近在前)。`?limit=`（默认 120，封顶 360）。无上报器 → `{"samples": []}`。

```json
{"samples": [
  {"ts": 1782500000.0,
   "gw": {"pull": 12884901888, "push": 3221225472},
   "tun": {"rx": 52934963, "tx": 29419407},
   "t": {"api": {"rx": 21013394, "tx": 19238566}, "minio": {"rx": 11409690, "tx": 4168538}},
   "w": {"gpu-DXP4800": {"pull": 8000000, "push": 2000000}}}
]}
```
- `gw`=网关聚合累计、`tun`=隧道总累计、`t`=每隧道累计、`w`=每远程 worker 网关累计（cumulative;前端取相邻差算速率/趋势）。

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

#### GET /api/workers/{id}/tasks — Worker 任务(task)历史

该 worker 执行过的 task 记录(task = 某作业 job 的某步骤 step 的一次执行;每条对应一个 step 记录)。`?limit=` 默认 50，范围 1–200。
enrich 作业标题/类型(批量查 jobs 表,一次 IN),前端主显作业标题而非裸 job_id;与 `GET /api/queue` 同款 task 形态(统一 TaskRow 渲染)。

Response `200`:
```json
[
  {
    "job_id": "j_xxx", "title": "深入理解 Transformer", "content_type": "video", "domain": "ai",
    "step": "10_smart", "status": "done",
    "started_at": "2026-05-17T12:00:00Z", "finished_at": "2026-05-17T12:00:45Z",
    "duration_sec": 45.2, "error": null
  }
]
```
> `title`/`content_type`/`domain` 来自作业 enrich,作业已删/查不到则为 `null`(前端退 类型名 → 流水线 → job_id)。

#### GET /api/queue — 任务队列(排队中 + 运行中)

各资源池里【排队中】(redis `queue:{pool}` ZSET 只读窥视,ZRANGE 不弹出)+【运行中】(各 worker 当前 `current_job`/`current_step` 派生)的 task。两类都 enrich 作业标题/类型,与 worker 任务历史共用 TaskRow。`?pool=` 可选,只看单池。每池排队最多列出 200 条(`queued_count` 仍报总数,超出不静默截断)。

入队时间戳存独立 redis hash `queue:enqueued`(field=`{pool}|{job_id}|{step}`→epoch 秒),**不写入 ZSET 成员**(避免改成员破坏 ZADD 去重);enqueue 时 set、dequeue 时 hdel、return 时重置。`list_queue` 读时 join 补 `enqueued_at`(旧 task 无则 `null`)。

Response `200`:
```json
{
  "pools": [
    {
      "name": "ai",
      "queued_count": 12,
      "queued_shown": 12,
      "running": [
        {
          "state": "running", "job_id": "j_xxx", "title": "深入理解 Transformer",
          "content_type": "video", "domain": "ai", "pipeline": "video",
          "step": "10_smart", "pool": "ai", "started_at": "2026-05-17T12:00:00Z",
          "worker_id": "ai-a1b2c3d4", "worker_type": "ai", "worker_hostname": "office-pc"
        }
      ],
      "queued": [
        {
          "state": "queued", "job_id": "j_yyy", "title": "RLHF 综述",
          "content_type": "paper", "domain": "ai", "pipeline": "paper",
          "step": "10_smart", "pool": "ai", "priority": 100,
          "enqueued_at": 1747483200.0, "tags": [], "require_tags": []
        }
      ]
    }
  ],
  "limit": 200
}
```
> 运行中 task 的 `pool`/`started_at` 取自 job_steps 运行中行;无法解析归属池的运行中 task 归入名为 `(未归类)` 的兜底组(`queued` 为空)。队列是动态快照,列出瞬间可能已被认领(刷新即更新)。

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
GET  /api/pipelines                    → 流水线只读:各 pipeline 步骤 DAG {name, steps:[{key,label,pool,needs:[key...],is_ai,has_override}]};needs=依赖(YAML needs→内部 depends_on),前端据此画分层 DAG;is_ai=pool=='ai'(可编辑 AI 节点)、has_override=该 (pipeline,step) 已有 prompt 覆盖(画 ● 角标,白盒 Phase2);模板/'.'前缀/default 不计
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
| `rss` | 通用 RSS/Atom feed（含 RSSHub/公众号桥、博客、arxiv、播客、YouTube 频道 RSS 等） | feed URL | `rss` | 按 entry 判定：arxiv→paper、youtube→video、audio enclosure→audio，否则 article。**audio 条目的 `url` = 音频 enclosure 真链**（非页面 link），下载步据此取音源；`item_id` 仍用 guid/link 作去重键 |
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

#### GET /api/collections/{id}/jobs — 集合内作业列表

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

#### GET /api/domains/{domain}/concept-graph — 概念图谱（共现网络）

把该领域的概念组织成力导向图，供工作台「图谱」视图。**节点 = 概念**；**边 = 共现**：两概念若其 `occurrences` 引用同一 `job_id` 即相连，`weight` = 两者共享的 `job_id` 数。手动维护的 `related`（术语名列表，实践中多为空）叠加为额外边（权重 1；与已有共现边同一对时取较大权重）。指向不存在概念的 `related` 项忽略，自连忽略。孤立概念（无共现）仍作为节点保留（度 0）。全程按 `domain` 作用域。空领域返回空 `nodes`/`edges` 与零计数（不 404）。逻辑在 `api/services/kb.py:concept_graph`（单一来源，REST 与 MCP 工具共用）。

```
GET /api/domains/finance/concept-graph
```

Response `200`：

```json
{
  "nodes": [
    {"id": "通胀", "term": "通胀", "definition": "物价普涨。", "status": "accepted", "is_topic": true, "occurrence_count": 3},
    {"id": "利率", "term": "利率", "definition": "资金的价格。", "status": "accepted", "is_topic": false, "occurrence_count": 2}
  ],
  "edges": [
    {"source": "通胀", "target": "利率", "weight": 2}
  ],
  "stats": {"node_count": 2, "edge_count": 1, "isolated_count": 0}
}
```

- `nodes[].id` = `term`（领域内唯一）。`definition` 为短定义（首句或截断，便于节点 tooltip/侧栏）。`occurrence_count` = 该概念出现处数（节点大小 ∝ 此值）。`status` ∈ `suggested`/`accepted`，`is_topic` 标主题。
- `edges` 无向且去重，`(source, target)` 按字典序规范化方向，按 `weight` 降序、再按术语名排序。
- `stats.isolated_count` = 度为 0 的节点数。

#### GET /api/domains/{domain}/radar — 概念趋势雷达（本周知识雷达）

对比「最近 `window_days` 天」与「紧邻其前的同长窗口」，算出该领域近期的概念热度变化与新增内容，供「雷达/周报」页快速加载（**不调 LLM**）。概念出现时间口径与 concept-timeline 一致：`occurrences[*].job_id` → `job` 的 `COALESCE(published_at, created_at)`。`window_days`：默认 `7`，范围 `1..90`，越界 `422`。窗口为半开区间 `recent = [now-window_days, now)`、`prior = [now-2*window_days, now-window_days)`。空领域返回各空数组（不 404）。

```
GET /api/domains/finance/radar?window_days=7
```

Response `200`：

```json
{
  "domain": "finance",
  "rising_concepts": [
    {"term": "量化交易", "recent": 3, "prior": 1, "delta": 2}
  ],
  "new_concepts": [
    {"term": "JEPQ", "definition": "主动型高股息 ETF", "first_seen": "2026-06-22T00:00:00+00:00"}
  ],
  "recent_jobs": [
    {"job_id": "r1", "title": "量化交易入门", "published_at": "2026-06-22T00:00:00+00:00", "content_type": "video"}
  ],
  "top_recent_concepts": [
    {"term": "量化交易", "recent": 3}
  ],
  "window": {"days": 7, "since": "2026-06-19T...", "until": "2026-06-26T..."}
}
```

- `rising_concepts`：最近窗口出现次数 > 前窗口的概念，按 `delta` 降序。
- `new_concepts`：最早一次出现落在最近窗口内的概念（按 `first_seen` 降序）。
- `recent_jobs`：时间落在最近窗口内的内容（按时间降序）。
- `top_recent_concepts`：最近窗口出现最多的概念（最多 10 个）。

#### POST /api/domains/{domain}/digest — 本周摘要（按需调 LLM）

先算同款雷达，再用 `digest` builder 拼 prompt → **投递独立 AI task（`queue:ai`，§3.1）给 ai-worker** 异步生成中文周报（本周在聊什么 / 新概念 / 热点）；**API 进程不再调 claude**（claude 全在 ai-worker，用量 `ai_usage`/白盒审计 `ai_task_logs` 也在 worker 侧记，P1）。与 GET radar 分离：页面先秒开雷达，用户点「生成本周摘要」再触发本端点。`window_days` 同 radar（`1..90`，越界 `422`）。

```
POST /api/domains/finance/digest?window_days=7
```

Response `202`（投递成功；`markdown` 经 `GET /api/ai-tasks/{task_id}/result` 轮询取，digest 读 `markdown` 别名）：

```json
{
  "task_id": "at_3f9c…",
  "window": {"days": 7, "since": "2026-06-19T...", "until": "2026-06-26T..."}
}
```

投递失败（redis 不可用）→ 仍 `202`，`{"task_id": null, "window": {...}, "markdown": "⚠️ 周报生成暂不可用…"}`（优雅降级，不 5xx）。

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

#### POST /api/ask — 跨源综合问答（Cross-Source Synthesis Q&A）

自然语言提问 → 跨语料检索相关笔记 → LLM 综合出**带引用**的答案，内联标注 `[来源N]`、并附「共识 / 分歧」段。需 `verify_token`。

**检索缓解**：底层只有 FTS5 `trigram` 检索，且 `search_notes` 把整条查询当一个带引号字面短语匹配（自然语言整句几乎不可能命中）。故服务端先把问句**拆词**（去停用词/标点，CJK 连续串做 2–4 字滑窗，ascii 词保留）并叠加**术语表里出现在问句中的术语**，得到一组（≤6）派生查询，分别打 FTS 后**并集去重**（保留最佳 rank），取前 `limit` 篇，批量拉取整段正文（每段截断 ~4000 字）喂给 LLM。综合走 `claude-cli` 订阅。

请求体：

| 字段 | 默认 | 说明 |
|------|------|------|
| `question` | （必填，≥1 字） | 自然语言问题 |
| `domain` | `null` | 限定知识库（domain）；`null`=全库 |
| `limit` | 8 | 检索并喂给 LLM 的最大笔记数（1–20） |

```bash
curl -X POST http://localhost:8000/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"反向传播和梯度下降有什么区别？","domain":"deep-learning"}'
```

**异步**：检索/拼 prompt 后**投递独立 AI task（`queue:ai`，§3.1）给 ai-worker**（claude 全在 ai-worker，P1），API 不在进程内调 claude。

Response `202`（`sources` 提交时已算好；`answer_markdown` 经 `GET /api/ai-tasks/{task_id}/result` 轮询取，ask 读 `answer_markdown` 别名，内 `[来源N]` 与 `sources` 下标 +1 对应）：
```json
{
  "question": "反向传播和梯度下降有什么区别？",
  "task_id": "at_8b2e…",
  "answer_markdown": null,
  "sources": [
    {"job_id": "j_bp", "title": "反向传播详解", "domain": "deep-learning", "content_type": "video"}
  ],
  "retrieved_count": 1
}
```
- 命中为 0 → `task_id:null`、`answer_markdown` 为固定提示文案、`sources:[]`，**不投 task**（短路）。
- 投递失败（redis 不可用）→ `task_id:null` + 降级文案 + 已检索 `sources`（不 5xx）。

用量（`ai_usage`，`step=synthesis`，`job_id=null`）与白盒审计（`ai_task_logs`）由 **ai-worker** 记账（P1-2），API 不再记。

#### GET /api/ai-tasks/{task_id}/result — 独立 AI task 结果（轮询）

`/ask`、`/digest` 提交的 AI task 的结果。读 `airesult:{task_id}`（§3.1，worker 写，TTL≈600s）：

| status | 响应 |
|---|---|
| `pending` | 未就绪（worker 没跑完/已过期）：`{"status":"pending","task_id":...}` |
| `error` | 失败：`{"status":"error","task_id":...,"error":"..."}` |
| `done` | 完成：`{"status":"done","task_id":...,"content":"...","answer_markdown":"...","markdown":"...","provider":...,"model":...,"cost_usd":...}` |

`answer_markdown`/`markdown` 均 = `content`（ask 读前者、digest 读后者）。前端也可 `WS /api/ws/jobs/{task_id}` 收 `ai_task_done`（§3.5）后再取本端点。

#### GET /api/ai-tasks/{task_id}/log — 独立 AI task 白盒审计

镜像 DAG 的 `GET /api/jobs/{id}/ai-logs`：读 `ai_task_logs`（§3.1），返回该 task 每次 claude 调用的完整审计（路由/尝试链/渲染 prompt/输出/raw/用量），最近在前。

```json
{"task_id":"at_…","count":1,"calls":[{"task_id":"at_…","exec_id":"…","step":"synthesis","domain":"ml","provider":"claude-cli","model":"subscription","ok":true,"error":null,"created_at":"…","record":{"routing":{"attempts":[…]},"prompt":{"system":"…","messages":[…]},"output":"…","raw":{…},"usage":{…}}}]}
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

### 1.14 Prompt 白盒（`/api/prompts/*`，Phase 2：网页编辑每步 prompt 覆盖）

让每个 AI 步的默认 prompt **可见、可改**（白盒）。覆盖存 DB 表 `prompt_overrides`，主键 `(scope, domain, pipeline, step)`，列 `content`/`version`/`updated_at`；`scope='global'`（`domain=''`）或 `'domain'`（`domain=域名`）。**注入机制**（同 §1.1 `ai_overrides` 走 job.json 的范式）：job 创建时由 api（有 DB）调 `resolve_prompt_overrides(pipeline, domain)`（domain 覆盖优先于 global）解析出 `{step: {content, version}}`，写进 `job.json.prompt_overrides` 随 job 下发；worker（pure，无 DB）据此应用（兼容旧 job.json 的纯字符串形态，见 §1.1 `job.json`）。

**版本管理（类 Grafana save，1.1.5/1.1.6）**：每个 `(scope, domain, pipeline, step)` 覆盖带【版本历史】，存 DB 表 `prompt_override_versions`（主键加 `version`，列 `content`/`note`/`created_at`）；主表 `prompt_overrides.version` 是【激活指针】，`content` 仍存激活版本内容供注入快读。保存有两种动作：**「覆盖当前版本」**（`mode=overwrite`，改激活版本行内容，版本号不变）/ **「另存为新版本」**（`mode=new`，`version=max+1` 并设为激活）；首次保存恒为 `v1`。job 创建时注入的版本号即【当时激活版本】的**快照**，供 Job 详情比对「本任务用 vX vs 当前 vY」（见 §1.1 `GET /api/jobs/{id}` 的 `prompt_versions`）。

**激活/停用（非破坏，1.1.10）**：「回内置默认」与删历史**解耦**。`POST .../activate {version|null}`：① `version=数字` → 把某历史版本**设为当前激活**（re-activate，派发即用它）；② `version=null` → **停用覆盖回内置默认**（deactivate）——只删主表激活指针那一行，`prompt_override_versions` 历史**全部保留**（下拉里仍能看到 v1/v2…，可随时再激活），`resolve_prompt_overrides` 据此返回空 → 派发用内置默认。`version` 列 `NOT NULL DEFAULT 1`（不可空），故 deactivate 用「删激活行」表达，而非置 NULL；主表因此【可缺行而历史仍在】。`DELETE`（彻底删除，连同全部历史）保留为可选的真删除入口，**不再**充当「恢复默认」。

**所见即所改**（1.1.0 起对齐）：编辑器展示的默认 = 该步外置 **user-prompt 模板**（`prompts/templates/{step}.md`，含 `08_punctuate.zh`/`08_punctuate.translate`/`11_smart.vision` 等变体），覆盖替换的就是它。worker `_load_prompt_template` 回退顺序 = **DB 注入覆盖 > 模板文件 `templates/{name}.md` > 内联默认**；覆盖只作用于该步的覆盖目标 name（`name==step`，或无同名主模板时的变体 name——如 11_smart 仅主模板 `11_smart` 吃覆盖、变体 `11_smart.vision` 不吃；08_punctuate 无主模板，覆盖落到当次加载的 `.zh`/`.translate`）。**评审步**（`05_review`=audio、`06_review`=paper+article、`12_review`=video；article 步名 1.1.4 起对齐 yaml `06_review`）自 1.1.4 起也外置了**可编辑骨架模板** `templates/{step}.md`（四条 pipeline 评审结构一致 → 三个文件内容相同,占位 `{{intro}}`/`{{dimensions}}`/`{{score_example}}`/`{{ref_block}}`,由 `build_review_prompt` 运行期 `str.replace` 按本步实参注入；**`score_keys` 仍只从步内 dimensions 代码取,绝不解析模板文本** → 覆盖文本被改坏也不破坏评分 JSON 解析；缺 `{{ref_block}}` 占位时参照块兜底补在末尾）——故评审覆盖同走 user-prompt 模板层（所见即所改）,GET 返回非空 default,**不再回落 system**。**仍无任何外置模板的 AI 步**覆盖才回落为 system prompt（`_load_system_prompt`，回退 = DB 覆盖[仅无模板步] > `prompts/{step}.md` 钩子 > None）。**模板读取双保险**：api 优先读运行时 `prompts_dir/templates`，缺失回退镜像烤入 `config_dir/prompts/templates`（命名卷会 shadow 烤入的 `/data/prompts`，故 api 须额外挂 `templates` 子路径或靠此回退）。Phase 1 的 AI 审计日志 `template.source` 据此标 `db_override`（注入命中）/`override`（`{step}.md` 钩子）/`default`。

```
GET    /api/prompts                                      → 列各 pipeline 可编辑 AI 步 + 已有哪些覆盖
GET    /api/prompts/{pipeline}/{step}                    → 单步默认模板(只读,全变体)+ system 钩子 + 该 (scope,domain) 当前覆盖 + active_version + versions[]
GET    /api/prompts/{pipeline}/{step}/versions/{version} → 查看某历史版本的完整内容(?scope&domain)
PUT    /api/prompts/{pipeline}/{step}                    → 存该步 prompt 覆盖(mode=overwrite|new + note;content 纯空白=彻底删除清全部版本)→ active_version
POST   /api/prompts/{pipeline}/{step}/activate           → 切激活指针:{version:数字}=设该历史版本为激活;{version:null}=停用回内置默认(非破坏,留历史)→ active_version
DELETE /api/prompts/{pipeline}/{step}                    → 彻底删除该 (scope,domain) 覆盖(连同全部历史版本;非「恢复默认」入口)
```

#### GET /api/prompts

Response `200`：`steps` 为四条 pipeline 的全部 AI 步（`pool=='ai'`）。`overrides` 列出该步已有的覆盖（供设置页标 ●）。
```json
{
  "steps": [
    {"pipeline": "video", "step": "11_smart", "label": "智能笔记", "pool": "ai",
     "is_ai": true, "has_template": true,
     "overrides": [{"scope": "global", "domain": ""}, {"scope": "domain", "domain": "finance"}]}
  ]
}
```

#### GET /api/prompts/{pipeline}/{step}?scope=&domain=

`scope` 默认 `global`；`domain` 仅 `scope=domain` 时有意义。Response `200`：
```json
{
  "pipeline": "video", "step": "11_smart", "label": "智能笔记", "pool": "ai", "is_ai": true,
  "default_template": "...(主模板 prompts/templates/11_smart.md 内容;无主取首个变体;全无则 null)...",
  "default_templates": [
    {"name": "11_smart", "content": "...(主)..."},
    {"name": "11_smart.vision", "content": "...(变体)..."}
  ],
  "default_system": null,
  "override": {"scope": "global", "domain": "", "content": "你是...", "version": 2, "updated_at": "..."},
  "active_version": 2,
  "versions": [
    {"version": 1, "note": "首版", "created_at": "..."},
    {"version": 2, "note": "加了配图要求", "created_at": "..."}
  ]
}
```
- `default_template`：向后兼容字段 = 主模板内容（`{step}.md`，无主则首个变体，全无 `null`）。
- `default_templates`：该步全部外置模板 `[{name, content}]`（主 + 变体，主在前）；空数组 = 该步无外置模板（prompt 内联）。
- `default_system`：外置 system 钩子 `prompts/{step}.md` 内容；多数步为 `null`。
- 双保险:api 没挂 `templates` 时仍从镜像烤入 `config_dir/prompts/templates` 读到上述默认。
- `override` 无覆盖时为 `null`。step 不属于该 pipeline → `404`。
- `active_version`：当前激活版本号（无激活指针 `null`——含「从未覆盖」与「已 deactivate 停用」两态）；`versions`：该 `(scope,domain)` 全部历史版本元信息（`[{version, note, created_at}]`，`version` 升序，不含 `content`）。**deactivate 后 `active_version=null` 但 `versions[]` 仍非空**（历史保留，可再激活）。

#### GET /api/prompts/{pipeline}/{step}/versions/{version}?scope=&domain=

查看某历史版本的**完整内容**（供编辑器「选历史版本」载入后基于它改）。Response `200`：
```json
{"version": 1, "content": "...(该版本 prompt 全文)...", "note": "首版", "created_at": "..."}
```
- `scope` 默认 `global`；`domain` 仅 `scope=domain` 时有意义。该版本不存在 → `404`。

#### PUT /api/prompts/{pipeline}/{step}

请求体 `{scope, domain?, content, mode?, note?}`。`content` 即覆盖内容（替换该步展示的默认 user-prompt 模板；无模板步作 system prompt）。纯空白即视为**彻底删除**该覆盖（连同全部版本历史）——若只想回内置默认而**保留历史**，用 `POST .../activate {version:null}`。
- `mode`：`overwrite`（默认，改当前激活版本内容，版本号不变）或 `new`（另存为新版本 `version=max+1` 并激活）；首次保存恒为 `v1`（`mode` 忽略）。`note`：该版本一行备注（可空；`overwrite` 留空则保留原 note）。
- `scope='domain'` 但 `domain` 空 → `400`；step 非 AI 步（`pool!='ai'`）→ `400`；step 不存在 → `404`。
- 成功保存 `{"status": "saved", "active_version": <新激活版本号>, ...}`；空内容删除 `{"status": "deleted", ...}`。

```json
{"scope": "global", "content": "你是资深技术编辑,产出结构化中文笔记...", "mode": "new", "note": "加了配图要求"}
```

#### POST /api/prompts/{pipeline}/{step}/activate

切换该步 `(scope,domain)` 的【激活指针】，**非破坏**（历史版本始终保留）。请求体 `{scope, domain?, version}`：
- `version=<数字>` → 把该**历史版本设为当前激活**（re-activate）：主表 `content`/`version` 同步成该版本，下次派发用它。该版本不存在 → `404`；成功 `200 {"status":"activated", "active_version": <version>, ...}`。
- `version=null` → **停用覆盖回内置默认**（deactivate）：删主表激活指针，`prompt_override_versions` 历史**全部保留**；`resolve_prompt_overrides` 随即返回空 → 派发用内置默认。成功 `200 {"status":"deactivated", "active_version": null, ...}`。
- `scope='domain'` 但 `domain` 空 → `400`；step 非 AI 步（`pool!='ai'`）→ `400`；step 不存在 → `404`。

```json
{"scope": "global", "version": 2}
```

#### DELETE /api/prompts/{pipeline}/{step}?scope=&domain=

**彻底删除**该 `(scope,domain,pipeline,step)` 覆盖（连同其全部版本历史）。无则 no-op。Response `200 {"status":"deleted",...}`。注：这不是「恢复默认」入口（那会删历史）——回内置默认且保留历史请用 `POST .../activate {version:null}`。

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

**独立 AI task（P1 AI-worker-split）**：`/api/ask`、`/digest` 把单次 claude 调用作为独立 task 投进 `queue:ai`，由 ai-worker（`claude-cli` tag）执行——**不挂 job、不走 storage**，载荷与结果都内联。member 形态带 `kind:"ai"`（与 pipeline-step task 区分）：

```
Key:    queue:ai
Member: {"kind":"ai","task_id":"at_xxx","step":"synthesis|digest","domain":"<domain>|null",
         "request":<LLMRequest jsonable>,"tags":[...],"require_tags":["claude-cli"],"pool":"ai"}  (JSON string)
```

- `request` = `shared.models.LLMRequest.to_jsonable()`（messages/system/max_tokens/temperature/allowed_tools…；images 序列化为 str 路径，AI-RPC 路径一般不带图）。
- `require_tags:["claude-cli"]` → 只有持订阅/凭证的 ai-worker 认领（无凭证 worker 不会领了再运行时失败）。
- pipeline-step task 的 member **不带 `kind`**（向后兼容，缺省即 `step`）。
- `queue:enqueued` field（§3.x 等待时长用）：step task=`{pool}|{job_id}|{step}`；**ai task=`{pool}|ai|{task_id}`**。
- 结果回执：`airesult:{task_id}`（STRING，JSON = `LLMResponse.to_jsonable()` 或 `{"error":"..."}`，带 TTL≈600s），供 API 端 `GET …/result/{task_id}` / 同步等待取回（P1-3）。AI 用量经 `ai_usage`（`job_id=null, step=<step_name>`）记账（worker 侧，P1-2）。
- 完成事件：worker 执行后 `publish events:{task_id}`（`ai_task_start/ai_task_done/ai_task_failed`，见 §3.5），供 `/ask`、`/digest` 经 `WS /api/ws/jobs/{task_id}`（端点对任意 id 通用）或轮询取信号。
- **白盒审计**：ai-worker 每次执行写一条 DB 表 **`ai_task_logs`**（按 `task_id`；对齐 DAG 步的 `output/ai_logs/{step}.jsonl`）。索引列：`exec_id/step_name/domain/provider/model/ok/error/各 token/cost_usd/duration_sec/num_turns/created_at`；`record_json` 存全量审计（路由/尝试链/渲染 prompt[system+messages]/输出/raw/用量）。与 `ai_usage`（成本归因）**并存不合并**（白盒 vs 计费两套）。查看端点见 P1-3。

### 3.2 资源池计数（holder 集合，根治幽灵泄漏）

并发槽不再用裸计数器，改用 **holder 集合**：holder = `exec_id`（worker 认领时生成的唯一执行 id，`{worker_id}:{ms}:{rand}`）。
占槽 = `SADD holders exec_id`（Lua：未 frozen 且 `SCARD < limit`；同一 exec_id 重占幂等放行）；放槽 = `SREM holders exec_id`（**幂等**——worker finally / 调度器 reclaim / 删 job 多方释放同一 holder 都安全，不双减）；`used = SCARD`。worker 突死/删 running job 漏放的陈旧 holder，由调度器周期 `reconcile_slots`（连续两拍不属任何 running 步才清，避开认领窗口）SREM 收敛。

```
Key:    pool:{pool_name}:holders        ← 旧 pool:{pool_name}:count(STRING 计数器)已废弃,新代码读/写本 SET
Type:   SET
Members: 当前持槽的 exec_id 集合;已占槽数 = SCARD

Key:    res:{resource}:holders          ← 细粒度资源槽(单账号/单出口IP)同机制,同 Lua
Type:   SET
Members: 当前持该资源槽的 exec_id 集合;已占数 = SCARD

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

Channel: events:{task_id}            ← 独立 AI task(kind='ai')执行事件,供 /ask、/digest 的 ws/轮询取信号
Payload: {"event":"ai_task_start|ai_task_done|ai_task_failed","task_id":"at_xxx","step":"synthesis|digest"[,"error":"..."]}
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
      timeout: 1800                     # 静态下限(短集)
      timeout_per_min: 90              # 可选:超时随媒体时长伸缩(每分钟音/视频 90s 墙钟预算)
      timeout_max_sec: 21600           # 可选:动态超时上限(6h,防失控)
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
- **paper**：`01_download` → `02_pdf_parse` → (`03_sections`, `04_figures`) → `04_translate_paper`(条件) → `05_smart_paper` → `06_review`。
  - `02_pdf_parse` 检测正文主语言写 `parsed.json.lang`(判据与文章共用 `steps.utils.lang`);**非中文**额外写 `intermediate/needs_translation.json`;元信息(title/authors/abstract/pages/lang/**venue**)经 JobDetail `media` 透出「元信息」tab(与文章同)。
  - **来源(`media.venue` / `media.sitename`)**:论文 `02_pdf_parse._extract_venue` 抽会议/期刊+年份(`OSDI 2023`/`arXiv`;全名→缩写映射在 `configs/venues.yaml`,配置与代码分离);文章 `02_parse_article` 取 trafilatura `sitename`(URL 域名兜底)。前端「来源」= `venue`/`sitename`,无则回退来源类型标签。
  - **arxiv 元数据**:`01_download._fetch_arxiv_meta` 查 arxiv API(`export.arxiv.org/api/query`,feedparser 解析)→ `title/authors/abstract/published_at` 写入 `input/metadata.json`(权威来源);`02_pdf_parse` 优先用其 `title/authors/abstract` 覆盖 PDF 启发(PDF 标题常误抓左边距 arXiv 戳、作者为空)。best-effort,API 失败回退 PDF 解析。
  - `04_figures`:**按图注渲染 PDF 页面区域**为图(`get_pixmap(clip=区域)`,矢量+栅格通吃;旧 `get_images` 只能抽栅格、漏矢量正文图)。区域 = 图注上方、同列(按图注所在文本块宽判单/双栏)、`drawings`+图片矩形并集。`intermediate/figures.json` = `[{id, page, caption, filename, index, ocr_text}]`:`filename`=渲染图(`assets/figure-NNNN.png`,经 `GET /api/jobs/{id}/assets/{filename}`),渲不出图者 `filename/index` 为 `null`(仅文字图注);`index`=`05_smart` 内联占位符 `img:N` 的 N。前端「图表」tab 直接展示(不依赖智能笔记/AI worker)。
  - `04_translate_paper`(AI,`rules.exists` 门控,仅非中文论文):章节文本忠实翻译为简体中文 → `output/translated.md`(保留结构/公式/图表引用),供「译文」tab。
  - `05_smart_paper` `needs` 含 `04_translate_paper`:非中文论文笔记**基于 章节+图表+译文**(有译文则用译文正文);译文跳过(中文论文)依赖视为满足、读原文。
- **article**：`01_download` → `02_parse_article` → `03_article_sections` → `04_smart_article`(可选,`smart_note`)/`04_translate_article`(条件) → `05_concepts`(必跑) → `06_review`(可选)。
  - `02_parse_article` 检测正文主语言写入 `parsed.json` 的 `lang`(`zh`/`non-zh`/`unknown`);**非中文**正文额外写标记 `intermediate/needs_translation.json`。
  - **空正文护栏**:`02_parse_article` 抽出的正文【有效字符数】(去空白后)< `MIN_BODY_CHARS`(=200)→ 直接抛 `InputInvalidError`(`error_type=input_invalid`,不重试),**不写任何产物**,job 明确 `failed`。挡付费墙/JS 渲染/订阅残桩页(抽 0 或极短正文)流向 `03/04/05` 的 AI 步——否则 LLM 拿空正文幻觉编笔记/概念,污染概念库与图谱(`key_terms` 是图谱唯一概念来源)。命中常见付费墙/登录墙标记仅细化错误信息,判废只看正文长度。
  - `04_translate_article`(AI):`rules.exists: intermediate/needs_translation.json` 门控——**仅非中文文章触发**,把 `output/original.md` 忠实全文翻译为简体中文 → `output/translated.md`(保留 Markdown 结构与 `![](assets/…)` 图位),供前端「译文」tab。
  - `04_smart_article` / `05_concepts` **`needs` 含 `04_translate_article`**:非中文文章的中文产出**基于译文**(术语一致,不重复英→中)——`04_smart` 有 `translated.md` 则笔记基于译文;`05_concepts` 源优先级 **智能笔记 > 译文 > 原文章节**。译文被跳过(中文文章)时依赖视为满足,两步照常读原文。
- **audio**：`01_download` → `02_whisper` → `03_transcript_parse` → `04_smart_podcast` → `05_review`。
  - `01_download`(`content_type=audio`):支持音频直链(`.mp3/.m4a/.wav/.aac/.flac`)与播客**页面 URL**(best-effort 从页面 `og:audio`/`<audio>`/`<source>`/`<enclosure>`/裸 `*.mp3` 链解析音频真链);下载后 **ffprobe 校验**(无可解码时长=拿到 HTML/404 → `InputInvalidError`,不再拖到 whisper 才报晦涩 ffmpeg 错)。
  - `02_whisper`:超时**随时长伸缩**(见 `timeout_per_min`/`timeout_max_sec`)——无 GPU 时长集 CPU 转写远超固定 1800s。worker 跑步前读 `input/metadata.json.duration_sec`,有效超时 = `clamp(max(timeout, ceil(分钟)*timeout_per_min), timeout_max_sec)`;缺 `timeout_per_min` 或读不到时长则用静态 `timeout`(行为不变)。机制通用,任何步均可在 pipeline 加这两字段启用。
  - `04_smart_podcast`:**不再 12k 截断**。转写 ≤ `SINGLE_PASS_CHAR_LIMIT`(24000 字)单次成稿;超过则 **map-reduce**(按 segment 边界分段提炼要点 → 合并成完整笔记),覆盖全集不丢正文。`result.meta` 增 `mode`(`single`/`map_reduce`)与 `chunks`。

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

### 4.2.1 sources.yaml + net-zone 网络区域路由

下载步（`net_steps`：`01_download` / `07_danmaku`）按**网络可达区域**路由,支持分布式 worker（NAS 国内 / 香港 ECS 等）。配置外置到 `configs/sources.yaml`,缺此文件回落内置默认（`_NET_STEPS`）。

```yaml
net_routing:
  net_steps: ["01_download", "07_danmaku"]   # 受网络区域路由影响的步骤
```

**区域 tag（只有两个）**：`net-cn`（大陆视角,B站等 geo 限大陆站可达）/ `net-global`（可达国际站）。**旧 `net-proxy` / `net-direct` / `bili` 路由 tag 已移除。**

- **worker 自动探测**（`worker/worker.py:_probe_net_zones`）：启动试连探针 URL（`NET_PROBE_CN` 默认 `api.bilibili.com`、`NET_PROBE_GLOBAL` 默认 `github.com`,用自己网络含自带代理）→ 通则自报对应 zone tag。`NET_ZONES=cn,global` 可强制覆盖（如香港 worker 设 `NET_ZONES=global`）。探针 URL 是**启动配置**（compose `common-env` 注入,**不烤镜像**）。worker 详情页展示「可达区域」。
- **URL→区域分类**（`shared/net_zone.py:required_zone`,**任务分发时判**）：平台源 `bilibili`→net-cn、`youtube`→net-global（权威）；其余按 host 查 **CN 域名表** + `.cn` TLD → net-cn,否则 net-global。CN 表 = `felixonmars/dnsmasq-china-list`,**构建时拉取烤进镜像** `/app/data/cn_domains.txt`（`base.Dockerfile`,`USE_USTC_MIRROR=1`→jsdelivr/ghproxy 国内源优先；约 11 万域名；失败回退仅 `.cn` TLD）。
- **路由**：`enqueue_step` 对 `net_steps` 步设 `require_tags += [zone]`（硬门控,只有自报覆盖该区域的 worker 能认领）；境外 URL→net-global→香港/带代理 worker,都没有则等待（不误派到到不了的 worker）。代理这件事完全是 worker 本地的事,scheduler 不碰代理。
- **B站登录态**：`bili` 路由 tag 已删；SESSDATA 经 **per-job 凭证文件**传给 worker（`create_job_core` 写 + 下载步 `step_01` 自读）,与区域路由正交。

经 `AppConfig.net_routing` 注入；`reload_config` / `resubmit` 后即时生效。

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

---

## MCP(把知识库作为 MCP 提供给 agent)

<!-- contract: 借鉴 Notion — 单 server 管整库 + 工具少而精 + Markdown 输出;domain 作用域;非一库一 server。 -->
模块 `api/mcp_server`(模块名避开 pip `mcp` SDK 包)。只读;工具薄包 `api/services/kb.py`(单一来源,
与未来 FastAPI 路由共用)。检索后端可插拔(v1 `FtsSearch` 包 notes_fts5;v2 可换 sqlite-vec 语义,工具签名不变)。

<!-- contract: 单一 HTTP 传输 -->
**传输**(`python -m api.mcp_server`,streamable-http,**仅此一种**,stdio 已移除):uvicorn 监听 `MCP_PORT`(默认 8090),端点路径 **`/mcp`**;经 Caddy 暴露到公网。
  · 按库作用域:用路径 **`/mcp/{domain}`**(由 DomainScopeASGI 中间件处理),无需每库起进程;或 env **`FLORI_MCP_DEFAULT_DOMAIN=<domain>`** 设全局默认库。
  · 鉴权:**`Authorization: Bearer <FLORI_MCP_TOKEN>`**。fail-closed(对齐 API):设了 `FLORI_MCP_TOKEN`→不匹配 401;
    未设→503,除非 `FLORI_MCP_ALLOW_NO_AUTH=1`(仅可信内网放行)。compose 服务 `mcp-http`(profile `mcp`,默认绑 127.0.0.1)。
  · <!-- contract: MCP-http 限流 429 -->**限流**:`RateLimitASGI`(最外层,鉴权之前)进程内全局时间窗计数器,
    上限 env **`FLORI_MCP_RATE_LIMIT`**(请求/分钟,默认 120;`0`/留空=关闭)。超限 → **`429`**,体 `{"error":"rate_limited"}`,带 `Retry-After: 60`。lifespan 等非 http scope 不计数直通。
  · curl 冒烟:`curl -H "Authorization: Bearer $TOK" -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" -X POST https://<host>/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`

<!-- contract: 按库作用域端点 /mcp/{domain} —— 单 server + contextvar,非一库一 server -->
**按库作用域端点 `/mcp/{domain}`**(给某 agent 一个只见某知识库的 MCP):
- 仍是**同一个** MCP server。`DomainScopeASGI` 中间件(在 Bearer 鉴权内层)把 `/mcp/{domain}` 及子路径
  **改写为 `/mcp[/...]`**(streamable_http_path 是 `/mcp`),并经请求级 contextvar 给工具一个「生效 domain」。
- 该端点下工具**自动锁定**该库,无法越库:`search` 忽略入参 domain 强制锁定;`list_knowledge_bases` 只回该库一条;
  `get_note` 校验 job 归属(越库视同 not-found,不泄露其它库笔记);`get_glossary/get_term/concept_timeline/list_collections`
  的 domain 默认/覆盖为作用域。精确 `/mcp`(无 domain 段)= 全局端点,行为不变。
- 鉴权同 `/mcp`(Bearer)。**Caddy/隧道无需改**:`/mcp*` 路由按前缀已覆盖 `/mcp/{domain}`。
  · curl:`... -X POST https://<host>/mcp/<domain> -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
v2(未做):写工具(submit);sqlite-vec 语义后端。

**接入信息端点**(供系统页「接入 MCP」卡片渲染;只读,挂 /api 经 Caddy basic_auth / API_ALLOW_NO_AUTH 收口):
- `GET /api/mcp/info` → `{enabled, http_path:"/mcp", local_url:"http://127.0.0.1:<MCP_PORT>/mcp", token_configured:bool, tools:[{name, description}], stats:{total:int, by_tool:{name:int}}}`。tools 从 MCP server 实时派生(不写死);不回传 token 明文。**接入统一走 HTTP(streamable-http + Bearer),不再用 stdio**:前端本地端点 = `local_url`(同机直连 mcp-http)、公网端点 = `window.location.origin + http_path`,仅 URL 不同。<!-- contract: stats = MCP 工具调用计数 -->`stats` 是 MCP 工具调用计数(MCP-http 进程 best-effort 写 redis:总计 `mcp:calls:total` + 按工具 `mcp:calls:tool:{name}`;API 只读透出),redis 不可用 → `{total:0, by_tool:{}}`(不报错)。
- `GET /api/mcp/token` → `{token: string|null}`。前端默认遮掩 token、点击「显示/复制」时才取(明文经此端点;LAN :8080 无鉴权,注意)。

### 工具(7,只读)
- **`list_knowledge_bases()`** → `[{domain, collection_count, job_count, concept_count, subscription_count, last_active_at}]`
  —— agent 探索起点。
- **`search(query, domain?=null, limit?=10)`** → `[{title, snippet, job_id, domain, kind}]`
  —— 全文检索(FTS5 trigram,中文子串;**查询≥3 字符**才命中);`snippet` 内 `<mark>` 包裹命中;`domain` 限定某库;
  `kind`=note_type。先 search 再 get_note。
- **`get_note(job_id)`** → `{job_id, title, domain, collection_id, content_type, status, note_file, markdown}`
  —— 取最新版智能笔记完整 Markdown;`markdown=null` 表示该内容智能笔记未生成。job 不存在→错误。
- **`list_collections(domain?=null)`** → `[{id, name, domain, job_count, [source_type, source_id, last_synced_at, last_sync_status]}]`
  —— 集合(内容分组/订阅来源)清单;`domain` 可选限定;订阅集合才带 source 字段。
- **`get_glossary(domain, status?=null)`** → `[{term, definition, status, is_topic, occurrence_count}]`
  —— 某库概念/术语表;`status` 可选(accepted/review)。单条详情用 get_term。
- **`get_term(domain, term)`** → `{domain, term, definition, status, is_topic, occurrences, related} | null`
  —— 单条术语详情(定义+出处+相关);未命中 null。
- **`concept_timeline(domain, granularity?=month)`** → `{domain, granularity, ...buckets}`
  —— 概念按源内容发布时间分桶计数;`granularity`=day|week|month。
- **`concept_graph(domain)`** → `{nodes:[{id,term,definition,status,is_topic,occurrence_count}], edges:[{source,target,weight}], stats:{node_count,edge_count,isolated_count}}`
  —— 概念共现网络:边=两概念引用同一 job,权重=共享 job 数,叠加手动 related;孤立概念仍作节点。等价于 REST `GET /api/domains/{domain}/concept-graph`。

### 迭代约定(新增工具)
service 函数(单一来源)→ `@mcp.tool()` 薄包(写好面向 LLM 的 docstring)→ pytest 集成(进 CI)→ 本节同提交更新(`contract:`)→
Inspector 眼检 → 版本 +1。工具少而精;签名**只增可选参数**保向后兼容(它是 agent 的公开契约)。
