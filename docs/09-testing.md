# 09 · 测试

> 分层验证策略。利用原型产物做测试数据，每步独立验证。

## 1. 测试金字塔

```
        ┌──────────────┐
        │  端到端 (E2E)  │  手机投递 → 笔记可读
        ├──────────────┤
        │  集成测试      │  调度器 + Worker + 步骤联调
        ├──────────────┤
        │  单步验证      │  每个步骤独立验证（核心）
        └──────────────┘
```

## 2. 单步验证

每步用已有产物做输入验证，不需要跑上游步骤。

### 验证命令

```bash
# 准备测试数据（从已有产物复制）
mkdir -p /tmp/test-job/input /tmp/test-job/intermediate /tmp/test-job/assets
cp /path/to/existing/output/scenes.json /tmp/test-job/intermediate/
cp /path/to/existing/output/assets/*.jpg /tmp/test-job/assets/

# 跑单步
docker compose run --rm worker-cpu python3 steps/03_dedup.py --job-dir /tmp/test-job

# 自动验证
python3 verify_step.py --step 03_dedup --job-dir /tmp/test-job
```

如有原型项目的已有产物，可直接用作测试输入——复制对应步骤的输出文件到测试目录即可。

### verify_step.py

每步有检查项列表：

| 步骤 | 检查项 |
|------|--------|
| 01_scene | scenes.json 可解析、scenes 非空、首 start_sec==0 |
| 02_frames | jpg 数量 ≥ scenes 数、每张 >10KB |
| 03_dedup | 每项有 keep/phash、保留率 25%-100% |
| 04_ocr | 长度 == keep=true 数、nonempty >30% |
| 08_smart | >500 字符、有 ## 标题、无拒绝话术 |
| 09_review | overall 1-5、有 6 维度评分 |

## 3. 集成测试

调度器 + Worker + Redis 联调：

```bash
# 启动基础设施
docker compose up -d redis scheduler worker-cpu worker-ai

# 提交测试任务（用本地已有视频，跳过下载）
curl -X POST http://localhost:8000/api/jobs \
  -H "Authorization: Bearer $API_TOKEN" \
  -d '{"upload": true, "domain": "deep-learning"}'
# 预先把测试视频放到 /data/jobs/{id}/input/

# 监控进度
watch -n 2 'curl -s http://localhost:8000/api/jobs/{id} | python3 -m json.tool'

# 验证产物
python3 verify_step.py --step all --job-dir /data/jobs/{id}
```

## 4. E2E 测试

手机投递 URL → 全流程跑完 → 笔记可读。

验收标准：
- 投递到笔记可读 < 30 分钟（短视频）
- 笔记评审分 ≥ 4/5
- WebSocket 进度实时更新
- 截图正常显示
- 时间戳可点击

## 5. 并发安全测试

LLM 调用花真钱，重复执行 = 重复扣费。并发相关的逻辑必须在不花钱的环境下充分测试。

### 测试环境

```
真 Redis（Docker 启动，测完销毁）
真 SQLite（内存模式 :memory:）
假步骤执行（mock subprocess，sleep 模拟耗时，不调真 AI）
假 AI Gateway（记录调用次数，不发真请求）
```

```python
# conftest.py
@pytest.fixture
async def redis():
    r = await aioredis.from_url("redis://localhost:6379/15")  # 用独立 db
    await r.flushdb()
    yield r
    await r.flushdb()

@pytest.fixture
def mock_step():
    """假步骤：sleep 随机时间，写一个输出文件"""
    async def execute(job_dir, step):
        await asyncio.sleep(random.uniform(0.01, 0.1))
        (job_dir / f".{step}.done").write_text("{}")
    return execute

@pytest.fixture
def mock_ai_gateway():
    """假 AI Gateway：记录调用次数，不花钱"""
    class MockGateway:
        def __init__(self):
            self.call_count = 0
        async def route(self, step, request):
            self.call_count += 1
            return LLMResponse(content="mock", cost_usd=0.18, ...)
    return MockGateway()
```

### 核心并发用例

#### 用例 1：乐观锁——两个 Worker 抢同一个步骤

```python
async def test_optimistic_lock(redis, mock_step):
    """两个 Worker 同时拿到同一个任务，只有一个能执行"""
    # 准备：一个 ready 步骤
    await redis.hset("job:j1:steps", "08_smart", "ready")
    await redis.zadd("queue:ai", {'{"job_id":"j1","step":"08_smart","tags":[]}': 0})

    worker_a = Worker(redis, "ai", ["ai"], tags=set())
    worker_b = Worker(redis, "ai", ["ai"], tags=set())
    executed = []

    async def run_worker(w):
        task = await w.fetch_task()
        if task:
            # execute 内部有乐观锁
            result = await w.execute(task)
            if result:  # 拿到执行权
                executed.append(w.worker_id)

    await asyncio.gather(run_worker(worker_a), run_worker(worker_b))

    assert len(executed) == 1  # 只有一个成功执行
    assert await redis.hget("job:j1:steps", "08_smart") == "running"
```

#### 用例 2：exec_id 防重复计费

```python
async def test_exec_id_dedup(db):
    """同一个 exec_id 写两次 ai_usage，只记一条"""
    exec_id = "worker-a1b2:1716000000000"

    db.execute("INSERT OR IGNORE INTO ai_usage (exec_id, job_id, step, provider, model, cost_usd, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (exec_id, "j1", "08_smart", "anthropic", "sonnet", 0.18, "2026-05-17"))

    # 重复写入
    db.execute("INSERT OR IGNORE INTO ai_usage (exec_id, job_id, step, provider, model, cost_usd, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (exec_id, "j1", "08_smart", "anthropic", "sonnet", 0.18, "2026-05-17"))

    count = db.execute("SELECT COUNT(*) FROM ai_usage WHERE exec_id=?", (exec_id,)).fetchone()[0]
    assert count == 1  # 只有一条记录
    total = db.execute("SELECT SUM(cost_usd) FROM ai_usage WHERE job_id='j1'").fetchone()[0]
    assert total == 0.18  # 不是 0.36
```

#### 用例 3：on_step_done 幂等——重复事件不推重复下游

```python
async def test_scheduler_idempotent(redis, scheduler):
    """on_step_done 重复触发，下游步骤只入队一次"""
    # 准备：step A done → 应该推 step B
    await redis.hset("job:j1:steps", "07_mechanical", "running")
    await redis.hset("job:j1:steps", "08_smart", "waiting")

    # 触发两次
    await scheduler.on_step_done("j1", "07_mechanical", exec_id="e1")
    await scheduler.on_step_done("j1", "07_mechanical", exec_id="e2")

    # 08_smart 只被推入队列一次（ZSET member 相同 → 天然去重）
    queue_len = await redis.zcard("queue:ai")
    assert queue_len == 1
```

#### 用例 4：Tag 亲和性——不匹配的任务被放回

```python
async def test_tag_reject(redis):
    """Worker 的 reject_tags 生效，任务被放回队列"""
    await redis.zadd("queue:ai",
        {'{"job_id":"j1","step":"08_smart","tags":["vision","private"]}': 0})

    worker = Worker(redis, "ai", ["ai"],
                   tags={"vision"}, reject_tags={"private"})

    task = await worker.fetch_task()
    assert task is None  # 被 reject

    # 任务还在队列里（被放回了）
    queue_len = await redis.zcard("queue:ai")
    assert queue_len == 1
```

#### 用例 5：压力测试——10 个 Worker 抢 5 个任务

```python
async def test_concurrent_10_workers_5_tasks(redis, mock_step, mock_ai_gateway):
    """10 个 Worker 并发处理 5 个任务，每个任务恰好执行一次"""
    # 准备 5 个任务
    for i in range(5):
        job_id = f"j_{i}"
        await redis.hset(f"job:{job_id}:steps", "08_smart", "ready")
        await redis.zadd("queue:ai",
            {json.dumps({"job_id": job_id, "step": "08_smart", "tags": []},
                       sort_keys=True): -i})

    # 10 个 Worker 并发
    workers = [Worker(redis, "ai", ["ai"], tags=set()) for _ in range(10)]
    results = await asyncio.gather(*[
        worker_run_once(w, mock_step) for w in workers
    ])

    executed_jobs = [r for r in results if r is not None]
    assert len(executed_jobs) == 5                        # 恰好 5 个被执行
    assert len(set(executed_jobs)) == 5                   # 每个都不同
    assert mock_ai_gateway.call_count == 5                # AI 只调了 5 次
```

### AI Gateway 安全开关

所有测试环境和开发环境强制使用 mock provider，防止误调真 API：

```python
# AI Gateway 初始化
if os.environ.get("TESTING") or os.environ.get("DRY_RUN"):
    gateway.force_provider = MockProvider()  # 所有调用走 mock，零开销
```

```bash
# 跑测试时
TESTING=1 pytest tests/test_concurrency.py -v

# 开发调试时（想看完整流程但不花钱）
DRY_RUN=1 docker compose up
```

## 6. 性能基线

基于原型的实测数据（6 核 x86 主机）：

| 步骤 | 8 分钟视频 | 22 分钟视频 |
|------|-----------|------------|
| 01_scene | ~2min | ~5min |
| 02_frames | ~15s | ~30s |
| 03_dedup | ~10s | ~20s |
| 04_ocr | ~45s | ~2min |
| 06_punctuate | ~30s | ~1min |
| 08_smart | ~3min | ~5min |
| **总计** | **~8min** | **~15min** |
