# 11 · 开发流程

> 并行 Claude 会话开发、会话交接、Git 工作流。

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
service/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
│
├── scheduler/              # 会话 A
│   ├── Dockerfile
│   ├── main.py
│   ├── pools.py
│   └── config.py
│
├── api/                    # 会话 B
│   ├── Dockerfile
│   ├── main.py
│   └── routes/
│
├── worker/                 # 会话 A
│   ├── Dockerfile
│   ├── main.py
│   └── heartbeat.py
│
├── worker-gpu/             # M5（GPU 加速，未来）
│   └── Dockerfile
│
├── shared/                 # 会话 A (基础) + B (扩展)
│   ├── step_base.py
│   ├── db.py
│   ├── redis_client.py
│   ├── storage.py
│   └── events.py
│
├── steps/                  # 从原型迁移（按 pipeline 分子目录）
│   ├── common/step_01_download.py
│   ├── video/step_02_whisper.py ... step_11_review.py
│   ├── paper/  article/  audio/
│   └── utils/
│
├── configs/
│   ├── pools.yaml
│   ├── pipelines.yaml
│   └── domain/
│
├── prompts/
│   ├── punctuate.md
│   ├── smart_notes.md
│   └── review.md
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
      - ./api:/app        # 挂载源码
      - ${FLORI_DATA_DIR:-./data}:/data
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload

  worker-cpu:
    volumes:
      - ./worker:/app
      - ./steps:/app/steps
      - ./shared:/app/shared
      - ${FLORI_DATA_DIR:-./data}:/data
```

## 4. Git 工作流

```
main
  │
  ├── m1/infra        会话 A: 调度器+Worker
  ├── m1/api          会话 B: API 服务
  ├── m1/frontend     会话 C: 前端
  │
  └── merge → main    联调通过后合并
```

每个会话在自己的分支工作，联调通过后 merge 到 main。

### 提交规范

```
feat(scheduler): 实现 DAG 推进逻辑
feat(worker): Worker 自取 + 心跳
feat(api): 任务创建和查询 API
feat(frontend): 投递页 + 进度页
fix(worker): 修复 scene 池未冻结 cpu 的问题
```

## 5. 集成测试顺序

```
1. 各步骤独立通过 verify_step.py          ← 并行开发
2. 调度器 + Worker + 步骤 联调             ← A 完成后
3. API + 调度器 联调                       ← A+B 完成后
4. 前端 + API 联调                         ← B+C 完成后
5. 端到端: 手机投递 → 跑完 → 看笔记        ← 全部完成后
```

## 6. 每完成一个模块

```
1. 写代码
2. 跑测试
3. git commit
4. 更新 ROADMAP.md（标记完成）
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
cat > steps/video/step_12_translate.py << 'EOF'
from shared.step_base import StepBase

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
        translated = self.call_ai(f"翻译以下内容为英文:\n{transcript}")
        self.write_output("output/transcript_en.md", translated)
        return {"chars": len(translated)}
EOF

# 2. 在 pipelines.yaml 加入步骤（GitLab-CI 风格：jobs + needs）
# video:
#   jobs:
#     ...
#     "12_translate":
#       run: steps.video.step_12_translate
#       pool: ai
#       needs: ["08_punctuate"]
#       tags: []
#       timeout: 300
#       retry: 2
```

调度器自动识别新步骤的依赖关系，Worker 自动执行。已有 Job 通过 resubmit 即可补跑新步骤。

### 7.3 新增内容来源

只改 `steps/common/step_01_download.py`，其他步骤不动：

```python
# steps/common/step_01_download.py 里加一个分支
def detect_source(url):
    if "douyin.com" in url:
        return "douyin"
    # ... 已有的识别逻辑

def download_douyin(url, output_dir):
    # yt-dlp 支持抖音
    cmd = ["yt-dlp", url, "-o", str(output_dir / "source.%(ext)s")]
    self.run_subprocess(cmd)
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
#       tags: ["gpu"]
#     "04_smart_podcast":
#       run: steps.audio.step_04_smart_podcast
#       needs: ["03_transcript_parse"]
#       tags: []

# 3. 在 05-content-adapters 里加来源检测
# detect_content_type():
#   if url 是播客平台 or 文件是 mp3/wav → content_type = "podcast"
```

调度器/Worker/API 完全不用改——它们只看 pipelines.yaml。

### 7.5 扩展 Worker

**水平扩展（加副本）**：
```bash
# 同一台机器加 CPU Worker
docker compose up -d --scale worker-cpu=3

# 或在 .env 里配置
WORKER_CPU_REPLICAS=3
```

**跨机器扩展（加 GPU）**：
```bash
# GPU 机器上一条命令接入（连中转 Redis）
docker run --gpus all \
  -e REDIS_URL=rediss://:pass@relay:6380/0 \
  worker-gpu:latest python3 worker.py --type gpu
```

**新增 Worker 类型**：
```bash
# 1. 写 Dockerfile（安装特定依赖）
# 2. 配置消费哪些池
WORKER_POOLS = {
    "translation": ["ai"],           # 新类型：专门跑翻译步骤
    "gpu-heavy": ["gpu", "scene"],   # 新类型：多 GPU 卡
}
# 3. docker compose up worker-translation
```

Worker 只需连 Redis + 知道自己消费哪些池。加减 Worker 不影响调度器——多一个消费者就多一个并行度。
