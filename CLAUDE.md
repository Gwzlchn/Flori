# CLAUDE.md — AI 协作者指南

> 每个 Claude Code 会话开始时读这个文件。
> 仓库跟踪 `AGENTS.md -> CLAUDE.md` 供 Codex 读取；规则只维护本文件这一份，禁止复制双真源。

## 项目

AI 辅助的个人学习知识库。把视频/论文/文章自动转化为结构化笔记，积累为可检索的知识体系。

## 文档体系

```
docs/README.md              → 文档大纲（先读这个）
docs/00-vision.md           → 为什么做、不做什么
docs/01-architecture.md     → 系统全景图 + 部署拓扑
docs/02-domain-model.md     → 领域模型 + 状态机 + DB Schema
docs/03-contracts.md        → API / WebSocket / Redis / 文件 Schema
docs/04-module-design/      → 各模块详设（10 个文件）
docs/05-content-adapters.md → 内容适配器（视频/论文/文章）
docs/06-prompt-engineering.md → Prompt 工程
docs/07-security.md         → 安全
docs/08-deployment.md       → 部署（单机/分层/GPU 接入）
docs/09-testing.md          → 测试
docs/10-observability.md    → 可观测
docs/11-dev-workflow.md     → 开发流程 + 并行会话
docs/12-cicd.md             → CI/CD & 发布（GitHub Actions + 镜像）
docs/13-dependencies.md     → 开源依赖（工具选型 / License）
docs/14-comment-and-doc-style.md → 注释与文档风格（权威版）
docs/adr/                   → 架构决策记录
ROADMAP.md                  → 里程碑和进度
```

## 开发约定

### 代码风格
- Python 3.11+，type hints
- 异步用 asyncio（调度器/API/Worker）
- 同步用 subprocess（步骤脚本调外部工具）
- 配置用 YAML，数据用 JSON
- 日志用 structlog（结构化 JSON）

### 注释与文档风格（速查；权威 + 反例对照在 `docs/14-comment-and-doc-style.md`，所有会话必须遵守）
- 注释讲 why/坑/不变量/边界，用直陈短句；不翻译代码，不复述 docs 已有内容（一行指向 `docs/xx §y` 或 `ADR-00NN`），不讲已删的旧设计（历史在 git）。
- 禁装饰：box-drawing 分隔线（`# ── x ──`/`════`）、★ ● ✅ ① ② 等符号、【】当强调、「」『』当强调引号（「」仅可引用真实 UI 文案）。注释与 docstring 的标点一律半角 `() : , ;`。
- 一句话最多一个补充括号，括号里不嵌因果链，超了就拆句。
- 版本号/阶段码/审计号/commit sha/日期不进注释（进 git log 与 worklog）；注释掉的死代码直接删。
- docstring：模块级首行一句中文点题；函数级写做什么 + 坑 + 边界返回；函数名已自明就省略，不写复述式。FastAPI 路由 docstring 进 OpenAPI，改写不删。
- TODO 格式 `# TODO: 做什么 + 触发条件`，保持低量。

### 架构规则
- **全程容器化**：开发、测试、部署全在 Docker 内，宿主机不装任何 Python/Node 依赖
  - 开发：`docker compose -f docker-compose.dev.yml up`（挂载源码热更新）
  - 测试：`docker compose run --rm test pytest`（容器内跑测试）
  - 部署：`docker compose up -d`（生产镜像）
  - 每个模块有 Dockerfile，最终产物是可直接部署的镜像
- **镜像源**：Dockerfile 中 pip 使用 USTC 源（`-i https://mirrors.ustc.edu.cn/pypi/web/simple`），apt 同理
- **文件是接口**：步骤间通过 JSON/MD 文件通信，不共享内存
- **幂等**：每步检查输入指纹，输入没变就跳过
- **故障隔离**：单任务失败不影响其他任务
- **配置与代码分离**：领域知识在 YAML/Prompt 文件里，不硬编码

### 不做的事
- 不用 ORM（SQLite 直接用 sql）
- 不用消息中间件（Redis Streams 够用）
- 不用 Kubernetes（Docker Compose 够用）
- 不做国际化（中文为主）
- 不做用户系统（个人工具，Basic Auth）

### 提交规范（跨会话 / 多 agent 统一，**权威在此处**）

> 最终进入 `main` 的正式提交按本节执行。分支 checkpoint 只用于保存可恢复进度，合入前必须整理，不按正式提交发布。与某个 agent 的 harness 默认（如型号后缀、session 链接）不一致时，**以本节为准**（CLAUDE.md OVERRIDE 默认行为）。

**交付单元与提交边界**
- `main` 上一个正式提交对应一个可独立验收、可独立回滚、部署后完整可用的价值单元。提交边界由验收与回滚边界决定，不由 agent、文件数、代码行数或反馈轮次决定。
- 同一功能的多 agent 实现、多轮评审和 UI 调整留在同一交付单元。只有契约、部署顺序或回滚边界确实独立时才拆分。
- 实现与回归测试、对外接口与 `docs/03-contracts.md`、数据迁移与消费方必须在同一交付单元闭环，不得把不可 build、不可测或不可部署的中间状态留在 `main`。
- 分支允许 `wip:` / `fixup!` checkpoint。checkpoint 不 bump 版本、不进入 `main`；integrator 合入前必须用 squash 方式把全部 checkpoint 整合为待提交 diff，通过价值门后再创建一个正式提交。紧急生产修复、独立 revert 和独立 CI 修复可形成小而完整的正式提交。

**集成责任**
- 每个交付单元指定唯一 integrator。integrator 负责整合 diff、并集验证、契约与文档同步、最终版本号、正式提交、push、部署和 worktree 回收。
- 子 agent 默认只在已登记的租约 worktree 和文件 scope 内实现、测试并报告结果；可创建 branch checkpoint，不得自行修改最终版本号、合入 `main`、push 或部署，除非被明确指定为 integrator。
- `pyproject.toml`、`docs/03-contracts.md`、`shared/db.py`、`shared/models.py`、`configs/pipelines.yaml`、前端 router/types、CI 和 deploy 文件属于共享热点。一个交付单元内为每个热点指定单一 owner，其他 agent 通过 integrator 协调。
- 集成前由 integrator 汇总全部 touched paths，运行并集相关测试、build 和手验；子 agent 各自绿灯不等于交付单元已通过。

**正式提交前价值门**
- integrator 必须能明确回答：本提交交付的完整价值是什么，哪条测试/手验证明它，回滚是否能完整撤销该价值。
- 若仍有本交付单元内的测试、契约、消费方、迁移或文档留到下一提交，或本次只是一轮反馈/保存点，则继续迭代，不创建正式提交。

**标题**
- 改变可部署产品、对外契约、运行时配置或构建产物的发布交付 commit：`<type>(<scope>): <中文摘要>;<新版本>`。
- 仅改文档、公约、调研、测试或 CI，且不改变构建产物与运行时行为的非发布治理 commit：`<type>(<scope>): <中文摘要>`，不带版本。
- `type` ∈ `feat / fix / refactor / chore / ops / contract / test / docs / perf / build`（与迭代记录类型对齐）。
- `scope` = 受影响模块/领域，小写：`article / jobs / ui / mcp / net-zone / concept-graph / build`…（尽量带，可省）。
- 摘要用**中文**、一句话说清「做了什么 + 为什么」，**不写句号**，逗号用半角 `,`（沿用既有风格）。
- 动了**对外接口**（API / WebSocket / Redis / 文件 Schema）→ 用 `contract:` 或 `contract(scope):`，并**同提交**更新 `docs/03-contracts.md`。

**版本号**：单一来源 = `pyproject.toml` 的 `[project].version`（当前值以该文件为准，勿在文档里硬编码；api/scheduler/worker 共用 `shared.version.FLORI_VERSION`，前端从后端取、`package.json` 不跟随）。三档递增：
- **普通改动** → patch（第 3 段）+1；**逢 10 进位**：每段满 10 向前进 1、本段归 0（`0.8.9 → 0.9.0`，`0.9.9 → 1.0.0`）。
- **大的重构** → minor（中间段）+1，patch 归 0（如 `0.8.3 → 0.9.0`）。
- **架构级大重构** → major（第 1 段）+1，后两段归 0（如 `0.8.3 → 1.0.0`）。
- 只有最终进入 `main` 的发布交付 commit 才 bump 一次并在标题结尾带 `;<新版本>`。同一交付单元无论包含多少 agent、checkpoint 或评审轮次，只在最终提交 bump 一次。
- branch checkpoint 与非发布治理 commit 不修改版本。发布与否按是否改变产物或运行时行为判定，不按 commit type 机械判定。

**正文 body**：解释「为什么这么改」，不是罗列 diff。建议顺序：
1. 背景 / 动机（用户诉求或问题根因）；
2. 关键改动点（按模块分条，带取舍）；
3. `tests:` 验证了什么（跑了哪些、passed 数）；
4. 动接口时一行 `contract:` 说明已同步 `docs/03-contracts.md`。

**署名 trailer**：正文后**仅一行**，不要 `Claude-Session` URL、不要 `(1M context)` 等后缀：
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

非发布治理示例：
```
chore(workflow): 以交付单元收敛开发与发布治理
```
（提交的 agent 非 Opus 时，把型号换成对应模型名，如 `Claude Sonnet 4.6`；其余格式不变。）

**示例**：
```
feat(article): 非中文文章自动翻译步(忠实全文译文 + 独立「译文」tab);0.8.0

英文等非中文文章希望有中文翻译。新增条件 AI 步,与 04_smart 正交——这里是忠实全文翻译。
- 02_parse_article:检测正文主语言写 parsed.json.lang;非中文额外标记 needs_translation。
- 新步 04_translate_article:忠实翻译 original.md → translated.md(保留 MD 结构 + 图位)。
- 前端 JobDetailView:hasTranslation → 独立「译文」tab。
tests:TestArticleLangDetect + TestTranslateArticleStep;article 步 39 passed。
contract: docs/03-contracts.md 更新 article 链 + lang 字段 + translated.md。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| CPU | 4 核 | 6+ 核 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 50 GB | 500 GB+ |
| Docker | 20.0+ | 最新稳定版 |
| Claude | CLI 可用（订阅或 API） | 订阅 Max |
| 网络 | 能访问 Claude API | 代理可选 |
| GPU | 不需要（可选） | NVIDIA 8GB+ 显存 |

## 项目目录结构

```
flori/
├── CLAUDE.md                    # 本文件（AI 协作者指南）
├── ROADMAP.md                   # 里程碑和进度
├── README.md                    # 项目说明
├── pyproject.toml               # Python 包定义 + 依赖
│
├── docker/
│   └── base.Dockerfile          # 基础镜像（所有 Python 服务共用）
├── docker-compose.yml           # 生产部署
├── docker-compose.dev.yml       # 开发（挂载源码热更新）
├── docker-compose.test.yml      # 测试
│
├── shared/                      # 共享层（所有模块依赖此层）
│   ├── models.py                # 数据模型 + 枚举
│   ├── errors.py                # 错误层级 + 重试策略
│   ├── config.py                # 配置加载
│   ├── db.py                    # SQLite 数据库层
│   ├── redis_client.py          # Redis 客户端封装
│   ├── storage.py               # StorageBackend (Local/Remote)
│   ├── ai_gateway.py            # AI Gateway (多 Provider)
│   └── step_base.py             # StepBase 基类
│
├── scheduler/                   # 调度器（M1 实现）
├── worker/                      # Worker 主循环（M1 实现）
├── api/                         # FastAPI 服务（M1 实现）
├── steps/                       # 步骤脚本（从原型迁移）
├── frontend/                    # Vue3 前端（M1 实现）
│
├── configs/                     # 运行时配置
│   ├── pipelines.yaml           # 步骤链定义
│   ├── pools.yaml               # 资源池配置
│   └── providers.yaml           # AI Provider 配置
│
├── tests/                       # 测试（容器内运行）
│   ├── conftest.py
│   └── test_*.py
│
├── docs/                        # 设计文档（00-13 + adr/）
└── LOCAL.md                     # 本地开发笔记（不入 git）
```

**Docker 镜像策略**：所有 Python 服务（API、调度器、Worker）共用一个基础镜像 `docker/base.Dockerfile`，启动命令不同。一次 build，所有服务用。

## 开发方式

- 全部在本地 Docker 开发（localhost），部署时改 `.env`
- 一个里程碑可以开多个并行 Claude 会话（基础设施/业务/前端）
- 步骤代码独立开发+验证，用已有产物做测试输入
- 每完成一个可独立验收与回滚的交付单元，由 integrator 整合验证后创建正式提交；功能状态变化时同步更新 ROADMAP

## 目录与开发/运行规约（2026-06-22 治理后,务必遵守）

### 命名
- 品牌 **Flori**（README/文档/UI 标题）；技术标识符全小写 `flori`（仓库/包/镜像/容器/卷/CLI）；env 前缀 `FLORI_`。GitHub 仓库 `Gwzlchn/Flori`。

### 目录布局（顶层契约）
- 入 git：`api/ shared/ scheduler/ worker/ steps/ frontend/ configs/ docker/ deploy/ scripts/ tests/ docs/` + 根级 `*.md / pyproject.toml / docker-compose*.yml / .github/ / .gitignore / .dockerignore / .env.example`。
- **禁 `_前缀` 顶层目录**。本地专用 → `.local/`（gitignored）；可分享部署配方 → `deploy/`（入 git,密钥用 `${ENV}` 外置 + `.env.example`）。
- **永不入 git**：运行时数据（`data/ inbox/ output/ backups/` + Docker 命名卷）、密钥（`.env`、`deploy/**/.env`、`deploy/tunnel/ssh/`）。
- `inbox/` = local_dir 订阅监听目录（丢文件即入库）；`.local/processing/<日期>/` = 每次迭代工作日志（规范见该目录根的 `迭代记录规范.txt`）。

### 运行时数据
- 容器内统一 `/data`；NAS 生产用 bind：`FLORI_DATA_DIR=/volume2/DATA/flori`、`MINIO_DATA_DIR=…/minio`、MinIO bucket `flori`；临时产物 `/tmp/flori-work`。**数据永不放进仓库目录树**。（2026-06-24 从 HDD `/volume1` 冷迁到 **NVMe `/volume2`**；Docker 本体[镜像/命名卷如 redis]早在 /volume2。）

### 测试规约（唯一入口 `scripts/test.sh` —— 跨会话/多 agent 统一,权威在此）

> 所有 agent / 会话跑测试【一律走 `scripts/test.sh`】,**不要各写 `docker compose run …`**（命令漂移 + 漏 `-n auto` = 慢）。全容器内(宿主不装依赖),用【常驻热容器 flori-test-warm】免每次启停税;标准 flags(`-p no:cacheprovider -m 'not fuzz' -n auto`)已烤进脚本。

- **入口子命令**：
  - `scripts/test.sh -m <模块>`  → 只跑相关模块(本地快测,**默认**)。
  - `scripts/test.sh --changed`  → 只跑受本次改动影响的用例(pytest-testmon,迭代秒级)。
  - `scripts/test.sh --all`      → 全量 + 覆盖率门 75%(对齐 CI)。
  - `scripts/test.sh --fe [参数]` → 前端 vitest;`--rebuild`(改了 pyproject `[test]` 依赖后重建镜像)、`--down`(收热容器)。
  - `scripts/test.sh --integration` → 真 Redis、生产 Database 冷启动/迁移/DR 兼容、Docker daemon、Gateway Worker、pipeline 检索闭环和 AOF 恢复门。
  - `scripts/test.sh --external <场景|all>` → 显式公网 article / audio / RSS / YouTube 验证;缺 URL 返回非零，不计为通过。
- **本地/CI 分工**：本地只跑【新增/相关】用例(`-m` 或 `--changed`);**全量回归 + 覆盖率门(75%) + 前端 vitest 交 CI**（`.github/workflows/ci.yml`:main 按 Dockerfile + 去版本 pyproject 内容键复用无源码测试 runtime，普通测试在 collection 前按完整文件预分 14 组，worker 1 分片 + 真依赖 integration 两分组与 runtime prepare 并行启动，拉取后固定本地 RepoDigest 并挂当前源码 → `coverage-gate` 合并判门 → 候选镜像提升;PR 仍在各 runner 本地构建测试 stage;纯文档提交 `paths-ignore` 跳 CI;路径分类从最近完整成功 run 累计到当前 HEAD，前端-only 改动不重建后端镜像;同 ref 新 run 只取消旧测试/build，已启动的镜像发布不取消;schemathesis 独立每日 cron `fuzz.yml`）。

### 开发 / 测试 / 交付节奏（全容器内,宿主不装依赖）
- 开发热更新：`docker compose -f docker-compose.dev.yml up -d`
- 容器内测试：**唯一入口 `scripts/test.sh`**（见上 §测试规约;全量 `scripts/test.sh --all` 本地少用,全量回归交 CI）
- **本地快测 + CI 异步回归 + 即时部署**（提速节奏,务必遵守）：
  1. 本地只跑【新增 / 直接相关】用例（不跑全量）：**`scripts/test.sh -m <新模块>`**（或 `--changed` 只跑受影响用例;前端 `scripts/test.sh --fe`）。见 §测试规约。
     全量回归由 **CI 承担**：push/PR 自动跑后端普通文件级 14 分片 + worker 1 分片 + 真依赖 integration 两分组 + 覆盖率门(75%) + 前端 vitest（`.github/workflows/ci.yml`);schemathesis 独立每日 cron。
  2. **「子任务完成」判定** = 约定 scope 实现完成 + 相关用例绿 + 改动与验证清单已报告。子 agent 完成不等于交付单元可发布。
  3. **「交付单元完成」判定** = integrator 已整合全部 diff + 并集相关测试绿 + 本地 build 对应镜像 +（API 调用 或 Playwright MCP）手验通过 + 必要契约/文档已同步。
  4. 交付单元完成后：① integrator 按 §提交规范创建一个正式提交（发布交付才 bump）→ push main（触发 CI 全量回归,异步）；② **部署**：NAS 即时 recreate（本地 build-uptest 镜像,不等 CI）。★**后端发布交付一律三件套全建全滚**（`scripts/build-uptest.sh scheduler api worker` + recreate 全部后端容器）——共享 shared/ 且全系统单一版本,只滚"改到的那个"必致版本漂移（scheduler/api 停旧版、/system 报 worker 版本漂移,踩过两回）；前端-only 发布交付才可只滚 frontend。ECS 边缘**无手动直传**——git push → CI 建 ghcr → Watchtower（120s 轮询）自动 pull+重建（单一路径,不回退,见 §部署）。
  5. **CI 后台红灯**：尚未进入 `main` 的问题并回当前交付单元，整理 checkpoint 后再验证，不制造主线微型修复提交；已部署版本默认 fix-forward，以小而完整的 `fix(scope): …;<版本>` 发布交付修正，仅当线上功能明显坏才 `scripts/rollback.sh` 回滚。

### 本地活栈（NAS,override 叠加）
```
docker compose -f docker-compose.yml -f .local/docker-compose.uptest.yml --env-file .env \
  --profile distributed up -d --scale worker-cpu=0 --scale worker-ai=0
```
- ★`.env` 必须 `IMAGE_TAG=uptest`（用本地镜像,否则去拉不存在的 `flori:latest` 被代理 reset）；base `worker-cpu/ai` 缩到 0（由 uptest 的专用 worker：claude×2/nas-cpu/foreign-dl/nas-dl 替代）。
- 容器命名 `flori-*`；改源码/镜像后重建对应容器。

### 部署（边缘 ECS）
- `deploy/edge`（Caddy 反代 + basic_auth + 前端）+ `deploy/tunnel`（反向 SSH 隧道,外部网络 `flori_default`）。
- 边缘前端**全自动 CD**：git push → CI 建 ghcr 公开镜像 → Watchtower（120s）自动 pull+重建（frontend `watchtower.enable=true`；nginx base 层缓存,增量拉快,**无手动直传/暂停**）。登录凭证在 `.local/ops/flori-access.txt`（用户名 `flori`）。

### GitHub / 网络（NAS 特例）
- NAS shell 推 GitHub 须**清代理 env**：`env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY git push`（SSH 直连可用；HTTP 代理 11081 对 github/ghcr 不稳）。
- push main 后 CI 自动构建并推 `ghcr.io/<owner>/flori` 镜像；Watchtower 跟随更新。

### 单一来源 / 防漂移
- 依赖只在 `pyproject.toml`（optional extras）；Dockerfile/CI 按 extras 名装,勿重抄版本。
- 改任何对外接口 → 同提交更新 `docs/03-contracts.md`（commit 用 `contract:` 前缀）。

### Worktree 租约制（并行开发必守）
- 多 agent 并行、主工作树已有未提交改动、或需要隔离风险时,代码改动必须使用租约制 worktree。
- Worktree 和临时目录规范不得写入个人绝对路径。用 `$REPO` 表示仓库根目录,用 `$FLORI_WORKING_DIR` 表示仓库外工作区。本机真实路径只放 `.local/` 或 shell 环境,不进入 git 文档。
- worktree 统一放 `$FLORI_WORKING_DIR/wt/<slug>/`;临时产物放 `$FLORI_WORKING_DIR/tmp/`;`.local/` 只放工作日志、登记和归档 diff,不放活跃 worktree 本体。
- 创建 worktree 前,必须在本次工作项头部记录验收目标、integrator、`branch`、`worktree path`、`base commit`、文件 scope、共享热点 owner、测试责任、合并方式和预计回收条件。
- 多 agent 不得同时操作同一 git worktree、Docker build tag、容器、版本号或部署资源。子 agent 若使用 checkpoint，integrator 必须在进入 `main` 前全部 squash，原 checkpoint 不得进入主线历史。
- 合入 `main` 后必须立刻回收 worktree 和分支。最终正式提交所在分支已 fast-forward/merge 进入 `main` 时用 `git branch -d <branch>`；checkpoint 分支因 squash 不建立祖先关系，必须先确认其 diff 已完整纳入最终 `main` SHA、worktree 无未归档改动，再用 `git branch -D <checkpoint-branch>`。本任务创建的远程分支已纳入后同步删除;`badges`、`mutation-data` 等自动数据分支例外。
- 最终回复前必须复查 `git worktree list --porcelain`、本交付单元登记的全部分支（`git branch --list <branch>`，并辅以 `git branch --merged main` / `--no-merged main`）、`git status --short --branch`。若 worktree 或本任务分支未清理,必须说明原因并写入 `.local/processing/待办池.txt`。
- 脏 worktree 删除前,先把 `git status`、`git diff` 和必要的 `git log main..branch` 归档到 `$REPO/.local/processing/<YYYY-MM-DD>/worktree-archive/`。

### 迭代工作记录（每次开发/运维都要保持的习惯）
**粒度铁律：一个交付单元一个 txt**。一个工作项可覆盖多 agent、多轮评审和多个 branch checkpoint；全部步骤写入同一份时间线。checkpoint 只记在时间线，头部在集成完成后登记唯一的最终 `main` SHA。若确有独立验收/回滚边界而拆成多个正式提交，也同步拆成多个工作项。文件继续放在 `.local/processing/<YYYY-MM-DD>/`，**边做边更新且永不入 git**：
- 命名 `NN-类型-简述.txt`（类型对齐 git：feat/fix/refactor/chore/ops/research/plan/docs/test）。
- 头部：类型 / 状态（计划→进行中→已完成/阻塞）/ 创建·开始·结束·耗时（绝对时间 `YYYY-MM-DD HH:MM`）/ 分支·提交。
- 正文（**详写、分节，不是一行**）：背景 → 计划（动手前写）→ 实际实现（与计划差异、踩坑）→ 涉及改动 → 验证 → 遗留 → **步骤时间线**（§7 必写：每个开发步骤都详记 `开始(date) → 结束(date) + 做了什么(详)`；checkpoint 与最终提交均在对应步骤标 SHA）。
- 当天建 `00-当日索引.txt`；跨天未完的滚动进 `.local/processing/待办池.txt`。
- 标准/模板/待办池放 `.local/processing/` 根目录（长存,不随日期清理）；完整规范见 `.local/processing/迭代记录规范.txt`。

### 调研结论即写盘（防 context 重复调研，省 token）
调研/排查出的**非显然结论**别只留在对话里——一压缩就丢，下个会话又重查一遍（拖慢节奏 + 烧 token）。**研完即落盘，写在对的层**：
- 非显然代码事实/坑（"X 靠 Y 实现"、"配置在 Z"、某 gotcha）→ **auto-memory**（`/remember`；每会话作为 system-reminder 重载，扛压缩）。
- 稳定架构事实 → **CLAUDE.md / docs/ADR**；本次开发过程/发现 → **`.local/processing` worklog §3/§4/§7**。
- 广搜（"X 在哪 / 找所有调用方 / Y 怎么流转"）→ 派 **Explore / general-purpose 子 agent 或 fork**，只把**结论**带回主 context（文件 dump 不进主 context，不触发压缩），结论再写盘。
- 原则：花了 >2 分钟、之后还要用的结论，**移到下一步前先写盘**；代码引用一律 `file:line` 便于精准重读，不整文件重扫。
- 全程在 `.local/`（gitignored,永不入 git）；值得长存的决策升格 `docs/adr/`,接口变更进 `docs/03-contracts.md`。

## 已迁移项目记忆（Claude Code / Codex 共用）

以下来自历史 memory，作为跨会话稳定约定与踩坑记录。真实凭证不写入本文件。

### 规则与偏好
- 版本递增按三档执行：普通改动 patch +1（逢 10 进位），大的重构 minor +1，架构级重构 major +1。只有最终进入 `main` 的发布交付 commit 才 bump `pyproject.toml [project].version` 并在标题带 `;<新版本>`；branch checkpoint 与非发布治理 commit 不 bump。
- 提交格式以本文件「提交规范」为准，覆盖 agent harness 默认。禁止 `Claude-Session` URL、`(1M context)` 等后缀；正文只保留一行 `Co-Authored-By` trailer。
- 每个交付单元只有唯一 integrator 可创建正式提交、push、部署和回收 worktree。后台 fork/agent 默认只在租约 scope 内实现、测试和报告，不得自走完整发布链。
- 现在是单人开发，`frontend/` 与 `design/` 也归当前 agent，可改功能、设计、UI。对外接口仍以 `docs/03-contracts.md` 为契约；接口变更同提交更新契约文档。
- 每个交付单元维护一份 `.local/processing/<YYYY-MM-DD>/` 工作项：先写计划，再记录多 agent、多轮评审中的实际实现、踩坑、checkpoint、验证、遗留和步骤时间线。当天建 `00-当日索引.txt`，跨天未完滚动到待办池。
- 写工作日志任何时间戳前必须运行 `date '+%Y-%m-%d %H:%M'` 取真实北京时间；不要估算，也不要写未来时间。
- 多 agent 并行时代码改动按上文 Worktree 租约制执行；工作日志始终写 `$REPO/.local/processing/...`，项目文档中的 worktree 路径不写个人绝对路径。
- 用户偏好彻底调研和多 agent 深挖，不以省 token 为优先。调研/审计/计划/过程笔记放 gitignored `.local/`，不要随手进 `docs/` 或提交。
- 仓库整洁优先：不要留下宿主缓存、临时目录、个人化路径、开发阶段痕迹、陈旧设计草案或研究笔记。公开前去个人化，必要时清理历史中的敏感 diff。
- 目录与配置治理：顶层目录遵守本文件「目录布局」；配置单一来源，依赖只在 `pyproject.toml`；本地专用放 `.local/`，可分享部署配方放 `deploy/`。

### 项目知识与运维坑
- 后端 uptest 镜像已拆成多 target 镜像。构建本地 uptest 一律用 `scripts/build-uptest.sh`，不要裸跑 `docker build`；后端共享 `shared/`，后端提交按 scheduler / api / worker 三件套全建全滚。
- CI 测试已按普通/worker 拆分，测试唯一入口仍是 `scripts/test.sh`。测 CI 墙钟不要 rerun 同一个 commit；修复用新 commit 触发。
- 命名卷 `flori-data:/data` 会 shadow 镜像内烤入的 `/data/*`。默认配置、prompts、fallback 资源优先放 `/app` 或代码内回退，不要只依赖镜像里的 `/data`。
- 边缘前端以 ghcr + Watchtower 为真源；不要再用 SSH 手动直传前端绕过。前端回退类问题优先检查 index/cache、运行容器镜像 digest、Watchtower 日志和 ghcr tag。
- ECS Caddy + 反向 SSH 隧道新增路由时，必须同时考虑边缘 Caddy、NAS 后端、tunnel 容器和端口占用。API 容器重启后通常要按正确顺序重启对应 tunnel，避免旧反向端口僵尸连接。
- Flori MCP server 的方向是把知识库作为 MCP 提供给 agent，工具围绕搜索、读取、概念图谱、问答等能力扩展。传输、契约和安全边界要与 `docs/03-contracts.md` / MCP 路由保持同步。
- `flori.wiki` HTTPS 证书来自 DNS-01 续期流程；未备案杭州 ECS 存在 SNI 阻断现实。证书/续期脚本、Caddy 和 tunnel 改动前先查 `.local/acme/` 与部署文件。
- 已上线的“工厂非仓库”能力包括概念图谱、跨源综合问答、概念雷达/周报。相关后端端点、MCP 工具和前端视图要保持一致；图谱深链初开要注意容器尺寸和 canvas 0x0 问题。
- 前端视觉验收用 Dockerized Playwright MCP：`$PLAYWRIGHT_MCP_WRAPPER` 运行官方 `mcr.microsoft.com/playwright/mcp`，`--network host`，挂当前项目到 `/workspace`，输出到 `$FLORI_WORKING_DIR/tmp/playwright-mcp`。常规 UI 验证至少覆盖 3 个 CSS viewport：4K 显示器 `3840x2160`、14 寸 MacBook `1512x982`、iPhone 16 Pro Max `440x956`。
- NAS 部署/重跑：`api`、`scheduler` 是长驻进程，代码改动需重启；步骤子进程通常自动重载。强制重跑某步要理解 `.done` 与 input hash，必要时删对应 done marker 或走专门 rerun API。
- 下载网络路由使用 worker 自动探测的 `net-cn` / `net-global` 区域 tag。旧 `net-proxy` / `net-direct` / `bili` 路由 tag 已废弃；URL 分类在 scheduler enqueue 时决定，代理是 worker 本地问题。
- 并发槽模型是 holder SET，不是裸计数器：`pool:{pool}:holders` / `res:{resource}:holders`，`used = SCARD`，holder 是 `exec_id`。释放用 `SREM`，幂等；查槽不要再看旧 `:count`。
- pre-commit 密钥扫描钩子可能有已知误报，但真实 PAT、AI key、真实主机 IP、NAS 私有路径、MinIO 真密钥不能 `--no-verify` 放过。合并前源分支要自查，因为 merge/ff 不触发 pre-commit。
- worker 重注册报 503 `registration disabled` 通常是 Redis `runner:registration_token` 过期或丢失；从本地 `.local/docker-compose.uptest.yml` 的 worker registration token 重写 Redis 后重启 worker。
- worker 能力由 `--pools` 集合表达，`--type` / `WORKER_POOLS` 已删除。强机需要多能力就显式 `--pools gpu cpu` 等；前端接入新 worker 的命令也必须生成 `--pools`。

### 凭证与本机 secret
- MCP 凭证已迁到 `~/.bashrc`：`CONTEXT7_API_KEY` 和 `GITHUB_COPILOT_MCP_TOKEN`。`~/.codex/config.toml` 只引用环境变量；真实值不要进仓库。
- Playwright MCP 无 secret，使用本机 Docker wrapper `$PLAYWRIGHT_MCP_WRAPPER`。
- Claude worker 认证现在按 worker 独立 home 管理：`$FLORI_WORKER_HOME_ROOT/<name>/` 内含 `.claude/` 凭证副本。临时用某个 worker home 跑批任务前，先 `FORCE=1 scripts/seed-worker-home.sh <name>` 重 seed，并立即跑完，避免 OAuth refresh 轮换导致闲置副本失效。
- B 站字幕下载依赖 `/data/cookies/bilibili.txt`。该文件过期会导致匿名下载、无字幕、降清晰度。刷新时从应用凭证库读取有效 `bili_cookies`，写 Netscape cookie 文件并 `chmod 600`；不要把 cookie 原文写进日志或文档。
- worker registration token 属本地运行 secret：来源在 `.local/docker-compose.uptest.yml`，运行态在 Redis `runner:registration_token`。只记录位置和恢复流程，不记录值。
- 访问线上/边缘的 basic auth、MinIO、隧道、部署密钥等都只放 `.local/`、`.env`、NAS 数据目录或部署机 secret；本文件只记录位置和流程。
