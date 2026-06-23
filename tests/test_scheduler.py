"""tests for scheduler — 使用 fakeredis + 临时 DB。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.conftest import make_fakeredis
from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, JobStatus, StepStatus, AIUsage
from scheduler.scheduler import Scheduler


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
def simple_pipelines():
    """三步线性 pipeline: A → B → C"""
    return {
        "test": {
            "steps": [
                {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
            ]
        }
    }


@pytest.fixture
def parallel_pipelines():
    """A → {B, C} → D"""
    return {
        "par": {
            "steps": [
                {"name": "A", "pool": "cpu", "depends_on": []},
                {"name": "B", "pool": "cpu", "depends_on": ["A"]},
                {"name": "C", "pool": "io", "depends_on": ["A"]},
                {"name": "D", "pool": "cpu", "depends_on": ["B", "C"]},
            ]
        }
    }


@pytest.fixture
def video_pipelines(configs_dir):
    """使用真实 pipelines.yaml（归一化为内部 step 结构）"""
    from shared.config import load_pipelines
    return load_pipelines(configs_dir / "pipelines.yaml")


@pytest.fixture
def config(tmp_path, tmp_jobs_dir, simple_pipelines, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines=simple_pipelines,
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}}},
        providers={},
    )


def make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines=pipelines,
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}, "scene": {"limit": 1}}},
        providers={},
    )


def _stub_workers_present(s):
    """让 scheduler 视所有 pool 都有 worker，跳过 skip_no_worker 死锁打破逻辑。"""
    async def _has_workers(_pool):
        return True
    s._pool_has_workers = _has_workers
    return s


async def _skip_step(scheduler, redis, db, job_id, step):
    """测试辅助:模拟某步被跳过并触发下游检查(原 scheduler.mark_skipped 仅测试用,已移除)。"""
    await redis.set_step_status(job_id, step, "skipped")
    db.update_step(job_id, step, status="skipped")
    await scheduler._check_downstream(job_id)


@pytest.fixture
def scheduler(redis, db, config):
    return _stub_workers_present(Scheduler(redis, db, config))


def make_job(pipeline="test", job_id="j_test_001"):
    return Job(
        id=job_id,
        content_type="video",
        pipeline=pipeline,
        domain="general",
    )


# ── Tests ──


class TestSubmitJob:
    @pytest.mark.asyncio
    async def test_initializes_all_steps(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        statuses = await redis.get_all_step_statuses("j_test_001")
        assert set(statuses.keys()) == {"A", "B", "C"}

    @pytest.mark.asyncio
    async def test_enqueues_root_steps(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        status_a = await redis.get_step_status("j_test_001", "A")
        assert status_a == "ready"
        status_b = await redis.get_step_status("j_test_001", "B")
        assert status_b == "waiting"

        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_adds_to_active_jobs(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        active = await redis.get_active_jobs()
        assert "j_test_001" in active

    @pytest.mark.asyncio
    async def test_unknown_pipeline_fails_job(self, scheduler, redis, db):
        """Submitting a job with unknown pipeline should mark it as FAILED."""
        job = make_job(pipeline="nonexistent")
        db.create_job(job)
        await scheduler.submit_job(job)

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.FAILED
        active = await redis.get_active_jobs()
        assert "j_test_001" not in active


class TestSkipNoWorker:
    """覆盖 skip_no_worker 死锁打破器（_check_downstream 末段）。
    只在『剩余未完成步骤全部为 ready 且其 pool 无 worker』时介入，并用
    CAS(ready→skipped) 避免覆盖被 worker 抢成 running 的步骤。用真实
    _pool_has_workers，不走 scheduler fixture 的桩。"""

    @pytest.mark.asyncio
    async def test_pool_has_workers_reflects_registration(self, redis, db, config):
        s = Scheduler(redis, db, config)
        assert await s._pool_has_workers("cpu") is False
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        assert await s._pool_has_workers("cpu") is True
        assert await s._pool_has_workers("gpu") is False

    @pytest.mark.asyncio
    async def test_all_ready_no_worker_required_step_not_skipped(self, redis, db, config):
        # 仅剩 A=ready(必需步,无 condition/rules)、其余 done/skipped、cpu 无 worker:
        # 死锁打破器【不】skip 必需步——留给 check_no_worker 超宽限 fail-fast,避免「不完整却显示完成」。
        # (此前行为是把必需步也 skip 掉令 job 收尾,与 pools.yaml fail-fast 意图冲突,审阅报告 B3。)
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"

    @pytest.mark.asyncio
    async def test_running_step_blocks_eager_skip(self, redis, db, config):
        # 存在 running 在途步骤时，no-worker 的 ready 兄弟不被误 skip（守卫核心）
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "done")
        await redis.set_step_status("j_test_001", "B", "running")
        await redis.set_step_status("j_test_001", "C", "ready")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_waiting_step_not_skipped(self, redis, db, config):
        # 仍有 waiting（依赖未满足）属正常等待，不是死锁，不触发 skip
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)  # A=ready, B/C=waiting
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_status("j_test_001", "B") == "waiting"

    @pytest.mark.asyncio
    async def test_ready_won_by_worker_during_skip_not_skipped(self, redis, db, config):
        # CAS 保护：判定无 worker 后该步骤被 worker 抢成 running，skip 应放弃。
        # B3 后死锁打破器只 skip 条件步,故把 A 视为条件步以走到 ready→skipped 的 CAS 路径。
        s = Scheduler(redis, db, config)
        s._step_is_conditional = lambda cfg: True  # 令 A 走条件步 skip 分支(测 CAS 保护)

        async def _no_workers(_pool):
            return False
        s._pool_has_workers = _no_workers

        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")

        real_cas = redis.cas_step_status

        async def _racing_cas(job_id, step, expected, new):
            if step == "A" and expected == "ready" and new == "skipped":
                await redis.set_step_status(job_id, "A", "running")  # worker 抢先
                return False
            return await real_cas(job_id, step, expected, new)
        redis.cas_step_status = _racing_cas

        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_ready_step_survives_when_pool_has_worker(self, redis, db, config):
        s = Scheduler(redis, db, config)
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestNoWorkerFailFast:
    """check_no_worker:无 running 且所有 ready 步的 pool 无 worker、超宽限期 → fail-fast,
    避免未部署 gpu worker 时 audio 永久挂起。用真实 _pool_has_workers。"""

    @pytest.mark.asyncio
    async def test_stuck_job_fails_after_grace(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0  # 立即判定,免等宽限
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)  # A=ready(cpu 无 worker), B/C=waiting
        await s.check_no_worker()
        assert "j_test_001" not in await redis.get_active_jobs()
        assert (await asyncio.to_thread(db.get_job, "j_test_001")).status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_within_grace_not_failed(self, redis, db, config):
        s = Scheduler(redis, db, config)  # 默认宽限 90s
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await s.check_no_worker()  # 首次只记时,不该失败
        assert "j_test_001" in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_not_failed_when_pool_has_worker(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await s.check_no_worker()  # cpu 有 worker → 可推进,不失败
        assert "j_test_001" in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_running_step_not_failed(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await s.check_no_worker()  # 有 running 步 → 在推进,不失败
        assert "j_test_001" in await redis.get_active_jobs()

    async def _bili_dl_job(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir,
                           jid, worker_tags):
        pipelines = {"v": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        cfg = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        s = Scheduler(redis, db, cfg)
        s._NO_WORKER_GRACE_SEC = 0
        await redis.register_worker("w_io", {"type": "io", "pools": "io",
                                             "tags": worker_tags, "status": "idle"})
        job = Job(id=jid, content_type="video", pipeline="v")
        db.create_job(job)
        await redis.init_job(jid, "v", {"source": "bilibili", "url": "https://b23.tv/x"})
        await redis.set_step_status(jid, "01_download", "ready")
        await redis.add_active_job(jid)
        await s.check_no_worker()

    @pytest.mark.asyncio
    async def test_bili_download_no_bili_worker_fails(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir):
        """B6:B站 01_download(require bili)落 io 池但 io worker 无 bili 标签 → 超宽限 fail-fast
        (旧逻辑只看池不看 tag 会误判可推进、永久卡 ready)。"""
        await self._bili_dl_job(redis, db, config, tmp_path, tmp_jobs_dir, configs_dir, "j_nobili", "")
        assert "j_nobili" not in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_bili_download_with_bili_worker_ok(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir):
        """io worker 带 bili 标签 → 满足 require,不 fail-fast。"""
        await self._bili_dl_job(redis, db, config, tmp_path, tmp_jobs_dir, configs_dir, "j_hasbili", "bili")
        assert "j_hasbili" in await redis.get_active_jobs()


class TestMarkdownToText:
    """_markdown_to_text 在入 FTS 索引前剥 HTML 标签,断高亮 snippet XSS 之源。"""

    def test_strips_html_tags(self):
        from scheduler.scheduler import _markdown_to_text
        out = _markdown_to_text("天 <img src=x onerror=alert(1)> 气 <script>bad</script>")
        assert "<" not in out and ">" not in out
        assert "onerror" not in out
        assert "天" in out and "气" in out


class TestDAGProgression:
    @pytest.mark.asyncio
    async def test_linear_chain(self, scheduler, redis, db):
        """A done → B ready → B done → C ready"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_done("j_test_001", "B")

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_parallel_join(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, parallel_pipelines, configs_dir):
        """A → {B, C} → D. D waits for both B and C."""
        config = make_config(tmp_path, tmp_jobs_dir, parallel_pipelines, configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="par")
        db.create_job(job)
        await sched.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await sched.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "ready"
        assert await redis.get_step_status("j_test_001", "D") == "waiting"

        await redis.set_step_status("j_test_001", "B", "running")
        await sched.on_step_done("j_test_001", "B")
        assert await redis.get_step_status("j_test_001", "D") == "waiting"

        await redis.set_step_status("j_test_001", "C", "running")
        await sched.on_step_done("j_test_001", "C")
        assert await redis.get_step_status("j_test_001", "D") == "ready"

    @pytest.mark.asyncio
    async def test_mark_job_done_when_all_complete(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.DONE

        active = await redis.get_active_jobs()
        assert "j_test_001" not in active


class TestSkipPropagation:
    @pytest.mark.asyncio
    async def test_skipped_unblocks_downstream(self, scheduler, redis, db):
        """A done, B skipped → C should become ready (skipped counts as done for deps)."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")

        await _skip_step(scheduler, redis, db, "j_test_001", "B")

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_all_skipped_marks_job_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")
        await _skip_step(scheduler, redis, db, "j_test_001", "B")

        await redis.set_step_status("j_test_001", "C", "running")
        await scheduler.on_step_done("j_test_001", "C")

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.DONE


class TestConditions:
    @pytest.mark.asyncio
    async def test_has_subtitle_true(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "test.srt").write_text("subtitle")

        assert await scheduler.check_condition("j_cond", "has_subtitle") is True
        assert await scheduler.check_condition("j_cond", "no_subtitle") is False

    @pytest.mark.asyncio
    async def test_has_subtitle_false(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond2"
        (job_dir / "input").mkdir(parents=True)

        assert await scheduler.check_condition("j_cond2", "has_subtitle") is False
        assert await scheduler.check_condition("j_cond2", "no_subtitle") is True

    @pytest.mark.asyncio
    async def test_has_danmaku(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond3"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "danmaku.ass").write_text("ass")

        assert await scheduler.check_condition("j_cond3", "has_danmaku") is True

    @pytest.mark.asyncio
    async def test_nonexistent_dir(self, scheduler):
        assert await scheduler.check_condition("j_nodir", "has_subtitle") is False
        assert await scheduler.check_condition("j_nodir", "no_subtitle") is True

    @pytest.mark.asyncio
    async def test_unknown_condition_defaults_true(self, scheduler, tmp_jobs_dir):
        # 文档化契约(scheduler.py:692):未知条件名 → 默认 True(放行,不静默跳过该步)。
        # 配置期无条件名白名单,故此默认是误拼/打错条件名时的安全兜底,值得钉死。
        (tmp_jobs_dir / "j_cond_unknown" / "input").mkdir(parents=True)
        assert await scheduler.check_condition("j_cond_unknown", "bogus_condition") is True

    @pytest.mark.asyncio
    async def test_unknown_condition_returns_true(self, scheduler):
        """Unknown conditions should default to True."""
        result = await scheduler.check_condition("j_any", "some_new_condition")
        assert result is True

    @pytest.mark.asyncio
    async def test_condition_uses_storage_not_local_disk(self, redis, db, config):
        """分布式部署:产物在对象存储、调度器本地盘为空。条件须查 storage,否则
        has_subtitle/has_danmaku 永远 False → 05/06 被误跳、whisper 误跑(本次修复的 bug)。"""
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.list_files.return_value = ["input/subtitle.srt", "input/danmaku.ass", "input/source.mp4"]
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        # 本地 jobs_dir 完全没有该 job 的文件,仅靠 storage 判断
        assert await s.check_condition("j_remote", "has_subtitle") is True
        assert await s.check_condition("j_remote", "has_danmaku") is True
        assert await s.check_condition("j_remote", "no_subtitle") is False
        # 声明式 rules 同源
        assert await s._eval_rules("j_remote", [{"exists": "input/*.srt", "when": "skip"}]) is False
        assert await s._eval_rules("j_remote", [{"exists": "input/*.ass", "when": True}]) is True

    @pytest.mark.asyncio
    async def test_condition_storage_empty_means_absent(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.list_files.return_value = []
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        assert await s.check_condition("j_empty", "has_subtitle") is False
        assert await s.check_condition("j_empty", "no_subtitle") is True


class TestWhisperPunctuateRecheck:
    @pytest.mark.asyncio
    async def test_skipped_revives_when_condition_met(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        """02_whisper generates srt → skipped 08_punctuate revives to ready."""
        pipelines = {
            "video_mini": {
                "steps": [
                    {"name": "01_download", "pool": "io", "depends_on": []},
                    {"name": "02_whisper", "pool": "gpu", "depends_on": ["01_download"],
                     "condition": "no_subtitle", "tags": ["gpu"]},
                    {"name": "08_punctuate", "pool": "ai", "depends_on": ["01_download"],
                     "condition": "has_subtitle"},
                    {"name": "09_mechanical", "pool": "io",
                     "depends_on": ["08_punctuate"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="video_mini")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)

        await redis.set_step_status("j_test_001", "01_download", "running")
        await sched.on_step_done("j_test_001", "01_download")

        assert await redis.get_step_status("j_test_001", "02_whisper") == "ready"
        assert await redis.get_step_status("j_test_001", "08_punctuate") == "skipped"

        (job_dir / "input" / "generated.srt").write_text("whisper output")

        await redis.set_step_status("j_test_001", "02_whisper", "running")
        await sched.on_step_done("j_test_001", "02_whisper")

        assert await redis.get_step_status("j_test_001", "08_punctuate") == "ready"


class TestNewFormatConsumption:
    """调度器消费归一化后的新格式（needs→DAG、rules→skip/run）：行为与旧格式等价。"""

    def _normalized(self, raw_new):
        from shared.config import normalize_pipelines
        return normalize_pipelines(raw_new)

    @pytest.mark.asyncio
    async def test_needs_produce_dag_order(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # needs 声明的依赖边归一化为 depends_on，调度器据此推进 DAG。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "A": {"run": "m.a", "pool": "cpu"},
                    "B": {"run": "m.b", "pool": "cpu", "needs": ["A"]},
                    "C": {"run": "m.c", "pool": "cpu", "needs": ["B"]},
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_status("j_test_001", "B") == "waiting"

        await redis.set_step_status("j_test_001", "A", "running")
        await sched.on_step_done("j_test_001", "A")
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

    @pytest.mark.asyncio
    async def test_rules_exists_skip_and_run(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # rules: exists+when=skip → 有 srt 则跳过 whisper；exists+when=on → 有 srt 才跑 punctuate。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "download": {"run": "m.dl", "pool": "io"},
                    "whisper": {
                        "run": "m.ws", "pool": "gpu", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "skip"}],
                    },
                    "punctuate": {
                        "run": "m.pu", "pool": "ai", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "on"}],
                    },
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "subs.srt").write_text("srt")

        await redis.set_step_status("j_test_001", "download", "running")
        await sched.on_step_done("j_test_001", "download")

        # 已有 srt：whisper(when=skip) 跳过，punctuate(when=on) 运行。
        assert await redis.get_step_status("j_test_001", "whisper") == "skipped"
        assert await redis.get_step_status("j_test_001", "punctuate") == "ready"

    @pytest.mark.asyncio
    async def test_rules_exists_run_when_no_match(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # 无 srt：whisper(when=skip,未命中→默认运行) 运行，punctuate(when=on,未命中→默认运行?) 见下。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "download": {"run": "m.dl", "pool": "io"},
                    "whisper": {
                        "run": "m.ws", "pool": "gpu", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "skip"}],
                    },
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)  # 无 srt

        await redis.set_step_status("j_test_001", "download", "running")
        await sched.on_step_done("j_test_001", "download")

        # 无 srt：whisper 的 skip 规则未命中 → 默认运行。
        assert await redis.get_step_status("j_test_001", "whisper") == "ready"


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_within_limit(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "some error", "processing")

        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "C", "running")
        await scheduler.on_step_failed("j_test_001", "C", "fatal", "processing")

        assert await redis.get_step_status("j_test_001", "C") == "failed"
        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_input_missing_no_retry(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "missing file", "input_missing")

        assert await redis.get_step_status("j_test_001", "A") == "failed"


class TestRetryByFailureType:
    """BUILD（不重试，标 failed）vs SYSTEM（退避重试至上限）的调度器矩阵。
    pipeline_retries 与 RETRY_POLICY.max 取 min：用户配置只能收紧不能放大。"""

    async def _drain_delayed(self, scheduler):
        """ai 类延迟重试经 create_task 异步入队，等其完成再断言。"""
        await asyncio.sleep(0)
        if scheduler._delayed_tasks:
            await asyncio.gather(*list(scheduler._delayed_tasks))

    @pytest.mark.asyncio
    async def test_input_invalid_not_retried(self, scheduler, redis, db):
        # BUILD：input_invalid 确定性失败，A 直接标 failed，job 失败。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "bad json", "input_invalid")

        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert await redis.get_step_retries("j_test_001", "A") == 0
        assert db.get_job("j_test_001").status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_unknown_not_retried(self, scheduler, redis, db):
        # 缺表项 unknown 走 BUILD 兜底：不重试。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "boom", "unknown")

        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert await redis.get_step_retries("j_test_001", "A") == 0

    @pytest.mark.asyncio
    async def test_ai_retried_with_configured_delay(self, scheduler, redis, db):
        # SYSTEM：A(pipeline retries=2) 失败 ai → 重试，且用 policy 的 30s 延迟。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A 入队

        captured = []

        async def mock_delayed(delay, job_id, step):
            captured.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
        await self._drain_delayed(scheduler)

        assert captured == [30]  # ai 首次退避
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1

    @pytest.mark.asyncio
    async def test_ai_capped_by_pipeline_retries(self, scheduler, redis, db):
        # pipeline_retries 封顶：B(retries=1) 把 ai 的 max 3 收紧到 1。
        # 第一次失败 → 重试；第二次失败 → 已达上限 → 标 failed。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        delays = []

        async def mock_delayed(delay, job_id, step):
            delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        # 第一次 ai 失败：current_retries 0 < min(3,1)=1 → 重试。
        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_failed("j_test_001", "B", "5xx", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_retries("j_test_001", "B") == 1

        # 第二次 ai 失败：current_retries 1 >= 1 → 不再重试，标 failed。
        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_failed("j_test_001", "B", "5xx again", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "B") == "failed"
        assert delays == [30]  # 只重试过一次
        assert db.get_job("j_test_001").status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_ai_capped_by_policy_max(self, scheduler, redis, db):
        # policy_max 封顶反向：A 的 pipeline retries=2 仍受 ai 的 max=3 约束，
        # min(3,2)=2，故 A 可重试两次（验证 policy.max 不会被 pipeline 放大）。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")

        delays = []

        async def mock_delayed(delay, job_id, step):
            delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        for expected_retries in (1, 2):
            await redis.set_step_status("j_test_001", "A", "running")
            await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
            await self._drain_delayed(scheduler)
            assert await redis.get_step_retries("j_test_001", "A") == expected_retries

        # 第三次：current_retries 2 >= min(3,2)=2 → 停止重试。
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert delays == [30, 60]  # 两次退避，第三次不再重试


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_duplicate_step_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.dequeue_step("cpu")  # drain A from queue (simulate Worker take)
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")
        await scheduler.on_step_done("j_test_001", "A")  # duplicate — should be no-op

        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # only B, not B+B

    @pytest.mark.asyncio
    async def test_step_done_wrong_status_ignored(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await scheduler.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestOrphanScan:
    @pytest.mark.asyncio
    async def test_reclaims_lost_worker(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-dead")

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        import asyncio
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await scheduler.orphan_scan()
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1
        assert "orphan reclaimed" in received[0]["error"]

    @pytest.mark.asyncio
    async def test_skips_alive_worker(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-alive")
        await redis.register_worker("cpu-alive", {"type": "cpu", "status": "busy"})

        await scheduler.orphan_scan()

        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_reclaim_releases_pool_slot(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """Orphan reclaim should release the pool slot."""
        pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                    {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = make_job()
        db.create_job(job)
        await sched.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-dead")
        await redis.try_acquire_slot("cpu", limit=3)
        assert await redis.get_pool_count("cpu") == 1

        received = []
        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await sched.orphan_scan()
        await asyncio.wait_for(task, timeout=2.0)

        # Pool slot should be released
        assert await redis.get_pool_count("cpu") == 0


class TestCheckStuck:
    @pytest.mark.asyncio
    async def test_detects_stale_progress(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"source": "step", "updated_at": time.time() - 120, "current": 5, "total": 10}
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        import asyncio
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await scheduler.check_stuck()
        await asyncio.wait_for(task, timeout=2.0)

        assert "progress stale" in received[0]["error"]

    @pytest.mark.asyncio
    async def test_ignores_fresh_progress(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"source": "step", "updated_at": time.time(), "current": 5, "total": 10}
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_ignores_no_updated_at(self, scheduler, redis, db, tmp_jobs_dir):
        """Steps without report_progress (no updated_at) are not flagged."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"worker_heartbeat_at": time.time()}
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_worker_heartbeat_prevents_stuck(self, scheduler, redis, db, tmp_jobs_dir):
        """If step has stale updated_at but fresh worker_heartbeat_at, don't flag as stuck."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {
            "updated_at": time.time() - 120,  # stale
            "worker_heartbeat_at": time.time(),  # fresh
        }
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        # Should NOT be flagged as stuck because worker_heartbeat_at is fresh
        assert await redis.get_step_status("j_test_001", "A") == "running"


class TestPriority:
    @pytest.mark.asyncio
    async def test_advanced_job_higher_priority(self, scheduler, redis, db):
        job_a = make_job(job_id="j_advanced")
        job_b = make_job(job_id="j_fresh")
        db.create_job(job_a)
        db.create_job(job_b)
        await scheduler.submit_job(job_a)
        await scheduler.submit_job(job_b)

        # Drain initial A entries from queue (simulate Workers taking them)
        await redis.dequeue_step("cpu")  # j_advanced A or j_fresh A
        await redis.dequeue_step("cpu")  # the other A

        for step in ["A", "B"]:
            await redis.set_step_status("j_advanced", step, "running")
            await scheduler.on_step_done("j_advanced", step)

        await redis.set_step_status("j_fresh", "A", "running")
        await scheduler.on_step_done("j_fresh", "A")

        # j_advanced C (score=-2) should be higher priority than j_fresh B (score=-1)
        item1, score1 = await redis.dequeue_step("cpu")
        assert item1["job_id"] == "j_advanced"
        assert item1["step"] == "C"

        item2, score2 = await redis.dequeue_step("cpu")
        assert score1 < score2  # -2 < -1


class TestRerun:
    @pytest.mark.asyncio
    async def test_resets_downstream(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        for step in ["B", "C"]:
            (job_dir / f".{step}.done").write_text("{}")

        reset = await scheduler.rerun("j_test_001", "B")
        assert set(reset) == {"B", "C"}

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

        assert not (job_dir / ".B.done").exists()
        assert not (job_dir / ".C.done").exists()


class TestRecover:
    @pytest.mark.asyncio
    async def test_recovers_orphaned_running(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-gone")

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        import asyncio
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await scheduler._recover()
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_recovers_ready_steps(self, scheduler, redis, db):
        """If deps are satisfied but step is still waiting, _recover pushes it."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "done")
        await redis.set_step_status("j_test_001", "B", "waiting")

        await scheduler._recover()

        assert await redis.get_step_status("j_test_001", "B") == "ready"


class TestDelayedRetry:
    @pytest.mark.asyncio
    async def test_delayed_enqueue_with_ai_error(self, scheduler, redis, db):
        """error_type="ai" has delay=[30,60,120]. Verify delayed enqueue is triggered."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A

        await redis.set_step_status("j_test_001", "A", "running")

        captured_delays = []

        async def mock_delayed_enqueue(delay, job_id, step):
            captured_delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed_enqueue
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        # _delayed_enqueue 经 create_task 异步运行，等其落盘完成再断言。
        await asyncio.sleep(0)
        await asyncio.gather(*scheduler._delayed_tasks)

        assert captured_delays == [30]
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1


class TestDelayedTaskTracking:
    """覆盖延迟重试任务的跟踪与取消（防泄漏 / shutdown / rerun 串台）。"""

    async def _trigger_delayed(self, scheduler, redis, db, hang):
        """触发一个 ai 延迟重试 task，并用 hang 替换 _delayed_enqueue 控制其存活。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A
        await redis.set_step_status("j_test_001", "A", "running")
        scheduler._delayed_enqueue = hang
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_task_added_to_set(self, scheduler, redis, db):
        async def _never(delay, job_id, step):
            await asyncio.Event().wait()
        await self._trigger_delayed(scheduler, redis, db, _never)
        assert len(scheduler._delayed_tasks) == 1
        task = next(iter(scheduler._delayed_tasks))
        assert task.get_name() == "delayed_enqueue:j_test_001:A"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_delayed_tasks(self, scheduler, redis, db):
        async def _sleep(delay, job_id, step):
            await asyncio.sleep(3600)
        await self._trigger_delayed(scheduler, redis, db, _sleep)
        task = next(iter(scheduler._delayed_tasks))
        await scheduler.shutdown()
        await asyncio.sleep(0)  # 让 done_callback（discard）执行
        assert task.cancelled()
        assert task.done()
        assert len(scheduler._delayed_tasks) == 0

    @pytest.mark.asyncio
    async def test_cancel_is_clean_no_enqueue(self, scheduler, redis, db):
        # 真实 _delayed_enqueue：delay 未到就取消 → enqueue 不发生，A 不被改回 ready
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        await asyncio.sleep(0)
        task = next(iter(scheduler._delayed_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert await redis.get_step_status("j_test_001", "A") != "ready"

    @pytest.mark.asyncio
    async def test_rerun_cancels_pending_delayed(self, scheduler, redis, db, tmp_jobs_dir):
        async def _sleep(delay, job_id, step):
            await asyncio.sleep(3600)
        await self._trigger_delayed(scheduler, redis, db, _sleep)
        task = next(iter(scheduler._delayed_tasks))
        (tmp_jobs_dir / "j_test_001").mkdir(parents=True, exist_ok=True)
        await scheduler.rerun("j_test_001", "A")
        await asyncio.sleep(0)
        assert task.cancelled()
        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestConcurrentCAS:
    """覆盖跨进程并发的 CAS / 去重不变量（fakeredis + gather 验证返回值分支与最终态）。"""

    @pytest.mark.asyncio
    async def test_cas_ready_to_running_only_one_wins(self, redis):
        await redis.set_step_status("j_cas", "A", "ready")
        results = await asyncio.gather(
            redis.cas_step_status("j_cas", "A", "ready", "running"),
            redis.cas_step_status("j_cas", "A", "ready", "running"),
        )
        assert sorted(results) == [False, True]  # 仅一个 worker 抢到 ready→running
        assert await redis.get_step_status("j_cas", "A") == "running"

    @pytest.mark.asyncio
    async def test_duplicate_on_step_done_idempotent(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A 的入队
        await redis.set_step_status("j_test_001", "A", "running")
        await asyncio.gather(
            scheduler.on_step_done("j_test_001", "A"),
            scheduler.on_step_done("j_test_001", "A"),
        )
        # 重复 done 只推进一次：CAS 仅一个成功 → B 仅入队一次
        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_record_ai_usage_exec_id_dedup(self, db):
        u1 = AIUsage(exec_id="e1", provider="kimi", model="k2", job_id="j", step="10_smart")
        u2 = AIUsage(exec_id="e1", provider="kimi", model="k2", job_id="j", step="10_smart")
        results = await asyncio.gather(
            asyncio.to_thread(db.record_ai_usage, u1),
            asyncio.to_thread(db.record_ai_usage, u2),
        )
        assert set(results) == {True, False}  # exec_id UNIQUE → 一成一败


class TestResubmit:
    @pytest.mark.asyncio
    async def test_adds_new_steps(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        # Add step D to pipeline
        new_pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                    {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
                    {"name": "D", "pool": "io", "depends_on": ["C"]},
                ]
            }
        }
        scheduler.config = make_config(tmp_path, tmp_jobs_dir, new_pipelines, configs_dir)
        scheduler.reload_config = lambda: None  # skip file reload

        await scheduler.resubmit("j_test_001")

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "done"
        assert await redis.get_step_status("j_test_001", "C") == "done"
        assert await redis.get_step_status("j_test_001", "D") == "ready"

    @pytest.mark.asyncio
    async def test_removes_deleted_steps(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        # Shrink pipeline: remove C
        new_pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": []},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"]},
                ]
            }
        }
        scheduler.config = make_config(tmp_path, tmp_jobs_dir, new_pipelines, configs_dir)
        scheduler.reload_config = lambda: None

        await scheduler.resubmit("j_test_001")

        assert await redis.get_step_status("j_test_001", "C") is None


class TestRetryFailed:
    @pytest.mark.asyncio
    async def test_retries_from_first_failed(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        await redis.set_step_status("j_test_001", "B", "failed")
        await redis.set_step_status("j_test_001", "C", "failed")

        await scheduler._retry_failed("j_test_001")

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_new_job(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)

        await scheduler._dispatch({"command": "new_job", "job_id": "j_test_001"})

        statuses = await redis.get_all_step_statuses("j_test_001")
        assert "A" in statuses

    @pytest.mark.asyncio
    async def test_dispatch_rerun(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        await scheduler._dispatch({
            "command": "rerun", "job_id": "j_test_001", "from_step": "C",
        })

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_dispatch_retry(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        await redis.set_step_status("j_test_001", "C", "failed")

        await scheduler._dispatch({"command": "retry", "job_id": "j_test_001"})

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_dispatch_step_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler._dispatch({
            "status": "done", "job_id": "j_test_001", "step": "A",
        })

        assert await redis.get_step_status("j_test_001", "A") == "done"

    @pytest.mark.asyncio
    async def test_dispatch_step_failed(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler._dispatch({
            "status": "failed", "job_id": "j_test_001", "step": "A",
            "error": "boom", "error_type": "input_missing",
        })

        assert await redis.get_step_status("j_test_001", "A") == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_action_field(self, scheduler, redis, db):
        """_dispatch should accept 'action' as alias for 'command'."""
        job = make_job()
        db.create_job(job)
        await scheduler._dispatch({"action": "new_job", "job_id": "j_test_001"})
        statuses = await redis.get_all_step_statuses("j_test_001")
        assert "A" in statuses


class TestCalcProgress:
    def test_equal_weight(self, scheduler):
        steps = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        assert scheduler._calc_progress(steps, {"A": "done", "B": "done", "C": "waiting"}) == 67

    def test_custom_weight(self, scheduler):
        steps = [
            {"name": "A", "weight": 1},
            {"name": "B", "weight": 3},
            {"name": "C", "weight": 1},
        ]
        # A done (1) + C skipped (1) = 2 of 5 total
        assert scheduler._calc_progress(
            steps, {"A": "done", "B": "waiting", "C": "skipped"}
        ) == 40

    def test_all_done(self, scheduler):
        steps = [{"name": "A", "weight": 2}, {"name": "B", "weight": 3}]
        assert scheduler._calc_progress(steps, {"A": "done", "B": "done"}) == 100

    def test_none_done(self, scheduler):
        steps = [{"name": "A"}, {"name": "B"}]
        assert scheduler._calc_progress(steps, {"A": "waiting", "B": "waiting"}) == 0


class TestEnqueueTags:
    @pytest.mark.asyncio
    async def test_ai_pool_merges_domain_tags(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """AI pool steps merge static_tags + domain + style_tags into tags, require_tags = static only."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "ai", "depends_on": [], "tags": ["vision"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_tag", content_type="video", pipeline="tagged",
                  domain="deep-learning", style_tags=["lecture", "case-study"])
        db.create_job(job)
        await sched.submit_job(job)

        item, _ = await redis.dequeue_step("ai")
        tags = set(item["tags"])
        assert "vision" in tags        # static
        assert "deep-learning" in tags       # domain
        assert "lecture" in tags       # style_tag
        assert "case-study" in tags    # style_tag
        assert item["require_tags"] == ["vision"]  # only static

    @pytest.mark.asyncio
    async def test_non_ai_pool_no_domain_tags(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """Non-AI pool steps should NOT have domain/style tags — only static tags."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "tags": ["gpu"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_notag", content_type="video", pipeline="tagged",
                  domain="deep-learning", style_tags=["lecture"])
        db.create_job(job)
        await sched.submit_job(job)

        item, _ = await redis.dequeue_step("cpu")
        assert item["tags"] == ["gpu"]        # static only
        assert "deep-learning" not in item["tags"]  # no domain
        assert item["require_tags"] == ["gpu"]

    @pytest.mark.asyncio
    async def test_tags_merge_invalid_style_tags_json(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """Invalid style_tags JSON in Redis should degrade gracefully."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "ai", "depends_on": [], "tags": ["vision"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_badtag", content_type="video", pipeline="tagged", domain="cs")
        db.create_job(job)
        await redis.init_job("j_badtag", "tagged", {"domain": "cs", "style_tags": "not-json"})
        for name in ["A"]:
            await redis.set_step_status("j_badtag", name, "waiting")
        await redis.add_active_job("j_badtag")

        await sched.enqueue_step("j_badtag", "A")

        item, _ = await redis.dequeue_step("ai")
        tags = set(item["tags"])
        assert "vision" in tags
        assert "cs" in tags
        assert item["require_tags"] == ["vision"]

    @pytest.mark.asyncio
    async def test_bili_download_requires_bili(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """B站源的 01_download → require_tags 含 'bili'(硬门控);net-direct 仍进软 tags。"""
        pipelines = {"v": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        job = Job(id="j_bdl", content_type="video", pipeline="v")
        db.create_job(job)
        await redis.init_job("j_bdl", "v", {"source": "bilibili", "url": "https://b23.tv/x"})
        await redis.set_step_status("j_bdl", "01_download", "waiting")
        await redis.add_active_job("j_bdl")
        await sched.enqueue_step("j_bdl", "01_download")
        item, _ = await redis.dequeue_step("io")
        assert "bili" in item["require_tags"]
        assert "net-direct" in item["tags"]

    @pytest.mark.asyncio
    async def test_arxiv_download_no_bili(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """非 B站 net-direct 源(arxiv)的 01_download 不加 bili require。"""
        pipelines = {"p": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        job = Job(id="j_adl", content_type="paper", pipeline="p")
        db.create_job(job)
        await redis.init_job("j_adl", "p", {"source": "arxiv", "url": "https://arxiv.org/abs/1"})
        await redis.set_step_status("j_adl", "01_download", "waiting")
        await redis.add_active_job("j_adl")
        await sched.enqueue_step("j_adl", "01_download")
        item, _ = await redis.dequeue_step("io")
        assert "bili" not in item["require_tags"]


class TestCleanupStaleWorkers:
    @pytest.mark.asyncio
    async def test_dead_worker_deleted_alive_marked_offline(self, scheduler, redis, db):
        """DB 心跳超时 + Redis 注册已过期 -> 删除；Redis 仍在 -> 标 offline。"""
        from datetime import datetime, timedelta, timezone
        from shared.models import Worker as WorkerModel

        # 用 aware UTC(与 cleanup_stale_workers 的 datetime.now(timezone.utc) 同基准);
        # 此前用 naive datetime.now() 在非 UTC 宿主上会把心跳算成「未来」,致 stale 永假。
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        # dead: DB 过期，Redis 无注册
        db.upsert_worker(
            WorkerModel(id="cpu-dead", type="cpu", status="busy",
                        first_seen=old, last_heartbeat=old)
        )
        # alive-but-stale: DB 过期，但 Redis 仍有注册键
        db.upsert_worker(
            WorkerModel(id="cpu-alive", type="cpu", status="idle",
                        first_seen=old, last_heartbeat=old)
        )
        await redis.register_worker("cpu-alive", {"type": "cpu", "pools": "cpu"}, ttl=30)

        await scheduler.cleanup_stale_workers(timeout_sec=60)

        assert db.get_worker("cpu-dead") is None  # 真死了 -> 删除
        alive = db.get_worker("cpu-alive")
        assert alive is not None
        assert alive.status == "offline"

    @pytest.mark.asyncio
    async def test_fresh_worker_untouched(self, scheduler, redis, db):
        from datetime import datetime, timezone
        from shared.models import Worker as WorkerModel

        now = datetime.now(timezone.utc)
        db.upsert_worker(
            WorkerModel(id="cpu-fresh", type="cpu", status="idle",
                        first_seen=now, last_heartbeat=now)
        )
        await scheduler.cleanup_stale_workers(timeout_sec=60)
        got = db.get_worker("cpu-fresh")
        assert got is not None
        # 刚心跳的 worker 不应被回收；公共状态衍生为 online-idle
        assert got.status == "online-idle"

    @pytest.mark.asyncio
    async def test_aware_now_minus_legacy_naive_heartbeat_no_crash(
        self, scheduler, redis, db
    ):
        """UTC 迁移核心回归：cleanup 用 aware now 减去旧库 naive 心跳不能崩
        ('can't subtract offset-naive and offset-aware')，且仍正确判旧 worker stale。"""
        # 直接写一个 naive 心跳的旧行（绕过模型默认值，模拟历史数据）
        db._conn.execute(
            "INSERT INTO workers (id, type, status, first_seen, last_heartbeat) "
            "VALUES (?,?,?,?,?)",
            ("cpu-legacy", "cpu", "busy", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db._conn.commit()
        # Redis 无注册键 -> 视为真死 -> 删除
        await scheduler.cleanup_stale_workers(timeout_sec=60)
        assert db.get_worker("cpu-legacy") is None


class TestStaleExecGuard:
    """H3:孤儿重排后旧执行的迟到完成/失败事件按 exec_id 丢弃,不顶替当前在跑实例。"""

    @pytest.mark.asyncio
    async def test_stale_exec_done_ignored(self, redis, db, config):
        s = _stub_workers_present(Scheduler(redis, db, config))
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_exec_id("j_test_001", "A", "exec_2")
        # 旧执行 exec_1 迟到上报完成 → 应被忽略
        await s.on_step_done("j_test_001", "A", exec_id="exec_1")
        assert await redis.get_step_status("j_test_001", "A") == "running"
        # 当前执行 exec_2 上报 → 正常置 done
        await s.on_step_done("j_test_001", "A", exec_id="exec_2")
        assert await redis.get_step_status("j_test_001", "A") == "done"

    @pytest.mark.asyncio
    async def test_no_exec_record_backward_compatible(self, redis, db, config):
        # 未写 exec_id(旧库) → 不过滤,按原 CAS 行为置 done
        s = _stub_workers_present(Scheduler(redis, db, config))
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await s.on_step_done("j_test_001", "A", exec_id="exec_x")
        assert await redis.get_step_status("j_test_001", "A") == "done"


class TestOrphanClaimMismatch:
    """H4:在跑步骤的 worker 存活但其上报 current_step 不是本步(认领响应丢失),
    超宽限期回收,修永久卡 running。"""

    @pytest.mark.asyncio
    async def test_reclaim_when_worker_not_running_this_step(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._CLAIM_MISMATCH_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle",
                   "current_job": "", "current_step": ""},
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()
        assert ("j_test_001", "A") in calls

    @pytest.mark.asyncio
    async def test_no_reclaim_when_worker_runs_this_step(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._CLAIM_MISMATCH_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "busy",
                   "current_job": "j_test_001", "current_step": "A"},
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()
        assert calls == []  # current_step 匹配 → 不回收

    @pytest.mark.asyncio
    async def test_within_grace_not_reclaimed(self, redis, db, config):
        s = Scheduler(redis, db, config)  # 默认宽限 30s
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle", "current_step": ""},
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()  # 首次只记时,不回收
        assert calls == []
