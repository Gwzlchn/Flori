# ADR-0016: Rust vNext 以当前格式重新实现

## 状态

已采纳，适用于 `rust-vnext`。Python 生产系统在 WP16 冷切换前仍由旧文档描述。

## 背景

现有系统同时维护 Python 模型、Redis 队列、MinIO/本地双存储、多 Provider、三十余 Pipeline 步骤、历史 schema/Artifact 兼容、168 个接口和接近产品代码量的测试。个人项目的小需求因此经常跨越过多状态、适配器、文档和发布门。

直接按文件把旧系统翻译成 Rust 只会复制复杂度。vNext 必须先删除弱使用功能和永久兼容，再利用强类型把剩余契约收敛为一个真相。

## 决定

1. 后端、调度器和 Runner 使用 Rust；前端保留 Vue 3 + strict TypeScript。
2. PDF 和媒体生态继续使用无业务状态的外部工具或短命 extractor，不在 Rust 重造解析器。
3. Home Core 使用一个 SQLite 和一个 NAS POSIX Artifact 根；删除 Redis、MinIO 和 StorageBackend 平台。
4. ECS 只做无状态 Edge；任意内网 Runner 通过出站 HTTPS 认领和回传。
5. AI 只保留 QoderCLI 与 CodexCLI 两类 Runner，不支持通用 Provider、API key 或自动 fallback。
6. Rust 类型生成 OpenAPI 和 TypeScript；禁止手写 wire DTO、兼容解析和影子状态。
7. Pipeline 使用 GitLab CI YAML 的严格子集，不执行任意 script、image、services、include 或 extends。
8. Python 数据不迁移；Rust 使用空库重新投递。普通升级不备份，schema 变化只保留旧 SQLite 与旧镜像用于立即回退。
9. 开发采用一次批准后自主 deliver；复杂度预算、真实 fixture 和 L0-L3 风险门替代普遍重流程。

## 取代关系

对 vNext，本 ADR 取代 ADR-0001、0002、0003 和 0004，并改写 ADR-0005、0009、0010、0011、0014 和 0015 的运行边界。旧 ADR 只保留在 `main` 和 Git 历史，不要求 Rust 兼容。

## 后果

- Rust 业务实现必须等待 WP03 契约终审。
- 第一个完整产品切片是 digital PDF/arXiv；视频在共享 Runner 协议稳定后并行。
- 旧功能、旧测试和旧运维入口只有重新列入 [vNext 产品范围](../vnext/product.md) 才能实现。
- 最终切换有停机窗口，但不承担双系统、数据导入和混合版本成本。

## 拒绝的方案

- 一比一翻译现有 Python：保留全部错误边界和测试负担。
- 先把旧 Python 完整重构后再写 Rust：支付两次重构成本。
- 在线逐功能双写或混跑：产生跨语言状态、计费和 Artifact 一致性问题。
- 追求纯 Rust：PDF、下载、媒体和转写工具生态没有必要重造。
