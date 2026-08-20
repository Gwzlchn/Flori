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

WP12-A 只完成本地三秒视频的离线黄金基线，包括锁版 FFmpeg 探测、字幕规范化、关键帧和机械笔记；尚无视频 Pipeline 或 product daemon。WP13-A 已完成 current-only evidence、FTS 和 Artifact 读取投影；MCP 与其余知识库管理面仍待后续切片。WP14-A 已完成最小 PDF UI，可上传、创建 Job、查看 DAG、日志和发布 Artifact；它只使用生成的 OpenAPI client。当前仍不是生产候选。

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
| WP12-A | 本地视频离线黄金样本与共享类型验证 | 已完成；不等于完整 WP12 |
| WP12 | 视频 Pipeline、平台下载、转写和发布 | 待后续切片 |
| WP13-A | current evidence、FTS 和 Artifact 读取 | 已完成 |
| WP13 | MCP 和其余知识库管理面 | 待后续切片 |
| WP14-A | 最小 PDF 上传、Job 和 Artifact UI | 已完成 |
| WP14 | 其余保留页面 | 待后续切片 |
| WP15 | 删除、保留、观测和安全收口 | 待后续切片 |
| WP16 | 生产冷切换与旧 Python 退役 | 单独授权 |

WP05 之后的业务实现必须以已冻结的 `flori.v1` 契约为边界；发现缺口先修订唯一契约，不在实现层增加兼容字段。

## vNext 的一句话边界

Flori 把 PDF、arXiv、Bilibili、YouTube 和本地视频转换为可阅读、可检索、可回到原文位置的个人知识成果；Rust Home Core 保存唯一业务状态，内网 Runner 只执行任务，Vue 前端只消费生成契约。
