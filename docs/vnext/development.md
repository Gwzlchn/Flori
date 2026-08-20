# vNext 开发与 Agent 规范

## 默认目标

个人项目优先快速得到一个可验证的完整切片。规则只保留三类价值：阻止范围膨胀，提前发现契约错误，保护生产和数据边界。

普通功能不建立发布列车、长 worklog、通用框架或多轮人工审批。

## 执行模式

| 模式 | 行为 |
|---|---|
| `consult` | 只读回答和诊断，不写文件、不建工作项 |
| `deliver` | 执行包批准后，自主实现、验证、commit、push 并处理本范围 CI |
| `operate` | 生产、凭据、数据删除、停机和部署；必须单独授权 |

一次批准的执行包必须写明：用户价值、非目标、允许路径、复杂度预算、验收命令、真实手验、回滚边界和交付终点。

只有以下情况暂停并询问：

1. 改变已冻结产品行为或功能取舍。
2. 新增未申报的表、状态、endpoint、DSL 字段、Artifact kind、crate、依赖、服务、镜像、Provider、兼容或 fallback。
3. 触发范围报警且无法继续拆小。
4. fixture、工具或环境失效，原验收方法不可执行。
5. 产生未批准的真实 AI 费用、账号操作、生产停机、数据删除、凭据或公网变化。
6. CI 问题属于另一个工作包。

## 小需求预算

默认新增下列架构原语均为 0：持久实体、表、状态、API 资源、Pipeline 字段、Artifact kind、配置层、crate、常驻服务、镜像、依赖、feature flag、兼容读取和 fallback。

任一条件触发停线重切：

- 净新增手写生产代码超过 300 行。
- 修改手写文件超过 10 个。
- 跨两个以上业务 crate 或两个以上前端页面。
- 出现未申报架构原语。
- 连续两条实现路径失败。
- 20 分钟没有可编译最小骨架，或 45 分钟普通小需求仍无本地绿灯。

数字是报警线，不是目标。提交前必须删除未使用 public API、单实现无价值抽象、重复 DTO、逐层错误包装、无真实用例分支和测试专用生产接口。

## 语言基础设施

### Rust

- `rust-toolchain.toml`、`Cargo.lock`、`workspace.dependencies` 和 `workspace.lints` 集中版本与规则。
- 产品 crate 只允许 `flori-core`、`flori-store`、`flori-pipeline`、`flori-server` 和 `flori-runner`，另有一个薄 `xtask`。
- ID 使用 UUIDv7 newtype；状态、错误码、Artifact kind 和 executor 使用封闭 enum。
- 契约输入使用 Serde 严格结构和 `deny_unknown_fields`。
- domain/contract 禁止动态 `Value`、任意字段袋、alias、untagged fallback 和 `Unknown(String)` 兼容状态。
- SQLite 使用 SQLx 明文 SQL、查询宏和 `.sqlx` 离线元数据，不使用 ORM 或 Repository trait。
- `thiserror` 用于少量领域错误；`anyhow` 只在二进制入口和外部工具边界。
- 默认 `forbid(unsafe_code)`；不全局启用 `clippy::pedantic`。

### TypeScript

- Vue 3 + TypeScript `strict`，同时启用 `noUncheckedIndexedAccess`、`exactOptionalPropertyTypes`、`noImplicitReturns`、`noFallthroughCasesInSwitch` 和 `noImplicitOverride`。
- Rust Serde 类型经 utoipa 生成 OpenAPI，再由 openapi-typescript 生成 `frontend/.generated`，页面使用 openapi-fetch。
- 前端不手写 API DTO，不使用 `as unknown as`、`@ts-ignore`、`@ts-nocheck` 或全局 `any`。
- Flori API 只能经统一 client 调用；页面只定义 UI view model。
- vue-tsc、最小 type-aware ESLint 和 Knip 进入检查门。
- 不为每个 DTO 再写 Zod，不新增 Nx、Turborepo、第二个前端 package 或通用缓存层。

### 依赖

- cargo-deny 检查许可证、advisory、未知 Git 来源和可控重复。
- 新依赖必须删除手写代码或消除一类已证明错误；“以后可能用”不是理由。
- cargo-nextest、sccache、全量 JSON Schema、mutation 和第二套生成器只有测量到需求后再引入。

## 编译和 CI 硬门

编译直接发现：

- Rust enum 变化后的未覆盖 `match`。
- TypeScript discriminated union 变化后的未覆盖 `switch`。
- SQLx 的参数、列和可空性漂移。
- OpenAPI 变化后的前端路径、请求和响应不一致。

CI 额外拒绝：

- contract 目录中的动态 Value、兼容 alias、untagged/flatten 兜底和影子 DTO。
- 前端 cast/ignore/any 逃逸和统一 client 外直接访问 Flori API。
- OpenAPI 无法生成，或生成类型不能通过前端检查。WP05 接入 SQLx 时在同一工作包增加 `.sqlx` 离线漂移检查。
- Knip 新死代码、cargo-deny 拒绝项和未申报架构原语。

`xtask/policy.sha256` 固定依赖、核心枚举与 OpenAPI、Compose 服务、Docker target、CI 和语言逃逸规则。已批准任务必须在同一提交显式更新该清单；只改实现而未更新清单会直接失败，清单变化本身是终审热点。

机械检查不能识别所有改名后的同义模型。CI 绿灯不能替代复杂度预算和 deletion pass。

## 验证分层

| 等级 | 适用范围 | 最小验证 |
|---|---|---|
| L0 | 文档、样式、治理 | 静态检查和链接 |
| L1 | 纯函数、单页面 | fmt/check/typecheck + 直接单测 |
| L2 | API、SQLite、侧车契约 | L1 + 一个真实 SQLite/HTTP/侧车 integration |
| L3 | schema、安全、计费、删除、凭据 | L2 + 恶意输入、崩溃点、幂等和独立终审 |

Mock 只替代外部站点、Qoder/Codex 和媒体工具。不得 mock DAG、SQLite 事务、Artifact commit、evidence 或前端契约。每个产品 WP 至少跑通一个 WP02 fixture 的用户可见路径。

WP04 提供统一命令：

```text
cargo xtask check
cargo xtask test <crate-or-module>
cargo xtask integration <scenario>
cargo xtask image <target>
cargo xtask diff-budget <base>
cargo xtask janitor --dry-run|--apply
```

`check` 依次验证 Rust 格式、Clippy、OpenAPI 导出和容器内前端检查。`test` 可省略目标运行整个 workspace，也可指定一个已声明 crate。`integration` 首版只有 `foundation`，`image` 只接受五个冻结镜像名。不得临时创建第二套脚本入口。

前端生成文件只写入 `frontend/.generated`，不进入 Git。Node 检查通过 `compose.test.yml` 在容器内执行；宿主只需要锁定的 Rust 工具链和 Docker。

## 分支、worktree 与子 Agent

- `main` 在冷切换前代表 Python 生产系统；只接收必要修复。
- `rust-vnext` 是唯一 Rust 集成分支。
- 单 Agent、无冲突的小改可直接在当前 feature 分支工作。
- 并行或主树脏时，在 `$FLORI_WORKING_DIR/wt/<slug>` 创建短期 worktree；每个 worktree 使用独立 target/tmp，不执行共享 `cargo clean`。
- 只有三个以上真正独立节点才创建多 Agent DAG。

主 Agent 在启动子 Agent 前必须给出：

1. 所属 WP、依赖和一个可观察价值。
2. 允许修改路径和明确非目标。
3. contract revision 与 fixture。
4. 新增架构原语预算。
5. 验证命令、首个有效产物期限和回收条件。
6. 共享热点 owner；Cargo.lock、SQLite schema、Pipeline schema、OpenAPI、前端 router/types 和 CI 同时只能有一个 owner。

子 Agent 只实现、测试和报告，不改产品范围、最终版本、集成分支或生产。主 Agent 汇总 touched paths、运行并集验证、创建正式提交并回收 worktree。

## 提交和记录

- 一个可独立验收、可独立回滚的价值对应一个提交。
- 一个 WP 内的试错用 fixup/checkpoint，push 前整理，不按 Agent 或评审轮次拆提交。
- 普通小任务不建长 worklog；WP、迁移、运维和长调研只维护一份简短记录。
- 版本只在发布候选统一更新一次；普通功能提交不 bump。
- 生产 main 合并、部署、删除和 WP16 冷切换始终需要用户单独批准。

## 三个流程自检

| 场景 | 预期行为 |
|---|---|
| L1 页面修复，执行包声明 0 个架构原语 | Agent 本地迭代后自主 commit、push 和修复本范围 CI，不等待逐轮审批 |
| 为通过类型检查新建重复 DTO 或 `as unknown as` | 编译/CI 拒绝；删除逃逸并回到 Rust 唯一契约，不扩大架构 |
| 在生产删除一个 Source 或修改 Runner token | 转为 `operate`，固定目标、拒绝、回滚与验收并取得单独授权 |

若流程对这三个场景给出其它答案，CLAUDE、Skill 或本文件发生了漂移，必须先修正规则。
