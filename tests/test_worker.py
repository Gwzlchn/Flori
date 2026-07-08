"""worker 单测:用 fakeredis + 临时 DB。"""

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
    Worker, auto_discover_tags, _resolve_worker_id, _probe_net_zones,
    compute_effective_timeout, _read_media_duration, _codex_logged_in,
)
from worker.transport import RedisTransport


# Fixtures


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
    """入队 + 置 ready + init_job,凑齐可认领的最小状态。"""
    await redis.enqueue_step(pool, job_id, step, tags or [], priority)
    await redis.set_step_status(job_id, step, "ready")
    await redis.init_job(job_id, "test", {"domain": "general", "style_tags": "[]"})


async def request_step(worker):
    """按 worker 自身 pools/tags 走完整 transport 认领。"""
    return await worker.transport.request_step(
        worker.worker_id, worker.pools, worker._pool_limits(),
        worker.tags, worker.reject_tags,
    )


# Tests


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
        # 刚注册即心跳,公共状态衍生为 online-idle(存量列仍是 idle)
        assert db_worker.status == "online-idle"


class TestTagMatching:
    """标签匹配由 request_step 编排;worker 仅设 pools=[cpu] 隔离。"""

    @pytest.fixture(autouse=True)
    def _cpu_only(self, worker):
        worker.pools = ["cpu"]

    @pytest.mark.asyncio
    async def test_accept_matching_require_tags(self, worker, redis):
        """require_tags 是 worker.tags 子集时 accept."""
        await redis.enqueue_step("cpu", "j1", "A", ["vision"], priority=0,
                                 require_tags=["vision"])
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        claim = await request_step(worker)
        assert claim is not None
        assert claim["job_id"] == "j1"

    @pytest.mark.asyncio
    async def test_reject_tags_block(self, worker, redis):
        """tags 与 reject_tags 相交时 put back,即使 require_tags 匹配."""
        await redis.enqueue_step("cpu", "j1", "A", ["vision", "private"], priority=0,
                                 require_tags=["vision"])
        claim = await request_step(worker)
        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # put back

    @pytest.mark.asyncio
    async def test_insufficient_require_tags(self, worker, redis):
        """require_tags 不是 worker.tags 子集时 put back."""
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
        # 队列里有任务但状态已是 running,CAS ready->running 失败.
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
    """认领/释放任何池都不自动冻结其他池。"""
    @pytest.mark.asyncio
    async def test_claiming_cpu_step_does_not_freeze(self, worker, redis):
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})
        worker.pools = ["cpu"]

        claim = await request_step(worker)
        assert claim is not None
        assert claim["pool"] == "cpu"
        # 关键:认领 cpu 步全程零冻结。
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
    不复刻表达式断言副本:那样改了真实代码测试仍绿。"""

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
        # worker 具 gpu 标签 + 认到 gpu 池时启用.
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="gpu") is True

    @pytest.mark.asyncio
    async def test_gpu_tag_cpu_pool_no_raw_gpu(self, worker, tmp_jobs_dir):
        # 具 gpu 标签但 cpu 池且步骤配置无 gpu 标签时不启用(挡误启).
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="cpu") is False

    @pytest.mark.asyncio
    async def test_gpu_tag_cpu_pool_step_tagged_gpu(self, worker, tmp_jobs_dir):
        # cpu 池但步骤配置 tags 含 gpu 时启用(覆盖 raw.get("tags") 分支).
        worker.config.pipelines["test"]["steps"][0]["tags"] = ["gpu"]  # step "A"
        assert await self._captured_use_gpu(worker, tmp_jobs_dir, pool="cpu") is True

    @pytest.mark.asyncio
    async def test_no_gpu_worker_tag(self, worker, tmp_jobs_dir):
        # worker 不具 gpu 标签时,即便 gpu 池也不启用(挡漏判/误启).
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
        # auto_discover_tags 内含 net-zone 网络探测;默认屏蔽真探测,返回空 zone,
        # 避免单测联网/卡。net-zone 专项用例自行 patch _probe_reachable 验证逻辑。
        with patch("worker.worker._probe_net_zones", return_value=set()), \
             patch("worker.worker._codex_logged_in", return_value=False):
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
        # 镜像自带 claude 二进制但无凭证(纯 gateway worker)时不该标 vision/claude-cli.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                with patch("worker.worker._claude_logged_in", return_value=False):
                    tags = auto_discover_tags()
                    assert "vision" not in tags
                    assert "claude-cli" not in tags

    def test_claude_logged_in_adds_vision_and_cli(self):
        # claude 订阅已登录(~/.claude/.credentials.json 在)时标 vision + claude-cli.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}
        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                with patch("worker.worker._claude_logged_in", return_value=True):
                    tags = auto_discover_tags()
                    assert "vision" in tags
                    assert "claude-cli" in tags

    def test_codex_logged_in_adds_vision_and_cli(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL")}

        def fake_which(name):
            return "/usr/bin/codex" if name == "codex" else None

        with patch.dict(os.environ, env, clear=True):
            with patch("shutil.which", side_effect=fake_which):
                with patch("worker.worker._codex_logged_in", return_value=True):
                    tags = auto_discover_tags()
                    assert "codex-cli" in tags
                    assert "vision" in tags

    def test_codex_logged_in_checks_codex_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".codex"
        home.mkdir()
        auth = home / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(home))
        assert _codex_logged_in() is True

    _CRED_ENV = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_URL",
                 "BILI_" + "SE" + "SS" + "DATA",
                 "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

    def _clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in self._CRED_ENV}
        env.update(extra)
        return env

    def test_sessdata_does_not_add_routing_tag(self):
        # SESSDATA 是 worker 本地凭证(下载步自读),不自报 'bili' 路由 tag。
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
        # 无凭证时无 bili;路由走 net-zone 探测,不产生 net-proxy tag(本用例 autouse 屏蔽探测为空).
        with patch.dict(os.environ, self._clean_env(DATA_DIR=str(tmp_path)), clear=True):
            with patch("shutil.which", return_value=None):
                tags = auto_discover_tags()
                assert "bili" not in tags
                assert "net-proxy" not in tags


class TestNetZoneProbe:
    """net-zone 自动探测:env 强制覆盖 / 按探针可达性判 / 不联网(_probe_reachable mock)。"""

    def test_env_override_skips_probe(self):
        # NET_ZONES 显式覆盖时直接用,不探测(香港 worker 设 NET_ZONES=global).
        with patch.dict(os.environ, {"NET_ZONES": "global"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=AssertionError("不该探测")):
                assert _probe_net_zones() == {"net-global"}

    def test_probe_both_reachable(self):
        with patch.dict(os.environ, {"NET_ZONES": ""}, clear=False):
            with patch("worker.worker._probe_reachable", return_value=True):
                assert _probe_net_zones() == {"net-cn", "net-global"}

    def test_probe_only_cn(self):
        # 国内无代理:CN 探针通,global 不通时仅 net-cn.
        with patch.dict(os.environ, {"NET_ZONES": "", "NET_PROBE_CN": "https://cn", "NET_PROBE_GLOBAL": "https://g"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=lambda u, **k: u == "https://cn"):
                assert _probe_net_zones() == {"net-cn"}

    def test_probe_only_global(self):
        # 香港:global 通,CN(B站)不通时仅 net-global.
        with patch.dict(os.environ, {"NET_ZONES": "", "NET_PROBE_CN": "https://cn", "NET_PROBE_GLOBAL": "https://g"}, clear=False):
            with patch("worker.worker._probe_reachable", side_effect=lambda u, **k: u == "https://g"):
                assert _probe_net_zones() == {"net-global"}


class TestWorkerPoolsCli:
    """能力统一用 --pools:--pools 必填、worker_type 从 pools 派生、
    多池派生 type 的 worker_id 前缀 '+' 转为 '-'."""

    def test_pools_required_and_no_type_arg(self, monkeypatch):
        import worker.main as wm
        monkeypatch.setattr("sys.argv", ["worker"])           # 不给 --pools 时必填报错
        with pytest.raises(SystemExit):
            wm.parse_args()
        monkeypatch.setattr("sys.argv", ["worker", "--pools", "cpu", "gpu"])
        args = wm.parse_args()
        assert args.pools == ["cpu", "gpu"]                   # 多池解析成列表
        assert not hasattr(args, "type")                      # 不存在 --type 参数

    def test_worker_type_derived_from_pools(self):
        # main 里 worker_type = "+".join(sorted(set(pools))):单池="cpu",多池="cpu+gpu"(无主次)。
        assert "+".join(sorted(set(["gpu", "cpu"]))) == "cpu+gpu"
        assert "+".join(sorted(set(["cpu"]))) == "cpu"

    def test_resolve_worker_id_sanitizes_multi_pool_type(self, monkeypatch):
        monkeypatch.setenv("WORKER_NAME", "gpu-rig")
        wid = _resolve_worker_id("cpu+gpu")
        assert wid.startswith("cpu-gpu-") and "+" not in wid  # id 前缀 '+' 转为 '-'


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
        # /api/workers 读 DB,状态变更必须写回 DB
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
        # 心跳循环必须刷新 DB 的 last_heartbeat,否则前端 30s 后判 offline
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
        # 心跳带 load 时写 redis worker hash 的 load 字段(JSON);空 load 不写.
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
    """execute 全流程测试:mock _run_step 避免真实子进程。"""

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
        # returncode==0 但产物推送失败时必须报 failed(绝不标 done):否则下游拉不到输入,
        # 上游 done 但产物缺失即 input_missing。重试时会重新生成并推送。
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
        # 最小 claim(无 pipeline/domain/style_tags)会在 execute 的 try 内经 transport 回读后跑完.
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
        # get_job_pipeline 抛错会被 execute 接住转 report_failed:步骤判失败,槽位释放,worker 不崩.
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
        # Fill pool to capacity (limit=3 in fixture)。须用不同 holder 才真占满:同 holder 幂等只占 1。
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
        """concurrency=N 时 supervisor 起 N 条认领循环(各带 slot 序号).
        supervisor 会对非闲退的 done 循环重生(崩溃兜底),故 run() 不再随 stub 秒退而返回,
        需显式 shutdown 收尾。全局每池槽位仍是系统级上限。"""
        w = Worker(
            transport=RedisTransport(redis, db), config=config, storage=storage,
            worker_type="cpu", pools=["cpu", "io"], tags=set(), reject_tags=set(),
            concurrency=3,
        )
        slots: list[int] = []

        async def fake_loop(slot=0):
            slots.append(slot)
            while not w._shutdown:          # 常驻直到 shutdown(贴近真实循环,避免被判定重生)
                await asyncio.sleep(0.05)

        async def fake_hb():
            return

        w._claim_loop = fake_loop
        w.heartbeat_loop = fake_hb
        task = asyncio.create_task(w.run())
        for _ in range(100):
            if len(slots) >= 3:
                break
            await asyncio.sleep(0.05)
        w.shutdown()
        await asyncio.wait_for(task, timeout=10)
        assert sorted(slots) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_supervisor_scales_up_and_down(self, redis, db, config, storage):
        """中心配置热调并发:扩=supervisor 补新 slot;缩=超编 slot 自退(_claim_loop 循环顶自检)。"""
        w = Worker(
            transport=RedisTransport(redis, db), config=config, storage=storage,
            worker_type="cpu", pools=["cpu"], tags=set(), reject_tags=set(),
            concurrency=1,
        )

        async def empty_request(*args, **kwargs):
            await asyncio.sleep(0.02)
            return None

        w.transport.request_step = empty_request
        sup = asyncio.create_task(w._claim_supervisor())
        try:
            await asyncio.sleep(0.1)
            # 扩容 1 到 3:supervisor 下一拍(2s)补 slot;直接断言状态经 _apply
            w._apply_desired_config(
                {"desired_config": {"concurrency": 3}, "cfg_rev": 1})
            assert w.concurrency == 3
            # 缩容 3 到 1:rev 更高才应用
            w._apply_desired_config(
                {"desired_config": {"concurrency": 1}, "cfg_rev": 2})
            assert w.concurrency == 1 and w._cfg_applied_rev == 2
        finally:
            w.shutdown()
            await asyncio.wait_for(sup, timeout=10)


class TestUploadFaultTolerance:
    """上报通道抖动不得污染步骤结论,也不得杀 worker。"""

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
        # finally release 抛出会逃逸 _claim_loop 杀掉整个 worker。
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
        """设了 WORKER_NAME 时 id = {type}-sha256(name)[:8],确定性:重复解析/删缓存都同一 id."""
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
        """没 WORKER_NAME 时随机 {type}-{8hex} 缓存,二次解析复用同一 id."""
        monkeypatch.delenv("WORKER_NAME", raising=False)
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "id"))
        first = _resolve_worker_id("cpu")
        assert first.startswith("cpu-")
        assert _resolve_worker_id("cpu") == first

    def test_default_id_file_under_workers_dir(self, monkeypatch):
        """默认 id 文件收进 worker 家目录 /data/workers/<name>/worker.id(缺省名归 default/)。"""
        from worker.transport import default_worker_id_file
        monkeypatch.delenv("WORKER_ID_FILE", raising=False)
        monkeypatch.delenv("WORKER_NAME", raising=False)
        assert default_worker_id_file() == "/data/workers/default/worker.id"
        monkeypatch.setenv("WORKER_NAME", "claude-1")
        assert default_worker_id_file() == "/data/workers/claude-1/worker.id"

    def test_explicit_id_file_overrides(self, tmp_path, monkeypatch):
        """WORKER_ID_FILE 显式覆盖语义不变(不做迁移/改写)。"""
        from worker.transport import default_worker_id_file
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "custom.id"))
        assert default_worker_id_file() == str(tmp_path / "custom.id")

    def test_legacy_flat_id_file_migrates_to_home_dir(self, monkeypatch):
        """旧平铺布局(/data/workers/<name> 是 id 文件)启动自迁移成家目录 + worker.id.
        id 内容不变,不触发重注册。幂等:二次调用不再动。"""
        import shutil
        import uuid
        from worker.transport import default_worker_id_file
        name = f"testmig-{uuid.uuid4().hex[:8]}"     # 唯一名,避免污染共享 /data(xdist 并行安全)
        legacy = Path(f"/data/workers/{name}")
        try:
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("cpu-abc12345")        # 旧平铺 id 文件
            monkeypatch.delenv("WORKER_ID_FILE", raising=False)
            monkeypatch.setenv("WORKER_NAME", name)
            got = default_worker_id_file()
            assert got == f"/data/workers/{name}/worker.id"
            assert legacy.is_dir()                                    # 原路径升级成目录
            assert Path(got).read_text() == "cpu-abc12345"            # id 不变
            assert default_worker_id_file() == got                    # 幂等
            assert Path(got).read_text() == "cpu-abc12345"
        finally:
            shutil.rmtree(legacy, ignore_errors=True)


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
    """worker 认领并执行独立 AI task(kind='ai'),含白盒审计、错误回执、认领路由。"""

    def _ai_claim(self, task_id="at_1", step="synthesis", domain="dl",
                  provider="claude-cli", model="subscription"):
        return {
            "kind": "ai", "task_id": task_id, "step": step, "pool": "ai", "exec_id": "w:1",
            "request": LLMRequest(messages=[{"role": "user", "content": "Q"}], system="S").to_jsonable(),
            "domain": domain, "provider": provider, "model": model,
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
        assert rec["routing"]["requested"] == {"provider": "claude-cli", "model": "subscription"}
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
    async def test_execute_uses_requested_codex_provider(self, worker, redis, db, monkeypatch):
        seen = {}
        resp = LLMResponse(content="ANSWER", model="subscription", provider="codex-cli",
                           attempts=[{"tier": "primary", "provider": "codex-cli", "ok": True}])

        def fake_gateway(providers, pipelines):
            seen["pipelines"] = pipelines
            return _FakeGateway(resp=resp)

        monkeypatch.setattr("worker.worker.AIGateway", fake_gateway)
        await worker._execute_ai_task(
            self._ai_claim("at_codex", provider="codex-cli", model="subscription")
        )
        primary = seen["pipelines"]["steps"][0]["ai"]["primary"]
        assert primary == {"provider": "codex-cli", "model": "subscription"}
        logs = db.get_ai_task_logs("at_codex")
        assert logs[0]["provider"] == "codex-cli"
        rec = json.loads(logs[0]["record_json"])
        assert rec["routing"]["requested"] == {"provider": "codex-cli", "model": "subscription"}

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

    @pytest.mark.asyncio
    async def test_claim_routes_codex_ai_task_gated_by_tag(self, redis, db, config, storage):
        w = Worker(transport=RedisTransport(redis, db), config=config, storage=storage,
                   worker_type="ai", pools=["ai"], tags={"codex-cli"}, reject_tags=set())
        await w.register()
        await redis.enqueue_ai_task(
            AITask(task_id="at_codex", request=LLMRequest(messages=[]),
                   provider="codex-cli").to_task_payload())
        claim = await request_step(w)
        assert claim is not None and claim["provider"] == "codex-cli"
        assert claim["model"] == "subscription"
        assert claim["require_tags"] == ["codex-cli"]


class TestComputeEffectiveTimeout:
    """长集 whisper 超时随媒体时长伸缩(纯函数)。"""

    def test_no_per_min_returns_base(self):
        assert compute_effective_timeout(1800, None, 6000) == 1800

    def test_no_duration_returns_base(self):
        assert compute_effective_timeout(1800, 90, None) == 1800
        assert compute_effective_timeout(1800, 90, 0) == 1800

    def test_short_audio_uses_base_floor(self):
        # 10 分钟 * 90 = 900 < 1800 下限,返回 1800.
        assert compute_effective_timeout(1800, 90, 600) == 1800

    def test_long_audio_scales(self):
        # 90 分钟 * 90 = 8100 > 1800,返回 8100.
        assert compute_effective_timeout(1800, 90, 90 * 60) == 8100

    def test_rounds_up_partial_minute(self):
        # 89.5 分钟按 ceil=90 计算,返回 8100.
        assert compute_effective_timeout(1800, 90, 89.5 * 60) == 8100

    def test_cap_clamps(self):
        # 10h * 90 = 54000,但 cap=21600,返回 21600.
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
    """worker token 被拒后 fail-fast,不得用 registration token 自动复活。"""

    @pytest.mark.asyncio
    async def test_auth_failure_sets_shutdown_without_reregister(self, worker, monkeypatch):
        calls = []
        async def fake_register():
            calls.append(1)
        monkeypatch.setattr(worker, "register", fake_register)

        await worker._handle_auth_failure()

        assert worker._shutdown
        assert worker._fatal_error is not None
        assert calls == []

    @pytest.mark.asyncio
    async def test_auth_failure_handler_is_idempotent(self, worker, monkeypatch):
        calls = []
        async def fake_register():
            calls.append(1)
        monkeypatch.setattr(worker, "register", fake_register)
        await worker._handle_auth_failure()
        await worker._handle_auth_failure()
        assert worker._shutdown
        assert calls == []

    @pytest.mark.asyncio
    async def test_claim_loop_stops_on_auth_rejected(self, worker):
        from worker.transport import WorkerAuthRejected

        async def reject(*_a, **_k):
            raise WorkerAuthRejected()

        worker.transport.request_step = reject
        await worker._claim_loop()
        assert worker._shutdown

    @pytest.mark.asyncio
    async def test_execute_auth_rejected_stops_claim_loop(self, worker):
        from worker.transport import WorkerAuthRejected

        async def one_task(*_a, **_k):
            return make_claim()

        async def reject_execute(_claim):
            raise WorkerAuthRejected()

        worker.transport.request_step = one_task
        worker.execute = reject_execute
        await worker._claim_loop()
        assert worker._shutdown

    @pytest.mark.asyncio
    async def test_execute_auth_rejected_skips_release(self, worker, tmp_jobs_dir):
        from worker.transport import WorkerAuthRejected

        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def reject_run_step(ctx, on_progress, on_tick):
            await on_tick()
            return 0, ""

        release_calls = []

        async def release(_claim):
            release_calls.append(1)

        async def reject_alive(*_a, **_k):
            raise WorkerAuthRejected()

        worker.runner.run_step = reject_run_step
        worker.transport.report_step_alive = reject_alive
        worker.transport.release = release

        with pytest.raises(WorkerAuthRejected):
            await worker.execute(make_claim())
        assert release_calls == []

    @pytest.mark.asyncio
    async def test_gateway_request_step_raises_authrejected_on_401(self):
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


class TestWorkerRegisterRetry:
    """register() 连不上网关(部署时 api 晚起的启动竞态)→ WARN + 固定间隔重试,不崩进程;
    5xx 退避重试,4xx/auth/contract/config fail-fast。"""

    @pytest.mark.asyncio
    async def test_retries_on_connect_error_then_succeeds(self, worker, monkeypatch, capsys):
        import httpx
        calls = []
        async def flaky_register(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("All connection attempts failed")
            return "io-stable123"
        monkeypatch.setattr(worker.transport, "register", flaky_register)
        monkeypatch.setenv("REGISTER_RETRY_SEC", "0")        # 测试不等
        await worker.register()                               # 不崩
        assert len(calls) == 2                                # 第一次 ConnectError 触发重试,第二次成功
        assert worker.worker_id == "io-stable123"
        assert "register_connect_retry" in capsys.readouterr().out   # 打了 WARN(非整屏 traceback)

    @pytest.mark.asyncio
    async def test_retries_on_5xx_status_then_succeeds(self, worker, monkeypatch, capsys):
        import httpx
        req = httpx.Request("POST", "http://x/api/runner/register")
        resp = httpx.Response(503, request=req)
        calls = []

        async def rejecting_register(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.HTTPStatusError("api restarting", request=req, response=resp)
            return "io-stable456"

        monkeypatch.setattr(worker.transport, "register", rejecting_register)
        monkeypatch.setenv("REGISTER_RETRY_SEC", "0")

        await worker.register()

        assert len(calls) == 2
        assert worker.worker_id == "io-stable456"
        assert "register_http_retry" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_4xx_status_becomes_worker_contract_error(self, worker, monkeypatch):
        import httpx
        from worker.transport import WorkerContractError

        req = httpx.Request("POST", "http://x/api/runner/resume")
        resp = httpx.Response(404, request=req)

        async def rejecting_register(**kw):
            raise httpx.HTTPStatusError("missing endpoint", request=req, response=resp)

        monkeypatch.setattr(worker.transport, "register", rejecting_register)
        monkeypatch.setenv("REGISTER_RETRY_SEC", "0")

        with pytest.raises(WorkerContractError) as got:
            await worker.register()

        assert got.value.status_code == 404
        assert got.value.reason == "worker_register_rejected"


class TestCollectUsageOnFailure:
    """失败也要记用量:rc≠0 / 超时 / 意外异常路径也 collect(失败前完成的 LLM 调用是真实开销)。"""

    def _spy(self, worker):
        calls = []
        async def spy(job_id, step, work_dir):
            calls.append((job_id, step))
        worker._collect_usage = spy
        return calls

    @pytest.mark.asyncio
    async def test_failed_step_still_collects_usage(self, worker, redis, db, tmp_jobs_dir):
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", 3, "w_test:1")
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            return 1, "boom"                      # 步失败
        worker.runner.run_step = mock_run_step
        calls = self._spy(worker)

        await worker.execute(make_claim())
        assert calls == [("j_test_001", "A")]     # 失败路径也 collect
        assert db.get_steps("j_test_001")[0].status == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_still_collects_usage(self, worker, redis, db, tmp_jobs_dir):
        await worker.register()
        db.create_job(make_job())
        db.upsert_step(Step(job_id="j_test_001", name="A", status=StepStatus.READY, pool="cpu"))
        await redis.try_acquire_slot("cpu", 3, "w_test:1")
        (tmp_jobs_dir / "j_test_001").mkdir(exist_ok=True)

        async def mock_run_step(ctx, on_progress, on_tick):
            raise asyncio.TimeoutError()
        worker.runner.run_step = mock_run_step
        calls = self._spy(worker)

        await worker.execute(make_claim())
        assert calls == [("j_test_001", "A")]


class TestAITaskTranscript:
    """AI task 的 agentic 全轨迹内嵌 record_json(transcript 字段);找不到时返回 jsonl=None + reason."""

    def _ai_claim(self, task_id="at_t"):
        return {
            "kind": "ai", "task_id": task_id, "step": "synthesis", "pool": "ai", "exec_id": "w:1",
            "request": LLMRequest(messages=[{"role": "user", "content": "Q"}], system="S").to_jsonable(),
            "domain": None,
        }

    @pytest.mark.asyncio
    async def test_transcript_embedded_in_record(self, worker, redis, db, monkeypatch, tmp_path):
        src = tmp_path / "sess.jsonl"
        src.write_text('{"type":"user"}\n{"type":"assistant"}\n')
        resp = LLMResponse(content="A", model="claude-opus-4-8", provider="claude-cli",
                           session_id="s1", transcript_path=str(src))
        monkeypatch.setattr("worker.worker.AIGateway", lambda p, pl: _FakeGateway(resp=resp))
        await worker._execute_ai_task(self._ai_claim("at_t1"))
        rec = json.loads(db.get_ai_task_logs("at_t1")[0]["record_json"])
        assert rec["transcript"]["jsonl"].startswith('{"type":"user"}')
        assert rec["transcript"]["turns"] == 2 and rec["transcript"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_transcript_missing_records_reason(self, worker, redis, db, monkeypatch):
        resp = LLMResponse(content="A", model="m", provider="claude-cli", transcript_path=None)
        monkeypatch.setattr("worker.worker.AIGateway", lambda p, pl: _FakeGateway(resp=resp))
        await worker._execute_ai_task(self._ai_claim("at_t2"))
        rec = json.loads(db.get_ai_task_logs("at_t2")[0]["record_json"])
        assert rec["transcript"]["jsonl"] is None and "reason" in rec["transcript"]

    def test_load_transcript_truncates_over_cap(self, worker, tmp_path, monkeypatch):
        big = tmp_path / "big.jsonl"
        big.write_text("x" * 100)
        monkeypatch.setattr(type(worker), "_TRANSCRIPT_CAP", 10)   # 缩小上限直测截断
        resp = LLMResponse(content="A", model="m", provider="claude-cli", transcript_path=str(big))
        got = worker._load_ai_task_transcript(resp, [])
        assert got["truncated"] is True and len(got["jsonl"]) == 10

    def test_load_transcript_from_failed_attempts(self, worker, tmp_path):
        # 失败调用:尝试链带 transcript_path(gateway _attempt 透传)同样回收
        src = tmp_path / "fail.jsonl"
        src.write_text('{"e":1}\n')
        got = worker._load_ai_task_transcript(None, [{"tier": "primary", "ok": False,
                                                     "transcript_path": str(src)}])
        assert got["jsonl"] == '{"e":1}\n' and got["turns"] == 1


class TestConfigHotApply:
    """中心配置热应用(docs/03 §1.7.2):rev 幂等、并发热更、缩容槽自退、心跳带回配置。"""

    def test_apply_updates_concurrency_and_ignores_capability_fields(self, worker):
        before_pools = list(worker.pools)
        before_tags = set(worker.tags)
        before_reject_tags = set(worker.reject_tags)
        worker._apply_desired_config({
            "desired_config": {"pools": ["ai"], "concurrency": 4,
                               "tags": ["x"], "reject_tags": []},
            "cfg_rev": 2,
        })
        assert worker.concurrency == 4
        assert worker.pools == before_pools
        assert worker.tags == before_tags
        assert worker.reject_tags == before_reject_tags
        assert worker._cfg_applied_rev == 2

    def test_apply_is_rev_idempotent(self, worker):
        worker._apply_desired_config(
            {"desired_config": {"concurrency": 4}, "cfg_rev": 2})
        worker._apply_desired_config(
            {"desired_config": {"concurrency": 9}, "cfg_rev": 2})   # 同 rev 不再应用
        assert worker.concurrency == 4
        worker._apply_desired_config(
            {"desired_config": {"concurrency": 9}, "cfg_rev": 1})   # 旧 rev 忽略
        assert worker.concurrency == 4
        worker._apply_desired_config(None)                           # 空拍(网络抖动)忽略
        assert worker._cfg_applied_rev == 2

    def test_apply_ignores_pools(self, worker):
        before = list(worker.pools)
        worker._apply_desired_config(
            {"desired_config": {"pools": []}, "cfg_rev": 3})
        assert worker.pools == before

    @pytest.mark.asyncio
    async def test_claim_slot_retires_when_over_concurrency(self, worker):
        worker.concurrency = 1
        # slot 2 超编:循环顶自检直接退位,不认领任何任务
        await asyncio.wait_for(worker._claim_loop(slot=2), timeout=2)

    @pytest.mark.asyncio
    async def test_local_heartbeat_returns_config_payload(self, worker, redis, db):
        await worker.register()
        db.set_worker_desired_config(worker.worker_id, {"concurrency": 3})
        payload = await worker.transport.heartbeat(
            worker.worker_id, applied_cfg_rev=1, concurrency=4)
        assert payload == {"desired_config": {"concurrency": 3}, "cfg_rev": 1}
        info = await redis.get_worker_info(worker.worker_id)
        assert info.get("cfg_applied_rev") == "1"
        assert info.get("concurrency") == "4"
        assert db.get_worker(worker.worker_id).concurrency == 4

    @pytest.mark.asyncio
    async def test_register_applies_initial_config(self, worker, db):
        """最小三参数裸启:注册响应(LocalTransport 属性侧带)即吃到中心配置。"""
        # 预置:同 id 的中心配置已存在(页面此前下发过)
        wid = worker.worker_id
        from shared.models import Worker as _W
        db.upsert_worker(_W(id=wid, type="cpu", pools=["cpu"]))
        db.set_worker_desired_config(wid, {"concurrency": 5, "pools": ["cpu", "gpu"]})
        await worker.register()
        assert worker.concurrency == 5
        assert worker.pools != ["cpu", "gpu"]
        assert worker._cfg_applied_rev == 1
