# CLAUDE.md — AI 协作者指南

> 每个 Claude Code 会话开始时读这个文件。

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
docs/adr/                   → 8 个架构决策记录
ROADMAP.md                  → 里程碑和进度
```

## 开发约定

### 代码风格
- Python 3.11+，type hints
- 异步用 asyncio（调度器/API/Worker）
- 同步用 subprocess（步骤脚本调外部工具）
- 配置用 YAML，数据用 JSON
- 日志用 structlog（结构化 JSON）

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

> 不管哪个会话 / agent 提交，都按此格式。与某个 agent 的 harness 默认（如型号后缀、session 链接）不一致时，**以本节为准**（CLAUDE.md OVERRIDE 默认行为）。

**标题**：`<type>(<scope>): <中文摘要>;<版本>`（版本**必带**，见下）
- `type` ∈ `feat / fix / refactor / chore / ops / contract / test / docs / perf / build`（与迭代记录类型对齐）。
- `scope` = 受影响模块/领域，小写：`article / jobs / ui / mcp / net-zone / concept-graph / build`…（尽量带，可省）。
- 摘要用**中文**、一句话说清「做了什么 + 为什么」，**不写句号**，逗号用半角 `,`（沿用既有风格）。
- 动了**对外接口**（API / WebSocket / Redis / 文件 Schema）→ 用 `contract:` 或 `contract(scope):`，并**同提交**更新 `docs/03-contracts.md`。

**版本号**：单一来源 = `pyproject.toml` 的 `[project].version`（当前值以该文件为准，勿在文档里硬编码；api/scheduler/worker 共用 `shared.version.FLORI_VERSION`，前端从后端取、`package.json` 不跟随）。三档递增：
- **普通改动** → patch（第 3 段）+1；**逢 10 进位**：每段满 10 向前进 1、本段归 0（`0.8.9 → 0.9.0`，`0.9.9 → 1.0.0`）。
- **大的重构** → minor（中间段）+1，patch 归 0（如 `0.8.3 → 0.9.0`）。
- **架构级大重构** → major（第 1 段）+1，后两段归 0（如 `0.8.3 → 1.0.0`）。
- **每个提交都 bump、标题结尾都带 `;<新版本>`**（含 docs/test/chore，无例外）。

**正文 body**：解释「为什么这么改」，不是罗列 diff。建议顺序：
1. 背景 / 动机（用户诉求或问题根因）；
2. 关键改动点（按模块分条，带取舍）；
3. `tests:` 验证了什么（跑了哪些、passed 数）；
4. 动接口时一行 `contract:` 说明已同步 `docs/03-contracts.md`。

**署名 trailer**：正文后**仅一行**，不要 `Claude-Session` URL、不要 `(1M context)` 等后缀：
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
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
- 每完成一个模块提交 git + 更新 ROADMAP

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

### 开发 / 测试 / 交付节奏（全容器内,宿主不装依赖）
- 开发热更新：`docker compose -f docker-compose.dev.yml up -d`
- 容器内测试（全量）：`docker compose -f docker-compose.test.yml run --rm test`（本地少用,全量回归交给 CI）
- **本地快测 + CI 异步回归 + 即时部署**（提速节奏,务必遵守）：
  1. 本地只跑【新增 / 直接相关】用例（不跑全量）：
     `docker compose -f docker-compose.test.yml run --rm test pytest tests/test_<新模块>.py -p no:cacheprovider -q`（前端同理指定 vitest 文件）。
     全量回归由 **CI 承担**：push/PR 自动跑后端全套 + 覆盖率门(75%) + schemathesis + 前端 vitest（`.github/workflows/ci.yml`）。
  2. **「本地完成」判定** = 新增用例绿 + 本地 build 对应镜像 +（API 调用 或 Playwright MCP）手验通过。三者绿即算完成。
  3. 完成即：① 按 §提交规范 commit+bump → push main（触发 CI 全量回归,异步）；② **部署**：NAS 即时 recreate 对应容器（本地 build-uptest 镜像,不等 CI）；ECS 边缘**无手动直传**——git push → CI 建 ghcr → Watchtower（120s 轮询）自动 pull+重建（单一路径,不回退,见 §部署）。
  4. **CI 后台红灯 = fix-forward（默认）**：看失败用例 → 补 `fix(scope): …;<版本>` 提交修正 → push 复跑；**不回退**已部署版本。仅当线上功能明显坏才 `scripts/rollback.sh` 回滚。

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
### 迭代工作记录（每次开发/运维都要保持的习惯）
**粒度铁律：一个 commit 一个 txt**（不论大小开发，每个提交都建一个工作项 txt，记录产出该提交的 worktree 开发行为；一个 commit 含 N 个开发步骤进 §7，一个任务的多个 commit 则各自一个 txt）。文件在 `.local/processing/<YYYY-MM-DD>/`，**边做边更新**（不只动手不记录）：
- 命名 `NN-类型-简述.txt`（类型对齐 git：feat/fix/refactor/chore/ops/research/plan/docs/test）。
- 头部：类型 / 状态（计划→进行中→已完成/阻塞）/ 创建·开始·结束·耗时（绝对时间 `YYYY-MM-DD HH:MM`）/ 分支·提交。
- 正文（**详写、分节，不是一行**）：背景 → 计划（动手前写）→ 实际实现（与计划差异、踩坑）→ 涉及改动 → 验证 → 遗留 → **步骤时间线**（§7 必写：每个开发步骤都详记 `开始(date) → 结束(date) + 做了什么(详)`；一个 commit 常含 N 步，仅出提交的那步标 commit sha）。
- 当天建 `00-当日索引.txt`；跨天未完的滚动进 `.local/processing/待办池.txt`。
- 标准/模板/待办池放 `.local/processing/` 根目录（长存,不随日期清理）；完整规范见 `.local/processing/迭代记录规范.txt`。

### 调研结论即写盘（防 context 重复调研，省 token）
调研/排查出的**非显然结论**别只留在对话里——一压缩就丢，下个会话又重查一遍（拖慢节奏 + 烧 token）。**研完即落盘，写在对的层**：
- 非显然代码事实/坑（"X 靠 Y 实现"、"配置在 Z"、某 gotcha）→ **auto-memory**（`/remember`；每会话作为 system-reminder 重载，扛压缩）。
- 稳定架构事实 → **CLAUDE.md / docs/ADR**；本次开发过程/发现 → **`.local/processing` worklog §3/§4/§7**。
- 广搜（"X 在哪 / 找所有调用方 / Y 怎么流转"）→ 派 **Explore / general-purpose 子 agent 或 fork**，只把**结论**带回主 context（文件 dump 不进主 context，不触发压缩），结论再写盘。
- 原则：花了 >2 分钟、之后还要用的结论，**移到下一步前先写盘**；代码引用一律 `file:line` 便于精准重读，不整文件重扫。
- 全程在 `.local/`（gitignored,永不入 git）；值得长存的决策升格 `docs/adr/`,接口变更进 `docs/03-contracts.md`。

