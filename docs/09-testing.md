# 09 · 测试

> 分层验证策略。利用原型产物做测试数据，每步独立验证。

> **唯一入口 `scripts/test.sh`**（跨会话/多 agent 统一,权威规约见 CLAUDE.md §测试规约）:
> `scripts/test.sh -m <模块>`(快测) / `--changed`(受改动影响) / `--all`(全量+75%门) / `--fe`(前端)。
> 用常驻热容器免启停税、`-n auto` 并行；主 CI 运行 unit 分片、真依赖 integration、frontend、coverage gate 和镜像构建 / 发布，Schemathesis 独立每日 cron。**别再各写 `docker compose run …`**。

## 验证层级与门禁事实

| 层级 | 当前自动化与入口 | 是否主 CI 必经 |
|------|------------------|----------------|
| 主 CI | backend normal 6 分片 + worker 2 分片、真依赖 integration、frontend Vitest、coverage gate、按路径构建并在现有门通过后 push 镜像；拓扑以 `.github/workflows/ci.yml` 为准 | 是，非纯文档 push / PR |
| 组件集成 | `scripts/test.sh --integration` 统一编排真 Redis、生产 Database 多连接/多进程冷启动、迁移整链失败回滚、已发布历史版本到当前 manifest 的 DR 恢复查询、Gateway Worker、real-docker 和生产 AOF 空环境恢复 | 是，`integration` required job |
| pipeline E2E | 主 CI integration 覆盖 video / paper / article / audio 真实完成事件到 Search / Ask / MCP 命中；`.github/workflows/e2e.yml` 另保留真实 PDF 步骤链手动验收 | 闭环必经；外部素材需显式触发 |
| 条件外网 / 凭证 | `scripts/test.sh --external <article|audio|rss|youtube|all>` 统一编排公网场景，缺所选 URL 返回非零；B 站、arXiv 与真实 AI 仍按素材、网络和凭证条件执行 | 否，只在条件满足的受控环境执行 |
| 浏览器与视觉 | `docker-compose.e2e.yml` + `tests/e2e/smoke.py` 做已部署栈路由冒烟；UI 视觉验收另在 3840×2160、1512×982、440×956 三视口检查 | 否，当前为人工 / 发布验收 |

“脚本存在”不等于“每个 PR 已覆盖”。文档记录某项通过时必须同时写明入口、素材、是否 DRY_RUN、
是否联网以及运行时间，不能把 unit、integration、条件外网和人工视觉混写成一个“E2E 已完成”。

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

# 跑单步（这是运行服务容器手动执行步骤脚本，不是跑测试）
docker compose run --rm worker-cpu python3 -m steps.video.step_05_dedup --job-dir /tmp/test-job

# 跑该步骤的 pytest 用例（唯一入口 scripts/test.sh，`--` 透传路径）
scripts/test.sh -- tests/steps/test_step_05_dedup.py
```

如有原型项目的已有产物，可直接用作测试输入——复制对应步骤的输出文件到测试目录即可。

### 每步检查项

检查项由 `tests/steps/` 对应步骤用例覆盖：

| 步骤 | 检查项 |
|------|--------|
| 03_scene | scenes.json 可解析、scenes 非空、首 start_sec==0 |
| 04_frames | jpg 数量 ≥ scenes 数、每张 >10KB |
| 05_dedup | 每项有 keep/phash、保留率 25%-100% |
| 06_ocr | 长度 == keep=true 数、nonempty >30% |
| 11_smart | >500 字符、有 ## 标题、无拒绝话术 |
| 12_review | 扁平 6 维整数分（completeness/accuracy/structure/terminology/visual_integration/readability）各 1-5、overall 1-5、key_terms 为 `[{term,definition}]`、parse_failed 非 true |

## 3. 集成测试

主 CI 的 required integration 入口直接运行 `scripts/test.sh --integration`。数据库矩阵使用生产 `Database`
验证冷启动、跨连接可见、唯一键竞争和 current+1/current+2 后段故障的整链回滚。固定
format-v1 与 format-v2/schema-v2 归档均经生产 restore 入口恢复，再由当前 Database 执行
`init_schema/get_job/list_jobs`，避免只验压缩包形状而没有验证真实升级和读路径。

涉及学习候选 schema 时，还必须验证旧版本备份恢复后能继续升级到当前 manifest，当前版本快照能通过冻结 migration chain 自校验，未来版本仍在兼容门 fail-closed。测试不得只修改 `PRAGMA user_version` 伪造兼容性。

调度器 + Worker + Redis 联调：

```bash
# 启动集成栈（这是起服务，不是跑测试；专用 compose 免鉴权 + DRY_RUN 可选，CI 的 e2e.yml 同款）
docker compose -f docker-compose.integration.yml up -d

# 提交测试任务（用本地已有视频直接上传，跳过下载；文件经 storage 写入 input/）
curl -X POST http://localhost:8000/api/jobs/upload \
  -F "file=@/path/to/test.mp4" -F "domain=deep-learning"

# 监控进度
watch -n 2 'curl -s http://localhost:8000/api/jobs/{id} | python3 -m json.tool'

# 或一键跑现成集成脚本（自带提交 + 轮询 + 产物断言）
bash tests/integration/run_e2e_cpu.sh
```

## 4. 产品 E2E 目标

手机投递 URL → 全流程跑完 → 笔记可读。以下是产品级验收目标，不代表主 CI 已自动覆盖：

验收标准：
- 投递到笔记可读 < 30 分钟（短视频）
- 笔记评审分 ≥ 4/5
- WebSocket 进度实时更新
- 截图正常显示
- 时间戳可点击

当前自动接线范围和未自动化范围以上表及 `.github/workflows/e2e.yml` 为准；浏览器路由冒烟的
具体运行方法见 `tests/e2e/README.md`。

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
    await redis.hset("job:j1:steps", "10_smart", "ready")
    await redis.zadd("queue:ai", {'{"job_id":"j1","step":"10_smart","tags":[]}': 0})

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
    assert await redis.hget("job:j1:steps", "10_smart") == "running"
```

#### 用例 2：exec_id 防重复计费

```python
async def test_exec_id_dedup(db):
    """同一个 exec_id 写两次 ai_usage，只记一条"""
    exec_id = "worker-a1b2:1716000000000"

    db.execute("INSERT OR IGNORE INTO ai_usage (exec_id, job_id, step, provider, model, cost_usd, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (exec_id, "j1", "10_smart", "anthropic", "sonnet", 0.18, "2026-05-17"))

    # 重复写入
    db.execute("INSERT OR IGNORE INTO ai_usage (exec_id, job_id, step, provider, model, cost_usd, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (exec_id, "j1", "10_smart", "anthropic", "sonnet", 0.18, "2026-05-17"))

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
    await redis.hset("job:j1:steps", "09_mechanical", "running")
    await redis.hset("job:j1:steps", "10_smart", "waiting")

    # 触发两次
    await scheduler.on_step_done("j1", "09_mechanical", exec_id="e1")
    await scheduler.on_step_done("j1", "09_mechanical", exec_id="e2")

    # 10_smart 只被推入队列一次（ZSET member 相同 → 天然去重）
    queue_len = await redis.zcard("queue:ai")
    assert queue_len == 1
```

#### 用例 4：Tag 亲和性——不匹配的任务被放回

```python
async def test_tag_reject(redis):
    """Worker 的 reject_tags 生效，任务被放回队列"""
    await redis.zadd("queue:ai",
        {'{"job_id":"j1","step":"10_smart","tags":["vision","private"]}': 0})

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
        await redis.hset(f"job:{job_id}:steps", "10_smart", "ready")
        await redis.zadd("queue:ai",
            {json.dumps({"job_id": job_id, "step": "10_smart", "tags": []},
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

开发和联调环境用 `DRY_RUN=1` 强制走假响应，防止误调真 API：

```python
# AIGateway.call 入口（shared/ai_gateway.py）
if self._dry_run:  # DRY_RUN=1 时置位
    return await DryRunProvider().complete(request)  # 不发真请求，零开销
```

```bash
# 跑并发相关用例（用例在 scheduler/worker 模块）
scripts/test.sh -m scheduler -m worker

# 开发调试时（想看完整流程但不花钱；这是起服务，不是跑测试）
DRY_RUN=1 docker compose up
```

## 5.1 证据型自动学习卡测试矩阵

该能力把 AI 输出、人工审核和 SRS 写入串成一个事务边界，至少覆盖下列层级：

| 层级 | 必测不变量 | 入口 |
|---|---|---|
| migration | 历史 checksum 不变；上一版本到当前版本升级；故障后 DDL、ledger、`user_version` 全部回滚；exact current schema；无 vector/embedding 占位 | `scripts/test.sh -- tests/test_db_migrations.py` |
| DB / API | 伪 evidence id、跨 batch、domain/concept/hash/quote 失效、revision 竞态、bool/负数/SQLite 64 位边界、101 项、同 request 异 payload、整批回滚 | `scripts/test.sh -- tests/test_study_suggestions.py` |
| Redis | 多调用方对同 task id 只有一个原子 enqueue-once；marker、ZSET 和等待时间戳一起提交；重放不恢复已弹出的旧任务 | `scripts/test.sh -- tests/test_redis_client.py` |
| DR | 当前 suggestion 表和不可变审计可被快照、验证、恢复并重新打开；未来 schema 拒绝恢复 | `scripts/test.sh -- tests/test_backup_restore.py` |
| UI | 生成、跨刷新轮询、失败重试、证据预览、编辑、同 batch 批量接受/拒绝、409 刷新和掌握度 | `scripts/test.sh --fe frontend/src/views/StudyView.test.ts` |
| 真依赖闭环 | 真 Redis + production Worker + controlled AI Gateway；Scheduler 重启/收割后只产生一份 suggestion/card/operation，接受后 due，真实 `good` 评分后 mastery=80 | `scripts/test.sh --integration` |

真依赖闭环不得调用公网 LLM。controlled gateway 返回固定 JSON，并记录调用次数；Redis result TTL 丢失时从持久 AI log 恢复，旧 task 迟到、超时 retry 和多 Scheduler 副本必须分别有测试。最终还需断言队列、holder 和临时 result 清理完成。

证据一致性测试必须同时覆盖：chunk 同 hash 重建仍有效，正文变化变 `stale`，chunk/job 消失变 `unavailable`；job 删除保留快照与复习审计；concept merge 和 domain rename 在同一事务移动当前指针、已接受卡片和 fingerprint。掌握度只允许真实 review log 参与，候选、未复习卡和 rejected 卡均不得抬高分数。

## 6. 性能基线

基于原型的实测数据（6 核 x86 主机）：

| 步骤 | 8 分钟视频 | 22 分钟视频 |
|------|-----------|------------|
| 03_scene | ~2min | ~5min |
| 04_frames | ~15s | ~30s |
| 05_dedup | ~10s | ~20s |
| 06_ocr | ~45s | ~2min |
| 08_punctuate | ~30s | ~1min |
| 10_smart | ~3min | ~5min |
| **总计** | **~8min** | **~15min** |
