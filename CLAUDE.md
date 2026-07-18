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

### 提交与发布硬边界

> 最终进入 `main` 的正式提交按本节执行。分支 checkpoint 只用于保存可恢复进度，合入前必须整理，不按正式提交发布。与某个 agent 的 harness 默认（如型号后缀、session 链接）不一致时，**以本节为准**（CLAUDE.md OVERRIDE 默认行为）。

**交付单元与提交边界**
- `main` 上每个价值提交对应一个可独立验收、可独立回滚、部署后完整可用的价值单元。提交边界由验收与回滚边界决定，不由 agent、文件数、代码行数或反馈轮次决定。`multi` 末尾或 `single commit-only` 后续晋级发布时,允许额外一个只承载版本与必要发布元数据的 `build(release)` 提交；它对应发布边界，不伪装成新的功能单元。
- 同一功能的多 agent 实现、多轮评审和 UI 调整留在同一交付单元。只有契约、部署顺序或回滚边界确实独立时才拆分。
- 实现与回归测试、对外接口与 `docs/03-contracts.md`、数据迁移与消费方必须在同一交付单元闭环，不得把不可 build、不可测或不可部署的中间状态留在 `main`。
- 分支允许 `wip:` / `fixup!` checkpoint。checkpoint 不 bump 版本、不进入 `main`；integrator 合入前必须用 squash 方式把全部 checkpoint 整合为待提交 diff，通过价值门后再创建一个正式提交。紧急生产修复、独立 revert 和独立 CI 修复可形成小而完整的正式提交。

**执行分流与统一交付协议**
- 先按本次授权选择执行模式，再加载对应流程：`consult` = 只读回答/诊断/审查，不写持久文件、不改外部状态；`change` = 形成可复核修改，停在 commit/push/deploy 前；`ship` = 明确要求 commit、push、CI 或部署；`operate` = 修改运行数据、内容投递状态、凭据、清理目标或生产资源。模式可随用户扩大终点逐级增加，不得从 `consult/change` 擅自推断 `ship/operate`。
- `consult` 只读取回答所需证据，不建工作项、不新选交付 profile、不做无关 Git/worktree/发布盘点，普通咨询不运行产品测试；正式 reviewer 继承被审候选的 risk gate,可复跑风险矩阵、契约、恶意测试和不可核验证据,但不因此转为`change`。若用户明确要求落盘计划或报告，该持久写入从 `change` 开始。
- `change/ship/operate` 使用同一生命周期：定义范围与不变量 → 选择 profile → 必要时登记租约 → 实现/操作 → 定向验证 → 风险审查 → 到达用户指定终点 → 回收本单元资源。单单元是只有一个节点的发布列车。
- 权威按阶段懒加载：`CLAUDE.md` 已在当前上下文时不重复读；文档路由不明才读 `docs/README.md`；涉及 worktree/multi/证据集成才读 `docs/11-dev-workflow.md` 对应章节；进入 commit/CI/部署才读 `docs/12-cicd.md` 对应章节；内容投递/清理才读受影响的 `.local/delivery` 记录。

**交付 profile**
- 只有 `change/ship/operate` 开工前选择三个 profile：规模 `single | multi`，风险 `normal | contract | critical`，发布范围 `review-first | commit-only | ci | full-deploy`。纯运行运维无代码发布时发布 profile 可记为“不涉及”。
- `multi` 必须先画依赖 DAG，登记共享热点 owner、可并行节点、串行链和集成批次；`single` 不需要伪造 DAG。**实际修改或操作**安全边界、数据库迁移、灾备恢复、身份、凭据与权限默认属于 `critical`，必须执行威胁/不变量矩阵、恶意测试和独立审查；只读讨论这些主题仍是 `consult`，只在答案或设计中覆盖相关不变量与恢复边界。
- 若只把上述高风险主题写成持久设计稿,执行模式是`change/review-first`,风险profile用`normal`(设计本身改外部契约时用`contract`),风险门标`critical-target`:记录设计级不变量/威胁/拒绝/回滚/恢复矩阵,实现依赖它之前做一次独立设计审查,但设计稿本身不运行恶意产品测试或发布门。
- `review-first` 在用户确认前不得正式 commit、push 或部署；`commit-only`只形成已授权的本地无版本价值提交,不bump、不push,后续扩大到发布时另做唯一版本bump且不重写价值提交；`ci` 到最终 push 与 required jobs 全绿为止；`full-deploy` 还必须完成本地/NAS、ECS 与外部验证。用户明确授权的终止条件优先，但不得借 profile 降低契约、安全或数据完整性门禁。
- 多单元在`review-first`阶段保留可识别候选,不创建正式价值commit。进入`ship`且用户授权commit后,才在发布分支为各独立回滚边界创建价值commit并按依赖批次集成；全部批次通过后只做一次版本发布、一次push和一次部署。

**集成责任**
- 每个交付单元指定唯一 integrator；`multi` 另指定唯一列车 integrator，负责 DAG、批次集成、最终版本、push、部署和全局回收。单单元时两者是同一人。
- 子 agent 默认只在已登记的租约 worktree 和文件 scope 内实现、测试并报告结果；可创建 branch checkpoint，不得自行修改最终版本号、合入 `main`、push 或部署，除非被明确指定为 integrator。
- `pyproject.toml`、`docs/03-contracts.md`、`shared/db.py`、`shared/models.py`、`configs/pipelines.yaml`、前端 router/types、CI 和 deploy 文件属于共享热点。一个交付单元内为每个热点指定单一 owner，其他 agent 通过 integrator 协调。
- 集成前由 integrator 汇总全部 touched paths，运行并集相关测试、build 和手验；子 agent 各自绿灯不等于交付单元已通过。测试证据必须绑定候选标识、输入、命令、运行配置、依赖镜像和结果；前五项未变才可复用结果。候选标识优先用 checkpoint/tree SHA，未提交的 `review-first` 候选用确定性 diff digest；若候选含 gitignored 持久文件,使用覆盖这些文件的复合摘要。

**正式提交前价值门**
- integrator 必须能明确回答：本提交交付的完整价值是什么，哪条测试/手验证明它，回滚是否能完整撤销该价值。
- 若仍有本交付单元内的测试、契约、消费方、迁移或文档留到下一提交，或本次只是一轮反馈/保存点，则继续迭代，不创建正式提交。

只有进入`ship`并准备正式commit时才读取`docs/11-dev-workflow.md` §4.7的完整标题、版本、body和trailer规则。硬边界始终有效:对外接口与`docs/03-contracts.md`同提交;版本单一来源是`pyproject.toml`;checkpoint和非发布治理不bump;不得附加session URL或上下文后缀;Git身份只使用已配置值。

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
- 入 git：`api/ shared/ scheduler/ worker/ steps/ frontend/ configs/ docker/ deploy/ scripts/ tests/ docs/` + 项目 agent skills `.agents/skills/`（Claude/Codex 共用，Claude 经 `.claude/skills` symlink 读取）+ 根级 `*.md / pyproject.toml / docker-compose*.yml / .github/ / .gitignore / .dockerignore / .env.example`。
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
- **本地/CI 分工**：本地只跑【新增/相关】用例(`-m` 或 `--changed`);**全量回归 + 覆盖率门(75%) + 前端 vitest 交 CI**（`.github/workflows/ci.yml`:main 按 Dockerfile + 去版本 pyproject 内容键复用无源码测试 runtime，普通测试在全量 collection 前预分 15 组；轻文件保持完整，超过平均负载的巨型文件只做局部真实 collection 并按动态 nodeid 分组，组内 xdist 单项调度消除批量预取长尾。worker 1 分片 + 真依赖 integration 两分组与 runtime prepare 并行启动，detect 和 build-images 先占用 prepare 释放槽完成短控制链，coverage gate 随后预热并以当前 run/attempt 的全部成功生产 job 为屏障；制品下载、逐文件非空断言和 coverage combine 全部 fail-closed，判门后才允许候选镜像提升;PR 仍在各 runner 本地构建测试 stage;纯文档提交 `paths-ignore` 跳 CI;路径分类从最近完整成功 run 累计到当前 HEAD，前端-only 改动不重建后端镜像;同 ref 新 run 只取消旧测试/build，已启动的镜像发布不取消;schemathesis 独立每日 cron `fuzz.yml`）。
- **证据复用阶梯**：实现 agent 跑新增/直接相关用例并记录候选标识、输入、命令、运行配置、依赖镜像和结果；reviewer 只复跑风险矩阵、审查新增范围与无法核验的证据；integrator 每个集成批次跑一次 touched-path 并集、跨单元联调和对应镜像；最终 CI 负责全量。前五项未变时不得机械重复同一全量验证，任一项变化则旧结果失效。

### 开发 / 测试 / 交付节奏（全容器内,宿主不装依赖）
- 开发热更新：`docker compose -f docker-compose.dev.yml up -d`
- 容器内测试：**唯一入口 `scripts/test.sh`**（见上 §测试规约;全量 `scripts/test.sh --all` 本地少用,全量回归交 CI）
- `consult` 不因主题涉及代码就自动跑测试；只做回答所需的只读验证。`change/ship/operate` 才按下列验证阶梯执行。
- **本地快测 + 按 profile 集成与发布**（提速节奏,务必遵守）：
  1. 本地只跑【新增 / 直接相关】用例（不跑全量）：**`scripts/test.sh -m <新模块>`**（或 `--changed` 只跑受影响用例;前端 `scripts/test.sh --fe`）。见 §测试规约。
     全量回归由 **CI 承担**：push/PR 自动跑后端普通混合预分 15 片 + worker 1 分片 + 真依赖 integration 两分组 + 覆盖率门(75%) + 前端 vitest（`.github/workflows/ci.yml`);schemathesis 独立每日 cron。
  2. **「子任务完成」判定** = 约定 scope 实现完成 + 相关用例绿 + 改动与验证清单已报告。子 agent 完成不等于交付单元可发布。
  3. **「交付单元完成」判定** = integrator 已整合全部 diff + 并集相关测试绿 + 本地 build 对应镜像 +（API 调用 或 Playwright MCP）手验通过 + 必要契约/文档已同步。
  4. `single` 完成后按发布 profile 收口；`multi`在`review-first`保留候选,进入`ship`且授权commit后才创建不bump的本地价值commit。同一集成批次统一跑跨单元联调和镜像构建,不按每个单元重复全量构建。
  5. `review-first` 停在用户可检查的未提交候选；`commit-only`形成已授权的本地无版本价值提交后停止；`ci` 由 integrator 统一 push 并追踪 required jobs；`full-deploy` 再完成 NAS 与 ECS 验证。后端发布一律三件套全建全滚（`scripts/build-uptest.sh scheduler api worker` + recreate 全部后端容器），前端-only 发布才可只滚 frontend。ECS 仍只走 git push → coverage gate → GHCR → Watchtower。
  6. **CI 红灯**：尚未发布的问题回到对应交付单元或列车，整理后在实验/发布分支 fix-forward，不向 `main` 连续推送试错微提交；已部署版本默认以小而完整的新发布修正，仅当线上功能明显坏才 `scripts/rollback.sh` 回滚。

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
- 每个租约写明首个有效产物期限，默认 10 分钟。有效心跳必须给出 diff/checkpoint、测试或构建进程、已完成的证据，或可复现阻塞；只报告“规划中”不算。首次超时由 integrator 提醒，默认再等 5 分钟仍无证据就中断、归档并重派。已声明的长命令仍在运行且可观察时不算停滞。
- 被上游依赖阻塞的单元最多做一次不超过 15 分钟的轻量 preflight；详细文件审计、实现和正式 review 等依赖稳定后再启动。不要用周期性重扫填满空闲 agent 槽。
- 合入 `main` 后必须立刻回收 worktree 和分支。最终正式提交所在分支已 fast-forward/merge 进入 `main` 时用 `git branch -d <branch>`；checkpoint 分支因 squash 不建立祖先关系，必须先确认其 diff 已完整纳入最终 `main` SHA、worktree 无未归档改动，再用 `git branch -D <checkpoint-branch>`。本任务创建的远程分支已纳入后同步删除;`badges`、`mutation-data` 等自动数据分支例外。
- 最终回复前必须复查 `git worktree list --porcelain`、本交付单元登记的全部分支（`git branch --list <branch>`，并辅以 `git branch --merged main` / `--no-merged main`）、`git status --short --branch`。若 worktree 或本任务分支未清理,必须说明原因并写入 `.local/processing/待办池.txt`。
- 脏 worktree 删除前,先把 `git status`、`git diff` 和必要的 `git log main..branch` 归档到 `$REPO/.local/processing/<YYYY-MM-DD>/worktree-archive/`。

### 迭代工作记录（change / ship / operate）
`consult` 默认不创建工作项；只有用户要求持久报告，或调研结论将被后续实现/运维复用时才落到已有工作项或独立 `research/plan`。`change/ship/operate` 按**一个交付单元一个 txt**记录。一个工作项可覆盖多 agent、多轮评审和多个 branch checkpoint；checkpoint 只记在时间线，头部在集成完成后登记唯一的最终 `main` SHA。文件放在 `.local/processing/<YYYY-MM-DD>/`，边做边更新且永不入 git：
- 命名 `NN-类型-简述.txt`（类型对齐 git：feat/fix/refactor/chore/ops/research/plan/docs/test）。
- 核心字段只保留类型、状态、真实起止时间、执行模式/profile、验收目标、回滚边界、scope、基线、唯一integrator、验证证据、结果和遗留。分支/worktree、agent租约、热点owner与列车账本仅触发时填写；版本/发布/部署字段仅`ship`填；投递三字段仅内容投递或投递Bug填写。
- 正文保留背景、计划、实际、验证、遗留和步骤时间线；时间线只记录改变候选、风险或证据状态的实质里程碑，不为每个搜索、读取或机械命令单独记步骤。所有写入的时间戳仍必须来自当时的 `date`，不得估算。
- 当天建 `00-当日索引.txt`；跨天未完的滚动进 `.local/processing/待办池.txt`。
- 标准/模板/待办池放 `.local/processing/` 根目录（长存,不随日期清理）；完整规范见 `.local/processing/迭代记录规范.txt`。

### 内容投递台账与投递 Bug 闭环
- 内容来源策展、批量投递、清理重投或投递驱动的修复开工前,必须先读`$REPO/.local/delivery/README.txt`、涉及的catalog、batch和Bug记录。长期来源与当前投递状态不得继续写进日期worklog。
- `$REPO/.local/delivery/catalog/<domain>.yaml`按真实domain分片,但属于一套逻辑目录。来源ID和规范化URL全局唯一;subscription是来源的可选属性,不得另建订阅来源类别或平行清单。订阅属性必须固定规范化`source_type`和`source_id`,Bilibili UP使用数字mid,避免URL与mid形成重复集合。
- 全目录迁移或schema变化时对`delivery-state.yaml`做一次全量运行库对账。日常投递前只增量核对目标source、开放Bug、相关current lineage和订阅集合;运行态写操作后立即回填受影响source。`active_subscription`只表示集合启用,不得把`last_sync_status: ok`误当成子Job投递成功。
- batch文件名固定为`YYYY-MM-DD-两位序号.yaml`,日期取创建batch时的真实北京时间,完整时间写入文件。普通batch最多10个来源;用户追加内容必须新建下一序号batch。频道、Playlist和RSS还必须冻结fanout上限与停止条件。
- 投递发现产品Bug时,立即暂停受影响来源并创建独立`$REPO/.local/delivery/bugs/YYYY-MM-DD-两位序号.yaml`;Bug记录必须固定发现batch、source/Job、processing修复工作项、修复版本和重投batch。
- 投递驱动的代码修复另建`fix`交付单元,不并入`ops`投递工作项。只有内容投递、投递Bug和对应修复工作项填写`投递关联`、`投递Bug`、`重投验收`;普通产品开发不添加这三个条件字段。
- 代码测试、commit或部署完成不等于投递Bug关闭。修复后必须新建最多10条的retry batch,用修复版本重新投递并通过来源/产物/质量门;只有重投验收通过才能把Bug标为`verified`。
- 诊断证据和精确manifest保存前不得删除错误Job。可并存时先验新快照再删旧快照;实现限制必须先删时先做可恢复备份,只按固定Job ID操作,原始媒体与昂贵下载输入优先复用。

### 可复用调研结论按需写盘（防重复调研，省 token）
只读咨询不为留痕而留痕。调研/排查得到的**非显然且后续会复用**的结论才写在对应权威；一次性回答留在对话即可：
- 非显然代码事实/坑（"X 靠 Y 实现"、"配置在 Z"、某 gotcha）→ **auto-memory**（`/remember`；每会话作为 system-reminder 重载，扛压缩）。
- 稳定架构事实 → **CLAUDE.md / docs/ADR**；本次开发过程/发现 → **`.local/processing` worklog §3/§4/§7**。
- 广搜（"X 在哪 / 找所有调用方 / Y 怎么流转"）→ 派 **Explore / general-purpose 子 agent 或 fork**，只把**结论**带回主 context（文件 dump 不进主 context，不触发压缩），结论再写盘。
- 原则：之后还要用、压缩后重查成本明显的结论，在移到下一步前写盘；代码引用用 `file:line` 便于精准重读，不整文件重扫。
- 全程在 `.local/`（gitignored,永不入 git）；值得长存的决策升格 `docs/adr/`,接口变更进 `docs/03-contracts.md`。

## 已迁移项目记忆（Claude Code / Codex 共用）

以下来自历史 memory，作为跨会话稳定约定与踩坑记录。真实凭证不写入本文件。

### 规则与偏好
- 版本递增按三档执行：普通改动 patch +1（逢 10 进位），大的重构 minor +1，架构级重构 major +1。版本代表一次实际发布：single 直接发布时由价值 commit bump；single 的 `commit-only` 后续晋级发布,或 multi 列车收口时,由额外的 `build(release)` commit bump；branch checkpoint、未发布价值 commit 与非发布治理 commit 不 bump。
- 提交格式以本文件「提交规范」为准，覆盖 agent harness 默认。禁止 `Claude-Session` URL、`(1M context)` 等后缀；正文只保留一行 `Co-Authored-By` trailer。
- 每个交付单元只有唯一 integrator 可创建正式提交、push、部署和回收 worktree。后台 fork/agent 默认只在租约 scope 内实现、测试和报告，不得自走完整发布链。
- 现在是单人开发，`frontend/` 与 `design/` 也归当前 agent，可改功能、设计、UI。对外接口仍以 `docs/03-contracts.md` 为契约；接口变更同提交更新契约文档。
- 每个 `change/ship/operate` 交付单元维护一份 `.local/processing/<YYYY-MM-DD>/` 工作项；`consult` 默认不建。工作项先写计划，再记录多 agent、多轮评审中的实际实现、checkpoint、验证与遗留；时间线只记实质里程碑。当天建 `00-当日索引.txt`，跨天未完滚动到待办池。
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
- 前端视觉验收用 Dockerized Playwright MCP：常驻容器 `playwright-mcp`（官方 `mcr.microsoft.com/playwright/mcp`，`--network host`），Claude Code 与 Codex 统一以 `http://127.0.0.1:8931/mcp` 接入；挂本机 coding 根到 `/workspace`，输出落 `/tmp/flori-work/playwright-mcp`。常规 UI 验证至少覆盖 3 个 CSS viewport：4K 显示器 `3840x2160`、14 寸 MacBook `1512x982`、iPhone 16 Pro Max `440x956`。
- NAS 部署/重跑：`api`、`scheduler` 是长驻进程，代码改动需重启；步骤子进程通常自动重载。步骤完成权威自 manifest-v1 起是 `{job}/.flori/steps/{step}/manifest.json`（`STEP_COMPLETION_MODE=dual` 迁移期 manifest 优先、`.done` fallback 双写）；强制重跑走 rerun API（会撤销在途 commit、按旧 manifest 精确删输出与 manifest、再删 `.done`），手工只删 done marker 已不再充分。
- 下载网络路由使用 worker 自动探测的 `net-cn` / `net-global` 区域 tag。旧 `net-proxy` / `net-direct` / `bili` 路由 tag 已废弃；URL 分类在 scheduler enqueue 时决定，代理是 worker 本地问题。
- 并发槽模型是 holder SET，不是裸计数器：`pool:{pool}:holders` / `res:{resource}:holders`，`used = SCARD`，holder 是 `exec_id`。释放用 `SREM`，幂等；查槽不要再看旧 `:count`。
- pre-commit 密钥扫描钩子可能有已知误报，但真实 PAT、AI key、真实主机 IP、NAS 私有路径、MinIO 真密钥不能 `--no-verify` 放过。合并前源分支要自查，因为 merge/ff 不触发 pre-commit。
- worker 重注册报 503 `registration disabled` 通常是 Redis `runner:registration_token` 过期或丢失；从本地 `.local/docker-compose.uptest.yml` 的 worker registration token 重写 Redis 后重启 worker。
- worker 能力由 `--pools` 集合表达，`--type` / `WORKER_POOLS` 已删除。强机需要多能力就显式 `--pools gpu cpu` 等；前端接入新 worker 的命令也必须生成 `--pools`。

### 凭证与本机 secret
- MCP 凭证已迁到 `~/.bashrc`：`CONTEXT7_API_KEY` 和 `GITHUB_COPILOT_MCP_TOKEN`。`~/.codex/config.toml` 只引用环境变量；真实值不要进仓库。
- Playwright MCP 无 secret，走常驻容器 `playwright-mcp` 的本机 HTTP 端点；旧 stdio wrapper 脚本仅作回退保留。
- Claude worker 认证现在按 worker 独立 home 管理：`$FLORI_WORKER_HOME_ROOT/<name>/` 内含 `.claude/` 凭证副本。临时用某个 worker home 跑批任务前，先 `FORCE=1 scripts/seed-worker-home.sh <name>` 重 seed，并立即跑完，避免 OAuth refresh 轮换导致闲置副本失效。
- B 站字幕下载依赖 `/data/cookies/bilibili.txt`。该文件过期会导致匿名下载、无字幕、降清晰度。刷新时从应用凭证库读取有效 `bili_cookies`，写 Netscape cookie 文件并 `chmod 600`；不要把 cookie 原文写进日志或文档。
- worker registration token 属本地运行 secret：来源在 `.local/docker-compose.uptest.yml`，运行态在 Redis `runner:registration_token`。只记录位置和恢复流程，不记录值。
- 访问线上/边缘的 basic auth、MinIO、隧道、部署密钥等都只放 `.local/`、`.env`、NAS 数据目录或部署机 secret；本文件只记录位置和流程。
