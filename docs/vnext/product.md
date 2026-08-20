# vNext 产品范围

## 目标

Flori 是单用户、家庭存储优先的个人知识库。核心价值只有三件事：摄入可靠，笔记易读，结论可回到原始证据。

第一版优先跑通少量完整路径，不复制旧系统的全部入口、状态、页面和运维补救功能。

## 保留的用户路径

### PDF 与 arXiv

```text
安全下载或上传 digital PDF
  -> PDF 结构、Figure、caption、页坐标和 Table 区域
  -> 可选全文翻译
  -> AI 生成中文笔记、摘要、关键名词与解释
  -> Rust 校验 canonical evidence
  -> 原子发布、FTS 和阅读器
```

- 支持 arXiv URL、直接 PDF URL和本地 PDF 上传。
- 扫描 PDF 在检测阶段明确失败；vNext 不做 PDF OCR。
- Figure 保留图像、caption 和 PDF locator。
- Table 只保留页面区域截图或普通文本，不建立单元格模型。
- 不做普通 HTML、学术 HTML、网页资产闭包和 book TOC。

### 视频与订阅

```text
Bilibili、YouTube 或本地视频
  -> 保留成功下载的原始输入和弹幕原文件
  -> 平台字幕优先，Whisper 兜底
  -> 少量关键帧与时间戳
  -> 易读的忠实机械笔记
  -> AI 分析型智能笔记、摘要、关键名词与解释
  -> Rust 校验字幕时间段和关键帧引用
  -> 原子发布、FTS 和阅读器
```

- 保留 Bilibili 单视频、UP 订阅、二维码登录和 cookie 管理。
- 保留 YouTube 单视频与频道订阅。
- 保留 NAS 本地视频；远程与本地 Runner 使用同一 Artifact HTTP 路径。
- 保留分 P，但只输出一个有序 manifest，不维护 Parts 状态机。
- 场景检测、断句和图像去重只作为执行器内部算法，不成为独立 Task。
- vNext 首版不做视频 OCR。出现经过验证的画面文字场景后再单独决策。
- 删除音频、播客和 RSS。

## 知识库与管理面

保留：

- Source、Job、Task、Pipeline DAG、日志和声明 Artifact 查看。
- Domain、Collection、Domain profile 和专有术语管理。
- 当前 Job 关键名词、全局 Glossary、Concept graph、timeline 和 topic 投影。
- SQLite FTS5、文档/视频双笔记阅读器、PDF 页和视频时间/帧证据。
- 只读 MCP 服务。
- Prompt UI；Prompt 是强类型 Pipeline 输入，创建 Job 时固化快照。
- Runner 管理、AI 用量、费用或订阅 credits、精简 Dashboard。
- 可选 Prometheus、Dozzle 和 tunnel-stats；它们不保存 Job 真相。

删除：

- Ask、Radar、Study/SRS 及其建议、掌握度和复习状态。
- 批量 retry-failed、rebuild-stale 和通用异步 AI task。
- 删除单个 Task、Artifact、Job 或版本的入口；只允许完整删除 Source。
- 双向 WebSocket；页面实时刷新只用 SSE。

## AI 执行

- 只支持 QoderCLI Runner 和 CodexCLI Runner。
- Pipeline 用 `tags` 选择 Runner；不建立通用 Provider 平台。
- 不支持 ClaudeCLI、API-key Provider、自动 fallback 或多 Provider 对比。
- Runner 上报 CLI 版本、支持的 model 和 effort；websearch 由 Runner 自检，不进入注册能力字段。
- 指定 Runner 重跑时，在新 Job 创建时固化 `runner_id`、`model`、`effort` 和 `runner_config_revision`。
- 配置变化不影响已排队 Job；Runner 不可用时等待或失败，不自动换 Provider。

## 版本、重跑和删除

- 每次初次投递或重跑都创建新的 `job_id`。
- 支持整条 Pipeline 重跑，或从一个 Task 开始重跑；后继 Task 全部重跑。
- 重跑可复用仍满足契约的上游 Artifact，不产生 Provider 专用命令。
- Source 只指向两个成功发布成果：`current_job_id` 和 `previous_job_id`。
- 失败 Job 不改变 current/previous；详细审计只保留最近一个失败 Job。
- 删除只提供 `delete_source`，必须删除该 Source 的 Job、Task、原始输入、声明 Artifact、usage 明细和发布投影，不留孤儿。

## 基础设施边界

- SQLite 是业务、队列、lease、FTS、投影和 usage ledger 的唯一结构化真相。
- 家庭 NAS POSIX 文件系统是 Artifact 字节的唯一真相。
- 删除 Redis、MinIO、RemoteStorage、多 StorageBackend 和多 pool 控制面。
- Runner 只通过出站 HTTPS 认领、续租、传日志和 Artifact，不直连 SQLite 或 NAS。
- Artifact 物理备份由 NAS 负责，Flori 不实现备份仓库、导入导出或 Exact DR。
- 普通升级不备份 SQLite。只有 schema 实际变化时才停止写入、保留旧 SQLite 和旧镜像；迁移失败立即退出并回退。
- Python 到 Rust 不导入旧业务数据，使用空 SQLite 和新 Artifact 根重新投递。

## A-I 决策归属

| 组 | 已冻结边界 | 权威位置 |
|---|---|---|
| A 内容入口 | PDF/arXiv、Bilibili/YouTube、本地视频保留；HTML/audio/RSS/book 删除 | 本文“保留的用户路径” |
| B 文档链 | digital PDF、Figure、Table 区域、翻译、笔记和 evidence | 本文“PDF 与 arXiv” |
| C 视频链 | 字幕/Whisper、关键帧、机械与智能笔记、弹幕原文件 | 本文“视频与订阅” |
| D 产品面 | DAG、知识库、MCP、Prompt、Runner、usage 保留；Ask/Radar/Study 删除 | 本文“知识库与管理面” |
| E AI | 仅 QoderCLI 与 CodexCLI Runner，无 fallback | 本文“AI 执行” |
| F 运行时 | ECS Edge、家庭 Home Core、多个出站 Runner | [architecture.md](architecture.md) |
| G 恢复 | 只在 schema 变化时保留旧 SQLite/镜像，无产品备份 | [deployment.md](deployment.md) |
| H 前端 | Vue 3 + strict TypeScript，Rust 契约单向生成 | [development.md](development.md) |
| I 开发 | 宿主 Cargo 快循环、Docker 部署、少量 CI 门和短期 worktree | [development.md](development.md) |

## 长期非目标

- 多租户、Kubernetes、云端托管算力和商业计费平台。
- 零停机数据库迁移、混合版本 Worker 和永久协议兼容。
- 为尚不存在的第三 Provider、第二存储或第二数据库建立插件层。
- 逐文件翻译 Python、搬运旧 schema、旧 Artifact 或 4306 个旧测试。
