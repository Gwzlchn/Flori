# Worker

> 职责：从资源池队列自取任务，执行步骤脚本，上报结果。
> Worker 不知道 DAG，只管"取任务 → 跑 → 报结果"。

## 1. Worker 类型

| 类型 | 运行位置 | 消费池 | 负责步骤 |
|------|---------|--------|---------|
| download | 主机 | io | 00_download |
| cpu | 主机 / GPU机器 | scene, cpu, io | 01-05, 07 |
| ai | 主机 | ai, io | 06, 08, 09 |
| gpu | GPU 机器 | gpu, scene, cpu, io | 00b, 01(GPU), 04(GPU) |

Worker 类型决定了它消费哪些池的队列。一个 Worker 可以消费多个池。

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
        # 检查是否 draining
        status = await self.redis.hget(f"worker:{self.worker_id}", "status")
        if status == "draining":
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
                if pool == "scene":
                    await self.redis.set("pool:cpu:frozen", "1")
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
            if pool == "scene":
                await self.redis.delete("pool:cpu:frozen")
            await self.update_worker_status("idle")
```

## 3. 注册、心跳与持久化

Worker 信息存两处：

| 存储 | 用途 | 生命周期 |
|------|------|---------|
| Redis HASH | 实时心跳 + 当前状态 | TTL 30s，崩溃自动消失 |
| SQLite workers 表 | 历史记录 + 统计 + 运维备注 | 持久，Worker 下线后仍保留 |

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
python3 worker.py --type ai --tags vision,claude-cli
python3 worker.py --type gpu --tags gpu,vision

# 排斥标签（不接受某些领域的任务）
python3 worker.py --type ai --tags vision --reject-tags private,confidential
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
| `vision` | 支持图片输入 | 08_smart | 有 Claude/GPT-4o 的 Worker |
| `gpu` | 有 GPU 硬件 | 00b_whisper | GPU 机器 |
| `claude-cli` | Claude CLI 订阅 | — | 挂载了 ~/.claude 的 Worker |
| `cn-network` | 可访问中国网络 | 00_download (B站) | 在国内网络的 Worker |
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

### draining 状态（安全下线）

管理员通过 `PUT /api/workers/{id}` 设置 Worker 为 draining → Worker 的 `fetch_task()` 检测到 draining → 不再接新任务 → 当前任务完成后自动退出。

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

### 两种实现

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
    """远程部署：通过 MinIO 拉取/推送"""

    def __init__(self, minio_client):
        self.minio = minio_client

    async def pull(self, job_id, step):
        local_dir = Path(f"/tmp/jobs/{job_id}")
        local_dir.mkdir(parents=True, exist_ok=True)
        await self.minio.download_job_files(job_id, local_dir)
        return local_dir

    async def push(self, job_id, step, work_dir):
        await self.minio.upload_step_outputs(job_id, step, work_dir)

    async def cleanup(self, job_id, step, work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
```

### Worker 初始化时选择 backend

```python
class Worker:
    def __init__(self, redis, worker_type, pool_names):
        ...
        # 根据环境变量决定 storage 模式
        if os.environ.get("MINIO_URL"):
            self.storage = RemoteStorage(Minio(os.environ["MINIO_URL"], ...))
        else:
            self.storage = LocalStorage()
```

**所有 Worker 代码统一**——不需要 `GpuWorker` 子类。本地 Worker 和远程 GPU Worker 跑的是同一份代码，只是 StorageBackend 不同。

### 数据同步流程

```
远程 Worker (GPU):
  pull:  MinIO → /tmp/jobs/{id}/     (下载输入)
  exec:  steps/*.py --job-dir /tmp/jobs/{id}/
  push:  /tmp/jobs/{id}/ → MinIO     (上传产物)
  clean: rm -rf /tmp/jobs/{id}/

本地 Worker:
  pull:  return /data/jobs/{id}/     (no-op，直接用)
  exec:  steps/*.py --job-dir /data/jobs/{id}/
  push:  (no-op，已在本地)
  clean: (no-op，不清理)
```

### 调度器端的 MinIO 同步

远程 Worker 完成后，调度器需要把产物从 MinIO 拉回到主机本地存储（如果后续步骤在本地执行）：

```python
# 调度器 on_step_done 中
if step_was_remote(job_id, step_name):
    await minio.download_step_outputs(job_id, step_name, local_job_dir)
```

### 空闲退出

```bash
# GPU Worker 启动命令
docker run --gpus all \
  -e REDIS_URL=rediss://:password@${RELAY_IP}:6380/0 \
  -e MINIO_URL=https://${RELAY_IP}:9000 \
  -e IDLE_TIMEOUT=600 \
  worker-gpu:latest python3 worker.py --type gpu
```

空闲 10 分钟无任务自动退出，下次有任务时手动启动。

## 5. AI Worker 特殊行为

AI Worker 通过挂载宿主机 Claude CLI 到容器内，用 subprocess 调用：

```yaml
# docker-compose.yml
worker-ai:
  volumes:
    - ~/.claude:/home/user/.claude
    - ~/.claude.json:/home/user/.claude.json
    - ~/.local/share/claude:/home/user/.local/share/claude:ro
    - ~/.local/bin/claude:/usr/local/bin/claude:ro
  environment:
    - HOME=/home/user
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
