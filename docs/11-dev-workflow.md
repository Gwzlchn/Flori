# 11 · 开发流程

> 并行 Claude 会话开发、会话交接、Git 工作流。
> `CLAUDE.md` 是协作规则权威源，仓库跟踪的 `AGENTS.md` 仅是指向它的 Codex 入口。

## 1. 会话拆分

每个里程碑可开多个并行 Claude Code 会话：

```
M1 实现:
├── 会话 A: 基础设施（调度器 + Worker + Redis）
├── 会话 B: API 服务
├── 会话 C: 前端
└── 会话 D: 联调验收
```

### 每个会话只需读

```
会话 A: CLAUDE.md + ROADMAP.md + 04/scheduler.md + 04/worker.md + 04/step-base.md + 03-contracts.md
会话 B: CLAUDE.md + ROADMAP.md + 04/api.md + 03-contracts.md
会话 C: CLAUDE.md + ROADMAP.md + 04/frontend.md + 03-contracts.md
会话 D: CLAUDE.md + ROADMAP.md + 09-testing.md
```

### 为什么能并行

1. **接口已约定**：03-contracts.md 定义了所有 API/Redis/文件格式
2. **步骤解耦**：步骤间通过文件通信，调度器通过 Redis 通信
3. **现成测试数据**：原型产物可做任何步骤的输入
4. **可 Mock**：前端 Mock API，Worker Mock 步骤

## 2. 代码目录结构

```
flori/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
│
├── docker/
│   └── base.Dockerfile     # 多阶段，api/scheduler/worker 共用
│
├── scheduler/              # 会话 A
│   ├── main.py
│   └── scheduler.py
│
├── api/                    # 会话 B
│   ├── main.py
│   └── routes/
│
├── worker/                 # 会话 A
│   ├── main.py
│   ├── worker.py
│   ├── transport.py        # RedisTransport（直连）
│   └── gateway_transport.py # GatewayTransport（出站 HTTPS）
│
├── shared/                 # 会话 A (基础) + B (扩展)
│   ├── step_base.py
│   ├── db.py
│   ├── redis_client.py
│   ├── storage.py
│   └── ai_gateway.py
│
├── steps/                  # 从原型迁移（按 pipeline 分子目录）
│   ├── common/step_01_download.py
│   ├── video/step_02_whisper.py ... step_12_review.py
│   ├── paper/  article/  audio/
│   └── utils/
│
├── configs/
│   ├── pools.yaml
│   ├── pipelines.yaml
│   ├── prompts/            # 模板/风格（templates / profiles / styles）
│   └── domain/
│
└── frontend/               # 会话 C
    ├── package.json
    └── src/
```

## 3. 开发环境

全部在主机 Docker 内，不在宿主机装任何依赖：

```bash
# 启动开发环境
docker compose -f docker-compose.dev.yml up

# docker-compose.dev.yml 不同于 prod:
# - ports 暴露到宿主机（方便调试）
# - 挂载源码目录（代码热更新）
# - 单副本 Worker
# - 不启动公网入口（Caddy + 反向 SSH 隧道仅生产用，见 deploy/edge、deploy/tunnel）
```

```yaml
# docker-compose.dev.yml 差异
services:
  api:
    volumes:
      - ./api:/app/api    # 挂载源码
      - ./shared:/app/shared
      - ${FLORI_DATA_DIR:-./data}:/data
    environment:
      - API_RELOAD=1      # uvicorn 热更新（生产默认关）

  worker-cpu:
    volumes:
      - ./worker:/app/worker
      - ./steps:/app/steps
      - ./shared:/app/shared
      - ${FLORI_DATA_DIR:-./data}:/data
```

## 4. Git 工作流

```
main
  │
  ├── $FLORI_WORKING_DIR/wt/<slug-a>   agent A 租约 scope
  ├── $FLORI_WORKING_DIR/wt/<slug-b>   agent B 租约 scope
  └── integrator 整合 → squash 为候选 diff → 并集验证 → main 正式提交
```

并行会话或主工作树有未提交改动时,每个会话必须在租约制 worktree 中工作。`$FLORI_WORKING_DIR` 是仓库外工作区,本机真实路径只放 `.local/` 或 shell 环境,不要写入 git 文档。

创建 worktree 前先登记到本次工作项:验收目标、integrator、分支、worktree path、base commit、文件 scope、共享热点 owner、测试责任、合并方式和预计回收条件。推荐目录:

```
$FLORI_WORKING_DIR/
├── tmp/            临时产物和 scratch
└── wt/<slug>/      活跃 worktree
```

合入 `main` 后立即回收。最终正式提交所在分支已成为 `main` 祖先时:

```bash
git worktree remove "$FLORI_WORKING_DIR/wt/<slug>"
git branch -d <final-branch>
```

checkpoint 分支经 squash 后不会成为 `main` 祖先。integrator 必须先确认 checkpoint diff 已完整纳入最终 `main` SHA、worktree 无未归档改动，再回收 worktree 并用 `git branch -D <checkpoint-branch>` 删除。本任务创建的远程分支已纳入后同步删除;`badges`、`mutation-data` 等自动数据分支例外。若 worktree 因未合入、脏 diff、用户要求保留或阻塞项不能清理,必须在最终回复说明原因,并写入 `.local/processing/待办池.txt`。

每个交付单元只有一个 integrator。子 agent 默认只在自己的租约 scope 内实现、测试和报告，可创建 `wip:` / `fixup!` checkpoint，但不得自行修改最终版本、合入 `main`、push 或部署。integrator 必须用 squash 方式把全部 checkpoint 整合为待提交 diff，原 checkpoint 不得进入主线历史；对全部 touched paths 重跑并集相关测试、build 和手验并通过价值门后，才创建一个正式提交。

`pyproject.toml`、`docs/03-contracts.md`、`shared/db.py`、`shared/models.py`、`configs/pipelines.yaml`、前端 router/types、CI 和 deploy 文件是共享热点。同一交付单元内每个热点只能有一个 owner，多 agent 不得同时操作同一 git worktree、Docker build tag、容器、版本号或部署资源。

### 提交规范

> **权威定义在 `CLAUDE.md` §提交规范**（交付单元 / integrator / checkpoint / 标题格式 / 版本判定 / body / trailer，跨会话·多 agent 统一）。本节只留示例，规则改动请改 CLAUDE.md，勿在两处各写一份。

```
feat(scheduler): 实现 DAG 推进逻辑;0.2.0
fix(worker): 修复 scene 池未冻结 cpu 的问题;0.2.1
contract(api): 任务队列接口 + 同步 docs/03-contracts.md;0.7.1
chore(workflow): 以交付单元收敛开发与发布治理
```

## 5. 集成测试顺序

```
1. integrator 汇总全部 touched paths 与验收目标
2. 子 agent 在各自 scope 跑相关测试并报告结果
3. integrator 整合全部改动，把 checkpoint squash 为待提交 diff
4. 按 touched paths 跑并集相关测试与跨模块联调
5. 构建受影响镜像并做 API / Playwright 手验
6. 契约、迁移、文档与消费方闭环后进入 main
```

子 agent 的局部测试结果可复用为证据，但不能替代第 4-6 步。跨调度器、Worker、API 和前端的交付单元仍按依赖顺序联调，端到端验收覆盖完整用户路径。

## 6. 每完成一个交付单元

```
1. 在工作项写清验收目标、integrator、scope、共享热点 owner 和回滚边界
2. 子 agent 在租约 worktree 内实现、测试，可按需创建 checkpoint
3. integrator 整合全部改动，把 checkpoint squash 为待提交 diff
4. 跑并集相关测试、构建和手验，全量回归交 CI
5. 同步契约、迁移、消费方、ROADMAP 和必要文档
6. 只有发布交付 bump 一次版本；治理提交和 checkpoint 不 bump
7. integrator 创建一个正式提交，按授权 push、部署
8. 删除已合入 worktree 和本地分支
9. 最终回复前复查 git worktree list --porcelain、本单元登记的全部分支(--list + --merged/--no-merged main)、git status --short --branch
```

## 7. 扩展指南

### 7.1 步骤 DAG 拆分原则

什么时候该拆成两个步骤：

| 条件 | 说明 |
|------|------|
| 资源类型不同 | CPU 密集步骤和 AI 步骤拆开 → 可以并行 |
| 可能独立重跑 | 改了 OCR 阈值不应该重跑场景检测 |
| 耗时差异大 | 快步骤不应被慢步骤阻塞 |
| 中间产物有独立价值 | OCR 结果单独可用 |

什么时候不该拆：
- 始终一起执行、中间产物无独立价值
- 拆了增加 IO 开销（如读写大视频文件）

### 7.2 新增步骤

两步完成，不改框架代码：

```bash
# 1. 写步骤脚本
cat > steps/video/step_13_translate.py << 'EOF'
import json

from shared.step_base import StepBase, file_hash

class TranslateStep(StepBase):
    def validate_inputs(self):
        if not (self.job_dir / "output/transcript.md").exists():
            return ["output/transcript.md"]
        return []

    def input_hashes(self):
        return {
            "transcript": file_hash(self.job_dir / "output/transcript.md"),
            "config": json.dumps(self.config.get("translate", {}), sort_keys=True),
        }

    def execute(self):
        transcript = (self.job_dir / "output/transcript.md").read_text()
        translated = self.ai.call(f"翻译以下内容为英文:\n{transcript}")
        self.artifacts.write("output/transcript_en.md", translated)
        return {"chars": len(translated)}


if __name__ == "__main__":
    TranslateStep.cli_main("13_translate")
EOF

# 2. 在 pipelines.yaml 加入步骤（GitLab-CI 风格：jobs + needs）
# video:
#   jobs:
#     ...
#     "13_translate":
#       run: steps.video.step_13_translate
#       pool: ai
#       needs: ["08_punctuate"]
#       tags: []
#       timeout: 300
#       retry: 2
```

调度器自动识别新步骤的依赖关系，Worker 自动执行。已有 Job 通过 resubmit 即可补跑新步骤。

### 7.3 新增内容来源

识别加在 `shared/source_detect.py`（api 与 steps 共用），下载分支加在 `steps/common/step_01_download.py`，其他步骤不动：

```python
# shared/source_detect.py 里加识别分支
def detect_source(url):
    if "douyin.com" in url:
        return "douyin"
    # ... 已有的识别逻辑

# steps/common/step_01_download.py 里加下载分支
def download_douyin(url, output_dir):
    # yt-dlp 支持抖音
    cmd = ["yt-dlp", url, "-o", str(output_dir / "source.%(ext)s")]
    self.commands.run(cmd)
```

如果新来源的视频格式不同（如竖屏短视频），可以通过 style_tags 标签调整 AI prompt，不需要改 pipeline。

### 7.4 新增内容类型

三步完成：

```bash
# 1. 写内容特有步骤（按 pipeline 子目录，键各自从 01 递增）
steps/audio/step_03_transcript_parse.py   # 转写解析
steps/audio/step_04_smart_podcast.py      # AI 生成播客笔记

# 2. 在 pipelines.yaml 新增 pipeline（GitLab-CI 风格：jobs + needs）
# audio:
#   jobs:
#     "01_download":
#       run: steps.common.step_01_download
#       pool: io
#     "02_whisper":              # 复用 video 的 whisper 步
#       run: steps.video.step_02_whisper
#       needs: ["01_download"]
#       image: flori/step-gpu    # cpu 池即可跑（CPU int8），worker 有 GPU 时自动加速
#     "04_smart_podcast":
#       run: steps.audio.step_04_smart_podcast
#       needs: ["03_transcript_parse"]
#       tags: []

# 3. 加来源检测（api/routes/jobs.py 的 _detect_content_type + shared/source_detect.py）
# _detect_content_type():
#   if url 是音频直链 or 文件是 mp3/wav → content_type = "audio"
```

调度器/Worker 完全不用改——它们只看 pipelines.yaml。

### 7.5 扩展 Worker

**水平扩展（加副本）**：

> ⚠️ **不要用 `docker compose up -d --scale worker-cpu=3`**。所有副本共用同一服务定义、同一
> id 来源,会注册成**同一个 worker_id** → 监控里互相覆盖、多数显示离线、心跳/状态错乱。
> 同机多 worker 必须各起**命名服务**并设**独立 `WORKER_NAME`**:worker 据此派生确定性、唯一的
> id(`{type}-sha256(WORKER_NAME)[:8]`,缓存在 `/data/workers/<name>`),重装/删缓存/重注册都不变、不撞。

```yaml
# 同机加一个 CPU worker:叠加到一个 override compose,命名服务 + 独立 WORKER_NAME
services:
  worker-cpu-2:
    extends: { file: docker-compose.yml, service: worker-cpu }
    container_name: flori-worker-cpu-2
    environment:
      WORKER_NAME: cpu-2
      WORK_DIR: /tmp/flori-work-cpu-2
```

**跨机器扩展（加 GPU）**：
```bash
# 任意能出站 HTTPS 的机器一条命令接入：走 /api/runner/* 网关，不直连 Redis/MinIO
# （见 ADR-0009；管理页「接入新 Worker」会按勾选能力自动生成完整命令）
docker run -d --restart unless-stopped --gpus all \
  -e GATEWAY_URL=https://<主机域名> \
  -e WORKER_REGISTRATION_TOKEN=<flw- 接入 token> \
  -e WORKER_NAME=gpu-1 \
  ghcr.io/gwzlchn/flori-worker:latest \
  python -m worker.main --pools cpu gpu
```

whisper 等转写步排在 cpu 池（刻意不进 gpu 池，避免无 GPU worker 时任务没人认领），带 `--gpus all`
的 worker 跑到时自动用 GPU 加速。

接入 token 由管理页或 `POST /api/workers/registration-token` 铸造（默认 24h 过期，重铸作废旧的）；
注册后服务端签发 per-worker token，产物经网关代理读写，删除 worker 即吊销。凭证 env、GPU 模型
缓存、自签证书等完整选项见 [08-deployment](08-deployment.md)。

**扩展 Worker 能力**：
```bash
# 没有类型注册表：能力 = 启动参数 --pools 集合，路由只按池匹配（type 仅为显示标签，由 pools 派生）
python -m worker.main --pools ai         # 专门跑 AI 步骤
python -m worker.main --pools cpu gpu    # 强机：cpu、gpu 池都消费
# 需要额外依赖（如 whisper）时基于 worker 镜像加装 [gpu] extra，再照常 --pools 启动
```

Worker 只需一条通路（本机直连 Redis，或远程经网关出站 HTTPS）+ 用 `--pools` 声明消费哪些池。加减 Worker 不影响调度器——多一个消费者就多一个并行度。
