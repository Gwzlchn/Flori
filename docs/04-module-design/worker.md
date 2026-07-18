# Worker

> 职责：认领资源池中的任务，执行步骤脚本，上报结果。
> Worker 不知道 DAG，只管"取任务 → 跑 → 报结果"。

> 现状提示（M-W 已完成）：worker 已 GitLab-runner 化。**远程 worker 单条出站 HTTPS 接入网关，不直连 Redis/MinIO**——经 `/api/runner/*` 注册换 per-worker token、长轮询认领步骤、上报结果、产物经网关代理读写（见 [ADR-0009](../adr/0009-worker-gateway-outbound-https.md)、[docs/03-contracts.md §1.7](../03-contracts.md)）。本文 §2 中「直接 zpopmin 队列」「直连 Redis」是 M-W 前的本地/同机形态示意；认领/资源槽/exec_id 去重等逻辑已搬到服务端共享 `runner_ops`，单机部署仍可走直连模式，远程一律走网关。步骤名为各 pipeline 内 `01..N`。

## 1. Worker 类型

| 类型 | 运行位置 | 消费池 | 负责步骤（video 示例） |
|------|---------|--------|---------|
| io | 主机 | io | 01_download / 07_danmaku / 09_mechanical（下载/出网步，类型名即其唯一订阅的池） |
| cpu | 主机 / GPU机器 | cpu, io | 02_whisper / 03_scene / 04_frames / 05_dedup / 06_ocr |
| ai | 主机 | ai, io | 08_punctuate / 10_evidence / 11_smart / 12_review |
| gpu | GPU 机器 | gpu, cpu, io | 当前无步骤固定配 gpu 池；订 cpu 池跑 02_whisper/06_ocr，步骤内探测到 GPU 自动加速 |

消费哪些池由启动参数 `--pools` 声明（能力集合，可多个，无主次）；类型从池集合派生，仅作显示标签。上表为典型搭配，scene 池已并入 cpu。

**并发度（本机容量）**：`--concurrency N`（或 env `WORKER_CONCURRENCY`，默认 1）让一个 worker 进程并发跑 N 个 step（起 N 条认领循环）。异构机器据此自报容量（强机调大、弱机=1）；**全局每池槽位（`pools.yaml` 的 `limit`）仍是系统级天花板**，并发度只决定单 worker 的并行上限。接多台同类机器（如多台 GPU 机）= 各起一个 worker，要跨机并行需相应调大对应池的 `limit`。

## 2. 自取主循环

```python
class Worker:
    def __init__(self, redis: Redis, worker_type: str, pool_names: list[str],
                 tags: set[str] = None, reject_tags: set[str] = None):
        self.redis = redis
        self.worker_id = f"{worker_type}-{uuid.uuid4().hex[:8]}"
        self.worker_type = worker_type
        self.pool_names = pool_names
        self.tags = tags or set()                # 能力标签：{"vision", "gpu", ...}
        self.reject_tags = reject_tags or set()  # 排斥标签：{"private", ...}
        self.data_dir = Path(os.environ.get("DATA_DIR", "/data/jobs"))

    async def run(self):
        await self.register()
        asyncio.create_task(self.heartbeat_loop())

        idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "0"))
        last_task_time = time.time()

        while True:
            task = await self.fetch_task()
            if task:
                last_task_time = time.time()
                await self.execute(task)
            else:
                if idle_timeout and time.time() - last_task_time > idle_timeout:
                    break  # GPU Worker 空闲退出
                await asyncio.sleep(1)

    async def fetch_task(self) -> dict | None:
        """从多个池队列中取最高优先级任务（带 tag 亲和性）"""
        # 检查是否被管理员暂停（独立的 admin_status 叠加位，与运行时 status 解耦）
        admin_status = await self.redis.hget(f"worker:{self.worker_id}", "admin_status")
        if admin_status == "paused":
            return None

        for pool in self.pool_names:
            # 检查池是否冻结
            if await self.redis.get(f"pool:{pool}:frozen") == "1":
                continue
            # 尝试获取资源槽
            if not await self.try_acquire_slot(pool):
                continue

            # 弹出 → 检查 tag → 不匹配就放回
            task = await self.pop_matching_task(pool)
            if task:
                task["pool"] = pool
                return task

            # 没取到匹配的任务，归还槽
            await self.redis.decr(f"pool:{pool}:count")
        return None

    async def pop_matching_task(self, pool: str, max_tries: int = 5) -> dict | None:
        """弹出任务，检查 tag 匹配，不匹配放回。纯 Python，无 Lua。"""
        for _ in range(max_tries):
            items = await self.redis.zpopmin(f"queue:{pool}", count=1)
            if not items:
                return None

            task_json, score = items[0]
            task = json.loads(task_json)
            step_tags = set(task.get("tags", []))

            # 匹配规则：step.tags ⊆ self.tags AND step.tags ∩ self.reject_tags = ∅
            if step_tags.issubset(self.tags) and not step_tags.intersection(self.reject_tags):
                return task

            # 不匹配，放回队列（保留原优先级）
            await self.redis.zadd(f"queue:{pool}", {task_json: score})

        return None  # 连续 max_tries 个都不匹配，暂时放弃

    async def execute(self, task: dict):
        job_id = task["job_id"]
        step = task["step"]
        pool = task["pool"]
        # 步骤执行 ID（标识"这次步骤执行"）
        # AI 调用 ID 由 Gateway 在每次 LLM 调用时独立生成：{exec_id}:{call_index}
        exec_id = f"{self.worker_id}:{int(time.time()*1000)}"

        # 乐观锁：只有 status=ready 时才能抢到执行权
        acquired = await self.redis.eval(
            "if redis.call('HGET',KEYS[1],ARGV[1])=='ready' then "
            "redis.call('HSET',KEYS[1],ARGV[1],'running') return 1 "
            "else return 0 end",
            1, f"job:{job_id}:steps", step
        )
        if not acquired:
            await self.redis.decr(f"pool:{pool}:count")
            return

        # 上报开始（带 exec_id）
        await self.redis.hset(f"job:{job_id}:step_worker", step, self.worker_id)
        await self.update_worker_status("busy", job_id, step)
        await self.redis.publish("step_started", json.dumps({
            "job_id": job_id, "step": step, "worker": self.worker_id,
            "exec_id": exec_id,
        }))

        try:
            # 1. Pull 输入
            work_dir = await self.storage.pull(job_id, step)

            # 2. 执行步骤
            start = time.time()
            result = subprocess.run(
                ["python3", f"steps/{step}.py", "--job-dir", str(work_dir)],
                capture_output=True, text=True,
                timeout=self.get_timeout(step)
            )
            duration = time.time() - start

            if result.returncode == 0:
                # 3. Push 结果
                await self.storage.push(job_id, step, work_dir)
                await self.redis.publish("step_completed", json.dumps({
                    "job_id": job_id, "step": step, "status": "done",
                    "duration": round(duration, 1), "worker": self.worker_id,
                    "exec_id": exec_id,
                }))
            else:
                await self.redis.publish("step_failed", json.dumps({
                    "job_id": job_id, "step": step, "status": "failed",
                    "error": result.stderr[-500:], "worker": self.worker_id,
                    "exec_id": exec_id,
                }))
        except subprocess.TimeoutExpired:
            await self.redis.publish("step_failed", json.dumps({
                "job_id": job_id, "step": step, "status": "failed",
                "error": "timeout", "worker": self.worker_id,
            }))
        finally:
            await self.storage.cleanup(job_id, step, work_dir)
            await self.redis.decr(f"pool:{pool}:count")
            await self.update_worker_status("idle")
```

## 3. 注册、心跳与持久化

Worker 信息存两处：

| 存储 | 用途 | 生命周期 |
|------|------|---------|
| Redis HASH | 实时心跳 + 当前状态 | TTL = `online_window_sec`（默认 30s，单一事实源，崩溃自动消失） |
| SQLite workers 表 | 历史记录 + 统计 + 运维备注 | 持久，Worker 下线后仍保留 |

> **Worker 身份**：`worker_id` 不再每次随机。① 设 `WORKER_NAME` → **确定性派生** `{type}-sha256(WORKER_NAME)[:8]`，缓存在 `/data/workers/<name>`，重装/删缓存/重注册永远同一 id、不同名不撞(同机多 worker 各给一个唯一名即可)。② 不设 `WORKER_NAME` → 读缓存(默认 `/data/workers/worker.id`)，无则随机 `{type}-{8hex}` 写回，靠缓存文件跨重启稳定。gateway 模式以服务端 `register` 返回的 id 为准。id 文件统一收进 `/data/workers/` 文件夹（不再散在 `/data` 根）。
> **Redis TTL 单一事实源**：liveness key 的 TTL 由 `configs/pools.yaml` 的 `worker_status.online_window_sec` 驱动（与对外"在线"判定同窗口），不再各处硬编码 30。
> **并发上限服务端权威**：gateway 认领时以服务端 `pools.yaml` 夹取 worker 自报的 `pool_limits`（`min(client, server)`），worker 报超大值也无法突破全局每池并发。

```python
async def register(self):
    """首次注册：写 Redis + 写/更新 SQLite"""
    info = {
        "type": self.worker_type,
        "pools": ",".join(self.pool_names),
        "tags": ",".join(sorted(self.tags)),
        "hostname": socket.gethostname(),
        "status": "idle",
        "started_at": now_iso(),
    }
    # Redis（实时）
    await self.redis.hset(f"worker:{self.worker_id}", mapping=info)
    await self.redis.expire(f"worker:{self.worker_id}", 30)
    # SQLite（持久，通过 API 写入）
    await self.report_to_api("register", info)

async def heartbeat_loop(self):
    while True:
        await self.redis.hset(f"worker:{self.worker_id}",
                             "last_heartbeat", now_iso())
        await self.redis.expire(f"worker:{self.worker_id}", 30)
        await asyncio.sleep(10)

async def on_task_done(self, job_id, step, duration):
    """任务完成后更新 SQLite 统计"""
    await self.report_to_api("task_done", {
        "job_id": job_id, "step": step, "duration": duration
    })
```

Worker 崩溃后 Redis 心跳 30s 过期自动消失。SQLite 记录保留，状态由调度器标记为 `offline`。

### Tag 亲和性

Worker 启动时声明能力标签和排斥标签，取任务时只接匹配的步骤。

**匹配规则**（两个条件同时满足）：
1. `step.tags ⊆ worker.tags`（步骤需求 ⊆ Worker 能力）
2. `step.tags ∩ worker.reject_tags = ∅`（步骤标签不在 Worker 排斥列表中）

```bash
# 能力标签
python -m worker.main --pools ai --tags vision claude-cli
python -m worker.main --pools gpu cpu --tags gpu vision

# 排斥标签（不接受某些领域的任务）
python -m worker.main --pools ai --tags vision --reject-tags private confidential
```

也支持自动发现（根据环境变量推断）：

```python
def auto_discover_tags(self) -> set:
    tags = set()
    if os.environ.get("ANTHROPIC_API_KEY"):
        tags.add("vision")               # Anthropic 模型支持视觉
    if shutil.which("claude"):
        tags.update(["vision", "claude-cli"])
    if os.environ.get("DEEPSEEK_API_KEY"):
        tags.add("text-only")
    if os.path.exists("/usr/bin/nvidia-smi"):
        tags.add("gpu")
    if os.environ.get("OLLAMA_URL"):
        tags.add("local")
    return tags
```

手动 `--tags` 优先；未指定时用自动发现。

**常用能力标签**（步骤声明需求，Worker 声明拥有）：

| tag | 含义 | 步骤示例 | Worker 示例 |
|-----|------|---------|-----------|
| `vision` | 支持图片输入 | 11_smart（视觉 pass） | 有 Claude/GPT-4o 的 Worker |
| `gpu` | 有 GPU 硬件 | 02_whisper | GPU 机器 |
| `claude-cli` | Claude CLI 订阅 | — | 家目录(workers/<名>/)已 seed 凭证的 Worker |
| `cn-network` | 可访问中国网络 | 01_download (B站) | 在国内网络的 Worker |
| `heavy` | 大内存/高配额 | 长视频处理 | 高配机器 |

**常用排斥标签**（Worker 声明拒绝）：

用于隐私/合规/资源隔离。步骤的 tags 中包含这些标签时，声明了 reject 的 Worker 不会接。

| reject tag | 场景 |
|-----------|------|
| `private` | Worker 不希望处理内部数据（合规要求） |
| `confidential` | Worker 不处理机密/受限内容 |
| `large-file` | Worker 磁盘小，不接大文件任务 |

**步骤的 tags 从两个来源合并**：
- `pipelines.yaml` 中的静态 tags（如 `["vision"]`）
- Job 创建时的动态 tags（如 `["deep-learning"]`，从 domain 自动添加；隐私/合规标签如 `["private"]` 也可在此注入）

```python
# 调度器推入队列时合并 tags
step_tags = pipeline_step["tags"]                    # 静态：["vision"]
job_tags = [job.domain] + job.style_tags             # 动态：["deep-learning", "case-study"]
task["tags"] = list(set(step_tags + job_tags))        # 合并：["vision", "deep-learning", "case-study"]
```

这样声明了 `reject_tags: {"private"}` 的 Worker 就能过滤掉所有标记为内部数据的任务。

### paused 状态（暂停 / 恢复）

管理员通过 `PUT /api/workers/{id}` 传 `{"status":"paused"}` 暂停、`{"status":"active"}` 恢复。服务端把暂停态写进**独立的 `admin_status` 字段**（Redis hash + DB 列），认领时 `claim_step` 读它 → 暂停则不再认领新任务（已在跑的步跑完为止，进程留存、几乎不耗资源，恢复前不接新活）。

与运行时 `status`(busy/idle) 解耦是关键：旧的 draining 复用 `status` 字段，会被 claim/release 的 busy/idle 写入、以及 gateway 心跳自报的 idle 覆盖（三个 bug）；拆成 `admin_status` 后暂停态稳定。对本地与远程（网关）worker 一致生效，无需 docker.sock。前端「暂停/恢复」按钮即调此接口（见 ADR-0011）。

## 4. 统一存储接口（StorageBackend）

所有 Worker 统一使用 pull/push 模型，不区分本地/远程：

```python
class StorageBackend:
    async def pull(self, job_id: str, step: str) -> Path:
        """拉取该步骤需要的输入文件，返回工作目录路径"""
        ...

    async def push(self, job_id: str, step: str, work_dir: Path):
        """推送该步骤的输出文件"""
        ...

    async def cleanup(self, job_id: str, step: str, work_dir: Path):
        """清理临时文件（远程模式需要）"""
        ...
```

`step` 参数是完整执行键。Part task 解析后把步骤工作目录切到 `parts/{part_id}/`，步骤进程看到的
`--job-dir` 仍是一个自洽根；Job task 在根目录执行，fan-in 可读全部 Part。三种 backend 共用强制范围：
Part 只读写自己的前缀，Job 可读全部但禁止写 Part 前缀。Gateway API 重复核验租约 scope，远端 Worker
不能通过伪造相对路径越权。

### 三种实现（`shared/storage.py`）

```python
class LocalStorage(StorageBackend):
    """本地部署：数据在本机，pull/push 都是 no-op"""

    async def pull(self, job_id, step):
        return Path(f"/data/jobs/{job_id}")   # 直接返回本地路径

    async def push(self, job_id, step, work_dir):
        pass                                   # 已经写在本地了

    async def cleanup(self, job_id, step, work_dir):
        pass                                   # 不清理，数据需要保留


class RemoteStorage(StorageBackend):
    """对象存储中转:直连 MinIO 拉取/推送(worker 与中心存储同网可达的分布式 compose 场景)"""

    def __init__(self, minio_client):
        self.minio = minio_client

    async def pull(self, job_id, step):
        local_dir = Path(f"/tmp/jobs/{job_id}")
        local_dir.mkdir(parents=True, exist_ok=True)
        await self.minio.download_job_files(job_id, local_dir)
        return local_dir

    async def push(self, job_id, step, work_dir):
        # 只回传相对 pull 快照新增/改动的文件,不删,并行分支互不覆盖
        await self.minio.upload_step_outputs(job_id, step, work_dir)

    async def cleanup(self, job_id, step, work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)


class GatewayStorage(StorageBackend):
    """远程 worker:产物经 API 网关代理读写,纯出站 HTTPS,不直连 MinIO(见 ADR-0009)"""

    async def pull(self, job_id, step):
        # GET /api/runner/jobs/{id}/artifacts 取清单,逐个下载到本机临时 work_dir
        ...

    async def push(self, job_id, step, work_dir):
        # PUT /api/runner/jobs/{id}/artifacts/{rel},同样只回传新增/改动的文件
        ...
```

### Worker 初始化时选择 backend

```python
# worker/main.py：按环境变量选择
if os.environ.get("GATEWAY_URL"):
    storage = GatewayStorage(...)     # 远程 worker:产物经网关,纯出站 HTTPS
elif os.environ.get("MINIO_URL"):
    storage = RemoteStorage(...)      # 直连中心 MinIO 的分布式部署
else:
    storage = LocalStorage(jobs_dir)  # 单机:本地盘
```

**所有 Worker 代码统一**——不需要 `GpuWorker` 子类。本地 Worker 和远程 GPU Worker 跑的是同一份代码，只是 StorageBackend 不同。

### 数据同步流程

```
远程 Worker (GPU):
  pull:  中心存储 → /tmp/jobs/{id}/  (MinIO 直连或网关代理，下载输入)
  exec:  steps/*.py --job-dir /tmp/jobs/{id}/
  push:  /tmp/jobs/{id}/ → 中心存储  (上传产物)
  clean: rm -rf /tmp/jobs/{id}/

本地 Worker:
  pull:  return /data/jobs/{id}/     (no-op，直接用)
  exec:  steps/*.py --job-dir /data/jobs/{id}/
  push:  (no-op，已在本地)
  clean: (no-op，不清理)
```

### 中心存储即产物权威，无需拉回

调度器不做产物同步：远程 Worker push 后产物已在中心存储，后续步骤无论落在哪台 Worker，都由各自的 StorageBackend pull；API 读产物也走同一 backend。

### 空闲退出

```bash
# 远程 GPU Worker 启动命令（网关接入，纯出站 HTTPS）
docker run --gpus all \
  -e GATEWAY_URL=https://${API_HOST} \
  -e WORKER_REGISTRATION_TOKEN=${TOKEN} \
  -e IDLE_TIMEOUT=600 \
  flori-worker:latest python -m worker.main --pools gpu cpu
```

空闲 10 分钟无任务自动退出，下次有任务时手动启动。

## 5. AI Worker 特殊行为

claude 二进制烤在 worker 镜像里(base.Dockerfile worker stage);订阅凭证放【该 worker 自己的家目录】
`${FLORI_DATA_DIR}/workers/<worker名>/`,容器把它挂为 HOME。每 worker 独立凭证副本(各自 refreshToken
续期,无并发写冲突);claude CLI 的会话 transcript(`$HOME/.claude/projects/…`)随之落数据卷 = 纳管,
审计层按 session_id 回收 agentic 全轨迹(见 shared/ai_gateway.py ClaudeCLIProvider._find_transcript)。
worker.id 稳定身份缓存也在同目录(worker/transport.py default_worker_id_file;旧平铺文件启动自迁移)。

```yaml
# docker-compose.yml
worker-ai:
  volumes:
    # 先 scripts/seed-worker-home.sh ai(seed 凭证到家目录),再挂;★不要直挂宿主 ~/.claude
    - ${FLORI_DATA_DIR}/workers/ai:/home/worker
  environment:
    - HOME=/home/worker
    - HTTPS_PROXY=${HTTPS_PROXY:-}
  extra_hosts:
    - "host.docker.internal:host-gateway"
  user: "1000:10"
```

## 6. Docker 镜像

### CPU Worker

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    scenedetect[opencv] imagehash pillow scikit-image \
    opencv-python-headless rapidocr-onnxruntime \
    pysrt pyyaml redis structlog
WORKDIR /app
COPY steps/ steps/
COPY shared/ shared/
```

### GPU Worker

```dockerfile
FROM nvidia/cuda:12.2-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y python3 python3-pip ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    scenedetect[opencv] imagehash pillow scikit-image \
    opencv-python-headless paddlepaddle-gpu paddleocr \
    faster-whisper minio \
    pysrt pyyaml redis structlog
WORKDIR /app
COPY steps/ steps/
COPY shared/ shared/
```


## manifest-v1 提交协议(九步;契约见 docs/03-contracts.md §7.5)

成功路径:rc=0 → push(既有,双写期不变)→ 按 outputs glob 展开精确输出并校验(普通文件/路径边界/无凭证/无 symlink;NO_PUSH 与 >10GiB 豁免)→ 流式 SHA-256 → 组装 final manifest(digest 即 commit token 的 candidate_digest)→ `begin_step_commit`(围栏拒绝=换代,跳过 done;404/405=中心不支持,走既有 done)→ staging → token 保护下 promote + read-back → manifest 最后原子发布 → 同 token 上报 done → 清理 staging。失败/超时路径只回传诊断白名单(`output/ai_logs/*`、`logs/*`、`.{step}.progress/.meta.json/.error.json` + step 声明的 `output_policy.audit_globs`),不推业务输出、不发布 manifest。幂等跳过(candidate `reused`)且中心 manifest 与当前 digest 一致时省去整套重发。
