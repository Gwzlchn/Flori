# Flori vNext

Flori 是自托管的个人知识库，把 PDF、arXiv、Bilibili、YouTube 和本地视频转换为易读笔记，并让每个结论可以回到 PDF 页、字幕时间段或关键帧。

`rust-vnext` 是 clean-slate Rust 重写分支。旧 Python 生产系统只保留在 `main` 和 Git 历史，本分支不维护旧代码、旧 schema、旧 Artifact 或兼容层。

## 当前阶段

WP01-WP03 只建立流程、黄金样本和冻结契约。WP03 独立设计终审通过前没有 Rust 产品实现；请勿把当前分支当成可部署版本。

进度见 [ROADMAP.md](ROADMAP.md)，设计从 [docs/vnext/README.md](docs/vnext/README.md) 开始。

## 目标架构

```text
Browser -> ECS Edge -> reverse SSH tunnel -> Home Rust Server
                                                |-- SQLite
                                                `-- NAS Artifacts

Internal Runner -> outbound HTTPS -> ECS Edge -> Home Rust Server
```

- Rust Home Core：API、Pipeline DAG、SQLite、NAS、MCP 和发布。
- Rust Runner：内网出站认领任务，调用成熟 PDF/媒体/AI 工具并回传声明 Artifact。
- Vue 3 + strict TypeScript：只消费 Rust 生成的 OpenAPI 类型。
- QoderCLI 和 CodexCLI：两个明确的 AI Runner，不建立通用 Provider 平台。

## 第一版范围

保留 digital PDF/arXiv、视频与频道订阅、机械笔记、AI 智能笔记、翻译、证据定位、阅读器、FTS、Domain、Collection、Glossary、Concept 和 MCP。

不做 HTML、音频、RSS、扫描 PDF OCR、Ask、Radar、Study、Redis、MinIO、备份导入、旧数据迁移和永久兼容。

完整取舍见 [产品范围](docs/vnext/product.md)。

## 开发原则

- 个人项目以快速、可验证的垂直切片为主。
- 一个领域类型只在 Rust 手写一次；OpenAPI 和 TypeScript 单向生成。
- 新架构原语默认预算为 0，第三个真实调用方前不抽通用框架。
- 宿主 Cargo 做快速循环，Python/Node/媒体工具走容器，生产全 Docker。
- 生产合并、部署、删除和最终冷切换单独授权。

协作规则见 [CLAUDE.md](CLAUDE.md) 和 [vNext 开发规范](docs/vnext/development.md)。
