# 09 · 测试

> 分层验证策略。利用原型产物做测试数据，每步独立验证。

> **唯一入口 `scripts/test.sh`**（跨会话/多 agent 统一,权威规约见 CLAUDE.md §测试规约）:
> `scripts/test.sh -m <模块>`(快测) / `--changed`(受改动影响) / `--all`(全量+75%门) / `--fe`(前端)。
> 用常驻热容器免启停税、`-n auto` 并行；主 CI 运行 unit 分片、真依赖 integration、frontend、coverage gate 和镜像构建 / 发布，Schemathesis 独立每日 cron。**别再各写 `docker compose run …`**。

## 验证层级与门禁事实

| 层级 | 当前自动化与入口 | 是否主 CI 必经 |
|------|------------------|----------------|
| 主 CI | main 复用内容键测试 runtime，backend normal 在全量 collection 前混合预分 15 组：轻文件保持完整，巨型文件局部 collection 后按实际 nodeid 分散，组内 xdist 单项调度避免批量预取长尾；worker 1 分片(均 4 xdist worker)和真依赖 integration 两分组拉取并固定不可变 digest 后挂当前源码。detect 和 build-images 在 prepare 释放首层槽后先完成短控制链，coverage gate 随后预热，并以当前 run/attempt 的全部成功生产 job 为屏障；制品下载和逐文件非空断言继续 fail-closed。另含 frontend Vitest、按路径构建候选并在现有门通过后提升镜像。PR 仍独立构建测试 stage；拓扑以 `.github/workflows/ci.yml` 为准 | 是，非纯文档 push / PR |
| 组件集成 | `scripts/test.sh --integration` 统一编排真 Redis、生产 Database 多连接/多进程冷启动、迁移整链失败回滚、已发布历史版本到当前 manifest 的 DR 恢复查询、Gateway Worker、real-docker 和生产 AOF 空环境恢复 | 是，`integration` 两分组均为 required |
| pipeline E2E | 主 CI integration 覆盖 video / document / audio 真实完成事件到 Search / Ask / MCP 命中；Document 矩阵再按 kind/profile 覆盖 HTML、数字 PDF、扫描 PDF | 闭环必经；外部素材需显式触发 |
| 检索质量决策 | 24 个冻结 job 经真实 Scheduler completion 摄入，96 条查询分层评估 Search / MCP / Ask；输出 `retrieval-quality.json` | 是，`decision_evidence_gate` 必须通过 |
| Canonical evidence | 三类 producer sidecar → Scheduler → DB → Search/Ask/MCP/UI；Document 覆盖 HTML segment、译文 range、PDF page+bbox 和歧义 fail-closed | 是，跟随 pipeline integration |
| 条件外网 / 凭证 | `scripts/test.sh --external <article|audio|rss|youtube|youtube_playlist|all>` 统一编排公网场景，缺所选 URL 返回非零；B 站、arXiv 与真实 AI 仍按素材、网络和凭证条件执行 | 否，只在条件满足的受控环境执行 |
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

### NAS source library关键矩阵

| 层级 | 必测不变量 | 入口 |
|---|---|---|
| 引用解析 | 中文相对路径规范往返;绝对/`..`/NUL/反斜线/非视频拒绝;父目录和叶子symlink均不跟随 | `scripts/test.sh -m source_library` |
| API/DB | 准入full SHA-256+大小不符时零Job副作用;幂等manifest绑source身份;详情不泄漏宿主路径 | `scripts/test.sh -m api_jobs` |
| Scheduler/claim | 只有01-04与08步需`source-root:<id>`;错Worker不能原子claim;claim携带ref/digest/size而非绝对路径 | `scripts/test.sh -m scheduler -m runner_ops` |
| Worker/runner | 执行前与产物发布前重验;执行中替换时不push、不done;临时`source.mp4` symlink执行后清理;DooD嵌套容器只读挂同一root | `scripts/test.sh -m worker -m step_runner_docker` |
| Storage/删除 | Local/Remote/Gateway不列举、上传、clone或跟随源symlink;语义exclude在链接被意外替换为普通文件时仍拦上传;删Job后原片逐字节不变 | `scripts/test.sh -m storage -m api_jobs` |

发布前还必须用隔离临时root生成可播视频,走真API、Scheduler、`worker-source`和真step容器;
验证MinIO清单无`parts/*/input/source.mp4`,01-04与08能读原片,05-07及09后可由无root普通Worker执行,且删Job前后临时root的size+SHA-256一致。

### 每步检查项

检查项由 `tests/steps/` 对应步骤用例覆盖：

| 步骤 | 检查项 |
|------|--------|
| 03_scene | scenes.json 可解析、scenes 非空、首 start_sec==0 |
| 04_frames | jpg 数量 ≥ scenes 数、每张 >10KB |
| 05_dedup | 每项有 keep/phash、保留率 25%-100% |
| 06_ocr | 长度 == keep=true 数、nonempty >30%；真实帧 SHA-256 与宽高写入 sidecar，识别中换帧 fail-closed，缺图身份字段为 null |
| 11_smart | >500 字符、有 ## 标题、无拒绝话术 |
| 12_review | 扁平 6 维整数分（completeness/accuracy/structure/terminology/visual_integration/readability）各 1-5、overall 1-5、key_terms 为 `[{term,definition}]`、parse_failed 非 true |

Document 必须额外通过下列并集，不能用小型合成 HTML 替代真实样本：

- source profile：`scholarly_html|generic_html|digital_pdf|scanned_pdf`；kind 至少覆盖
  `research_paper|article|whitepaper`，并验证同一 whitepaper kind 的 HTML/PDF 两种 profile。
- 真实学术样本：FlashAttention 公式、Attention Is All You Need 作者/机构与 Figure 1–5 多 panel、
  Distributed Representations 的源 Figure/Table 数量对账；另含数字 PDF 表格和扫描 PDF OCR。
- 契约：sources/capability 并集、HTML↔PDF 唯一 crosswalk、歧义拒绝、OCR 0.8 exact-evidence 门、
  Figure/Table complete/degraded/rejected、译文 1:1/1:N/N:1 与表格行列/protected token 不变量。
- 消费闭环：真实 Document completion 进入 Search/Ask/MCP，canonical evidence 可回到原文 HTML、译文 HTML
  和 PDF page+bbox；源或 fingerprint 改变后旧定位 stale。
- 前端：图/表分组目录、multi-panel、宽表、缺 media、source crop、deep link 和 PDF overlay 在
  3840×2160、1512×982、440×956 三个 viewport 通过。视觉证据记录 job、浏览器、viewport、截图和时间。

## 3. 集成测试

主 CI 的 required integration 入口直接运行 `scripts/test.sh --integration`。数据库矩阵使用生产 `Database`
验证冷启动、跨连接可见、唯一键竞争和 current+1/current+2 后段故障的整链回滚。固定
format-v1 与 format-v2/schema-v2 归档均经生产 restore 入口恢复，再由当前 Database 执行
`init_schema/get_job/list_jobs`，避免只验压缩包形状而没有验证真实升级和读路径。

主 CI 把上述场景分为 data 与 services 两个独立 Compose project，分别使用 Redis DB 14/15，JUnit、pytest basetemp 和 coverage 工件也互不共享。本地不指定分组时仍串行运行全部场景。

main 的测试 runtime 只装系统和 Python 依赖，不含仓库源码。普通、worker 与 integration runner 和 prepare 同时启动，按同一内容键限时拉取 tag，随后从本地 `RepoDigests` 校验并固定不可变 digest，Compose 再只读挂载当前 checkout。cache miss 时 consumer 等待 prepare 发布，prepare 仍是 coverage gate 的直接前置门。灾备演练启动嵌套容器时使用显式 host repository 路径挂载 `shared/` 与 `configs/`，不能依赖 runtime 内残留源码。runtime key 由 Dockerfile 与忽略版本值后的 pyproject 生成；依赖输入不变时不创建 Buildx builder，cache miss 才构建并发布。PR 不复用 main runtime，继续从 PR checkout 构建最终 test stage，保证依赖和 Dockerfile 改动在合入前 fail-closed。

不验收迁移语义的 unit 测试从 session/xdist-worker 级 current-schema 空库复制独立 SQLite 文件，避免每个用例重放迁移链。`test_db_migrations`、backup/restore、冷启动和多进程 integration 仍必须调用生产 `init_schema`，不得改成模板副本。

涉及学习候选 schema 时，还必须验证旧版本备份恢复后能继续升级到当前 manifest，当前版本快照能通过冻结 migration chain 自校验，未来版本仍在兼容门 fail-closed。测试不得只修改 `PRAGMA user_version` 伪造兼容性。

### 3.1 检索黄金集与向量决策

黄金集固定为 video / document / audio 共 24 个去敏 job；Document 内保留 research_paper/article 分层标签，以及 exact 24、paraphrase 10、
synonym 10、cross-language 20、cross-source 16、unanswerable 16 共 96 条查询。单来源样本保证
中英文各 32、三类内容和 Document kind 分层样本均衡，跨语言两个方向各 10。corpus、query、source artifact、chunk body
和完整 main SHA 都进入工件，不能运行后再移动阈值或替换真值。

评测分别记录 Search、MCP 与 Ask 的 Recall@k、MRR、无命中、不可回答、跨来源覆盖、重复
job/source、引用和延迟，并保存逐 query 有序结果与 miss reason。两个新 SQLite 使用相同生产
pipeline completion 摄入后，ranking digest 必须字节一致。Ask 引用评测由公开 citation validator
校验本次 source manifest；固定测试响应不调用公网 LLM，零引用不能获得真空 precision。

门禁顺序固定：`decision_evidence_gate` 先证明数据、过滤、引用、指纹、已知修复和确定性都可信；
`quality_gate` 再与预声明阈值比较。仅在前者通过且剩余失败全部属于确认的语义缺口时，工件才允许
记录 `semantic_quality_below_threshold_after_known_fixes`。在此之前不得添加 vector dependency、
配置、feature flag、migration、表或列。

触发后的收益门继续使用同一 24/96 冻结数据，并要求目标语义层 Recall@5 至少提升 10 个百分点
或 MRR@10 至少提升 0.08，其他关键层回退不超过 2 个百分点，unanswerable false-positive 恶化
不超过 2 个百分点，Search warm P95 同时不超过同层 FTS5 的 2 倍和 250ms。固定 int8 多语言
ONNX 候选通过质量门且没有引入关键层或 unanswerable 回归，但同容器、同 ASGI Search 路由的
首轮 P95 为 FTS5 的 3.21 倍，三轮确认仍为 2.78、3.65、3.98 倍。因此候选按预声明门关闭，
生产继续只使用 FTS5，且不保留向量依赖、配置、模型、feature flag、migration 或索引 schema。
只有新候选或新执行架构在相同口径下同时通过全部冻结门，才允许重新开启生产实现。

### 3.2 Canonical evidence 闭环

| 层 | 必测场景 |
|---|---|
| producer | video/audio 毫秒范围、Document HTML segment/exact、PDF 真页码/bbox、OCR confidence；smart exact quote 覆盖 Document/audio/video 合法单段整行。HTML/PDF crosswalk 仅唯一匹配成立，低置信 OCR 禁止 exact evidence；支持产物删除、篡改、空白页、Poppler/OCR 失败、路径逃逸、NFC/NFKC 边界和畸形 marker 全部 fail-closed |
| transaction | note/chunk/provenance/source hash 与 evidence fingerprint 可重算；重建索引的 stale 标记与新 valid 集合原子提交 |
| consumers | 同一 chunk 在 Search、Ask、MCP、JobDetail 和 MarkdownViewer 返回同 `evidence_id/status/fingerprint`；概念在精确 evidence 关系落库前保持未接入 |
| resolver | GET 非法/unknown/失效三分；batch 上限100、禁重、保序与 unknown missing 占位；support/manifest 篡改 stale，恢复原字节后同 identity 重新 valid |
| 恶意边界 | 跨 job 绑定、原始 path 穿越、source/note/chunk hash 篡改、text anchor 多解、image asset 变化均不产生 link |
| UI | valid+link 才可点；stale/missing 明示不可跳转；已接入的 media/PDF/text/image 只消费服务端 href，不从 locator 拼 URL |

定向后端契约与前端 Vitest 分别走 `scripts/test.sh -- tests/test_canonical_evidence_consumers.py`
和 `TEST_WARM_NAME=<unique> scripts/test.sh --fe frontend/src/components/evidence/EvidenceLocatorLink.test.ts ...`。

### 3.3 概念定义版本与佐证闭环

| 层 | 必测场景 |
|---|---|
| migration | fresh/v5→v6/fault rollback/backup compatibility；history UPDATE/DELETE 拒绝；current 必须指向本 identity 最新 version；lock revision 不回退、不跳号，domain rename/concept merge 仅允许 identity-transfer `+1` |
| evidence binding | definition version 插入时 evidence 必须是同 domain/term 精确 occurrence 绑定的当前 canonical ID；伪 ID、跨 job/domain/concept、重复/乱序全部拒绝；后续 job/evidence 删除不让历史账本变成不可打开 |
| attestation | reliable review、精确 note path/SHA、chunk excerpt SHA 与 resolver valid 并集；stale/missing/delete/unreliable、重复 source fingerprint 进入 excluded；四级边界按 distinct job/source/content type 可复算 |
| concurrency | manual edit、lock/unlock、自动/手动 resynthesis 使用 current+lock revision CAS；AI 调用期间 attestation 变化必须拒绝；locked/no quorum/source-set noop 不调用或不重复调用 provider |
| scheduler | full-job occurrence 对账清除旧映射；真实 keyword-only Database 接缝；同概念 automatic resynthesis 在途去重、失败不阻塞 completion、shutdown 取消并 gather |
| REST/MCP/UI | REST 与 MCP 同一 detail projection；history/occurrence cap+total；409 reload、502 恢复、route race；只有 valid+link 证据可跳转，TermDetail/Graph 共用 panel |

定向入口至少覆盖 `tests/test_concept_definition_history.py`、`tests/test_concept_occurrences.py`、
`tests/test_concept_attestation.py`、`tests/test_concept_synthesis.py`、`tests/test_scheduler_glossary.py`、
`tests/test_api_glossary.py`、`tests/test_mcp.py` 及对应前端 panel/composable Vitest。最终并集还必须进入
真实 migration/backup integration 和三视口部署验收。

调度器 + Worker + Redis 联调：

```bash
# 启动集成栈（这是起服务，不是跑测试；专用 compose 免鉴权 + DRY_RUN 可选，CI 的 e2e.yml 同款）
docker compose -f docker-compose.integration.yml up -d

# 提交测试任务（TEST_VIDEO_URL 必须能从 integration worker 访问）
curl -X POST 'http://localhost:8000/api/jobs' \
  -H 'Content-Type: application/json' \
  -d '{"content_type":"video","parts":[{"url":"https://example.test/test.mp4","title":"P01"}],"domain":"deep-learning"}'

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


## manifest-v1 测试矩阵

- `tests/test_step_manifest.py` / `test_step_commit.py`:canonical 稳定性、schema fail-closed、围栏状态机与恢复决策表。
- `tests/test_step_output_commit.py`:逐故障点注入(`test_fence_rejection_at_each_checkpoint_blocks_manifest` 等)、换代拒绝(`test_old_exec_cannot_promote_or_publish_after_new_generation`)、Part 并发隔离(`test_two_parts_commit_concurrently_without_crosstalk`)、失败仅诊断(`test_failure_pushes_only_diagnostics_whitelist`)、三后端协议等价(`TestRemoteCommitProtocol`/`TestGatewayCommitEndpoints`)、TTL 续期、stale 越权、幽灵输出。
- `tests/test_step_completion.py`:对账修复幂等(`TestReconcileRepairsProjection`)、缺/损降级失效下游(`TestManifestMissingDemotion`)、Part rerun 失效边界(`TestPartRerunInvalidationBoundary`)、AI/CPU 同一 stale 算法(`TestUnifiedStaleAlgorithm`)、skip 恢复(`TestSkipRecovery`)、backfill 全流程与 fail-closed(`TestBackfill`)。
- dual→manifest-only→cleanup→exact DR 回滚演练与 `--integration` 崩溃恢复门在切换 manifest-only 前执行,见迁移工单。
