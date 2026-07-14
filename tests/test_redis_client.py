"""tests for shared/redis_client.py — 使用 fakeredis。"""
import json

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


class TestListQueue:
    @pytest.mark.asyncio
    async def test_list_queue_orders_by_priority_and_joins_enqueued_at(self, rc):
        await rc.enqueue_step("cpu", "j_a", "06_ocr", ["cpu"], priority=5)
        await rc.enqueue_step("cpu", "j_b", "06_ocr", ["cpu"], priority=-2)
        items = await rc.list_queue("cpu")
        # 只读窥视:不弹出,队列仍在
        assert (await rc.get_queue_info("cpu"))["length"] == 2
        # 按 score 升序(越小越先)
        assert [it["job_id"] for it in items] == ["j_b", "j_a"]
        # enqueue 顺带写入 queue:enqueued → list_queue join 出 enqueued_at(秒)
        assert all(isinstance(it["enqueued_at"], float) for it in items)
        assert items[0]["priority"] == -2
        assert items[0]["step"] == "06_ocr"

    @pytest.mark.asyncio
    async def test_list_queue_empty(self, rc):
        assert await rc.list_queue("io") == []

    @pytest.mark.asyncio
    async def test_list_queue_limit_truncates(self, rc):
        for i in range(5):
            await rc.enqueue_step("io", f"j_{i}", "01_download", [], priority=i)
        items = await rc.list_queue("io", limit=3)
        assert len(items) == 3
        assert (await rc.get_queue_info("io"))["length"] == 5  # 总数不变,仅列出截断

    @pytest.mark.asyncio
    async def test_dequeue_clears_enqueued_at(self, rc):
        await rc.enqueue_step("cpu", "j_a", "06_ocr", ["cpu"], priority=0)
        field = "cpu|j_a|06_ocr"
        assert await rc.r.hexists("queue:enqueued", field)
        await rc.dequeue_step_raw("cpu")
        assert not await rc.r.hexists("queue:enqueued", field)

    @pytest.mark.asyncio
    async def test_return_step_resets_enqueued_at(self, rc):
        await rc.enqueue_step("ai", "j_x", "10_smart", ["vision"], priority=-5)
        raw_json, _, score = await rc.dequeue_step_raw("ai")
        await rc.return_step("ai", raw_json, score)
        items = await rc.list_queue("ai")
        assert len(items) == 1
        assert isinstance(items[0]["enqueued_at"], float)


class TestPool:
    """并发槽是 holder 集合,holder 取 exec_id:used=SCARD,占=SADD,放=SREM 幂等,避免裸计数器的幽灵泄漏。"""

    @pytest.mark.asyncio
    async def test_acquire_and_release(self, rc):
        assert await rc.try_acquire_slot("cpu", 2, "h1") is True
        assert await rc.get_pool_count("cpu") == 1
        assert await rc.try_acquire_slot("cpu", 2, "h2") is True
        assert await rc.get_pool_count("cpu") == 2
        assert await rc.try_acquire_slot("cpu", 2, "h3") is False   # 满

        await rc.release_slot("cpu", "h1")
        assert await rc.get_pool_count("cpu") == 1
        assert await rc.try_acquire_slot("cpu", 2, "h3") is True     # 空出名额,h3 可进

    @pytest.mark.asyncio
    async def test_frozen_blocks_acquire(self, rc):
        await rc.freeze_pool("cpu")
        assert await rc.is_pool_frozen("cpu") is True
        assert await rc.try_acquire_slot("cpu", 10, "h1") is False

        await rc.unfreeze_pool("cpu")
        assert await rc.is_pool_frozen("cpu") is False
        assert await rc.try_acquire_slot("cpu", 10, "h1") is True

    @pytest.mark.asyncio
    async def test_acquire_over_limit(self, rc):
        assert await rc.try_acquire_slot("test_pool", 1, "h1") is True
        assert await rc.try_acquire_slot("test_pool", 1, "h2") is False   # 不同 holder,满

    @pytest.mark.asyncio
    async def test_acquire_atomicity(self, rc):
        # 三个不同 holder 并发抢 limit=1:仅一个进(SCARD<limit 原子判定)。
        results = await asyncio.gather(
            rc.try_acquire_slot("scene", 1, "h1"),
            rc.try_acquire_slot("scene", 1, "h2"),
            rc.try_acquire_slot("scene", 1, "h3"),
        )
        assert sum(results) == 1

    @pytest.mark.asyncio
    async def test_idempotent_release_no_double_decrement(self, rc):
        # SREM 幂等:同一 holder 释放两次,不会把别人的槽也减掉,避免裸 DECR 的双减/泄漏。
        await rc.try_acquire_slot("cpu", 5, "h1")
        await rc.try_acquire_slot("cpu", 5, "h2")
        assert await rc.get_pool_count("cpu") == 2
        assert await rc.release_slot("cpu", "h1") is True
        assert await rc.release_slot("cpu", "h1") is False   # 再放已无此 holder = 安全 no-op
        assert await rc.get_pool_count("cpu") == 1            # h2 仍在,未被双减误伤

    @pytest.mark.asyncio
    async def test_full_pool_same_holder_reacquire_idempotent(self, rc):
        # 满载时同一 holder 重新占(认领重试/重入)幂等放行,不被误拒、不重复计数。走 SISMEMBER 分支。
        assert await rc.try_acquire_slot("cpu", 1, "h1") is True
        assert await rc.get_pool_count("cpu") == 1            # 满
        assert await rc.try_acquire_slot("cpu", 1, "h1") is True   # 同 holder 重占
        assert await rc.get_pool_count("cpu") == 1            # 仍 1,没多占
        assert await rc.try_acquire_slot("cpu", 1, "h2") is False  # 别人仍被挡

    @pytest.mark.asyncio
    async def test_resource_slot_by_holder(self, rc):
        assert await rc.try_acquire_resource("acct:a", 1, "h1") is True
        assert await rc.get_resource_count("acct:a") == 1
        assert await rc.try_acquire_resource("acct:a", 1, "h2") is False
        await rc.release_resource("acct:a", "h1")
        assert await rc.get_resource_count("acct:a") == 0

    @pytest.mark.asyncio
    async def test_get_all_holders_and_release_holders(self, rc):
        # 跨池 + 资源槽 holder 全集 + 定向释放(删 running job / 对账用)。
        await rc.try_acquire_slot("cpu", 9, "h1")
        await rc.try_acquire_slot("io", 9, "h2")
        await rc.try_acquire_resource("acct:x", 9, "h1")     # h1 同时占资源槽
        assert await rc.get_all_holders() == {"h1", "h2"}
        removed = await rc.release_holders({"h1"})
        assert removed == 2                                   # cpu 池 + acct:x 资源槽各 SREM 一个
        assert await rc.get_all_holders() == {"h2"}
        assert await rc.get_pool_count("cpu") == 0
        assert await rc.get_resource_count("acct:x") == 0
        assert await rc.release_holders(set()) == 0           # 空集 = no-op

    @pytest.mark.asyncio
    async def test_pool_limit_override_roundtrip(self, rc):
        assert await rc.get_pool_limit_override("cpu") is None
        await rc.set_pool_limit_override("cpu", 5)
        assert await rc.get_pool_limit_override("cpu") == 5
        assert await rc.get_all_pool_limit_overrides() == {"cpu": 5}
        await rc.clear_pool_limit_override("cpu")
        assert await rc.get_pool_limit_override("cpu") is None
        assert await rc.get_all_pool_limit_overrides() == {}

    @pytest.mark.asyncio
    async def test_release_unheld_returns_false(self, rc):
        """释放一个从未持有的 holder → SREM 返回 0(False),计数不变负。"""
        result = await rc.release_slot("empty_pool", "nobody")
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
        # 对齐 cleanup_job:清该步在所有 per-step hash 的 field,不留惰性垃圾。
        jid = "j_x"
        await rc.set_step_status(jid, "A", "done")
        for sub in ("retries", "step_worker", "step_exec", "step_resources", "step_progress"):
            await rc.r.hset(f"job:{jid}:{sub}", "A", "v")
        await rc.delete_step_status(jid, "A")
        for sub in ("steps", "retries", "step_worker", "step_exec", "step_resources", "step_progress"):
            assert await rc.r.hget(f"job:{jid}:{sub}", "A") is None

    @pytest.mark.asyncio
    async def test_reset_step_retries(self, rc):
        # rerun 前清重试计数:reset 后归零,重跑步骤恢复重试预算。
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
        # 接入 token 不该污染 worker 列表:否则对 string 键 hgetall 会 WRONGTYPE,/api/workers 报 500。
        await rc.register_worker("cpu-1", {"type": "cpu"})
        await rc.set_registration_token("flw-secret", ttl_sec=3600)
        ids = await rc.list_worker_ids()
        assert set(ids) == {"cpu-1"}
        assert await rc.get_registration_token() == "flw-secret"

    @pytest.mark.asyncio
    async def test_legacy_worker_registration_token_key_skipped(self, rc):
        # 兼容旧数据:token 可能残留在 worker:registration_token(string 键)。扫描须跳过,不得 WRONGTYPE。
        await rc.register_worker("cpu-1", {"type": "cpu"})
        await rc._redis.set("worker:registration_token", "flw-legacy")
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
    async def test_publish_subscribe(self, rc, monkeypatch):
        received = []
        subscribed = asyncio.Event()
        real_pubsub = rc.r.pubsub

        def tracked_pubsub(*args, **kwargs):
            pubsub = real_pubsub(*args, **kwargs)
            real_subscribe = pubsub.subscribe

            async def tracked_subscribe(*channels, **options):
                result = await real_subscribe(*channels, **options)
                subscribed.set()
                return result

            pubsub.subscribe = tracked_subscribe
            return pubsub

        monkeypatch.setattr(rc.r, "pubsub", tracked_pubsub)

        async def listener():
            async for msg in rc.subscribe("test_channel"):
                received.append(msg)
                break

        task = asyncio.create_task(listener())
        await asyncio.wait_for(subscribed.wait(), timeout=1.0)
        await rc.publish("test_channel", {"event": "step_done", "step": "03_scene"})
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1
        assert received[0]["event"] == "step_done"

    @pytest.mark.asyncio
    async def test_subscribe_survives_connection_error(self, rc, monkeypatch):
        """连接级异常(TimeoutError)不应让 subscribe 抛出,而是重连重订阅后继续。"""
        from redis.exceptions import TimeoutError as RedisTimeoutError

        received = []
        calls = {"pubsub": 0, "getmsg": 0}
        resubscribed = asyncio.Event()
        real_pubsub = rc.r.pubsub

        def flaky_pubsub(*a, **kw):
            calls["pubsub"] += 1
            ps = real_pubsub(*a, **kw)
            real_get = ps.get_message
            real_subscribe = ps.subscribe

            async def tracked_subscribe(*channels, **options):
                result = await real_subscribe(*channels, **options)
                if calls["pubsub"] >= 2:
                    resubscribed.set()
                return result

            async def flaky_get_message(*ga, **gkw):
                calls["getmsg"] += 1
                # 首次 get_message 抛连接级异常,触发重连重订阅。
                if calls["getmsg"] == 1:
                    raise RedisTimeoutError("simulated")
                return await real_get(*ga, **gkw)

            ps.get_message = flaky_get_message  # type: ignore[assignment]
            ps.subscribe = tracked_subscribe  # type: ignore[assignment]
            return ps

        # reconnect 不真正换连接(fakeredis),只让 pubsub 重新创建走 real 路径。
        async def fake_reconnect():
            return None

        monkeypatch.setattr(rc, "reconnect", fake_reconnect)
        monkeypatch.setattr(rc.r, "pubsub", flaky_pubsub)

        async def listener():
            async for msg in rc.subscribe("ch2"):
                received.append(msg)
                break

        task = asyncio.create_task(listener())
        await asyncio.wait_for(resubscribed.wait(), timeout=4.0)
        await rc.publish("ch2", {"event": "ok"})
        await asyncio.wait_for(task, timeout=4.0)

        assert calls["pubsub"] >= 2  # 至少重订阅过一次
        assert received and received[0]["event"] == "ok"


class TestComponentHeartbeat:
    @pytest.mark.asyncio
    async def test_set_get_component_heartbeat(self, rc):
        await rc.set_component_heartbeat("scheduler", {
            "version": "abc123", "loop_lag_sec": 0.5, "pid": 7,
        })
        hb = await rc.get_component_heartbeat("scheduler")
        assert hb is not None
        assert hb["version"] == "abc123"
        assert hb["loop_lag_sec"] == "0.5"   # 全转 str 存
        assert "ts" in hb                      # set 注入 ts

    @pytest.mark.asyncio
    async def test_get_missing_component_returns_none(self, rc):
        assert await rc.get_component_heartbeat("nope") is None

    @pytest.mark.asyncio
    async def test_component_heartbeat_has_ttl(self, rc):
        await rc.set_component_heartbeat("scheduler", {"version": "x"})
        ttl = await rc.r.ttl("component:scheduler")
        assert 0 < ttl <= rc.COMPONENT_TTL


class TestServerInfo:
    @pytest.mark.asyncio
    async def test_server_info_parses(self, rc, monkeypatch):
        # fakeredis 不支持 INFO,mock .r.info/.ping 验解析逻辑(ping 计时 + version/内存/连接数)。
        from unittest.mock import AsyncMock

        async def fake_info(section):
            return {
                "server": {"redis_version": "7.2.4", "uptime_in_seconds": 1000},
                "memory": {"used_memory": 2097152, "used_memory_human": "2.0M",
                           "maxmemory": 0},
                "clients": {"connected_clients": 3},
            }[section]

        monkeypatch.setattr(rc.r, "info", fake_info)
        monkeypatch.setattr(rc.r, "ping", AsyncMock(return_value=True))
        info = await rc.server_info()
        assert info["version"] == "7.2.4"
        assert info["used_memory_mb"] == 2.0
        assert info["maxmemory_mb"] == 0.0
        assert info["connected_clients"] == 3
        assert info["uptime_sec"] == 1000
        assert info["ping_ms"] >= 0


class TestSystemEvents:
    @pytest.mark.asyncio
    async def test_push_event_ring_and_fields(self, rc):
        await rc.push_event("orphan_reclaimed", job_id="j1", step="03_scene", reason="worker lost", pool=None)
        await rc.push_event("job_failed", job_id="j2", error="boom")
        raw = await rc.r.lrange("events:system", 0, -1)
        assert len(raw) == 2
        top = json.loads(raw[0])          # 最近在上(LPUSH)
        assert top["kind"] == "job_failed" and top["job_id"] == "j2" and top["error"] == "boom"
        assert "ts" in top
        second = json.loads(raw[1])
        assert second["kind"] == "orphan_reclaimed" and second["reason"] == "worker lost"
        assert "pool" not in second       # None 字段剔除

    @pytest.mark.asyncio
    async def test_push_event_trims_to_200(self, rc):
        for i in range(210):
            await rc.push_event("worker_cleaned", worker_id=f"w{i}")
        assert await rc.r.llen("events:system") == 200


class TestTraffic:
    @pytest.mark.asyncio
    async def test_incr_and_get_by_worker_and_total(self, rc):
        await rc.incr_traffic("pull", "ai-1", 100)
        await rc.incr_traffic("pull", "ai-1", 50)
        await rc.incr_traffic("pull", "cpu-2", 30)
        t = await rc.get_traffic("pull")
        assert t["total"] == 180
        assert t["by_worker"] == {"ai-1": 150, "cpu-2": 30}

    @pytest.mark.asyncio
    async def test_directions_are_independent(self, rc):
        await rc.incr_traffic("pull", "ai-1", 100)
        await rc.incr_traffic("push", "ai-1", 7)
        pull = await rc.get_traffic("pull")
        push = await rc.get_traffic("push")
        assert pull["total"] == 100 and pull["by_worker"] == {"ai-1": 100}
        assert push["total"] == 7 and push["by_worker"] == {"ai-1": 7}

    @pytest.mark.asyncio
    async def test_get_empty_returns_zero(self, rc):
        t = await rc.get_traffic("pull")
        assert t == {"total": 0, "by_worker": {}}

    @pytest.mark.asyncio
    async def test_zero_or_empty_worker_skipped(self, rc):
        # n<=0 或空 worker_id 不写(不污染 hash,且避免无谓 HINCRBY)。
        await rc.incr_traffic("pull", "ai-1", 0)
        await rc.incr_traffic("pull", "", 100)
        await rc.incr_traffic("pull", "ai-1", -5)
        t = await rc.get_traffic("pull")
        assert t == {"total": 0, "by_worker": {}}

    @pytest.mark.asyncio
    async def test_incr_never_raises_on_backend_error(self, rc, monkeypatch):
        # best-effort:redis 抛错也吞掉(产物传输优先于计数)。
        async def boom(*a, **kw):
            raise RuntimeError("redis down")
        monkeypatch.setattr(rc.r, "hincrby", boom)
        await rc.incr_traffic("pull", "ai-1", 100)  # 不抛即通过


class TestLinkTraffic:
    """链路流量快照 + 时间线(tunnel_stats 上报器写,/api/status 读)。"""

    @pytest.mark.asyncio
    async def test_link_traffic_roundtrip(self, rc):
        assert await rc.get_link_traffic() is None  # 未写过 → None
        payload = {"ts": 1.0, "tunnel": {"rx": 100, "tx": 50, "up": True, "tunnels": []}}
        await rc.set_link_traffic(payload)
        assert await rc.get_link_traffic() == payload

    @pytest.mark.asyncio
    async def test_timeline_capped_and_recent_first(self, rc):
        for i in range(5):
            await rc.push_traffic_sample({"ts": i, "tun_rx": i}, cap=3)
        tl = await rc.get_traffic_timeline()
        assert [s["ts"] for s in tl] == [4, 3, 2]  # LTRIM 保留最近 3、最近在前

    @pytest.mark.asyncio
    async def test_link_traffic_read_never_raises(self, rc, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("redis down")
        monkeypatch.setattr(rc.r, "get", boom)
        monkeypatch.setattr(rc.r, "lrange", boom)
        assert await rc.get_link_traffic() is None  # 失败回 None,不抛
        assert await rc.get_traffic_timeline() == []


class TestRemoveJobTasks:
    """删 job 时精准清队列里其未认领的排队 task(queue:{pool} + queue:enqueued)。"""

    @pytest.mark.asyncio
    async def test_removes_only_target_job_across_pools(self, rc):
        # job_a 在 cpu+ai 两池各有 task;job_b 在 cpu 有 task。
        await rc.enqueue_step("cpu", "job_a", "04_smart", ["cpu"], priority=-3)
        await rc.enqueue_step("ai", "job_a", "05_concepts", ["ai"], priority=-3)
        await rc.enqueue_step("cpu", "job_b", "04_smart", ["cpu"], priority=-3)

        removed = await rc.remove_job_tasks("job_a")
        assert removed == 2
        # job_a 两条都没了,job_b 还在。
        cpu = await rc.list_queue("cpu")
        ai = await rc.list_queue("ai")
        assert [t["job_id"] for t in cpu] == ["job_b"]
        assert ai == []
        # queue:enqueued:job_a 的时间戳清掉,job_b 的保留。
        ats = await rc.r.hgetall("queue:enqueued")
        keys = set(ats.keys())
        assert "cpu|job_b|04_smart" in keys
        assert not any(k.startswith("cpu|job_a|") or k.startswith("ai|job_a|") for k in keys)

    @pytest.mark.asyncio
    async def test_empty_safe(self, rc):
        assert await rc.remove_job_tasks("nope") == 0


class TestAITaskQueue:
    """独立 AI task(kind='ai')投递/队列窥视/结果回执 + 与 step task 向后兼容。"""

    @pytest.mark.asyncio
    async def test_enqueue_ai_task_and_list(self, rc):
        from shared.models import AITask, LLMRequest
        payload = AITask(
            task_id="at_x",
            request=LLMRequest(messages=[{"role": "user", "content": "q"}]),
            step_name="synthesis", domain="dl",
        ).to_task_payload()
        await rc.enqueue_ai_task(payload, priority=-1)
        listed = await rc.list_queue("ai")
        assert len(listed) == 1
        item = listed[0]
        assert item["kind"] == "ai" and item["task_id"] == "at_x"
        assert item["job_id"] is None and item["step"] == "synthesis"
        assert item["require_tags"] == ["claude-cli"]
        assert item["enqueued_at"] is not None  # ai|at_x 入队时间戳能 join 上

    @pytest.mark.asyncio
    async def test_step_task_backward_compatible(self, rc):
        # 普通 step task 列出:kind 缺省 'step'、task_id None;旧行为不破
        await rc.enqueue_step("cpu", "j1", "06_ocr", ["cpu"], priority=-1)
        item = (await rc.list_queue("cpu"))[0]
        assert item["kind"] == "step" and item["task_id"] is None
        assert item["job_id"] == "j1" and item["step"] == "06_ocr"

    @pytest.mark.asyncio
    async def test_dequeue_ai_task_cleans_enqueued(self, rc):
        from shared.models import AITask, LLMRequest
        await rc.enqueue_ai_task(
            AITask(task_id="at_d", request=LLMRequest(messages=[])).to_task_payload()
        )
        raw, parsed, score = await rc.dequeue_step_raw("ai")
        assert parsed["kind"] == "ai" and parsed["task_id"] == "at_d"
        assert await rc.list_queue("ai") == []
        ats = await rc.r.hgetall("queue:enqueued")
        assert not any("at_d" in k for k in ats.keys())  # 出队即清,无孤儿时间戳

    @pytest.mark.asyncio
    async def test_set_get_ai_result(self, rc):
        await rc.set_ai_result("at_r", {"content": "answer", "provider": "claude-cli"}, ttl=60)
        got = await rc.get_ai_result("at_r")
        assert got["content"] == "answer" and got["provider"] == "claude-cli"
        assert await rc.get_ai_result("missing") is None
