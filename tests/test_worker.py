"""tests for worker — 使用 fakeredis + 临时 DB。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import make_fakeredis
from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, Step, StepStatus
from shared.storage import LocalStorage
from worker.worker import Worker, WORKER_POOLS, auto_discover_tags, _resolve_worker_id
from worker.transport import RedisTransport


# ── Fixtures ──


@pytest.fixture
def tmp_jobs_dir(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return jobs


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


@pytest.fixture
def config(tmp_path, tmp_jobs_dir, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines={
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2,
                     "module": "steps.test_a", "timeout_sec": 60},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1,
                     "module": "steps.test_b", "timeout_sec": 60},
                ]
            }
        },
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}, "scene": {"limit": 1}}},
        providers={},
    )


@pytest.fixture
def storage(tmp_jobs_dir):
    return LocalStorage(tmp_jobs_dir)


@pytest.fixture
def worker(redis, db, config, storage):
    w = Worker(
        transport=RedisTransport(redis, db), config=config, storage=storage,
        worker_type="cpu",
        pools=["scene", "cpu", "io"],
        tags={"vision", "gpu"},
        reject_tags={"private"},
    )
    return w


def make_job(pipeline="test", job_id="j_test_001"):
    return Job(id=job_id, content_type="video", pipeline=pipeline, domain="general")


def make_claim(job_id="j_test_001", step="A", pool="cpu", pipeline="test",
               domain="general", style_tags=None, exec_id="w_test:1"):
    """构造一个 execute 入参 claim(等价 request_step 的返回)。"""
    return {
        "job_id": job_id, "step": step, "pool": pool, "exec_id": exec_id,
        "pipeline": pipeline, "domain": domain, "style_tags": style_tags or [],
    }


async def setup_task_in_queue(redis, pool="cpu", job_id="j_test_001", step="A", tags=None, priority=0):
    """Helper: enqueue a task and set it as ready."""
    await redis.enqueue_step(pool, job_id, step, tags or [], priority)
    await redis.set_step_status(job_id, step, "ready")
    await redis.init_job(job_id, "test", {"domain": "general", "style_tags": "[]"})


async def request_step(worker):
    """Helper: 通过 transport 走完整认领(等价旧 worker.fetch_task)。"""
    return await worker.transport.request_step(
        worker.worker_id, worker.pools, worker._pool_limits(),
        worker.tags, worker.reject_tags,
    )


# ── Tests ──


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_writes_redis_and_db(self, worker, redis, db):
        await worker.register()

        info = await redis.get_worker_info(worker.worker_id)
        assert info is not None
        assert info["type"] == "cpu"
        assert info["status"] == "idle"
        assert "hostname" in info

        db_worker = db.get_worker(worker.worker_id)
        assert db_worker is not None
        assert db_worker.type == "cpu"
        # 刚注册即心跳，公共状态衍生为 online-idle（存量列仍是 idle）
        assert db_worker.status == "online-idle"


class TestTagMatching:
    """标签匹配现由 request_step 编排(原 pop_matching_task);worker 仅设 pools=[cpu] 隔离。"""

    @pytest.fixture(autouse=True)
    def _cpu_only(self, worker):
        worker.pools = ["cpu"]

    @pytest.mark.asyncio
    async def test_accept_matching_require_tags(self, worker, redis):
        """require_tags ⊆ worker.tags → accept"""
        await redis.enqueue_step("cpu", "j1", "A", ["vision"], priority=0,
                                 require_tags=["vision"])
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        claim = await request_step(worker)
        assert claim is not None
        assert claim["job_id"] == "j1"

    @pytest.mark.asyncio
    async def test_reject_tags_block(self, worker, redis):
        """tags ∩ reject_tags ≠ ∅ → put back (even if require_tags match)"""
        await redis.enqueue_step("cpu", "j1", "A", ["vision", "private"], priority=0,
                                 require_tags=["vision"])
        claim = await request_step(worker)
        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # put back

    @pytest.mark.asyncio
    async def test_insufficient_require_tags(self, worker, redis):
        """require_tags ⊄ worker.tags → put back"""
        await redis.enqueue_step("cpu", "j1", "A", ["heavy"], priority=0,
                                 require_tags=["heavy"])
        claim = await request_step(worker)
        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_empty_require_tags_always_match(self, worker, redis):
        """step with no require_tags matches any worker"""
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        claim = await request_step(worker)
        assert claim is not None

    @pytest.mark.asyncio
    async def test_domain_tags_dont_block_when_not_in_require(self, worker, redis):
        """domain/style tags in 'tags' but not in 'require_tags' do not block matching."""
        # worker has tags={"vision","gpu"}, reject_tags={"private"}
        # domain tags that are not in reject_tags
        await redis.enqueue_step("cpu", "j1", "A",
                                 tags=["vision", "nlp", "lecture"],
                                 priority=0,
                                 require_tags=["vision"])
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        claim = await request_step(worker)
        assert claim is not None

    @pytest.mark.asyncio
    async def test_domain_tags_still_enable_reject(self, worker, redis):
        """domain tags should still be checked against reject_tags."""
        # worker has reject_tags={"private"}
        await redis.enqueue_step("cpu", "j1", "A",
                                 tags=["private", "case-study"],
                                 priority=0,
                                 require_tags=[])
        claim = await request_step(worker)
        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1


class TestCAS:
    @pytest.mark.asyncio
    async def test_cas_prevents_double_execution(self, worker, redis, db):
        await setup_task_in_queue(redis)

        acquired1 = await redis.cas_step_status("j_test_001", "A", "ready", "running")
        acquired2 = await redis.cas_step_status("j_test_001", "A", "ready", "running")

        assert acquired1 is True
        assert acquired2 is False

    @pytest.mark.asyncio
    async def test_slot_release_on_cas_fail(self, worker, redis):
        """When CAS fails (step already running), request_step releases the acquired slot."""
        worker.pools = ["cpu"]
        await setup_task_in_queue(redis)
        # 队列里有任务但状态已是 running → CAS ready->running 失败。
        await redis.set_step_status("j_test_001", "A", "running")

        claim = await request_step(worker)
        assert claim is None

        count = await redis.get_pool_count("cpu")
        assert count == 0


class TestPaused:
    @pytest.mark.asyncio
    async def test_paused_returns_none(self, worker, redis):
        await worker.register()
        await redis.set_worker_field(worker.worker_id, "admin_status", "paused")
        await setup_task_in_queue(redis)

        claim = await request_step(worker)
        assert claim is None


class TestNoPoolFreeze:
    """scene 已并入 cpu 池,取消 scene↔cpu 全局冻结:认领/释放任何池都不再自动冻结其他池。"""
    @pytest.mark.asyncio
    async def test_claiming_cpu_step_does_not_freeze(self, worker, redis):
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        worker.pools = ["cpu"]

        claim = await request_step(worker)
        assert claim is not None
        assert claim["pool"] == "cpu"
        # 关键:认领 cpu 步全程零冻结(旧版认领 scene 会冻 cpu,现已移除)。
        assert await redis.is_pool_frozen("cpu") is False


class TestSlotRelease:
    @pytest.mark.asyncio
    async def test_slot_released_after_task(self, worker, redis, tmp_jobs_dir):
        """Slot count returns to 0 after execute (regardless of success)."""
        await setup_task_in_queue(redis)
        await redis.try_acquire_slot("cpu", limit=3)
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            return 0, ""

        worker.runner.run_step = mock_run_step
        await worker.execute(make_claim())

        count = await redis.get_pool_count("cpu")
        assert count == 0


class TestUseGpuGating:
    """直接驱动真实 worker.execute,捕获传给 runner 的 StepContext.use_gpu,
    覆盖 worker.py 内联表达式 use_gpu=("gpu" in tags) and (pool=="gpu" or "gpu" in raw_tags)。
    取代此前在 test_step_runner_docker.py 里复刻该表达式只断副本(改真实代码测试仍绿)的做法。"""

    async def _captured_use_gpu(self, worker, tmp_jobs_dir, *, step="A", pool="cpu"):
        (tmp_jobs_dir / "j_gpu").mkdir(exist_ok=True)
        captured = {}

        async def mock_run_step(ctx, on_progress, on_tick):
            captured["use_gpu"] = ctx.use_gpu
            return 0, ""

        worker.runner.run_step = mock_run_step
        await worker.execute(make_claim(job_id="j_gpu", step=step, pool=pool))
        return captured["use_gpu"]

    @pytest.mark.asyncio
    async def test_gpu_tag_and_gpu_pool(self, worker, tmp_jobs_dir):
        # worker 具 gpu 标签 + 认到 gpu 池 → 启用。
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="gpu") is True

    @pytest.mark.asyncio
    async def test_gpu_tag_cpu_pool_no_raw_gpu(self, worker, tmp_jobs_dir):
        # 具 gpu 标签但 cpu 池且步骤配置无 gpu 标签 → 不启用(挡误启)。
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="cpu") is False

    @pytest.mark.asyncio
    async def test_gpu_tag_cpu_pool_step_tagged_gpu(self, worker, tmp_jobs_dir):
        # cpu 池但步骤配置 tags 含 gpu → 启用(覆盖 raw.get("tags") 分支)。
        worker.config.pipelines["test"]["steps"][0]["tags"] = ["gpu"]  # step "A"
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="cpu") is True

    @pytest.mark.asyncio
    async def test_no_gpu_worker_tag(self, worker, tmp_jobs_dir):
        # worker 不具 gpu 标签 → 即便 gpu 池也不启用(挡漏判/误启)。
        worker.tags = {"vision"}
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="gpu") is False


class TestPoolFrozen:
    @pytest.mark.asyncio
    async def test_frozen_pool_skipped(self, worker, redis):
        await worker.register()
        await redis.freeze_pool("cpu")
        await redis.freeze_pool("scene")
        await redis.freeze_pool("io")
        await setup_task_in_queue(redis)

        claim = await request_step(worker)
        assert claim is None


class TestIdleTimeout:
    @pytest.mark.asyncio
    async def test_idle_timeout_exit(self, worker, redis):
        worker.idle_timeout = 1
        await worker.register()

        start = time.time()

        async def empty_request(*args, **kwargs):
            return None

        worker.transport.request_step = empty_request
        await worker._claim_loop()
        elapsed = time.time() - start
        assert elapsed >= 1.0


class TestAutoDiscoverTags:
    def test_anthropic_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            tags = auto_discover_tags()
            assert "vision" in tags

    def test_deepseek_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ds-test"}, clear=False):
            tags = auto_discover_tags()
            assert "text-only" in tags

    def test_no_keys(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value=None):
                with patch("os.path.exists", return_value=False):
                    tags = auto_discover_tags()
                    assert "vision" not in tags
                    assert "gpu" not in tags

    def test_claude_binary_present_but_not_authed(self):
        # 镜像自带 claude 二进制但无凭证(纯 gateway worker)→ 不该标 vision/claude-cli
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                with patch("worker.worker._claude_logged_in", return_value=False):
                    tags = auto_discover_tags()
                    assert "vision" not in tags
                    assert "claude-cli" not in tags

    def test_claude_logged_in_adds_vision_and_cli(self):
        # claude 订阅已登录(~/.claude/.credentials.json 在)→ 标 vision + claude-cli
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                with patch("worker.worker._claude_logged_in", return_value=True):
                    tags = auto_discover_tags()
                    assert "vision" in tags
                    assert "claude-cli" in tags


class TestWorkerPools:
    def test_default_pools(self):
        # Structural validation instead of copying hardcoded values
        for wtype, pools in WORKER_POOLS.items():
            assert isinstance(pools, list), f"{wtype} pools should be a list"
            assert len(pools) > 0, f"{wtype} should have at least one pool"
        # gpu 保留 cpu fallback;scene 已并入 cpu(无独立 scene 池)。
        assert WORKER_POOLS["gpu"] == ["gpu", "cpu"]
        assert WORKER_POOLS["cpu"] == ["cpu"]
        assert WORKER_POOLS["io"] == ["io"]
        assert WORKER_POOLS["ai"] == ["ai"]
        # 下载隔离:只有 io 订 io 池;cpu/ai/gpu 都不下载。
        assert "io" not in WORKER_POOLS["cpu"]
        assert "io" not in WORKER_POOLS["ai"]
        assert "io" not in WORKER_POOLS["gpu"]
        # 无 scene 池残留。
        assert all("scene" not in p for p in WORKER_POOLS.values())
        # All types should exist
        assert set(WORKER_POOLS.keys()) >= {"io", "cpu", "ai", "gpu"}


class TestUpdateWorkerStatus:
    @pytest.mark.asyncio
    async def test_updates_redis_fields(self, worker, redis):
        await worker.register()
        await worker.transport.update_status(worker.worker_id, "busy", "j1", "A")

        info = await redis.get_worker_info(worker.worker_id)
        assert info["status"] == "busy"
        assert info["current_job"] == "j1"
        assert info["current_step"] == "A"

    @pytest.mark.asyncio
    async def test_updates_db_fields(self, worker, redis, db):
        # /api/workers 读 DB，状态变更必须写回 DB
        await worker.register()
        await worker.transport.update_status(worker.worker_id, "busy", "j1", "A")

        got = db.get_worker(worker.worker_id)
        # 心跳新鲜 + 有在跑任务 -> 公共状态衍生为 online-busy
        assert got.status == "online-busy"
        assert got.current_job == "j1"
        assert got.current_step == "A"

    @pytest.mark.asyncio
    async def test_clears_on_idle(self, worker, redis):
        await worker.register()
        await worker.transport.update_status(worker.worker_id, "busy", "j1", "A")
        await worker.transport.update_status(worker.worker_id, "idle")

        info = await redis.get_worker_info(worker.worker_id)
        assert info["status"] == "idle"
        assert info["current_job"] == ""
        assert info["current_step"] == ""


class TestHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_db(self, worker, redis, db, monkeypatch):
        # 心跳循环必须刷新 DB 的 last_heartbeat，否则前端 30s 后判 offline
        from datetime import datetime, timedelta, timezone

        await worker.register()
        # 人为把 DB 心跳改老
        w = db.get_worker(worker.worker_id)
        w.last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.upsert_worker(w)

        # 跑一轮心跳循环后退出
        original_sleep = asyncio.sleep

        async def stop_after_first(_secs):
            worker._shutdown = True
            await original_sleep(0)

        monkeypatch.setattr("worker.worker.asyncio.sleep", stop_after_first)
        await worker.heartbeat_loop()

        got = db.get_worker(worker.worker_id)
        assert (datetime.now(timezone.utc) - got.last_heartbeat).total_seconds() < 5


class TestFetchTask:
    @pytest.mark.asyncio
    async def test_fetches_from_first_available_pool(self, worker, redis):
        await worker.register()
        await setup_task_in_queue(redis, pool="cpu")

        claim = await request_step(worker)
        assert claim is not None
        assert claim["pool"] == "cpu"

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, worker, redis):
        await worker.register()
        claim = await request_step(worker)
        assert claim is None


class TestParseErrorType:
    def test_reads_error_json(self, worker, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j1"
        job_dir.mkdir()
        error_data = {"error_type": "ai_rate_limit", "message": "429"}
        (job_dir / ".A.error.json").write_text(json.dumps(error_data))

        assert worker._parse_error_type(job_dir, "A") == "ai_rate_limit"

    def test_missing_file(self, worker, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j2"
        job_dir.mkdir()
        assert worker._parse_error_type(job_dir, "A") == "unknown"

    def test_corrupt_json(self, worker, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j3"
        job_dir.mkdir()
        (job_dir / ".A.error.json").write_text("not json")
        assert worker._parse_error_type(job_dir, "A") == "unknown"


class TestExecuteFullFlow:
    """execute 全流程测试：mock _run_step 避免真实子进程。"""

    @pytest.mark.asyncio
    async def test_success_publishes_and_updates_db(self, worker, redis, db, tmp_jobs_dir):
        await worker.register()  # 让 transport._worker_id 与 worker.worker_id 一致
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        completed_events = []

        async def capture_completed():
            async for msg in redis.subscribe("step_completed"):
                completed_events.append(msg)
                break

        async def mock_run_step(ctx, on_progress, on_tick):
            return 0, ""

        worker.runner.run_step = mock_run_step

        listener = asyncio.create_task(capture_completed())
        await asyncio.sleep(0.05)
        await worker.execute(make_claim())
        await asyncio.wait_for(listener, timeout=2.0)

        assert len(completed_events) == 1
        assert completed_events[0]["status"] == "done"
        assert completed_events[0]["job_id"] == "j_test_001"
        assert completed_events[0]["exec_id"] == "w_test:1"

        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.DONE
        assert db_step.worker_id == worker.worker_id

        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_minimal_claim_resolves_pipeline_via_transport(self, worker, redis, db, tmp_jobs_dir):
        # 最小 claim(无 pipeline/domain/style_tags)→ execute 在 try 内经 transport 回读后跑完。
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "lecture", "style_tags": "[]"})
        await redis.try_acquire_slot("cpu", limit=3)
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            return 0, ""
        worker.runner.run_step = mock_run_step

        await worker.execute({"job_id": "j_test_001", "step": "A",
                              "pool": "cpu", "exec_id": "w_test:1"})

        assert db.get_steps("j_test_001")[0].status == StepStatus.DONE

    @pytest.mark.asyncio
    async def test_job_read_failure_fails_step_not_crash(self, worker, redis, db, tmp_jobs_dir):
        # get_job_pipeline 抛错 → 被 execute 接住转 report_failed:步骤判失败、槽位释放、worker 不崩。
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.RUNNING, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def boom(job_id):
            raise RuntimeError("redis down")
        worker.transport.get_job_pipeline = boom

        await worker.execute({"job_id": "j_test_001", "step": "A",
                              "pool": "cpu", "exec_id": "w_test:1"})

        assert db.get_steps("j_test_001")[0].status == StepStatus.FAILED
        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_failure_publishes_events_and_updates_db(self, worker, redis, db, tmp_jobs_dir):
        await worker.register()
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        failed_events = []
        ws_events = []

        async def capture_failed():
            async for msg in redis.subscribe("step_failed"):
                failed_events.append(msg)
                break

        async def capture_ws():
            async for msg in redis.subscribe(f"events:j_test_001"):
                if msg.get("event") == "step_failed":
                    ws_events.append(msg)
                    break

        async def mock_run_step(ctx, on_progress, on_tick):
            return 1, "segfault"

        worker.runner.run_step = mock_run_step

        listener1 = asyncio.create_task(capture_failed())
        listener2 = asyncio.create_task(capture_ws())
        await asyncio.sleep(0.05)
        await worker.execute(make_claim())
        await asyncio.wait_for(listener1, timeout=2.0)
        await asyncio.wait_for(listener2, timeout=2.0)

        assert len(failed_events) == 1
        assert failed_events[0]["status"] == "failed"
        # rc!=0 分支带 exec_id(保留旧 payload 差异)
        assert failed_events[0]["exec_id"] == "w_test:1"

        assert len(ws_events) == 1
        assert ws_events[0]["event"] == "step_failed"

        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.FAILED

        assert await redis.get_pool_count("cpu") == 0
        # rc!=0 计入 failed 统计(count_stats=True)
        db_worker = db.get_worker(worker.worker_id)
        assert db_worker.tasks_failed == 1


class TestSubprocessTimeout:
    @pytest.mark.asyncio
    async def test_timeout_publishes_failure(self, worker, redis, db, tmp_jobs_dir):
        """When run_step times out, execute should publish step_failed with timeout error."""
        await worker.register()
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        async def mock_run_step_timeout(ctx, on_progress, on_tick):
            raise asyncio.TimeoutError()

        worker.runner.run_step = mock_run_step_timeout

        failed_events = []

        async def capture_failed():
            async for msg in redis.subscribe("step_failed"):
                failed_events.append(msg)
                break

        listener = asyncio.create_task(capture_failed())
        await asyncio.sleep(0.05)
        await worker.execute(make_claim())
        await asyncio.wait_for(listener, timeout=2.0)

        assert len(failed_events) == 1
        assert "timeout" in failed_events[0].get("error", "").lower() or failed_events[0].get("error_type") == "timeout"
        # timeout 分支不带 exec_id(保留旧 payload 差异)
        assert "exec_id" not in failed_events[0]
        # Slot should be released
        assert await redis.get_pool_count("cpu") == 0
        # timeout 分支不计 failed 统计(count_stats=False)
        db_worker = db.get_worker(worker.worker_id)
        assert db_worker.tasks_failed == 0


class TestPoolExhaustion:
    @pytest.mark.asyncio
    async def test_full_pool_returns_none(self, worker, redis):
        """When pool is at capacity, fetch_task should return None for that pool."""
        await worker.register()
        # Fill pool to capacity (limit=3 in fixture)
        for _ in range(3):
            await redis.try_acquire_slot("cpu", limit=3)

        await setup_task_in_queue(redis, pool="cpu")
        # request_step should skip cpu pool because it's full
        # But it tries other pools too. We need to also exhaust scene and io.
        await redis.freeze_pool("scene")
        await redis.freeze_pool("io")

        claim = await request_step(worker)
        assert claim is None


class TestStoragePullFailure:
    @pytest.mark.asyncio
    async def test_pull_failure_releases_slot_and_publishes_failed(self, worker, redis, db, tmp_jobs_dir):
        """When storage.pull raises, slot released + step_failed published + DB updated."""
        await worker.register()
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)

        async def failing_pull(job_id, step):
            raise IOError("disk full")

        worker.storage.pull = failing_pull

        failed_events = []

        async def capture_failed():
            async for msg in redis.subscribe("step_failed"):
                failed_events.append(msg)
                break

        listener = asyncio.create_task(capture_failed())
        await asyncio.sleep(0.05)
        await worker.execute(make_claim())
        await asyncio.wait_for(listener, timeout=2.0)

        assert await redis.get_pool_count("cpu") == 0
        assert len(failed_events) == 1
        assert "disk full" in failed_events[0]["error"]
        # 通用异常分支不带 exec_id(保留旧 payload 差异)
        assert "exec_id" not in failed_events[0]
        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.FAILED
        # 通用异常分支不计 failed 统计(count_stats=False)
        db_worker = db.get_worker(worker.worker_id)
        assert db_worker.tasks_failed == 0


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_stops_main_loop(self, worker, redis):
        """shutdown() sets _shutdown flag, _claim_loop should exit."""
        await worker.register()
        worker.idle_timeout = 999  # Don't exit from idle

        async def schedule_shutdown():
            await asyncio.sleep(0.1)
            worker.shutdown()

        asyncio.create_task(schedule_shutdown())
        await asyncio.wait_for(worker._claim_loop(), timeout=2.0)
        # If we reach here, _claim_loop exited due to shutdown


class TestConcurrency:
    def test_default_is_one(self, worker):
        assert worker.concurrency == 1

    def test_clamped_to_min_one(self, redis, db, config, storage):
        w = Worker(
            transport=RedisTransport(redis, db), config=config, storage=storage,
            worker_type="cpu", pools=["cpu", "io"], tags=set(), reject_tags=set(),
            concurrency=0,
        )
        assert w.concurrency == 1

    @pytest.mark.asyncio
    async def test_run_starts_n_claim_loops(self, redis, db, config, storage):
        """concurrency=N → run() 起 N 条认领循环(各带 slot 序号)。全局每池槽位仍是系统级上限。"""
        w = Worker(
            transport=RedisTransport(redis, db), config=config, storage=storage,
            worker_type="cpu", pools=["cpu", "io"], tags=set(), reject_tags=set(),
            concurrency=3,
        )
        slots: list[int] = []

        async def fake_loop(slot=0):
            slots.append(slot)

        async def fake_hb():
            return

        w._claim_loop = fake_loop
        w.heartbeat_loop = fake_hb
        await w.run()
        assert sorted(slots) == [0, 1, 2]


class TestUploadFaultTolerance:
    """上报通道抖动不得污染步骤结论 / 杀 worker(审计 I-H2/I-H3)。"""

    @pytest.mark.asyncio
    async def test_success_not_flipped_when_usage_collection_raises(
        self, worker, redis, db, tmp_jobs_dir
    ):
        # returncode==0 的成功步骤,即使 usage 收集/上报抛错,也必须保持 DONE 而非被翻成 FAILED。
        import worker.worker as worker_mod

        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", limit=3)
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            return 0, ""
        worker.runner.run_step = mock_run_step

        def boom(*_a, **_k):
            raise RuntimeError("usage parse/upload exploded")
        # _collect_usage 内部依赖,模拟 usage 收集/上报抖动。
        with patch.object(worker_mod, "collect_usage_from_file", boom):
            await worker.execute(make_claim())

        # 成功步骤未被上报通道抖动翻盘。
        assert db.get_steps("j_test_001")[0].status == StepStatus.DONE
        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_gateway_upload_methods_best_effort_on_http_error(self, monkeypatch):
        # gateway 上报四法遇 httpx 错误必须 best-effort(重试后只 log,不抛);否则 execute 的
        # finally release 抛出会逃逸 _claim_loop 杀掉整个 worker(审计 I-H3)。
        import httpx
        import worker.gateway_transport as gw_mod
        from worker.gateway_transport import GatewayTransport
        from shared.models import AIUsage

        async def _no_sleep(*_a, **_k):
            return None
        monkeypatch.setattr(gw_mod.asyncio, "sleep", _no_sleep)  # 别让重试退避拖慢测试

        gt = GatewayTransport(
            "https://gw.example", registration_token="t",
            id_file="/tmp/.wid_beff_test", inner=None,
        )

        class _BoomClient:
            async def post(self, *a, **k):
                raise httpx.ConnectError("gateway down")
        gt._client = _BoomClient()

        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "w:1"}
        # 任一上报抛出即视为缺陷;以下四调用均应静默返回 None。
        assert await gt.report_done(claim, 1.0, 0.0) is None
        assert await gt.report_failed(
            claim, "e", "processing", 1.0, 0.0, count_stats=False) is None
        assert await gt.release(claim) is None
        usage = AIUsage(
            exec_id="w:1", provider="p", model="m", job_id="j1", step="A",
            input_tokens=1, output_tokens=1, cost_usd=0.0, duration_sec=0.1, cached=False,
        )
        assert await gt.record_ai_usage(usage) is None


class TestWorkerIdentity:
    def test_worker_name_deterministic(self, tmp_path, monkeypatch):
        """设了 WORKER_NAME → id = {type}-sha256(name)[:8],确定性:重复解析/删缓存都同一 id。"""
        import hashlib
        monkeypatch.setenv("WORKER_NAME", "nas-cpu")
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "id"))
        expect = f"cpu-{hashlib.sha256(b'nas-cpu').hexdigest()[:8]}"
        assert _resolve_worker_id("cpu") == expect
        (tmp_path / "id").unlink(missing_ok=True)
        assert _resolve_worker_id("cpu") == expect  # 不依赖缓存

    def test_distinct_names_distinct_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "id"))
        monkeypatch.setenv("WORKER_NAME", "claude-1")
        a = _resolve_worker_id("ai")
        monkeypatch.setenv("WORKER_NAME", "claude-2")
        b = _resolve_worker_id("ai")
        assert a != b and a.startswith("ai-") and b.startswith("ai-")

    def test_no_name_falls_back_to_cached(self, tmp_path, monkeypatch):
        """没 WORKER_NAME → 随机 {type}-{8hex} 缓存,二次解析复用同一 id。"""
        monkeypatch.delenv("WORKER_NAME", raising=False)
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "id"))
        first = _resolve_worker_id("cpu")
        assert first.startswith("cpu-")
        assert _resolve_worker_id("cpu") == first

    def test_default_id_file_under_workers_dir(self, monkeypatch):
        """默认 id 文件收进 /data/workers/,不再散在 /data 根。"""
        from worker.transport import default_worker_id_file
        monkeypatch.delenv("WORKER_ID_FILE", raising=False)
        monkeypatch.delenv("WORKER_NAME", raising=False)
        assert default_worker_id_file() == "/data/workers/worker.id"
        monkeypatch.setenv("WORKER_NAME", "claude-1")
        assert default_worker_id_file() == "/data/workers/claude-1"
