"""tests for shared/redis_client.py — 使用 fakeredis。"""

import asyncio

import pytest

from tests.conftest import make_fakeredis


@pytest.fixture
async def rc():
    """用 fakeredis 替代真实 Redis。"""
    client = make_fakeredis()
    yield client
    await client.close()


class TestQueue:
    @pytest.mark.asyncio
    async def test_enqueue_dequeue_priority(self, rc):
        await rc.enqueue_step("cpu", "j_a", "06_ocr", ["cpu"], priority=-5)
        await rc.enqueue_step("cpu", "j_b", "06_ocr", ["cpu"], priority=-2)
        await rc.enqueue_step("cpu", "j_c", "06_ocr", ["cpu"], priority=-8)

        item1, score1 = await rc.dequeue_step("cpu")
        assert item1["job_id"] == "j_c"
        assert score1 == -8

        item2, score2 = await rc.dequeue_step("cpu")
        assert item2["job_id"] == "j_a"

    @pytest.mark.asyncio
    async def test_dequeue_empty(self, rc):
        assert await rc.dequeue_step("cpu") is None

    @pytest.mark.asyncio
    async def test_return_step_preserves_score(self, rc):
        await rc.enqueue_step("ai", "j_x", "10_smart", ["vision"], priority=-3)
        raw_json, task, score = await rc.dequeue_step_raw("ai")
        await rc.return_step("ai", raw_json, score)

        item2, score2 = await rc.dequeue_step("ai")
        assert item2["job_id"] == "j_x"
        assert score2 == -3

    @pytest.mark.asyncio
    async def test_enqueue_with_require_tags(self, rc):
        """require_tags should be stored separately from tags."""
        await rc.enqueue_step("ai", "j_x", "10_smart",
                              tags=["vision", "nlp", "case-study"],
                              priority=0,
                              require_tags=["vision"])
        raw_json, task, score = await rc.dequeue_step_raw("ai")
        assert set(task["tags"]) == {"vision", "nlp", "case-study"}
        assert task["require_tags"] == ["vision"]

    @pytest.mark.asyncio
    async def test_enqueue_without_require_tags_defaults_empty(self, rc):
        """require_tags defaults to [] when not provided."""
        await rc.enqueue_step("ai", "j_x", "A", ["gpu"], priority=0)
        raw_json, task, score = await rc.dequeue_step_raw("ai")
        assert task["require_tags"] == []

    @pytest.mark.asyncio
    async def test_queue_info(self, rc):
        await rc.enqueue_step("io", "j_1", "01_download", [], priority=0)
        await rc.enqueue_step("io", "j_2", "01_download", [], priority=0)
        info = await rc.get_queue_info("io")
        assert info["length"] == 2


    @pytest.mark.asyncio
    async def test_dequeue_step_raw_returns_triple(self, rc):
        await rc.enqueue_step("cpu", "j_a", "03_scene", ["gpu"], priority=-3)
        result = await rc.dequeue_step_raw("cpu")
        assert result is not None
        raw_json, task, score = result
        assert isinstance(raw_json, str)
        assert task["job_id"] == "j_a"
        assert task["step"] == "03_scene"
        assert score == -3

    @pytest.mark.asyncio
    async def test_dequeue_step_raw_empty(self, rc):
        assert await rc.dequeue_step_raw("cpu") is None

    @pytest.mark.asyncio
    async def test_dequeue_step_raw_return_roundtrip(self, rc):
        await rc.enqueue_step("ai", "j_x", "10_smart", ["vision"], priority=-5)
        raw_json, task, score = await rc.dequeue_step_raw("ai")
        await rc.return_step("ai", raw_json, score)
        item, score2 = await rc.dequeue_step("ai")
        assert item["job_id"] == "j_x"
        assert score2 == -5


class TestPool:
    @pytest.mark.asyncio
    async def test_acquire_and_release(self, rc):
        assert await rc.try_acquire_slot("cpu", limit=2) is True
        assert await rc.get_pool_count("cpu") == 1
        assert await rc.try_acquire_slot("cpu", limit=2) is True
        assert await rc.get_pool_count("cpu") == 2
        assert await rc.try_acquire_slot("cpu", limit=2) is False

        await rc.release_slot("cpu")
        assert await rc.get_pool_count("cpu") == 1
        assert await rc.try_acquire_slot("cpu", limit=2) is True

    @pytest.mark.asyncio
    async def test_frozen_blocks_acquire(self, rc):
        await rc.freeze_pool("cpu")
        assert await rc.is_pool_frozen("cpu") is True
        assert await rc.try_acquire_slot("cpu", limit=10) is False

        await rc.unfreeze_pool("cpu")
        assert await rc.is_pool_frozen("cpu") is False
        assert await rc.try_acquire_slot("cpu", limit=10) is True

    @pytest.mark.asyncio
    async def test_acquire_over_limit(self, rc):
        ok1 = await rc.try_acquire_slot("test_pool", limit=1)
        ok2 = await rc.try_acquire_slot("test_pool", limit=1)
        assert ok1 is True
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_acquire_atomicity(self, rc):
        results = await asyncio.gather(
            rc.try_acquire_slot("scene", limit=1),
            rc.try_acquire_slot("scene", limit=1),
            rc.try_acquire_slot("scene", limit=1),
        )
        assert sum(results) == 1

    @pytest.mark.asyncio
    async def test_release_at_zero_returns_false(self, rc):
        """release_slot when count is already 0 should return False and not go negative."""
        result = await rc.release_slot("empty_pool")
        assert result is False
        assert await rc.get_pool_count("empty_pool") == 0


class TestCAS:
    @pytest.mark.asyncio
    async def test_cas_prevents_double_transition(self, rc):
        await rc.set_step_status("j_x", "A", "ready")
        acquired1 = await rc.cas_step_status("j_x", "A", "ready", "running")
        acquired2 = await rc.cas_step_status("j_x", "A", "ready", "running")
        assert acquired1 is True
        assert acquired2 is False
        assert await rc.get_step_status("j_x", "A") == "running"


class TestDeleteStepStatus:
    @pytest.mark.asyncio
    async def test_delete_step_status(self, rc):
        await rc.set_step_status("j_x", "A", "done")
        await rc.delete_step_status("j_x", "A")
        assert await rc.get_step_status("j_x", "A") is None

    @pytest.mark.asyncio
    async def test_delete_step_status_clears_all_per_step_fields(self, rc):
        # 对齐 cleanup_job:清该步在所有 per-step hash 的 field,不留惰性垃圾(审计 I-L11)。
        jid = "j_x"
        await rc.set_step_status(jid, "A", "done")
        for sub in ("retries", "step_worker", "step_exec", "step_resources", "step_progress"):
            await rc.r.hset(f"job:{jid}:{sub}", "A", "v")
        await rc.delete_step_status(jid, "A")
        for sub in ("steps", "retries", "step_worker", "step_exec", "step_resources", "step_progress"):
            assert await rc.r.hget(f"job:{jid}:{sub}", "A") is None

    @pytest.mark.asyncio
    async def test_reset_step_retries(self, rc):
        # rerun 前清重试计数(审计 I-H4):reset 后归零,重跑步骤恢复重试预算。
        await rc.incr_step_retries("j_x", "A")
        await rc.incr_step_retries("j_x", "A")
        assert await rc.get_step_retries("j_x", "A") == 2
        await rc.reset_step_retries("j_x", "A")
        assert await rc.get_step_retries("j_x", "A") == 0


class TestJobState:
    @pytest.mark.asyncio
    async def test_init_and_get(self, rc):
        await rc.init_job("j_x", "video", {"domain": "deep-learning", "style_tags": ["case"]})
        assert await rc.get_job_pipeline("j_x") == "video"
        info = await rc.get_job_info("j_x")
        assert info["domain"] == "deep-learning"

    @pytest.mark.asyncio
    async def test_step_status(self, rc):
        await rc.set_step_status("j_x", "03_scene", "waiting")
        assert await rc.get_step_status("j_x", "03_scene") == "waiting"

        await rc.set_step_status("j_x", "03_scene", "running")
        statuses = await rc.get_all_step_statuses("j_x")
        assert statuses["03_scene"] == "running"

    @pytest.mark.asyncio
    async def test_cas_success(self, rc):
        await rc.set_step_status("j_x", "03_scene", "ready")
        assert await rc.cas_step_status("j_x", "03_scene", "ready", "running") is True
        assert await rc.get_step_status("j_x", "03_scene") == "running"

    @pytest.mark.asyncio
    async def test_cas_failure(self, rc):
        await rc.set_step_status("j_x", "03_scene", "done")
        assert await rc.cas_step_status("j_x", "03_scene", "ready", "running") is False
        assert await rc.get_step_status("j_x", "03_scene") == "done"

    @pytest.mark.asyncio
    async def test_step_worker(self, rc):
        await rc.set_step_worker("j_x", "03_scene", "cpu-abc")
        assert await rc.get_step_worker("j_x", "03_scene") == "cpu-abc"

    @pytest.mark.asyncio
    async def test_retries(self, rc):
        assert await rc.get_step_retries("j_x", "10_smart") == 0
        assert await rc.incr_step_retries("j_x", "10_smart") == 1
        assert await rc.incr_step_retries("j_x", "10_smart") == 2
        assert await rc.get_step_retries("j_x", "10_smart") == 2

    @pytest.mark.asyncio
    async def test_cleanup(self, rc):
        await rc.init_job("j_x", "video", {})
        await rc.set_step_status("j_x", "03_scene", "done")
        await rc.set_step_worker("j_x", "03_scene", "cpu-1")
        await rc.incr_step_retries("j_x", "03_scene")

        await rc.cleanup_job("j_x")
        assert await rc.get_job_pipeline("j_x") is None
        assert await rc.get_all_step_statuses("j_x") == {}


class TestWorker:
    @pytest.mark.asyncio
    async def test_register_and_get(self, rc):
        await rc.register_worker("cpu-1", {"type": "cpu", "status": "idle"}, ttl=30)
        info = await rc.get_worker_info("cpu-1")
        assert info["type"] == "cpu"
        assert await rc.worker_exists("cpu-1") is True

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_heartbeat(self, rc):
        await rc.register_worker("cpu-1", {"type": "cpu", "status": "idle"}, ttl=30)
        await rc.heartbeat("cpu-1", ttl=30)
        info = await rc.get_worker_info("cpu-1")
        assert "last_heartbeat" in info

    @pytest.mark.asyncio
    async def test_nonexistent_worker(self, rc):
        assert await rc.worker_exists("nope") is False
        assert await rc.get_worker_info("nope") is None

    @pytest.mark.asyncio
    async def test_set_field(self, rc):
        await rc.register_worker("cpu-1", {"type": "cpu", "status": "idle"})
        await rc.set_worker_field("cpu-1", "status", "busy")
        info = await rc.get_worker_info("cpu-1")
        assert info["status"] == "busy"

    @pytest.mark.asyncio
    async def test_list_workers(self, rc):
        await rc.register_worker("cpu-1", {"type": "cpu"})
        await rc.register_worker("ai-1", {"type": "ai"})
        ids = await rc.list_worker_ids()
        assert set(ids) == {"cpu-1", "ai-1"}

    @pytest.mark.asyncio
    async def test_registration_token_not_listed_as_worker(self, rc):
        # 接入 token 不该污染 worker 列表(否则 hgetall 一个 string 键会 WRONGTYPE → /api/workers 500)。
        await rc.register_worker("cpu-1", {"type": "cpu"})
        await rc.set_registration_token("mnw-secret", ttl_sec=3600)
        ids = await rc.list_worker_ids()
        assert set(ids) == {"cpu-1"}
        assert await rc.get_registration_token() == "mnw-secret"

    @pytest.mark.asyncio
    async def test_legacy_worker_registration_token_key_skipped(self, rc):
        # 兼容历史:旧版把 token 写在 worker:registration_token(string)。扫描须跳过,不得 WRONGTYPE。
        await rc.register_worker("cpu-1", {"type": "cpu"})
        await rc._redis.set("worker:registration_token", "mnw-legacy")
        ids = await rc.list_worker_ids()
        assert set(ids) == {"cpu-1"}
        for wid in ids:
            assert await rc.get_worker_info(wid) is not None


class TestActiveJobs:
    @pytest.mark.asyncio
    async def test_add_and_get(self, rc):
        await rc.add_active_job("j_a")
        await rc.add_active_job("j_b")
        jobs = await rc.get_active_jobs()
        assert jobs == {"j_a", "j_b"}

    @pytest.mark.asyncio
    async def test_remove(self, rc):
        await rc.add_active_job("j_a")
        await rc.add_active_job("j_b")
        await rc.remove_active_job("j_a")
        jobs = await rc.get_active_jobs()
        assert jobs == {"j_b"}

    @pytest.mark.asyncio
    async def test_empty(self, rc):
        jobs = await rc.get_active_jobs()
        assert jobs == set()

    @pytest.mark.asyncio
    async def test_add_idempotent(self, rc):
        await rc.add_active_job("j_a")
        await rc.add_active_job("j_a")
        jobs = await rc.get_active_jobs()
        assert jobs == {"j_a"}


class TestPubSub:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self, rc):
        received = []

        async def listener():
            async for msg in rc.subscribe("test_channel"):
                received.append(msg)
                break

        task = asyncio.create_task(listener())
        await asyncio.sleep(0.05)
        await rc.publish("test_channel", {"event": "step_done", "step": "03_scene"})
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1
        assert received[0]["event"] == "step_done"

    @pytest.mark.asyncio
    async def test_subscribe_survives_connection_error(self, rc, monkeypatch):
        """连接级异常（TimeoutError）不应让 subscribe 抛出，而是重连重订阅后继续。"""
        from redis.exceptions import TimeoutError as RedisTimeoutError

        received = []
        calls = {"pubsub": 0, "getmsg": 0}
        real_pubsub = rc.r.pubsub

        def flaky_pubsub(*a, **kw):
            calls["pubsub"] += 1
            ps = real_pubsub(*a, **kw)
            real_get = ps.get_message

            async def flaky_get_message(*ga, **gkw):
                calls["getmsg"] += 1
                # 首次 get_message 抛连接级异常，触发重连重订阅。
                if calls["getmsg"] == 1:
                    raise RedisTimeoutError("simulated")
                return await real_get(*ga, **gkw)

            ps.get_message = flaky_get_message  # type: ignore[assignment]
            return ps

        # reconnect 不真正换连接（fakeredis），只让 pubsub 重新创建走 real 路径。
        async def fake_reconnect():
            return None

        monkeypatch.setattr(rc, "reconnect", fake_reconnect)
        monkeypatch.setattr(rc.r, "pubsub", flaky_pubsub)

        async def listener():
            async for msg in rc.subscribe("ch2"):
                received.append(msg)
                break

        task = asyncio.create_task(listener())
        # 轮询等"重订阅真正发生"(pubsub 重建,calls>=2),而非赌固定 1.4s——
        # 后者把生产退避常量(1s)硬编码进测试,慢机器上 1.4s 不够会偶发 flaky。
        for _ in range(80):                 # 上限 4s,够 1s 退避 + 调度余量
            if calls["pubsub"] >= 2:
                break
            await asyncio.sleep(0.05)
        await rc.publish("ch2", {"event": "ok"})
        await asyncio.wait_for(task, timeout=4.0)

        assert calls["pubsub"] >= 2  # 至少重订阅过一次
        assert received and received[0]["event"] == "ok"
