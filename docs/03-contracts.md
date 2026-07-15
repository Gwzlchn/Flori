# 03 · 接口契约

> API 端点、WebSocket 事件、Redis 消息、文件 Schema、错误码。实现时以此为准。

## 1. REST API

Base URL: `/api`

### 1.0 来源目录

#### GET /api/sources — 内容与订阅来源目录

需通过与其他业务 API 相同的 Bearer Token 鉴权。响应由 `configs/sources.yaml` 派生，供前端和
集成方获取当前可投递内容类型、直接投递来源与订阅来源；检测正则、集合 ID 规则等内部字段不返回。

```json
{
  "content_types": [
    {"type": "video", "label": "视频", "pipeline": "video", "upload_extensions": [".mp4", ".mkv"]}
  ],
  "job_sources": [
    {"type": "youtube", "label": "YouTube", "content_types": ["video"], "creatable": true},
    {"type": "other", "label": "未支持来源", "content_types": [], "creatable": false}
  ],
  "subscription_sources": [
    {"type": "book_toc", "label": "在线书目录", "group": "book", "icon": "book-open",
     "id_label": "目录页 URL", "placeholder": "https://book.example.com/index.html",
     "hint": "解析目录页,按目录顺序入库各章节。", "home_url_template": "{source_id}"}
  ]
}
```

`content_types[].type` 与 `POST /api/jobs.content_type` 的 OpenAPI enum 一致；
`subscription_sources[].type` 与 `POST /api/collections.source_type` 的 OpenAPI enum 一致。新增来源必须
同时满足 registry 完整性、真实 pipeline 或已加载 source adapter，不能只扩枚举而没有执行链。

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

`content_type` 可显式指定，也可由 API 根据 URL 自动推断（arxiv→paper、**非 arxiv 直链 `.pdf`
（OSDI/usenix 等）→paper**、音频后缀→audio、其余 http(s) 网页→article、B 站 / YouTube→video）。
直链 PDF 经 `detect_source` 归为 `pdf` 源，下载步存 `input/source.pdf` 走论文流水线。未知 scheme、
未知裸标识、来源与显式类型错配或没有 pipeline 的类型在写文件、写 DB、入队前返回 `422`；公开 API
不接受 `file://`。`file://` 只允许已注册的 `local_dir` 订阅在内部以 `actor=subscription` 创建
`source=local_file` 的 job，不能借公开投递读取中心主机文件。

可选字段 `smart_note`（bool，默认 `null`）：是否生成 AI 智能笔记及随附评审。`null`＝按内容类型默认（`article` 默认 `false` 走轻链路；video/paper/audio 默认 `true`）。无论开关如何，**概念提取 + 一句话摘要始终生成**（article v2 的 `05_concepts` 步，进概念库/图谱）。`smart_note=false` 时跳过 `04_smart_article`/`06_review`。该开关存入 `job.meta.flags`，由调度器在 `rules.if_flag` 求值时决定相应步骤是否运行。

#### POST /api/jobs/upload — 文件上传创建

`multipart/form-data`：`file`（必填）+ `domain`（默认 `general`）+ `style_tags`（JSON 字符串，默认 `[]`）。
按 `configs/sources.yaml` 的扩展名识别类型：`.pdf`→paper，
`.mp4/.mkv/.webm/.flv/.mov`→video，`.mp3/.m4a/.wav/.aac/.flac`→audio，
`.html/.htm/.txt/.md`→article；未知扩展名在读取文件体和创建 job 前返回 `422`。上限 2GB。
上传进入 pipeline 前会规范化为既有输入契约：视频统一为 `input/source.mp4`，文章统一为
`input/source.html`，论文和音频保留各自受支持的标准扩展名。

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
    {"name": "10_evidence",   "label": "权威来源", "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "11_smart",      "label": "智能版",   "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "12_concepts",   "label": "概念 + 摘要", "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null},
    {"name": "12_review",     "label": "质量评审", "status": "waiting", "started_at": null, "finished_at": null, "duration_sec": null, "meta": {}, "error": null}
  ],
  "prompt_versions": {"11_smart": "2"}
}
```

字段说明（除 `JobResponse` 的公共字段外）：
- `url`：原始投递 URL（上传任务可为 `null`）。
- `updated_at`：最近一次状态/进度更新时间（ISO8601，可为 `null`）。
- `published_at`：源内容在 B 站等平台的发布时间（「上传于」），由 `01_download` 写入 `input/metadata.json`，读不到则 `null`。
- `collection_name`：由 `collection_id` join 出的集合名，无归属/集合已删则 `null`。
- 每个 `steps[]` 项：`label`（步骤中文名，取自 `pipelines.yaml`，缺省 `null`）、`started_at` / `finished_at`（ISO8601，未开始/未结束为 `null`）、`duration_sec`（未完成为 `null`）、`meta`（步骤产出统计）、`error`（失败时的错误信息，否则 `null`）。
- `prompt_versions`：`{step: version}`，本任务各 AI 步派发时用的 prompt 覆盖**版本号快照**。HTTP 响应中的 `version` 是十进制字符串，避免 SQLite 64 位整数进入 JavaScript 后丢失精度；内部 `job.json.prompt_overrides[step].version` 仍为整数。无覆盖/旧 job 纯字符串形态的步不出现，故常为 `{}`。前端 Job 详情据此与当前激活版本（`GET /api/prompts/{pipeline}/{step}`）比对，不一致时高亮提示「重跑该步」（白盒版本管理，见 §1.15）。重跑用 `POST /api/jobs/{id}/rerun {from_step}`（清该步及下游 `.done` 重跑）。
- `source_kind`：论文源类型（读 `intermediate/parsed.json.source_kind`，best-effort）：`"arxiv-html"`＝有干净 HTML 源（前端原文变体直接渲染 `output/original.md`）；`"pdf-only"`＝只有 PDF（原文＝内嵌 PDF，AI 步直喂 PDF）；非论文/旧 job 为 `null`。

#### GET /api/jobs/{id}/concepts — 该内容命中的概念（反查）

返回 `occurrences` 含本 job 的概念（按本 job 的 `domain` 过滤；LIKE 粗筛 + 精确过滤防子串误命中）。每行是完整 glossary 行（`GlossaryTermResponse` 全字段，含 `created_at` / `updated_at`，见 1.10）外加 `job_occurrences` = 本 job 命中的出现位置数组。未找到 job 返回 `404`。

```json
[
  {
    "domain": "deep-learning",
    "term": "注意力机制",
    "zh_name": "",
    "aliases": [],
    "definition": "...",
    "occurrences": [{"job_id": "j_xxx", "content_type": "video", "location": "scene-3"}],
    "related": [{"term": "Transformer", "rel": "part_of"}],
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
GET /api/jobs/j_xxx/steps/11_smart/log        → 尾部 256KB
GET /api/jobs/j_xxx/steps/11_smart/log?raw=1  → 完整
```

错误：`400` 非法 step（含 `/` / `..` / 空字节）、`404` 日志不存在。

#### GET /api/jobs/{id}/ai-logs — 完整 AI 审计日志（prompt 白盒化）

返回该 job 各 AI 步的**完整 AI 调用审计**(只读)。读 `output/ai_logs/{step}.jsonl`(每个 AI 步每次 LLM 调用一条,经 StorageBackend 同时适用本地和 MinIO),按 `job_id` 归成一条 trace。`?step={step}` 只返回该步。每条记录含:路由(provider/api/model/tier_used + 逐 tier `attempts` 尝试链)、延迟(ttft_ms/api_ms/总时长)、prompt(`rendered` 是实际发出的 system+user,`template` 是模板名/来源/版本/路径/字节数/SHA-256,`values` 是注入值)、输出、`transcript`(agentic 全轨迹 sidecar 引用,见下)、用量(in/out/cache)、成本、原始返回 `raw`、溯源(Flori 版本/git_commit、input_hashes、env worker)、`ok/error`(失败调用也整条记)。模板元数据和 `rendered` 中的正文来自同一份解析快照。落盘点为 `shared.step_ai.AIInvocation.call`,审计写入 best-effort 且不破坏主流程。

> **AI worker 接入方式 / provider 审计对齐**:Claude CLI、Codex CLI、Kimi API key 都归同一种 `ai` worker,差异只在接入方式/凭证方式。审计必须保留具体 `provider`、`requested_model`、`effective_model`(可解析时)、`worker_id`、`worker_tags`、`ai_access_method` 与 `credential_kind`。`pool=ai` 只表示资源池,不能推断接入方式。CLI provider 有 transcript sidecar;API-key provider 无 CLI transcript 时写 `{"file": null, "reason": "non_cli_provider"}`,但其它 prompt/response/usage/cost 字段必须与 CLI provider 同形。

> **`transcript` 字段(agentic 全轨迹白盒)**:claude-cli 的多轮 agentic 调用(取证 WebSearch/Bash、视觉逐图 Read)顶层 json 只回最终汇总,中间轮工具轨迹在 CLI 自写的会话 transcript 里;codex-cli 的非交互调用以 `codex exec --json` JSONL event stream 作为 trace。审计层按 provider 返回的 `transcript_path` 回收,拷为 job 产物 sidecar `output/ai_logs/{step}.turns.{call_index}.jsonl`(随产物入 storage、随删 job 级联删),记录内留引用:`{"file": "output/ai_logs/….turns.N.jsonl", "turns": 行数, "bytes": 大小, "source": 原路径}`;不可得(非 CLI provider / HOME 未挂 / 会话无档)为 `{"file": null, "reason": …}`。失败调用经尝试链 `attempts[].transcript_path` 同样回收。

> **`phase` 字段(外杀留痕)**:每次调用【发起前】先落一条 `phase:"pending"` 记录(输入侧全量:渲染后 prompt/system、模板来源、input_hashes、ts_start;`ok:null`)并即刻 flush;调用完成后**原位替换**为 `phase:"final"` 完整记录。步被外杀(如步超时 SIGKILL)时磁盘仅存 pending 条 → 该调用的输入仍可审计,ts_start 可与 worker 家目录 transcript 按时间窗对上;失败/超时路径 worker 会 best-effort 把 `output/ai_logs/*` 推回中心存储,故 API 可见。workdir 复用重试时续写同一 jsonl(历史记录保留、`call_index` 续增),上次执行的 pending 不会被覆盖。

```
GET /api/jobs/j_xxx/ai-logs                  → {job_id, steps:[{step, calls:[...]}]}
GET /api/jobs/j_xxx/ai-logs?step=11_smart    → 只该步
```

错误：`400` 非法 job_id/step。无日志时返回 `{job_id, steps: []}`（非 404）。

#### POST /api/jobs/{id}/retry — 重试失败任务

从失败步骤开始重跑（仅对 status=failed 的 Job）。Response `200`：
```json
{"job_id": "j_20260516_abc123", "status": "processing", "retry_from": "11_smart"}
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
  -d '{"from_step": "11_smart"}'
```

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "from_step": "11_smart"}
```

典型场景：对视频 AI 笔记质量不满意 → rerun from 11_smart → Claude 重新生成。

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

用指定 AI provider 重新生成智能笔记并重评(生成新版本,旧版本保留)。智能步与评审步按 pipeline 动态解析,不写死 video 步名:

| pipeline | 智能步 | 评审步 |
|----------|--------|--------|
| `video` | `11_smart` | `12_review` |
| `paper` | `05_smart_paper` | `06_review` |
| `article` | `04_smart_article` | `06_review` |
| `audio` | `04_smart_podcast` | `05_review` |

```bash
curl -X POST http://localhost:8000/api/jobs/j_xxx/rerun-smart \
  -d '{"provider": "anthropic"}'
```

请求体:`{"provider": "anthropic"}`(必填)。provider 必须存在于当前运行配置,且智能步、评审步各自都有一个在线、未暂停、属于目标 pool 并满足全部硬标签的 Worker。两步可以由不同 Worker 满足。除 provider tag 外,还会检查步骤静态标签和按当前产物算出的条件能力;例如 pdf-only paper 没有非空 `output/translated.md` 或 `output/original.md` 时,`05_smart_paper` 额外要求 `read`。

写入 `job.json`（关键字段）：
```json
{"ai_overrides": {"11_smart": "anthropic", "12_review": "anthropic"}}
```

> `job.json` 另有 `prompt_overrides`(白盒编辑,见 §1.15):`{step: {content, version}}`,并兼容存量 `{step: content}` 字符串形态。job 创建时由 API 按 `(pipeline, domain)` 解析当时激活的 DB 覆盖,固化进 job 后再下发给 pure Worker。覆盖键始终使用 pipeline 运行时步骤名,正文模板名可由 pipeline 显式映射。Worker 与 Prompt API 通过同一解析契约读取覆盖和默认模板,与 `ai_overrides`(provider 覆盖)正交。

Response `200`:
```json
{"job_id": "j_20260516_abc123", "status": "processing", "provider": "anthropic", "from_step": "11_smart", "review_step": "12_review"}
```

错误语义:job 不存在返回 `404`;pipeline 没有智能/评审角色、provider 未配置、没有匹配 Worker 或 provider 不支持所需能力返回 `400`;`job.json` 不是合法 JSON、顶层不是对象或 `ai_overrides` 不是对象返回 `409`;请求体缺字段或类型错误返回 `422`。所有失败都发生在写 `job.json` 和发布 rerun 命令之前,保持零副作用。成功时才把两个角色的 provider 覆盖写进 `job.json`,并从智能步发布 rerun。

### 1.2 笔记与产物

通用端点（所有内容类型）：
```
GET /api/jobs/{id}/notes/smart          → text/markdown (AI 笔记;?file= 取指定版本)
GET /api/jobs/{id}/note-versions        → application/json (智能笔记各版本+总分,见下)
GET /api/jobs/{id}/review               → application/json (评审安全投影;?file= 取版本化评审)
GET /api/jobs/{id}/evidence             → application/json (取证安全投影)
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
> `{"versions": [{"provider","model","version","file","review_file","overall","review_state"}...]}`(按 version 倒序)。`review_state` 为 `reliable` / `unreliable` / `legacy_unverified`;只有 `reliable` 评审返回 `overall`,其余为 `null`。
>
> `/artifacts` 返回:`{"groups":[{"step","label","total_bytes","files":[{"path","kind","size"}...]}...],"total_bytes":<int>}`。
> `size`/`total_bytes` 为字节(本地盘 rglob 自带 st_size、MinIO list_objects 自带 obj.size,不逐文件 stat);
> `total_bytes`(顶层)=全部已分组产物体积合计,供前端透出每步/整 job 产物体积。

### 1.3 系统状态

#### GET /api/status

返回全量系统状态：`version` + 有序 `components`（系统健康总览页 §2）+ 统一 `health` readiness 模型 + live 四段（`workers`/`pools`/`jobs`/`disk`）+ `throughput_1h`。逐组件探测各自 try+超时（redis 2s；SQLite/MinIO 默认 3s）：单项异常 → 该组件 `status="unknown"`（采集失败≠挂）或 `down`（连接拒绝/超时），其余照常返回，**绝不整体 500**。SQLite 与 MinIO 写探针按 `configs/pools.yaml::readiness` 使用短 TTL 单飞缓存；缓存过期后不回旧绿灯，超时或异常 fail-closed。MinIO SDK 同时限制 connect/read 且关闭重试，避免黑洞网络留下持续累积的后台线程。`components` 是**有序数组**（顺序固定 `api→scheduler→redis→minio`，前端按 `name` 作 key），便于追加新组件不破坏类型。`components.detail` 与 `health` 均不暴露密钥/连接串。

```json
{
  "version": "<FLORI_VERSION>",
  "//version": "FLORI_VERSION = 语义版本(pyproject [project].version)+构建短sha,如 <semver>+<build-sha>;构建sha 未注入则仅语义版本。顶层 version = components[kind=api].version 的冗余。前端拆「+」显示 v<语义> + 构建号",
  "components": [
    {"name": "api", "kind": "api", "status": "up", "version": "<FLORI_VERSION>",
     "last_heartbeat": "2026-06-24T07:21:55+00:00", "uptime_sec": 273840, "detail": null,
     "extra": {"rss_mb": 128.4}},
    {"name": "scheduler", "kind": "scheduler", "status": "up", "version": "<FLORI_VERSION>",
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
  "//minio.version": "MinIO 服务端版本经带短 connect/read timeout 的 MinioAdmin.info() 取 servers[].version,首次成功后缓存；取不到或本地盘 mode=local 则为 null。版本失败不覆盖 put/delete canary 的可写结论",
  "//minio.extra.objects/size_bytes": "MinIO bucket 对象数 + 总字节。MinIO 无聚合 API → 须全量 list 求和(贵),故 api 侧后台缓存(每 600s 刷,RemoteStorage.capacity 经 to_thread),build_full_status 只读缓存;无缓存(刚起/采集失败)则不带这俩字段(前端显 —)。绝不在 /api/status 同步扫",
  "//components.status": "up|degraded|down|unknown（组件专用四态，非 worker 的 online-*/stale）。scheduler 据 component:scheduler 心跳新鲜度（复用 worker_status 的 30/900 窗口）+ loop_lag>5s 叠 degraded；redis 据 ping/内存；minio 据短生命周期对象 put/delete canary；mode=local 时 minio=unknown（本地盘）",
  "health": {
    "version": "<FLORI_VERSION>",
    "status": "degraded",
    "ready": true,
    "degraded": true,
    "checks": {
      "redis": {"status": "ok", "required": true, "detail": null, "recovery": "..."},
      "db": {"status": "ok", "required": true, "detail": null, "recovery": null, "journal_mode": "wal"},
      "disk": {"status": "ok", "required": true, "detail": null, "recovery": "...", "free_gb": 600.0, "free_pct": 97.5, "min_free_gb": 5.0, "min_free_pct": 5.0},
      "data_writable": {"status": "ok", "required": true, "detail": null, "recovery": null},
      "scheduler": {"status": "ok", "required": true, "detail": null, "recovery": "..."},
      "storage": {"status": "ok", "required": true, "detail": null, "recovery": "...", "mode": "remote"},
      "workers": {"status": "ok", "required": true, "detail": null, "recovery": "...", "total": 2, "online": 2, "paused": 0},
      "pool:io": {"status": "ok", "required": true, "detail": null, "recovery": "...", "online": 1, "paused": 0},
      "pool:cpu": {"status": "ok", "required": true, "detail": null, "recovery": "...", "online": 1},
      "pool:ai": {"status": "ok", "required": true, "detail": null, "recovery": "...", "online": 2},
      "pool:gpu": {"status": "degraded", "required": false, "detail": "可选资源池 gpu 当前离线", "recovery": "...", "online": 0}
    },
    "reasons": [{"code": "pool:gpu", "severity": "degraded", "message": "可选资源池 gpu 当前离线", "recovery": "需要该能力时启动声明 --pools gpu 的 Worker"}]
  },
  "//health": "status=ready|degraded|not_ready。required=true 且 status=error 才阻断接单；required 组件 degraded 仍可接单但整体降级。Worker 列表复用 /api/workers 的 SQLite+Redis 合并口径并按 pools 多池计数；全部 paused 不算在线。阈值、TTL/timeout 与 required/optional pools 来自 configs/pools.yaml::readiness",
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
  "//cost": "claude-cli CLI 成本为「等价 API 成本」（非真实账单），前端按 provider==claude-cli 标「(等价)」"
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

#### GET /api/health/live

免鉴权 liveness。只证明 API 进程和事件循环可响应；Redis、DB、存储、调度器或 Worker 故障不改变结果，避免编排器把依赖故障误当进程故障反复重启。

```json
{"status": "alive", "alive": true, "version": "<FLORI_VERSION>"}
```

#### GET /api/health/ready

免鉴权 readiness。复用 `/api/status.health` 的同一检查模型；可安全接单时 HTTP `200`，任一 required 检查为 `error` 时 HTTP `503`。检查覆盖 Redis、SQLite WAL 真实写事务（临时建表+写入后回滚，不留 schema）、数据盘真实可写、磁盘绝对值与百分比阈值、中心存储 put/delete canary、scheduler 心跳，以及从 SQLite+Redis 合并的必要/可选 Worker pool。组件 `degraded` 只让整体降级，不自动转成 `503`；必要池无在线 Worker 或全部暂停仍阻断。写探针采用短 TTL、singleflight、超时和 fail-closed，响应不含路径、连接串、对象 key 或凭证。

#### GET /api/health

兼容旧监控的免鉴权入口，响应体与 `/api/health/ready` 相同，但始终返回 HTTP `200`；调用方必须读取 `ready`。新编排和发布门使用 `/api/health/live` 与 `/api/health/ready`，不要再把单一健康端点同时当 liveness 和 readiness。

```json
{
  "version": "<FLORI_VERSION>",
  "status": "not_ready",
  "ready": false,
  "degraded": false,
  "checks": {
    "redis": {"status": "ok", "required": true, "detail": null, "recovery": "..."},
    "pool:ai": {"status": "error", "required": true, "detail": "必要资源池 ai 没有在线 Worker", "recovery": "启动至少一个声明 --pools ai 的 Worker", "online": 0}
  },
  "reasons": [{"code": "pool:ai", "severity": "blocking", "message": "必要资源池 ai 没有在线 Worker", "recovery": "启动至少一个声明 --pools ai 的 Worker"}]
}
```

### 1.4 Worker 管理

`GET /api/workers` 返回的 `status` 是后端按心跳新鲜度+是否在跑+管理员叠加位读时派生的公共态（`online-idle` / `online-busy` / `offline` / `stale` / `paused`，见 §3.4）；下文示例中的 `idle`/`busy` 是历史字段示意，实际响应为派生态。

#### POST /api/workers/registration-token — 铸接入 token

铸/重置短期 bootstrap token（`flw-*`，重铸即作废旧的，可过期）。无 cached worker token 的远程 worker 首次接入时持此 token 经 `POST /api/runner/register` 换取长期 per-worker token（`flwt-*`，gateway 接入流程见 §1.7）。已缓存 `flwt-*` 的 worker 重启后走 `POST /api/runner/resume`，不得用 registration token 自动复活。

Response `200`:
```json
{"token": "flw-xxxxxxxx", "expires_in_sec": 86400}
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
    "step": "11_smart", "status": "done",
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
          "step": "11_smart", "pool": "ai", "started_at": "2026-05-17T12:00:00Z",
          "worker_id": "ai-a1b2c3d4", "worker_type": "ai", "worker_hostname": "office-pc"
        }
      ],
      "queued": [
        {
          "state": "queued", "job_id": "j_yyy", "title": "RLHF 综述",
          "content_type": "paper", "domain": "ai", "pipeline": "paper",
          "step": "05_smart_paper", "pool": "ai", "priority": 100,
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
      "current_step": "11_smart",
      "tasks_completed": 142,
      "tasks_failed": 3,
      "total_duration_sec": 28800.0,
      "traffic": {"pull": 8589934592, "push": 1073741824},
      "first_seen": "2026-05-10T08:00:00+08:00",
      "started_at": "2026-05-17T09:00:00+08:00",
      "last_heartbeat": "2026-05-17T12:30:15+08:00",
      "admin_note": "内网机器，有 Claude Max 账号"
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
    {"job_id": "j_xxx", "step": "11_smart", "status": "done", "duration_sec": 45.2, "finished_at": "..."},
    {"job_id": "j_yyy", "step": "12_review", "status": "done", "duration_sec": 12.1, "finished_at": "..."},
    {"job_id": "j_zzz", "step": "11_smart", "status": "failed", "error": "timeout", "finished_at": "..."}
  ]
}
```

#### PUT /api/workers/{id} — 更新 Worker 配置

```bash
# 暂停 Worker（停止认领新任务，跑完当前步后等待；服务端写独立 admin_status 叠加位，
# 与运行时 busy/idle 解耦，busy worker 暂停后跑完当前步不会丢暂停态；
# 离线或重建期间不会被 stale worker GC 删除）
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"status": "paused"}'

# 恢复 Worker（status 传 active / idle / resume 均视为恢复）
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"status": "active"}'

# 添加运维备注
curl -X PUT http://localhost:8000/api/workers/ai-a1b2c3d4 \
  -d '{"admin_note": "内网机器，有 Claude Max 账号"}'
```

#### DELETE /api/workers/{id} — 移除 Worker 记录

移除已下线 Worker 的历史记录。删除状态判定与列表/详情一致:Redis 新鲜实时态覆盖 DB 旧心跳;`online-idle` / `online-busy` / `paused` 不带 `force=true` 时返回 `409`。暂停态 worker 即使离线或 Redis 注册过期,stale worker GC 也不会删除 DB 行,以保留管理员暂停意图。删除成功会删除 DB worker 行、吊销该 worker 名下 per-worker token、清 Redis worker 实时态;后续 runner 请求必须 `401/403`。Response `204`。

### 1.5 平台认证

B站扫码登录走 `/api/bili/*`（cookie 入库 DB）；YouTube cookies 与平台 cookie 文件状态走 `/api/auth/*`：

```
POST /api/bili/login/start             → 生成扫码二维码（passport QR）
GET  /api/bili/login/poll?qrcode_key=  → 轮询扫码结果
GET  /api/bili/status                  → 当前 B站登录态
POST /api/bili/logout                  → 清除已入库 B站 cookie
GET  /api/auth/status                  → 平台凭证配置状态(DB credentials 有无)
POST /api/auth/youtube/cookies         → 上传 YouTube cookies(入库 credentials 表并镜像 redis 分发)
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

外部 worker 的标准接入路径只有 Gateway HTTPS:注册/恢复在线、长轮询认领步骤、上报结果、经网关代理读写产物（worker 不直连 Redis/MinIO，见 [ADR-0009](adr/0009-worker-gateway-outbound-https.md)）。旧 RedisTransport/MinIO 直连是内部实现细节,不作为用户接入 contract。`register` 用 bootstrap registration token（`POST /api/workers/registration-token` 铸发）门禁,其余端点用 per-worker token（`Authorization: Bearer`）。

```
POST   /api/runner/register                                → 首次 bootstrap,换发 per-worker token
POST   /api/runner/resume                                  → cached per-worker token 恢复在线态,不签发新 token
POST   /api/runner/heartbeat                               → 刷新存活（暂停态由 claim_step 据 admin_status 兜底，不经心跳回发）；可带 concurrency、load、applied_cfg_rev 与 running=[{job_id,step,exec_id}]；只有完整且仍有效的 running 四元组才续租并刷新步骤进度；响应带 desired_config/cfg_rev（运行配置热下发通道，§1.7.2）
POST   /api/runner/offline                                 → 主动下线
POST   /api/runner/jobs/request                            → 长轮询认领一步（认到即返回 enrich 后的 claim）
POST   /api/runner/jobs/{id}/steps/{step}/complete         → 上报完成
POST   /api/runner/jobs/{id}/steps/{step}/fail             → 上报失败
POST   /api/runner/jobs/{id}/steps/{step}/release          → 释放认领（不计成败）
POST   /api/runner/jobs/{id}/steps/{step}/progress         → 上报运行中进度（转发到 events:{id}）
POST   /api/runner/jobs/{id}/steps/{step}/alive            → 步进度心跳（on_tick 每 10s，仅子进程存活时；供远程 job 卡死检测）
POST   /api/runner/ai-tasks/{task_id}/executing            → AI claim 进入执行态
POST   /api/runner/ai-tasks/{task_id}/renew                → AI claim 续租
POST   /api/runner/ai-tasks/{task_id}/result               → AI claim 回写结果;Ask 在写入端按服务端锚点复算 citation_validation
POST   /api/runner/ai-tasks/{task_id}/log                  → AI claim 回写白盒审计;task/exec/step 由租约覆盖
POST   /api/runner/ai-tasks/{task_id}/finish               → AI claim 进入 succeeded/failed 并由服务端发布终态事件
POST   /api/runner/ai-tasks/{task_id}/release              → AI claim 释放池槽
POST   /api/runner/usage                                   → 记录一次 AI 用量（exec_id 去重）。body 含 worker_id（api 以鉴权 token 认定为准）、input/output_tokens、cache_creation/cache_read_input_tokens（命中率=read/(input+read+creation)）、cost_usd、duration_sec、num_turns、cached；claude-cli 经 `claude -p --output-format json` 取真实 usage+total_cost_usd。api 侧据 LiteLLM 价表（每天拉 `model_prices_and_context_window.json` 存 MinIO `_pricing/litellm.json`,缓存感知 per-token 单价）对**非 cli** provider 填权威 cost_usd（命中时覆盖上报值；空表/未命中回退上报值）；claude-cli 用其 CLI total_cost_usd（CLI 等价 API 成本,不覆盖）
GET    /api/runner/jobs/{id}/artifacts                     → 产物清单（GatewayStorage.pull 据此）
GET    /api/runner/jobs/{id}/artifacts/{rel}              → 流式取单个产物；支持单段 Range，计实际发送的 traffic:pull
PUT    /api/runner/jobs/{id}/artifacts/{rel}              → 流式回传单个产物；校验大小/SHA-256 后原子发布，计成功写入的 traffic:push
GET    /api/runner/credentials/{key}                       → 领取下载凭证（§1.7.1）
```

`POST /api/runner/register` Response `200`:
```json
{"worker_id": "ai-a1b2c3d4", "worker_token": "flwt-...", "desired_config": {"concurrency": 4}, "cfg_rev": 3}
```
注册成功只返回一次 `worker_token`;同一 worker 新 bootstrap 前服务端先吊销旧 token,保证单 worker 单 active token。同 ID Redis 新鲜在线时返回 `409 duplicate worker`,调用方应使用 cached worker token 走 `resume`。

`POST /api/runner/resume` Request body 与 `register` 相同但 `worker_id` 必填;Authorization 使用该 worker 的 `flwt-*`。body.worker_id 与 token 归属不一致返回 `403`。Response `200`:
```json
{"worker_id": "ai-a1b2c3d4", "desired_config": {"concurrency": 4}, "cfg_rev": 3}
```
`resume` 刷新 Redis worker hash 全量信息并 upsert DB worker 行,保留 DB `admin_status` / `desired_config` / `cfg_rev`;不返回新 token。
（`desired_config`/`cfg_rev` 为中心期望运行配置,§1.7.2;未配置时 `null`/`0`。）

`jobs/request` 成功认领普通 pipeline 步时，服务端同时创建 180 秒 task-scoped lease。租约权威身份为 `(worker_id, job_id, step, exec_id)`，并绑定 claim 的 `pool`；Gateway worker 必须从 claim 原样携带四元组，不能推断或复用其他槽位的执行。

独立 AI task 使用等价的专用 lease，权威身份为 `(worker_id, task_id, step, exec_id)`，其中 `exec_id=claim_id holder`，并同时绑定服务端 claim 的 `attempt/revision/batch_id/state/lease_until`。Gateway 用同一组三个 `X-Flori-Lease-*` header，`Lease-Job` 在此承载 `task_id`。跨 task/exec、过期或已终态 lease 的 result/log/usage/finish 一律拒绝。任务入队时 API/Scheduler 另写 `ai:anchor:{task_id}` 服务端原始 payload 锚点；Worker 返回的 `source_manifest` 和 `citation_validation` 都不是信任源，Ask 在写入和读取结果时均按锚点复算，锚点缺失或损坏时 fail-closed。

不可信或远端 Worker 必须使用 Gateway HTTPS，不能获得 Redis/DB 凭证。`RedisTransport` 直连模式仅适用于受信任的同机 Worker；它与中心共享 Redis 权限，因而不声称能抵御该进程主动改写 `ai:anchor:*`。这一区分是恶意 Worker 安全边界，不是可选部署建议。

- `progress`、`alive`、`usage`、artifact list/get/put 与 `credentials` 必须携带 `X-Flori-Lease-Job`、`X-Flori-Lease-Step`、`X-Flori-Lease-Exec`。头字段缺失、路径与租约不一致、租约过期、rerun 已替换 exec 或 worker 越权均返回 `403`。
- `complete`、`fail`、`release` 继续在 body 携带 `exec_id` 与 `pool`，服务端以 worker token 和当前租约四元组原子核验。同一终态结果和正常 release 可幂等重放；done/failed 冲突、伪造 pool 或陈旧 exec 返回 `403`。
- `complete` / `fail` 通过租约后不直接写 SQLite，而是把绑定 `exec_id + lifecycle_generation` 的终态原子写入 Redis Stream；Scheduler 成功落 DB、推进 DAG 和执行声明式副作用后才 ACK。首次接受返回 `{"ok":true,"duplicate":false}`，同结果重放返回 `{"ok":true,"duplicate":true}`，租约核验后又因旧代/旧执行被终态门拒绝则返回 `{"ok":false,"stale":true}`。
- heartbeat 不再替缺失的 `exec_id` 猜当前执行。长上传/下载每 30 秒重新核验并续租；租约失效时中断流，暂存内容不得成为可见制品。
- `credentials/{key}` 额外要求当前 `step=01_download`；其他有效步骤也返回 `403`。

artifact GET 响应带 `Accept-Ranges: bytes` 与 `Content-Length`；单段 `Range: bytes=<start>-<end>` 成功返回 `206` 和 `Content-Range`，非法或不可满足范围返回 `416`。PUT 可带 `Content-Length` 与 `X-Content-SHA256`；实际大小或摘要不匹配返回 `422`，无效摘要返回 `400`，超过 10 GiB 返回 `413`。LocalStorage 通过同目录暂存加 `os.replace` 发布，MinIO 通过隐藏暂存对象加服务端 copy 发布；失败、取消或断线只清理暂存，不覆盖旧制品。成功响应为 `{"ok":true,"size":<bytes>,"sha256":"<hex>"}`。

<!-- contract: runner 鉴权自卫 + 诊断头 + 可观测(worker↔gateway 健壮性) -->
**鉴权与自卫**：per-worker token 端点缺失/未命中/已吊销 → `401`;token/body worker_id 不一致 → `403`;**同一 token 连续 401 达阈值（5）→ `429` + `Retry-After: 60`**（挡失效 token 死刷 `jobs/request`）。token 命中即清该 hash 计数。worker 侧拿 `401/403/429` 必须 fail fast,视为 token 已 revoke、被限流或配置错误;不得自动改用 registration token 重新 bootstrap。
**诊断头（可选，不可信，仅诊断）**：worker 每个 runner 请求带 `X-Worker-Id / X-Worker-Type / X-Worker-Host / X-Worker-Version`（自报身份）；即使 401 服务端也据此记 `claimed_*`（知道是谁、什么版本在刷——`version` 是排障关键，一眼认出旧版没更新的 worker）。
**可观测**：worker 连接/认证事件（`worker_registered / worker_auth_rejected / worker_token_throttled`）进 `events:system` → `GET /api/events`（/system 事件页）+ structlog→Dozzle；runner 高频轮询端点（`heartbeat` / `jobs/request`）的 uvicorn access 记录从主日志流摘掉（declutter Dozzle，不影响业务/审计/其余 access 日志）。

#### 1.7.1 下载凭证中心分发（`GET /api/runner/credentials/{key}`）

<!-- contract: 凭证中心分发(废除 /data 下 cookie 文件共享;本地/远程 worker 统一) -->
凭证单一持久源 = DB `credentials` 表（B站扫码 `bili_cookies` JSON、YouTube 上传 `youtube_cookies` Netscape 文本，Fernet 加密），写入时镜像 redis `cred:{dispatch_key}`（scheduler 启动时从 DB 重灌，防 redis 卷重建丢镜像）。worker 认领 `01_download` 步时按 job.source 领取所需凭证并注入步骤子进程 env（`BILI_SESSDATA` / `FLORI_YT_COOKIES`，随子进程消亡不落盘）；claim 响应因此附 `source` 字段。本地 worker 走 redis 镜像、远程 worker 走本端点，同一 transport 抽象。

- dispatch key 白名单：`bili_sessdata`（从 bili_cookies 提取的 SESSDATA 值）、`youtube_cookies`（Netscape 原文）；其余 `404`。
- 鉴权：per-worker token（同 §1.7 其余端点）。
- Response `200`：`{"key": "...", "value": "...|null"}`——`value=null` 表示中心未配置（worker 匿名降级，B站降 480P/无字幕，不视为错误）。
- **审计**：每次领取记 structlog 事件 `credential_issued(worker_id, key, present)`——文件共享时代无凭证使用审计，此为新增。
- 凭证失效恢复：管理页重新扫码/上传 → 入库 + 镜像即刻更新 → 全部 worker 下一个下载任务自动用新凭证（无需逐机操作）。

#### 1.7.2 Worker 运行配置中心化（`PUT /api/workers/{id}/config`）

<!-- contract: worker 运行配置中心化(启动参数最小化;心跳热下发;Watchtower 原参重建即最新) -->
worker 启动参数收敛为永不变化的最小集：`GATEWAY_URL` + `WORKER_REGISTRATION_TOKEN` + `WORKER_NAME`（`--pools` 仅作首次注册的初始能力）。中心运行配置当前只支持 `concurrency`;`pools` / `tags` / `reject_tags` 是 worker 自报能力,不接受中心配置接口修改。

- 写入：`PUT /api/workers/{id}/config`，body 只允许 `{"concurrency": 1..64}`;空 body 返回 `400`,携带 `pools` / `tags` / `reject_tags` 等未知字段返回 `422`。写 DB `workers.desired_config`（JSON）并单调递增 `cfg_rev`。Response：`{"cfg_rev": N, "desired_config": {"concurrency": N}}`。
- 下发：`register` / `resume` 响应与每拍 `heartbeat` 响应携带 `desired_config` + `cfg_rev`;worker 比对本地已生效 rev,更高才应用（幂等）。
- 热应用：并发扩=即刻补认领槽,缩=超编槽跑完当前任务自然退位,**不打断在跑步骤、不重启容器**。应用后经心跳 `concurrency` + `applied_cfg_rev` 回报,分别写 worker 实时态/DB 基本信息与 redis worker hash `cfg_applied_rev`。
- 可见性：`GET /api/workers` 响应含 `desired_config` / `cfg_rev` / `applied_cfg_rev`（前端据此显示「配置待同步/已生效」徽标）；下发记 `worker_config_updated` 系统事件。
- `desired_config` 为空 = 无中心覆盖,尊重 worker 自报。DB `upsert_worker` 为 ON CONFLICT UPDATE（非 REPLACE）,register/resume 不冲掉中心配置。

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
| `local_dir` | 本地目录（挂进 api+worker 容器的监听目录） | 容器内绝对路径（约定 `/data/inbox`） | `local` | 按 registry 扩展名：pdf→paper、mp4/mkv/webm/flv/mov→video、mp3/m4a/wav/aac/flac→audio、md/txt/html/htm→article（其它扩展名忽略） |
| `book_toc` | Jupyter Book / Sphinx 等在线书目录 | 目录页 URL | `book` | article；章 job 强制 `smart_note=true`，按目录顺序串行投递 |

- 同一来源种类可通过 registry 的 `group` 收敛为同一**来源标签**：三种 B 站来源都收敛到 `bilibili`。
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

错误：`400` 手动集合 name 为空 / 订阅集合 domain 为 general / 该来源已订阅；`422`
`source_type` 与 `source_id` 未成对提供、source_type 不在 registry enum，或 registry 声明的适配器
未加载。校验均发生在写集合前。

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
    "related": [{"term": "Attention", "rel": "part_of"}, {"term": "Self-Attention", "rel": "related"}],
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
  "zh_name": "",
  "aliases": [],
  "definition": "…",
  "occurrences": [
    {"job_id": "j_xxx", "content_type": "video", "location": "…", "title": "内容标题"}
  ],
  "related": [{"term": "Attention", "rel": "part_of"}],
  "status": "accepted",
  "is_topic": true,
  "definition_locked": false,
  "current_definition_version_id": "cdv_<64hex>",
  "lock_revision": 3,
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

#### GET /api/domains/{domain}/concept-graph — 概念图谱（真边 + 共现降噪）

把该领域的概念组织成力导向图，供工作台「图谱」视图。**节点 = 概念**（`rejected` 不进图）；**边**两类（09 工单 P2）：

- **`related` 真边**：`kind` = 关系类型（`prerequisite` 先修 / `is_a` 是一种 / `part_of` 组成 / `related` 相关），方向保留 `source→target`（`prerequisite` 有语义方向，前端画箭头）；`weight` 取该对共现数（无共现为 1）。
- **共现边**：`kind: "cooccur"`，两概念的 `occurrences` 引用同一 `job_id` 即候选，`weight` = 共享 `job_id` 数；**仅保留 `weight ≥ min_cooccur`**（query 参数，默认 `2`，范围 `1..10`，剪掉单篇 N 概念全连的噪声），同一对已有真边则不重复出共现边。

指向不存在概念的 `related` 项忽略（未入库不建边，待其被采集后自动连上），自连忽略。孤立概念仍作为节点保留（度 0）。全程按 `domain` 作用域。空领域返回空 `nodes`/`edges` 与零计数（不 404）。逻辑在 `api/services/kb.py:concept_graph`（单一来源，REST 与 MCP 工具共用）。

```
GET /api/domains/finance/concept-graph?min_cooccur=2
```

Response `200`：

```json
{
  "nodes": [
    {"id": "通胀", "term": "通胀", "zh_name": "", "definition": "物价普涨。", "status": "accepted", "is_topic": true, "occurrence_count": 3},
    {"id": "利率", "term": "利率", "zh_name": "", "definition": "资金的价格。", "status": "accepted", "is_topic": false, "occurrence_count": 2}
  ],
  "edges": [
    {"source": "利率", "target": "通胀", "weight": 2, "kind": "prerequisite"}
  ],
  "stats": {"node_count": 2, "edge_count": 1, "typed_edge_count": 1, "isolated_count": 0}
}
```

- `nodes[].id` = `term`（领域内唯一）。`definition` 为短定义（首句或截断，便于节点 tooltip/侧栏）。`occurrence_count` = 该概念出现处数（节点大小 ∝ 此值）。`status` ∈ `suggested`/`accepted`，`is_topic` 标主题。
- `edges` 去重（每对一条），共现边 `(source, target)` 按字典序规范化方向、真边保留语义方向，按 `weight` 降序、再按术语名排序。
- `stats.typed_edge_count` = 真边数；`isolated_count` = 度为 0 的节点数。

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
  "watched_concepts": [
    {"term": "Kelly criterion", "zh_name": "凯利准则", "recent": 1, "total": 4}
  ],
  "window": {"days": 7, "since": "2026-06-19T...", "until": "2026-06-26T..."}
}
```

- `rising_concepts`：最近窗口出现次数 > 前窗口的概念，按 `delta` 降序。
- `new_concepts`：最早一次出现落在最近窗口内的概念（按 `first_seen` 降序）。
- `recent_jobs`：时间落在最近窗口内的全部 current 内容（按时间降序）。查询不设固定条数上限；SQL 只做带安全余量的候选粗筛，最终由 aware-UTC 精确比较保证 `[since, until)` 微秒边界。
- `top_recent_concepts`：最近窗口出现最多的概念（最多 10 个）。
- `watched_concepts`：关注（`watched=1`）的概念全量列出，近窗有新出现（`recent`）的排前；驱动雷达页「我关注的概念」区与工作台提示条。
- 统计口径：`rejected` 概念不参与任何板块。

#### POST /api/domains/{domain}/digest — 本周摘要（按需调 LLM）

先算同款雷达，再从窗口内 current/done job 的 canonical note chunks 冻结 `digest_sources` manifest，最后用 `digest` builder 拼 prompt → **投递独立 AI task（`queue:ai`，§3.1）给 ai-worker** 异步生成中文周报。**API 进程不调 claude**（用量 `ai_usage`/白盒审计 `ai_task_logs` 在 worker 侧记）。与 GET radar 分离：页面先秒开雷达，用户点「生成本周摘要」再触发本端点。`window_days` 同 radar（`1..90`，越界 `422`）。

`digest_sources` 清单绑定 `task_id/domain/window`，每条绑定 canonical `source_id/job_id/note_type/chunk_id`、excerpt/chunk hash 和 source fingerprint，清单再签 `manifest_sha256`。硬上限为 16 个 source、每 job 2 个 source、单 excerpt 1200 字符、excerpt 总计 12000 字符，最终 system+user prompt 按 UTF-8 不得超过 32 KiB。候选顺序按 job 公平分配，超界只在 manifest 记 `selection_truncated=true`，不会改变雷达窗口统计或静默伪装成全量证据。title/section/excerpt 全部按不可信 JSON 数据渲染，生成温度固定为 0。

```
POST /api/domains/finance/digest?window_days=7
```

Response `202`（投递成功；`markdown` 经 `GET /api/ai-tasks/{task_id}/result` 轮询取，digest 读 `markdown` 别名）：

```json
{
  "task_id": "at_3f9c…",
  "window": {"days": 7, "since": "2026-06-19T...", "until": "2026-06-26T..."},
  "source_count": 8,
  "manifest_sha256": "<64 lowercase hex>"
}
```

每个实质事实行必须是某个冻结 excerpt 内有词元、数量/币种/单位和否定极性边界的连续原文，并在行尾使用精确标签 `[来源:ce_<64 lowercase hex>]`。Worker 写入和 API 读取都从服务端 original payload 重算 `citation_validation`，不信任 Worker 自报的 manifest 或 reliable 标记。未引用、引用未知/畸形/错位、孤立标签、一行多事实、不受原文支持、manifest 篡改和旧任务缺 manifest 都 fail-closed，`citation_validation.reliable=false`。

无窗口活动或无 canonical evidence 时不投任务，返回 `task_id:null` 和 `citation_validation`（分别为 `not_applicable/reliable=true` 或 `unverified/reliable=false`）。投递失败（Redis 不可用）仍返回 `202`，但降级正文必带 `unverified/reliable=false` 和 `digest_enqueue_failed`，不 5xx。

#### GET /api/domains/{domain}/digest/latest — 最新自动周报

**每周自动周报（09 工单 P3）**：scheduler periodic 循环每天检查，当天（UTC）是配置星期（env `RADAR_DIGEST_CRON_DOW`，`0`=周一，默认 `0`）则给每个近 7 天有动静（新内容/飙升/新概念任一非空）的 domain 投一条 digest AI task；当日防重复靠 redis `radar:digest:auto:{domain}:{day}` SET NX 锁（TTL 3 天）。`airesult` 只有 ~600s TTL 而自动周报没人守屏，scheduler 收割结果搬进 `radar:digest:latest:{domain}`（无 TTL），并 `push_event`（`radar_digest_queued` / `radar_digest_ready`）进事件页。本端点读该键：

```json
{"task_id": "at_…", "queued_at": "2026-07-06T00:00:30+00:00", "markdown": "# 本周…", "generated_at": "2026-07-06T00:02:10+00:00", "source_manifest": {...}, "citation_validation": {"status": "valid", "reliable": true, ...}}
```

从未生成过 → `{"task_id": null}`；生成失败/超时/旧数据缺验证/任一 citation 门未通过 → 带 `error` 和 `citation_validation.reliable=false`，服务端不返回 `markdown`。只有 `reliable=true` 的自动周报才保存和公开正文；前端同样只展示这一状态，domain 切换会使旧请求和旧 poll 的迟到结果失效。

### 1.10 术语库 / 概念图

> 按 `domain` 维度维护的术语表。**一条 = 一个概念实体**（09 工单 P1）：AI 采集经 `shared.concepts.resolve` 归一——大小写/全半角/括号注音变体、中英说法（经 `zh_name`/`aliases`）都挂到同一实体，`occurrences` 跨内容累积。术语有两种来源：AI 抽取步骤自动采集（落 `status=suggested` 候选）、用户手动新增（直接 `accepted`）。`accepted` 的术语会同步进对应 domain 的 `Profile.terminology`，供后续 AI 步骤复用。`is_topic` 标记主题概念，用于概念图。主键为 `(domain, term)`；主名规则：英文术语为 `term`、中文进 `zh_name`，纯中文概念 `term`=中文。

所有端点走 Basic/Token 鉴权。`domain` / `term` 路径段不得含 `..`、`/`、`\x00`，否则 `400`。

**`GlossaryTermResponse` 字段**：

```json
{
  "domain": "deep-learning",
  "term": "Attention Mechanism",
  "zh_name": "注意力机制",
  "aliases": ["attention mechanism", "注意力機制"],
  "definition": "一种让模型动态聚焦输入关键部分的机制",
  "occurrences": [
    {"job_id": "j_20260516_abc123", "content_type": "video", "location": "scene-12", "title": "视频标题"}
  ],
  "related": [{"term": "Transformer", "rel": "part_of"}, {"term": "自注意力", "rel": "related"}],
  "status": "accepted",
  "is_topic": true,
  "definition_locked": false,
  "created_at": "2026-05-16T20:00:00+08:00",
  "updated_at": "2026-05-16T20:00:00+08:00"
}
```

- `status`：`suggested`（AI 采集的候选，待审）/ `accepted`（已采纳）/ `rejected`（已驳回）。**生命周期（09 工单 P3）**：采集时 `suggested` 实体的 `occurrences` 覆盖 ≥2 个不同 job → **自动晋升 `accepted`**（跨内容复现 = 真概念的强信号）；`rejected` 行保留，采集链 resolve 命中即整条跳过（同名/变体不再被重复建议），且**各消费面默认排除**（`GET /api/glossary` 未指定 status 时、正文 term-link、图谱、雷达、`term_map` 翻译注入、topic/timeline/top-terms/jobs-concepts）——只在显式 `status=rejected` 时可见。**status 语义定死**：正文 term-link 高亮 = `accepted`；翻译 `term_map` 注入 = 全量非 rejected（译名一致性收益大于误注入风险）；雷达/图谱默认 = 非 rejected（图谱前端默认再收窄到 accepted+高频，开关放宽）。
- `watched`：概念订阅标记（bool，单用户）。watched 概念在雷达返回 `watched_concepts` 区（近窗有新出现的置顶），工作台顶部出提示条。
- `zh_name`：标准中文译名（实体双语名，可为空串）。`aliases`：归并进本实体的变体名（采集归一与合并留痕，检索命中）。正文 term-link 对 `term`/`zh_name`/`aliases` **大小写不敏感**命中（纯 ASCII 变体按词边界），统一链到实体主名。
- `related`：类型化关系边 `[{term, rel}]`，`rel` ∈ `prerequisite`/`is_a`/`part_of`/`related`（09 工单 P2）。写入端（PUT/POST body）元素可为字符串（视为 `rel="related"`）或对象，落库/读出统一归一为对象（存量字符串读出时同样归一）。来源：`05_concepts` v3 抽取（两端经 resolve 归一，目标未入库不建边）+ 手动维护 + `scripts/backfill_concept_edges.py` 存量补边。
- `occurrences`：兼容来源摘要，元素 `{job_id, content_type, location}`，由抽取步骤累积（同一 job 去重）。**详情端点**额外 enrich `title`，最多返回 100 项并给 `occurrence_total/occurrence_limit`；精确多证据关系存于正规化 `concept_occurrences`，通过详情的 `attestation` 投影。
- `is_topic`：是否为主题概念。`definition_locked`：定义是否已钉住。`current_definition_version_id` 与 `lock_revision` 是定义写入、lock/unlock、重综合共用的 CAS 快照；定义版本只追加不原地修改。
- `created_at` / `updated_at`：ISO8601 字符串，缺失时为 `null`。
- **响应分层**：列表、domain 简版详情及普通写端点返回 `GlossaryTermResponse`；`GET /api/glossary/{d}/{t}` 返回其超集 `ConceptTermDetailResponse`，MCP `get_term` 与该详情共用同一 async projection。

#### GET /api/glossary — 列术语

可按 `domain` / `status` 过滤（均可选）；`q` 检索 `term`/`zh_name`/`aliases` 子串（大小写不敏感，中英说法都能搜到同一实体）。按 `term` 升序返回。

```
GET /api/glossary?domain=deep-learning&status=suggested&q=注意力
```

Response `200`：`GlossaryTermResponse` 数组（同上结构）。

#### POST /api/glossary/{domain}/{term}/merge — 合并实体

把 `{term}`（src）并入 body `target`（dst）实体：兼容 occurrences、别名、status、topic、lock 与 related 按原规则合并；精确 occurrence 移到 dst，definition history 保持不可变，并为 dst 追加 `concept_merge` identity-transfer version 后切 current。旧 CAS 令牌通过 `lock_revision + 1` 失效；然后删 src 行。存量批量清洗走 `scripts/merge_glossary_entities.py`（同一 db 方法）。

```json
{"target": "Attention Mechanism"}
```

Response `200`：合并后的 `GlossaryTermResponse`。错误：`400` src==dst 或 `target` 为空；`404` 任一行不存在。

#### POST /api/glossary/{domain}/{term}/reject — 驳回概念

`status` → `rejected`。行保留（不再被自动建议 + 各消费面默认排除，语义见上）。误驳可用 `accept` 恢复。`404` 不存在。Response `200`：更新后的 `GlossaryTermResponse`。

#### POST /api/glossary/{domain}/{term}/watch — 关注/取关概念

请求体 `{"watched": true|false}`。`404` 不存在。Response `200`：更新后的 `GlossaryTermResponse`。

#### POST /api/glossary/batch — 批量采纳/驳回

待审列表「全部采纳」/多选操作。请求体：

```json
{"action": "accept", "items": [{"domain": "deep-learning", "term": "注意力机制"}]}
```

`action` ∈ `accept`/`reject`（否则 `400`）。`accept` 逐条同步进 `Profile.terminology`（与单条 accept 一致）；不存在/字段缺失的条目计入 `skipped`，不整批失败。Response `200`：`{"updated": n, "skipped": m}`。

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

未命中 `404`。Response `200`：`ConceptTermDetailResponse`，在 `GlossaryTermResponse` 基础上增加：

- `current_definition` 与 `definition_history`：不可变版本完整投影；历史最多 100 条，并给 `definition_history_total/definition_history_limit`。
- `attestation`：`level`、distinct evidence/job/source fingerprint/content type 计数、`source_set_fingerprint`，以及 `included/excluded`。只有当前 valid 且绑定可靠评审原文快照的 included evidence 才带 locator/link；excluded 始终不可跳转。
- 每个 definition version 记录 evidence IDs、生成 strategy、provider/model、prompt/input hash、前驱、actor 与创建时间。manual edit 的 evidence 集为空，不伪装成自动佐证定义。

#### PUT /api/glossary/{domain}/{term} — 修改术语

仅改 `definition` / `related`；不动 `status` / `occurrences` / `is_topic`。body 中字段为 `null`（或省略）则保留原值。变更 definition 时必须同时提交 `expected_current_version_id` 与严格非负整数 `expected_lock_revision`；current、revision 或锁状态变化返回 `409`，未命中 `404`。只改 related 不创建假 definition version。

```bash
curl -X PUT "http://localhost:8000/api/glossary/deep-learning/注意力机制" \
  -H "Content-Type: application/json" \
  -d '{"definition":"更新后的定义","related":["Transformer","自注意力"],"expected_current_version_id":"cdv_<64hex>","expected_lock_revision":3}'
```

Response `200`：更新后的 `GlossaryTermResponse`。

#### POST /api/glossary/{domain}/{term}/lock | unlock — 定义锁 CAS

两端点请求体相同：`{"expected_current_version_id":"cdv_<64hex>","expected_lock_revision":3}`。成功返回 `{current_definition_version_id,lock_revision,locked,changed}`；幂等重复不增加 revision，真实 lock 状态切换恰好 `+1`。不存在返回 `404`，过期 current/revision 返回 `409`。locked 时人工定义改写与后台重综合都不得越过。

#### POST /api/glossary/{domain}/{term}/resynthesize — 受控重综合

请求体同 CAS。只在未锁定、至少两个可靠独立 job/source fingerprint 且 source set 变化时调用 provider；输入是 resolver 重验通过的有界 evidence excerpt。返回 `{created,reason,current?,version?,attestation?}`，`reason` 可为 `locked/no_quorum/source_set_unchanged/input_too_large`。provider/配置/解析失败返回 `502`且不切 current；AI 返回后会再次投影 attestation，证据、source set 或 input hash 改变返回 `409`。Scheduler 在精确 occurrence 对账后以同一服务 best-effort 自动触发，同概念在途去重，失败不阻塞 job 终态。

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

基于 SQLite FTS5（`trigram` tokenizer，对中文做子串匹配）。3 字及以上查询走参数化 FTS5 phrase；**恰好 2 个 CJK 字符**走参数化 `instr` 兼容检索；单字、单字母、纯标点或空查询直接返回空结果（`total: 0`）。所有路径都使用绑定参数，不拼接用户输入。

video、paper、article、audio 四类真实 pipeline 都通过 `pipelines.yaml::jobs.*.on_complete` 进入该全文索引；Ask 使用同一次原子写入生成的 `note_chunks_fts5`，MCP `search` 与本端点共用 `notes_fts5`。重复完成事件或恢复对账不会累加 FTS 行、证据块或概念 occurrence。

```bash
curl "http://localhost:8000/api/search?q=注意力机制&domain=deep-learning&limit=20"
```

查询参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `q` | `""` | 检索词；2 字 CJK 走参数化子串，3+ 字符走 trigram |
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
      "collection_id": "c_xxx",
      "canonical_evidence": [{
        "evidence_id": "ce_<64 lowercase hex>",
        "status": "valid",
        "reason": null,
        "job_id": "j_20260516_abc123",
        "note_type": "smart",
        "chunk_id": "j_20260516_abc123:smart:0",
        "section": "训练过程",
        "evidence_fingerprint": "<64 lowercase hex>",
        "source_fingerprint": "<64 lowercase hex>",
        "locator": {"kind": "media", "start_ms": 12500, "end_ms": 16000},
        "link": {"kind": "media", "href": "/api/jobs/.../media?...#t=12.5", "label": "00:12"},
        "validated_at": "2026-07-14T14:00:00Z"
      }]
    }
  ]
}
```

`items[].canonical_evidence` 是下节 canonical evidence 安全投影的稳定 ID 顺序数组。同一 chunk
可以绑定多个来源片段；Search/MCP 的 note 级结果最多投影当前稳定顺序的前 20 项，完整笔记证据走分页
job endpoint。无 provenance 的存量笔记返回空数组，不得用 `job_id` 或历史文件路径在消费端猜链接。

#### Canonical evidence 安全投影与解析

Search、Ask、MCP 和内容详情共用同一个 evidence identity 与三态投影。概念出处只有在后续
`(concept,job,evidence)` 精确关系落库后才允许接入，禁止按 job 附加整篇证据：

内部 sidecar 契约为：

- `intermediate/source_segments.json` v2 顶层保持
  `schema_version/job_id/pipeline/source_artifacts/segments`；每个 segment 在 v1 坐标字段之外必须有
  `support_text:string|null` 和 `support_artifact:object|null`，两者必须同时为空或同时非空。
  `support_text` UTF-8 最大 4096 bytes；`support_artifact` 只允许
  `kind/path/sha256/selector`，`path` 为 job 内规范相对路径，`sha256` 为 64 位小写十六进制。
  五种绑定为：`html` 使用与 source artifact 相同的 path/SHA 及 `start/end`；
  `audio_segments` 使用 `intermediate/segments.json` 及 `index`；`video_subtitle` 使用
  `input/*.srt` 及 `index`；`video_ocr` 使用 `intermediate/ocr.json` 及
  `entry_index/box_index`；`pdf_pages` 使用 `intermediate/pdf_page_support.json` 及 `page`。
  Scheduler builder 与读时 resolver 都必须重读 SHA 对应的实际产物，按 selector 复算
  support text 并与 manifest 精确比较。HTML、音视频转写和 OCR box 写真实文本，
  PDF-only 由 `PdfParseStep` 使用 Poppler 一次提取全文，再按实测页严格分配原文并绑定对应页码和 PDF SHA；空白页、
  提取失败、非 UTF-8 或单页超限时该页写 `null`，不得截断或错位绑定。
- `output/provenance/{note_type}.json` v2 的每个 mapping 在 v1 锚点字段之外必须有
  `verification_policy=direct_locator_v1|exact_quote_v1`。direct 用于确定性 producer；smart 只能用
  exact quote，且每个 mapping 必须恰好绑定一个 source segment。整行 claim 只做 NFC 和有限
  空白归一后，必须逐字包含于该 segment 的 support text；不做 NFKC 兼容归一，也不对
  所有来源全局做 HTML entity 解码。同行多 ref，包括同源和跨模态组合，均不产生映射。
- reader 严格兼容 v1 direct original/transcript/mechanical；v1 smart 非空 mapping、额外字段、未知 policy、
  改写/纯数字 claim、PDF 空白页或提取失败页、跨语言 translated/smart claim 均 fail-closed。
  producer 和 Scheduler 分别复算，客户端不能提交或拼装 canonical mapping。

```json
{
  "evidence_id": "ce_<64 lowercase hex>",
  "status": "valid|stale|missing",
  "reason": null,
  "job_id": "j_xxx",
  "note_type": "smart",
  "chunk_id": "j_xxx:smart:0",
  "section": "引言",
  "evidence_fingerprint": "<64 lowercase hex>",
  "source_fingerprint": "<64 lowercase hex>",
  "locator": {"kind": "pdf", "page": 3, "bbox": null},
  "link": {"kind": "pdf", "href": "/api/jobs/.../media?...#page=3", "label": "跳到 PDF 页"},
  "validated_at": "2026-07-14T14:00:00Z"
}
```

- `valid` 才允许 `reason=null` 且返回安全 `locator/link`。`link` 只能由服务端 resolver 派生，
  消费端不从 locator、job_id 或任何 path 拼接 URL。
- `stale/missing` 必须有非空 `reason`，且 `locator=null/link=null`。跨 job 绑定、原始 path 越界、
  source/note/chunk hash 篡改、text anchor 多解或无解均 fail-closed，不产生链接。
- locator union 字段固定：`media={kind,start_ms,end_ms}`；
  `pdf={kind,page,bbox:[x0,y0,x1,y1]|null}`；
  `text={kind,exact,prefix,suffix,dom_path}`；
  `image={kind,bbox:[x0,y0,x1,y1],start_ms,end_ms,page}`。可选定位值用 `null`，
  image 投影永不暴露 `asset_path/asset_sha256`。
- `link={kind,href,label}` 的 `kind` 与 locator 一致。前端只在 `status=valid` 且 link 存在时渲染可点定位；
  其余情况明确显示「证据已过期」或「证据缺失」。

Resolver：

```
GET  /api/evidence/{evidence_id}/resolve
POST /api/evidence/resolve  {"evidence_ids":["ce_...", "ce_..."]}
GET  /api/evidence/jobs/{job_id}?note_type=smart&offset=0&limit=100
```

GET 中非法 id 返回 `422`；合法但未知 id 返回 `404 canonical_evidence_not_found`；已存在但失效的
id 返回 `200 stale|missing`。batch 最多 100 个且不允许重复，响应 `{"items":[...]}` 严格保持请求顺序；
未知 id 在原位返回 `missing` 占位，不让批量消费者丢失位置关系。
job 端点按当前 note chunk 快照分页返回投影 `{"total":N,"items":[...]}`，供内容详情展示；`limit`
范围为 1..100，job 不存在返回 `404`，存在但当前 note type 没有 canonical evidence 返回
`{"total":0,"items":[]}`。它只读取当前 chunk 绑定的 ID，因此不会
带回重建索引前的历史 ID；当前 ID 即使暂时 stale/missing 也保留在结果中，以便来源恢复后重新变为 valid。

#### POST /api/ask — 跨源综合问答（Cross-Source Synthesis Q&A）

自然语言提问 → 跨语料检索相关笔记 → LLM 综合出**带引用**的答案，内联标注 `[来源N]`、并附「共识 / 分歧」段。需 `verify_token`。

**检索缓解**：服务端先把问句**拆词**（去停用词/标点，CJK 连续串做 2–4 字滑窗，ascii 词保留）并叠加**术语表里出现在问句中的术语**，得到一组（≤6）派生查询。检索只查 `note_chunks_fts5` 证据块（由 `index_job_notes` 从笔记正文切分生成），对所有派生查询做确定性 RRF；排序以分数和稳定身份打破并列，并保证一个 `job_id` 最多进入一个 chunk，避免同一笔记挤占来源位。无 chunk 命中即无来源。综合走 `claude-cli` 接入方式。

请求体：

| 字段 | 默认 | 说明 |
|------|------|------|
| `question` | （必填，1–4000 字） | 自然语言问题 |
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
    {
      "job_id": "j_bp",
      "title": "反向传播详解",
      "domain": "deep-learning",
      "content_type": "video",
      "evidence": {
        "chunk_id": "j_bp:smart:0",
        "note_type": "smart",
        "section": "训练过程",
        "snippet": "…反向传播…",
        "chunk_index": 0,
        "char_start": 0,
        "char_end": 640,
        "timestamp_sec": null,
        "page": null,
        "frame_path": null,
        "image_path": null
      },
      "canonical_evidence": [{
        "evidence_id": "ce_<64 lowercase hex>",
        "status": "valid",
        "reason": null,
        "job_id": "j_bp",
        "note_type": "smart",
        "chunk_id": "j_bp:smart:0",
        "section": "训练过程",
        "evidence_fingerprint": "<64 lowercase hex>",
        "source_fingerprint": "<64 lowercase hex>",
        "locator": {"kind": "media", "start_ms": 12500, "end_ms": 16000},
        "link": {"kind": "media", "href": "/api/jobs/.../media?...#t=12.5", "label": "00:12"},
        "validated_at": "2026-07-14T14:00:00Z"
      }]
    }
  ],
  "retrieved_count": 1
}
```
- `sources[].evidence` 保持既有 chunk 摘要契约，供 citation/source manifest 冻结和兼容旧消费者；
  `sources[].canonical_evidence` 是稳定 ID 顺序数组，无 provenance 时为空。同一 canonical evidence 在
  Search/Ask/MCP 返回相同 `evidence_id/status/evidence_fingerprint/source_fingerprint`。
- 投递前服务端把本次来源冻结为 `ask_sources` manifest：每项固定绑定 `index/job_id/note_type/chunk_id/artifact_sha256/body_sha256/body/source_fingerprint`，manifest 再绑定 `task_id/question/manifest_sha256`。`body` 最多 4000 字；`artifact_sha256` 是完整被索引 note artifact，`body_sha256` 是规范化 chunk body，均为 64 位小写 hex。该清单只随 AI task 的 `audit_context.ask_source_manifest`、结果与审计持久化，不信任模型自行回报的来源。
- 命中为 0 → `task_id:null`、`answer_markdown` 为固定提示文案、`sources:[]`，**不投 task**（短路）。
- 投递失败（redis 不可用）→ `task_id:null` + 降级文案 + 已检索 `sources`（不 5xx）。

用量（`ai_usage`，`step=synthesis`，`job_id=null`）与白盒审计（`ai_task_logs`）由 **ai-worker** 记账（P1-2），API 不再记。

#### GET /api/ai-tasks/{task_id}/result — 独立 AI task 结果（轮询）

`/ask`、`/digest` 提交的 AI task 的结果。读 `airesult:{task_id}`（§3.1，worker 写，TTL≈600s）：

| status | 响应 |
|---|---|
| `pending` | 未就绪（worker 没跑完/已过期）：`{"status":"pending","task_id":...}` |
| `error` | 失败：`{"status":"error","task_id":...,"error":"...","source_manifest":...|null,"citation_validation":...|null}` |
| `done` | 完成：`{"status":"done","task_id":...,"content":"...","answer_markdown":"...","markdown":"...","provider":...,"model":...,"cost_usd":...,"source_manifest":...|null,"citation_validation":...|null}` |

`answer_markdown`/`markdown` 均 = `content`（ask 读前者、digest 读后者）。前端也可 `WS /api/ws/jobs/{task_id}` 收 `ai_task_done`（§3.5）后再取本端点。

Ask 的 `citation_validation` 由 Worker 先做本地检查，Gateway 写入结果和 API 读取结果时再以 URL 中的 `task_id` 重算，并与入队端冻结在 `ai:anchor:{task_id}` 的 `audit_context.ask_source_manifest` 精确比对，不信任远端 Worker 自报 `valid` 或替换整套来源。识别格式只允许 `[来源N]`。返回 `status=valid|unverified|invalid`、`checked/items/errors`，以及 `metrics.structural_precision/source_precision/claim_precision/coverage`。unknown index、跨 task manifest、原始 manifest 缺失、结果 manifest 缺失或替换、manifest/source/body hash 篡改、非逐字支撑 claim、畸形标签和零引用均 fail-closed，绝不标为 `valid`。

Digest task 的 `source_manifest` 与 `citation_validation` 同样只从 original payload 的 `audit_context.digest_source_manifest` 派生。识别格式只允许 `[来源:ce_<64 lowercase hex>]`；返回 `kind=digest_citations`、`status=valid|unverified|invalid`、`reliable`、`checked_claims/supported_claims/items/issues/manifest_sha256`。公开 `source_manifest.manifest_sha256` 必须与 validation 的 hash 一致，Worker 回传的替换 manifest/audit/validation 字段在写入端删除，读取端也不使用。

#### GET /api/ai-tasks/{task_id}/log — 独立 AI task 白盒审计

镜像 DAG 的 `GET /api/jobs/{id}/ai-logs`：读 `ai_task_logs`（§3.1），返回该 task 每次 claude 调用的完整审计（路由/尝试链/渲染 prompt/输出/raw/用量/`transcript` agentic 全轨迹），最近在前。

```json
{"task_id":"at_…","count":1,"calls":[{"task_id":"at_…","exec_id":"…","step":"synthesis","domain":"ml","provider":"claude-cli","model":"claude-opus-4-8[1m]","ok":true,"error":null,"created_at":"…","record":{"routing":{"attempts":[…]},"prompt":{"system":"…","messages":[…]},"output":"…","raw":{…},"transcript":{"jsonl":"…","turns":12,"truncated":false,"path":"…"},"usage":{…}}}]}
```

> `record.transcript`(agentic 全轨迹白盒):AI task 不挂 job、无 storage 产物区,CLI 会话 transcript **全文内嵌** `record_json`(`{"jsonl": 全文, "turns", "truncated", "path"}`;>5MB 截断并 `truncated:true`;不可得为 `{"jsonl": null, "reason": …}`)。Ask 审计另持久化 `record.audit_context.ask_source_manifest` 与 `record.citation_validation`，即使 Redis 结果 TTL 到期也能按本次来源复算。

### 1.12 学习闭环 / Flashcards / SRS（`/api/study/*`）

学习卡片是个人知识库的复习层。当前闭环支持手动卡片、证据型自动建议、批量审核、到期队列、四档评分、概念掌握度、幂等重试、revision CAS 和全量统计。所有端点走 Basic/Token 鉴权。

**StudyCard 字段**：

```json
{
  "card_id": "sc_...",
  "domain": "deep-learning",
  "job_id": "j_20260709_abc123",
  "concept_term": "反向传播",
  "card_type": "basic",
  "front": "反向传播解决什么问题?",
  "back": "高效计算梯度。",
  "explanation": "链式法则让多层网络可训练。",
  "evidence": [{"chunk_id": "j:smart:0", "snippet": "…"}],
  "status": "active",
  "source": "manual",
  "revision": 1,
  "created_at": "2026-07-09T00:00:00+00:00",
  "updated_at": "2026-07-09T00:00:00+00:00",
  "review": {
    "due_at": "2026-07-09T00:00:00+00:00",
    "interval_days": 0,
    "ease": 2.5,
    "repetitions": 0,
    "lapses": 0,
    "last_grade": null,
    "last_reviewed_at": null,
    "updated_at": "2026-07-09T00:00:00+00:00"
  }
}
```

- `card_type` ∈ `basic` / `cloze` / `qa` / `quiz_single` / `quiz_multi`。
- `status` ∈ `suggested` / `active` / `suspended` / `rejected`。通用状态机只允许 `active ↔ suspended` 和 `suggested → rejected`；同状态重试不写库。`suggested/rejected` 不能通过通用端点恢复为 `active`。
- `revision` 是 SQLite 64 位正整数且单调递增。评分和状态更改使用它执行 CAS。
- `evidence` 是最多 100 个 JSON object 的数组，可存 RAG chunk evidence 或手动来源片段。自动建议卡的强证据 schema 不属于本接口。
- `review` 为空表示未排入复习队列；`active` 新卡默认立即 due。
- 公开时间都返回 UTC ISO 8601；库内同时保存 epoch 微秒作为排序和到期判定真相。新写入拒绝无时区 datetime，所以 `Z/+08:00/-05:00` 表示同一时刻时语义相同。
- 评分 `grade` ∈ `again` / `hard` / `good` / `easy`。简化 SM-2: `again` 精确 600 秒后重来并增加 lapses；`good` 前两次分别为 1/3 天；`easy` 前两次分别为 3/6 天。ease 限制在 1.3–3.0，interval 不超过 36500 天，datetime 溢出时截断到可表示上界。

#### POST /api/study/suggestion-batches — 创建证据型建议批次

请求体:

```json
{
  "request_id": "study-suggest:018f...",
  "domain": "deep-learning",
  "job_ids": ["j_..."],
  "concept_terms": ["反向传播"],
  "max_cards": 10
}
```

`request_id` 是 1–128 字符的全局幂等 key；`job_ids` 和 `concept_terms` 各最多 100 项且不得重复；`max_cards` 为 1–50。服务端在单个 `BEGIN IMMEDIATE` 事务中固化当前 note chunks、已采纳概念、证据 locator、正文/引用 hash、AI 请求和 prompt 原始字节，之后才由 Scheduler 投递 AI task。没有可用证据、job 不属于该 domain、job 未完成或概念未采纳时 fail-closed。同一 `request_id` 的相同 canonical payload 返回既有批次，异 payload 返回 `409 study_suggestion_request_id_conflict`。

内部 `llm_request.prompt_snapshot` 固定为:

```json
{
  "name": "study_suggestions",
  "content_b64": "<原始 UTF-8 字节的 base64>",
  "bytes": 1234,
  "sha256": "sha256:<64 lowercase hex>",
  "source": "override|hot|image",
  "version": 7
}
```

prompt 路径不入快照。Scheduler 重启、Redis 丢失和显式 retry 都只读取该持久快照，不重新解析当前文件；`generator_fingerprint=sha256:<64 lowercase hex>` 绑定生成器 schema、parser 和 prompt hash。返回 `202 StudySuggestionBatch`，状态机为 `pending_enqueue → queued → ready|failed`，`revision` 和 `attempt` 均为 SQLite 64 位正整数。

#### GET /api/study/suggestion-batches/{batch_id} — 查询持久批次

返回 `StudySuggestionBatch`:

```json
{
  "batch_id": "ssb_...",
  "domain": "deep-learning",
  "status": "queued",
  "revision": 2,
  "attempt": 1,
  "task_id": "at_...",
  "provider": "claude-cli",
  "model": "<explicit-model>",
  "max_cards": 10,
  "error_code": null,
  "error_message": null,
  "deadline_at": "...",
  "evidence_count": 3,
  "suggestion_count": 0,
  "created_at": "...",
  "updated_at": "..."
}
```

批次状态以 SQLite 为真相，可跨 API/Scheduler 重启轮询。Scheduler 用 `(batch_id,task_id,attempt,revision)` 推进 CAS；多副本只能投递同一个 canonical task。Redis `airesult` 过期时，成功或失败结果可从 `ai_task_logs` 的持久审计恢复。进入 provider 后租约失效的任务标为 `failed/error_code=ai_task_ambiguous`，不得自动重试或消费迟到结果。

#### POST /api/study/suggestion-batches/{batch_id}/retry — 重试失败批次

请求体: `{"request_id":"study-retry:018f...","expected_revision":4}`。仅 `failed` 批次可重试；保留原输入、证据和 prompt 快照，增加 `attempt/revision` 并生成新 `task_id`。同 request payload 重放返回首次结果；旧 task 的 Redis 结果或审计不得推进新 attempt。返回 `202 StudySuggestionBatch`。

#### GET /api/study/suggestions — 建议列表

查询参数: `domain`、`batch_id`、`status=suggested|accepted|rejected` 可选；`limit` 1–200，`offset` 0–2147483647。返回 `{"total":n,"items":[StudySuggestion...]}`。每项包含 `suggestion_id/batch_id/ordinal/status/revision/domain/concept_term/knowledge_key/card_type/front/back/explanation/accepted_card_id/rejection_reason/evidence/created_at/updated_at`。`evidence[]` 携带已固化 quote、quote/body hash、locator 和当前有效性；接受前再次验证来源 job、domain、chunk 和正文 hash，证据漂移则拒绝提交。

#### POST /api/study/suggestions/operations — 批量审核建议

```json
{
  "request_id": "study-operate:018f...",
  "batch_id": "ssb_...",
  "items": [{
    "suggestion_id": "ss_...",
    "expected_revision": 1,
    "action": "edit|accept|reject",
    "patch": {"front": "...", "back": "...", "concept_term": "反向传播"},
    "reason": "duplicate"
  }]
}
```

一次最多 100 项，extra 字段拒绝。服务端在一个 `BEGIN IMMEDIATE` 事务中完成全局 request replay、批次/证据校验、逐建议 revision CAS、去重、卡片与 due 状态创建、建议终态和 append-only operation ledger；任一步失败则整批回滚。`accept` 产生一张 `source=suggestion:{suggestion_id}` 的 active 卡片并立即 due；`edit` 保持 suggested；`reject` 必须写入 reason。返回 `{"batch_id":...,"items":[...],"cards":[...]}`。

#### GET /api/study/mastery — 概念掌握度

查询参数 `domain` 可选。只聚合 active/suspended、绑定概念且至少有一次真实 review log 的卡片；每张卡取最新评分，`again/hard/good/easy` 分别映射 `0/50/80/100`，再按概念取平均。返回项含 `score`、`level=fragile|learning|mastered`、`reviewed_cards`、`reviews_total` 和 `last_reviewed_at`；没有真实评分的自动卡不进入结果。

建议接口的结构化业务错误使用 `404/409/422`，常见 `message.code` 包括 `study_suggestion_batch_not_found`、`study_suggestion_not_found`、`study_suggestion_request_id_conflict`、`study_suggestion_revision_stale`、`study_suggestion_evidence_unavailable`、`study_suggestion_duplicate`、`study_suggestion_terminal` 和 `study_suggestion_constraint_conflict`。

#### POST /api/study/cards — 创建卡片

请求体：

```json
{
  "domain": "deep-learning",
  "job_id": "j_...",
  "concept_term": "反向传播",
  "card_type": "basic",
  "front": "问题",
  "back": "答案",
  "explanation": "可省略",
  "evidence": [{"chunk_id": "j:smart:0"}],
  "status": "active",
  "source": "manual"
}
```

Response `201`: `StudyCard`。该公开端点只创建 `source=manual` 且 `status=active|suspended` 的卡片；`domain/front/back` 在 trim 后不能为空。

#### GET /api/study/cards — 卡片库

查询参数:

| 参数 | 默认 | 说明 |
|------|------|------|
| `domain` | — | 限定知识库 |
| `status` | — | 限定状态 |
| `q` | — | 在 front/back/explanation/concept_term 中做 LIKE 检索 |
| `limit` | 100 | 1–200 |
| `offset` | 0 | 0–2147483647 |

Response `200`: `{"total": n, "items": [StudyCard...]}`。

#### GET /api/study/due — 到期复习队列

查询参数: `domain` 可选,`limit` 默认 50、范围 1–200。仅返回 `status=active` 且 `due_at_epoch_us <= now_epoch_us` 的卡片，按 epoch 升序。恰好等于 now 属于到期，未来 1 微秒不属于到期。

Response `200`: `{"total": n, "items": [StudyCard...]}`。

#### GET /api/study/stats — 学习全量统计

查询参数: `domain` 可选。服务端用单次 CTE 直接聚合已提交的 cards/reviews/logs，不使用分页列表或物化计数器。

```json
{
  "total": 251,
  "statuses": {"suggested": 2, "active": 203, "suspended": 45, "rejected": 1},
  "due": 203,
  "reviewed_cards": 80,
  "reviews_total": 120,
  "grades": {"again": 20, "hard": 25, "good": 50, "easy": 25},
  "retained_reviews": 100,
  "retention_rate": 0.8333
}
```

`retained_reviews = hard + good + easy`，`retention_rate = retained_reviews / reviews_total`；无 review 时比率为 `0.0`。

#### POST /api/study/reviews — 提交复习评分

请求体:

```json
{
  "request_id": "study-review:018f...",
  "card_id": "sc_...",
  "expected_revision": 7,
  "grade": "good",
  "response_ms": 1200
}
```

`request_id` 是 1–128 字符的全局幂等 key，客户端在超时、断网等结果不明时必须复用原 key。`expected_revision` 是 1..2^63-1 的真整数，`bool`、0、负数和 2^63 都返回 `422`；`response_ms` 可省略，有值时是 0..2^63-1 的真整数。

处理顺序固定在一个 `BEGIN IMMEDIATE` 事务内: 全局 request replay → 卡片存在性 → active-only → revision CAS → 调度/review → immutable log → commit。同 key 且 canonical payload 相同时返回首次保存的完全相同 `StudyCard`，不再写库；同 key 异 payload、陈旧 revision 或非 active 卡片返回结构化 `409`。不存在返回结构化 `404`。

```json
{"error":"conflict","message":{"code":"study_revision_stale","message":"study card revision is stale"}}
```

`409 message.code` 可为 `study_request_id_conflict` / `study_revision_stale` / `study_revision_exhausted` / `study_card_not_active` / `study_status_transition_invalid`；`study_revision_exhausted` 表示卡片 revision 已到 SQLite 64 位上限，服务端拒绝继续写入而不产生部分提交。`404 message.code=study_card_not_found`。

#### POST /api/study/cards/{card_id}/status — 改卡片状态

请求体: `{"status":"suspended","expected_revision":7}`。Response `200`: 更新后的 `StudyCard`。同目标状态的模糊重试直接返回当前卡片而不递增 revision；其它请求执行状态机和 CAS。恢复为 `active` 时若缺复习状态，立即排入 due 队列。

#### DELETE /api/study/cards/{card_id} — 删除卡片

删除卡片及其复习状态/日志。Response `204`;不存在 `404`。

### 1.13 Profile 管理（`/api/profiles/*`）

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

### 1.14 AI Provider 列表

#### GET /api/providers — 列 AI provider 及可用性

供前端"选 provider 重跑"挑选;当前没有通用可用 Worker 的 provider 标灰(`available=false`)。本地 ollama(`local`)默认不展示。Response `200`:

```json
{
  "providers": [
    {"name": "anthropic", "type": "api", "available": true,  "label": "API"},
    {"name": "claude-cli", "type": "cli", "available": true,  "label": "CLI"},
    {"name": "codex-cli",  "type": "codex_cli", "available": true, "label": "CLI"},
    {"name": "kimi",      "type": "openai_compatible", "available": true, "label": "API"},
    {"name": "openai",    "type": "api", "available": false, "label": "API"}
  ]
}
```

- `name`：provider 键（`providers.yaml` 中的键）。
- `type`：取自 provider 配置的 `type`（如 `anthropic` / `openai` / `openai_compatible` / `cli` / `codex_cli`）。
- `available`:provider 存在于当前配置,且 Redis 在线快照中至少有一个未暂停、非 offline、属于 `ai` pool 且具备该 provider 硬标签的 Worker。
- `label`：`type == "cli"` 或 `type == "codex_cli"` 时为 `"CLI"`，否则 `"API"`（前端展示用）。

`available=true` 是通用 provider 可用性,不保证某个具体步骤的 `vision` / `read` 等额外能力。进入 `queue:ai` 后仍按完整 `require_tags` 硬门控;`rerun-smart` 还会对智能步与评审步分别重算静态标签和条件能力,因此 provider 在本端点可用仍可能被特定 rerun 拒绝。

`POST /api/jobs/{id}/rerun-smart` 的 `provider` 必须是本端点列出且 `available=true` 的 provider。

### 1.15 Prompt 白盒(`/api/prompts/*`)

每个 AI 步的默认 prompt 正文可见、可覆盖。覆盖存在 DB 表 `prompt_overrides`,主键为 `(scope, domain, pipeline, step)`,列为 `content/version/updated_at`;`scope='global'` 时 `domain=''`,`scope='domain'` 时 `domain` 是域名。job 创建时 API 按 domain 优先于 global 的规则生成 `{step: {content, version}}`,并固化到 `job.json.prompt_overrides`。Worker 无需访问 DB,同一 job 也不会因激活版本后续变化而漂移。存量 job 的纯字符串覆盖仍可读。

**版本管理（类 Grafana save，1.1.5/1.1.6）**：每个 `(scope, domain, pipeline, step)` 覆盖带【版本历史】，存 DB 表 `prompt_override_versions`（主键加 `version`，列 `content`/`note`/`created_at`）；主表 `prompt_overrides.version` 是【激活指针】，`content` 仍存激活版本内容供注入快读。保存有两种动作：**「覆盖当前版本」**（`mode=overwrite`，改激活版本行内容，版本号不变）/ **「另存为新版本」**（`mode=new`，`version=max+1` 并设为激活）；首次保存恒为 `v1`。job 创建时注入的版本号即【当时激活版本】的**快照**，供 Job 详情比对「本任务用 vX vs 当前 vY」（见 §1.1 `GET /api/jobs/{id}` 的 `prompt_versions`）。

Prompt version 的合法范围是 SQLite 有符号 64 位正整数 `1..2^63-1`。HTTP 路径、响应和 activate 请求的规范表示均为十进制字符串；activate 只为兼容旧客户端额外接受 JavaScript 安全整数 `1..2^53-1`。`0`、负数、浮点、布尔、大于上界的字符串整数，以及大于 `2^53-1` 的 JSON number 均在 API 层返回 `422`，不会进入 SQLite 绑定；合法范围内但不存在的版本仍返回 `404`。当历史最大版本已是 `2^63-1` 时，`mode=new` 返回 `409`，`overwrite` 当前版本仍可用。

**激活/停用（非破坏，1.1.10）**：「回内置默认」与删历史**解耦**。`POST .../activate {version|null}`：① `version="十进制字符串"` → 把某历史版本**设为当前激活**（re-activate，派发即用它）；② `version=null` → **停用覆盖回内置默认**（deactivate）——只删主表激活指针那一行，`prompt_override_versions` 历史**全部保留**（下拉里仍能看到 v1/v2…，可随时再激活），`resolve_prompt_overrides` 据此返回空 → 派发用内置默认。`version` 列 `NOT NULL DEFAULT 1`（不可空），故 deactivate 用「删激活行」表达，而非置 NULL；主表因此【可缺行而历史仍在】。`DELETE`（彻底删除，连同全部历史）保留为可选的真删除入口，**不再**充当「恢复默认」。

**所见即所改**:`configs/prompts/templates/*.md` 中的 15 份 tracked 文件是 prompt 正文唯一真源。API 展示和 Worker 执行共用同一解析契约；每次解析从一份原始字节同时导出 UTF-8 文本、SHA-256、来源、覆盖版本和文件路径。单次 API 响应复用已解析结果，单个 Worker 步骤实例也缓存解析结果供指纹、AI 审计和实际调用复用。API 与 Worker 是独立进程，不承诺跨请求共享内存快照；job override 由创建任务时固化的正文和版本保证执行可复现。

正文解析优先级固定为:

1. `job.json.prompt_overrides[<runtime step>]` 中的任务固化覆盖。
2. `/data/prompts/templates/<template>.md` 运行时热编辑文件。
3. `/app/configs/prompts/templates/<template>.md` 镜像内 tracked 文件。

只有 ENOENT 表示当前层可回退到下一层。高优先级来源存在但不可读、权限拒绝、非 UTF-8 或内容为空时直接 fail-closed;三层均缺失时返回结构化的输入失败。正文解析不再保留内联副本或 `prompts/<step>.md` 第三条兜底路径。

覆盖键使用 pipeline 运行时步骤名,模板名可由 `prompt_template` 映射。`11_smart` 有主模板 `11_smart.md` 和视觉变体 `11_smart.vision.md`;该步覆盖只替换主模板,不污染视觉 pass。`08_punctuate` 没有同名主模板,覆盖替换当次实际选中的 `.zh` 或 `.translate` 变体。video 概念步运行时身份为 `12_concepts`,正文通过 `prompt_template: 05_concepts` 复用 tracked 模板;它的 done/progress/AI log/prompt override 仍全部使用 `12_concepts`。

三个评审步 `05_review/06_review/12_review` 共用逐字相同的 tracked 骨架,占位符为 `{{intro}}/{{dimensions}}/{{score_example}}/{{ref_block}}`。运行期参数注入不改变骨架真源;`score_keys` 仍由评分维度配置决定,不从模板反向解析。覆盖删除 `{{ref_block}}` 时,完整参照块追加到末尾,避免被评内容丢失。

```
GET    /api/prompts                                      → 列各 pipeline 可编辑 AI 步 + 已有哪些覆盖
GET    /api/prompts/{pipeline}/{step}                    → 单步默认模板(只读,全变体)+ system 钩子 + 该 (scope,domain) 当前覆盖 + active_version + versions[]
GET    /api/prompts/{pipeline}/{step}/versions/{version} → 查看某历史版本的完整内容(?scope&domain)
PUT    /api/prompts/{pipeline}/{step}                    → 存该步 prompt 覆盖(mode=overwrite|new + note;content 纯空白=彻底删除清全部版本)→ active_version
POST   /api/prompts/{pipeline}/{step}/activate           → 切激活指针:{version:"十进制字符串"}=设该历史版本为激活;{version:null}=停用回内置默认(非破坏,留历史)→ active_version
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
    {"name": "11_smart", "content": "...(主)...", "bytes": 128, "sha256": "sha256:...", "source": "image", "version": null},
    {"name": "11_smart.vision", "content": "...(变体)...", "bytes": 256, "sha256": "sha256:...", "source": "image", "version": null}
  ],
  "default_system": null,
  "override": {"scope": "global", "domain": "", "content": "你是...", "version": "2", "updated_at": "..."},
  "active_version": "2",
  "versions": [
    {"version": "1", "note": "首版", "created_at": "..."},
    {"version": "2", "note": "加了配图要求", "created_at": "..."}
  ]
}
```
- `default_template`:向后兼容字段,值为主模板内容;无同名主模板时取第一个变体。
- `default_templates`:该步当前可见的主模板和变体。`content/bytes/sha256/source/version` 来自同一份原始字节快照;`source` 为 `hot/image`,`version` 仅在解析任务固化覆盖时有值。
- `default_system`:向后兼容字段,当前 tracked 正文契约不把它作为缺失模板的回退层。
- `/data` 热编辑模板缺失时从镜像内 `/app/configs/prompts/templates` 读取;只有 ENOENT 允许这一回退。
- `override` 无覆盖时为 `null`。step 不属于该 pipeline → `404`。
- `active_version`：当前激活版本号（无激活指针 `null`——含「从未覆盖」与「已 deactivate 停用」两态）；`versions`：该 `(scope,domain)` 全部历史版本元信息（`[{version, note, created_at}]`，`version` 升序，不含 `content`）。**deactivate 后 `active_version=null` 但 `versions[]` 仍非空**（历史保留，可再激活）。

#### GET /api/prompts/{pipeline}/{step}/versions/{version}?scope=&domain=

查看某历史版本的**完整内容**（供编辑器「选历史版本」载入后基于它改）。Response `200`：
```json
{"version": "1", "content": "...(该版本 prompt 全文)...", "note": "首版", "created_at": "..."}
```
- `scope` 默认 `global`；`domain` 仅 `scope=domain` 时有意义。该版本不存在 → `404`。
- `version` 只接受规范十进制字符串 `1..2^63-1`；格式错误或越界 → `422`。

#### PUT /api/prompts/{pipeline}/{step}

请求体 `{scope, domain?, content, mode?, note?}`。`content` 替换该运行时步骤的正文覆盖目标,目标选择规则见本节。纯空白即视为**彻底删除**该覆盖(连同全部版本历史)。若只想回到 tracked 默认且**保留历史**,用 `POST .../activate {version:null}`。
- `mode`：`overwrite`（默认，改当前激活版本内容，版本号不变）或 `new`（另存为新版本 `version=max+1` 并激活）；首次保存恒为 `v1`（`mode` 忽略）。`note`：该版本一行备注（可空；`overwrite` 留空则保留原 note）。
- `scope='domain'` 但 `domain` 空 → `400`；step 非 AI 步（`pool!='ai'`）→ `400`；step 不存在 → `404`。
- 成功保存 `{"status": "saved", "active_version": "<新激活版本号>", ...}`；空内容删除 `{"status": "deleted", ...}`；历史已到 `2^63-1` 时继续 `mode=new` → `409`。

```json
{"scope": "global", "content": "你是资深技术编辑,产出结构化中文笔记...", "mode": "new", "note": "加了配图要求"}
```

#### POST /api/prompts/{pipeline}/{step}/activate

切换该步 `(scope,domain)` 的【激活指针】，**非破坏**（历史版本始终保留）。请求体 `{scope, domain?, version}`：
- `version="<十进制字符串>"` → 把该**历史版本设为当前激活**（re-activate）：主表 `content`/`version` 同步成该版本，下次派发用它。该版本不存在 → `404`；成功 `200 {"status":"activated", "active_version":"<version>", ...}`。旧客户端可继续发送 `1..2^53-1` 范围内的 JSON number，服务端响应仍规范化为字符串。
- `version` 的范围是 `1..2^63-1`；`0`、负数、浮点、布尔、大于上界的字符串整数，或大于 `2^53-1` 的 JSON number → `422`。
- `version=null` → **停用覆盖回内置默认**（deactivate）：删主表激活指针，`prompt_override_versions` 历史**全部保留**；`resolve_prompt_overrides` 随即返回空 → 派发用内置默认。成功 `200 {"status":"deactivated", "active_version": null, ...}`。
- `scope='domain'` 但 `domain` 空 → `400`；step 非 AI 步（`pool!='ai'`）→ `400`；step 不存在 → `404`。

```json
{"scope": "global", "version": "2"}
```

#### DELETE /api/prompts/{pipeline}/{step}?scope=&domain=

**彻底删除**该 `(scope,domain,pipeline,step)` 覆盖（连同其全部版本历史）。无则 no-op。Response `200 {"status":"deleted",...}`。注：这不是「恢复默认」入口（那会删历史）——回内置默认且保留历史请用 `POST .../activate {version:null}`。

### 1.16 前端 selected OpenAPI wire

前端稳定 JSON 契约由 `frontend/openapi/selected-paths.json` 显式选择，`scripts/generate-frontend-wire.sh` 生成确定性的 `frontend/openapi/openapi.json` 和 `frontend/src/types/generated/api.ts`。当前清单覆盖 sources、jobs/notes、status/system、workers、study、review/evidence、locator/concept、search/ask、AI tasks 和 prompts，共 83 个 HTTP operation。新增 operation 必须显式进入清单，不按 path 前缀自动扩张。

所选 operation 的每个 JSON 2xx 响应必须声明精确 response model；声明的错误响应统一为 `ErrorResponse {error,message}`。`GET /api/health/ready` 的 503 是 readiness 阻断投影而非错误信封，显式复用 `ReadinessResponse`。WebSocket、纯文本、二进制、Range 响应，以及 `meta/extra` 和审计诊断原始字段继续保留手写边界，不进入自动生成类型。

运行 `scripts/test.sh --wire` 校验 OpenAPI 快照和 TypeScript 输出的字节级一致性；CI 在后端普通分片中执行同一 drift gate。生成器同时校验 operationId、引用闭包、错误信封和手工例外，生成产物有漂移时必须先更新后端 schema 或显式清单，再重新生成并审阅 diff。

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
{"event": "step_failed",   "step": "11_smart", "error": "Claude rate limit", "retries": 1}
{"event": "step_skipped",  "step": "02_whisper", "reason": "subtitle exists"}
{"event": "job_done",      "progress_pct": 100}
{"event": "job_failed",    "error": "11_smart: Claude rate limit after 3 retries"}
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

**独立 AI task**：`/api/ask`、`/digest` 和学习建议把单次 AI 调用作为独立 task 投进 `queue:ai`，由具备对应 AI 接入方式 tag 的 ai-worker 执行——**不挂 job、不走 storage**，载荷与结果都内联。member 形态带 `kind:"ai"`（与 pipeline-step task 区分）：

```
Key:    queue:ai
Member: {"kind":"ai","task_id":"at_xxx","step":"synthesis|digest","domain":"<domain>|null",
         "provider":"<configured-provider>","model":"<explicit-model>",
         "request":<LLMRequest jsonable>,"tags":[...],"require_tags":["<provider-tag>","<capability-tag>"],
         "audit_context":<optional JSON object>,"pool":"ai"}  (JSON string)
```

`step=study_suggestions` 额外携带 `batch_id/attempt/revision/generator_fingerprint/input_fingerprint/prompt_snapshot/task_payload_sha256`。prompt、generator 和 task payload 这三类跨组件 hash 使用 `sha256:<64 lowercase hex>`；`input_fingerprint` 保持 64 位小写 hex。`task_payload_sha256` 覆盖除自身和认领运行字段外的 canonical JSON，Scheduler 入队和 Worker 调 provider 前都必须校验。

- `request` = `shared.models.LLMRequest.to_jsonable()`（messages/system/max_tokens/temperature/allowed_tools…；images 序列化为 str 路径，AI-RPC 路径一般不带图）。
- `audit_context` 可选、最大 512 KiB，必须是 JSON object。`step=synthesis` 必须携带 `ask_source_manifest`，并由 manifest hash、source fingerprint、artifact/body SHA 与同一个 `task_id` 形成不可变信任链；其他 AI task 缺省不写该字段。
- `provider/model` = 本次独立 AI task 请求的 provider 与模型,必须显式带出。缺失视为非法 AI task,不得补默认 provider/model。
- `require_tags` = provider 与运行能力的完整硬门控:Claude/Codex CLI 分别用 `claude-cli` / `codex-cli`;API provider 使用 `<provider>-api`(`anthropic-api` / `deepseek-api` / `kimi-api` / `openai-api`);需要文件 Read 时另加 `read`。无全部标签的 `ai` worker 不得认领该 task。
- `model` 必须是具体模型名。CLI provider 与 API-key provider 都不得使用模型占位符。
- pipeline-step task 的 member **不带 `kind`**（向后兼容，缺省即 `step`）。
- `queue:enqueued` field（§3.x 等待时长用）：step task=`{pool}|{job_id}|{step}`；**ai task=`{pool}|ai|{task_id}`**。
- 幂等投递: `ai:submitted:{task_id}` 保存 canonical task JSON 并带 7 天 TTL。相同 task 重放不重复入队；同 task_id 异 payload 直接冲突，不能覆盖。
- 原子认领: 一个 Lua 操作把 ZSET member 移出队列并写 `ai:claim:{task_id}` HASH，同时写 `ai:claims:expiry` ZSET；不存在 `ZPOPMIN` 后再建租约的任务丢失窗口。claim 精确绑定 `(task_id,batch_id,attempt,revision,worker_id,claim_id)`，状态为 `claimed → executing → succeeded|failed`，任何字段不匹配的 renew/finish 都失败。
- 崩溃恢复: `claimed` 到期最多安全回队一次并记录 `requeue_count=1`；再次到期进入 `ambiguous`。`executing` 表示 provider 可能已经产生副作用，到期只进入 `ambiguous`，绝不自动重试。Worker 在 provider 调用前 CAS 到 executing，调用期间续租，先写 Redis 结果和 DB 审计，再 CAS 终态。终态或 ambiguous 的迟到 worker 无权续租或覆盖新执行。
- 持久 deadline: 仍在 queue 的 task 必须原子移除 exact member 后才能把 DB 批次标为 timeout；`claimed` 只能用完整 `(task_id,batch_id,attempt,revision)` 在 provider 前 CAS 为 `canceled`。取消竞争失败不得推进 DB。持有活租约的 `executing` 不受业务 deadline 强制终止，继续等待 Worker 终态或租约到期转 `ambiguous`。
- 成功后处理: provider 成功与结果/审计/终态/事件发布分层处理。finish CAS 返回 false 或异常时保留成功 `airesult` 和唯一成功审计，claim 留在 `executing` 等待租约收敛；事件发布仅 best-effort，失败不得覆盖成功结果、追加 provider-failed 审计或把 claim 转成 failed。
- 槽位对账: `claimed/executing` 的 claim_id 是合法 `pool:ai:holders` holder，Scheduler `reconcile_slots` 不得当作泄漏释放；Worker `finally` 幂等释放。终态后 `ai:claims:expiry` 不再保留该 task。
- 结果回执：`airesult:{task_id}`（STRING，JSON = `LLMResponse.to_jsonable()` 或 `{"error":"..."}`，带 TTL≈600s）。Ask 成功结果额外带同一 `source_manifest` 与服务端 `citation_validation`；失败结果仍保留合法 source manifest 供排障。API 端通过 `GET …/result/{task_id}` / 同步等待取回（P1-3）。AI 用量经 `ai_usage`（`job_id=null, step=<step_name>`）记账（worker 侧，P1-2）。
- 自动周报（09 工单 P3）：`radar:digest:auto:{domain}:{YYYY-MM-DD}`（STRING SET NX，TTL 3 天，当日投递防重锁）；`radar:digest:latest:{domain}`（STRING JSON，无 TTL，最新一期 `{task_id, queued_at, [markdown, generated_at, error]}`——scheduler 收割 `airesult` 后搬入长存，`GET /api/domains/{d}/digest/latest` 读它）。
- 完成事件：worker 执行后 `publish events:{task_id}`（`ai_task_start/ai_task_done/ai_task_failed`，见 §3.5），供 `/ask`、`/digest` 经 `WS /api/ws/jobs/{task_id}`（端点对任意 id 通用）或轮询取信号。
- **白盒审计**：ai-worker 每次执行写一条 DB 表 **`ai_task_logs`**（按 `task_id`；对齐 DAG 步的 `output/ai_logs/{step}.jsonl`）。索引列：`exec_id/step_name/domain/provider/model/ok/error/各 token/cost_usd/duration_sec/num_turns/created_at`；`record_json` 存全量审计（路由/尝试链/渲染 prompt[system+messages]/输出/raw/用量/worker/ai_access_method/credential_kind/transcript）。Ask 额外存 `audit_context.ask_source_manifest` 与 `citation_validation`。与 `ai_usage`（成本归因）**并存不合并**（白盒 vs 计费两套）。查看端点见 P1-3。

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
  lifecycle_generation: 正整数，rerun/retry/resubmit 的新执行代递增
  terminal_generation:  已选出 job 终态的执行代（可缺）
  terminal_outcome:     "done" | "failed"（可缺）

Key:    job:{job_id}:steps
Type:   HASH
Fields: 每个步骤名 → 状态
  01_download:    "done"
  03_scene:       "running"
  11_smart:       "waiting"
  ...

Key:    job:{job_id}:retries
Type:   HASH
Fields: 每个步骤名 → 已重试次数
  11_smart:       "1"

Key:    job:{job_id}:step_worker
Type:   HASH
Fields: 每个 running 步骤 → 执行它的 Worker ID
  03_scene:       "cpu-a1b2c3d4"

Key:    job:{job_id}:step_exec / job:{job_id}:step_generation
Type:   HASH
Fields: 每个 running 步骤 → 当前 exec_id / lifecycle_generation

Key:    job:{job_id}:finalizer
Type:   HASH {generation,outcome,state,owner,lease_until}
State:  applying | applied；15 秒 owner lease 超时后可由其它 Scheduler 接管未完成副作用
```

### 3.4 Worker 注册

```
Key:    worker:{worker_id}
Type:   HASH
Fields:
  type:           "cpu" | "gpu" | "ai" | "io"
  pools:          "scene,cpu,io"
  tags:           "vision,read,claude-cli,codex-cli,kimi-api" ← 能力标签
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

`pools` 与 AI 接入方式分离:`--pools ai` 只声明该 worker 可进入 AI 资源池,不代表它拥有任何 AI 凭证。AI 接入方式一律通过 `tags` 硬区分:

| AI 接入方式 | 必需 tag | credential_kind | 典型 worker 命名 | 凭证位置/环境 |
|----------|----------|-----------------|------------------|---------------|
| `claude-cli` | `claude-cli` + `read` | `cli_auth` | `claude-1` | `$HOME/.claude/.credentials.json` 或等效 Claude CLI 登录态 |
| `codex-cli` | `codex-cli` | `cli_auth` | `codex-1` | `$CODEX_HOME/auth.json` 或 `$HOME/.codex/auth.json` |
| `anthropic` | `anthropic-api` | `api_key` | `anthropic-1` | `ANTHROPIC_API_KEY` |
| `deepseek` | `deepseek-api` | `api_key` | `deepseek-1` | `DEEPSEEK_API_KEY` |
| `kimi` | `kimi-api` | `api_key` | `kimi-1` | `KIMI_API_KEY` |
| `openai` | `openai-api` | `api_key` | `openai-1` | `OPENAI_API_KEY` |

同一 `ai` worker 进程可同时带多个接入方式 tag,但仅当该 worker **实际**具备对应凭证。匹配条件固定为 `require_tags ⊆ worker.tags`,全部硬标签都必须满足,不能只判断任意交集。`read` 是独立运行能力,不由 `pool=ai` 或 provider tag 隐含。注册和心跳事件必须记录 `worker_id/pools/tags/ai_access_methods`;删除 worker 后相应 per-worker token 吊销,旧 worker 即使仍有 AI 凭证也必须因 runner token `401/403` 退出。

Web UI `/system` 的「接入新 Worker」向导只负责生成启动参数,不直接改中心 worker 记录:勾选 `ai` 后出现「AI 接入方式」选择器。选择 `Claude CLI` 生成 `--pools ai ... --tags claude-cli read`;选择 `Codex CLI` 生成 `--tags codex-cli`;选择 `Kimi API key` 生成 `--tags kimi-api` 并在部署文件里要求 `KIMI_API_KEY`。Codex CLI 和 Kimi 不得生成 `read`;该选择器不得生成 `--pools claude` / `--pools codex` / `--pools kimi`,也不得把接入方式写成 worker type。

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

#### Worker task-scoped lease

```
Key:    runner:lease:{exec_id}
Type:   HASH {worker_id, job_id, step, exec_id, pool[, terminal]}
TTL:    180 秒；有效 progress/alive/usage/artifact/credential/heartbeat/长流复核可续租

Key:    runner:released:{exec_id}
Type:   HASH {worker_id, job_id, step, exec_id, pool}
TTL:    300 秒；只在正常 release 后留下，用于同一 release 幂等重放
```

有效租约还必须同时匹配 `job:{job_id}:step_worker[step]`、`job:{job_id}:step_exec[step]` 与 `job:{job_id}:steps[step]=running`。claim、terminal 占位与 lease hash/TTL 使用 Redis transaction/Lua 原子执行；rerun、orphan 回收或安全撤销删除 lease，不创建 released 墓碑，避免陈旧执行冒充正常重放。

**公共状态是读时派生，不直接存。** 运行时 `status`（`idle` / `busy` / `offline`，worker 自报）与管理员暂停态 `admin_status`（`"" / "paused"`，仅 API 写）是**两个独立字段**；`GET /api/workers` 不信任运行时 `status`，而是按 `shared/status.py` 的 `compute_worker_status()` 用 `last_heartbeat` 新鲜度 + `current_job` + 管理员 `admin_status` 叠加位现算出对外公共态。拆成两字段是为了让 `claim/release/心跳` 写运行时 `status` 时**不会覆盖暂停态**（旧实现 draining 复用 `status` 字段会被覆盖）：

| 公共态 | 含义 |
|--------|------|
| `online-busy` | 心跳新鲜且有在跑任务 |
| `online-idle` | 心跳新鲜且空闲 |
| `paused` | 管理员置 `admin_status=paused` 且仍在线（停止认领新任务，跑完当前步后等待，恢复前不接新活） |
| `offline` | 心跳超 `online_window`（默认 30s）但未到 `stale_window` |
| `stale` | 心跳缺失或超 `stale_window`（默认 900s），GC 信号 |

判定优先级：`paused`（仅在线生效）→ `offline` → `stale` → `online-busy` → `online-idle`。窗口阈值取自 `configs/pools.yaml` 的 `worker_status` 段，缺省回退内置默认。容器跑 UTC，故由后端统一派生，前端只渲染、不再用本地时区自算。`admin_status=paused` 是持久管理意图,worker 离线或重建导致 Redis 注册过期时不会被 stale worker GC 删除;恢复前仍不得认领新任务。

> 暂停态的调度交互：被暂停的 worker 在 `scheduler._pool_has_workers` 里算「无可用 worker」，故只剩暂停 worker 服务的池里、已就绪的步会等待，超 `NO_WORKER_GRACE_SEC`（默认 12h）才被 fail-fast。配合「夜间只跑 io worker / 白天暂停某类 worker」的运维窗口。

### 3.5 持久生命周期事件与展示通知

```
Stream: flori:lifecycle
Group:  flori:scheduler
Entry:  {topic,payload,emitted_at,schema="1"}
Topic:  job_command | step_completed | step_failed

Stream: flori:lifecycle:poison
Entry:  {source_id,topic,payload,error,attempts}
Limit:  近似保留 1000 条

Channel: step_started
Payload: {"job_id": "j_xxx", "step": "03_scene", "worker": "cpu-a1b2", "exec_id":"...", "generation":1}

Channel: events:{job_id}
Payload: (WebSocket 事件格式，同上 §2)

Channel: events:{task_id}            ← 独立 AI task(kind='ai')执行事件,供 /ask、/digest 的 ws/轮询取信号
Payload: {"event":"ai_task_start|ai_task_done|ai_task_failed","task_id":"at_xxx","step":"synthesis|digest"[,"error":"..."]}
```

`job_command` 与 pipeline step 终态以 Stream 为唯一权威通道。Scheduler 每批先用 `XAUTOCLAIM` 接管超时 PEL，再用 `XREADGROUP` 读新消息；处理成功后原子 `XACK + XDEL`。失败消息留在 PEL 重试，第 3 次失败转 poison stream 并 ACK，单个坏件不阻塞后续消息。Redis AOF 重启后 consumer group 和 PEL 必须仍可恢复。

pipeline 认领在单个 Lua 中完成全队列能力匹配、pool/resource holder、`ready -> running`、worker/exec/generation/progress 和 task lease 写入；不兼容队头不得阻塞后续可执行任务。step terminal 写入前原子核对当前 step status、exec_id、step/job generation 和 job 终态门，陈旧 worker 不能推进新一代执行。`step_started`、`events:{job_id}` 和 AI task 事件仍是可丢的展示/唤醒 Pub/Sub，不参与权威状态恢复；Stream 已持久后的 Pub/Sub 通知失败不得让命令 API 返回失败。

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
| `capability_rules` | 按当前产物决定的条件能力;目前支持 `read: {unless_any_nonempty: [安全相对路径...]}` |
| `rules` | 条件门：`exists` 命中后 `when: on`（启用）或 `when: skip`（跳过） |
| `prompt_template` | 可选的 tracked 正文模板名;省略时等于运行时步骤名 |
| `ai` | AI provider 路由：`primary` / `fallback` / `text_fallback`，各取 `{provider, model}` |
| `on_complete` | 步骤完成后的幂等副作用列表；每项为 `{action,...}`，支持 `sync_metadata`、`index_note`、`collect_glossary`、`collect_term_pairs` |

**每段 `variables`** 是该 content_type 的单一事实源（AI provider/model、OCR 超时等），job 用 `$VAR` 引用。

`on_complete` 是完成副作用的唯一声明源，scheduler 不维护内容类型或步骤白名单。`index_note.candidates` 按顺序选择首个存在的 `{note_type,path}`；`path` 可用 glob，article 默认按 `smart → translated → original` 回退且同一 job 只保留一个候选来源。步骤状态变成 `done` 后副作用可重复执行：全文、证据块和候选来源替换在同一 SQLite 事务中完成；job 进入 `done` 前会重放全部已完成步骤的声明，失败时保持 active 并由周期对账继续收敛。治理前已完成但尚无全文记录的当前 job 也按相同声明补账，不依赖遗留 Redis step 状态。

pipeline 中的 `step.name` 是唯一运行时身份。复用另一个步骤模块或通过 `prompt_template` 复用正文都不改变这个身份;done marker、progress、AI log、provider override 和 prompt override 全部使用运行时名。video 概念步因此始终是 `12_concepts`,即使它运行 article 概念模块并映射到 `05_concepts` 模板。

> **AI provider / model 显式规则**：`ai.provider` 和 `ai.model` 必须在任务配置或载荷中显式出现,不得由 `pool=ai`、worker 名称或运行时默认推断 provider。pipeline 和独立 AI task 必须使用具体模型名（如 `claude-opus-4-8[1m]`、Codex CLI 可接受的模型名、`moonshot-v1-128k`）。缺 provider 或缺 model 是契约错误,不得用运行时默认值补齐。

> **AI tier 保真**:当前四条 pipeline 共有 16 个可执行 AI 路由和 33 个有序 tier。`primary/fallback/text_fallback` 是尝试顺序,不是可去重的集合。即使相邻 tier 的 provider/model 完全相同,也保留两次独立尝试语义;调用次数、retry、usage、AI log 和请求 payload 均不因配置值相同而折叠。

> **AI 接入方式 tag 路由**:`claude-cli` → `claude-cli`;`codex-cli` → `codex-cli`;API provider → `<provider>-api`;`local` → `local`。没有 override 时,任务要求 pipeline 所有可执行 tier 的 provider tag;有 override 时只要求所选 provider tag。`pool=ai` 只是容量队列,接入方式 tag 才表示该 worker 具备哪种 AI 凭证。

`capability_rules` 的 `unless_any_nonempty` 表示:列出的任一产物存在且非空时不要求该能力,全部缺失或为空时才要求。当前唯一条件能力是 `read`;只有 provider 配置声明且运行时实现该能力时才可路由,Worker 还必须独立携带 `read` tag。路径必须是 job 内安全相对路径,未知能力、空路径列表、绝对路径或穿越路径均 fail-closed。scheduler 在入队、no-worker 对账和 rerun 校验时按中心存储计算;执行端按本地实际产物和实际 `allowed_tools=["Read"]` 再算,任一侧不满足都不得静默回退。当前 paper 的 `04_translate_paper` 与 `05_smart_paper` 使用该规则。

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
      version: "2"
      image: flori/step-heavy
      needs: ["05_dedup"]
      timeout: $OCR_TIMEOUT
      retry: $OCR_RETRIES

    "08_punctuate":
      extends: .ai-step
      run: steps.video.step_08_punctuate
      version: "3"
      needs: ["01_download", "02_whisper", "06_ocr"]
      timeout: 300
      retry: 3
      rules:
        - exists: "input/*.srt"
          when: on                     # 有字幕（含 whisper 产出）才标点
      ai:
        primary: {provider: $AI_PUNCT_PRIMARY_PROVIDER, model: $AI_PUNCT_PRIMARY_MODEL}
        fallback: {provider: $AI_PUNCT_FALLBACK_PROVIDER, model: $AI_PUNCT_FALLBACK_MODEL}

    "10_evidence":
      extends: .ai-step
      run: steps.video.step_evidence
      needs: ["09_mechanical"]
      ai:
        primary: {provider: claude-cli, model: "claude-opus-4-8[1m]"}
        fallback: {provider: claude-cli, model: "claude-opus-4-8[1m]"}

    "11_smart":
      extends: .ai-step
      run: steps.video.step_11_smart
      needs: ["09_mechanical", "10_evidence"]
      tags: ["vision"]
      ai:
        primary: {provider: $AI_SMART_PRIMARY_PROVIDER, model: $AI_SMART_PRIMARY_MODEL}
        fallback: {provider: $AI_SMART_FALLBACK_PROVIDER, model: $AI_SMART_FALLBACK_MODEL}
        text_fallback: {provider: $AI_SMART_TEXT_PROVIDER, model: $AI_SMART_TEXT_MODEL}

    "12_concepts":
      extends: .ai-step
      run: steps.article.step_05_concepts
      prompt_template: 05_concepts
      needs: ["11_smart"]
      ai:
        primary: {provider: $AI_CONCEPTS_PRIMARY_PROVIDER, model: $AI_CONCEPTS_PRIMARY_MODEL}
        fallback: {provider: $AI_CONCEPTS_FALLBACK_PROVIDER, model: $AI_CONCEPTS_FALLBACK_MODEL}

    "12_review":
      extends: .review
      run: steps.video.step_12_review
      needs: ["12_concepts"]
      ai:
        primary: {provider: $AI_REVIEW_PRIMARY_PROVIDER, model: $AI_REVIEW_PRIMARY_MODEL}
        fallback: {provider: $AI_REVIEW_FALLBACK_PROVIDER, model: $AI_REVIEW_FALLBACK_MODEL}
```

**各 content_type 的 job 链**（`needs` 推导）：

- **video**:`01_download` → `03_scene` → `04_frames` → `05_dedup` → `06_ocr`;`02_whisper` 由 `01_download` 旁路触发;`08_punctuate` 汇合 `01_download` + `02_whisper` + `06_ocr`,一次发布含字幕与 OCR 图像段的来源清单;`09_mechanical` 再汇合 `06_ocr` + `07_danmaku` + `08_punctuate` → `10_evidence` → `11_smart` → `12_concepts` → `12_review`。`11_smart` 同时依赖 `09_mechanical` 与 `10_evidence`。
- **paper**:`01_download` → `02_pdf_parse` → `03_sections` → `04_translate_paper`(条件) → `05_smart_paper` → `05_concepts` → `06_review`。(`04_figures` 已随 pymupdf 删除:arxiv 图随 HTML 进正文,pdf-only 图在 PDF 里模型直读;旧 job 的 `figures.json` 仍被 05/06 可选消费。)
  - **源头(2026-07 重做,HTML 优先)**:`01_download` 对 arxiv 除 PDF 外抓 **HTML 源**(官方 `arxiv.org/html/<id>` → 404 再 `ar5iv`)→ `input/source.html`,页内图片下载到 job 根 `assets/`、引用重写 `assets/<名>`。`02_pdf_parse`(步名保留,语义=论文解析):有 `source.html` → LaTeXML HTML 转干净 Markdown(`steps/utils/html_paper.py`;`<math alttext>`→`$…$`/`$$…$$`,图+图注,表 best-effort)→ **`output/original.md`(02 产,03 不再覆盖)** + 扁平 `sections`;`parsed.json.source_kind="arxiv-html"`。无 `source.html` → `source_kind="pdf-only"`,不产 original.md(原文=内嵌 PDF;AI 步直喂 PDF,见下一提交)。
  - **JobDetail 新字段 `source_kind`**(`"arxiv-html"|"pdf-only"|null`,读 parsed.json best-effort):前端原文变体据此分流——`arxiv-html` 直接渲染 `original.md`(公式/图无损);其余内嵌 PDF。
  - `02_pdf_parse` 检测正文主语言写 `parsed.json.lang`(判据与文章共用 `steps.utils.lang`);**非中文**额外写 `intermediate/needs_translation.json`;元信息(title/authors/abstract/pages/lang/**venue**)经 JobDetail `media` 透出「元信息」tab(与文章同)。
  - **来源(`media.venue` / `media.sitename`)**:arxiv-html 恒 `arXiv`;pdf-only 由 `_extract_venue` 抽会议/期刊+年份(`OSDI 2023`;全名→缩写映射在 `configs/venues.yaml`);文章 `02_parse_article` 取 trafilatura `sitename`(URL 域名兜底)。前端「来源」= `venue`/`sitename`,无则回退来源类型标签。
  - **arxiv 元数据**:`01_download._fetch_arxiv_meta` 查 arxiv API(`export.arxiv.org/api/query`,标准库 ElementTree 解析)→ `title/authors/abstract/published_at` 写入 `input/metadata.json`(权威来源);`02_pdf_parse` 优先用其 `title/authors/abstract`。best-effort,API 失败回退解析启发。
  - **pdf-only 直喂**:`02` 用 poppler `pdfinfo` 取页数(fail-loud)→ `parsed.json.pages` + 页区间伪章节(`sections[].kind="page-range"`,每 4 页);语言不可判 → `lang="unknown"` 且**恒写** `needs_translation.json`(用户约定:纯 PDF 默认要译)。`04_translate_paper` 按**每 2 页一块**逐块 `claude Read` 直读 PDF 翻译(prompt 模板 `templates/04_translate_paper.pdf.md`:标题层级固定映射/截断句照译/图表占位带页码 **`【图 N|第 p 页】`**,OSDI04 三轮人工对读验证);翻译聚合后**渲染含图页插真图**——对占位页码去重跑 `pdftoppm -r 110` → job `assets/pdf-page-<p>.png`,占位行下插 `![](assets/pdf-page-<p>.png)`(PDF 矢量图无法通用抽取,整页渲染兜底;单页失败留占位)。pdf-only 标题:PDF 内嵌 metadata 为垃圾(`"10things"`/`paper.dvi`/系列名)时,`02` 从 `pdftotext` 首页启发式提真标题(`shared/titles.py`),scheduler `metadata_sync` 对**已入库垃圾标题**允许被更优候选覆盖(同一套判定);`05_smart_paper` 无任何文本正文时同法 Read 直喂产笔记(≤60 页 cap)。`LLMRequest` 新增 `add_dirs`(claude-cli `--add-dir` 放行 PDF 目录,仅 allowed_tools 分支);worker 镜像新增 **poppler-utils**(Read 渲染 PDF 依赖 pdftoppm)。
  - `04_translate_paper`(AI,`rules.exists` 门控,仅非中文论文):**翻译源 = `output/original.md`**(arxiv-html 干净原文,图/公式已在原位;老 job 无 original.md 时回退 sections+figures 组装),分 chunk 忠实翻译为简体中文 → `output/translated.md`,供「译文」变体。步 `timeout: 7200`(多 chunk 串行,默认 600 必超)。
  - `05_smart_paper` `needs` 含 `04_translate_paper`:非中文论文笔记**基于 章节+图表+译文**(有译文则用译文正文);译文跳过(中文论文)依赖视为满足、读原文。
- **article**：`01_download` → `02_parse_article` → `03_article_sections` → `04_smart_article`(可选,`smart_note`)/`04_translate_article`(条件) → `05_concepts`(必跑) → `06_review`(可选)。
  - `02_parse_article` 检测正文主语言写入 `parsed.json` 的 `lang`(`zh`/`non-zh`/`unknown`);**非中文**正文额外写标记 `intermediate/needs_translation.json`。
  - **空正文护栏**:`02_parse_article` 抽出的正文【有效字符数】(去空白后)< `MIN_BODY_CHARS`(=200)→ 直接抛 `InputInvalidError`(`error_type=input_invalid`,不重试),**不写任何产物**,job 明确 `failed`。挡付费墙/JS 渲染/订阅残桩页(抽 0 或极短正文)流向 `03/04/05` 的 AI 步——否则 LLM 拿空正文幻觉编笔记/概念,污染概念库与图谱(`key_terms` 是图谱唯一概念来源)。命中常见付费墙/登录墙标记仅细化错误信息,判废只看正文长度。
  - `04_translate_article`(AI):`rules.exists: intermediate/needs_translation.json` 门控——**仅非中文文章触发**,把 `output/original.md` 忠实全文翻译为简体中文 → `output/translated.md`(保留 Markdown 结构与 `![](assets/…)` 图位),供前端「译文」tab。
  - `04_smart_article` / `05_concepts` **`needs` 含 `04_translate_article`**:非中文文章的中文产出**基于译文**(术语一致,不重复英→中)——`04_smart` 有 `translated.md` 则笔记基于译文;`05_concepts` 源优先级 **智能笔记 > 译文 > 原文章节**。译文被跳过(中文文章)时依赖视为满足,两步照常读原文。
- **audio**:`01_download` → `02_whisper` → `03_transcript_parse` → `04_smart_podcast` → `05_concepts` → `05_review`。
  - `01_download`(`content_type=audio`):支持音频直链(`.mp3/.m4a/.wav/.aac/.flac`)与播客**页面 URL**(best-effort 从页面 `og:audio`/`<audio>`/`<source>`/`<enclosure>`/裸 `*.mp3` 链解析音频真链);下载后 **ffprobe 校验**(无可解码时长=拿到 HTML/404 → `InputInvalidError`,不再拖到 whisper 才报晦涩 ffmpeg 错)。
  - `02_whisper`:超时**随时长伸缩**(见 `timeout_per_min`/`timeout_max_sec`)——无 GPU 时长集 CPU 转写远超固定 1800s。worker 跑步前读 `input/metadata.json.duration_sec`,有效超时 = `clamp(max(timeout, ceil(分钟)*timeout_per_min), timeout_max_sec)`;缺 `timeout_per_min` 或读不到时长则用静态 `timeout`(行为不变)。机制通用,任何步均可在 pipeline 加这两字段启用。
  - `04_smart_podcast`:**不再 12k 截断**。转写 ≤ `SINGLE_PASS_CHAR_LIMIT`(24000 字)单次成稿;超过则 **map-reduce**(按 segment 边界分段提炼要点 → 合并成完整笔记),覆盖全集不丢正文。`result.meta` 增 `mode`(`single`/`map_reduce`)与 `chunks`。

四类概念步共用一份来源解析契约:

- video 和 audio 只允许使用最新的版本化智能笔记,不回落到机械稿或转写。
- article 和 paper 依次选择最新智能笔记、`output/translated.md`、`intermediate/sections.json`。
- validate、input hash 和 execute 共用同一份来源快照,不在三个阶段重新选源或重读。快照绑定类型、路径和 SHA-256。
- 所有允许的来源均缺失、来源损坏或 pipeline 类型未知时 fail-closed,不发起 AI 调用。

新增内容类型的 DAG 仍在此文件声明。若复用概念步,必须同时在来源契约中显式登记该 pipeline;未知类型不得靠默认分支猜测来源。

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

仅对 `keep=true` 的帧做 OCR。`asset_sha256`、`width`、`height` 绑定 OCR 当时读取的真实图像；步骤在识别前后复算 SHA-256，帧变化时整步失败且不发布 sidecar。`text` 是各识别行用换行拼接的纯文本，`boxes` 是逐行的框/置信度明细：

```json
[
  {
    "index": 0,
    "filename": "frame-0000.jpg",
    "timestamp_sec": 1.5,
    "asset_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "width": 1920,
    "height": 1080,
    "text": "0.32\nloss\nepoch",
    "boxes": [
      {"text": "0.32", "confidence": 0.987, "box": [[10, 8], [60, 8], [60, 28], [10, 28]]}
    ]
  }
]
```

缺图条目保留 `filename/timestamp_sec`，但 `asset_sha256/width/height` 均为 `null`、`text` 为空且 `boxes=[]`。视频 image locator producer 只接受 64 位小写 SHA-256、正整数尺寸且与当前 `assets/<filename>` 字节和实际尺寸完全一致的条目；bbox 规范化为 `[x0,y0,x1,y1]` 后还必须落在图像尺寸内。旧 OCR sidecar 缺少这些身份字段时只跳过 image locator，不影响同一视频的 media locator。

### 4.7 danmaku.json — 弹幕

```json
[
  {"time_sec": 1.68, "text": "前排学习"},
  {"time_sec": 15.3, "text": "这个推导讲得真清楚"}
]
```

### 4.8 review.json — 评审结果

评审步写 `schema_version=2` 的严格、可重验结果:最新结果为 `output/review.json`,并按所评智能笔记版本 1:1 保存版本化评审。v2 顶层字段固定为 `schema_version`、`score_keys`、当前 pipeline 的全部评分键、`overall`、`key_terms`、`missing_concepts`、`top3_improvements`、`issues`、`review_reliable`、`reliability_reasons`、`review_input`、`completion`、`parse`、`citation_validation`、`review_coverage`、`note_file`、`provider`、`model`、`generated_at`;可靠结果不允许额外字段。

各 pipeline 的 `score_keys` 顺序也是契约:

- video:`completeness / accuracy / structure / terminology / visual_integration / readability`。
- paper:`completeness / accuracy / structure / terminology / formula_integrity / figure_references`。
- article:`completeness / accuracy / structure / readability / insight`。
- audio:`completeness / accuracy / structure / terminology / conciseness / readability`。

每项评分必须是真正的 JSON integer `1..5`,bool 不可冒充整数;`overall` 必须等于全部评分的一位小数均值。`key_terms` 是已讲清的概念及定义;`missing_concepts` 只供诊断,永不进入 glossary;`top3_improvements` 必须恰好三项。

`issues[]` 的固定语义:

- `type` 只能是 `consistency / missing_in_source / missing_external / traceability`。
- `severity` 只能是 `info / warning / error`。
- `evidence_status=supported` 必须带 `locator={source,quote,offset}`,且 quote 必须逐字命中本次送评的真实来源。
- `evidence_status=insufficient` 必须带非空 `reason`,不能伪造 locator。

`review_input` 与 `review_input.sources[]` 都记录 `artifact / sha256 / bytes / chars / truncated`;source 另带唯一 `label`。来源最多 14 个,单件、持久化 prompt 和来源合计分别以 8 MiB 为硬上限,任何超限都在 AI 调用或可信投影前失败,不静默截断。送评来源按内容摘要持久化,保证历史 locator 可重算。

`review_reliable=true` 不是可直接信任的自报字段。生成时和每次读取时都必须同时满足:

1. 严格 JSON、精确顶层字段与当前 pipeline 的 score profile。
2. provider 终态证明可重算为 complete,attempt 链只有最后一次成功,且 parse 为 strict。
3. prompt 与全部来源完整、未截断,摘要、字节数、字符数和路径均与当前产物一致,每个来源原文确实进入 prompt。
4. `note_file` 与 smart source 完全一致,`review_coverage` 覆盖整篇智能笔记,四类 pipeline 的来源集合符合生产规则。
5. issue locator 可在绑定来源中复算,citation 状态为 `valid` 或 `not_applicable`。

读取重验采用单次请求快照:同一相对路径的底层有界 reader 最多调用一次,`bytes`、缺失和首次读异常均在本次请求内复用。缺失、篡改、大小超限、非法 UTF-8、竞态或 reader 异常只会把结果降级,不得让持久化的 `review_reliable=true` 越过门禁或产生 500。

`GET /api/jobs/{id}/review` 返回固定安全投影,`reliability_state` 为 `reliable / unreliable / legacy_unverified`。只有 reliable 才暴露 `overall`、维度分、`key_terms`、issue locator、source artifact 和 `note_file`;unreliable/legacy 保留清洗后的诊断,但分数置空、`key_terms` 清空、locator/artifact 清空。文件不存在返回 `404`;非法 JSON、非对象或超过顶层读取上限返回 `422`;可解析的 legacy/unreliable 返回 `200` 安全投影。

### 4.9 evidence.json — 权威来源（案例取证，ADR-0012）

案例类 video(`domain=finance` 或 `style_tags` 含 `case-study`)由 `10_evidence` 产出 `output/evidence.json`;非案例类由步骤自门控跳过。模型只允许返回最多 12 个 `{title,url,publisher,reason}` 候选,不得自行下载正文或声明可信度。服务端禁代理抓取,对原始 URL 与每次 redirect 逐跳重验 scheme、userinfo、端口、DNS 与全球 IP,拒绝内网/环回/链路本地/保留地址;同时限制 MIME、编码、正文大小和最多 5 次 redirect。

v2 manifest 顶层字段必须精确为 `schema_version / job_id / ocr_refs / evidence / rejected / total_bytes / candidate_parse_failed / provider`:

```json
{
  "schema_version": 2,
  "job_id": "j_20260516_abc123",
  "ocr_refs": ["〔2018〕88号"],
  "evidence": [
    {
      "id": "E1",
      "job_id": "j_20260516_abc123",
      "title": "标题",
      "publisher": "发布方",
      "artifact": "output/evidence/evidence-01.md",
      "sha256": "sha256:<64 hex>",
      "bytes": 1234,
      "chars": 1188,
      "original_url": "https://www.csrc.gov.cn/example",
      "final_url": "https://www.csrc.gov.cn/example",
      "source_tier": "一手官方",
      "confidence": "high",
      "eligible": true,
      "eligibility_reasons": [],
      "matches": [{"anchor": "〔2018〕88号", "offset": 42}],
      "retrieved_at": "2026-07-14T08:00:00+00:00"
    }
  ],
  "rejected": [],
  "total_bytes": 1234,
  "candidate_parse_failed": false,
  "provider": "claude-cli"
}
```

稳定编号只允许 `E1..E12`,并与 `output/evidence/evidence-01.md` 到 `evidence-12.md` 一一对应。单件正文上限 1 MiB、全部正文合计上限 4 MiB、当前机械稿上限 8 MiB。候选解析失败必须写 `candidate_parse_failed=true`,整份 manifest 不可被当作可靠证据。

只有原始 URL 和最终 URL 均为 HTTPS 官方域名,且下载正文命中从当前机械稿重新提取的案号/文号锚点,服务端才派生 `eligible=true / confidence=high / source_tier=一手官方`。抓不到时如实进入 rejected 或低可信项,绝不用二手来源冒充一手。

每次读取重新验证 `job_id`、顶层精确 schema、E# 与固定文件名、规范相对路径、sha256、bytes/chars、总字节、当前机械稿锚点及所有派生字段。API 投影的 `manifest_state` 为 `verified / partial / invalid / legacy`,`reliability_state` 为 `verified / unreliable / legacy_unverified`。只有读时重新验证通过的高置信一手项可保留 `final_url`、`artifact` 与 `link_safe=true`;低可信、legacy、无效或未验证项必须清空 URL 与 artifact。

`11_smart` 只以 `[E1]..[E12]` 引用已绑定正文,`12_review` 服务端重验引用的来源资格、金额/数字/单位与所在上下文(DAG:`09_mechanical → 10_evidence → 11_smart → 12_concepts → 12_review`)。citation 总状态为 `valid / unverified / invalid / not_applicable`;畸形或越界 E 引用 fail-closed。

`GET /api/jobs/{id}/evidence` 返回上述安全投影,不是原始 manifest。文件不存在返回 `404`;非法 JSON、非对象或超过顶层读取上限返回 `422`;可解析的 legacy/partial/invalid 返回 `200` 诊断投影。

### 4.10 术语一致性(term_map / term_pairs / collections terms)

翻译专有名词一致性(分层 TermMap,工单 26-07-06/04;`shared/terms.py`):

- **`input/term_map.json`**(scheduler 在 submit/rerun 时导出;worker 只读):
  `{"<english term>": "<中文译名>", ...}` —— L1=该 domain 的 glossary 提炼
  (`zh_name` 列 > 「中文(English)」式 term > definition 首短名;提不出不导出),
  job 属集合且存在集合表时 L2 覆盖合并。翻译步按 chunk 命中注入 prompt(`<<TERMS>>` 段,上限 40 条)。
- **`output/term_pairs.json`**(翻译步产出,仅有新词才写):本篇新定的「英文→译名」对照
  (译文「中文(English)」回收,复现验证)。scheduler 于翻译步完成时回流:
  ① glossary(status=suggested,带 `zh_name`);② job 属集合 → merge 进集合表(先到先得)。
- **`collections/{collection_id}/terms.json`**(对象存储,book 集合级 L2):结构同 term_map。
- glossary 表新增列 `zh_name`(标准中文译名,默认空;概念步 key_terms 的 `zh_name` 字段回填,
  存量经 `scripts/backfill_zh_names.py` 三段式补齐)。
- 优先级:L3(篇内首译)> L2(集合)> L1(域);已注入的译名不被后续 chunk 改写。

**book_toc 订阅契约**:`source_type=book_toc`,`source_id=书目录 URL`(jupyter-book/sphinx 结构);
章 job=article 链 + `smart_note=true`;sync 建章 **defer**(不 publish new_job),由 scheduler 在
前章终态时按 created_at 序 submit(严格串行,失败章放行不卡书);`BOOK_MAX_CHAPTERS` env 控章数(默认 5)。

### 4.11 概念实体与关系边(output/concepts.json / glossary 归一,工单 26-07-06/09)

- **`output/concepts.json`**(四类链的 concepts 步产出,scheduler `_collect_glossary` 优先采集):
  顶层可带 `evidence_note_type=smart|translated|original`；`key_terms` 元素
  `{term,definition,zh_name|null,related:[{term,rel}],evidence_source_segment_ids:[seg_<64hex>]}`。
  evidence refs 不信任模型自报：producer 只从已重验 path/hash/job/pipeline 的 provenance anchor 中，
  对 term/zh_name 做唯一逐字命中后覆盖生成；Latin 名称按 token boundary。Scheduler 在 canonical
  index 完成后把 `(job,note_type,source_segment_id)` 映射为当前 evidence ID，并按整 job 原子替换
  `concept_occurrences`；本次空/坏/不可靠输入会清旧精确映射，不删除 glossary 实体。
  `related.rel` ∈
  `prerequisite`/`is_a`/`part_of`/`related`,只允许引用本次 `key_terms` 中的其它概念。
  采集时两端经 `shared.concepts.resolve` 归一到实体主名;目标未入库不建边(待其被采集后
  下次出现自动连上)。只有存量 job 缺 `output/concepts.json` 时才回退 `output/review.json`;
  回退前必须按当前 pipeline 完整重验,仅 `review_reliable=true` 的 v2 `key_terms` 可进入 glossary。
  legacy、抢救解析、截断、篡改、引用失败、不可靠或未知 pipeline 全部拒绝,`missing_concepts` 永不入库。
- **实体归一**(`shared/concepts.py`):`norm_key` = 小写 + 全半角统一 + 空白折叠 + 剥
  「主名 (Note)」注音尾;采集先按 `(domain, term)` 精确匹配,再撞域内 `term`/`zh_name`/
  `aliases` 归一键,命中挂 occurrence(job 去重)+ 新变体入 `aliases`,未命中按主名规则新建。
- **存量运维脚本**(三段式,LLM 段只产建议留档、人审后 apply;文件交接走 worker 家目录):
  `scripts/merge_glossary_entities.py`(scan 确定性合并 / suggest 语义组+junk / apply-llm),
  `scripts/backfill_concept_edges.py`(export 核心概念 / suggest 关系边 / apply)。

### 4.12 `shared/migrations/manifest.json` + `schema_migrations`

SQLite schema 由不可变 migration manifest、代码 registry 和数据库 ledger 共同约束：

- manifest 的 `format` 固定为 `flori-sqlite-migrations`。`minimum_supported_version`、`current_version`、`ledger_version` 和 `migrations[].version` 必须是真正的 JSON integer，`bool` 不可冒充整数。
- 当前格式要求 `minimum_supported_version == 0`、`current_version >= 1`、`1 <= ledger_version <= current_version`。`migrations` 长度必须等于 `current_version`，版本从 1 连续递增；`name` 非空，`checksum` 是 64 位小写十六进制 SHA-256。
- 代码 registry 的版本、名称和 payload checksum 必须与 manifest 完全一致，而且必须在触碰数据库前完成验证。已发布条目只可追加，不可改写。
- ledger 字段固定为 `version/name/checksum/applied_at`。达到 `ledger_version` 后，`schema_migrations` 必须精确覆盖 `1..PRAGMA user_version`，每条记录匹配 manifest，且 `applied_at` 为非空字符串。
- 当前 schema 数字不在本文硬编码，以 tracked manifest 为单一来源。
- SRS 迁移只追加当前 schema: `study_cards.revision`，reviews/logs 的 UTC epoch 微秒，log 的全局 request id/fingerprint/revision before+after/immutable outcome。历史 v1/v2 payload 与 checksum 不修改；当前 validator 校验全部 schema，不在合法新版 schema 上调用旧版 exact validator。

### 4.13 DR archive manifest v2

新发布归档的顶层格式为：

```text
format = "flori-disaster-recovery"
format_version = 2
compatibility.min_restore_format = 2
compatibility.sqlite_user_version = N
compatibility.database_schema:
  version = N
  minimum_supported_version
  maximum_supported_version
  migration_history = manifest.migrations[0:N] 的 {version,name,checksum} 投影
  migration_history_sha256
sqlite.migration_history = 数据库 ledger 的 {version,name,checksum} 投影；低于 ledger_version 时为 null
```

- `database_schema.migration_history` 长度必须等于 SQLite `user_version`，版本连续，并且是恢复端本地 migration manifest 的完全相同前缀。
- `migration_history_sha256` 是 history 使用 UTF-8、对象 key 排序、无多余空白的 canonical JSON 编码后的 SHA-256。
- 达到 ledger 版本后，归档数据库内 `schema_migrations` 的 `version/name/checksum` 投影、`sqlite.migration_history` 和 `database_schema.migration_history` 三者必须一致；`applied_at` 只保留在数据库 ledger 中，不进入归档 history。低版本库不得伪造 ledger。
- 恢复端除比较版本和 checksum 外，还会在临时数据库副本上执行同一生产 migration runner，证明归档能迁移到当前版本并通过完整 validator。
- 当前恢复器仍接受 legacy format v1。v1 可以没有 migration history，但仍受本地版本范围和生产迁移链 dry-run 约束。
- 未来版本、同版本 checksum 分叉、篡改 ledger 或无法通过启动 validator 的 schema，均在切换目标前被拒绝。

### 4.14 Restore transaction marker 与 result JSON

每个资产切换使用 `.flori-dr-transaction.json`，字段集合固定为：

```text
format, generation, asset, base, status,
old_names, new_names, preserve_names, moved_old, moved_new
```

状态机为：

```text
prepared -> switching -> committed -> accepted -> finalizing -> marker 删除
```

- `asset` 仅可为 `data/redis/minio/config`，`generation` 和 `base` 必须与本次恢复一致。名称列表必须唯一、有序且只含目标根直属名称；marker、base、old/new 和受控名称都不得经 symlink 逃逸。
- `moved_old` 和 `moved_new` 必须分别是声明列表的已移动前缀；进入 `committed/accepted/finalizing` 时必须完成全部切换。
- commit 阶段完成后、accept 阶段开始前发生切换错误时，本次调用统一反向回滚。当前进程一旦开始 accept，普通异常会尝试统一 roll-forward；即使首个 `accepted` marker 尚未持久化，也不在同一调用中回滚已经 commit 的新代。
- 进程重启后的恢复只以持久 marker 为准：全部资产仍是 `committed` 时统一反向回滚；任一资产已经进入 `accepted/finalizing` 即形成持久的全局提交决策，其余 `committed` 资产只能 roll-forward，禁止回滚。
- `finalizing` 表示新代已经生效，只剩 stage 或旧代清理；即使 stage 已删除但 marker 尚在，也必须幂等完成 marker 清理。
- marker 损坏、活动事务所需 marker 缺失、stage 存在但 marker 缺失、混合 generation、重复 asset，或 `accepted` 与未提交状态混合时，恢复器保留现场并 fail-closed，不自动猜测或删除。没有活动事务或 finalize 已正常完成时，目标根无需保留 marker。

成功 restore result 的字段为：

```text
status, operation, generation, started_at, completed_at, rto_seconds,
restored_assets, skipped_assets, cleanup_pending,
commit_recovered_after_error, error_type,
preserved_target_entries, checks
```

正常成功时 `commit_recovered_after_error=false`、`error_type=null`。若 accept 阶段发生普通错误，但统一前滚和收尾成功，仍返回 `status=success`，同时令 `commit_recovered_after_error=true`、`error_type=<原异常类>`。`cleanup_pending` 非空表示新代已经提交，只是对应 marker 处于 `accepted/finalizing` 的清理待续状态，不是回滚信号。常规失败 result 仅保证 `status/operation/error_type/error/completed_at`。

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
| 416 | `range_not_satisfiable` | Range 请求越界 |
| 422 | `invalid_request` | 请求体校验失败，或来源 / 内容类型 / 上传扩展名没有可执行适配器 |
| 429 | `rate_limited` | 请求触发限流 |
| 502 | `bad_gateway` | 上游服务返回无效结果 |
| 503 | `unavailable` | 必需依赖暂不可用 |
| 500 | `error` | 服务内部错误 |

Response body:
```json
{"error": "not_found", "message": "job not found"}
```

Selected OpenAPI operation 声明的非 2xx JSON 响应统一引用 `ErrorResponse`，运行时校验错误也投影为相同的 `error/message` 两字段，不暴露 FastAPI 默认 `detail` 结构。唯一显式例外是 `GET /api/health/ready`：HTTP 503 表示 readiness 阻断状态，响应仍为完整 `ReadinessResponse`，便于发布门读取各项检查。

> 契约与实现现状（避免再漂移）：
> - `POST /api/jobs` 的 `url` 接受 http(s) 链接**或裸 B 站 BV 号**（`detect_source` 解析），不强制
>   http(s) 前缀；其它未知裸标识和 scheme 以 `422 invalid_request` fail-closed。
> - 同 URL / 同 BV 重投**不返回 409**，而是建新任务（job_id 加随机后缀消歧），故不返回
>   `job_already_exists`。
> - 业务端点（如 `POST /api/jobs`）的限流 429 与「无在线 worker」503 `no_workers` 仍**未实现**；
>   但 **runner 鉴权自卫已实现 429**（per-worker token 连续 401 达阈值 → 429 + `Retry-After`，见 §1.7）、MCP-http 限流 429 也已实现（§4）。

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
    未设→503,除非 `FLORI_MCP_ALLOW_NO_AUTH=1`(仅可信内网放行)。compose 服务 `mcp-http` 默认绑 127.0.0.1。
  · **DNS-rebinding allowlist**:`FLORI_MCP_ALLOWED_HOSTS` 为逗号分隔 Host;配置可省略端口,服务启动时会按 `FLORI_MCP_PUBLIC_PORT` / `MCP_PORT` 补齐带端口的 Host 与 Origin。
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

**接入信息端点**(供设置页「接入 MCP」卡片渲染;只读,挂 /api 经 Caddy basic_auth / API_ALLOW_NO_AUTH 收口):
- `GET /api/mcp/info` → `{enabled, http_path:"/mcp", local_url:"http://127.0.0.1:<FLORI_MCP_PUBLIC_PORT>/mcp", token_configured:bool, tools:[{name, description}], stats:{total:int, by_tool:{name:int}}}`。`FLORI_MCP_PUBLIC_PORT` 未设时回退 `MCP_PORT` / 8090。tools 从 MCP server 实时派生(不写死);不回传 token 明文。**接入统一走 HTTP(streamable-http + Bearer),不再用 stdio**:前端本地端点 = `local_url`(同机直连 mcp-http)、公网端点 = `window.location.origin + http_path`,仅 URL 不同。<!-- contract: stats = MCP 工具调用计数 -->`stats` 是 MCP 工具调用计数(MCP-http 进程 best-effort 写 redis:总计 `mcp:calls:total` + 按工具 `mcp:calls:tool:{name}`;API 只读透出),redis 不可用 → `{total:0, by_tool:{}}`(不报错)。
- `GET /api/mcp/token` → `{token: string|null}`。前端默认遮掩 token、点击「显示/复制」时才取(明文经此端点;LAN :8080 无鉴权,注意)。

### 工具(7,只读)
- **`list_knowledge_bases()`** → `[{domain, collection_count, job_count, concept_count, subscription_count, last_active_at}]`
  —— agent 探索起点。
- **`search(query, domain?=null, limit?=10)`** → `[{title, snippet, job_id, domain, kind, canonical_evidence}]`
  —— 全文检索(2 字 CJK 参数化子串 + FTS5 trigram;单字不命中);`snippet` 内 `<mark>` 包裹命中;`domain` 限定某库;
  `kind`=note_type；`canonical_evidence` 是与 REST Search/Ask 同 identity/status 的稳定数组。先 search 再 get_note。
- **`get_note(job_id)`** → `{job_id, title, domain, collection_id, content_type, status, note_file, markdown, canonical_evidence}`
  —— 取最新版智能笔记完整 Markdown;`markdown=null` 表示该内容智能笔记未生成。job 不存在→错误。
  `canonical_evidence` 与 REST job evidence 端点共用当前 ID 和 resolver 安全投影，无 provenance 时为空数组。
- **`list_collections(domain?=null)`** → `[{id, name, domain, job_count, [source_type, source_id, last_synced_at, last_sync_status]}]`
  —— 集合(内容分组/订阅来源)清单;`domain` 可选限定;订阅集合才带 source 字段。
- **`get_glossary(domain, status?=null)`** → `[{term, zh_name, definition, status, is_topic, occurrence_count}]`
  —— 某库概念/术语表;`status` 可选(accepted/review)。单条详情用 get_term。
- **`get_term(domain, term)`** → `ConceptTermDetailResponse | null`
  —— 与 REST `GET /api/glossary/{domain}/{term}` 共用同一详情投影，包含 current/history、精确 attestation 与有界 totals；stale/missing evidence 不返回可用 locator/link。未命中 null。
- **`concept_timeline(domain, granularity?=month)`** → `{domain, granularity, ...buckets}`
  —— 概念按源内容发布时间分桶计数;`granularity`=day|week|month。
- **`concept_graph(domain)`** → `{nodes:[{id,term,zh_name,definition,status,is_topic,occurrence_count}], edges:[{source,target,weight,kind}], stats:{node_count,edge_count,typed_edge_count,isolated_count}}`
  —— 概念网络:related 真边(kind=rel)+ 共现降噪边(kind='cooccur',仅共享 job 数≥2);孤立概念仍作节点。等价于 REST `GET /api/domains/{domain}/concept-graph`。

### 迭代约定(新增工具)
service 函数(单一来源)→ `@mcp.tool()` 薄包(写好面向 LLM 的 docstring)→ pytest 集成(进 CI)→ 本节同提交更新(`contract:`)→
Inspector 眼检 → 版本 +1。工具少而精;签名**只增可选参数**保向后兼容(它是 agent 的公开契约)。
