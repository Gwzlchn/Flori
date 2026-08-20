# vNext 架构

## 部署拓扑

```text
浏览器
  |
  v
ECS Edge: Caddy + Vue 静态文件 + Basic Auth
  |
  | 家庭侧主动建立的反向 SSH 隧道
  v
Home Core: Rust Server
  |-- SQLite: 业务、队列、lease、FTS、投影、usage
  `-- NAS: 原始输入、声明 Artifact、日志与审计文件

任意内网 Runner
  `-- 出站 HTTPS -> ECS Edge -> Home Core
```

ECS 不保存业务数据库、任务队列或 Artifact，也不远程挂载家庭 SQLite。隧道中断只影响访问和认领，不产生第二真相。

## 组件责任

### flori-edge

- 提供公网固定入口、TLS、Basic Auth、Vue 静态文件和反向代理。
- 不解析 Job、不缓存 Artifact、不持有 Runner 业务状态。

### flori-server

- 提供 UI API、Runner API、MCP、SSE 和 Artifact 流式传输。
- 编译 Pipeline DAG，创建 Job/Task，执行 SQLite 状态转换和原子发布。
- 管理 Runner token、平台 cookie、Prompt、Domain、Collection 和投影。
- 是唯一可写 SQLite 与最终 NAS Artifact 根的组件。

### flori-runner

- 使用长期 Runner token 从公网入口长轮询 Task。
- 下载声明输入，在隔离临时目录调用一个明确 executor。
- 按 sequence 推送日志，上传声明输出并请求原子完成。
- 不访问数据库，不决定发布版本，不持有长期业务状态。

同一个 Rust Runner 二进制用于所有镜像。镜像只因外部工具和凭据不同而拆分：

| 镜像 | 外部能力 |
|---|---|
| `flori-runner-media` | PDF extractor、yt-dlp、yutto、FFmpeg、可选 Whisper |
| `flori-runner-ai-qoder` | 固定版本 QoderCLI |
| `flori-runner-ai-codex` | 固定版本 CodexCLI |

## 唯一真相

| 数据 | 唯一真相 | 可重建内容 |
|---|---|---|
| 业务状态 | Home Core SQLite | Dashboard、SSE 页面状态 |
| Artifact 字节 | Home Core NAS | 阅读器响应、部分投影 |
| Pipeline 定义 | Git 中 YAML | PipelineRevision 和 DAG |
| Prompt 当前配置 | SQLite | Job 的不可变 PromptSnapshot |
| HTTP 契约 | Rust 类型生成的 OpenAPI | TypeScript API 类型 |
| 搜索与概念关系 | SQLite 发布投影 | 从 current Artifact 重建 |

SSE、Prometheus、Dozzle、隧道统计和 Runner 本地文件都不是业务真相。

## 核心边界

- `Source` 是一个稳定内容来源或订阅 lineage。
- `Job` 是一次 Pipeline 执行；初次投递和每次重跑都有新 ID。
- `Task` 是 Job DAG 中的一个节点。
- `Attempt` 是一次 Task 执行，Attempt ID 同时作为 `exec_id` 和 lease fence。
- `Artifact` 是 YAML 中声明并经服务端校验、提交的不可变输出。
- `PipelineRevision` 是创建 Job 时固化的不可变 YAML 修订。
- `Runner` 是一个可吊销 token 对应的执行实例。

详细字段和状态只在 [contracts.md](contracts.md) 定义。

## 原子性

1. Home Core 在 SQLite 中原子认领 ready Task，创建 Attempt 和到期 lease。
2. Runner 使用 `exec_id` 执行、续租和上传。
3. Server 把上传写入 NAS staging，校验声明、路径、大小和摘要。
4. 同文件系统 rename 后，在一个 SQLite 事务内登记 Artifact、完成 Task 并推进 DAG。
5. 事务提交失败时，未挂接文件由 staging ledger 恢复或精确清理。
6. lease 已过期、被取消或不是当前 Attempt 时，所有迟到写入均拒绝。

Job 成功后才在一个事务内轮换 Source 的 current/previous。失败 Job 永不替换正式成果。

## 安全边界

- Browser 使用 Basic Auth；MCP 使用独立 token；Runner 使用 per-runner Bearer token。
- 新 Runner 只使用一次性注册 token 换取长期 token；长期 token 只返回一次并以摘要存储。
- Bilibili/YouTube cookie 在 Home Core 明文存储，只作为目标 Attempt 的 `secret_inputs` 经 HTTPS 下发。
- secret 不进入 Task 持久 payload、日志、Artifact、SSE 或 usage 明细。
- Artifact 名称来自 Pipeline 声明；Server 拒绝绝对路径、父目录、隐藏路径、符号链接、超限和摘要不符。
- 外部 URL 下载经过 scheme、DNS/IP、重定向和大小限制，拒绝 SSRF。

## 故障与恢复

- Runner 断线：lease 到期后同一 Task 可产生新 Attempt；旧 Attempt 的迟到提交被 fence。
- Server 重启：从 SQLite 恢复 ready/leased 状态，从 staging ledger 恢复未完成上传。
- 隧道断线：Runner poll 和 UI 请求失败，但 Home Core 业务真相不改变。
- schema 迁移失败：进程立即退出，运维切回旧 SQLite 和旧镜像，不启用兼容读取。
- NAS 故障：由 NAS 快照和硬盘恢复处理；Flori 只校验 manifest 与 SHA-256。
