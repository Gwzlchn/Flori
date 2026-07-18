"""tests for shared/runner_ops.py:认领/上报编排(fakeredis + db).

状态机语义只在本文件验证;test_transport.py 只守薄适配的参数转调与错误映射,
避免两套镜像断言让重构时一同错误通过.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import make_fakeredis
from tests.pubsub_helpers import subscription_barrier
from shared import runner_ops
from shared.models import Job, JobPart, Step, StepStatus
from shared.step_scope import execution_step_key, part_scope
from tests.current_schema_db import clone_current_schema_database


# Fixtures


@pytest.fixture
def db(tmp_path, current_schema_db_template):
    d = clone_current_schema_database(
        current_schema_db_template,
        tmp_path / "test.db",
    )
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


# claim_step


class TestClaimStep:
    @pytest.mark.asyncio
    async def test_ai_queue_claim_is_atomic_and_bound_to_holder(self, redis, db):
        await _register_worker(redis, db)
        payload = {
            "kind": "ai", "task_id": "at-runner", "step": "synthesis",
            "request": {}, "tags": [], "require_tags": ["vision"],
        }
        await redis.enqueue_ai_task_once(payload)

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["ai"], {"ai": 1}, {"vision"}, {"private"},
        )

        assert claim["task_id"] == "at-runner" and claim["state"] == "claimed"
        assert claim["claim_id"] == claim["exec_id"]
        assert await redis.r.zcard("queue:ai") == 0
        persisted = await redis.get_ai_task_claim("at-runner")
        assert persisted["worker_id"] == WORKER_ID
        assert persisted["claim_id"] == claim["exec_id"]

    @pytest.mark.asyncio
    async def test_ai_claim_publish_failure_requeues_and_releases_slot(self, redis, db):
        await _register_worker(redis, db)
        payload = {
            "kind": "ai", "task_id": "at-publish-fail", "step": "synthesis",
            "request": {}, "tags": [], "require_tags": ["vision"],
        }
        await redis.enqueue_ai_task_once(payload)
        redis.publish = AsyncMock(side_effect=RuntimeError("publish down"))

        with pytest.raises(RuntimeError, match="publish down"):
            await runner_ops.claim_step(
                redis, db, WORKER_ID, ["ai"], {"ai": 1}, {"vision"}, {"private"},
            )

        assert await redis.r.zcard("queue:ai") == 1
        assert await redis.get_pool_count("ai") == 0
        assert (await redis.get_ai_task_claim("at-publish-fail"))["state"] == "requeued"
        assert (await redis.get_worker_info(WORKER_ID))["status"] == "idle"
        assert db.get_worker(WORKER_ID).status == "online-idle"

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
                         "exec_id": claim["exec_id"], "generation": 1}
        assert claim["exec_id"].startswith(f"{WORKER_ID}:")
        assert await redis.get_step_status("j1", "A") == "running"
        assert await redis.get_pool_count("cpu") == 1
        assert await redis.get_step_worker("j1", "A") == WORKER_ID
        assert await redis.validate_task_lease(
            WORKER_ID, "j1", "A", claim["exec_id"],
        )
        assert await redis.r.ttl(redis._task_lease_key(claim["exec_id"])) > 0

    @pytest.mark.asyncio
    async def test_task_lease_rejects_wrong_quartet_and_rerun(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )
        exec_id = claim["exec_id"]
        assert not await redis.validate_task_lease("other", "j1", "A", exec_id)
        assert not await redis.validate_task_lease(WORKER_ID, "j2", "A", exec_id)
        assert not await redis.validate_task_lease(WORKER_ID, "j1", "B", exec_id)
        assert not await redis.validate_task_lease(WORKER_ID, "j1", "A", "forged")

        await redis.set_step_exec_id("j1", "A", "new-exec")
        await redis.create_task_lease(WORKER_ID, "j1", "A", "new-exec")
        assert not await redis.validate_task_lease(WORKER_ID, "j1", "A", exec_id)
        assert await redis.validate_task_lease(WORKER_ID, "j1", "A", "new-exec")

    @pytest.mark.asyncio
    async def test_part_lease_cannot_complete_same_template_step_in_other_part(
        self, redis, db,
    ):
        await _register_worker(redis, db)
        job = Job(id="j1", content_type="video", pipeline="video")
        db.create_job(job, [
            JobPart(
                "pt_01", job.id, 1,
                source_url="BV1xx411c7mD", meta={"source": "bilibili"},
            ),
            JobPart(
                "pt_02", job.id, 2,
                source_url="https://youtu.be/dQw4w9WgXcQ",
                meta={"source": "youtube"},
            ),
        ])
        p01 = execution_step_key(part_scope("pt_01"), "01_download")
        p02 = execution_step_key(part_scope("pt_02"), "01_download")
        for priority, step in enumerate((p01, p02)):
            await redis.enqueue_step("cpu", "j1", step, [], priority=priority)
            await redis.set_step_status("j1", step, "ready")

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim["step"] == p01
        assert claim["source"] == "bilibili"
        assert await redis.validate_task_lease(
            WORKER_ID, "j1", p01, claim["exec_id"],
        )
        assert not await redis.validate_task_lease(
            WORKER_ID, "j1", p02, claim["exec_id"],
        )
        assert await redis.begin_task_terminal(
            WORKER_ID, "j1", p02, claim["exec_id"], "done",
        ) == 0
        assert await redis.get_step_status("j1", p02) == "ready"

    @pytest.mark.parametrize(
        ("step_name", "carries_identity"),
        [("03_scene", True), ("08_punctuate", True), ("05_dedup", False)],
    )
    @pytest.mark.asyncio
    async def test_nas_part_claim_carries_reference_identity_without_host_path(
        self, redis, db, step_name, carries_identity,
    ):
        await _register_worker(redis, db)
        job = Job(id="j_nas", content_type="video", pipeline="video")
        part = JobPart(
            "pt_nas", job.id, 1,
            source_ref="nas://zg-library/20250914/P01.mkv",
            source_digest="sha256:" + "a" * 64,
            size_bytes=123,
            meta={"source": "nas_source"},
        )
        db.create_job(job, [part])
        step = execution_step_key(part_scope(part.id), step_name)
        await redis.enqueue_step(
            "cpu", job.id, step, [], priority=0,
            require_tags=["source-root:zg-library"],
        )
        await redis.set_step_status(job.id, step, "ready")

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS,
            {"source-root:zg-library"}, set(),
        )

        assert claim["source"] == "nas_source"
        if carries_identity:
            assert claim["source_ref"] == part.source_ref
            assert claim["source_digest"] == part.source_digest
            assert claim["source_size_bytes"] == 123
        else:
            assert "source_ref" not in claim
            assert "source_digest" not in claim
            assert "source_size_bytes" not in claim
        assert "/volume" not in json.dumps(claim)

    @pytest.mark.asyncio
    async def test_terminal_lease_allows_one_outcome_and_release_cleanup(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )
        exec_id = claim["exec_id"]
        assert await redis.begin_task_terminal(WORKER_ID, "j1", "A", exec_id, "done") == 1
        assert await redis.begin_task_terminal(WORKER_ID, "j1", "A", exec_id, "done") == 2
        assert await redis.begin_task_terminal(WORKER_ID, "j1", "A", exec_id, "failed") == 0
        assert not await redis.validate_task_lease(WORKER_ID, "j1", "A", exec_id)
        assert await redis.validate_task_lease(
            WORKER_ID, "j1", "A", exec_id, require_active=False,
        )
        await runner_ops.release_step(redis, db, WORKER_ID, claim)
        assert not await redis.validate_task_lease(
            WORKER_ID, "j1", "A", exec_id, require_active=False,
        )
        assert await redis.validate_released_task_lease(
            WORKER_ID, "j1", "A", exec_id, "cpu",
        )

    @pytest.mark.asyncio
    async def test_claim_refreshes_progress_heartbeat(self, redis, db):
        # 认领即刷 progress_at:覆盖上次执行残留的旧心跳,否则 check_stuck 在认领到首拍窗口
        # 按 now-旧值(小时/天级)误杀刚认领的步(线上 "progress stale 250689s")。
        import time
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "lecture", "style_tags": '["formal"]'})
        await redis.r.hset("job:j1:step_progress", "A", str(time.time() - 250_000))  # 2.9 天前的残留

        await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, {"private"},
        )

        at = await redis.get_step_progress_at("j1", "A")
        assert at is not None and time.time() - at < 5

    @pytest.mark.asyncio
    async def test_claim_does_not_freeze(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is not None and claim["pool"] == "cpu"
        # 认领任何池都不会自动冻结其他池。
        assert await redis.is_pool_frozen("cpu") is False

    @pytest.mark.asyncio
    async def test_pool_limit_override_caps_claim(self, redis, db):
        await _register_worker(redis, db)
        await redis.set_pool_limit_override("cpu", 1)  # 运行时覆盖到 1:只能领 1 个
        for s in ("A", "B"):
            await redis.enqueue_step("cpu", "j1", s, [], priority=0)
            await redis.set_step_status("j1", s, "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        c1 = await runner_ops.claim_step(redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, set(), set())
        assert c1 is not None
        c2 = await runner_ops.claim_step(redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, set(), set())
        assert c2 is None  # 覆盖上限=1 时第二个领不到(即便 POOL_LIMITS cpu=3)

    @pytest.mark.asyncio
    async def test_paused_returns_none(self, redis, db):
        await _register_worker(redis, db)
        await redis.set_worker_field(WORKER_ID, "admin_status", "paused")
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )
        assert claim is None

    @pytest.mark.asyncio
    async def test_pause_survives_runtime_status_write(self, redis, db):
        """暂停态 admin_status 与运行时 status 解耦:claim/release/gateway 心跳写 status(busy/idle)不得清掉暂停,本测试钉死该不变量。"""
        await _register_worker(redis, db)
        await redis.set_worker_field(WORKER_ID, "admin_status", "paused")
        # 模拟在跑 worker 释放任务(等价 release_step 的 _set_status idle)
        await redis.set_worker_field(WORKER_ID, "status", "idle")
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )
        assert claim is None  # 仍暂停,运行时 status 写入未清掉 admin_status

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
    async def test_reject_tag_returns_to_queue_and_releases_slot(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step(
            "cpu", "j1", "A", ["vision", "private"], priority=0,
            require_tags=["vision"],
        )

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, {"private"},
        )

        assert claim is None
        assert (await redis.get_queue_info("cpu"))["length"] == 1
        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_max_tries_returns_all_mismatches_to_queue(self, redis, db):
        await _register_worker(redis, db)
        for index in range(6):
            await redis.enqueue_step(
                "cpu", f"j_{index}", "A", ["exotic"], priority=0,
                require_tags=["exotic"],
            )

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is None
        assert (await redis.get_queue_info("cpu"))["length"] == 6
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
    async def test_atomic_claim_has_no_python_cas_crash_window(self, redis, db):
        await _register_worker(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")

        # 普通 claim 不再经过 Python CAS;旧崩溃注入点不能吞掉已出队任务。
        with patch.object(redis, "cas_step_status", side_effect=RuntimeError("boom")):
            claim = await runner_ops.claim_step(
                redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
            )

        assert claim is not None
        assert (await redis.get_queue_info("cpu"))["length"] == 0
        assert await redis.get_pool_count("cpu") == 1
        assert await redis.get_step_status("j1", "A") == "running"

    @pytest.mark.asyncio
    async def test_incompatible_prefix_longer_than_five_does_not_starve(self, redis, db):
        await _register_worker(redis, db)
        for index in range(7):
            job_id = f"blocked-{index}"
            await redis.enqueue_step(
                "cpu", job_id, "A", [], priority=index, require_tags=["gpu"],
            )
            await redis.set_step_status(job_id, "A", "ready")
        await redis.enqueue_step("cpu", "compatible", "A", [], priority=8)
        await redis.set_step_status("compatible", "A", "ready")

        claim = await runner_ops.claim_step(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, set(),
        )

        assert claim is not None and claim["job_id"] == "compatible"
        assert (await redis.get_queue_info("cpu"))["length"] == 7

    @pytest.mark.asyncio
    async def test_job_terminal_fence_is_generation_bound(self, redis):
        await redis.init_job("j1", "test", {})
        generation = await redis.get_job_generation("j1")
        winners = await asyncio.gather(
            redis.try_finalize_job("j1", generation, "done"),
            redis.try_finalize_job("j1", generation, "failed"),
        )
        assert sorted(winners) == [0, 1]
        new_generation = await redis.advance_job_generation("j1")
        assert await redis.try_finalize_job("j1", generation, "done") == 0
        assert await redis.try_finalize_job("j1", new_generation, "failed") == 1

    @pytest.mark.asyncio
    async def test_job_finalizer_single_owner_and_expired_takeover(self, redis):
        await redis.init_job("j1", "test", {})
        owners = await asyncio.gather(
            redis.acquire_job_finalizer("j1", 1, "done", "owner-a", now=100, lease_sec=10),
            redis.acquire_job_finalizer("j1", 1, "done", "owner-b", now=100, lease_sec=10),
        )
        assert sorted(owners) == [0, 1]
        assert await redis.acquire_job_finalizer(
            "j1", 1, "done", "recovery", now=111, lease_sec=10,
        ) == 1
        assert await redis.complete_job_finalizer("j1", 1, "done", "recovery")
        assert await redis.acquire_job_finalizer(
            "j1", 1, "done", "late", now=200, lease_sec=10,
        ) == 2


# report_step_done


class TestReportDone:
    @pytest.mark.asyncio
    async def test_appends_current_terminal_without_writing_db(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        await redis.init_job("j1", "test", {})
        await redis.set_step_status("j1", "A", "running")
        await redis.set_step_exec_id("j1", "A", f"{WORKER_ID}:1")
        await redis.r.hset("job:j1:step_generation", "A", "1")
        claim = {
            "job_id": "j1", "step": "A", "pool": "cpu",
            "exec_id": f"{WORKER_ID}:1", "generation": 1,
        }

        accepted = await runner_ops.report_step_done(
            redis, db, WORKER_ID, claim, 12.34, time.time() - 12.34,
        )
        duplicate = await runner_ops.report_step_done(
            redis, db, WORKER_ID, claim, 12.34, time.time() - 12.34,
        )

        assert accepted is True and duplicate is False
        entries = await redis.r.xrange(redis.LIFECYCLE_STREAM)
        payload = json.loads(entries[0][1]["payload"])
        assert payload["duration"] == 12.3
        assert payload["exec_id"] == f"{WORKER_ID}:1"
        assert db.get_steps("j1")[0].status == StepStatus.RUNNING
        assert db.get_worker(WORKER_ID).tasks_completed == 0


# report_step_failed


class TestReportFailed:
    @pytest.mark.asyncio
    async def test_count_stats_flag_is_durable_but_not_applied_by_runner(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        await redis.init_job("j1", "test", {})
        await redis.set_step_status("j1", "A", "running")
        await redis.set_step_exec_id("j1", "A", f"{WORKER_ID}:9")
        await redis.r.hset("job:j1:step_generation", "A", "1")
        claim = {
            "job_id": "j1", "step": "A", "pool": "cpu",
            "exec_id": f"{WORKER_ID}:9", "generation": 1,
        }

        accepted = await runner_ops.report_step_failed(
            redis, db, WORKER_ID, claim, "x" * 600, "segfault", 5.0,
            time.time() - 5.0, count_stats=True,
        )

        assert accepted is True
        entries = await redis.r.xrange(redis.LIFECYCLE_STREAM)
        payload = json.loads(entries[0][1]["payload"])
        assert payload["count_stats"] is True
        assert payload["exec_id"] == f"{WORKER_ID}:9"
        assert db.get_steps("j1")[0].status == StepStatus.RUNNING
        assert db.get_worker(WORKER_ID).tasks_failed == 0

    @pytest.mark.asyncio
    async def test_old_exec_is_rejected_without_stream_or_db_write(self, redis, db):
        await _register_worker(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))
        await redis.init_job("j1", "test", {})
        await redis.set_step_status("j1", "A", "running")
        await redis.set_step_exec_id("j1", "A", "new-exec")
        await redis.r.hset("job:j1:step_generation", "A", "1")
        claim = {
            "job_id": "j1", "step": "A", "pool": "cpu",
            "exec_id": "old-exec", "generation": 1,
        }

        accepted = await runner_ops.report_step_failed(
            redis, db, WORKER_ID, claim, "timeout", "timeout", 3.0,
            time.time() - 3.0, count_stats=False,
        )

        assert accepted is False
        assert await redis.r.xlen(redis.LIFECYCLE_STREAM) == 0
        assert db.get_steps("j1")[0].status == StepStatus.RUNNING
        assert db.get_worker(WORKER_ID).tasks_failed == 0


# release_step


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_slot_and_idle(self, redis, db):
        await _register_worker(redis, db)
        await redis.try_acquire_slot("cpu", 1, "e")   # holder = 下方 release 的 exec_id

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"},
        )

        assert await redis.get_pool_count("cpu") == 0
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"

    @pytest.mark.asyncio
    async def test_release_non_scene_does_not_unfreeze(self, redis, db):
        await _register_worker(redis, db)
        await redis.try_acquire_slot("cpu", 3, "e")   # holder = 下方 release 的 exec_id
        await redis.freeze_pool("cpu")

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"},
        )

        assert await redis.get_pool_count("cpu") == 0
        assert await redis.is_pool_frozen("cpu") is True

    @pytest.mark.asyncio
    async def test_release_skips_when_exec_id_superseded(self, redis, db):
        # check_stuck 重排后旧 worker 迟到的 release 不得释放/解冻已被新执行接管的槽与冻结。
        await _register_worker(redis, db)
        await redis.try_acquire_slot("scene", 1, "e_new")  # 槽属存活的新执行 e_new
        await redis.freeze_pool("cpu")
        await redis.set_step_exec_id("j1", "A", "e_new")  # 新执行已接管该步

        await runner_ops.release_step(
            redis, db, WORKER_ID,
            {"job_id": "j1", "step": "A", "pool": "scene", "exec_id": "e_old"},
        )

        # 陈旧 worker 只会 SREM 自己的 holder(e_old,本不在集合),新执行 e_new 的槽未被误放.
        assert await redis.get_pool_count("scene") == 1   # 槽未被误放
        assert await redis.is_pool_frozen("cpu") is True   # cpu 未被误解冻
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"                    # 旧 worker 仍回 idle
