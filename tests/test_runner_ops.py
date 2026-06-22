"""tests for shared/runner_ops.py — 认领/上报编排(fakeredis + db)。

这套测试针对抽出来的纯函数,与 test_transport.py 的 RedisTransport 用例互为镜像;
两者都过,才能保证薄包装与服务端端点共用同一份编排、行为不分叉。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from tests.conftest import make_fakeredis
from shared import runner_ops
from shared.db import Database
from shared.models import Job, Step, StepStatus


# ── Fixtures ──


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


WORKER_ID = "w_t1"
POOL_LIMITS = {"cpu": 3, "io": 999, "scene": 1}


async def _register_worker(redis, db, worker_id=WORKER_ID):
    from datetime import datetime, timezone
    from shared.models import Worker as WorkerModel

    now = datetime.now(timezone.utc)
    info = {"type": "cpu", "pools": "cpu,io", "tags": "vision",
            "reject_tags": "private", "hostname": "h", "status": "idle",
            "started_at": now.isoformat(), "last_heartbeat": now.isoformat()}
    await redis.register_worker(worker_id, info, ttl=30)
    db.upsert_worker(WorkerModel(
        id=worker_id, type="cpu", pools=["cpu", "io"],
        tags={"vision"}, reject_tags={"private"}, hostname="h",
        status="idle", started_at=now, first_seen=now, last_heartbeat=now,
    ))


# ── claim_step ──


class TestClaimStep:
    @pytest.mark.asyncio
    async def test_claims_ready_step_with_cas_and_exec_id(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "lecture", "style_tags": '["formal"]'})

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, {"private"},
        )

        assert claim == {"job_id": "j1", "step": "A", "pool": "cpu",
                         "exec_id": claim["exec_id"]}
        assert claim["exec_id"].startswith(f"{WORKER_ID}:")
        assert await redis.get_step_status("j1", "A") == "running"
        assert await redis.get_pool_count("cpu") == 1
        assert await redis.get_step_worker("j1", "A") == WORKER_ID

    @pytest.mark.asyncio
    async def test_scene_freezes_cpu(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["scene"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is not None and claim["pool"] == "scene"
        assert await redis.is_pool_frozen("cpu") is True

    @pytest.mark.asyncio
    async def test_draining_returns_none(self, redis, db):
        await _register_worker(redis, db)
        await redis.set_worker_field(WORKER_ID, "status", "draining")
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )
        assert claim is None

    @pytest.mark.asyncio
    async def test_tag_mismatch_returns_to_queue_and_releases_slot(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", ["heavy"], priority=0,
                                 require_tags=["heavy"])

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is None
        assert (await redis.get_queue_info("cpu"))["length"] == 1
        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_cas_lost_releases_slot_and_unfreezes(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "running")  # CAS ready->running 必失败
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["scene"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is None
        assert await redis.get_pool_count("scene") == 0
        assert await redis.is_pool_frozen("cpu") is False

    @pytest.mark.asyncio
    async def test_pop_then_crash_returns_raw_to_queue(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")

        # dequeue 成功后 CAS 抛错 → raw 必须回队列,槽位释放,异常透传。
        with patch.object(redis, "cas_step_status", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await runner_ops.claim_step(
                    redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
                )

        assert (await redis.get_queue_info("cpu"))["length"] == 1
        assert await redis.get_pool_count("cpu") == 0


# ── report_step_done ──


class TestReportDone:
    @pytest.mark.asyncio
    async def test_publishes_writes_and_increments(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": f"{WORKER_ID}:1"}

        events = []

        async def capture():
            async for msg in redis.subscribe("step_completed"):
                events.append(msg)
                break

        listener = asyncio.create_task(capture())
        await asyncio.sleep(0.05)
        await runner_ops.report_step_done(redis, db, WORKER_ID, claim, 12.34, time.time() - 12.34)
        await asyncio.wait_for(listener, timeout=2.0)

        assert events[0]["status"] == "done"
        assert events[0]["duration"] == 12.3
        assert events[0]["exec_id"] == f"{WORKER_ID}:1"
        assert events[0]["worker"] == WORKER_ID
        assert db.get_steps("j1")[0].status == StepStatus.DONE
        assert db.get_worker(WORKER_ID).tasks_completed == 1


# ── report_step_failed ──


class TestReportFailed:
    @pytest.mark.asyncio
    async def test_count_stats_true_includes_exec_id_and_increments(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": f"{WORKER_ID}:9"}

        topic_events, ws_events = [], []

        async def capture_topic():
            async for msg in redis.subscribe("step_failed"):
                topic_events.append(msg)
                break

        async def capture_ws():
            async for msg in redis.subscribe("events:j1"):
                if msg.get("event") == "step_failed":
                    ws_events.append(msg)
                    break

        l1 = asyncio.create_task(capture_topic())
        l2 = asyncio.create_task(capture_ws())
        await asyncio.sleep(0.05)
        await runner_ops.report_step_failed(
            redis, db, WORKER_ID, claim, "x" * 600, "segfault", 5.0,
            time.time() - 5.0, count_stats=True,
        )
        await asyncio.wait_for(l1, timeout=2.0)
        await asyncio.wait_for(l2, timeout=2.0)

        assert topic_events[0]["exec_id"] == f"{WORKER_ID}:9"
        assert len(ws_events[0]["error"]) == 200
        assert db.get_steps("j1")[0].status == StepStatus.FAILED
        assert db.get_worker(WORKER_ID).tasks_failed == 1

    @pytest.mark.asyncio
    async def test_count_stats_false_skips_increment_and_omits_exec_id(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": f"{WORKER_ID}:9"}

        topic_events, ws_events = [], []

        async def capture_topic():
            async for msg in redis.subscribe("step_failed"):
                topic_events.append(msg)
                break

        async def capture_ws():
            async for msg in redis.subscribe("events:j1"):
                if msg.get("event") == "step_failed":
                    ws_events.append(msg)
                    break

        l1 = asyncio.create_task(capture_topic())
        l2 = asyncio.create_task(capture_ws())
        await asyncio.sleep(0.05)
        await runner_ops.report_step_failed(
            redis, db, WORKER_ID, claim, "timeout", "timeout", 3.0,
            time.time() - 3.0, count_stats=False,
        )
        await asyncio.wait_for(l1, timeout=2.0)
        await asyncio.wait_for(l2, timeout=2.0)

        assert "exec_id" not in topic_events[0]
        assert ws_events[0]["error"] == "timeout"
        assert db.get_steps("j1")[0].status == StepStatus.FAILED
        assert db.get_worker(WORKER_ID).tasks_failed == 0


# ── release_step ──


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_slot_and_unfreeze_scene(self, redis, db):
        await _register_worker(redis, db)
        await redis.try_acquire_slot("scene", limit=1)
        await redis.freeze_pool("cpu")

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "scene", "exec_id": "e"},
        )

        assert await redis.get_pool_count("scene") == 0
        assert await redis.is_pool_frozen("cpu") is False
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"

    @pytest.mark.asyncio
    async def test_release_non_scene_does_not_unfreeze(self, redis, db):
        await _register_worker(redis, db)
        await redis.try_acquire_slot("cpu", limit=3)
        await redis.freeze_pool("cpu")

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"},
        )

        assert await redis.get_pool_count("cpu") == 0
        assert await redis.is_pool_frozen("cpu") is True

    @pytest.mark.asyncio
    async def test_release_skips_when_exec_id_superseded(self, redis, db):
        # check_stuck 重排后旧 worker 迟到的 release 不得释放/解冻已被新执行接管的槽与冻结(审计 SCHED-N1)。
        await _register_worker(redis, db)
        await redis.try_acquire_slot("scene", limit=1)
        await redis.freeze_pool("cpu")
        await redis.set_step_exec_id("j1", "A", "e_new")  # 新执行已接管该步

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "scene", "exec_id": "e_old"},
        )

        assert await redis.get_pool_count("scene") == 1   # 槽未被误放
        assert await redis.is_pool_frozen("cpu") is True   # cpu 未被误解冻
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"                    # 旧 worker 仍回 idle
