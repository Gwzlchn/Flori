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
from shared.models import AITask, Job, LLMRequest, LLMResponse, Step, StepStatus
from shared.storage import LocalStorage
from worker.worker import (
    Worker, WORKER_POOLS, auto_discover_tags, _resolve_worker_id, _probe_net_zones,
    compute_effective_timeout, _read_media_duration,
)
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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id
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
    @pytest.fixture(autouse=True)
    def _no_real_net_probe(self):
        # auto_discover_tags 现会做网络探测(net-zone),默认屏蔽真探测(返回空 zone),
        # 避免单测联网/卡;net-zone 专项用例自行 patch _probe_reachable 验证逻辑。
        with patch("worker.worker._probe_net_zones", return_value=set()):
            yield

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

    _CRED_ENV = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL",
                 "BILI_SESSDATA", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

    def _clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in self._CRED_ENV}
        env.update(extra)
        return env

    def test_sessdata_does_not_add_routing_tag(self):
        # SESSDATA 是 worker 本地凭证(下载步自读),【不】再自报 'bili' 路由 tag。
        with patch.dict(os.environ, self._clean_env(BILI_SESSDATA="x", DATA_DIR="/no-such"), clear=True):
            with patch("shutil.which", return_value=None):
                assert "bili" not in auto_discover_tags()

    def test_net_zones_merged_into_tags(self):
        # _probe_net_zones 探出的区域并入 tags(覆盖 autouse 的空 mock)。
        with patch.dict(os.environ, self._clean_env(DATA_DIR="/no-such"), clear=True):
            with patch("shutil.which", return_value=None):
                with patch("worker.worker._probe_net_zones", return_value={"net-cn", "net-global"}):
                    tags = auto_discover_tags()
                    assert "net-cn" in tags and "net-global" in tags

    def test_no_cred_no_bili_no_net_proxy(self, tmp_path):
        # 无凭证 → 无 bili;net-proxy 机制已移除(改 net-zone,由探测决定,本用例 autouse 屏蔽为空)。
        with patch.dict(os.environ, self._clean_env(DATA_DIR=str(tmp_path)), clear=True):
            with patch("shutil.which", return_value=None):
                tags = auto_discover_tags()
                assert "bili" not in tags
                assert "net-proxy" not in tags


class TestNetZoneProbe:
    """net-zone 自动探测:env 强制覆盖 / 按探针可达性判 / 不联网(_probe_reachable mock)。"""

    def test_env_override_skips_probe(self):
        # NET_ZONES 显式覆盖 → 直接用,不探测(香港 worker 设 NET_ZONES=global)。
        with patch.dict(os.environ, {"NET_ZONES": "global"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=AssertionError("不该探测")):
                assert _probe_net_zones() == {"net-global"}

    def test_probe_both_reachable(self):
        with patch.dict(os.environ, {"NET_ZONES": ""}, clear=False):
            with patch("worker.worker._probe_reachable", return_value=True):
                assert _probe_net_zones() == {"net-cn", "net-global"}

    def test_probe_only_cn(self):
        # 国内无代理:CN 探针通、global 不通 → 仅 net-cn。
        with patch.dict(os.environ, {"NET_ZONES": "", "NET_PROBE_CN": "https://cn", "NET_PROBE_GLOBAL": "https://g"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=lambda u, **k: u == "https://cn"):
                assert _probe_net_zones() == {"net-cn"}

    def test_probe_only_global(self):
        # 香港:global 通、CN(B站)不通 → 仅 net-global。
        with patch.dict(os.environ, {"NET_ZONES": "", "NET_PROBE_CN": "https://cn", "NET_PROBE_GLOBAL": "https://g"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=lambda u, **k: u == "https://g"):
                assert _probe_net_zones() == {"net-global"}


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

    @pytest.mark.asyncio
    async def test_heartbeat_writes_live_load_to_redis(self, worker, redis):
        # B 档:心跳带 load → 写 redis worker hash 的 load 字段(JSON);空 load 不写。
        await worker.register()
        await worker.transport.heartbeat(
            worker.worker_id, load={"cpu_pct": 12.5, "mem_pct": 40.0, "loadavg": 0.7},
        )
        info = await redis.get_worker_info(worker.worker_id)
        assert info is not None
        load = json.loads(info["load"])
        assert load["cpu_pct"] == 12.5 and load["loadavg"] == 0.7

    @pytest.mark.asyncio
    async def test_heartbeat_no_load_leaves_field_absent(self, worker, redis):
        await worker.register()
        await worker.transport.heartbeat(worker.worker_id, load=None)
        info = await redis.get_worker_info(worker.worker_id)
        assert "load" not in info   # 不写空,保留上次(此处从未写过)


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
        error_data = {"error_type": "ai_rate_limit", "message": "429 rate limited"}
        (job_dir / ".A.error.json").write_text(json.dumps(error_data))

        etype, emsg = worker._parse_error(job_dir, "A")
        assert etype == "ai_rate_limit"
        assert emsg == "429 rate limited"   # message 用于 stderr 为空时的兜底

    def test_missing_file(self, worker, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j2"
        job_dir.mkdir()
        assert worker._parse_error(job_dir, "A") == ("unknown", "")

    def test_corrupt_json(self, worker, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j3"
        job_dir.mkdir()
        (job_dir / ".A.error.json").write_text("not json")
        assert worker._parse_error(job_dir, "A") == ("unknown", "")


class TestExecuteFullFlow:
    """execute 全流程测试：mock _run_step 避免真实子进程。"""

    @pytest.mark.asyncio
    async def test_success_publishes_and_updates_db(self, worker, redis, db, tmp_jobs_dir):
        await worker.register()  # 让 transport._worker_id 与 worker.worker_id 一致
        job = make_job()
        db.create_job(job)
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id

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
    async def test_push_failure_on_success_reports_failed_not_done(self, worker, redis, db, tmp_jobs_dir):
        # ★ returncode==0 但产物推送失败 → 必须报 failed(绝不标 done),否则下游拉不到输入
        #   (上游 done 但产物缺失 → input_missing)。重试时会重新生成并推送。
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            return 0, ""
        worker.runner.run_step = mock_run_step

        async def boom_push(job_id, step, work_dir):
            raise RuntimeError("minio down")
        worker.storage.push = boom_push

        await worker.execute(make_claim())

        assert db.get_steps("j_test_001")[0].status == StepStatus.FAILED  # 不是 DONE
        assert await redis.get_pool_count("cpu") == 0                     # 槽位仍释放

    @pytest.mark.asyncio
    async def test_minimal_claim_resolves_pipeline_via_transport(self, worker, redis, db, tmp_jobs_dir):
        # 最小 claim(无 pipeline/domain/style_tags)→ execute 在 try 内经 transport 回读后跑完。
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.init_job("j_test_001", "test", {"domain": "lecture", "style_tags": "[]"})
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id
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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id
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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id

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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id

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
        # Fill pool to capacity (limit=3 in fixture)——须用【不同】holder 才真占满(同 holder 幂等只占 1)。
        for i in range(3):
            await redis.try_acquire_slot("cpu", 3, f"filler{i}")

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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id

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
        await redis.try_acquire_slot("cpu", 3, "w_test:1")   # holder = claim 的 exec_id
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


class _FakeGateway:
    """假 AIGateway:.call 返回预置 LLMResponse 或抛异常(测 ai-task 分流执行,不真调 claude)。"""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def call(self, step_name, request):
        if self._exc is not None:
            raise self._exc
        return self._resp


class TestAITaskExecution:
    """worker 认领并执行独立 AI task(kind='ai')+ 白盒审计 + 错误回执 + 认领路由(P1-2)。"""

    def _ai_claim(self, task_id="at_1", step="synthesis", domain="dl"):
        return {
            "kind": "ai", "task_id": task_id, "step": step, "pool": "ai", "exec_id": "w:1",
            "request": LLMRequest(messages=[{"role": "user", "content": "Q"}], system="S").to_jsonable(),
            "domain": domain,
        }

    @pytest.mark.asyncio
    async def test_execute_success(self, worker, redis, db, monkeypatch):
        resp = LLMResponse(content="ANSWER", model="claude-opus-4-8", provider="claude-cli",
                           cost_usd=0.12, input_tokens=100, output_tokens=50, num_turns=1,
                           attempts=[{"tier": "primary"}], session_id="s1")
        monkeypatch.setattr("worker.worker.AIGateway", lambda p, pl: _FakeGateway(resp=resp))
        await worker._execute_ai_task(self._ai_claim("at_ok"))
        # 1) 结果回执 airesult
        got = await redis.get_ai_result("at_ok")
        assert got["content"] == "ANSWER" and got["provider"] == "claude-cli"
        # 2) 白盒审计落表(渲染 prompt/输出/尝试链/用量)
        logs = db.get_ai_task_logs("at_ok")
        assert len(logs) == 1 and logs[0]["ok"] == 1
        assert logs[0]["provider"] == "claude-cli" and logs[0]["step_name"] == "synthesis"
        rec = json.loads(logs[0]["record_json"])
        assert rec["output"] == "ANSWER" and rec["prompt"]["system"] == "S"
        assert rec["routing"]["attempts"] == [{"tier": "primary"}]
        assert rec["usage"]["input_tokens"] == 100
        # 3) 成本归因 ai_usage(job_id null, step=synthesis)
        rows = db._conn.execute("SELECT job_id, step, cost_usd FROM ai_usage").fetchall()
        assert len(rows) == 1
        assert rows[0]["job_id"] is None and rows[0]["step"] == "synthesis"
        assert abs(rows[0]["cost_usd"] - 0.12) < 1e-9

    @pytest.mark.asyncio
    async def test_execute_error_sets_error_result(self, worker, redis, db, monkeypatch):
        monkeypatch.setattr("worker.worker.AIGateway",
                            lambda p, pl: _FakeGateway(exc=RuntimeError("provider down")))
        await worker._execute_ai_task(self._ai_claim("at_err", step="digest"))  # 不抛(绝不崩 worker)
        got = await redis.get_ai_result("at_err")
        assert "error" in got and "provider down" in got["error"]
        logs = db.get_ai_task_logs("at_err")
        assert len(logs) == 1 and logs[0]["ok"] == 0
        assert "provider down" in (logs[0]["error"] or "")

    @pytest.mark.asyncio
    async def test_claim_routes_ai_task_gated_by_tag(self, redis, db, config, storage):
        # 有 claude-cli tag 的 ai-worker 能认领,且 claim 是 ai 形态(无 job_id,带 request)
        w = Worker(transport=RedisTransport(redis, db), config=config, storage=storage,
                   worker_type="ai", pools=["ai"], tags={"claude-cli"}, reject_tags=set())
        await w.register()
        await redis.enqueue_ai_task(
            AITask(task_id="at_c", request=LLMRequest(messages=[]), step_name="synthesis").to_task_payload())
        claim = await request_step(w)
        assert claim is not None and claim["kind"] == "ai"
        assert claim["task_id"] == "at_c" and claim["step"] == "synthesis"
        assert "request" in claim and "job_id" not in claim
        # 无 claude-cli tag 的 worker 不应认领(require_tags 门控)
        w2 = Worker(transport=RedisTransport(redis, db), config=config, storage=storage,
                    worker_type="cpu", pools=["ai"], tags=set(), reject_tags=set())
        await w2.register()
        await redis.enqueue_ai_task(
            AITask(task_id="at_c2", request=LLMRequest(messages=[]), step_name="digest").to_task_payload())
        assert await request_step(w2) is None


class TestComputeEffectiveTimeout:
    """长集 whisper 超时随媒体时长伸缩(纯函数)。"""

    def test_no_per_min_returns_base(self):
        assert compute_effective_timeout(1800, None, 6000) == 1800

    def test_no_duration_returns_base(self):
        assert compute_effective_timeout(1800, 90, None) == 1800
        assert compute_effective_timeout(1800, 90, 0) == 1800

    def test_short_audio_uses_base_floor(self):
        # 10 分钟 * 90 = 900 < 1800 下限 → 1800。
        assert compute_effective_timeout(1800, 90, 600) == 1800

    def test_long_audio_scales(self):
        # 90 分钟 * 90 = 8100 > 1800 → 8100。
        assert compute_effective_timeout(1800, 90, 90 * 60) == 8100

    def test_rounds_up_partial_minute(self):
        # 89.5 分钟 → ceil=90 → 8100。
        assert compute_effective_timeout(1800, 90, 89.5 * 60) == 8100

    def test_cap_clamps(self):
        # 10h * 90 = 54000,但 cap=21600 → 21600。
        assert compute_effective_timeout(1800, 90, 10 * 3600, 21600) == 21600


class TestReadMediaDuration:
    def test_reads_duration(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "metadata.json").write_text(json.dumps({"duration_sec": 123.4}))
        assert _read_media_duration(tmp_path) == 123.4

    def test_missing_file_none(self, tmp_path):
        assert _read_media_duration(tmp_path) is None

    def test_missing_field_none(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "metadata.json").write_text(json.dumps({"source": "podcast"}))
        assert _read_media_duration(tmp_path) is None


class TestWorkerAuthRecovery:
    """worker 认证自愈:401(WorkerAuthRejected)→ 重注册 + 指数退避;连续超阈值 → 自杀(_shutdown)。
    根因:旧 worker 拿 401 不重注册也不退避,每秒死刷 jobs/request 刷爆 api 日志。"""

    @pytest.mark.asyncio
    async def test_auth_failure_reregisters_and_backs_off(self, worker, monkeypatch):
        calls = []
        async def fake_register():
            calls.append(1)
        monkeypatch.setattr(worker, "register", fake_register)
        assert worker._auth_failed_since is None
        await worker._handle_auth_failure()
        assert worker._auth_failed_since is not None      # 进入失败态
        assert len(calls) == 1                             # 触发重注册
        assert worker._auth_backoff == 2.0                 # 退避翻倍(1→2)
        assert not worker._shutdown

    @pytest.mark.asyncio
    async def test_note_auth_ok_resets(self, worker, monkeypatch):
        async def fake_register():
            return None
        monkeypatch.setattr(worker, "register", fake_register)
        await worker._handle_auth_failure()
        assert worker._auth_failed_since is not None
        worker._note_auth_ok()                             # 一次成功请求
        assert worker._auth_failed_since is None           # 清零自愈
        assert worker._auth_backoff == 1.0

    @pytest.mark.asyncio
    async def test_giveup_after_timeout_sets_shutdown(self, worker, monkeypatch):
        worker._auth_giveup_sec = 100
        async def fake_register():
            return None
        monkeypatch.setattr(worker, "register", fake_register)
        await worker._handle_auth_failure()                # 首发:置 _auth_failed_since=now
        assert not worker._shutdown                         # 未超时
        worker._auth_failed_since -= 200                    # 回拨首发时刻,模拟已连续失败 200s>100s
        await worker._handle_auth_failure()
        assert worker._shutdown                             # 自杀

    @pytest.mark.asyncio
    async def test_reauth_debounced(self, worker, monkeypatch):
        calls = []
        async def fake_register():
            calls.append(1)
        monkeypatch.setattr(worker, "register", fake_register)
        await worker._handle_auth_failure()                # 第一次重注册
        await worker._handle_auth_failure()                # 30s 窗口内 → 不重注册(去抖)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_gateway_request_step_raises_authrejected_on_401(self, monkeypatch):
        from worker.gateway_transport import GatewayTransport
        from worker.transport import WorkerAuthRejected

        class _Resp:
            status_code = 401
            def raise_for_status(self):
                pass
            def json(self):
                return {}

        class _FakeClient:
            async def post(self, *a, **k):
                return _Resp()

        gt = GatewayTransport("https://x", registration_token="t",
                              id_file="/tmp/_nonexistent_worker_id")
        gt._client = _FakeClient()                          # _http 属性 None 检查后直接返回它
        with pytest.raises(WorkerAuthRejected):
            await gt.request_step("w1", ["cpu"], {}, set(), set())
