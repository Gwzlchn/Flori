# 12 · CI/CD & 发布

> GitHub Actions（GitHub-hosted runner）+ ghcr.io 镜像发布；self-hosted runner 可选。

## 1. Pipeline 概览

```
Push/PR to main   → Unit Test（普通 4 shard + worker 2 shard 并行）+ 分支覆盖率门(≥75%) + 前端 vitest
Merge to main     → + Push Image (ghcr.io，构建已与测试并行暖好缓存) → Watchtower 自动拉取重建（CD）
每日 cron          → Schemathesis 模糊(无 5xx，fuzz.yml) / Mutation 变异测试（mutation.yml）
手动触发           → E2E 集成回归（workflow_dispatch）
```

每次 PR/push 跑容器内单测（按普通/worker 拆两组 job 分片并行）+ **分支覆盖率门** + 前端 vitest（见 §4）；纯文档提交(`paths-ignore`)不触发。**Schemathesis 模糊/契约**已拆到独立每日 cron(`fuzz.yml`)，变异测试(`mutation.yml`)也是每日 cron + 可手动；集成回归(`e2e.yml`)是**手动门**(`workflow_dispatch`)。三者都不挂在每个 PR 上以免给主 CI 加负载。

## 2. 镜像发布

```
Registry: ghcr.io/gwzlchn/flori-{api,scheduler,worker,frontend}
Tags:     latest, <git-short-sha>
```

用户一键部署：
```bash
git clone https://github.com/gwzlchn/flori
cp .env.example .env   # 填 API key
docker compose up -d   # 拉公开镜像，不需要本地 build
```

## 3. Runner 选择

默认且当前唯一在用的是 GitHub-hosted runner：公开仓库免费无限分钟，自带 Docker + buildx，跑单测与构建镜像足够、零维护。

self-hosted runner 仅在将来需要本地资源时可选（如用本地视频素材跑端到端验证、国内 USTC 镜像加速）；目前 CI 不依赖它。

安全：公开仓库不要用 self-hosted runner 处理 fork PR（不受信代码可读取 secrets、在你机器上执行）；如需自托管，仅限私仓或 push/已审核 PR 触发。

self-hosted 安装（可选）：
```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/gwzlchn/flori --token <TOKEN>
sudo ./svc.sh install && sudo ./svc.sh start
```

## 4. Workflow 设计

实际实现见 `.github/workflows/ci.yml`（主 CI）+ `fuzz.yml`（Schemathesis，每日 cron）+ `e2e.yml`（集成回归，手动）+ `step-images.yml`（按步执行镜像，手动）+ `mutation.yml`（变异测试，每日 cron + 手动）：

- `unit-normal` / `unit-worker`：push/PR 到 main 触发；纯文档提交(`paths-ignore`)不触发。单测按普通/worker 拆两个 job 并行。普通 job 建轻量 `test` 镜像跑非 step 用例,拆 4 shard;worker job 建含 ffmpeg 与媒体库的 `test-worker` 镜像跑 step/worker 用例,拆 2 shard。分片用 pytest-split 按实测时长均衡。各 shard 产**部分覆盖率**并上传 artifact。本地跑测试统一走 `scripts/test.sh`(见 CLAUDE.md §测试规约),不手写 `docker compose run`。
- `coverage-gate`：下载全部 shard 的部分覆盖率,在 `python:slim` 容器里 `coverage combine` 后判**分支覆盖率门** `--fail-under=75`。低于 75% 直接红,防覆盖率倒退。覆盖率配置(分支/markers)单一事实源在 `pyproject.toml`。
- `fe-test`：容器化 vitest 跑前端单测 + 覆盖率,与后端并行,各自为门。
- `coverage-badge`：仅 main。把前后端覆盖率写成 shields endpoint JSON,force-push 到 `badges` 数据分支,README 徽章读它。
- `fuzz.yml`（每日 cron + 可手动）：**Schemathesis 模糊/契约**,`pytest -m fuzz tests/test_openapi_fuzz.py`。in-process 从 `/openapi.json` 自动派生用例喂每个端点,断言不 5xx(`not_a_server_error` + `response_schema_conformance`,检查集见仓库根 `schemathesis.toml`)。曾借此揪出分页 `offset` 溢出 SQLite int64 的 500 并修复。从 push CI 拆出,不再拖慢每次 push 的关键路径。
- `build-images` / `push-images`：build 与 push 拆成两个 job。`build-images` 与测试**并行**,只构建暖 buildcache 不推送;PR 也构建,用来验 Dockerfile 与 vue-tsc。`push-images` 仅 main、测试通过后跑,命中暖缓存后秒级 build + push。均用 buildx 构 **amd64**（所有目标机均为 x86，不构 arm64）。矩阵四个镜像：`flori-api` / `flori-scheduler` / `flori-worker` 是 `docker/base.Dockerfile` 的不同 target,加 `flori-frontend`。`detect` job 按改动路径过滤,前端-only 改动不重建后端镜像,反之亦然。
- `step-images.yml`：步骤执行镜像（`flori-step-base` / `flori-step-heavy` / `flori-step-gpu`）独立于主 CI，`workflow_dispatch` 手动触发，同样只构 amd64。
- `e2e.yml`（**集成回归门**，`workflow_dispatch` 手动触发，不挂 PR）：补审计缺口 #7 —— 主 CI 只跑单测，缺 pipeline DAG ↔ worker ↔ scheduler ↔ step 的接线回归。含两个互不依赖、可并行的 job：

  **① `integration-smoke` —— 接线健康探针**（用 `docker-compose.integration.yml`，`DRY_RUN=1` 起栈）：
  1. 起 redis/api/scheduler/worker-cpu/worker-ai；
  2. 探活 API（`/openapi.json`，api 无专用 health 端点），确认 api↔redis 连通；
  3. 校验 scheduler/worker 容器存活且未反复重启（catch 导入/接线错误）；
  4. 跑容器内全量单测（与主 CI 同路径）兜底回归。

  **② `paper-e2e` —— 真实素材端到端**（`tests/integration/ci_paper_e2e.sh`，`DRY_RUN=1` 起同一栈）：
  投一个仓库自带的微型 PDF `tests/fixtures/sample.pdf`（~2KB，PyMuPDF 生成，含可抽文本 + 标题 + 多个章节标题 + 一条 `Figure 1:` 图注），走 `POST /api/jobs/upload` 进 **paper** pipeline，轮询到 `done`，断言 `notes/smart`(200) + `review`(200, 合法 JSON) + `sections.json` 非空。**无需任何外部网络 / arXiv / B站 / API key**。这是审计缺口 #7 在 GitHub-hosted runner 上的**实质**覆盖（不止探活，真跑解析链）。
  - **真跑（REAL）**：`01_download`（upload 模式——文件已落 `input/source.pdf`，本步只抽 metadata，不联网）、`02_pdf_parse`（PyMuPDF 解析）、`03_sections`（章节树）、`04_figures`（抽图 + 图注成条）。
  - **合成（SYNTHETIC）**：`05_smart_paper`、`06_review` 经 `DRY_RUN=1` → `DryRunProvider` 返回占位产物（不调真实 AI），但落盘 / 版本化 / 接线全程真实。
  - 脚本用独立 compose 项目名（默认 `flori-ci-paper`）+ 退出 trap `down -v` 拆栈，本地跑也不会误碰生产栈（本地若 8000 被占，需先停占用方或换独立项目；CI runner 干净直接用 8000）。

  **仍是人工/自托管的覆盖**（本 workflow 不跑）：真实**视频** mp4 / 真连 B站·arXiv 联网下载 / **真实 AI** 笔记全链路。`01_download` 对 URL 源会真连 B站/arXiv（`DRY_RUN` 不绕过下载），真实 AI 步需真 API key，GitHub-hosted runner 无网络素材跑不通，只能在装好素材的机器上对**已部署栈**手动执行：
  ```bash
  TEST_VIDEO_FILE=/path/to.mp4 bash tests/integration/run_e2e_cpu.sh           # 下载+CPU 链
  KIMI_API_KEY=... TEST_VIDEO_FILE=/path/to.mp4 bash tests/integration/run_e2e_ai.sh   # 全链路+真实 AI 笔记
  ```

- `mutation.yml`（**变异测试**，每日 cron + `workflow_dispatch` 手动）：对核心模块注入变异,逐个跑相关测试。目标清单在 `scripts/mutation_score.py` 的 `TARGETS`：`shared/ai_gateway.py` 计费与 `exec_id` 去重、`shared/db.py`、`scheduler/` 状态机、`worker/` 乐观锁。**存活变异 = 测试抓不住的真实 bug**——`ai_usage` 去重或乐观锁里若有存活变异 = 字面意义的重复计费/双跑风险。慢 → 不挂 PR;报告态非阻塞:分数写 job summary,并追加到 `mutation-data` 分支的 `history.csv`,再生成趋势与徽章 JSON 供 README 读取。手动可传 `target`(如 `ai_gateway`)只跑子集;只跑子集时不写历史。注:mutmut 3.x 配置键是 `source_paths`(非 v2 的 `paths_to_mutate`)。

部署为自动 CD：生产 `docker-compose.yml` 跑 Watchtower（`containrrr/watchtower`），每 120s 查 ghcr，只更新带 `com.centurylinklabs.watchtower.enable=true` 标签的容器，自动 pull + 重建 + 清理旧镜像。无 SSH 自动部署脚本。

## 5. docker-compose.yml 改造

```yaml
# 生产用：拉远程镜像
services:
  api:
    image: ghcr.io/gwzlchn/flori-api:latest
    # ...
```

```yaml
# 开发用（docker-compose.dev.yml）：本地 build + 挂载源码
services:
  api:
    build:
      context: .
      dockerfile: docker/base.Dockerfile
    volumes:
      - ./shared:/app/shared
    # ...
```

## 6. .env.example

```bash
# === 必填 ===
ANTHROPIC_API_KEY=sk-ant-...    # 或留空用 DRY_RUN
DEEPSEEK_API_KEY=sk-...         # AI 笔记生成
KIMI_API_KEY=                   # AI 笔记生成（Moonshot/Kimi）

# === 可选 ===
API_TOKEN=                      # API 认证 token（留空不鉴权）
HTTPS_PROXY=                    # 代理（不需要可留空）
DRY_RUN=0                       # 1=AI 步骤不调真实 API

# === 高级 ===
FLORI_DATA_DIR=                 # 数据/产物挂载目录；留空=命名卷(prod)/./data(dev)
MINIO_DATA_DIR=                 # MinIO 对象落盘目录；留空=命名卷
```

## 7. GitHub Secrets

| Secret | 用途 |
|--------|------|
| `ANTHROPIC_API_KEY` | 生产环境 |
| `DEEPSEEK_API_KEY` | 生产环境 |

> 推镜像到 ghcr.io 用 Actions 内置 `GITHUB_TOKEN`（`packages: write` 权限），无需额外 secret。CI 单测 job（`unit-normal` / `unit-worker`）跑容器内单测，不需 API key。

## 8. TODO

- [x] 创建 `.github/workflows/ci.yml`（test + amd64 build-push 到 ghcr.io）
- [x] docker-compose.yml 改用 `image: ghcr.io/gwzlchn/flori-*:latest`（拉远程镜像部署）
- [x] docker-compose.yml 接入 Watchtower 自动 CD
- [x] 创建 `.env.example`
- [x] 创建 `.github/workflows/e2e.yml`（手动集成回归门：`integration-smoke` 接线探针 + 单测兜底 / `paper-e2e` 真实素材 paper 链跑到 done）
- [x] 真实素材 paper pipeline E2E 自动化（自带微型 PDF fixture `tests/fixtures/sample.pdf` + `tests/integration/ci_paper_e2e.sh`，无需网络/API key，已并入 `e2e.yml` 的 `paper-e2e` job）
- [x] `test` job 接入**分支覆盖率门**(`--cov-branch --cov-fail-under=75`)+ **Schemathesis 模糊/契约**(`-m fuzz`,`schemathesis.toml`)
- [x] 创建 `.github/workflows/mutation.yml`(变异测试手动门;core 模块 `source_paths` 见 `pyproject.toml [tool.mutmut]`)
- [ ] 首次 push 后到仓库 Packages 确认镜像、Watchtower 自动更新验证
- [ ] 真实素材**视频/AI 全链路** E2E 自动化（需自托管 runner + 固定 mp4 素材 + 真实 API key，当前人工执行）
