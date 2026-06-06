# 调度器

> 职责：监听步骤完成事件，推进 DAG 中的下一步骤到对应资源池队列。
> 不执行任何步骤，只做"推进"逻辑。

## 1. 职责边界

| 调度器做 | 调度器不做 |
|---------|-----------|
| 接收新 Job，初始化步骤状态 | 执行步骤（Worker 做） |
| 监听步骤完成/失败事件 | 管理文件存储 |
| 检查 DAG 依赖，推入就绪队列 | 直接与前端通信（API 做） |
| 检查跳过条件（has_subtitle 等） | 管理 Worker 生命周期 |
| 处理重试逻辑 | |
| 标记 Job 整体完成/失败 | |

## 2. 核心流程

```python
class Scheduler:
    def __init__(self, redis: Redis, pipelines_config: dict, pools_config: dict):
        self.redis = redis
        self.pipelines = pipelines_config    # pipelines.yaml: {video: {steps: [...]}, paper: ...}
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

05_danmaku condition="has_danmaku" → skipped
07_mechanical depends_on=[04_ocr, 05_danmaku, 06_punctuate]
  → 05_danmaku=skipped 视为满足 → 等 04_ocr 和 06_punctuate 即可
```

跳过后立即触发一次 `on_step_done` 检查，让下游步骤有机会推进。

### 特殊情况：00b_whisper → 06_punctuate

06_punctuate 的 DAG 依赖只写了 `00_download`，但 condition 是 `has_subtitle`（检查 srt 是否存在）。

- 有字幕的视频：00_download 完成后 srt 已存在 → 06 立即就绪
- 无字幕的视频：00_download 完成后无 srt → 06 的 condition 不满足 → 标记 skipped
  - 但 00b_whisper 生成了 srt → whisper 完成后调度器重新检查所有 waiting/skipped 步骤
  - 此时 06 的 condition 满足 → 从 skipped 恢复为 ready → 入队执行

`on_step_done` 中不仅检查 waiting 步骤，也重新检查 skipped 步骤的 condition 是否因新产物而满足。

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
        if pool_name == "scene":
            await self.redis.delete("pool:cpu:frozen")
```

注意：资源池获取/释放在 **Worker** 端做（自取模式），不在调度器。调度器只负责把步骤放进队列。

## 6. 重跑与 Pipeline 变更

### 强制重跑（API 触发）

```
POST /api/jobs/{id}/rerun
Body: {"from_step": "08_smart"}
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

### GPU 步骤失败后的降级

pipelines.yaml 中 gpu 池有 `fallback: cpu`。GPU Worker 不在线时：

```
调度器发现 gpu 队列无消费者 → 检查 fallback → 推入 cpu 队列
CPU Worker 取到 → 步骤内 device.py 检测无 GPU → 走 CPU 路径

Whisper: GPU large-v3 (3分钟) → CPU base (30分钟，质量较低但能用)
OCR:     PaddleOCR GPU (10秒) → RapidOCR CPU (1分钟)
场景检测: GPU 解码 (20秒) → CPU 解码 (2分钟)
```
