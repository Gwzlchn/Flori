# Pipeline 与 Runner 契约 v1

状态：已冻结。Pipeline 负责“做什么和依赖什么”，Runner 只负责“认领一个已编译 Task 并返回已声明结果”。

## Pipeline 文件

Pipeline 位于 Git 的 `pipelines/<pipeline-key>.yml`。文件名给出稳定 key；数据库 `pipeline_id` 是稳定 UUID，文件内容每次变化产生新的 `pipeline_revision_id`。Job 创建时只引用一个不可变 revision。

YAML 顶层只能是 Task map；Task key 必须匹配 `[a-z][a-z0-9_-]{0,47}`。禁止锚点、merge key、自定义 tag、重复 key和非 UTF-8 输入。

每个 Task 只允许八个字段：

```text
executor | with | needs | rules | tags | retry | timeout | artifacts
```

不支持 `script`、`image`、`services`、`stages`、`include`、`extends`、变量注入、shell 片段或插件字段。Pipeline YAML 只在 Git 修改，不提供在线编辑。

## 最小示例

```yaml
acquire:
  executor: document.acquire
  with:
    source: $source
  tags: [media]
  retry: 1
  timeout: 10m
  artifacts:
    - name: original
      kind: source_original
      path: output/source.pdf
      required: true
      when: on_success
      max_bytes: 104857600
    - name: log
      kind: task_log
      path: logs/task.ndjson
      required: true
      when: always
      max_bytes: 10485760

extract:
  executor: document.extract
  with:
    pdf: $needs.acquire.original
  needs: [acquire]
  tags: [media]
  retry: 1
  timeout: 20m
  artifacts:
    - name: structure
      kind: document_structure
      path: output/document.json
      required: true
      when: on_success
      max_bytes: 52428800
    - name: figures
      kind: figure
      path: output/figures/*
      required: false
      when: on_success
      max_files: 128
      max_bytes: 20971520
    - name: tables
      kind: table_region
      path: output/tables/*
      required: false
      when: on_success
      max_files: 128
      max_bytes: 20971520

translate:
  executor: ai.document_translate
  with:
    document: $needs.extract.structure
    prompt: $prompts.document_translate
  needs: [extract]
  rules:
    - if: $job.translate == true
  tags: [ai]
  retry: 0
  timeout: 30m
  artifacts:
    - name: translation
      kind: translation
      path: output/translation.md
      required: true
      when: on_success
      max_bytes: 52428800
    - name: audit
      kind: ai_audit
      path: logs/ai-audit.json
      required: true
      when: always
      max_bytes: 1048576

note:
  executor: ai.document_note
  with:
    document: $needs.extract.structure
    prompt: $prompts.document_note
  needs: [extract]
  tags: [ai]
  retry: 0
  timeout: 30m
  artifacts:
    - name: smart_note
      kind: smart_note
      path: output/smart-note.md
      required: true
      when: on_success
      max_bytes: 10485760

validate:
  executor: core.validate
  with:
    source: $needs.extract.structure
    notes: $needs.note
  needs: [extract, note]
  retry: 0
  timeout: 2m
  artifacts:
    - name: evidence
      kind: evidence
      path: output/evidence.json
      required: true
      when: on_success
      max_bytes: 10485760

publish:
  executor: core.publish
  with:
    validated: $needs.validate.evidence
  needs: [validate, translate]
  retry: 0
  timeout: 2m
  artifacts: []
```

示例省略部分 log、summary 和 terms 声明以突出语法；正式 Pipeline 必须满足下文 executor 输出要求。

## 八个字段

### executor

首版是封闭 enum：

| executor | 责任 | 允许的业务输出 kind |
|---|---|---|
| `document.acquire` | 安全下载 arXiv/PDF 或读取上传 PDF；扫描检测在此完成 | `source_original` |
| `document.extract` | 结构、章节、Figure/caption、Table 页面区域 | `document_structure`、`figure`、`table_region` |
| `ai.document_translate` | 可选全文翻译 | `translation` |
| `ai.document_note` | 中文智能笔记、摘要、关键名词 | `smart_note`、`summary`、`terms` |
| `video.acquire` | yt-dlp/yutto/本地输入、字幕、弹幕原文件、分P清单 | `source_original`、`subtitle`、`danmaku`、`parts_manifest` |
| `video.subscription` | 使用 yt-dlp/yutto 枚举频道最新条目，受 Collection fanout 限制 | `subscription_manifest` |
| `video.transcribe` | 平台字幕优先，Whisper 兜底并标准化时间段 | `transcript` |
| `video.frames` | 关键帧选择和内部简单去重 | `keyframe` |
| `video.mechanical_note` | 忠于字幕的易读机械笔记 | `mechanical_note` |
| `ai.video_note` | 分析型智能笔记、摘要、关键名词 | `smart_note`、`summary`、`terms` |
| `core.validate` | 校验 PDF/视频 locator、quote 和关键帧引用 | `evidence` |
| `core.publish` | 原子 current/previous、FTS 和知识投影 | 无业务 Artifact |

所有 Runner executor 还可输出已声明的 `task_log`；`ai.*` 还必须声明 `ai_audit`。`core.*` 由 Home Core 内部执行，不经过 poll，不使用 tags/model/effort，也不能访问 cookie。

不得把场景检测、断句、图片去重、分P合并或 OCR 建成 executor。它们分别是现有 media executor 的内部算法；视频 OCR 在 v1 不存在。

频道 Source 使用 `video.subscription -> core.publish`。publish 校验 `subscription_manifest` 后，按 Collection 的 `fanout_limit` 为未见过的视频创建或复用子 Source并创建 `trigger=subscription` Job；同 `kind + canonical_ref` 去重。Collection 只保存成员关系和最近同步结果，不建立第二套订阅队列。

### with

`with` 由 executor 对应的严格 Rust struct 解析，未知 key 失败。值只能是普通标量/列表或以下引用：

```text
$source                         当前 Source 的强类型描述
$job.translate                  创建 Job 时固化的 bool
$domain.profile                 PromptSnapshot 中固化的 Domain profile
$prompts.<key>                  PromptSnapshot 中的内容
$needs.<task>                   前序 Task 的完整 ArtifactSet
$needs.<task>.<artifact-name>   一个声明或声明组
```

引用必须来自 `needs`。循环、缺失 Task、缺失 Artifact、kind 不匹配或读取条件性 skipped 输出都返回 `pipeline_invalid`。条件 Task 的输出只有在消费 Task 使用完全相同的规范化 rules 时才能引用。

### needs

- 缺失等于空列表；元素唯一且必须是同文件已有 Task key。
- 编译器使用 `needs` 生成 DAG，禁止隐式顺序和 `stages`。
- 拓扑排序同级按 Task key 字典序，保证同一 YAML 产生同一摘要。
- 前序 `succeeded` 或 `skipped` 才满足依赖；`failed`/`canceled` 使尚未运行的后继 `canceled`。

### rules

缺失表示包含 Task。v1 只支持 GitLab `rules:if` 的极小子集：

```text
$source.kind == "<SourceKind>"
$source.kind != "<SourceKind>"
$job.translate == true|false
```

不支持 `&&`、`||`、括号、regex、文件变化、环境变量或 `when`。从上到下第一条为 true 即包含；无匹配则 Task 为 `skipped`。表达式在创建 Job 时只执行一次并写入编译结果。

### tags

- 缺失只允许 `core.*`；其它 executor 至少一个 tag。
- tag 匹配 `[a-z][a-z0-9_-]{0,31}`，全部必须由同一个 Runner 满足。
- `tags` 是调度条件，不是 Provider。推荐 `media`、`ai`、`qoder`、`codex`、`gpu` 等部署标签，但契约不硬编码注册表。

### retry

整数 `0..2`，表示首次 Attempt 之外最多增加几次自动 Attempt；数据库 `attempt_limit = retry + 1`。自动 retry 留在同一 Job/Task，只接受 [contracts.md](contracts.md) 冻结的五个 transient error code。AI executor 默认 0，避免隐式重复费用。

用户点击整条或单 Task 重跑永远创建新 Job，不使用此字段。

### timeout

必须是整数加 `s`、`m` 或 `h`，范围 1 秒到 24 小时。它限制单 Attempt；超时先 fence Attempt，再按 retry 决定 Task 回到 ready 或失败。

### artifacts

列表项只允许：

```text
name | kind | path | required | when | max_files | max_bytes
```

- `name` 在 Task 内唯一，匹配 Task key 同一字符规则。
- `kind` 必须在 executor 允许集合内。
- `path` 是相对执行目录的精确文件，或只在最后一段使用一个 `*`；不得使用 `..`、隐藏段或符号链接。
- 通配声明必须给 `max_files`，范围 1..256；精确文件不得给该字段。
- `max_bytes` 必填，适用于每个匹配文件。
- `required` 是 bool；required 声明零匹配时 Task不能成功，但不阻止失败/过期状态收敛。
- `when` 只有 `on_success` 和 `always`。业务输出只能 `on_success`；`task_log`、`ai_audit` 才可 `always`。

Runner 没有上传未声明文件的 API。匹配多个文件时，逻辑名是 `<name>/<basename>`，basename 必须唯一且通过同一安全校验。

## 编译与 PipelineRevision

Server 启动时读取 Git 文件并执行：

1. YAML 安全解析和八字段白名单。
2. executor-specific `with` 与 Artifact 声明验证。
3. rules 语法、引用、needs 和有向无环检查。
4. 对每种rules结果要求恰好一个 `core.publish`；它必须是唯一sink，且所有 included Task都是它的祖先。publish不得有下游、tags或retry。
5. 生成规范 `CompiledPipeline`，按 Task key、map key 和声明名排序。
6. 对规范 JSON 计算 SHA-256；同 Pipeline 同摘要复用 revision，否则插入新 revision 并更新 `current_revision_id`。

`compiler_version` 固定为 1；其它值一律 `pipeline_invalid`。历史 Job 直接读取已冻结的 Task 行，不重新编译 YAML，不执行或转换其它版本。

## Job 创建与重跑

### 初次与订阅

创建 Job 时在一个 SQLite 写事务重新确认 Source存在，并固化 request SHA、PipelineRevision、rules结果、PromptSnapshot、Task/needs、Job inputs和已指定的 AI Runner选择。事务完成前 Runner不可见任何Task；它与 `delete_source` 的 `BEGIN IMMEDIATE` fence不能穿越。

### 整条重跑

`mode=pipeline` 使用该 Pipeline 当前 revision 创建新 Job，所有包含 Task 从头执行。上传的 `source_inputs` 可读；URL/频道 acquire 重新执行。旧 Job ID、Task、Attempt 和 Artifact 均不修改。

### 从 Task 重跑

`mode=from_task` 使用当前 Pipeline revision：

1. 第一写事务验证边界、固化 request SHA 和完整 `PendingMaterializeCommit`，只创建 upload ledger；不插入 Job/Task，所以 Runner 和 UI 都看不到半成品。
2. 上游 `retention=published` Artifact 使用 reflink、只读 hardlink 或 copy 物化为新 Job自己的 Artifact ID和路径，并复核 SHA-256；`retention=source` 输入可直接复用。
3. 所有文件就绪后，最终单事务才插入 Job、上游 skipped Task、指定 Task及后继 pending/ready Task、物化 Artifact和bindings，并删除ledger。提交后 Job 才可见。
4. 只有 kind、media type、manifest schema 和当前 executor 输入完全匹配的上游 Artifact可复用；否则返回 `rerun_boundary_invalid`，用户必须选择更早 Task 或整条重跑。
5. `input_bindings_json` 只写新 Job Artifact或同Source的source-retention Artifact。新 Job不会引用可随更老 Job清理的字节，也不建立保留依赖图；成功后才成为 current，原 current顺移到previous。

物化期间重试必须以相同 `request_key + request_sha256` 恢复冻结的 commit；内容不同返回 `idempotency_conflict`。恢复、清理和崩溃窗口只按 [contracts.md](contracts.md) 的 upload 三态算法处理。

在智能笔记页选择另一 AI Runner，就是创建 `from_task` 新 Job并在对应 AI Task 写入：

```text
runner_id + model + effort + runner_config_revision
```

这些值在创建 Job 时固化。Runner 默认配置随后变化不影响该 Job；所选组合不再受支持时 Task 等待并显示 `capability_mismatch`，不换 Runner、不改 Provider、不自动 fallback。

普通 tag 调度不预选 Runner。认领事务把实际 `runner_id`、model、effort 和当时 `config_revision` 写入 Attempt。

## Runner 身份与注册

Runner 只能通过出站 HTTPS 访问 ECS 公网入口，和 Home Core/NAS 不共享路径或数据库。

1. 管理 UI 创建短期、单次注册 token。
2. 该操作先创建 disabled Runner slot并保存注册 token摘要；Runner 调用 `POST /runner/v1/register`，提交 `tools` 和 `ai_models` 实测能力。
3. Server 校验 slot 中的 name、tags、并发和默认 model/effort，返回 `runner_id` 与长期 bearer token各一次，清除注册 token并启用 Runner。
4. 每次重新注册或管理端整体更新配置，`config_revision` 加一。

注册 token 使用 `Authorization: Bearer <registration-token>`，不进入 JSON body。注册 body 只有严格的 `tools` 与 `ai_models`；成功响应只返回 `runner_id` 和长期 `token`。工具项固定为 `{tool,version}`，model项固定为 `{model,efforts[]}`；version、model和effort均使用下文同一受限标识符并在Server端去重、排序。

`tools` 只允许带版本的 `pdf_extractor`、`yt_dlp`、`yutto`、`ffmpeg`、`ffprobe`、`whisper_cpp`、`faster_whisper`、`qoder_cli`、`codex_cli`。`ai_models` 是严格的 `{model, efforts[]}` 列表；model 与 effort 是外部 CLI 报告的受限标识符，匹配 `[A-Za-z0-9._-]{1,64}`，Server只允许选择已上报的精确组合。

websearch 不在注册能力中。AI executor实际要求 websearch 时，Runner 在调用前探测 CLI；不可用则本 Attempt 明确失败，不伪造无搜索结果。

`ai_audit` 至少记录 tool、model、effort、PromptSnapshot摘要、脱敏参数、websearch是否启用及访问URL、usage invocation keys、退出状态和输出摘要。websearch URL是AI执行审计，不替代 PDF页或视频时间的 canonical evidence。

QoderCLI/CodexCLI 的锁版、镜像层缓存、本地 PC 代理和 GitHub 无代理边界见 [deployment.md](deployment.md)。

## Poll、lease 与 claim

Runner 使用 bearer token 长轮询：

```text
POST /runner/v1/poll
```

Server 在一个事务中选择 ready Task并创建 Attempt。条件是 Runner enabled、活跃 Attempt 小于并发数、全部 tags 匹配、executor 工具可用；AI Task还要求 model/effort组合可用。

认领同一事务把首个Task对应Job从queued改为running。每个Attempt API都重新检查父Job仍为running；父Job终态后只接受本契约规定的既有usage final。

claim 响应固定包含：

```text
job_id, task_id, task_key, exec_id, attempt_no
executor, timeout_ms, lease_expires_at_ms
resolved_inputs, output_declarations
model?, effort?, runner_config_revision
secret_inputs
```

`resolved_inputs` 的 Artifact 只给经过授权、短期有效的 HTTPS 下载 URL和摘要。`secret_inputs` 只在目标 download Task claim 中带 cookie 值，不进入任何持久 Task JSON。

若 Task声明 `task_log`，认领事务同时创建对应 upload ledger；logs endpoint只按sequence追加这个staging文件。Attempt终态时Server计算摘要并按 `when=always` 提交已有日志。Runner失联时允许提交已收到的部分日志，但缺失的required log/audit只阻止Task成功，不阻止Attempt过期或Job失败。

默认 lease 60 秒，Runner 每 20 秒调用续租。服务端可在部署配置中调节，但必须满足 renew 小于 lease 的一半。Task timeout 到达、Job取消、Runner禁用或 lease 到期后，`exec_id` 立即失效。

## Runner API v1

| 方法和路径 | 语义 |
|---|---|
| `POST /runner/v1/register` | 一次性 token 换 Runner ID与长期 token |
| `POST /runner/v1/poll` | 最多等待30秒，返回一个 claim 或 `204` |
| `POST /runner/v1/attempts/{exec_id}/renew` | CAS续租并返回新到期时间 |
| `POST /runner/v1/attempts/{exec_id}/logs` | 上传连续 NDJSON帧 `{sequence,sha256,line}` |
| `POST /runner/v1/attempts/{exec_id}/usage` | 幂等执行 usage `started` 或 `final` 转换 |
| `POST /runner/v1/attempts/{exec_id}/uploads` | 为一个声明逻辑名开始或恢复 upload |
| `PUT /runner/v1/uploads/{upload_id}` | 按精确 offset 流式追加；返回已接收 byte |
| `POST /runner/v1/uploads/{upload_id}/verify` | 校验 size/SHA，标记 verified |
| `POST /runner/v1/attempts/{exec_id}/complete` | 提交 manifest 摘要；原子完成 Task并推进 DAG |
| `POST /runner/v1/attempts/{exec_id}/fail` | 提交封闭错误码和已验证的 `when=always` 输出 |

HTTP wire 细节固定如下：

- `poll` 和 `renew` 没有 request body；没有Task时 `poll` 返回 `204`。
- `logs` 使用 `application/x-ndjson`，每行一个严格 `LogFrame`；响应 `{last_sequence}`。
- `uploads` 返回 `upload_id`、当前 `received_bytes` 和Server生成的完整 manifest entry。Runner不能修改该entry。
- `PUT /uploads/{upload_id}` body是原始字节，必须带十进制 `Upload-Offset` 和小写十六进制 `X-Flori-Chunk-SHA256`；响应当前cursor。
- `verify` body重复提交声明的 `{size_bytes,sha256}`，响应同一个Server manifest entry。
- Runner把全部已验证entry按name排序构造 `flori.artifact.v1` manifest并提交其规范JSON SHA-256；Server从ledger独立构造同一manifest并比较摘要。
- `fail` 只提交封闭 `error_code` 和可选manifest摘要，不接收Runner自定义message；所有非2xx响应使用统一 `ErrorResponse`。

相同请求的幂等边界：

- log sequence 相同且摘要相同返回成功；内容不同冲突。
- 单条 log line 最大64 KiB，累计不能超过该 Task声明的 `task_log.max_bytes`。
- upload 的 offset 必须等于服务端 cursor；重复 chunk 以 offset+摘要识别，不能覆盖已确认字节。
- verify 与 complete 同摘要可重复；不同摘要冲突。
- usage 由 `exec_id + invocation_key` 唯一。
- complete/fail 首次终态获胜；另一终态返回 `stale_attempt`。

Runner API没有“修改 Task”“选择下一个 Task”“发布 Source”或“删除 Artifact”能力。

## 失败、取消与恢复

- Runner 断线：lease 到期，Attempt 变 expired；有 retry 时 Task 回 ready，否则失败。
- Job 取消：pending/ready Task直接 canceled；leased Attempt先 fence再 canceled。迟到文件和状态写入全部拒绝；只允许原 Runner把已登记的 usage started 行补成 final。
- Server 重启：SQLite 恢复 Task/Attempt；`.staging/uploads` 按 uploads 表续传或删除。
- 隧道断线：poll、上传和 UI 暂停，Home Core 状态不产生副本。
- 同一 Task多次 Attempt各自保留已声明 log/audit；只有成功 Attempt能提交业务 Artifact。
- Task永久失败时，一个事务先fence全部活跃Attempt、把其它非终态Task置canceled，再把失败Task和Job置failed；current/previous不变。之后任何Attempt业务写入还会因父Job非running被拒绝。
- `core.*` 也创建 Attempt并使用同一状态机，但 `runner_id` 为空且不经过poll。`core.publish` CAS确认自己是唯一sink、其它Task全为succeeded/skipped后，才在一个事务把Task与Job置succeeded并切换发布指针。

## 实现禁区

- 不增加 Redis queue、Worker push、双向 WebSocket、Runner直连NAS/SQLite或本地专用路径。
- 不增加 Provider表、Claude/API-key适配器、自动 fallback、通用异步 AI task或任意 shell executor。
- 不把 model/effort 放到可变全局读取点；必须使用 Job/Attempt快照。
- 不在 Runner 接受旧 YAML、旧 Artifact manifest、旧错误码或未知字段。
- 不为单个 Task/Attempt提供用户删除；用户删除只有完整 Source。
