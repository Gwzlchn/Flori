"""tests for worker — 使用 fakeredis + 临时 DB。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import fakeredis.aioredis

from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, Step, StepStatus
from shared.redis_client import RedisClient
from shared.storage import LocalStorage
from worker.worker import Worker, WORKER_POOLS, auto_discover_tags
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
    client = RedisClient.__new__(RedisClient)
    client._url = "redis://fake"
    client._redis = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
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


async def setup_task_in_queue(redis, pool="cpu", job_id="j_test_001", step="A", tags=None, priority=0):
    """Helper: enqueue a task and set it as ready."""
    await redis.enqueue_step(pool, job_id, step, tags or [], priority)
    await redis.set_step_status(job_id, step, "ready")
    await redis.init_job(job_id, "test", {"domain": "general", "style_tags": "[]"})


def _minimal_step_cfg(config, step="A", timeout_sec=10):
    """构造 _run_step 需要的最小 step_cfg(只有 timeout 被 _run_step 读)。"""
    return {
        "step": {"name": step, "pool": "cpu", "timeout_sec": timeout_sec, "retries": 1},
        "ai": {},
        "domain": {"name": "general"},
        "style_tags": [],
        "paths": {
            "data_dir": str(config.data_dir),
            "prompts_dir": str(config.prompts_dir),
            "config_dir": str(config.config_dir),
        },
        "providers": {},
    }


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
    @pytest.mark.asyncio
    async def test_accept_matching_require_tags(self, worker, redis):
        """require_tags ⊆ worker.tags → accept"""
        await redis.enqueue_step("cpu", "j1", "A", ["vision"], priority=0,
                                 require_tags=["vision"])
        result = await worker.pop_matching_task("cpu")
        assert result is not None
        task, _raw, _score = result
        assert task["job_id"] == "j1"

    @pytest.mark.asyncio
    async def test_reject_tags_block(self, worker, redis):
        """tags ∩ reject_tags ≠ ∅ → put back (even if require_tags match)"""
        await redis.enqueue_step("cpu", "j1", "A", ["vision", "private"], priority=0,
                                 require_tags=["vision"])
        result = await worker.pop_matching_task("cpu")
        assert result is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # put back

    @pytest.mark.asyncio
    async def test_insufficient_require_tags(self, worker, redis):
        """require_tags ⊄ worker.tags → put back"""
        await redis.enqueue_step("cpu", "j1", "A", ["heavy"], priority=0,
                                 require_tags=["heavy"])
        result = await worker.pop_matching_task("cpu")
        assert result is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_empty_require_tags_always_match(self, worker, redis):
        """step with no require_tags matches any worker"""
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        result = await worker.pop_matching_task("cpu")
        assert result is not None

    @pytest.mark.asyncio
    async def test_domain_tags_dont_block_when_not_in_require(self, worker, redis):
        """domain/style tags in 'tags' but not in 'require_tags' do not block matching."""
        # worker has tags={"vision","gpu"}, reject_tags={"private"}
        # domain tags that are not in reject_tags
        await redis.enqueue_step("cpu", "j1", "A",
                                 tags=["vision", "nlp", "lecture"],
                                 priority=0,
                                 require_tags=["vision"])
        result = await worker.pop_matching_task("cpu")
        assert result is not None

    @pytest.mark.asyncio
    async def test_domain_tags_still_enable_reject(self, worker, redis):
        """domain tags should still be checked against reject_tags."""
        # worker has reject_tags={"private"}
        await redis.enqueue_step("cpu", "j1", "A",
                                 tags=["private", "case-study"],
                                 priority=0,
                                 require_tags=[])
        result = await worker.pop_matching_task("cpu")
        assert result is None
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
        """When CAS fails, resource slot should be released."""
        await setup_task_in_queue(redis)
        await redis.try_acquire_slot("cpu", limit=3)

        await redis.set_step_status("j_test_001", "A", "running")

        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}
        await worker.execute(task)

        count = await redis.get_pool_count("cpu")
        assert count == 0


class TestDraining:
    @pytest.mark.asyncio
    async def test_draining_returns_none(self, worker, redis):
        await worker.register()
        await redis.set_worker_field(worker.worker_id, "status", "draining")
        await setup_task_in_queue(redis)

        task = await worker.fetch_task()
        assert task is None


class TestSceneFreeze:
    @pytest.mark.asyncio
    async def test_scene_freezes_cpu(self, worker, redis):
        """Acquiring scene pool freezes cpu pool."""
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        worker.pools = ["scene", "cpu"]

        task = await worker.fetch_task()
        assert task is not None
        assert task["pool"] == "scene"
        assert await redis.is_pool_frozen("cpu") is True

    @pytest.mark.asyncio
    async def test_scene_unfreezes_on_release(self, worker, redis):
        """After scene task completes, cpu pool unfreezes."""
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "running")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        await redis.freeze_pool("cpu")
        await redis.try_acquire_slot("scene", limit=1)

        task = {"job_id": "j1", "step": "A", "pool": "scene"}
        await worker.execute(task)

        assert await redis.is_pool_frozen("cpu") is False


class TestSlotRelease:
    @pytest.mark.asyncio
    async def test_slot_released_after_task(self, worker, redis):
        """Slot count returns to 0 after execute (regardless of success)."""
        await setup_task_in_queue(redis)
        await redis.try_acquire_slot("cpu", limit=3)

        await redis.set_step_status("j_test_001", "A", "running")
        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}
        await worker.execute(task)

        count = await redis.get_pool_count("cpu")
        assert count == 0


class TestPoolFrozen:
    @pytest.mark.asyncio
    async def test_frozen_pool_skipped(self, worker, redis):
        await worker.register()
        await redis.freeze_pool("cpu")
        await redis.freeze_pool("scene")
        await redis.freeze_pool("io")
        await setup_task_in_queue(redis)

        task = await worker.fetch_task()
        assert task is None


class TestIdleTimeout:
    @pytest.mark.asyncio
    async def test_idle_timeout_exit(self, worker, redis):
        worker.idle_timeout = 1
        await worker.register()

        start = time.time()

        async def empty_fetch():
            return None

        worker.fetch_task = empty_fetch
        await worker.main_loop()
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


class TestWorkerPools:
    def test_default_pools(self):
        # Structural validation instead of copying hardcoded values
        for wtype, pools in WORKER_POOLS.items():
            assert isinstance(pools, list), f"{wtype} pools should be a list"
            assert len(pools) > 0, f"{wtype} should have at least one pool"
        # GPU type should include scene and cpu pools
        assert "scene" in WORKER_POOLS["gpu"]
        assert "cpu" in WORKER_POOLS["gpu"]
        # All types should exist
        assert set(WORKER_POOLS.keys()) >= {"download", "cpu", "ai", "gpu"}


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

        task = await worker.fetch_task()
        assert task is not None
        assert task["pool"] == "cpu"

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, worker, redis):
        await worker.register()
        task = await worker.fetch_task()
        assert task is None


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
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "general", "style_tags": "[]"})
        await redis.set_step_status("j_test_001", "A", "ready")
        await redis.try_acquire_slot("cpu", limit=3)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        completed_events = []

        async def capture_completed():
            async for msg in redis.subscribe("step_completed"):
                completed_events.append(msg)
                break

        async def mock_run_step(job_id, step, work_dir, exec_id, step_cfg, module):
            return 0, ""

        worker._run_step = mock_run_step
        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}

        listener = asyncio.create_task(capture_completed())
        await asyncio.sleep(0.05)
        await worker.execute(task)
        await asyncio.wait_for(listener, timeout=2.0)

        assert len(completed_events) == 1
        assert completed_events[0]["status"] == "done"
        assert completed_events[0]["job_id"] == "j_test_001"

        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.DONE
        assert db_step.worker_id == worker.worker_id

        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_failure_publishes_events_and_updates_db(self, worker, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "general", "style_tags": "[]"})
        await redis.set_step_status("j_test_001", "A", "ready")
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

        async def mock_run_step(job_id, step, work_dir, exec_id, step_cfg, module):
            return 1, "segfault"

        worker._run_step = mock_run_step
        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}

        listener1 = asyncio.create_task(capture_failed())
        listener2 = asyncio.create_task(capture_ws())
        await asyncio.sleep(0.05)
        await worker.execute(task)
        await asyncio.wait_for(listener1, timeout=2.0)
        await asyncio.wait_for(listener2, timeout=2.0)

        assert len(failed_events) == 1
        assert failed_events[0]["status"] == "failed"

        assert len(ws_events) == 1
        assert ws_events[0]["event"] == "step_failed"

        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.FAILED

        assert await redis.get_pool_count("cpu") == 0


class TestSubprocessTimeout:
    @pytest.mark.asyncio
    async def test_timeout_publishes_failure(self, worker, redis, db, tmp_jobs_dir):
        """When _run_step times out, execute should publish step_failed with timeout error."""
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "general", "style_tags": "[]"})
        await redis.set_step_status("j_test_001", "A", "ready")
        await redis.try_acquire_slot("cpu", limit=3)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        async def mock_run_step_timeout(job_id, step, work_dir, exec_id, step_cfg, module):
            raise asyncio.TimeoutError()

        worker._run_step = mock_run_step_timeout
        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}

        failed_events = []

        async def capture_failed():
            async for msg in redis.subscribe("step_failed"):
                failed_events.append(msg)
                break

        listener = asyncio.create_task(capture_failed())
        await asyncio.sleep(0.05)
        await worker.execute(task)
        await asyncio.wait_for(listener, timeout=2.0)

        assert len(failed_events) == 1
        assert "timeout" in failed_events[0].get("error", "").lower() or failed_events[0].get("error_type") == "timeout"
        # Slot should be released
        assert await redis.get_pool_count("cpu") == 0


class TestPoolExhaustion:
    @pytest.mark.asyncio
    async def test_full_pool_returns_none(self, worker, redis):
        """When pool is at capacity, fetch_task should return None for that pool."""
        await worker.register()
        # Fill pool to capacity (limit=3 in fixture)
        for _ in range(3):
            await redis.try_acquire_slot("cpu", limit=3)

        await setup_task_in_queue(redis, pool="cpu")
        # The worker's fetch_task should skip cpu pool because it's full
        # But it tries other pools too. We need to also exhaust scene and io.
        await redis.freeze_pool("scene")
        await redis.freeze_pool("io")

        task = await worker.fetch_task()
        assert task is None


class TestMaxTriesExhaustion:
    @pytest.mark.asyncio
    async def test_max_tries_returns_none(self, worker, redis):
        """When queue has many non-matching tasks, pop_matching_task gives up after max_tries."""
        for i in range(6):
            await redis.enqueue_step("cpu", f"j_{i}", "A", ["exotic_tag"], priority=0,
                                     require_tags=["exotic_tag"])

        result = await worker.pop_matching_task("cpu")
        assert result is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 6


class TestStoragePullFailure:
    @pytest.mark.asyncio
    async def test_pull_failure_releases_slot_and_publishes_failed(self, worker, redis, db, tmp_jobs_dir):
        """When storage.pull raises, slot released + step_failed published + DB updated."""
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "general", "style_tags": "[]"})
        await redis.set_step_status("j_test_001", "A", "ready")
        await redis.try_acquire_slot("cpu", limit=3)

        async def failing_pull(job_id, step):
            raise IOError("disk full")

        worker.storage.pull = failing_pull
        task = {"job_id": "j_test_001", "step": "A", "pool": "cpu"}

        failed_events = []

        async def capture_failed():
            async for msg in redis.subscribe("step_failed"):
                failed_events.append(msg)
                break

        listener = asyncio.create_task(capture_failed())
        await asyncio.sleep(0.05)
        await worker.execute(task)
        await asyncio.wait_for(listener, timeout=2.0)

        assert await redis.get_pool_count("cpu") == 0
        assert len(failed_events) == 1
        assert "disk full" in failed_events[0]["error"]
        db_step = db.get_steps("j_test_001")[0]
        assert db_step.status == StepStatus.FAILED


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_stops_main_loop(self, worker, redis):
        """shutdown() sets _shutdown flag, main_loop should exit."""
        await worker.register()
        worker.idle_timeout = 999  # Don't exit from idle

        async def schedule_shutdown():
            await asyncio.sleep(0.1)
            worker.shutdown()

        asyncio.create_task(schedule_shutdown())
        await asyncio.wait_for(worker.main_loop(), timeout=2.0)
        # If we reach here, main_loop exited due to shutdown


class TestRunStep:
    @pytest.mark.asyncio
    async def test_run_step_writes_config_and_collects_output(self, worker, redis, db, tmp_jobs_dir, config):
        """_run_step should write step_config.json and collect subprocess output."""
        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        step_cfg = {
            "step": {
                "name": "A",
                "pool": "cpu",
                "timeout_sec": 10,
                "retries": 2,
            },
            "ai": {},
            "domain": {"name": "general"},
            "style_tags": [],
            "paths": {
                "data_dir": str(config.data_dir),
                "prompts_dir": str(config.prompts_dir),
                "config_dir": str(config.config_dir),
            },
            "providers": {},
        }

        # _run_step runs: python3 -m <module> --job-dir <dir> --step-config <config>
        # Create a minimal step module that _run_step can invoke
        mod_dir = tmp_jobs_dir / "_fake_steps"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "noop.py").write_text(
            "import sys\nprint('step_output_ok')\nsys.exit(0)\n"
        )

        # Patch PYTHONPATH so subprocess can find the module
        orig_env = os.environ.copy()
        os.environ["PYTHONPATH"] = str(tmp_jobs_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")

        try:
            returncode, stderr = await worker._run_step(
                job_id="j_test_001",
                step="A",
                work_dir=job_dir,
                exec_id="test_exec_001",
                step_cfg=step_cfg,
                module="_fake_steps.noop",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

        assert returncode == 0
        assert stderr == ""

        # Config file should be cleaned up after _run_step
        config_path = job_dir / ".A.config.json"
        assert not config_path.exists(), "config file should be cleaned up after _run_step"

        # Log file should have been written with stdout
        log_path = job_dir / "logs" / "A.log"
        assert log_path.exists()
        log_content = log_path.read_text()
        assert "step_output_ok" in log_content

    @pytest.mark.asyncio
    async def test_run_step_streams_stdout_and_stderr_merged(
        self, worker, tmp_jobs_dir, config,
    ):
        """日志边跑边落盘:stdout/stderr 合一文件,stderr 带前缀;返回 stderr 尾部。"""
        job_dir = tmp_jobs_dir / "j_stream"
        job_dir.mkdir(exist_ok=True)

        step_cfg = _minimal_step_cfg(config)
        mod_dir = tmp_jobs_dir / "_fake_steps2"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "mixed.py").write_text(
            "import sys\n"
            "print('out_line_1')\n"
            "print('err_line_1', file=sys.stderr)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "sys.exit(0)\n"
        )

        orig_env = os.environ.copy()
        os.environ["PYTHONPATH"] = str(tmp_jobs_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")
        try:
            returncode, stderr_tail = await worker._run_step(
                job_id="j_stream", step="A", work_dir=job_dir,
                exec_id="x", step_cfg=step_cfg, module="_fake_steps2.mixed",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

        assert returncode == 0
        log = (job_dir / "logs" / "A.log").read_text()
        assert "out_line_1" in log
        assert "[stderr] err_line_1" in log
        # 返回的 stderr 尾部不带前缀,供 error_msg 使用
        assert "err_line_1" in stderr_tail
        assert "[stderr]" not in stderr_tail

    @pytest.mark.asyncio
    async def test_run_step_log_visible_before_completion(
        self, worker, tmp_jobs_dir, config,
    ):
        """长步骤运行中,日志文件应在进程结束前就有内容(流式落盘的核心保证)。"""
        job_dir = tmp_jobs_dir / "j_live"
        job_dir.mkdir(exist_ok=True)

        step_cfg = _minimal_step_cfg(config)
        mod_dir = tmp_jobs_dir / "_fake_steps3"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "slow.py").write_text(
            "import sys, time\n"
            "print('early_marker', flush=True)\n"
            "time.sleep(1.5)\n"
            "print('late_marker', flush=True)\n"
            "sys.exit(0)\n"
        )

        orig_env = os.environ.copy()
        os.environ["PYTHONPATH"] = str(tmp_jobs_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")

        log_path = job_dir / "logs" / "A.log"
        early_seen = asyncio.Event()

        async def watch_for_early():
            for _ in range(60):
                if log_path.is_file() and "early_marker" in log_path.read_text():
                    early_seen.set()
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch_for_early())
        try:
            returncode, _ = await worker._run_step(
                job_id="j_live", step="A", work_dir=job_dir,
                exec_id="x", step_cfg=step_cfg, module="_fake_steps3.slow",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)
            watcher.cancel()

        assert returncode == 0
        # early_marker 必须在 late_marker 出现前就已落盘
        assert early_seen.is_set(), "log was not visible mid-run (not streaming)"
        full = log_path.read_text()
        assert "early_marker" in full and "late_marker" in full

    @pytest.mark.asyncio
    async def test_run_step_failure_returns_stderr_tail(
        self, worker, tmp_jobs_dir, config,
    ):
        """失败步骤:returncode 非 0,stderr 尾部返回供 error_msg,日志含 stderr。"""
        job_dir = tmp_jobs_dir / "j_fail"
        job_dir.mkdir(exist_ok=True)

        step_cfg = _minimal_step_cfg(config)
        mod_dir = tmp_jobs_dir / "_fake_steps4"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "boom.py").write_text(
            "import sys\n"
            "print('boom_reason', file=sys.stderr, flush=True)\n"
            "sys.exit(3)\n"
        )

        orig_env = os.environ.copy()
        os.environ["PYTHONPATH"] = str(tmp_jobs_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")
        try:
            returncode, stderr_tail = await worker._run_step(
                job_id="j_fail", step="A", work_dir=job_dir,
                exec_id="x", step_cfg=step_cfg, module="_fake_steps4.boom",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

        assert returncode == 3
        assert "boom_reason" in stderr_tail
        assert "[stderr] boom_reason" in (job_dir / "logs" / "A.log").read_text()

    @pytest.mark.asyncio
    async def test_run_step_timeout_marks_log_and_raises(
        self, worker, tmp_jobs_dir, config,
    ):
        """超时:抛 TimeoutError,日志含 TIMEOUT 标记(超时行为不变 + 日志可见)。"""
        job_dir = tmp_jobs_dir / "j_to"
        job_dir.mkdir(exist_ok=True)

        step_cfg = _minimal_step_cfg(config)
        step_cfg["step"]["timeout_sec"] = 1
        mod_dir = tmp_jobs_dir / "_fake_steps5"
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "hang.py").write_text(
            "import time\n"
            "print('before_hang', flush=True)\n"
            "time.sleep(30)\n"
        )

        orig_env = os.environ.copy()
        os.environ["PYTHONPATH"] = str(tmp_jobs_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")
        try:
            with pytest.raises(asyncio.TimeoutError):
                await worker._run_step(
                    job_id="j_to", step="A", work_dir=job_dir,
                    exec_id="x", step_cfg=step_cfg, module="_fake_steps5.hang",
                )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

        log = (job_dir / "logs" / "A.log").read_text()
        assert "before_hang" in log
        assert "TIMEOUT" in log
