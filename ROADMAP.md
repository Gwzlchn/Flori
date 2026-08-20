# Flori vNext Roadmap

vNext 在同一仓库的 `rust-vnext` 分支开发。`main` 在 WP16 前仍是 Python 生产线；旧里程碑保留在 Git 历史，不复制进新的待办。

## 当前状态

| 工作包 | 状态 | 开放条件 |
|---|---|---|
| WP00 当前 main 收口 | 完成 | `bbaa06c`，CI 通过 |
| WP01 vNext 流程与权威文档 | 完成 | 已进入 `rust-vnext` |
| WP02 黄金样本与验收基线 | 完成 | 离线 fixture 已冻结 |
| WP03 契约冻结与独立终审 | 完成 | `flori.v1`，P0/P1 为 0 |
| WP04 Rust 工程与 CI 骨架 | 完成 | 统一命令和类型链可运行 |
| WP05-WP07 首批产品实现 | 完成 | SQLite、NAS Artifact 与 Pipeline 编译器已闭环 |
| WP08 Job 调度 | 完成 | 初次、整条和从 Task 重跑已通过真实 SQLite/NAS 验收 |
| WP09 Runner 协议 | 完成 | 出站 HTTP、日志、usage、Artifact 和恢复已闭环 |
| WP10 AI Runner | 开放 | 只接 QoderCLI 与 CodexCLI 执行器 |
| WP11-WP15 后续产品实现 | 未开放 | 按下方依赖推进 |
| WP16 冷切换 | 未开放 | 全部验收并获得生产授权 |

## 依赖

```text
WP01 流程 ─┐
           ├─> WP03 契约 ─> WP04 工程
WP02 样本 ─┘                 |
                              ├─> WP05 SQLite ─┐
                              ├─> WP06 Artifact ├─> WP08 调度 ─┐
                              `─> WP07 Pipeline ┘              |
                              └─> WP09 Runner 协议 ─> WP10 AI ─┤
                                                               ├─> WP11 PDF
                                                               ├─> WP12 视频
                                                               └─> WP13 知识库
                                                                    |
                                                                    v
                                                                  WP14 UI
                                                                    |
                                                                    v
                                                                  WP15 收口
                                                                    |
                                                                    v
                                                                  WP16 切换
```

WP05、WP06、WP07 可并行。WP11、WP12、WP13 在共享核心稳定后可并行。共享 schema、Artifact manifest、Pipeline schema、Runner OpenAPI、前端生成类型和 CI 各有一个 owner。

## 已完成基础批次

| 工作包 | 独占热点 | 不得修改 |
|---|---|---|
| WP05 SQLite 业务与队列核心 | SQLite schema、migration、`.sqlx`、`flori-store` | Artifact manifest、Pipeline schema、Runner OpenAPI |
| WP06 NAS Artifact 核心 | Artifact manifest、NAS 路径与 staging 实现 | SQLite schema、Pipeline schema、Runner OpenAPI |
| WP07 Pipeline 编译器 | Pipeline YAML schema、示例与 `flori-pipeline` | SQLite schema、Artifact manifest、Runner OpenAPI |

主 Agent 独占 Cargo.lock、workspace 根、CI、Compose、最终集成和 `rust-vnext`。三个子任务都不得新增 `flori.v1` 之外的表、状态、endpoint、DSL 字段、Artifact kind、crate、Provider、兼容或 fallback。

WP08 与 WP09 已完成：同一 PDF Pipeline 已通过真实 SQLite、NAS、TCP Server 和 RunnerClient 跑完 DAG，整条重跑会生成全新 ID，从 Task 重跑会物化上游 Artifact，发布事务只轮换 current/previous 指针。旧 previous 的物理 Artifact 回收、delete_source 和长期保留仍属于 WP15，不在调度器增加第二套 GC。

下一步只开放 WP10。它只实现 QoderCLI 与 CodexCLI 的实际执行、版本锁、websearch 探测和 usage 审计；不得在该工作包补公共 CRUD、PDF 内容处理、第三 Provider、fallback 或通用 executor 框架。

## 工作包

| WP | 可观察结果 |
|---|---|
| 01 | 未来 Agent 从 tracked 文档和 Skill 得到同一套简洁流程 |
| 02 | digital PDF、扫描 PDF 拒绝和短视频可离线重放 |
| 03 | 领域、SQLite、Pipeline、Artifact、Runner、usage 和错误契约冻结 |
| 04 | Cargo/TypeScript 快循环与防膨胀 CI 可运行 |
| 05 | 空目录创建 SQLite，认领、lease、usage 和事务可重启验证 |
| 06 | NAS staging、摘要、原子发布和恢复通过恶意/崩溃测试 |
| 07 | GitLab CI YAML 严格子集编译为确定性 DAG |
| 08 | 初次 Job、整条重跑和从 Task 重跑正确轮换 current/previous |
| 09 | 多内网 Runner 经出站 HTTPS 安全认领、传日志和 Artifact |
| 10 | QoderCLI/CodexCLI 镜像按锁定版本执行并记录幂等 usage |
| 11 | arXiv/PDF 形成阅读器、翻译、笔记、Figure/Table 区域和 evidence |
| 12 | Bilibili/YouTube/本地视频形成字幕、关键帧和双笔记 |
| 13 | Domain、Collection、Profile、Glossary、Concept、FTS 和 MCP 可重建 |
| 14 | Vue 只使用生成 client 完成保留页面 |
| 15 | 删除、保留、安全、schema 回退和观测闭环 |
| 16 | 空库部署 Rust，正常重投并在验收后退役 Python |

## 每个 WP 的完成定义

1. 用户可观察结果、非目标和复杂度预算没有漂移。
2. 契约、实现、消费方、测试和必要文档在同一 WP 闭环。
3. 本地最小验证和真实 fixture 路径通过。
4. deletion pass 完成，无影子 DTO、兼容层和未使用公开面。
5. 到达该 WP 已批准的 commit、push 和 CI 终点。

生产 main 合并、部署、数据删除和 WP16 冷切换始终单独授权。
