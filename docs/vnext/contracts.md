# vNext 契约 v1

状态：已冻结。实现发现缺口时先修改本文件，不在代码里增加影子字段、旧协议兼容或第二套状态。

## 通用表示

- 契约修订固定为 `flori.v1`。HTTP 和 Runner 请求必须带 `X-Flori-Protocol: 1`；缺失或其它值返回 `protocol_mismatch`，不协商版本。
- 所有业务 ID 是小写规范形式 UUIDv7。只有 SQLite 自增事件序号不是 UUID。
- 时间点是 UTC Unix 毫秒整数，时间段是非负毫秒整数，金额和 credits 使用整数微单位。
- SHA-256 是 64 位小写十六进制；文件大小使用非负整数 byte。
- API、Pipeline 和 manifest 输入拒绝未知字段。可选字段缺失与显式 `null` 不混用。
- JSON 只允许由封闭 Rust 类型产生的规范序列化；不得以 `Value` 或任意字段袋进入 domain/contract。

## 核心 ID 与关系

| 实体 | ID | 不变量 |
|---|---|---|
| Pipeline | `pipeline_id` | 稳定名称；`current_revision_id` 只指向该 Pipeline 最新导入修订 |
| PipelineRevision | `pipeline_revision_id` | 一份不可变 YAML、编译器版本和摘要 |
| Source | `source_id` | 一个规范化内容或频道来源；订阅开关、fanout 和成员关系属于 Collection；同 `kind + canonical_ref` 唯一 |
| Job | `job_id` | 一次完整 DAG 执行；初次投递和任何重跑都创建新 ID |
| Task | `task_id` | Job 中一个 `task_key` 的节点；同 Job 内 key 唯一 |
| Attempt | `attempt_id` | 一次 Task 执行，同时是 `exec_id` 和 lease fence，不再建立 Lease 实体 |
| Artifact | `artifact_id` | 一个已声明、已校验和已提交的不可变输出 |
| Runner | `runner_id` | 一个可吊销 token 对应的执行实例；显示名唯一 |
| PromptSnapshot | `prompt_snapshot_id` | Job 创建时固化的全部 Prompt 输入，嵌在 Job 行中保存 |

`Source.current_job_id` 和 `previous_job_id` 只指向同一 Source 的成功发布 Job，且两个值不能相同。新 Job 成功后在一个事务内执行：旧 `current` 变 `previous`，新 Job 变 `current`，更老成功 Job 不再是发布成果。失败或取消 Job 不改变两个指针。

## 封闭枚举

```text
SourceKind  = arxiv | pdf_url | pdf_upload
            | bilibili_video | bilibili_channel
            | youtube_video | youtube_channel | local_video

JobTrigger = initial | pipeline_rerun | task_rerun | subscription
JobState   = queued | running | succeeded | failed | canceled
TaskState  = pending | ready | leased | succeeded | failed | canceled | skipped
AttemptState = leased | succeeded | failed | expired | canceled
RunnerState  = enabled | disabled
CredentialKind = bilibili_cookie | youtube_cookie
AiTool = qoder_cli | codex_cli
UsageOrigin = observed | estimated | unavailable
```

`online` 不是 Runner 状态，只由 `last_seen_at_ms` 与服务端阈值计算。不得增加 `unknown`、`legacy`、`recovering` 或 Provider 状态。

Artifact kind 首版只有：

```text
source_original | document_structure | figure | table_region | translation
subtitle | transcript | keyframe | danmaku | parts_manifest | subscription_manifest
mechanical_note | smart_note | summary | terms | evidence
task_log | ai_audit
```

Table 只使用 `table_region`，不增加 cell/row/column Artifact 或数据库模型。

## 状态机

### Job

```text
queued  -> running | failed | canceled
running -> succeeded | failed | canceled
```

### Task

```text
pending -> ready | skipped | canceled
ready   -> leased | skipped | canceled
leased  -> ready | succeeded | failed | canceled
```

`leased -> ready` 只发生于 Attempt 失败或过期且仍有自动 retry。Task 进入终态后不再变化。

### Attempt

```text
leased -> succeeded | failed | expired | canceled
```

所有终态不可离开。任何状态写入必须同时检查旧状态；受 lease 保护的写入还必须满足父 Job仍为 `running`、`tasks.current_attempt_id = attempt_id` 且 lease 未过期。唯一不改变业务状态的例外是既有 usage started 行补 final。

## SQLite v1

SQLite 开启 foreign keys 和 WAL；Home Core 是唯一 writer。所有外键默认立即约束，只有 Pipeline/Source 的当前指针使用 deferred 外键解决同事务内轮换。

允许的表只有下列 24 个。字段名是实现约束；实现不得自行增加 `metadata_json`、`extra` 或兼容列。

| 表 | 字段和约束 |
|---|---|
| `schema_meta` | `version` PK、`contract_revision`、`applied_at_ms`；v1 恰有一行 |
| `pipelines` | `id` PK、`key` UNIQUE、`current_revision_id`、`created_at_ms` |
| `pipeline_revisions` | `id` PK、`pipeline_id` FK CASCADE、`compiler_version` CHECK 1、`git_commit`、`yaml_sha256`、`yaml_text`、`created_at_ms`；UNIQUE(`pipeline_id`,`yaml_sha256`) |
| `sources` | `id` PK、`kind`、`canonical_ref`、`title`、`domain_id` FK RESTRICT NOT NULL、`credential_id` FK SET NULL、`current_job_id`、`previous_job_id`、`request_key` UNIQUE、`request_sha256`、`created_at_ms`、`updated_at_ms`；UNIQUE(`kind`,`canonical_ref`) |
| `source_inputs` | `id` PK、`source_id` FK CASCADE、`name`、`media_type`、`size_bytes`、`sha256`、`relative_path`、`created_at_ms`；UNIQUE(`source_id`,`name`) |
| `jobs` | `id` PK、`source_id` FK CASCADE、`pipeline_revision_id` FK、`trigger`、`rerun_of_job_id` FK SET NULL、`rerun_from_task_key`、`state`、`prompt_snapshot_id` UNIQUE、`prompt_snapshot_sha256`、`prompt_snapshot_json`、`request_key` UNIQUE、`request_sha256`、`created_at_ms`、`started_at_ms`、`finished_at_ms`、`error_code`、`error_message` |
| `tasks` | `id` PK、`job_id` FK CASCADE、`task_key`、`executor`、`spec_json`、`input_bindings_json`、`state`、`pinned_runner_id` FK、`selected_model`、`selected_effort`、`runner_config_revision`、`attempt_limit`、`timeout_ms`、`current_attempt_id` FK SET NULL deferred、`ready_at_ms`、`started_at_ms`、`finished_at_ms`、`error_code`、`error_message`；UNIQUE(`job_id`,`task_key`) |
| `attempts` | `id` PK、`task_id` FK CASCADE、`attempt_no`、`runner_id` FK nullable、`state`、`model`、`effort`、`runner_config_revision`、`lease_expires_at_ms`、`last_log_sequence`、`started_at_ms`、`finished_at_ms`、`error_code`、`error_message`；UNIQUE(`task_id`,`attempt_no`)，runner_id只允许 `core.*` Attempt为空 |
| `uploads` | `id` PK、`owner_kind` CHECK `source`/`attempt`/`materialize`、`owner_id`、`request_key`、`request_sha256`、`commit_json`、`name`、`target_id`、`source_artifact_id` FK SET NULL、`staging_path`、`final_relative_path`、`expected_size_bytes`、`expected_sha256`、`received_bytes`、`state` CHECK `receiving`/`verified`/`moved`、`created_at_ms`、`updated_at_ms`; UNIQUE(`owner_kind`,`owner_id`,`name`)，source upload和每组materialize第一行另有非空 `request_key` UNIQUE partial index |
| `artifacts` | `id` PK、`source_id` FK CASCADE、`job_id` FK CASCADE、`task_id` FK CASCADE、`attempt_id` FK nullable、`origin` CHECK `produced`/`materialized`、`materialized_from_artifact_id` FK SET NULL、`name`、`kind`、`media_type`、`file_name`、`size_bytes`、`sha256`、`relative_path`、`retention` CHECK `source`/`published`/`failed_audit`、`created_at_ms`; produced要求attempt_id，materialized要求attempt_id为空；produced UNIQUE(`attempt_id`,`name`)，materialized partial UNIQUE(`job_id`,`task_id`,`name`) |
| `runners` | `id` PK、`name` UNIQUE、`state`、`token_digest` UNIQUE nullable、`registration_token_digest` UNIQUE nullable、`registration_expires_at_ms`、`config_revision`、`max_concurrency`、`tags_json`、`tools_json`、`ai_models_json`、`default_model`、`default_effort`、`last_seen_at_ms`、`created_at_ms`、`updated_at_ms` |
| `credentials` | `id` PK、`kind`、`name` UNIQUE、`plaintext_value`、`created_at_ms`、`updated_at_ms` |
| `prompts` | `key` PK、`content`、`sha256`、`updated_at_ms` |
| `ai_usage` | `id` PK、`job_id` FK CASCADE、`task_id` FK CASCADE、`attempt_id` FK CASCADE、`invocation_key`、`state` CHECK `started`/`final`、`tool`、`model`、`effort`、`origin`、`input_tokens`、`output_tokens`、`cost_micros`、`credits_micros`、`created_at_ms`、`finalized_at_ms`; UNIQUE(`attempt_id`,`invocation_key`) |
| `job_events` | `id` INTEGER PK AUTOINCREMENT、`scope` CHECK `system`/`source`/`job`/`runner`、`scope_id`、`kind`、`payload_json`、`created_at_ms` |
| `domains` | `id` PK、`slug` UNIQUE、`name`、`description`、`profile_text`、`created_at_ms`、`updated_at_ms` |
| `collections` | `id` PK、`domain_id` FK RESTRICT NOT NULL、`name`、`kind` CHECK `manual`/`subscription`、`subscription_source_id` FK SET NULL、`enabled`、`fanout_limit`、`last_synced_at_ms`、`last_sync_error`、`created_at_ms`、`updated_at_ms`; UNIQUE(`domain_id`,`name`)，并CHECK manual无订阅字段、subscription引用同Domain频道Source且fanout为1..100 |
| `collection_sources` | `collection_id` FK CASCADE、`source_id` FK CASCADE、`added_at_ms`; PK(`collection_id`,`source_id`) |
| `glossary_terms` | `id` PK、`domain_id` FK CASCADE NOT NULL、`term`、`normalized_term`、`explanation`、`state` CHECK `active`/`hidden`、`created_at_ms`、`updated_at_ms`; UNIQUE(`domain_id`,`normalized_term`) |
| `concept_occurrences` | `id` PK、`term_id` FK CASCADE、`source_id` FK CASCADE、`job_id` FK CASCADE、`evidence_id` FK CASCADE、`source_order`、`created_at_ms` |
| `concept_edges` | `from_term_id` FK CASCADE、`to_term_id` FK CASCADE、`relation`、`job_id` FK CASCADE、`evidence_id` FK CASCADE、`weight`; PK(`from_term_id`,`to_term_id`,`relation`,`job_id`,`evidence_id`) |
| `evidence` | `id` PK、`source_id` FK CASCADE、`job_id` FK CASCADE、`artifact_id` FK CASCADE、`locator_kind` CHECK `pdf`/`video`、`page`、`x1`、`y1`、`x2`、`y2`、`start_ms`、`end_ms`、`keyframe_artifact_id`、`quote`; CHECK locator 所需字段完整且范围有效 |
| `search_chunks` | FTS5 trigram：`chunk_id` UNINDEXED、`source_id` UNINDEXED、`job_id` UNINDEXED、`artifact_id` UNINDEXED、`title`、`body`; 只索引 current Job，少于3字符的规范化查询对 current chunk使用受限 LIKE |
| `search_chunk_evidence` | `chunk_id`、`evidence_id` FK CASCADE；PK(`chunk_id`,`evidence_id`) |

这些 `*_json` 列只保存以下正式类型：`PromptSnapshot`、`CompiledTaskSpec`、`TaskInputBindings`、`RunnerTags/Tools/AiModels`、`TaskLogEvent`、`PendingSourceCommit`、`PendingAttemptUpload`、`PendingMaterializeCommit`。`TaskInputBindings` 是创建 Job 时固化的符号引用；Runner claim 中的 `ResolvedTaskInputs` 是前序 Artifact 就绪后生成的具体输入，两者不得混用。读取失败或引用语义不合法即 `corrupt_state` 并停止相关写入，不做宽松解析。

Source必须属于一个Domain。Source只能加入同Domain的Collection；subscription Source还必须是该Domain的 `bilibili_channel` 或 `youtube_channel`。这些跨行不变量在同一写事务检查。

### schema 变化

- Python 数据库不是 v1 的前置版本，不导入。
- 二进制只接受当前 schema，或执行随同一镜像提供的明确前向 migration；migration 完成前不开 API 和 Runner poll。
- schema 变化前停止 writer，保留旧 SQLite 文件和旧镜像。migration 任一步失败即进程退出，由运维换回二者。
- 不双写、不读旧列、不保留 alias，也不为普通镜像升级复制数据库。

## PromptSnapshot

`prompts` 只保存 UI 当前值，不维护在线激活和无限版本历史。创建 Job 时：

1. Pipeline 编译结果列出实际引用的 Prompt key，并取得 Source 所属 Domain 的 `profile_text`。
2. 在同一事务读取内容，按 key 排序形成 prompts列表，同时固化 `{domain_id, profile_text, sha256}`。
3. 生成 `prompt_snapshot_id` 和整体 SHA-256，写入 Job。
4. Task 只引用该快照；之后修改 Prompt 或 Domain profile 不影响已排队 Job。Glossary v1不自动注入AI上下文。

## Artifact 与文件契约

NAS 根只使用服务端生成的相对路径：

```text
sources/<source_id>/inputs/<source_input_id>/<file_name>
sources/<source_id>/retained/<artifact_id>/<file_name>
sources/<source_id>/jobs/<job_id>/tasks/<task_id>/<artifact_id>/<file_name>
.staging/uploads/<upload_id>
.trash/sources/<source_id>
.trash/jobs/<job_id>
```

Runner 不能提交路径。它只提交声明名、media type、size 和 SHA-256；Server 从 Pipeline 声明决定 kind、文件名上限、相对路径和保留策略。拒绝绝对路径、`..`、隐藏段、分隔符、符号链接、摘要不符、超限和未声明输出。

Server 校验上传后生成严格 manifest：

```json
{
  "schema": "flori.artifact.v1",
  "job_id": "uuidv7",
  "task_id": "uuidv7",
  "exec_id": "uuidv7",
  "artifacts": [
    {
      "name": "smart_note",
      "kind": "smart_note",
      "media_type": "text/markdown",
      "size_bytes": 123,
      "sha256": "64 lowercase hex",
      "relative_path": "server generated"
    }
  ]
}
```

manifest 是服务端控制文件，不是额外 Artifact kind。Server 在 upload 开始时生成 `target_id` 和最终路径。上传先写 `.staging`；同文件系统 rename 后把ledger置 `moved`，SQLite 成功事务再插入 Artifact、删除 upload行、完成Task并推进DAG。

启动恢复按 upload行执行唯一三态算法：`receiving/verified` 应只有 staging；`moved` 应只有 final且还没有目标数据库行；upload行已删除则必须已有目标行。rename后、state更新前崩溃允许 `verified + only final`，校验摘要后收敛为moved。业务事务失败后保持 `moved + only final` 等待幂等重试，不反向rename。

有效owner从现有状态续传或继续提交。Attempt已fence、source/materialize请求已取消或超过清理TTL即为失效owner：先删除现有staging/final，再删除ledger；若在两步之间崩溃，重启遇到“失效owner + 无文件”直接删除ledger。只有有效owner出现无文件、两份文件或不匹配摘要才是 `corrupt_state`。所有路径来自ledger，不扫描猜测。

过期 Attempt 的 upload、log、completion 和新 usage start 全部拒绝。唯一例外是该 Attempt 已存在的 usage started 行可由原 Runner补一次 final；它不能改变 Task状态或创建新计费项。

浏览器上传 PDF/本地视频也使用 `uploads`，`owner_kind=source`，`owner_id` 是预生成的 Source ID。第一行同时冻结 `request_sha256` 和强类型 `PendingSourceCommit`；重试先比较摘要。文件校验并移到最终路径后，一个事务插入 Source/SourceInput、复制request摘要并删除ledger；失败保持moved等待重试。同 request key只返回原目标，不复制输入。

`from_task` 复用不建立跨 Job保留依赖，并固定三阶段：

1. 第一写事务只预生成Job/Task/Artifact ID、验证来源并写 `owner_kind=materialize` ledger，不插入Job或Task。第一行冻结 `request_sha256` 和包含PipelineRevision、PromptSnapshot、rules、Runner选择及全部生成ID的 `PendingMaterializeCommit`；`source_artifact_id` 临时阻止janitor清理来源。
2. 用 reflink、只读hardlink或copy把每个上游 `retention=published` Artifact物化到目标路径并复核SHA-256。此时没有ready Task可被认领。
3. 最终单事务插入Job、Task、`origin=materialized` Artifact和bindings，复制request摘要并删除全部ledger。事务提交后Job才可见。

`retention=source` 输入可直接绑定，因为其寿命本来就是整个 Source。新 Job的 `input_bindings_json` 只能指向自身 Artifact或同Source的 `retention=source` Artifact。重试必须先比较ledger request摘要，再从冻结commit继续。

所有需要持久日志的 Runner Task 都声明 `task_log`；AI Task 还声明 `ai_audit`。未声明文件只存在于 Attempt 临时目录，结束后删除，UI 不提供访问。`core.*` 的状态变化进入 `job_events`，不伪造 Runner 日志 Artifact。

### Artifact 内容类型

Rust core 为下列结构定义唯一 Serde 类型；JSON都带精确 `schema=flori.<name>.v1`，未知字段拒绝。实现不得从 `tests/fixtures` 复制 DTO。

| kind | 必填内容与不变量 |
|---|---|
| `document_structure` | `language`、pages尺寸、ordered sections、figures和tables；Figure含ID/page/bbox/caption/Artifact逻辑名，Table含ID/page/bbox/caption/text/截图逻辑名，不含cells |
| `parts_manifest` | 一个逻辑视频的ordered parts；每项含index/title/duration和原视频、字幕、弹幕Artifact逻辑名，引用可缺但不能指向未声明输出 |
| `subscription_manifest` | newest-first items；每项只有平台视频kind、canonical_ref、title、published_at_ms，去重且数量不超过Collection fanout |
| `transcript` | `language`、`duration_ms`、ordered cues `{start_ms,end_ms,text}`；区间有效且不重叠 |
| `terms` | ordered `{term,explanation,evidence_ids}`；term规范化后唯一，解释非空 |
| `evidence` | ordered canonical evidence；字段与本文件 locator规则一致，ID唯一且只能引用同Source本Job可见Artifact |
| `ai_audit` | tool/model/effort、PromptSnapshot摘要、脱敏参数、websearch URL、usage keys、退出状态和输出摘要；不含prompt全文或secret |
| `task_log` | UTF-8 NDJSON；每行是 `{timestamp_ms,level,message}`，level只有debug/info/warn/error，无任意上下文字段 |

`mechanical_note`、`smart_note`、`translation` 和 `summary` 是 UTF-8 Markdown。机械笔记只重组字幕事实并带视频时间范围；智能笔记必须分开“来源事实”和“AI分析”，事实、摘要和关键名词用 evidence ID关联。Figure/Table/keyframe等二进制元数据只在上述结构JSON中维护。

### 保留

- `source_original`、acquire得到的 `subtitle`/`danmaku` 和 `source_inputs` 位于 `retained`/`inputs`，保留到删除整个 Source。
- 完整成功成果只展示并保留 current、previous。
- 失败详情只保留最近一个失败 Job 的 `task_log` 和 `ai_audit`。
- 更早 Job 保留 SQLite 状态、错误和可聚合 `ai_usage`；无活跃materialize ledger引用后，其它文件与 Artifact 行按 Job 目录原子移入 `.trash/jobs` 后清理。
- `.trash/jobs` 只移动非 `retention=source` 的 Job目录。启动恢复只检查 `relative_path` 实际位于该Job目录的Artifact行：仍有行则rename回原处，否则删除trash；位于 `retained/` 的同Job Artifact不参与判定。

## canonical evidence 和发布

- PDF locator 使用 1-based page 与左上角原点 point 坐标，满足 `0 <= x1 < x2 <= width`、`0 <= y1 < y2 <= height`。
- 视频 locator 满足 `0 <= start_ms < end_ms <= duration_ms`；关键帧时间落在区间或距离边界不超过一帧。
- quote 必须能在对应结构化文本或字幕中规范化匹配。
- `core.validate` 校验全部笔记引用后，唯一 sink `core.publish` 才可执行。publish用一个CAS事务完成自身 Task、Job、current/previous、FTS、Glossary occurrence 和 Concept 投影；事务开始时其它Task必须全部 `succeeded`/`skipped`，否则拒绝。
- FTS、Concept graph、timeline 和 topic 都只读 current；它们可从 current Artifact 重建，不成为第三份成果真相。

## AI usage 幂等

每个 AI executor 为每次真实 CLI 调用生成稳定 `invocation_key`。调用 CLI 前先幂等写入 `state=started`；拿到结果后只允许一次 `started -> final` 并写实际指标。写入必须满足 UNIQUE(`attempt_id`,`invocation_key`)：

- 重复 started 或完全相同的 final 返回已有记录。
- final 指标冲突、final 回退 started 或第二次改变 final 返回 `usage_conflict`，不得累加。
- Attempt 成功事务必须确认每个 started 调用都已 final；CLI不报告指标时也以 `origin=unavailable` final。
- Runner 崩溃留下的 started 行保留为“可能已产生费用”的审计，AI Task默认不自动 retry。
- UI 按 Runner、`tool`、model、effort 和时间聚合；契约中没有 Provider 字段。
- 删除 Source 时随 Job cascade 删除详细 ledger，不留下悬空费用记录。

## 日志与 SSE

Runner 对每个 Attempt 从 sequence 1 连续上传 UTF-8 NDJSON 帧。重复 sequence 且内容摘要相同是幂等成功；相同 sequence 不同内容返回 `log_sequence_conflict`；跳号返回 `log_sequence_gap`。服务端写 staging 日志并更新 `last_log_sequence`，终态时按声明提交 `task_log`。

SSE 是单向提示，不是状态真相：

```text
GET /api/v1/events?after=<event_id>
GET /api/v1/jobs/<job_id>/events?after=<event_id>
```

事件 union 仅有：

```text
source_changed     {source_id}
job_state          {job_id,state,error_code?}
task_state         {job_id,task_id,state,attempt_id?,error_code?}
artifact_committed {job_id,task_id,artifact_id,kind}
log_cursor         {job_id,task_id,attempt_id,last_sequence}
runner_changed     {runner_id,state,online,config_revision}
system_health      {status:healthy|degraded,queue_depth,disk_free_bytes}
```

SSE `event` 等于kind，`id` 使用 `job_events.id`，`data` 是对应严格结构。客户端断线后用 `Last-Event-ID` 或 `after` 续读，仍须通过普通 GET 获取完整状态。删除 Source 时对应事件按 scope 一起删除。

Job事件只保留 current、previous 和最近一个失败 Job；Runner/system事件最多各保留最近10000条。cursor 已被清理时返回 `event_cursor_expired`，客户端做一次完整 GET 后从最新 cursor 继续。

## Public API v1

Rust 类型生成唯一 OpenAPI；前端不得重写下表 DTO。会产生 Source 或 Job 的命令带调用方生成的 `request_key`。Server对规范化请求计算 `request_sha256`；上传Source的摘要还包含文件SHA-256。命中同 key时先比较摘要：相同返回原结果，不同返回 `idempotency_conflict`。

| 方法和路径 | 输入或结果 |
|---|---|
| `POST /api/v1/sources` | `CreateRemoteSource {request_key, kind, canonical_ref, title?, domain_id, collection_ids, credential_id?}`；Domain必填 |
| `POST /api/v1/sources/uploads` | multipart：严格 `CreateUploadSource {request_key,kind,title?,domain_id,collection_ids,file_sha256}` JSON + 单一 `file`；仅 `pdf_upload`/`local_video` |
| `GET /api/v1/sources[/{id}]` | Source 列表/详情、current/previous 和最近 Job |
| `DELETE /api/v1/sources/{id}` | 无 body；唯一删除入口，目标已不存在也返回成功 |
| `POST /api/v1/sources/{id}/jobs` | `CreateJob {request_key, pipeline_id, inputs:{translate}}` |
| `GET /api/v1/jobs[/{id}]` | 过滤列表或完整 DAG、Attempt、声明 Artifact |
| `POST /api/v1/jobs/{id}/rerun` | `RerunJob {request_key, mode, from_task_key?, ai_selection?}`；总是返回新 `job_id` |
| `POST /api/v1/jobs/{id}/cancel` | 无 body；相同取消可重复，不删除历史 |
| `GET /api/v1/artifacts/{id}` | 元数据；`/content` 流式读取，支持 Range |
| `GET /api/v1/events` | 全局 SSE；Job 还有同名子资源 |
| `GET/POST /api/v1/domains`、`GET/PUT/DELETE /api/v1/domains/{id}` | Domain 和 `profile_text` 的强类型 CRUD；非空 Domain 拒绝删除 |
| `GET/POST /api/v1/collections`、`GET/PUT/DELETE /api/v1/collections/{id}` | 手工分组或订阅；`POST /{id}/sync` 在 `subscription_source_id` 创建 Job |
| `GET/PUT/DELETE /api/v1/prompts/{key}` | 读取、整体替换或删除当前 Prompt；已有 Snapshot 不变 |
| `GET/POST /api/v1/glossary`、`GET/PUT/DELETE /api/v1/glossary/{id}` | active/hidden 术语与解释，不含锁定/接受/拒绝流 |
| `GET /api/v1/concepts` | current 投影的 graph、timeline、topic 查询 |
| `GET /api/v1/search?q=` | FTS current 命中，返回 Source/Job/Artifact 和 evidence IDs |
| `GET /api/v1/runners[/{id}]` | Runner、在线推导、并发、tags、tools、models/efforts |
| `PUT /api/v1/runners/{id}/config` | 整体替换 enabled、并发、默认 model/effort；revision 加一 |
| `POST /api/v1/runners/registration-tokens` | `CreateRunnerSlot {name,tags,max_concurrency,default_model?,default_effort?}`；创建 disabled Runner并只返回一次注册 token |
| `POST /api/v1/runners/{id}/registration-token` | 禁用并fence该Runner的活跃Attempt，吊销长期token，返回一次性重新注册token |
| `GET/POST /api/v1/credentials`、`PUT/DELETE /api/v1/credentials/{id}` | cookie 元数据与整体替换；任何响应都不返回明文 |
| `POST /api/v1/credentials/bilibili-qr`、`POST /api/v1/credentials/bilibili-qr/{session}/poll` | Home Core 短期内存 QR 会话；确认后直接创建 cookie，重启只需重新扫码 |
| `GET /api/v1/usage` | 按 Runner/tool/model/effort/时间聚合 |
| `GET /api/v1/system` | Home Core、队列、磁盘、Runner 与 AI 用量摘要 |

`RerunJob.mode` 只有 `pipeline` 和 `from_task`。`ai_selection` 为 `{task_key, runner_id, model, effort, runner_config_revision}`；只能选择 Runner 当前已上报的组合。创建新 Job 时固化这些值，之后 Runner 配置变化不修改排队 Job。

MCP 合入 `flori-server` 的 `/mcp`，使用独立 bearer token，只提供 current FTS 搜索、Source/Artifact 读取和 canonical evidence 定位。没有写工具，也不读取 previous 或失败 Job。

## 凭据

- Cookie 由 Home Core SQLite 明文保存，这是用户批准的单用户取舍；数据库文件、NAS 根和容器挂载必须只对服务账号可读。
- API 永不返回 `plaintext_value`，只允许创建、整体替换和删除。
- Source 通过 `credential_id` 选择 cookie。认领下载 Task 时，服务端把实际值放入一次性 claim 响应的 `secret_inputs`；本地和远程 Runner 完全走同一 HTTPS 路径。
- Task 持久 `spec_json` 只保存 credential ID，不保存值。secret 不得进入日志、错误、Artifact、manifest、SSE 或 AI 输入。
- Bilibili QR 会话不进入业务表、不形成 Job或 Artifact；Home Core 代理官方 QR start/poll，确认后直接写 cookie。会话过期或重启就重新开始。
- Runner 长期 token 只返回一次，SQLite 仅保存摘要；一次性注册 token 短期有效且使用后立即失效。
- QoderCLI/CodexCLI 登录态只存在于对应 Runner 的本地 Docker secret或只读挂载，不上传 Home Core，也不出现在 claim。

## 下载与输入安全

- 通用 PDF 下载只接受 `http`/`https`，每次解析和重定向都拒绝 loopback、private、link-local、multicast 和保留地址；连接使用已校验地址，最多5次重定向。
- Bilibili/YouTube executor 只接受规范化平台 ID生成的 URL；CLI 使用参数数组，不拼 shell命令。
- 下载和上传都流式执行，边读边限制部署配置中的最大 byte、超时和摘要；声明 media type 与 magic不符即失败。
- PDF 在 extractor 前探测页数和文本层。若每一页去空白后都少于32个 Unicode字符，返回 `unsupported_scanned_pdf`；不调用 OCR。
- Archive、HTML、audio 和未列入 SourceKind 的输入在创建 Source 时拒绝，不进入 Pipeline。

## 删除 Source

唯一删除算法：

1. 开启 `BEGIN IMMEDIATE`，重新检查 Source 存在、没有 queued/running Job，也没有引用该Source Artifact的materialize upload；否则回滚并返回 `source_busy`。
2. 保持该写事务，同文件系统把 `sources/<source_id>` rename 到 `.trash/sources/<source_id>`。只有数据库没有任何input/Artifact行时，缺失目录才按空目录处理；其它I/O错误回滚并fail-closed。
3. 在同一事务删除 Source；FK 与显式 scope 清理删除全部 Job、Task、Attempt、input、Artifact、usage、event、FTS、evidence、Collection membership 和 Concept 投影。引用它的 subscription Collection 同时清空 `subscription_source_id` 并禁用。
4. 提交成功后删除 trash 目录并确认数据库无 `source_id`、NAS 无源目录或 trash。所有Job创建也使用SQLite写事务并在插入前重新确认Source存在，因此不能穿过删除 fence。

进程崩溃后的唯一恢复规则：Source 行仍在则把 trash rename 回去并报告删除未完成；Source 行已无则完成 trash 删除。不得留下墓碑 Source、部分版本或孤立 usage。运行中的 Source 必须先取消 Job 并等 lease 失效，再重新删除。

## 错误码

错误响应固定为 `{error:{code,message,request_id,field?,retry_after_ms?}}`。首版 code：

```text
invalid_request | protocol_mismatch | not_found | conflict
idempotency_conflict | source_busy | unsupported_source | rerun_boundary_invalid
unsupported_scanned_pdf | pipeline_invalid | pipeline_cycle
runner_unavailable | runner_disabled | capability_mismatch
lease_expired | stale_attempt | task_canceled
attempt_timeout | runner_lost | network_temporary | upstream_rate_limited
tool_temporarily_unavailable | executor_failed
artifact_undeclared | artifact_invalid_path | artifact_too_large | digest_mismatch
log_sequence_gap | log_sequence_conflict | usage_conflict
evidence_invalid | credential_unavailable | storage_unavailable
event_cursor_expired | corrupt_state | schema_mismatch | internal
```

`message` 是可显示的短说明，不含 secret、命令原文或路径。调用方只按 code 分支，不解析 message。

自动 retry 只接受 `attempt_timeout`、`runner_lost`、`network_temporary`、`upstream_rate_limited` 和 `tool_temporarily_unavailable`。其它错误包括 `executor_failed` 都直接结束 Task；Runner 不能提交任意错误字符串绕过该集合。

## critical-target 验收矩阵

| 风险 | 必须拒绝 | 恢复/回滚 | WP04+ 可执行测试 |
|---|---|---|---|
| 旧/未知协议或字段 | 请求进入 domain 前拒绝 | 客户端升级到同版本 | 旧 header、额外字段、未知 enum 均 4xx |
| DAG 环、缺失 need、非法 Artifact 引用 | Job 创建失败 | 修正 Git YAML | 编译同一 YAML 两次同摘要；恶意图 fail-closed |
| 迟到 Runner 覆盖新 Attempt | 非 current 或过期 exec 的文件/状态写入全拒绝 | 新 Attempt 继续 | 过期后 upload/log/complete和新usage失败；只允许既有started usage final |
| 重复完成或计费 | 同摘要幂等，不同内容冲突 | 返回首次结果 | 并发重复 completion 与 usage 只产生一行 |
| 路径穿越、符号链接、超限 | Artifact 不进入最终根 | 清理对应 staging | `..`、绝对路径、symlink、错误摘要与超限矩阵 |
| 日志乱序或覆盖 | 跳号和异内容重号拒绝 | Runner 从服务端 cursor 续传 | crash/retry 后 sequence 连续且只一份 Artifact |
| 引用越界或伪 quote | publish 失败，current 不变 | 修复生成结果后新 Job | PDF bbox、视频时间、关键帧、quote 恶意样本 |
| Prompt/Runner 排队后变化 | 已建 Job 快照不变 | 新建重跑 Job采用新配置 | 修改 Prompt/model 后旧 Job仍用旧 snapshot |
| 删除竞态或中途崩溃 | active Source 拒绝删除 | 按 trash/SQLite 规则恢复 | rename 前后、事务前后逐点 kill，无孤儿 |
| schema migration 失败 | API/Runner poll 不启动 | 旧 SQLite + 旧镜像 | 每个 migration 崩溃点保持旧库可启动 |
| cookie/token 泄漏 | 输出渠道不得出现 secret | 吊销并替换 | 日志、错误、SSE、manifest、AI audit 扫描 |

本矩阵无 P0/P1 设计缺口后才能开始 Rust 业务实现。
