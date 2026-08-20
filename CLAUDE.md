# Flori vNext Agent 指南

本文件同时供 Claude Code 与 Codex 使用；`AGENTS.md` 是本文件的 symlink。只维护这一份。

## 当前分支

`rust-vnext` 是精简 Rust 重写的集成分支。`main` 在 WP16 前仍代表 Python 生产系统。

先读 [docs/vnext/README.md](docs/vnext/README.md)，再只读当前任务对应的一个或两个权威文档。旧 ADR、Python 代码和旧前端只存在于 `main` 与 Git 历史，不产生 vNext 兼容要求。

WP03 独立设计终审无 P0/P1 前，禁止编写 Rust 业务代码。

## 产品边界

保留 PDF/arXiv、本地 PDF、Bilibili/YouTube 与频道订阅、本地视频、机械笔记、AI 智能笔记、翻译、canonical evidence、阅读器、FTS、Domain、Collection、Profile、Glossary、Concept、MCP、Prompt、Runner 与 AI usage。

删除 HTML、book TOC、audio、RSS、Ask、Radar、Study/SRS、Claude/API-key Provider、自动 fallback、通用异步 AI task、Redis、MinIO、通用 StorageBackend、Exact DR、导入导出、历史 schema/Artifact 兼容和部分删除入口。

详细范围只以 [docs/vnext/product.md](docs/vnext/product.md) 为准。

## 架构硬边界

- Rust Home Core 是唯一业务 writer；SQLite 是唯一结构化真相，家庭 NAS 是唯一 Artifact 字节真相。
- ECS Edge 只做 TLS、Basic Auth、静态前端和反向代理。
- Runner 只通过出站 HTTPS 认领、续租、传日志与 Artifact，不直连 SQLite/NAS。
- Pipeline YAML 只在 Git 修改；Prompt 可在 UI 修改，创建 Job 时固化快照。
- Source 只保留 current 和 previous 两个成功发布成果；重跑总是新 Job。
- 只允许完整 `delete_source`，不允许删除单个 Job、Task、Artifact 或版本。
- 普通升级不备份；schema 变化才保留旧 SQLite 与旧镜像，失败立即回退。
- Python 到 Rust 不迁移业务数据，以空库和新 Artifact 根正常重投。

## 语言与目录

后端使用 Rust，前端使用 Vue 3 + strict TypeScript。外部 PDF、下载、媒体和转写工具是 Runner 内短命执行器，不持有业务状态。

计划中的产品 crate 只有：

```text
flori-core
flori-store
flori-pipeline
flori-server
flori-runner
xtask
```

不得按功能建立新 crate。Rust workspace、vNext Compose 和新实现由 WP04 后续创建。

唯一类型链：

```text
Rust Serde 类型 -> utoipa OpenAPI -> openapi-typescript -> openapi-fetch -> Vue
```

- ID 用 UUIDv7 newtype，状态用封闭 enum，状态消费必须穷尽匹配。
- 契约输入默认 `deny_unknown_fields`。
- domain/contract 禁止动态 Value、任意字段袋、alias、untagged fallback、Unknown 兼容状态和影子 DTO。
- SQLite 使用 SQLx 明文 SQL和查询宏，不用 ORM、Repository trait 或 DAO 基类。
- 前端不手写 API DTO，不使用 `as unknown as`、`@ts-ignore`、`@ts-nocheck` 或全局 `any`。
- 一个错误只在领域定义一次；`anyhow` 只用于进程入口和外部工具上下文。

## 执行模式

### consult

只读回答、诊断和审查。读取回答所需证据，不建工作项、不改文件、不跑无关测试。

### deliver

用户批准包含目标、非目标、scope、复杂度预算、验收和终点的执行包后，Agent 自主完成：

```text
实现 -> 本地最小验证 -> deletion pass -> 风险测试
     -> 自审 -> commit -> push -> 处理本范围 CI -> 报告
```

不在“是否 commit”“是否 push”“是否继续修 CI”处重复询问。

### operate

生产部署、停机、数据删除、凭据、外部账号和真实内容投递必须单独授权，并先固定目标、拒绝条件、回滚和验收。

## 六类暂停条件

只有以下情况重新询问用户：

1. 改变已冻结产品功能。
2. 新增未申报的表、状态、endpoint、DSL 字段、Artifact kind、crate、依赖、服务、镜像、Provider、兼容或 fallback。
3. 触发复杂度报警且无法拆小。
4. fixture、工具或环境失效，原验收不能执行。
5. 产生未批准 AI 费用、账号操作、生产停机、数据删除、凭据或公网变化。
6. CI 问题属于另一个 WP。

## 复杂度门

小需求默认新增架构原语为 0。第三个真实调用方出现前不抽通用框架；两处短重复优先于错误抽象。

以下任一项触发停线重切：

- 净新增手写生产代码超过 300 行。
- 修改手写文件超过 10 个。
- 跨两个以上业务 crate 或两个以上前端页面。
- 出现未申报架构原语。
- 两条实现路径连续失败。
- 20 分钟没有可编译骨架，或 45 分钟普通小需求没有本地绿灯。

禁止增加只转发参数的 service/repository/manager、单实现 factory/registry、未使用 feature flag、永久兼容或“以后可能用”的配置。

提交前删除未使用 public API、无价值抽象、重复 DTO、逐层错误包装、无真实用例分支和测试专用生产接口。CI 绿灯不能证明没有同义模型。

## 验证

| 等级 | 变更 | 验证 |
|---|---|---|
| L0 | 文档、样式、治理 | 静态检查、链接和模拟决策 |
| L1 | 纯函数、单页面 | fmt/check/typecheck + 直接单测 |
| L2 | API、SQLite、侧车 | L1 + 真实 integration |
| L3 | schema、安全、计费、删除、凭据 | L2 + 恶意输入、崩溃点、幂等、独立终审 |

Mock 只能替代外部站点、Qoder/Codex 和媒体工具，不能替代 DAG、SQLite 事务、Artifact commit、evidence 和前端契约。

WP04 后统一使用 `cargo xtask check|test|integration|image|diff-budget|janitor`。WP04 前不得临时建立第二套入口。

## 分支、worktree 与子 Agent

- 默认一个主 Agent 和一个 integrator。
- 无并行冲突时不创建多 Agent DAG。
- 并行或主树脏时使用 `$FLORI_WORKING_DIR/wt/<slug>`；每个 worktree 独立 target/tmp，不清共享 Cargo 下载缓存。
- 子 Agent 必须得到 WP、依赖、scope、非目标、contract revision、fixture、架构预算、验证命令、热点 owner 和回收条件。
- 子 Agent 只实现、测试和报告，不改产品范围、最终版本、`rust-vnext`、生产或共享热点。
- Cargo.lock、SQLite schema、Pipeline schema、Artifact manifest、Runner OpenAPI、前端 router/types、CI 和 Compose 同时只有一个 owner。
- 主 Agent 汇总 diff、运行并集验证、整理提交、push 和回收。

## Git 与记录

- Git 使用已配置的 `user.name` 和 `user.email`，不得在命令中覆盖。身份缺失时询问用户。
- 一个可独立验收和回滚的价值对应一个提交；Agent、评审和反馈轮次不是提交边界。
- 普通小任务不建长 worklog。WP、迁移、运维和长调研只维护一份简短记录。
- 非发布治理和 rust-vnext 中间提交不 bump 版本。版本只在发布候选统一修改一次。
- 对外契约、实现、消费方和测试必须在同一 WP 闭环。
- 生产 main 合并、部署、删除和 WP16 冷切换始终需要用户单独授权。

## 注释与文档

- 注释解释不变量、边界和坑，不复述代码，不记录已删除历史。
- 使用直陈短句和半角标点；不要装饰性分隔线、符号或嵌套括号长句。
- 一个稳定规则只有一个权威位置，其它文档用链接引用。
- 代码事实进入代码和测试；设计原因进入 ADR；过程只进入简短 WP 记录。
