# 调度器

> 职责：监听步骤完成事件，推进 DAG 中的下一步骤到对应资源池队列。
> 不执行任何步骤，只做"推进"逻辑。

> 现状提示（正文多为早期设计示意，落地形态以代码为准）：
> - pipeline 已改为 GitLab-CI 风格：`needs` 推导 DAG、`rules` 声明式跳过、`extends`/`variables`。见 `configs/pipelines.yaml` 与 [docs/03-contracts.md §4.1](../03-contracts.md)。正文的 `depends_on`/`condition` 对应现在的 `needs`/`rules`。
> - 步骤名为各 pipeline 内独立 `01..N`（video 现为 `01_download`/`02_whisper`/…/`11_smart`/`12_review`）。正文示例里的旧步骤名以 `configs/pipelines.yaml` 为准。
> - worker 已 GitLab-runner 化：认领/上报走服务端 `/api/runner/jobs/*`，远程 worker 不直连 Redis。见 [worker.md](worker.md) 与 [ADR-0009](../adr/0009-worker-gateway-outbound-https.md)。
> - scene 池已并入 cpu：`03_scene` 只是 cpu 池里的一个步骤，scene→cpu 互斥冻结已删。单机抢资源由 per-worker 并发度（`WORKER_CONCURRENCY`）控制，见 [ADR-0011](../adr/0011-worker-runtime-orchestration.md)。
> - 核心不变量仍成立：DAG 推进、资源池上限与运行时覆盖、下载隔离到 io 池、B站 cookie 硬门控、优先级、孤儿回收、幂等。

## 1. 职责边界

| 调度器做 | 调度器不做 |
|---------|-----------|
| 接收新 Job，初始化步骤状态 | 执行步骤（Worker 做） |
| 监听步骤完成/失败事件 | 管理文件存储 |
| 检查 DAG 依赖，推入就绪队列 | 直接与前端通信（API 做） |
| 检查跳过条件（has_subtitle 等） | 管理 Worker 生命周期 |
| 处理重试逻辑 | |
| 标记 Job 整体完成/失败 | |

### 实现边界

`scheduler.scheduler.Scheduler` 是稳定兼容入口，持有 Redis、Database、配置、Storage 和后台任务状态。内部职责通过显式组合拆分，不使用动态属性转发：

| 组件 | 职责 |
|------|------|
| `DagPlanner` | 依赖满足、条件跳步、下游推进和进度计算 |
| `TaskRouter` | pool/tag/网络区域选择、条件求值和入队 |
| `LifecycleCoordinator` | Job 提交、步骤开始/完成/失败和执行身份校验 |
| `RecoveryCoordinator` | 启动恢复、孤儿与卡住步骤回收、重跑和重提交 |
| `EffectDispatcher` | pipeline 声明的索引、术语和证据副作用 |
| `JobFinalizer` | generation-bound Job 终态、索引对账和书籍链推进 |
| `BackgroundServices` | durable lifecycle Stream、通知、周期任务、心跳和关闭 |

组件之间只经 `Scheduler` 的显式 façade 协作。外部调用仍以 `Scheduler` 为入口；子模块不依赖 API package，未知完成副作用按失败处理并由终态对账重试。

## 2. 核心流程

```python
class Scheduler:
    def __init__(self, redis: Redis, pipelines_config: dict, pools_config: dict):
        self.redis = redis
        self.pipelines = pipelines_config    # pipelines.yaml: video / document / audio
        self.pools = PoolManager(redis, pools_config)

    def get_steps(self, pipeline: str) -> dict:
        """按 pipeline 名称获取步骤配置"""
        steps = self.pipelines[pipeline]["steps"]
        return {s["name"]: s for s in steps}

    async def submit_job(self, job: Job):
        """API 调用：提交新任务"""
        steps = self.get_steps(job.pipeline)

        # 初始化所有步骤为 waiting
        for step_cfg in steps.values():
            await self.redis.hset(f"job:{job.id}:steps", step_cfg["name"], "waiting")

        # 找到无依赖的步骤，推入就绪队列
        for step_cfg in steps.values():
            if not step_cfg.get("depends_on"):
                await self.enqueue_step(job.id, step_cfg["name"])

    async def run(self):
        """主循环：监听事件，推进 DAG"""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("step_completed", "step_failed")

        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            data = json.loads(msg["data"])
            if data["status"] == "done":
                await self.on_step_done(data["job_id"], data["step"])
            elif data["status"] == "failed":
                await self.on_step_failed(data["job_id"], data["step"], data.get("error"))

    async def get_job_steps(self, job_id: str) -> dict:
        """从 Redis 查 job 的 pipeline，返回步骤配置"""
        pipeline = await self.redis.hget(f"job:{job_id}", "pipeline")
        return self.get_steps(pipeline)

    async def on_step_done(self, job_id: str, step_name: str, exec_id: str = None):
        """步骤完成 → 检查哪些后续步骤变为就绪（幂等：重复事件不会重复推入）"""
        # 先确认步骤确实是 done/running → 设为 done（幂等写入）
        current = await self.redis.hget(f"job:{job_id}:steps", step_name)
        if current not in ("running", "done"):
            return  # 已经被其他事件处理过，或状态不对
        await self.redis.hset(f"job:{job_id}:steps", step_name, "done")

        steps = await self.get_job_steps(job_id)
        step_statuses = await self.redis.hgetall(f"job:{job_id}:steps")

        for name, cfg in steps.items():
            if step_statuses.get(name) != "waiting":
                continue

            # 检查所有依赖是否完成
            deps = cfg.get("depends_on", [])
            if not all(step_statuses.get(d) in ("done", "skipped") for d in deps):
                continue

            # 检查条件（has_subtitle / no_subtitle / has_danmaku）
            condition = cfg.get("condition")
            if condition and not await self.check_condition(job_id, condition):
                await self.mark_skipped(job_id, name)
                continue

            await self.enqueue_step(job_id, name)

        # 检查是否全部完成
        fresh = await self.redis.hgetall(f"job:{job_id}:steps")
        if all(v in ("done", "skipped") for v in fresh.values()):
            await self.mark_job_done(job_id)

    async def on_step_failed(self, job_id: str, step_name: str, error: str):
        """步骤失败 → 重试或标记 Job 失败"""
        steps = await self.get_job_steps(job_id)
        cfg = steps[step_name]
        max_retries = cfg.get("retries", 0)
        retries = int(await self.redis.hget(f"job:{job_id}:retries", step_name) or 0)

        if retries < max_retries:
            await self.redis.hincrby(f"job:{job_id}:retries", step_name, 1)
            await self.enqueue_step(job_id, step_name)
        else:
            await self.redis.hset(f"job:{job_id}:steps", step_name, "failed")
            await self.mark_job_failed(job_id, f"{step_name}: {error}")

    async def enqueue_step(self, job_id: str, step_name: str):
        """将步骤推入对应资源池队列（合并 tags 供 Worker 亲和性匹配）"""
        steps = await self.get_job_steps(job_id)
        step_cfg = steps[step_name]
        await self.redis.hset(f"job:{job_id}:steps", step_name, "ready")
        pool = step_cfg["pool"]

        # 合并 tags：步骤静态 tags + Job 动态 tags（domain + style_tags）
        job_info = json.loads(await self.redis.hget(f"job:{job_id}", "info") or "{}")
        static_tags = step_cfg.get("tags", [])
        dynamic_tags = [job_info.get("domain", "")] + job_info.get("style_tags", [])
        merged_tags = sorted(set(static_tags + [t for t in dynamic_tags if t]))

        # 优先级：已完成步骤越多越优先
        statuses = await self.redis.hgetall(f"job:{job_id}:steps")
        done_count = sum(1 for v in statuses.values() if v in ("done", "skipped"))
        priority = -done_count

        # task JSON 必须确定性（sorted tags + sorted keys）→ ZSET 天然去重
        task = json.dumps({"job_id": job_id, "step": step_name, "tags": merged_tags},
                         sort_keys=True)
        await self.redis.zadd(f"queue:{pool}", {task: priority})

        # 发布事件给 WebSocket
        await self.publish_event(job_id, "step_ready", step=step_name)

    async def check_condition(self, job_id: str, condition: str) -> bool:
        job_dir = Path(f"/data/jobs/{job_id}")
        if condition == "no_subtitle":
            return not list((job_dir / "input").glob("*.srt"))
        if condition == "has_subtitle":
            return bool(list((job_dir / "input").glob("*.srt")))
        if condition == "has_danmaku":
            return bool(list((job_dir / "input").glob("*.ass")))
        return True
```

## 3. 优先级策略

```
优先级 = -(已完成步骤数)

Job A 已完成 7 步 → score = -7 (最优先)
Job B 已完成 2 步 → score = -2
Job C 刚开始      → score = 0  (最低)

效果：先让接近完成的 Job 做完 → 用户更快看到第一批结果
```

## 4. 跳过条件传播

跳过的步骤（skipped）在 DAG 中等同于 done，不阻塞后续步骤：

```
场景：视频无弹幕

07_danmaku rules:[exists danmaku → on] → 不满足 → skipped
09_mechanical needs=[06_ocr, 07_danmaku, 08_punctuate]
  → 07_danmaku=skipped 视为满足 → 等 06_ocr 和 08_punctuate 即可
```

跳过后立即触发一次 `on_step_done` 检查，让下游步骤有机会推进。

### 特殊情况：02_whisper → 08_punctuate

08_punctuate 的 DAG 依赖只写了 `01_download`，但 `rules` 是 `exists input/*.srt → when: on`（有字幕才标点）。

- 有字幕的视频：01_download 完成后 srt 已存在 → 08 立即就绪
- 无字幕的视频：01_download 完成后无 srt → 08 的 rules 不满足 → 标记 skipped
  - 但 02_whisper 生成了 srt → whisper 完成后调度器重新检查所有 waiting/skipped 步骤
  - 此时 08 的 rules 满足 → 从 skipped 恢复为 ready → 入队执行

`on_step_done` 中不仅检查 waiting 步骤，也重新检查 skipped 步骤的 rules 是否因新产物而满足。

## 5. 资源池管理（PoolManager）

```python
class PoolManager:
    # 3 行 Lua，原子 check-and-incr，避免超限
    ACQUIRE_LUA = """
    if redis.call('GET',KEYS[2])=='1' then return 0 end
    if tonumber(redis.call('GET',KEYS[1]) or '0') >= tonumber(ARGV[1]) then return 0 end
    redis.call('INCR',KEYS[1]) return 1
    """

    async def try_acquire(self, pool_name: str) -> bool:
        limit = self.config["pools"][pool_name]["limit"]
        return await self.redis.eval(
            self.ACQUIRE_LUA, 2,
            f"pool:{pool_name}:count", f"pool:{pool_name}:frozen",
            limit
        ) == 1

    async def release(self, pool_name: str):
        await self.redis.decr(f"pool:{pool_name}:count")
```

`frozen` 键是通用的池冻结开关：占槽 Lua 与认领循环都会检查，置位的池不再被认领；scene→cpu 的专用互斥冻结已随 scene 池并入 cpu 删除，当前没有生产路径置位该键。现行实现用 holder 集合（`pool:{pool}:holders`，`SCARD` 判上限）替代示意中的计数器，见 `shared/redis_client.py`。

注意：资源池获取/释放在 **Worker** 端做（自取模式），不在调度器。调度器只负责把步骤放进队列。

## 6. 重跑与 Pipeline 变更

### 强制重跑（API 触发）

```
POST /api/jobs/{id}/rerun
Body: {"from_step": "11_smart"}
```

调度器收到后：
1. 清除 `from_step` 及所有下游步骤的 `.done` 标记
2. 将这些步骤状态重置为 `waiting`
3. 推进 DAG——满足依赖的步骤立即入队
4. Worker 执行时 `should_run()` 返回 True → 重跑

### 重新提交已有 Job（pipeline 变更后）

当 `pipelines.yaml` 修改后（如调整步骤配置、新增步骤），对已完成的 Job 重新提交：

```
POST /api/jobs/{id}/resubmit
```

调度器：
1. 按新 pipeline 初始化步骤状态
2. 推入所有无依赖的步骤
3. Worker 执行每步时，`should_run()` 自动判断：
   - 输入没变 → 跳过（之前的产物还能用）
   - 输入或配置变了 → 重跑
4. 级联失效自动传播到下游

**无需人工判断哪些步骤要重跑**——指纹机制自动处理。

### 并行 Job 的资源竞争

多个 Job 同时处理时共享资源池。优先级策略确保接近完成的 Job 先做完：

```
Job A: 已完成 8/10 步 → score=-8 → 最优先
Job B: 已完成 3/10 步 → score=-3
Job C: 刚提交         → score=0  → 最低

效果：A 最快完成 → 用户看到第一批结果
      同时 B、C 的无依赖步骤也在其他池并行推进
```

## 7. 配置热更新

运行中可调整三类配置，无需重启：

### 资源池大小

```
PUT /api/config/pools
Body: {"pools": {"cpu": {"limit": 4}, "ai": {"limit": 3}}}
```

调度器监听 Redis 配置 key 变化，动态调整池大小。Worker 数量变化（加/减容器）自动适应——多一个 Worker 就多一个消费者。

### Prompt 模板

Worker 每次执行 LLM 步骤前从 `/data/prompts/` 读取最新 prompt。修改 prompt 文件后，新任务自动使用新 prompt，已完成任务的指纹检测到 prompt hash 变化 → resubmit 时自动重跑 LLM 步骤。

### 领域配置

`/data/configs/domain/*.yaml` 中的参数（场景检测阈值、OCR 置信度等）同理——Worker 每次执行前读取，配置变化通过指纹机制级联重跑受影响的步骤。

## 8. 容错

### 基本容错

| 场景 | 处理 |
|------|------|
| 调度器重启 | 从 Redis 恢复状态，对所有 running 步骤执行孤儿检测 |
| Redis 重启 | AOF 持久化，丢失最多 1 秒数据 |
| 重复推入队列 | Worker 执行前检查步骤状态，非 ready 则丢弃 |

### 孤儿步骤回收（Worker 消失）

核心问题：Worker 取了任务后崩溃/断网，步骤卡在 `running`，没人处理。

**检测机制**：调度器每 30 秒扫描一次所有 `running` 步骤：

```python
async def orphan_scan(self):
    """扫描 running 步骤，检查分配的 Worker 是否还有心跳"""
    for job_key in await self.redis.keys("job:*:steps"):
        job_id = job_key.split(":")[1]
        steps = await self.redis.hgetall(job_key)

        for step_name, status in steps.items():
            if status != "running":
                continue

            # 检查执行该步骤的 Worker 是否还活着
            worker_id = await self.redis.hget(f"job:{job_id}:step_worker", step_name)
            if not worker_id:
                # 无 worker 记录，直接标记失败
                await self.reclaim_step(job_id, step_name, "no worker assigned")
                continue

            worker_exists = await self.redis.exists(f"worker:{worker_id}")
            if not worker_exists:
                # Worker 心跳过期（已消失）→ 回收
                await self.reclaim_step(job_id, step_name, f"worker {worker_id} lost")

async def reclaim_step(self, job_id: str, step_name: str, reason: str):
    """回收孤儿步骤：释放资源槽 → 发 step_failed → 触发重试"""
    pool = self.get_step_pool(job_id, step_name)
    await self.pools.release(pool)
    await self.redis.publish("step_failed", json.dumps({
        "job_id": job_id, "step": step_name,
        "status": "failed", "error": f"orphan reclaimed: {reason}",
    }))
```

**时间线**：
```
t=0    Worker 取到任务，标记 running
t=10   Worker 崩溃
t=30   Worker 心跳 TTL 过期，Redis 自动删除 worker:{id}
t=60   调度器 orphan_scan 检测到 running 步骤无心跳 → 回收
t=61   on_step_failed → retries < max → 重新入队
t=62   另一个 Worker 取到任务 → 重跑
```

从 Worker 崩溃到重跑：**约 1 分钟**。

### GPU 长时间任务 vs 真正卡住

GPU 步骤（如 Whisper 转写 30 分钟视频）正常就会跑很久。不能把"跑得久"误判为"卡住"。

区分方法：**心跳在就没事，心跳没了才回收**。

```
正常场景：GPU Worker 跑 Whisper 30 分钟
  → 期间心跳每 10 秒续期一次
  → orphan_scan 每次都能找到 worker:{id}
  → 不回收 ✓

异常场景：GPU Worker 跑了 5 分钟后断网
  → 心跳 30 秒后过期
  → orphan_scan 发现 running 步骤的 worker 不存在
  → 回收 → 重试 ✓
```

补充：步骤的 `timeout_sec`（pipelines.yaml 定义）是 **Worker 层面的 subprocess 超时**，不是调度器层面的。Worker 正常运行时自己会超时中止并报 step_failed。orphan_scan 只处理 Worker 本身消失的情况。

### GPU 加速与 CPU 回退

池层面没有隐式降级或 fallback。`pipelines.yaml` 现把 `02_whisper` 等重步骤配在 cpu 池——刻意不配 gpu 池，否则无 GPU worker 时任务无人认领卡死；GPU 机器要参与就显式订阅 `--pools gpu cpu`。加速与回退发生在步骤内部：

```
Worker 取到步骤 → steps/utils/device.py 探测 nvidia-smi
有 GPU → Whisper 按显存选 large-v3/medium/small (float16)
无 GPU → Whisper base (int8)，慢但能用
OCR 当前仅 rapidocr (CPU)，paddleocr-GPU 尚未接入
```

某池的就绪步长期无在线 worker 时，调度器按 `NO_WORKER_GRACE_SEC` 宽限（代码默认 90s，compose 部署设 12h）后 fail-fast，给出明确错误而非静默挂起。
