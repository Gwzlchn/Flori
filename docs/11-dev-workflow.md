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
│   ├── document/  audio/
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

## 4. 分级交付工作流

项目执行入口是 `.agents/skills/flori-delivery-train/SKILL.md`。先判断本次授权到哪一层,再加载对应规则。Skill 只负责编排,不复制或覆盖 `CLAUDE.md` 的权威规则。

### 4.1 执行模式

| 模式 | 触发边界 | 默认动作 |
|------|----------|----------|
| `consult` | 只读回答、诊断、审查、状态或方案讨论 | 只查相关证据并回复;不建工作项、不新选profile、不盘点无关Git/发布状态;正式reviewer继承被审候选风险门并可复跑必要验证 |
| `change` | 修改tracked文件或持久本地项目记录 | 建一个精简工作项,实现并定向验证,停在可复核候选 |
| `ship` | commit、push、PR、CI、版本、镜像或部署 | 先完成change,到达对应阶段时才加载发布规则 |
| `operate` | 修改运行数据、内容投递状态、清理目标、凭据或生产资源 | 先固定目标、回滚/恢复和验收证据;需要发代码时再叠加ship |

模式只按用户授权升级。`consult`若被要求写出持久设计稿,从首次写盘开始转为`change`;`change`不因代码已经可用就自动升级为`ship`。

### 4.2 权威懒加载

1. 当前会话已注入`CLAUDE.md`时不重复读取。
2. 文档路由不明确才读`docs/README.md`;普通consult直接读受影响文件。
3. 创建/修改工作项才读`.local/processing/迭代记录规范.txt`;涉及worktree、multi、证据集成才读本文对应章节。
4. 进入commit/CI/部署前才读`docs/12-cicd.md`对应章节。
5. 内容投递、清理/重投或投递Bug才读`.local/delivery/README.txt`和受影响记录,不扫全目录。

### 4.3 Profile 选择

只有`change/ship/operate`分别选择三个profile并写入工作项:

| 维度 | 取值 | 额外要求 |
|------|------|----------|
| 规模 | `single` / `multi` | `multi` 必须生成依赖 DAG、共享热点 owner 表和集成批次 |
| 风险 | `normal` / `contract` / `critical` | `contract` 同步契约和消费方;`critical` 先冻结威胁/不变量矩阵,再写恶意与恢复测试 |
| 发布 | `review-first` / `commit-only` / `ci` / `full-deploy` | 分别停在未提交候选、本地无版本价值提交、CI全绿、完整本地与ECS外部验收;纯运行运维可填不涉及 |

实际修改或操作安全边界、数据库迁移、灾备恢复、身份、凭据与权限默认是`critical`。只读讨论这些主题仍是`consult`,只要求答案覆盖相关不变量和恢复边界,不提前运行实现/测试/发布门。一个高风险修改不能因为规模是`single`而降级。

只把高风险主题写成持久设计稿时使用`change/review-first`,风险profile用`normal`(设计本身修改外部契约时用`contract`),风险门记`critical-target`:记录设计级不变量/威胁/拒绝/回滚/恢复矩阵,实现把它作为依据前做一次独立设计审查;设计文档本身不跑恶意产品测试、CI或部署。

### 4.4 多单元发布列车

`multi` 开工前在列车工作项记录:

1. 每个节点的 unit ID、验收与回滚边界、依赖和 integrator。
2. 可并行节点、共享热点串行链和每个热点的唯一 owner。
3. 集成批次以及每批触发的跨单元测试、镜像和手验。
4. 唯一列车 integrator、发布分支、目标版本和最终 push/deploy 条件。
5. 以候选标识为键的证据账本;优先使用checkpoint/tree SHA,未提交的`review-first`候选使用确定性diff digest。候选包含gitignored持久文件时,摘要必须显式覆盖这些文件。单元TXT只引用证据ID,不复制完整输出。

`review-first`阶段各单元保留可识别候选,不创建正式价值commit。进入`ship`且用户授权commit后,每个独立回滚边界才在发布分支形成一个不bump的价值commit;全部批次通过后由列车integrator创建一个`build(release)`commit统一bump,再一次push、CI和部署。

### 4.5 Worktree 租约与停滞回收

并行会话或主工作树有未提交改动时,每个会话必须在租约制 worktree 中工作。`$FLORI_WORKING_DIR` 是仓库外工作区,本机真实路径只放 `.local/` 或 shell 环境,不要写入 git 文档。

创建 worktree 前登记验收目标、profile、integrator、分支、worktree path、base commit、文件 scope、共享热点 owner、测试责任、首个有效产物期限、合并方式和预计回收条件。推荐目录:

```
$FLORI_WORKING_DIR/
├── tmp/            临时产物和 scratch
└── wt/<slug>/      活跃 worktree
```

默认 10 分钟内必须出现首个有效产物。有效心跳至少包含一项:可检查 diff/checkpoint、仍在运行的测试或构建、已完成的证据、可复现阻塞。只报告“规划中”不算。首次超时提醒一次;默认再等 5 分钟仍无证据就中断,归档已有内容并重派。租约中声明的长命令可使用更长期限,但必须能观察进程和当前阶段。

被上游阻塞时只允许一次不超过 15 分钟的轻量 preflight,记录可行性、预期 scope 和依赖风险。详细文件审计、实现和正式 review 等依赖稳定后再启动,不要周期性重扫同一范围。

合入 `main` 后立即回收。最终正式提交所在分支已成为 `main` 祖先时:

```bash
git worktree remove "$FLORI_WORKING_DIR/wt/<slug>"
git branch -d <final-branch>
```

checkpoint 分支经 squash 后不会成为 `main` 祖先。integrator 必须先确认 checkpoint diff 已完整纳入最终 `main` SHA、worktree 无未归档改动,再回收 worktree并用 `git branch -D <checkpoint-branch>` 删除。本任务创建的远程分支已纳入后同步删除;`badges`、`mutation-data` 等自动数据分支例外。无法清理时必须在最终回复说明原因,并写入 `.local/processing/待办池.txt`。

### 4.6 审查预算

1. 开工前把已知不变量、攻击类别、拒绝条件和恢复场景转成检查表或红测试,不要等每轮 reviewer 临时扩充范围。
2. `normal` 默认一次实现审查;`contract` / `critical` 默认一次实现审查加一次独立终审。发现新的 P0/P1 类别可以重开门禁;同类小修继续在当前轮关闭,P2/P3 新范围进入后续工作项。
3. reviewer 优先验证风险矩阵、diff 和未覆盖边界,复用满足§5全部匹配维度的已有证据;证据仍有效时不机械重跑全量套件。
4. checkpoint 是可恢复保存点,不是审查或正式提交单位。多轮反馈继续写入同一工作项时间线。

### 4.7 提交规范

> 本节只在进入`ship`并准备正式commit时加载。`CLAUDE.md`保留硬边界,具体格式单一来源在此。

标题:

- 单单元直接发布的价值commit,以及`single commit-only`晋级或多单元列车末尾的`build(release)`commit:`<type>(<scope>): <中文摘要>;<新版本>`。
- 尚未单独push/部署的列车内价值commit,以及文档、公约、调研、测试或CI治理commit:`<type>(<scope>): <中文摘要>`,不带版本。
- `type`只允许`feat/fix/refactor/chore/ops/contract/test/docs/perf/build`;`scope`使用受影响模块/领域的小写标识。
- 摘要用中文说明“做了什么 + 为什么”,不写句号,逗号用半角`,`。
- 修改API、WebSocket、Redis或文件Schema时使用`contract`类型,并在同一价值提交更新`docs/03-contracts.md`及消费方。

版本:

- 单一来源是`pyproject.toml`的`[project].version`;后端共用`shared.version.FLORI_VERSION`,前端从后端读取,`package.json`不跟随。
- 普通改动patch+1并逢10进位;大重构minor+1且patch归0;架构级大重构major+1且后两段归0。
- 版本代表一次实际发布。`single`直接发布在价值commit中bump一次;`single commit-only`晋级发布时新增一个`build(release)`commit统一bump;`multi`各价值commit不bump,列车末尾由唯一integrator创建一个`build(release)`commit统一bump。
- checkpoint、未单独发布的列车内价值commit和非发布治理commit不bump。若列车分成两次push/部署,每次都是独立发布并各bump一次。
- `commit-only`的本地价值commit不bump。若之后扩大到`ci/full-deploy`,保留该价值commit并新增一次`build(release)`版本提交,不重写已审候选。

正文:

1. 解释背景/动机和关键取舍,不要只罗列diff。
2. 用`tests:`写明验证命令和结果。
3. 修改对外接口时用`contract:`说明契约已同步。
4. 正文后只保留一行`Co-Authored-By` trailer。型号使用实际agent型号;不要附加session URL、上下文长度或其它harness后缀。

示例:

```
feat(scheduler): 实现 DAG 推进逻辑              # 列车内价值 commit,未单独发布
feat(scheduler): 实现 DAG 推进逻辑;0.2.0        # single 直接发布
build(release): 发布本列车已验收价值;0.3.0      # multi 统一发布
build(release): 发布已审单元价值;0.3.1          # single commit-only 后续晋级
```

非发布治理示例:

```
chore(workflow): 以交付单元收敛开发与发布治理
```

完整body示例:

```
feat(document): Document 对齐翻译与独立译文阅读面;0.8.0

非中文Document需要可核验的中文阅读面。新增按稳定segment对齐的条件翻译,原生HTML/PDF保持不可变。
- 02_parse:检测来源语言并发布Document Model与locator。
- 04_translate:翻译自然语言segment,公式/引用/数字冻结校验后发布translation.json与translated.html。
- 前端JobDetailView:译文高亮可反向跳回HTML segment或PDF page+bbox。
tests:Document contract、translation alignment和reader定向测试通过。
contract:docs/03-contracts.md已更新Document/Translation/locator文件契约。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

## 5. 验证阶梯与证据复用

| 阶段 | 责任人 | 默认验证 | 输出证据 |
|------|--------|----------|----------|
| 实现 | agent | 新增、红测试和直接相关用例 | 候选标识、输入、命令、运行配置、依赖镜像、结果和产物路径 |
| 审查 | reviewer | 风险矩阵、审查新增范围、无法核验项 | issue 类别、结论和复跑结果 |
| 单元集成 | unit integrator | touched-path 并集、契约/迁移/消费方闭环 | 单元候选 SHA 与验收清单 |
| 批次集成 | train integrator | 跨单元联调、对应镜像、API/Playwright 手验 | 批次 SHA 与集成证据 |
| 发布 | CI/deployer | 全量门禁、镜像提升、部署和外部验证 | run URL、digest、版本、health |

证据只有在候选标识、测试输入、命令、运行配置和依赖镜像未变化时才可复用既有结果。单元之后合入了无关节点,原定向证据仍可引用,但批次集成不能省略。镜像默认每个集成批次构建一次;只有 Dockerfile、依赖、构建上下文或运行时输入变化才提前重建。

## 6. 收口规则

### 6.1 Consult

1. 回答用户问题并标明未核实或受时间影响的边界。
2. 没有写入、运行态修改或本任务资源时,不创建工作项,也不做worktree/分支/版本/发布收尾。
3. 只有结论后续会复用或用户要求持久报告时才写盘,并从该写入开始按change记录。

### 6.2 单个变更或操作单元

1. 记录模式、profile、验收目标、回滚边界、scope、唯一integrator和可复用证据;agent租约、热点owner、版本、部署等条件字段只在触发时填写。
2. 实现与定向验证后按风险 profile 完成审查。
3. integrator squash checkpoint,执行单元集成并同步契约、迁移、消费方和必要文档。
4. `review-first`停在未提交候选;`commit-only`形成已授权的本地无版本价值提交后停止;直接进入`ci/full-deploy`的产品改动把价值commit作为发布commit并bump一次。已有`commit-only`价值commit后再扩大终点时,另建一次`build(release)`版本提交。纯文档、公约、调研、测试或CI治理仍不bump。
5. 只执行当前模式到达的检查。使用worktree时核对并报告本任务分支;进入ship后才检查origin、CI、版本和部署。

### 6.3 多个交付单元

1. 先冻结 DAG、串行热点链、集成批次、列车 integrator 和证据账本。
2. 各单元按同一生命周期推进,只在依赖满足时做详细审计和实现。
3. `review-first`单元完成后保留候选标识;进入`ship`且用户授权commit后才形成不bump的价值commit。每批只跑一次跨单元集成与镜像构建。
4. 全部批次通过后创建一个发布 commit,一次 push、CI、版本发布和部署。
5. 按证据账本生成完成矩阵,再统一回收 worktree、checkpoint、临时分支和实验资源。

只有使用worktree的change、ship或需要Git回收的operate在最终回复前检查本任务worktree、登记分支和`git status`。可用`.agents/skills/flori-delivery-train/scripts/delivery-snapshot.sh`合并机械查询;候选含gitignored持久文件时用`--extra <label=path>`纳入复合摘要。不要因其它会话存在无关worktree就扩展本任务scope。

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
  ghcr.io/<owner>/flori-worker:latest \
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
