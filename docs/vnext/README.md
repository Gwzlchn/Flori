# Flori vNext 文档入口

本目录是 `rust-vnext` 分支的设计真相。Python 生产系统只存在于 `main` 和 Git 历史，用于生产维护和最终回退，不得作为 vNext 实现依据。

## 阅读顺序

| 需要回答的问题 | 权威文档 |
|---|---|
| 保留什么、删除什么 | [product.md](product.md) |
| 系统部署在哪里、组件怎样通信 | [architecture.md](architecture.md) |
| ID、状态、SQLite、HTTP 和文件语义 | [contracts.md](contracts.md) |
| Pipeline YAML、重跑和 Runner 协议 | [pipeline-runner.md](pipeline-runner.md) |
| Rust/TypeScript 规则、测试和 Agent 流程 | [development.md](development.md) |
| 镜像、环境和冷切换 | [deployment.md](deployment.md) |
| 离线黄金样本 | [tests/fixtures/vnext/README.md](../../tests/fixtures/vnext/README.md) |

## 权威顺序

1. `CLAUDE.md` 决定协作和交付行为。
2. 本目录决定 vNext 产品与契约。
3. WP04 后，Rust 类型、SQLite migration 和生成的 OpenAPI 分别成为代码级真相；文档只解释不变量和边界。
4. `main` 和 Git 历史中的旧 Python 代码只证明现状，不产生兼容要求。

出现冲突时停止实现并修改唯一权威，不在消费方增加 alias、fallback、影子 DTO 或双读。

## 当前阶段

WP01-WP11 已完成。PDF 上传、直接 URL 和 arXiv 使用同一 Pipeline；digital PDF 可经 media Runner 生成结构、Figure、Table 区域和严格 evidence，再由 QoderCLI 或 CodexCLI 生成并发布 current 成果。扫描 PDF 在 extractor 和 AI 前拒绝。真实 Qoder 验收已在单独授权下完成；它不构成生产部署授权。

WP12-B 已闭合本地 MP4 的首条产品链：media Runner 完成探测、离线 faster-whisper 转写、场景代表帧、长场景补帧、pHash/SSIM 去重和机械笔记，AI Runner 生成视频笔记后由 Home Core 校验时间 Evidence、发布 current 并进入 FTS；前端可播放视频、查看关键帧并按 Evidence 毫秒定位。WP12-C 已增加独立 download Runner：YouTube 单视频只使用自己的显式代理和可选 cookie，Bilibili 单视频强制禁用代理并使用独立可选 cookie；原生字幕优先，缺失时才由 media Runner 调用 Whisper，弹幕保留为原始 Artifact。频道订阅留在 WP12-D。WP13-A 已完成 current-only evidence、FTS 和 Artifact 读取投影；MCP 与其余知识库管理面仍待后续切片。WP14-A 至 WP14-H 已完成 PDF 上传、搜索、重跑和 evidence 阅读，并恢复领域、分类、内容三级知识库导航、HTML 优先阅读、Pipeline 工作台、丰富元信息、图表目录、System、Events、Runner 和 About 页面；前端只使用生成的 OpenAPI client。当前仍不是生产候选。

WP15 已完成 Job 取消、Source 完整删除、旧 Job Artifact 保留清理、SSE、system health 和启动恢复。生产冷切换、现有数据删除、公网变更与旧 Python 退役仍只属于 WP16，必须单独授权。

| 工作包 | 目标 | 产品代码 |
|---|---|---|
| WP01 | 建立本目录、精简协作规则 | 不允许 |
| WP02 | 冻结最小黄金样本 | 不允许 |
| WP03 | 冻结首版契约并完成独立终审 | 不允许 |
| WP04 | 建立 Rust/TypeScript 工程与 CI 硬门 | 仅工程骨架 |
| WP05-WP07 | SQLite、NAS Artifact 和 Pipeline 编译器 | 已完成 |
| WP08 | Job 创建、重跑、DAG 推进和发布轮换 | 已完成 |
| WP09 | Runner 注册、lease、日志、usage、Artifact 和终态协议 | 已完成 |
| WP10 | QoderCLI/CodexCLI AI Runner | 完成 |
| WP11 | PDF 三入口、解析、AI 笔记、evidence、发布和读取 | 已完成 |
| WP12-A | 本地视频离线黄金样本与共享类型验证 | 已完成 |
| WP12-B | 本地视频 Pipeline、离线转写、AI 笔记、发布和阅读 | 已完成 |
| WP12-C | Bilibili/YouTube 公开单视频、字幕/弹幕与独立下载 Runner | 已完成 |
| WP12-D | 频道订阅与 Collection fanout | 待后续切片 |
| WP13-A | current evidence、FTS 和 Artifact 读取 | 已完成 |
| WP13 | MCP 和其余知识库管理面 | 待后续切片 |
| WP14-A | PDF 上传、Job、Artifact、搜索和 evidence 阅读 UI | 已完成 |
| WP14-C | 知识库导航与 PDF 内容详情 | 已完成 |
| WP14-B～H | 响应式工作台、HTML 阅读、Pipeline、图表、System 与产品验收 | 已完成 |
| WP15 | 删除、保留、观测和安全收口 | 已完成 |
| WP16 | 生产冷切换与旧 Python 退役 | 单独授权 |

WP05 之后的业务实现必须以已冻结的 `flori.v1` 契约为边界；发现缺口先修订唯一契约，不在实现层增加兼容字段。

## vNext 的一句话边界

Flori 把 PDF、arXiv、Bilibili、YouTube 和本地视频转换为可阅读、可检索、可回到原文位置的个人知识成果；Rust Home Core 保存唯一业务状态，内网 Runner 只执行任务，Vue 前端只消费生成契约。
